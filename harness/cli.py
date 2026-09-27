"""Terminal REPL for the harness.

This is the "terminal-based (CLI) application" the brief requires
(Section 7.1) and the entry point the container starts. Running the
container drops the user straight into the `agent` mode: the full ReAct
loop with the built-in file tools.

``print`` lives here and nowhere else: ``AGENTS.md`` bans it in library code
so that the loop and tools stay usable from tests, sub-agents, and any
future non-terminal front end.
"""

import argparse
import sys
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

try:
    # Importing this is what makes input() support arrow-key history and
    # in-line editing at all — without it, pressing Up at the "You:" prompt
    # prints a raw escape sequence instead of recalling the previous line,
    # which is the single biggest reason a plain input()-based REPL feels
    # broken to use. Not every platform ships GNU readline (Windows CPython
    # does not), so this degrades to "no history, still works" rather than
    # failing the whole harness over a terminal convenience.
    import readline
except ImportError:  # pragma: no cover - platform without GNU readline
    readline = None  # type: ignore[assignment]


from harness import __version__, console
from harness.budget import (
    BudgetExceeded,
    BudgetStatus,
    PromptBudgetExceeded,
)
from harness.config import Settings, load_settings, reject_nested_roots
from harness.console import Sgr, style
from harness.llm import LLMError
from harness.mcp import McpDiscovery
from harness.memory import (
    SESSIONS_FILENAME,
    MemoryStore,
)
from harness.memory.protocol import MemoryUnavailableError
from harness.memory.records import parse_slot
from harness.memory.scratchpad import NOT_PERSISTED
from harness.observability import (
    as_trace_id,
)
from harness.observability.evals import (
    FAILURE_REASONS,
    QualityStore,
    RunQuality,
    suggest_reason,
)
from harness.observability.replay import (
    TraceNotFoundError,
    find_trace,
    list_traces,
    load_trace,
    render_list,
    render_run,
)
from harness.permissions import (
    resolve_mode,
)
from harness.prompts import (
    AGENT_SYSTEM_PROMPT,
    DATE_SECTION_HEADER,
    today_section,
)
from harness.session import HarnessSession, migration_note
from harness.skills import (
    SkillLibrary,
)
from harness.tools import ToolRegistry

# Typed once so the REPL and the help text cannot drift apart.
EXIT_COMMANDS = frozenset({"/exit", "/quit", "exit", "quit"})

# What a person types to pick a stopped task back up. Plain words rather than
# a slash command on purpose: the harness has just told them in a sentence
# that they can continue, and answering a sentence with a sentence is what
# anyone does without being taught (ADR-089).
CONTINUE_WORDS = frozenset({"continue", "carry on", "keep going", "go on", "resume"})

# What a person types to accept the overspend. Longer and less accidental
# than "continue", because this one costs money that was not budgeted: it
# has to be said, not stumbled into.
CREDIT_WORDS = frozenset(
    {
        "use credit on the go",
        "use credit",
        "on credit",
        "continue on credit",
        "use the credit",
    }
)

# The nudge that resumes a task. Phrased as the user speaking, because that
# is whose turn it is, and kept short: the conversation already holds the
# original instruction and every observation so far, so repeating the task
# would only spend tokens re-describing what the model can already see.
RESUME_TASK = (
    "Continue the previous task from where you stopped. Do not start over, "
    "and do not repeat work that is already done."
)

# Records ``/memory`` prints. A preview, not a dump: the command answers
# "did that persist", and a collection with four hundred facts should not
# fill the terminal to say yes.
MEMORY_PREVIEW_LINES = 20

# Where REPL input history is kept, under the persistent memory root so it
# survives across container runs the same way traces do (a sibling, not a
# subdirectory of ./memory itself, to keep it out of anything long-term
# memory or a trace reader iterates over). Capped, because an unbounded
# history file is a slow-motion leak in a volume nobody is watching.
HISTORY_FILENAME = ".repl_history"
HISTORY_MAX_ENTRIES = 1000


def _usage_line(
    turn_prompt: int, turn_completion: int, session_total: int
) -> str | None:
    """Return a trace line for this turn's token usage, or ``None``.

    Both counts at zero means the endpoint omitted ``usage``. We do not
    invent a substitute (ADR-031), so the line is skipped rather than
    printed as zeros or as an estimate.

    Args:
        turn_prompt: Prompt tokens billed for this turn.
        turn_completion: Completion tokens billed for this turn.
        session_total: Cumulative prompt plus completion for the session.

    Returns:
        A single indented line, or ``None`` when the endpoint reported nothing.
    """
    if turn_prompt <= 0 and turn_completion <= 0:
        return None
    return (
        f"  tokens: {turn_prompt:,} prompt + {turn_completion:,} completion "
        f"(session {session_total:,})"
    )


def _budget_line(status: BudgetStatus) -> str:
    """Return the line printed after every answer saying where the day stands.

    Printed whether or not the trace is on, unlike the tokens line: the
    tokens line is diagnostics, this is the user's own allowance, and a
    ``--quiet`` run that silently hits the limit would be the worst way to
    find out it exists. Yellow past the warning threshold, red when the
    next call will be refused.

    Args:
        status: Today's usage against the limit.

    Returns:
        One indented line.
    """
    percent = int(status.fraction_used * 100)
    # "resets at" is only meaningful once the window has started, and it
    # starts on the first charged prompt rather than at midnight (ADR-089).
    resets = (
        status.resets_at.strftime("%H:%M %Z")
        if status.resets_at is not None
        else "on first use"
    )
    line = (
        f"  budget: {status.used_tokens:,} / {status.limit_tokens:,} tokens "
        f"in 24h ({percent}%) · {status.remaining_tokens:,} left · resets {resets}"
    )
    if status.credit_tokens:
        line = f"{line} · {status.credit_tokens:,} on credit"
    if status.exhausted and status.on_credit:
        return style(f"{line} · ON CREDIT", Sgr.YELLOW)
    if status.exhausted:
        return style(f"{line} · EXHAUSTED", Sgr.RED)
    if status.warning:
        return style(f"{line} · warning", Sgr.YELLOW)
    return line


def _banner(
    settings: Settings,
    registry: ToolRegistry,
    skills: SkillLibrary | None = None,
    mcp: McpDiscovery | None = None,
    store: MemoryStore | None = None,
    metrics_note: str | None = None,
    traces_note: str | None = None,
    subagents_note: str | None = None,
    telemetry_notes: list[str] | None = None,
    budget_note: str | None = None,
    notes_note: str | None = None,
    perms_note: str | None = None,
) -> None:
    """Print the startup header.

    Deliberately states every root and the tool list: the user is about to
    hand an LLM write access to directories, and the first thing they should
    see is exactly which ones and exactly what it can do. Memory earns its
    line because it is the one root whose contents outlive the container.

    Args:
        settings: Loaded settings.
        registry: The tool registry the agent will use.
        skills: Discovered skills, if the library was loaded.
        mcp: Connected MCP servers, if any started.
        store: Long-term memory, if enabled. Used only for its
            self-description line.
        metrics_note: Where ``/metrics`` is being served, or why it is not.
            ``None`` when metrics are disabled.
        traces_note: Where run traces are written. ``None`` when disabled.
        subagents_note: Which specialists this session can delegate to.
            ``None`` when delegation is switched off.
        telemetry_notes: Extra lines about the telemetry exporters — an
            OTLP collector that was configured, or one whose packages
            are missing, or a redaction mode that is not the default.
            Printed rather than swallowed for the same reason the
            metrics line is: an exporter that silently does nothing is
            how a demo ends up with an empty trace backend and nobody
            noticing (ADR-095).
        notes_note: Where the agent's short-term working notes live.
        budget_note: Where today's spend stands against the daily limit.
            ``None`` when the budget is switched off.
        perms_note: Appended to the mode line — how ``auto`` was resolved,
            so the person can see the file said one thing and the session
            is doing another, and why.
    """
    print(style(f"Agent Harness {__version__}", Sgr.BOLD))
    print("Mode:      agent")
    print(
        f"Model:     {settings.llm_model} via {settings.llm_provider} "
        f"(temperature {settings.temperature})"
    )
    print(f"Workspace: {settings.workspace_path}")
    # Stated up front for the same reason the roots are: the user is about
    # to hand an LLM write access, and what will and will not be asked about
    # first is part of what they are agreeing to.
    print(f"Perms:     {settings.permission_mode}{perms_note or ''}")
    if settings.memory_enabled and store is not None:
        # The backend line rather than a path, because the two silent
        # failure modes of vector memory — no database, and a fallback
        # embedder — are invisible otherwise, and a demo should not
        # discover either of them by getting bad recall (ADR-069).
        print(f"Memory:    {store.describe}")
        # ``describe`` has just connected, which is when a rebuild happens.
        rebuilt = migration_note(store)
        if rebuilt:
            print(style(f"           {rebuilt}", Sgr.YELLOW))
    else:
        print("Memory:    disabled")
    # Printed whether it worked or not. A metrics line that silently
    # vanishes when the port is taken is how a demo ends up scraping
    # nothing and nobody noticing until the dashboard is empty.
    print(f"Metrics:   {metrics_note or 'disabled'}")
    # The trace is the file a person opens after a run goes wrong, so where
    # it is belongs on the same screen as everything else they will look at.
    print(f"Traces:    {traces_note or 'disabled'}")
    # On the same screen as the roots and the permission mode, because it is
    # the same kind of fact: the user is being told that some of the work may
    # be carried out by another agent, under narrower permissions than this
    # one, before they type anything.
    print(f"Sub-agents: {subagents_note or 'disabled'}")
    for note in telemetry_notes or []:
        print(note)
    # Before the first prompt, because a limit the user only learns about
    # when it stops them is a limit they will assume is a bug.
    print(f"Budget:    {budget_note or 'disabled'}")
    print(f"Notes:     {notes_note or 'disabled'}")
    if skills is not None and len(skills):
        print(f"Skills:    {', '.join(skills.names())}")
    if mcp is not None and mcp.clients:
        # Named, with a tool count: the user is being told that some of
        # the tools below are executed by another process.
        servers = ", ".join(client.server_name for client in mcp.clients)
        print(f"MCP:       {servers} ({len(mcp.tools)} remote tools)")
    if len(registry):
        print(f"Tools:     {', '.join(registry.names())}")
        print("           deletion is not available by design")
    else:
        print("Tools:     none")
    print(
        "Commands:  /exit  /reset  /tools  /remember  /memory  "
        "/trace  /budget  /eval  /help"
    )
    print()


def _help() -> None:
    """Print the REPL command list."""
    print("  /exit           quit the harness")
    print("  /reset          forget the conversation so far")
    print("  /tools          list the registered tools")
    print("  /remember TEXT  keep a fact for future sessions")
    print("  /memory         show what is stored in long-term memory")
    print("  /trace [ID]     replay the last run, or the run whose id starts with ID")
    print("  /trace list     list every recorded run, newest first")
    print("  /eval ok|fail   record whether the last run did what you asked")
    print("  /budget         show today's token spend against the daily limit")
    print("  /help           show this list")


class Repl(HarnessSession):
    """The interactive session: a :class:`HarnessSession` a person talks to.

    Adds what only a terminal needs — reading lines, slash commands, the
    printed answer and budget line, command history — on top of the session
    :class:`~harness.session.HarnessSession` assembles.
    """

    def __init__(self, settings: Settings, trace: bool) -> None:
        """Build the session, then the terminal conveniences around it.

        Args:
            settings: Validated settings.
            trace: Whether to print the per-tool trace.
        """
        super().__init__(settings, trace)
        self._history_path: Path | None = None
        self._setup_readline(settings)

    def close(self) -> None:
        """Close the session, then save the command history."""
        super().close()
        if readline is not None and self._history_path is not None:
            try:
                self._history_path.parent.mkdir(parents=True, exist_ok=True)
                readline.write_history_file(self._history_path)
            except OSError:
                # A read-only memory mount is a supported configuration
                # (the file tools do not depend on it); losing history on
                # exit is a worse outcome than refusing to close cleanly.
                pass

    def _setup_readline(self, settings: Settings) -> None:
        """Enable command history and slash-command completion.

        Best-effort and silent on failure throughout: a read-only history
        file, a fresh checkout with no history yet, or a platform with no
        GNU readline (the top-of-module import already handled that case)
        must degrade to "no history", never crash a session that would
        otherwise run fine.

        Args:
            settings: Validated settings; ``memory_path`` is where the
                history file lives, alongside traces, so it survives across
                container runs on the same named volume (ADR-083's
                reasoning applies here too).
        """
        if readline is None:
            return
        # Cleared first: readline's history is process-global state, not
        # owned by this object, so a second Repl built in the same process
        # (the test suite builds dozens) must not inherit the first one's
        # lines.
        readline.clear_history()
        self._history_path = settings.memory_path / HISTORY_FILENAME
        try:
            if self._history_path.is_file():
                readline.read_history_file(self._history_path)
        except OSError:
            pass
        readline.set_history_length(HISTORY_MAX_ENTRIES)

        # Slash commands only: the task itself is free text with nothing to
        # complete against, and offering EXIT_COMMANDS's bare "exit"/"quit"
        # forms alongside real words would complete into the wrong thing
        # more often than it helps.
        commands = sorted(
            {
                "/exit",
                "/quit",
                "/reset",
                "/tools",
                "/remember",
                "/memory",
                "/trace",
                "/budget",
                "/eval",
                "/help",
            }
        )

        def _complete(text: str, state: int) -> str | None:
            matches = [c for c in commands if c.startswith(text)]
            return matches[state] if state < len(matches) else None

        # Python's readline binding defaults its word-break characters to a
        # wide punctuation set that includes "/" — so without this, "/rem"
        # + Tab asks the completer to match "rem" against a command list
        # that all start with "/", and nothing ever matches. Narrowing the
        # delimiters to whitespace makes "/remember" one word, the way a
        # user typing a slash command expects it to behave.
        readline.set_completer_delims(" \t\n")
        readline.set_completer(_complete)
        readline.parse_and_bind("tab: complete")

    def run(self) -> int:
        """Read, evaluate, print, loop.

        Returns:
            Process exit code: 0 for a clean exit.
        """
        while True:
            try:
                raw = input("You: ").strip()
            except (EOFError, KeyboardInterrupt):
                # Ctrl-D / Ctrl-C is a normal way to leave a REPL, not a
                # crash. A bare newline keeps the shell prompt tidy.
                print()
                return 0

            if not raw:
                continue
            if raw.lower() in EXIT_COMMANDS:
                return 0
            if raw.startswith("/"):
                self._command(raw)
                continue

            lowered = raw.lower().strip(" .!")
            if lowered in CREDIT_WORDS:
                self._take_credit()
                continue
            if lowered in CONTINUE_WORDS and self._resumable:
                self.ask(RESUME_TASK, resuming=True)
                continue

            self.ask(raw)

    def ask(self, task: str, *, resuming: bool = False) -> None:
        """Run one user task through the active loop and print the answer.

        Args:
            task: Raw user input.
            resuming: Whether this is a ``continue`` picking up a task the
                per-prompt allowance stopped. The conversation is not
                cleared either way — the difference is that a resumed task
                gets a fresh per-prompt allowance and does not clear the
                flag that let it be resumed until it finishes.
        """
        loop = self._agent_loop
        # A fresh per-prompt allowance for every instruction, and for every
        # `continue`. That is the whole mechanism: the checkpoint is not a
        # ceiling, it is a question asked once per allowance (ADR-089).
        if self._budget is not None:
            self._budget.start_prompt()
        self._context.ensure_system_prompt(AGENT_SYSTEM_PROMPT)
        # Refreshed per task, not once per session. A REPL left open over
        # midnight would otherwise keep telling the model it is yesterday,
        # and "tomorrow" would quietly mean today (ADR-079).
        self._context.set_system_section(
            DATE_SECTION_HEADER,
            today_section(datetime.now(ZoneInfo(self._settings.timezone))),
        )
        self._context.start_turn(task)
        # Deliberately no `self._active_skills.reset()` here. A skill's body
        # stays in the conversation after the turn that read it, and the
        # model keeps following it, so the restriction has to stay with it.
        # Resetting per turn let anyone lift the gate by asking again; the
        # way out is /reset, which clears both together (ADR-082).
        prompt_before = self._context.prompt_tokens
        completion_before = self._context.completion_tokens
        try:
            # The answer is printed by ConsoleTraceHook at run end, before
            # run-end memory work, so the return value is not needed here.
            loop.run(self._context)
        except PromptBudgetExceeded as exc:
            # Before ``BudgetExceeded``, which is a sibling rather than a
            # parent, and before ``LLMError``, which is the parent of both.
            # Not a failure: the task outgrew one allowance and the work so
            # far is in the conversation, so the run can be picked up
            # verbatim by typing `continue` (ADR-089).
            self._resumable = True
            print(style(f"Paused: {exc}", Sgr.YELLOW))
            self._print_budget_line()
            print()
            return
        except BudgetExceeded as exc:
            # Before ``LLMError``, which it is a subclass of. Not an error in
            # the endpoint sense — the harness did exactly what it was
            # configured to do — so it is named as a budget stop, and the
            # standing is printed underneath so the user sees the number
            # that caused it. The REPL stays alive: ``/budget`` still works,
            # and so does a config edit followed by a restart.
            # The window is spent. Resumable too, but only after the user
            # takes the run onto credit — which is a separate sentence
            # precisely so it is a decision and not a reflex.
            self._resumable = True
            print(style(f"Budget stop: {exc}", Sgr.RED))
            self._print_budget_line()
            print()
            return
        except LLMError as exc:
            # An endpoint failure is the user's problem to fix (bad token,
            # wrong model, network), so it is reported plainly and the REPL
            # stays alive rather than exiting and losing the conversation.
            print(style(f"LLM error: {exc}", Sgr.RED))
            return
        except KeyboardInterrupt:
            # Ctrl-C during an LLM wait should cancel this turn, not kill
            # the process and dump a traceback. The conversation survives.
            print("\nInterrupted.\n")
            return
        # The task reached an answer, so there is nothing left to resume and
        # "continue" goes back to meaning whatever the model makes of it.
        self._resumable = False
        self._print_trace_link()
        if self._trace:
            usage = _usage_line(
                self._context.prompt_tokens - prompt_before,
                self._context.completion_tokens - completion_before,
                self._context.total_tokens,
            )
            if usage is not None:
                print(usage)
        # Not gated by ``--quiet``: the trace is diagnostics, the budget is
        # the user's allowance, and it is shown after every prompt so the
        # day's standing is never a surprise (ADR-088).
        self._print_budget_line()
        print()

    def _print_trace_link(self) -> None:
        """Print a link to this run's trace in the viewer, when exporting.

        Run ids are minted per turn, after the banner, so nothing else on
        screen names the run a person would look for. The trace id *is* the
        run id (ADR-095), so the link can be built here without asking the
        viewer anything (ADR-111).
        """
        if self._trace_viewer is None or not self._context.run_id:
            return
        trace_id = as_trace_id(self._context.run_id)
        print(style(f"  trace: {self._trace_viewer}/trace/{trace_id}", Sgr.DIM))

    def _print_budget_line(self) -> None:
        """Print where today's spend stands, if a budget is configured."""
        if self._budget is not None:
            print(_budget_line(self._budget.status()))

    def _command(self, raw: str) -> None:
        """Handle a ``/`` command.

        Args:
            raw: The full command line, including the leading slash.
        """
        parts = raw.split()
        command = parts[0].lower()

        if command == "/help":
            _help()
        elif command == "/reset":
            self._context.reset()
            # The skill body leaves the context here, so the restriction it
            # opened leaves with it. This is the only place either goes.
            if self._active_skills is not None:
                self._active_skills.reset()
            # The notes belong to the task, and /reset is how a user says
            # they are done with it. Leaving them would inject the last
            # task's state into the next one (ADR-090).
            cleared = ""
            if self._scratchpad is not None:
                outcome = self._scratchpad.clear()
                # Reported rather than assumed: on a read-only volume the
                # old task's notes stay on disk and are injected again on
                # the next run, and "Conversation cleared." would be a
                # sentence that is not true (ADR-105).
                if NOT_PERSISTED in outcome:
                    cleared = style(f"  notes NOT cleared on disk: {outcome}", Sgr.RED)
            print("Conversation cleared.\n")
            if cleared:
                print(f"{cleared}\n")
        elif command == "/tools":
            self._show_tools()
        elif command == "/remember":
            self._remember(raw[len(command) :].strip())
        elif command == "/memory":
            self._show_memory()
        elif command == "/trace":
            self._show_trace(parts[1] if len(parts) > 1 else None)
        elif command == "/eval":
            self._eval_run(raw[len(command) :].strip())
        elif command == "/budget":
            self._show_budget()
        else:
            print(f"Unknown command {command}. Try /help.\n")

    def _show_tools(self) -> None:
        """List every registered tool with its permission tier and why.

        One row per tool — name, risk, reach, tier, source — rather than
        the comma-separated names it used to be. This is the review
        artefact for onboarding a server: the rows whose source is
        ``reach`` or ``risk`` are the ones nobody has written a rule for
        yet, and a reviewer can walk them in one screen (ADR-102).
        """
        rows = []
        for name in self._registry.names():
            tool = self._registry.get(name)
            decision = self._tiers.tier_of(tool)
            rows.append((name, tool.risk, tool.reach, decision.tier, decision.source))
        width = max(len(row[0]) for row in rows) if rows else 4
        print(f"  {'tool':<{width}}  risk     reach       tier   source")
        for name, risk, reach, tier, source in rows:
            print(f"  {name:<{width}}  {risk:<8} {reach:<11} {tier:<6} {source}")
        print(
            "  tier: allow runs; ask prompts in confirm_mutations; deny never runs."
            " deletion is not available by design\n"
        )

    def _take_credit(self) -> None:
        """Let this session spend past the window allowance, and say so.

        Session-scoped by design: the *spend* is persisted so tomorrow can
        see what was borrowed, but the *permission* is not. A consent flag
        that survived a restart would quietly disarm the budget for every
        run after it, which is the opposite of what someone asking for one
        more task means (ADR-089).
        """
        if self._budget is None:
            print("  budget: disabled, so there is nothing to put on credit\n")
            return
        already = self._budget.on_credit
        self._budget.allow_credit()
        if already:
            print("  already running on credit.")
        else:
            print(
                style(
                    "  Credit enabled for this session. Spend past the limit "
                    "is recorded separately and counts against the window "
                    "when it next resets.",
                    Sgr.YELLOW,
                )
            )
        self._print_budget_line()
        if self._resumable:
            print("  Type 'continue' to pick the stopped task back up.")
        print()

    def _show_budget(self) -> None:
        """Print today's spend, the limit, and where the total is kept."""
        if self._budget is None:
            print("  budget: disabled (budget_enabled: false)\n")
            return
        status = self._budget.status()
        usage = self._budget.ledger.usage()
        print(_budget_line(status))
        print(
            f"  {usage.prompt_tokens:,} prompt + {usage.completion_tokens:,} "
            f"completion in this window, every session on this volume included"
        )
        if status.prompt_limit_tokens:
            print(
                f"  this instruction: {status.prompt_used_tokens:,} / "
                f"{status.prompt_limit_tokens:,} tokens "
                f"(say 'continue' for a fresh allowance)"
            )
        if usage.credit_tokens:
            print(f"  {usage.credit_tokens:,} of that was spent on credit")
        print(f"  ledger: {self._budget.ledger.describe}")
        if status.resets_at is not None:
            when = status.resets_at.strftime("%H:%M %Z on %Y-%m-%d")
            print(f"  window resets at {when}")
        else:
            print("  the 24-hour window starts with the next charged prompt")
        print("  raise budget_daily_tokens in config.yaml to change the limit\n")

    def _remember(self, text: str) -> None:
        """Record a fact the user stated, without going through the agent.

        The user-driven write trigger. It exists because the most reliable
        source of a durable fact is the person stating it: routing "always
        answer in German" through the agent first adds a turn, a cost, and a
        chance of the model paraphrasing it into something else.

        The fact is stored with ``source="user"``, which is what makes it
        un-overwritable by the agent — for the four slotted keys (ADR-063)
        and, through the writer's provenance rank, for every other fact
        (ADR-107). It is also how the user corrects a fact: the writer
        supersedes whatever it replaces, whoever recorded that. With
        consolidation on, that comparison is one judge call when similar
        facts exist; the user's own words are what gets stored.

        Args:
            text: The fact, as typed after the command.
        """
        # Both or neither: the writer is built whenever the store is.
        if self._store is None or self._memory_writer is None:
            print("Memory is disabled (memory_enabled: false in config.yaml).\n")
            return
        if not text:
            print("Usage: /remember <fact to keep for later sessions>\n")
            return
        slot = parse_slot(text)
        replaced = None
        try:
            if slot is not None:
                record = self._store.upsert_slot(slot[0], slot[1], source="user")
            else:
                outcome = self._memory_writer.write(text, "core_fact", "user")
                record, replaced = outcome.record, outcome.replaced
        except (ValueError, MemoryUnavailableError) as exc:
            print(f"Could not record that: {exc}\n")
            return
        # Says what was kept, because the value of the feature is entirely
        # in the user trusting that it persisted. A slotted second write is
        # a replacement, not a second record, and so is a correction.
        verb = f"Set {record.slot_key}" if record.slot_key else "Recorded"
        print(f"{verb} in long-term memory: {record.text}\n")
        if replaced is not None:
            print(f"  (replaces: {replaced.text})\n")

    def _show_memory(self) -> None:
        """Print what is stored, so persistence is verifiable without the model.

        Deliberately reads the store rather than the current prompt: the
        question this answers is "what will the next session know", and
        showing the injected copy would hide a write that failed. It also
        names the backend and the embedder, because a session that silently
        fell back to lexical retrieval looks identical otherwise.
        """
        if self._store is None:
            print("Memory is disabled (memory_enabled: false in config.yaml).\n")
            return
        print(f"  backend: {self._store.describe}")
        if not self._store.available:
            print("  Nothing can be stored or retrieved until it is reachable.\n")
            return
        records = self._store.recent(MEMORY_PREVIEW_LINES)
        if records:
            print(f"  {self._store.count()} record(s), newest first:")
            for record in records:
                print(f"  - [{record.source}] ({record.category}) {record.text}")
        else:
            print("  Memory is empty. Add a fact with /remember <fact>.")
        log = self._store.root / SESSIONS_FILENAME
        if log.is_file():
            print(f"  run log: {log}")
        print()

    def _show_trace(self, reference: str | None) -> None:
        """Replay a recorded run as a timeline, without the model.

        The record is the JSONL file the trace hook wrote; this reads it back
        through :mod:`harness.observability.replay`, so what is shown is what
        was written, not what this process remembers. With no argument it
        shows the run that just finished — the one a person asks about — and
        falls back to the listing before any run has happened. ``list``
        always lists. Anything else is a run id or a prefix of one (ADR-087).

        Args:
            reference: ``None``, ``list``, or a run id prefix.
        """
        if self._trace_exporter is None:
            print("Traces are disabled (traces_enabled: false in config.yaml).\n")
            return
        traces_dir = self._trace_exporter.traces_dir
        quality = QualityStore(traces_dir)
        if reference is None and self._context.run_id:
            # The current context's id is the last top-level run; a file
            # that does not exist means the exporter could not write it.
            reference = self._context.run_id
        if reference is None or reference.lower() == "list":
            print(render_list(list_traces(traces_dir, quality)))
            print()
            return
        try:
            run = load_trace(find_trace(traces_dir, reference), quality)
        except (TraceNotFoundError, OSError) as exc:
            print(f"Could not read that trace: {exc}\n")
            return
        print(render_run(run))
        print()

    def _eval_run(self, arguments: str) -> None:
        """Attach a task-success verdict to a finished run.

        The user-facing half of ADR-096. A run's *system* success and
        *agent* success are already recorded — every request returned, the
        loop reached an answer — and neither of them knows whether the agent
        did what was asked. Nothing in the harness can know that, so the
        person who does says it, and it is stored against the run id so the
        judgement and the evidence stay together.

        Deliberately not an LLM judge. A model scoring its own harness would
        add an inference call, its own failure modes and a number nobody
        could check; a person pressing ``/eval ok`` after reading the answer
        is cheaper and more honest. The store takes a ``source`` field
        precisely so a future automated scorer can be added as one more
        writer without this command changing.

        Args:
            arguments: ``ok``/``fail`` optionally followed by a reason and a
                note, and optionally preceded by a run id.
        """
        if self._trace_exporter is None:
            print("Traces are disabled, so there is no run to judge.\n")
            return
        parts = arguments.split()
        if not parts:
            print(
                "Usage: /eval [RUN] ok|fail [reason] [note...]\n"
                f"  reasons: {', '.join(sorted(FAILURE_REASONS))}\n"
            )
            return

        # The run id is optional and comes first when present. Detected by
        # *not* being a verdict word, so `/eval ok` and `/eval 9b06 ok` both
        # read naturally and neither needs a flag.
        reference: str | None = None
        if parts[0].lower() not in ("ok", "pass", "fail", "bad"):
            reference = parts.pop(0)
        if not parts:
            print("Say whether the run succeeded: /eval [RUN] ok|fail\n")
            return

        verdict = parts.pop(0).lower()
        success = verdict in ("ok", "pass")
        traces_dir = self._trace_exporter.traces_dir
        reference = reference or self._context.run_id or "last"
        try:
            run = load_trace(find_trace(traces_dir, reference))
        except (TraceNotFoundError, OSError) as exc:
            print(f"Could not read that trace: {exc}\n")
            return

        # The signals are derived from the trace and stored *with* the
        # verdict, so an analysis of a hundred verdicts does not have to
        # re-read a hundred trace files.
        reason = "unspecified" if success else suggest_reason(run.signals)
        if parts and parts[0] in FAILURE_REASONS:
            reason = parts.pop(0)
        quality = QualityStore(traces_dir)
        recorded = quality.record(
            RunQuality(
                run_id=run.run_id,
                task_success=success,
                failure_reason=reason,
                source="human",
                note=" ".join(parts),
                signals=run.signals,
            )
        )
        if not recorded:
            print("Could not write the verdict (is the traces directory writable?)\n")
            return
        self._recorder.task_outcome("success" if success else "failure", reason)
        print(
            f"Recorded run {run.short_id} as task "
            f"{'success' if success else 'failure'} [{reason}].\n"
        )


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    """Parse command-line arguments.

    Args:
        argv: Argument list, or ``None`` to read ``sys.argv``.

    Returns:
        Parsed arguments.
    """
    parser = argparse.ArgumentParser(
        prog="harness",
        description="Terminal ReAct agent harness for the AI Engineering Lab.",
    )
    # Positional and optional: with no argument the container enters the
    # interactive agent mode the brief asks for; with one it runs a single
    # task and exits, which is what makes scripted E2E runs possible.
    parser.add_argument(
        "task",
        nargs="*",
        help="Run a single task and exit. Omit to start the interactive REPL.",
    )
    parser.add_argument(
        "--workspace",
        type=Path,
        default=None,
        help="Override the workspace path from config.yaml.",
    )
    parser.add_argument(
        "--permissions",
        # ``auto`` too, because config.yaml accepts it and run.sh forwards
        # whatever mode it validated: a flag that takes fewer values than the
        # setting it overrides turned ``./run.sh -p auto`` into a usage error
        # after the services had already started.
        choices=("allow_all", "confirm_mutations", "deny_mutations", "auto"),
        default=None,
        help=(
            "Override permission_mode from config.yaml for this run: run every "
            "tool, ask before mutating ones, refuse them, or auto (ask on a "
            "terminal, run everything otherwise)."
        ),
    )
    parser.add_argument(
        "--quiet",
        action="store_true",
        help=(
            "Hide the per-tool trace and the token-usage line. The daily "
            "budget line is always shown."
        ),
    )
    parser.add_argument("--version", action="version", version=f"harness {__version__}")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    """Load config, print a banner, and run the REPL or a single task.

    Args:
        argv: Argument list, or ``None`` to read ``sys.argv``.

    Returns:
        Process exit code.
    """
    args = _parse_args(argv)

    # load_settings raises SystemExit with a readable message on bad config,
    # which argparse-style exits already do too, so failures look uniform.
    settings = load_settings()
    if args.workspace is not None:
        # Resolved and created here so an overridden workspace gets the same
        # guarantees the configured one does — including the one that
        # matters most: it must not contain, or sit inside, the memory root.
        # Without this check `--workspace ./memory` would quietly hand the
        # file tools write access to long-term memory (ADR-035).
        override = args.workspace.resolve()
        override.mkdir(parents=True, exist_ok=True)
        reject_nested_roots(override, settings.memory_path)
        settings = settings.model_copy(update={"workspace_path": override})

    if args.permissions is not None:
        # A flag beats the file, matching how every other override in this
        # harness works, and making the strict modes demonstrable without a
        # config edit.
        settings = settings.model_copy(update={"permission_mode": args.permissions})

    # ``auto`` is resolved here, once, so that nothing downstream — the
    # policy, the banner, the sub-agent factory, the trace attribute — ever
    # sees a mode that is not one of the three the policy enforces.
    configured_mode = settings.permission_mode
    interactive = console.is_interactive(sys.stdin)
    resolved_mode = resolve_mode(configured_mode, interactive)
    settings = settings.model_copy(update={"permission_mode": resolved_mode})
    perms_note = (
        f" (auto: {'terminal' if interactive else 'no terminal'})"
        if configured_mode == "auto"
        else None
    )

    repl = Repl(settings, trace=not args.quiet)

    # try/finally so the child processes started above are stopped however
    # the session ends: a clean /exit, a one-shot task, Ctrl-C, or a crash.
    try:
        if args.task:
            # One-shot mode: no banner, so the output is easy to capture in
            # a script or a test.
            #
            # And a clean scratchpad. A one-shot run is a whole invocation
            # to itself — `docker run … "summarise the ledger"` has no
            # relationship to whatever the last container was asked — so
            # inheriting its notes injects another task's plan under a
            # heading that says "for this task". A REPL is the opposite
            # case: its turns *are* one thread, so they keep the notes and
            # /reset is how a person ends it (ADR-105).
            repl.reset_notes()
            repl.ask(" ".join(args.task))
            # No banner in this mode, so a rebuild — which happens on the
            # first memory call, during the task — is reported after it.
            rebuilt = migration_note(repl.store)
            if rebuilt:
                print(f"Warning: memory {rebuilt}")
            return 0

        _banner(
            settings,
            repl.registry,
            repl.skills,
            repl.mcp,
            repl.store,
            repl.metrics_note,
            repl.traces_note,
            repl.subagents_note,
            repl.telemetry_notes,
            repl.budget_note,
            repl.notes_note,
            perms_note=perms_note,
        )
        return repl.run()
    finally:
        repl.close()


if __name__ == "__main__":
    sys.exit(main())
