"""What the harness draws on the terminal: colour, the thinking spinner,
the per-call trace and the approval prompt.

Split out of ``cli.py``, which had grown to hold the REPL, the banner, the
session assembly and all of this. Everything here prints; nothing here
decides anything — the loop decides, and these classes show it. ``print``
is allowed here for the same reason it is allowed in ``cli.py``: this is
the terminal front end, and library code stays print-free (AGENTS.md).
"""

import os
import sys
import threading
import time
from collections.abc import Callable
from typing import Any

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


from harness.context import RunContext
from harness.observability.errors import PERMISSION_DECLINED, PERMISSION_DENIED
from harness.permissions import (
    TierDecision,
)
from harness.tools.base import ERROR_PREFIX, Tool, ToolOutcome


def _color_enabled() -> bool:
    """Whether trace output may carry ANSI colour.

    Off for anything that is not an interactive terminal — a pipe, a
    ``docker logs`` redirect, a test's captured stdout — so colour never
    leaks into text another program parses, and off when ``NO_COLOR`` is
    set (https://no-color.org/), which is the convention a user reaches
    for before the harness would ever know to ask.
    """
    if os.environ.get("NO_COLOR"):
        return False
    try:
        return sys.stdout.isatty()
    except (AttributeError, ValueError):
        return False


class Sgr:
    """The handful of ANSI SGR codes the trace uses."""

    RESET = "\033[0m"
    BOLD = "\033[1m"
    DIM = "\033[2m"
    CYAN = "\033[36m"
    GREEN = "\033[32m"
    RED = "\033[31m"
    YELLOW = "\033[33m"


def style(text: str, *codes: str) -> str:
    """Wrap ``text`` in ANSI codes, or return it unchanged.

    A single choke point rather than a check at every call site: every
    coloured line in this module goes through this function, so
    ``_color_enabled`` only has to be consulted once and a test capturing
    stdout never has to know colour exists.

    Args:
        text: The text to colour.
        codes: One or more ``Sgr`` codes to prefix it with.

    Returns:
        The coloured text when colour is enabled, otherwise ``text`` as-is.
    """
    if not codes or not _color_enabled():
        return text
    return "".join(codes) + text + Sgr.RESET


# Erase from the cursor to the end of the line. Used after a carriage
# return rewrite so a shorter frame does not leave glyphs from a longer
# one sitting on the right.
_ERASE_LINE = "\033[K"

# Braille spinner. ASCII is the fallback when stdout cannot encode it
# (a Latin-1 pipe, a mis-set locale) so the wait line never raises into
# the run it is supposed to explain.
_BRAILLE_SPINNER = ("⠋", "⠙", "⠹", "⠸", "⠼", "⠴", "⠦", "⠧", "⠇", "⠏")
_ASCII_SPINNER = ("|", "/", "-", "\\")


def _default_spinner_glyphs() -> tuple[str, ...]:
    """Pick spinner glyphs the current stdout can actually print."""
    encoding = getattr(sys.stdout, "encoding", None) or "utf-8"
    try:
        _BRAILLE_SPINNER[0].encode(encoding)
    except (LookupError, UnicodeEncodeError):
        return _ASCII_SPINNER
    return _BRAILLE_SPINNER


def _spinner_enabled() -> bool:
    """Whether the wait line may animate in place.

    Carriage-return animation is a TTY convenience. A pipe, a
    ``docker logs`` redirect, or a test's captured stdout must keep the
    one-shot thinking line from ADR-023: rewriting the same line is
    noise in a log file, and the wording those tests pin would drift.
    """
    try:
        return bool(sys.stdout.isatty())
    except (AttributeError, ValueError):
        return False


class Console:
    """The terminal the trace and the approval prompt share.

    Two things have to be shared between :class:`ConsoleTraceHook` and
    :class:`ConsoleConfirmer`. Owning them here and handing the same instance
    to both makes that coupling visible in their constructors, rather than
    hidden in module-level state each class reaches for.

    * **A lock.** Batched delegation runs sub-agents on worker threads
      (ADR-106), and ``print`` is not one write. Every line goes through
      :meth:`emit` so it is written whole, and the approval prompt holds the
      lock for the whole question-and-answer so the question stays the last
      line on screen until it is answered (ADR-113). Re-entrant, because the
      prompt prints its own lines while holding it.
    * **Whether a question was just asked, per thread.** The trace holds a
      call line back until the permission check decides; it must drop it
      when the prompt already showed the call, and print it when the call
      was approved without a question — "allow for session" (ADR-113).
    """

    def __init__(self) -> None:
        """Create an unlocked console with no question pending."""
        self._lock = threading.RLock()
        self._asked = threading.local()

    def emit(self, *args: Any, **kwargs: Any) -> None:
        """Print one line, whole, under the lock."""
        with self._lock:
            print(*args, **kwargs)

    def held(self) -> threading.RLock:
        """Return the lock, for a caller that needs several lines in a row."""
        return self._lock

    def mark_asked(self) -> None:
        """Record that this thread just put a question on screen."""
        self._asked.shown = True

    def take_asked(self) -> bool:
        """Return whether this thread asked about the current call, and reset."""
        asked = bool(getattr(self._asked, "shown", False))
        self._asked.shown = False
        return asked


def _thinking_label(iterations_used: int, max_iterations: int) -> str:
    """The thinking-line wording, shared by the static print and the spinner.

    Args:
        iterations_used: Current ReAct turn, 1-based.
        max_iterations: Configured cap. ``0`` means no cap, so the
            denominator is omitted (ADR-047).
    """
    progress = (
        f"{iterations_used}/{max_iterations}"
        if max_iterations
        else str(iterations_used)
    )
    return f"thinking (iteration {progress})"


class _ThinkingSpinner:
    """Rewrite one terminal line until the model answers.

    InnKube waits can last minutes. ADR-023's static ``… thinking`` line
    is better than silence, but it still looks frozen. This spinner
    lives in the CLI (it prints) and is not a TUI: one line, rewritten
    with ``\\r``, then a finished line with a newline so the tool arrows
    that follow start cleanly (ADR-100).
    """

    def __init__(
        self,
        label: str,
        *,
        stream: Any | None = None,
        interval: float = 0.1,
        glyphs: tuple[str, ...] | None = None,
        clock: Callable[[], float] | None = None,
    ) -> None:
        """Args:
        label: Text after the glyph, including the iteration count.
        stream: Where frames are written. Defaults to ``sys.stdout``.
        interval: Seconds between frames. Slow enough not to busy-loop,
            fast enough to read as motion rather than a hang.
        glyphs: Spinner frames. ``None`` picks braille or ASCII from
            the current stdout encoding.
        clock: Monotonic seconds. Injectable so elapsed-time tests do
            not have to wait a real second.
        """
        self._label = label
        self._stream = sys.stdout if stream is None else stream
        self._interval = interval
        self._glyphs = glyphs if glyphs is not None else _default_spinner_glyphs()
        self._clock = clock if clock is not None else time.monotonic
        self._started = self._clock()
        self._frame = 0
        self._stop = threading.Event()
        self._first_frame = threading.Event()
        self._lock = threading.Lock()
        self._finalized = False
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        """Begin rewriting the line on a daemon thread."""
        thread = threading.Thread(
            target=self._run, name="thinking-spinner", daemon=True
        )
        self._thread = thread
        thread.start()

    def wait_for_frame(self, timeout: float = 1.0) -> bool:
        """Block until the first frame is on the stream.

        A test helper, not part of the CLI path: starting a thread and
        asserting on its first write without this is a sleep.

        Args:
            timeout: Seconds to wait before giving up.

        Returns:
            Whether a frame was written in time.
        """
        return self._first_frame.wait(timeout)

    def tick(self) -> None:
        """Write one frame. The thread loops this; tests drive it directly."""
        if self._stop.is_set():
            return
        elapsed = int(self._clock() - self._started)
        glyph = self._glyphs[self._frame % len(self._glyphs)]
        line = f"  {glyph} {self._label}  {elapsed}s"
        with self._lock:
            if self._finalized:
                return
            self._write(f"\r{style(line, Sgr.DIM)}{_ERASE_LINE}")
            self._frame += 1
            self._first_frame.set()

    def stop(self) -> None:
        """Freeze the line with elapsed time and a newline.

        Idempotent: ``after_llm``, ``on_llm_error`` and ``on_run_end``
        all call this, and a KeyboardInterrupt can fire more than one
        of them. Joining the thread *before* the final write is what
        keeps a late tick from overwriting the finished line.
        """
        self._stop.set()
        thread = self._thread
        self._thread = None
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=1.0)
        elapsed = int(self._clock() - self._started)
        final = f"  … {self._label}  {elapsed}s"
        with self._lock:
            if self._finalized:
                return
            self._finalized = True
            self._write(f"\r{style(final, Sgr.DIM)}{_ERASE_LINE}\n")

    def _run(self) -> None:
        """Loop frames until ``stop`` is signalled."""
        while not self._stop.is_set():
            self.tick()
            if self._stop.wait(self._interval):
                break

    def _write(self, text: str) -> None:
        """Write one chunk, or give up if the terminal has gone away."""
        try:
            self._stream.write(text)
            self._stream.flush()
        except (OSError, ValueError):
            # stdout closed mid-wait (container killed, test tore down).
            # Stopping the animation is the only remaining job.
            self._stop.set()


class ConsoleTraceHook:
    """Print each tool call as it happens.

    Implements the :class:`~harness.hooks.Hook` protocol structurally — no
    inheritance needed, which is the point of using ``Protocol`` for the
    seam. It lives in the CLI rather than in ``hooks.py`` for two reasons:
    it prints, and real observability (metrics, structured traces) is a
    Week 3 deliverable that should not be half-built here.

    Its job is narrow: let the user and the TAs *see* the ReAct loop
    working, which is otherwise invisible between task and final answer.
    """

    def __init__(
        self,
        enabled: bool = True,
        max_iterations: int = 0,
        permission_mode: str | None = None,
        console: Console | None = None,
    ) -> None:
        """Args:
        enabled: When ``False`` every callback is a no-op, so ``--quiet``
            does not need a second hook class.
        max_iterations: Configured cap, shown as the denominator on the
            thinking line. ``0`` means no cap, and the line then shows the
            count alone (ADR-047).
        permission_mode: The session's resolved mode, named in the line a
            policy refusal prints so the reader knows which switch to flip.
        console: The terminal shared with the session's confirmer. A hook
            built alone gets its own.
        """
        self._console = console if console is not None else Console()
        self.enabled = enabled
        self._max_iterations = max_iterations
        self._permission_mode = permission_mode
        # The call line for the tool about to run, held back until the
        # permission check has decided. Printed then as `→ call` when the
        # call is simply allowed; dropped when the confirmation prompt has
        # already shown it; folded into the `✗` line when it is refused.
        # Printed at `before_tool` instead, a gated call would appear twice —
        # once as an arrow that reads as "running", then again as the
        # question (ADR-112).
        #
        # Per thread, not per hook: sub-agents share this hook and a batched
        # `delegate` runs them on worker threads (ADR-106), and one shared
        # slot would print child B's call line against child A's decision.
        self._pending = threading.local()
        # One spinner, the top-level run's. Sub-agents share this hook but
        # never animate — they print plain, named lines (ADR-113) — so one
        # slot is enough even when several of them think at once.
        self._spinner: _ThinkingSpinner | None = None

    def on_run_start(self, context: RunContext) -> None:
        """Note whose calls this thread is about to print.

        A sub-agent's tool lines carry its role, because concurrent children
        interleave on one terminal and an unlabelled ``→ write_file`` printed
        under "critic thinking" read as the critic writing. Kept per thread,
        like the held-back call line: each child runs on its own worker
        (ADR-106, ADR-120).
        """
        self._pending.role = context.role if context.depth > 0 else None

    def before_llm(self, context: RunContext) -> None:
        """Show that the harness is waiting on the model.

        InnKube can take minutes. Without this line the REPL looks frozen
        between ``You:`` and the first tool arrow. On a TTY the line
        animates so the wait itself does not look like a hang (ADR-100);
        a pipe keeps the one-shot line from ADR-023.
        """
        if not self.enabled:
            return
        label = _thinking_label(context.iterations_used, self._max_iterations)
        if context.depth > 0:
            # A sub-agent never animates. The spinner redraws one terminal
            # line, which assumed one agent thinking at a time; batched
            # delegation runs several children at once (ADR-106), and their
            # spinners overwrote each other and the lines between them. A
            # child gets one plain line instead, named, so "thinking
            # (iteration 2)" says whose iteration it is (ADR-113).
            self._pending.role = context.role
            self._console.emit(
                style(f"  … {context.role} {label}", Sgr.DIM), flush=True
            )
            return
        if _spinner_enabled():
            self._halt_spinner()
            self._spinner = _ThinkingSpinner(label)
            self._spinner.start()
            return
        self._console.emit(style(f"  … {label}", Sgr.DIM), flush=True)

    def after_llm(self, context: RunContext, response: Any) -> None:
        """Stop the spinner so the next tool arrow starts on a fresh line.

        Assistant text is still shown as the final answer by the caller;
        this only ends the wait indicator. A sub-agent has no spinner, and
        must not stop the one it does not own.
        """
        if context.depth == 0:
            self._halt_spinner()

    def on_llm_error(self, context: RunContext, error: BaseException) -> None:
        """Stop the spinner; the CLI prints the failure when it lands.

        Saying the error here as well would print the same failure twice,
        once as a trace line and once as the error the user is shown.
        """
        if context.depth == 0:
            self._halt_spinner()

    def before_tool(self, name: str, arguments: dict[str, Any]) -> None:
        """Remember the call; it is printed once the permission check decides."""
        # Nothing has been asked about *this* call yet, whatever the last
        # one on this thread did.
        self._console.take_asked()
        if self.enabled:
            self._pending_call = f"{name}({format_arguments(arguments)})"

    def after_permission(self, name: str, outcome: str) -> None:
        """Print the call line now that it is known whether it will run.

        ``allow`` prints the usual arrow, and so does a ``confirmed`` call
        that was approved without a question — "allow for session". When a
        question *was* shown, ``confirmed`` and ``declined`` print nothing:
        the prompt already showed the call. ``deny`` keeps the line for
        :meth:`after_tool`, which prints it as the refusal.
        """
        asked = self._console.take_asked()
        if outcome == "allow" or (outcome == "confirmed" and not asked):
            # Approved without a question on screen — the policy allowed it,
            # or the person had answered "allow for session". Either way the
            # call has not been shown yet, and a result must never appear
            # without its call above it.
            self._print_pending_call()
        elif outcome in ("confirmed", "declined"):
            self._pending_call = None

    @property
    def _pending_call(self) -> str | None:
        """This thread's held-back call line, if any."""
        call: str | None = getattr(self._pending, "call", None)
        return call

    @_pending_call.setter
    def _pending_call(self, call: str | None) -> None:
        """Hold back, or clear, this thread's call line."""
        self._pending.call = call

    @property
    def _who(self) -> str:
        """``[role] `` for a sub-agent's line on this thread, else empty."""
        role: str | None = getattr(self._pending, "role", None)
        return f"[{role}] " if role else ""

    def _print_pending_call(self) -> None:
        """Print the held-back call line as an arrow, once."""
        call = self._pending_call
        self._pending_call = None
        if self.enabled and call is not None:
            self._console.emit(style(f"  → {self._who}{call}", Sgr.CYAN))

    def after_tool(
        self, name: str, result: str, outcome: ToolOutcome | None = None
    ) -> None:
        """Show the first line of the observation fed back to the model.

        Failures get a ``!`` so ``Error:`` is not lost in a long arrow list,
        and — when the caller passed a typed outcome — the reason in
        brackets, because ``! Error: calendar MCP server: ...`` and
        ``! Error: ...`` are the same line to a reader in a hurry while
        ``mcp.connection`` and ``tool.not_found`` are not (ADR-094).

        Failure is read from ``outcome.ok`` when there is an outcome and
        from the prefix when there is not. The prefix test stays as the
        fallback for the one call site that has no outcome to give: a tool
        the failure guard retired before dispatching it.
        """
        if not self.enabled:
            return
        error_type = outcome.error_type if outcome is not None else None
        if error_type == PERMISSION_DECLINED:
            # The confirmation prompt already said why it did not run — the
            # person answered no, refused it for the session, or there was
            # no terminal to ask on. The observation is the instruction to
            # the model, not a sentence for the person.
            self._pending_call = None
            return
        if error_type == PERMISSION_DENIED:
            call = self._pending_call or name
            self._pending_call = None
            mode = f" in {self._permission_mode} mode" if self._permission_mode else ""
            self._console.emit(
                style(f"  ✗ {self._who}{call} — not run: not allowed{mode}", Sgr.RED)
            )
            return
        # Calls that never reached the permission check — a retired tool,
        # arguments that did not parse — still show what was attempted.
        self._print_pending_call()
        line = first_line(result)
        failed = (
            not outcome.ok if outcome is not None else result.startswith(ERROR_PREFIX)
        )
        if failed:
            reason = (
                f" [{outcome.error_type}]"
                if outcome is not None and outcome.error_type
                else ""
            )
            self._console.emit(style(f"  ← ! {self._who}{line}{reason}", Sgr.RED))
        else:
            self._console.emit(style(f"  ← {self._who}{line}", Sgr.GREEN))

    def on_run_end(self, context: RunContext) -> None:
        """Stop any spinner, then show the top-level run's answer.

        The answer is printed here, not by the caller after the loop
        returns, because this hook is composed last and ``CompositeHook``
        fans ``on_run_end`` out in reverse: it runs *first*. Everything
        slower that happens at run end — memory extraction is a whole model
        call — then happens after the user already has the answer, and still
        inside the run's trace span (ADR-110).

        A delegated child is skipped: it shares this hook, and its answer is
        a result for the parent, not a reply to the user. A run that ended on
        an error has no answer; the caller reports the error. The spinner is
        stopped either way, because ``KeyboardInterrupt`` is not
        ``Exception`` and ``on_llm_error`` does not fire for it — by the
        top-level run only, since a sub-agent owns no spinner.
        """
        if context.depth > 0:
            # The parent may resume on this same thread — always, when
            # delegation is sequential — and its lines are not the child's.
            self._pending.role = None
        if context.depth == 0:
            self._halt_spinner()
        if context.depth == 0 and context.final_answer is not None:
            # Coloured as one unit, not just the "Agent:" label: splitting
            # the two would put a reset code between them, and "Agent: done"
            # would stop being a contiguous substring of what was printed —
            # which is exactly what the CLI tests (and a person's eye) look
            # for.
            self._console.emit(
                style(f"Agent: {context.final_answer}", Sgr.BOLD, Sgr.GREEN)
            )

    def _halt_spinner(self) -> None:
        """Freeze and drop the current spinner, if any."""
        spinner = self._spinner
        self._spinner = None
        if spinner is not None:
            spinner.stop()


# The four answers to a confirmation prompt, and every spelling accepted for
# each. One table, so the prompt text, the parser and the tests cannot drift
# apart — the bug that shape prevents is a documented answer the parser does
# not know, which reads to the user as the agent ignoring them (ADR-103).
#
# Generous on purpose. A person answering the same question for the ninth
# time types what comes to hand: `y`, `Y`, `yes`, `ok`. Input is lowercased
# before it gets here, so `Y` and `YES` are covered by the lowercase forms.
CONFIRM_ANSWERS: dict[str, frozenset[str]] = {
    "yes": frozenset({"y", "yes", "ye", "yeah", "yep", "ok", "okay", "run", "1"}),
    # The bare Enter lives here: the safe answer has to be the one a
    # distracted person gives by reflex.
    "no": frozenset({"", "n", "no", "nope", "nah", "skip", "0"}),
    "allow_session": frozenset(
        {"a", "allow", "always", "as", "allow for session", "allow session", "all"}
    ),
    "deny_session": frozenset(
        {"d", "deny", "never", "ds", "deny for session", "deny session", "stop"}
    ),
}

# What the prompt offers, derived from nothing — written to be read by a
# person — but pinned against the table above by a test.
CONFIRM_PROMPT = "[y]es / [n]o / [a]llow for session / [d]eny for session"

# How many times a misspelling is met with the question again before the
# call is refused. Bounded so a stream of junk cannot spin forever, and
# small enough that it is obviously not a wall.
CONFIRM_ATTEMPTS = 3


def _read_answer(answer: str) -> str | None:
    """Return which of the four answers this is, or ``None`` if it is none.

    Args:
        answer: What the user typed, stripped and lowercased.

    Returns:
        ``"yes"``, ``"no"``, ``"allow_session"``, ``"deny_session"``, or
        ``None`` when nothing matched — which is a question to ask again,
        not a refusal.
    """
    for choice, spellings in CONFIRM_ANSWERS.items():
        if answer in spellings:
            return choice
    return None


class ConsoleConfirmer:
    """Ask the user on the terminal before a tool call the policy gated.

    Implements :class:`~harness.permissions.Confirmer` structurally, and
    lives here for the same reason :class:`ConsoleTraceHook` does: it prints
    and it reads stdin, and ``AGENTS.md`` keeps both out of library code.
    That split is the whole point of the seam — ``loop.py`` knows a call may
    need approval, and knows nothing about terminals (ADR-057).

    Four answers, not two. ``y`` and ``n`` decide this call; ``a`` allows
    every later call to *that* tool for the rest of the session and ``d``
    refuses it. Without the session pair, a task that writes twelve files
    asks twelve times, and a prompt that is tiring to answer gets answered
    ``y`` without being read, which is worse than not asking. ``d`` is for
    the person who has said no three times and would rather the model
    stopped trying (ADR-102).

    Each answer accepts the spellings someone actually types — ``y``, ``Y``,
    ``yes``, ``ok`` — and an answer that matches none of them is asked
    again rather than read as "no" (ADR-103). That distinction matters: the
    earlier version treated everything but ``y``/``yes`` as a refusal, so a
    typo denied the call and the person saw the agent give up for no visible
    reason. The vocabulary is :data:`CONFIRM_ANSWERS`.

    The prompt also says *why* it is asking — "reaches outside the
    sandbox", "changes state", "matched a permission rule" — when a policy
    that can explain itself has been attached. A person answering the same
    question a dozen times should at least be able to see what it is.
    """

    def __init__(
        self,
        stream: Any = None,
        explain: Callable[[Tool], TierDecision] | None = None,
        console: Console | None = None,
    ) -> None:
        """Args:
        stream: Input stream to test for interactivity. Defaults to
            ``sys.stdin``. Injectable so the behaviour with no terminal
            can be tested without one.
        explain: Returns a tool's tier decision, printed with the prompt.
            ``None`` prints the prompt without a reason.
        console: The terminal shared with the session's trace hook, so a
            question holds back the trace until it is answered. A confirmer
            built alone gets its own.
        """
        self._console = console if console is not None else Console()
        self._stream = stream if stream is not None else sys.stdin
        self._explain = explain
        # Session-scoped, not run-scoped: a REPL user who approved
        # write_file for this task should not be asked again on the next.
        self._always: set[str] = set()
        self._never: set[str] = set()

    def confirm(self, tool: Tool, arguments: dict[str, object]) -> bool:
        """Ask, and return whether the call was approved.

        Fails closed in every case where the question cannot actually be
        put to a person: a piped or redirected stdin (a one-shot run, a
        test, a container with no TTY), end of input, or Ctrl-C. Treating
        any of those as approval would make the prompt decorative exactly
        when nobody is watching.

        Args:
            tool: Tool awaiting approval.
            arguments: Arguments it would be called with.

        Returns:
            ``True`` to run the call.
        """
        return self.confirm_for("", tool, arguments)

    def confirm_for(self, asker: str, tool: Tool, arguments: dict[str, object]) -> bool:
        """Ask on behalf of a named agent, and return whether it was approved.

        Implements the optional
        :class:`~harness.subagents.confirm.AttributingConfirmer` seam. With
        several sub-agents running at once, "run write_file?" names neither
        the agent nor the reason, and there is no answer a person could give
        honestly; this is the same question with the asker in front of it.

        The session-wide *always* and *never* answers are keyed on the asker
        as well as the tool, so approving a writer sub-agent's ``write_file``
        for the session does not silently approve your own, or another
        specialist's. Narrower is the right default for a remembered answer:
        the person agreed to one thing.

        Args:
            asker: Who is asking — ``""`` for the top-level agent, which
                reads as it always did.
            tool: Tool awaiting approval.
            arguments: Arguments it would be called with.

        Returns:
            ``True`` to run the call.
        """
        # What the person sees and what the session remembers. `tool.name`
        # itself is left alone: `PermissionTiers.tier_of` matches the
        # deny-list and the operator's rules on it, so a decorated name
        # would be explained by the wrong rule.
        label = f"{asker}: {tool.name}" if asker else tool.name
        return self._ask(label, tool, arguments)

    def _ask(self, label: str, tool: Tool, arguments: dict[str, object]) -> bool:
        """Put one labelled question to the person. See :meth:`confirm_for`.

        Holds the console for the whole exchange. Other sub-agents keep
        working, but anything they print waits until the person has
        answered, so the question stays the last line on the screen instead
        of scrolling away under their output (ADR-113).
        """
        with self._console.held():
            return self._ask_holding_console(label, tool, arguments)

    def _ask_holding_console(
        self, label: str, tool: Tool, arguments: dict[str, object]
    ) -> bool:
        """The question itself; the caller holds the console lock."""
        if label in self._always:
            return True
        if label in self._never:
            self._console.emit(
                style(
                    f"  ✗ {label} refused for this session (you answered 'd')",
                    Sgr.RED,
                )
            )
            return False
        if not is_interactive(self._stream):
            self._console.emit(
                style(
                    f"  ✗ {label} needs approval but there is no terminal to ask "
                    f"on; refusing. Use --permissions allow_all for unattended runs.",
                    Sgr.RED,
                )
            )
            return False

        reason = ""
        if self._explain is not None:
            reason = f"  — {self._explain(tool).reason}"
        self._console.emit(
            style(f"  ? {label}({format_arguments(arguments)}){reason}", Sgr.YELLOW)
        )
        # The call is on screen now, with its question; the trace must not
        # print it a second time (ADR-112, ADR-113).
        self._console.mark_asked()
        for attempt in range(CONFIRM_ATTEMPTS):
            try:
                answer = input(f"    run it? {CONFIRM_PROMPT}: ").strip().lower()
            except (EOFError, KeyboardInterrupt):
                self._console.emit()
                self._console.emit(style("    ✗ no answer — not run", Sgr.RED))
                return False
            choice = _read_answer(answer)
            if choice == "allow_session":
                self._always.add(label)
                self._console.emit(f"    {label} allowed for the rest of this session.")
                return True
            if choice == "deny_session":
                self._never.add(label)
                self._console.emit(f"    {label} refused for the rest of this session.")
                return False
            if choice == "no":
                # Said here, because this is where the reason is known; the
                # trace prints nothing more for a declined call (ADR-112).
                self._console.emit(style("    ✗ declined — not run", Sgr.RED))
                return False
            if choice is not None:
                return choice == "yes"
            # Unrecognised. Asked again rather than read as "no", because
            # a typo is not an answer: someone who meant to approve a write
            # and typed `ys` should not have it silently refused and be left
            # wondering why the agent gave up (ADR-103).
            remaining = CONFIRM_ATTEMPTS - attempt - 1
            if remaining:
                self._console.emit(
                    style(
                        f"    '{answer}' is not one of the four answers. "
                        f"{remaining} more {'try' if remaining == 1 else 'tries'}.",
                        Sgr.YELLOW,
                    )
                )
        # Out of tries. Refusing is the same fail-closed rule the rest of
        # this class follows: an answer nobody managed to give is not a yes.
        self._console.emit(style(f"  ✗ {label} refused: no usable answer.", Sgr.RED))
        return False


def is_interactive(stream: Any) -> bool:
    """Return whether ``stream`` is a terminal a question can be asked on.

    Defensive about ``isatty``: a replaced stdin in a test or a wrapped
    stream in another front end may not have it, and a missing method means
    "not a terminal", not a crash.

    Args:
        stream: Candidate input stream.

    Returns:
        ``True`` when the stream is an interactive TTY.
    """
    try:
        return bool(stream.isatty())
    except (AttributeError, ValueError):
        return False


def format_arguments(arguments: dict[str, Any]) -> str:
    """Render tool arguments compactly for the trace line.

    Long values (a whole file being written) are clipped: the trace is a
    progress indicator, not a transcript, and a 200-line ``content``
    argument would bury the actual flow of the run.

    Args:
        arguments: Decoded tool arguments.

    Returns:
        A single-line ``key=value, ...`` string.
    """
    parts: list[str] = []
    for key, value in arguments.items():
        text = str(value)
        if len(text) > 40:
            text = text[:40] + "…"
        # repr-style quoting keeps whitespace and newlines visible, which
        # matters when debugging an edit_file that failed to match.
        parts.append(f"{key}={text!r}")
    return ", ".join(parts)


def first_line(text: str) -> str:
    """Return the first line of ``text``, clipped, for the trace line.

    Args:
        text: Full observation.

    Returns:
        One short line summarising it.
    """
    line = text.strip().splitlines()[0] if text.strip() else "(empty)"
    return line if len(line) <= 100 else line[:100] + "…"
