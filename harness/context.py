"""In-run short-term memory for one agent session.

This is the *Context* box of the architecture diagram and the "short-term
memory, updated after every loop step" of the brief (Section 2). It holds
the system instructions, the conversation, and every tool observation.

Deliberately **not** in this module:

* Long-term memory *storage*. Files that outlive the process live in
  :mod:`harness.memory`. What lives here is the mechanism that puts
  harness-supplied material into the window —
  :meth:`RunContext.set_system_section` — which memory and skills both use
  and neither owns (ADR-034, ADR-038).

Compaction of *old tool observations* lives here as :meth:`RunContext.compact`
so the loop stays a caller, not an owner of the policy (ADR-032).

A single ``RunContext`` survives across REPL turns, which is what makes the
CLI a *multi-turn* chat rather than a series of unrelated one-shot requests.
"""

import json
import uuid
from dataclasses import dataclass, field
from typing import Any

# Marker so a stub is not compacted a second time.
COMPACTED_PREFIX = "[compacted]"

# What a run calls itself when nobody delegated it. Defined here, where the
# field lives, because three other modules label their output with it — the
# metrics hook, the JSONL trace hook and the sub-agent factory — and three
# copies of a string that must agree is two copies too many.
MAIN_ROLE = "main"

# Fence around an injected section's body. Two jobs. It tells the model
# where harness-supplied data ends, which is the textual half of the
# untrusted-input defence (ADR-034); and it gives
# :meth:`RunContext.set_system_section` an unambiguous span to replace, so
# re-injecting a section cannot leave a fragment of the previous one behind.
#
# Wordy on purpose. A short marker like ``>>>`` collides with things that
# genuinely appear in stored text — git conflict markers, shell heredocs —
# and the body is scrubbed of the fence before injection, so a collision
# would corrupt legitimate content. It also has to be text a *poisoned*
# memory file is unlikely to reproduce by accident.
SECTION_OPEN = "<<<BEGIN HARNESS DATA"
SECTION_CLOSE = "END HARNESS DATA>>>"

# What a fence marker found inside a section body is replaced with. Visible
# rather than silent, so a reader of the transcript can tell that something
# in the stored text tried to imitate the harness.
_FENCE_REMOVED = "[fence marker removed]"

# The latest tool results stay verbatim so the current step still has
# evidence. Older ones become one-line pointers.
KEEP_RECENT_TOOL_RESULTS = 4


def message_chars(messages: list[dict[str, Any]]) -> int:
    """Return a cheap size estimate of the conversation payload.

    Character count, not tokens: InnKube often omits ``usage``, so a token
    trigger would never fire. Tool-call arguments are included because a
    ``write_file`` body sits on the assistant message, not on the tool
    result.

    Args:
        messages: The context's message list.

    Returns:
        Total characters across contents and tool-call payloads.
    """
    total = 0
    for message in messages:
        content = message.get("content")
        if isinstance(content, str):
            total += len(content)
        for call in message.get("tool_calls") or []:
            total += len(str(call))
    return total


def _remove_section(content: str, header: str) -> str:
    """Strip a previously injected section from a system message.

    Matching is anchored on the header line plus the fence rather than on
    the header alone. Anchoring on the header text alone would break the
    moment a memory file happened to contain the header string, which is
    not far-fetched: the model writes those files and can see the header in
    its own prompt.

    Args:
        content: Current system message text.
        header: Section label to remove.

    Returns:
        ``content`` without that section, or unchanged if it was absent.
    """
    marker = f"\n{header}\n{SECTION_OPEN}\n"
    start = content.find(marker)
    if start == -1:
        # Also handle a section that begins the message, which happens only
        # if the system prompt itself is empty.
        if not content.startswith(f"{header}\n{SECTION_OPEN}\n"):
            return content
        start = 0
        opening = len(f"{header}\n{SECTION_OPEN}\n")
    else:
        opening = start + len(marker)

    closing = content.find(f"\n{SECTION_CLOSE}", opening)
    if closing == -1:
        # A truncated section (only reachable if something else edited the
        # message) is dropped from the header onwards rather than left
        # half-present.
        return content[:start].rstrip()
    end = closing + len(f"\n{SECTION_CLOSE}")
    return (content[:start].rstrip() + "\n" + content[end:].lstrip()).rstrip()


# The section the compaction summary is injected under. A system section
# rather than a message, so it is replaced in place each time rather than
# growing a new copy per compaction (ADR-090).
COMPACTION_SECTION_HEADER = "## Earlier work (compacted)"

# The most compacted calls to list. Past this the summary would itself be
# the thing crowding the window out, so the oldest are dropped and the count
# says how many — losing the detail of the oldest work while keeping the
# fact that it happened is the right trade in that order.
COMPACTION_SUMMARY_LIMIT = 25


# An argument value longer than this is a payload rather than a parameter —
# a file's contents, a fetched page — and is what makes a bulk task's own
# history the thing that crowds it out. Short values (a path, a query, a
# flag) are left alone: they are the pointer that makes the stub readable.
MAX_KEPT_ARGUMENT_CHARS = 200


def _stub_arguments(encoded: str) -> str | None:
    """Shrink the long values in one serialised tool-call argument object.

    Args:
        encoded: The provider's ``arguments`` JSON string.

    Returns:
        A shorter JSON string, or ``None`` when there was nothing worth
        shrinking — which is the common case and must not count as a
        change.
    """
    try:
        parsed = json.loads(encoded)
    except (TypeError, ValueError):
        return None
    if not isinstance(parsed, dict):
        return None
    shrunk = False
    for key, value in parsed.items():
        if isinstance(value, str) and len(value) > MAX_KEPT_ARGUMENT_CHARS:
            parsed[key] = f"{COMPACTED_PREFIX} {len(value):,} chars"
            shrunk = True
    return json.dumps(parsed, sort_keys=True) if shrunk else None


def _kept_arguments(encoded: str) -> str:
    """Render the arguments that survived stubbing, as a readable pointer.

    ``{"path": "f3.md", "content": "[compacted] 400 chars"}`` becomes
    ``path=f3.md``: the part that identifies *which* call this was, without
    the measurement of the part that is gone.

    Args:
        encoded: A serialised argument object, already stubbed.

    Returns:
        A short ``key=value`` list, or ``""`` when nothing readable is left.
    """
    try:
        parsed = json.loads(encoded)
    except (TypeError, ValueError):
        return ""
    if not isinstance(parsed, dict):
        return ""
    kept = [
        f"{key}={value}"
        for key, value in sorted(parsed.items())
        if isinstance(value, str) and not value.startswith(COMPACTED_PREFIX)
    ]
    return f"({', '.join(kept)})" if kept else ""


def _stub_observation(name: str, content: str) -> str:
    """Replace a long observation with a pointer the model can re-fetch.

    Args:
        name: Tool that produced the observation.
        content: Full observation text.

    Returns:
        A one-line stub that keeps the tool name and the first line.
    """
    body = content.strip()
    first = body.splitlines()[0] if body else "(empty)"
    if len(first) > 120:
        first = first[:120] + "…"
    # The size is part of the pointer, not decoration: "3 lines" and "900
    # lines" lead to different decisions about whether re-fetching is worth
    # a call, and the model cannot tell them apart from a first line.
    return (
        f"{COMPACTED_PREFIX} {name} ({len(body):,} chars): {first} "
        "Call this tool again if you need the full output."
    )


@dataclass
class RunContext:
    """Conversation state the ReAct loop appends to after every step.

    Attributes:
        task: The user request currently being worked on. Updated by
            :meth:`start_turn` on each new REPL input.
        messages: OpenAI-style chat messages, including tool results. This
            is the exact payload sent to the model, kept in provider format
            so no translation step can silently drop a field the API needs.
        plan: First assistant text of the session, if the model produced one
            before acting. Stored separately from ``messages`` so the CLI can
            show it and so future work (contrasting progress against the
            plan, or the LATS-style search sketched in ADR-020) has a named
            place to hang off.
        iterations_used: Loop iterations consumed by the most recent turn.
            Kept for the CLI and for the observability layer in Week 3.
        prompt_tokens: Cumulative prompt tokens billed this session, summed
            from each :class:`~harness.llm.protocol.LLMResponse`. Cleared
            on :meth:`reset`. Compaction and Week 3 metrics both read these
            totals; they are not a per-turn counter.
        completion_tokens: Cumulative completion tokens, same convention.
        tool_failures: Consecutive failures per tool name, for the current
            turn only. A success clears a tool's entry, so this counts a
            *streak* of failures rather than a total: a tool that fails,
            works, then fails again is not on its way to being retired.
            Reset by :meth:`start_turn`, because "this tool is broken" is a
            judgement about the task in hand, not a permanent verdict — the
            next task gets a clean slate (ADR-048).
        last_call_signature: The most recent (tool name, normalised
            arguments, observation) triple, or ``None`` before the first
            call of the turn. Compared against the next call to detect a
            loop that is repeating itself (ADR-049).
        repeated_calls: How many times in a row that signature has now been
            seen, counting the first occurrence as one. Reset by
            :meth:`start_turn` for the same reason as ``tool_failures``.
        run_id: Identifier for the run currently in progress, minted by
            :meth:`start_turn`. Empty before the first turn. It is *not* a
            metric label — Prometheus would keep one series per run — but it
            is the key the JSONL trace and the stored delegation artifacts
            are named by, which is what lets one run be reconstructed from
            the evidence (ADR-081, ADR-083).
        parent_run_id: The run that delegated this one, or ``None`` for the
            top-level agent. Set by the sub-agent factory and by nothing
            else: a context that assigns its own parent is a context that
            can lie about the execution tree (ADR-084).
        role: What kind of agent this run is — ``main``, or a registered
            specialist's role. The label metrics and traces group by.
        depth: How many delegations deep this run is. ``0`` for the parent.
            The recursion guard is structural (a child's registry has no
            ``delegate`` tool), so this is the *second* line of defence and
            the number the factory checks against
            ``subagent_max_depth`` before it builds anything.
        exit_reason: How the loop terminated — ``success``, ``capped``,
            ``no_progress`` or ``error`` — recorded by the loop at each of
            its terminal returns. It exists because a hook receives the
            context and never the loop's return value, so without it the
            observability layer has to *infer* how a run ended and cannot
            tell a repetition stop from a failure (ADR-081, ADR-084).
        compacted_calls: One line per observation that has been stubbed,
            oldest first — the tool's name and the head of what it said.
            Kept because a stub sitting where an observation used to be is
            still a hole: the model can see *that* something was compacted,
            but a run that has stubbed forty calls cannot reconstruct the
            sequence from forty scattered pointers. The summary is injected
            as one block instead (ADR-090).
        compactions: How many times this turn has stubbed old observations.
            Incremented by :meth:`compact` rather than by the loop, for the
            same reason ``exit_reason`` exists: compaction is a thing that
            happens *to* a run and a hook can otherwise not see it at all.
            Read as a difference, the way token counts are (ADR-114).
        final_answer: What the run answered — the model's final text, or
            the harness's own sentence for a capped or no-progress stop — or
            ``None`` while the run is in progress or when it ended on an
            error. Set by the loop next to ``exit_reason`` and for the same
            reason: ``on_run_end`` receives the context, never the loop's
            return value (ADR-110).
        conversation_summary: The rolling summary of earlier REPL turns that
            were folded out of :attr:`messages` to keep the conversation
            inside its budget, or empty when nothing has been folded.
            Written by
            :class:`~harness.memory.conversation.ConversationSummaryHook`
            and kept here, not on the hook, because it is conversation state:
            :meth:`reset` has to forget it with everything else (ADR-108).
    """

    task: str
    messages: list[dict[str, Any]] = field(default_factory=list)
    plan: str | None = None
    iterations_used: int = 0
    prompt_tokens: int = 0
    completion_tokens: int = 0
    tool_failures: dict[str, int] = field(default_factory=dict)
    last_call_signature: tuple[str, str, str] | None = None
    repeated_calls: int = 0
    run_id: str = ""
    parent_run_id: str | None = None
    role: str = MAIN_ROLE
    depth: int = 0
    exit_reason: str | None = None
    compactions: int = 0
    compacted_calls: list[str] = field(default_factory=list)
    final_answer: str | None = None
    conversation_summary: str = ""

    @property
    def total_tokens(self) -> int:
        """Return session prompt plus completion tokens."""
        return self.prompt_tokens + self.completion_tokens

    def record_tool_outcome(self, name: str, *, ok: bool) -> int:
        """Update one tool's failure streak and return its new length.

        Args:
            name: Tool name as the model called it, prefix included, so two
                servers offering the same remote name are counted apart.
            ok: Whether the call produced a usable observation.

        Returns:
            Consecutive failures for this tool, ``0`` after a success.
        """
        if ok:
            self.tool_failures.pop(name, None)
            return 0
        self.tool_failures[name] = self.tool_failures.get(name, 0) + 1
        return self.tool_failures[name]

    def tool_is_exhausted(self, name: str, limit: int) -> bool:
        """Return whether this tool has failed ``limit`` times in a row.

        Args:
            name: Tool name.
            limit: Failures allowed before the tool is treated as broken.
                ``0`` or less disables the guard entirely.
        """
        if limit < 1:
            return False
        return self.tool_failures.get(name, 0) >= limit

    def record_call_signature(
        self, name: str, arguments: dict[str, Any], observation: str
    ) -> int:
        """Count how many times this exact call has now repeated in a row.

        The signature is the tool name, its arguments, and the observation
        that came back. All three have to match: a tool called twice with
        the same arguments but answering differently — a clock, a directory
        that is being written to — is making progress and must not be
        counted. Arguments are serialised with sorted keys so a model that
        emits the same call with its keys in a different order is still
        recognised as repeating itself.

        Args:
            name: Tool name as the model called it.
            arguments: Arguments the model supplied.
            observation: The text the loop appended to the conversation.

        Returns:
            Length of the current run of identical calls, ``1`` for a call
            that differs from the previous one.
        """
        try:
            encoded = json.dumps(arguments, sort_keys=True, default=str)
        except (TypeError, ValueError):  # pragma: no cover - defensive
            encoded = repr(sorted(arguments.items()))
        signature = (name, encoded, observation)
        if signature == self.last_call_signature:
            self.repeated_calls += 1
        else:
            self.last_call_signature = signature
            self.repeated_calls = 1
        return self.repeated_calls

    def ensure_system_prompt(self, prompt: str) -> None:
        """Seed the system message exactly once per session.

        Guarding on emptiness rather than re-inserting keeps the system
        prompt at index 0 for the whole session. Some OpenAI-compatible
        servers only honour a leading system message, and a second one
        mid-conversation is both wasted tokens and a source of drift.

        Args:
            prompt: System instructions from :mod:`harness.prompts`.
        """
        if not self.messages:
            self.messages.append({"role": "system", "content": prompt})

    def set_system_section(
        self,
        header: str,
        body: str,
        max_chars: int = 0,
    ) -> bool:
        """Insert or replace a labelled block inside the system message.

        This is how long-term memory and the skill index reach a run without
        ``loop.py`` learning about either of them: a hook writes a section,
        the loop keeps sending ``messages`` unchanged.

        Three properties make it safe to call on every turn, which is what
        ``Hook.on_run_start`` does:

        * **Idempotent.** A section with the same header replaces the
          previous one. Appending instead would duplicate the whole memory
          block on every REPL turn, which is both a token leak and a source
          of contradictory copies as the underlying files change.
        * **Position-stable.** The section is written *into* the message at
          index 0 rather than added as a second system message. Some
          OpenAI-compatible servers only honour a leading system message,
          and ``ensure_system_prompt`` keeps the prompt pinned at index 0
          for exactly that reason.
        * **Compaction-proof.** :meth:`compact` only stubs ``tool``
          messages, so a section survives a long run. That is deliberate:
          memory that vanished once the conversation got long would be
          worse than no memory, because its absence would be invisible.

        The body is fenced and the header is expected to say that the
        content is data. That is the textual half of the defence against a
        poisoned memory file; the structural half is that no tool can
        delete, the three roots cannot reach each other, and the tool set is
        fixed when the registry is built (ADR-026, ADR-034).

        Args:
            header: Stable label, used both as the visible heading and as
                the replacement key. Must not be empty.
            body: Section content. An empty body removes the section, so a
                caller with nothing to say does not have to special-case
                it.
            max_chars: Optional ceiling on the body. Over it, the body is
                clipped and says so. ``0`` means no ceiling.

        Returns:
            Whether the system message changed.

        Raises:
            ValueError: If ``header`` is blank, or contains the fence.
            RuntimeError: If called before ``ensure_system_prompt``.
        """
        if not header.strip():
            raise ValueError("section header must not be empty")
        if SECTION_OPEN in header or SECTION_CLOSE in header:
            raise ValueError("section header must not contain the section fence")
        if not self.messages or self.messages[0].get("role") != "system":
            # Guard rather than silently seed a prompt: a caller in this
            # state has an ordering bug, and injecting memory into a
            # conversation with no system prompt would hide it.
            raise RuntimeError(
                "set_system_section requires a system message at index 0; "
                "call ensure_system_prompt first"
            )

        original = str(self.messages[0].get("content") or "")
        stripped = _remove_section(original, header)

        # Scrub the fence out of the body. Section content is untrusted: a
        # memory file the model wrote while reading a hostile document could
        # contain the closing marker, and text placed after it would appear
        # to the model as harness-authored instructions rather than as
        # recorded data. That is the one way an injected note could escalate
        # its own trust level, so it is removed structurally instead of
        # being forbidden in prose (ADR-026).
        trimmed = body.replace(SECTION_OPEN, _FENCE_REMOVED).replace(
            SECTION_CLOSE, _FENCE_REMOVED
        )
        trimmed = trimmed.strip()
        if max_chars > 0 and len(trimmed) > max_chars:
            trimmed = (
                trimmed[:max_chars] + f"\n... [{len(trimmed) - max_chars} characters "
                "of this section omitted]"
            )

        if not trimmed:
            updated = stripped
        else:
            updated = (
                f"{stripped.rstrip()}\n\n{header}\n"
                f"{SECTION_OPEN}\n{trimmed}\n{SECTION_CLOSE}"
            )

        if updated == original:
            return False
        self.messages[0]["content"] = updated
        return True

    def start_turn(self, task: str) -> None:
        """Record a new user request and append it to the conversation.

        A turn is the unit a run id names, not a session: a REPL that kept
        one id for an hour would put a morning's unrelated tasks in one
        trace file, and "inspect one agent run" is the question the id
        exists to answer. ``role``, ``parent_run_id`` and ``depth`` are
        deliberately *not* cleared here — they are properties of the agent,
        set once when the context is built, and a child that forgot it was a
        child on its second turn would vanish from the execution tree.

        Args:
            task: Raw user input for this turn.
        """
        self.task = task
        self.run_id = uuid.uuid4().hex
        self.exit_reason = None
        self.final_answer = None
        self.iterations_used = 0
        self.compactions = 0
        self.compacted_calls.clear()
        # A new task re-tries every tool: the previous one may have failed
        # because of its arguments, not because the tool is broken.
        self.tool_failures.clear()
        self.last_call_signature = None
        self.repeated_calls = 0
        # Before the user message, so a previous turn that was cut off
        # between a call and its result cannot make this one's request
        # invalid.
        self.answer_open_tool_calls()
        self.messages.append({"role": "user", "content": task})

    def add_assistant_message(
        self,
        content: str | None,
        tool_calls: list[dict[str, Any]] | None = None,
    ) -> None:
        """Append the assistant turn, including any tool calls it requested.

        The tool calls must be echoed back verbatim. The API pairs each
        ``tool`` result message with the assistant message that requested it
        via ``tool_call_id``; omitting the calls here makes the *next*
        request fail with an orphaned tool message, which is a confusing
        error to debug from the far end.

        Args:
            content: Assistant text, possibly ``None`` on a pure tool call.
            tool_calls: Provider-shaped tool call dicts, if any.
        """
        message: dict[str, Any] = {"role": "assistant", "content": content}
        if tool_calls:
            message["tool_calls"] = tool_calls
        self.messages.append(message)

    def record_usage(self, prompt_tokens: int, completion_tokens: int) -> None:
        """Add one LLM call's token counts to the session totals.

        Missing usage arrives as zeros from the client (ADR-031) and adds
        nothing. We do not estimate a substitute. Negative values are
        treated as zero so a bad payload cannot shrink the running total.

        Args:
            prompt_tokens: Prompt tokens billed for this call.
            completion_tokens: Completion tokens billed for this call.
        """
        self.prompt_tokens += max(0, prompt_tokens)
        self.completion_tokens += max(0, completion_tokens)

    def compact(
        self,
        max_chars: int,
        *,
        keep_recent: int = KEEP_RECENT_TOOL_RESULTS,
    ) -> bool:
        """Stub old tool observations when the conversation exceeds a budget.

        The system prompt, user turns, assistant messages, and the most
        recent ``keep_recent`` tool results are left alone. Older ``tool``
        messages keep their ``tool_call_id`` (the API pairs them with the
        assistant turn) but their bodies become a one-line pointer. The
        model can call the tool again; the workspace is the source of truth.

        Does not call the LLM. ``max_chars < 1`` disables compaction.

        Args:
            max_chars: Character budget for :attr:`messages`. InnKube often
                omits token usage, so this is a char cap, not a token cap.
            keep_recent: How many of the latest tool results stay verbatim.

        Returns:
            ``True`` if any observation was stubbed.
        """
        if max_chars < 1 or message_chars(self.messages) <= max_chars:
            return False
        changed = self._stub_old_tool_results(keep_recent)
        # The other half of the conversation, and on a bulk task much the
        # larger one: what the model *sent*. A hundred `write_file` calls
        # carry a hundred file bodies on their assistant messages, so a
        # compactor that only shrinks results looks at 6% of the problem
        # and reports that there is nothing it can do (ADR-090).
        changed = self._stub_old_tool_arguments(keep_recent) or changed
        # Still over: keep only the latest result verbatim.
        if message_chars(self.messages) > max_chars and keep_recent > 1:
            changed = self._stub_old_tool_results(1) or changed
            changed = self._stub_old_tool_arguments(1) or changed
        # Counted here, not at the call site. The loop discards this
        # method's return value, and compaction is exactly the kind of event
        # that is invisible until it has already cost you an answer: the
        # model stops being able to see what a tool told it, and nothing
        # anywhere says so (ADR-114).
        if changed:
            self.compactions += 1
            self._refresh_compaction_summary()
        return changed

    def _refresh_compaction_summary(self) -> None:
        """Rewrite the summary of everything stubbed so far this turn.

        A stub in place of an observation tells the model that *this* call
        was compacted. It does not tell it what the run has done overall,
        and on a long task that is the question — "have I already written
        f70.md" is answered by the list, not by forty pointers scattered
        through a conversation the model is reading past.

        Replaced rather than appended: one section, always current, at a
        stable place in the system prompt.
        """
        recorded = self.compacted_calls
        if not recorded:
            # Nothing itemised yet. A heading promising a list and
            # delivering none is worse than no heading: it reads as "the
            # record is empty" rather than "there is no record".
            return
        shown = recorded[-COMPACTION_SUMMARY_LIMIT:]
        dropped = len(recorded) - len(shown)
        lines = [f"- {entry}" for entry in shown]
        if dropped:
            lines.insert(0, f"- (…{dropped} earlier calls not listed)")
        body = (
            "Tool results below were replaced by pointers to make room. "
            "The calls themselves still happened — do not redo them to find "
            "out what they returned unless you need the full output.\n"
            + "\n".join(lines)
        )
        self.set_system_section(COMPACTION_SECTION_HEADER, body)

    def _stub_old_tool_results(self, keep_recent: int) -> bool:
        """Replace older tool bodies with stubs. Private helper for compact."""
        tool_indices = [
            index
            for index, message in enumerate(self.messages)
            if message.get("role") == "tool"
        ]
        protected = set(tool_indices[-keep_recent:]) if keep_recent > 0 else set()
        changed = False
        for index in tool_indices:
            if index in protected:
                continue
            message = self.messages[index]
            content = message.get("content")
            if not isinstance(content, str) or content.startswith(COMPACTED_PREFIX):
                continue
            name = str(message.get("name") or "tool")
            stub = _stub_observation(name, content)
            if len(stub) >= len(content):
                continue
            message["content"] = stub
            # Recorded before the body is gone. The ledger is what the
            # summary section is built from, and it is the only place the
            # order of compacted work survives.
            self.compacted_calls.append(stub[len(COMPACTED_PREFIX) :].strip())
            changed = True
        return changed

    def _stub_old_tool_arguments(self, keep_recent: int) -> bool:
        """Shrink payload-sized arguments on older assistant tool calls.

        The call's ``id`` and ``name`` are never touched: the API pairs each
        tool result with the assistant message that requested it, and an
        edit there would orphan the pair and fail the *next* request rather
        than this one. Only the oversized values inside ``arguments`` are
        replaced, so ``path`` survives and ``content`` becomes a measurement
        — which is the pointer a model needs to know the call happened
        without carrying the payload forever.

        Args:
            keep_recent: How many of the latest assistant tool-call messages
                stay verbatim, mirroring the protection on results.

        Returns:
            Whether anything was shrunk.
        """
        indices = [
            index
            for index, message in enumerate(self.messages)
            if message.get("role") == "assistant" and message.get("tool_calls")
        ]
        protected = set(indices[-keep_recent:]) if keep_recent > 0 else set()
        changed = False
        for index in indices:
            if index in protected:
                continue
            for call in self.messages[index].get("tool_calls") or []:
                function = call.get("function")
                if not isinstance(function, dict):
                    continue
                encoded = function.get("arguments")
                if not isinstance(encoded, str):
                    continue
                shrunk = _stub_arguments(encoded)
                if shrunk is not None and len(shrunk) < len(encoded):
                    function["arguments"] = shrunk
                    # Recorded the same way a stubbed result is. On a bulk
                    # task these *are* the compacted calls — the results
                    # were short enough to keep — so a ledger that only
                    # tracked results would leave the summary empty for
                    # exactly the run that needs it (ADR-090).
                    self.compacted_calls.append(
                        f"{function.get('name') or 'tool'} "
                        f"{_kept_arguments(shrunk)}".strip()
                    )
                    changed = True
        return changed

    def add_tool_result(self, tool_call_id: str, name: str, observation: str) -> None:
        """Append one tool observation.

        Args:
            tool_call_id: The id from the corresponding :class:`ToolCall`.
            name: Tool that produced the observation.
            observation: Result text, already truncated by the tool.
        """
        self.messages.append(
            {
                "role": "tool",
                "tool_call_id": tool_call_id,
                "name": name,
                "content": observation,
            }
        )

    def answer_open_tool_calls(self) -> int:
        """Give every tool call that has no result a stub result saying so.

        A turn can end between an assistant message and the results for its
        calls: Ctrl-C while a tool runs, or the no-progress guard stopping
        partway through a reply that asked for several calls. The history is
        then invalid — OpenAI-compatible endpoints reject an assistant
        ``tool_calls`` message that is not followed by one ``tool`` message
        per id — so every later turn in the session would fail on it.

        Each stub is placed directly after its siblings, where the API
        expects it, and says the call was not run so the model does not
        take it for a result.

        Returns:
            How many stubs were added.
        """
        repaired: list[dict[str, Any]] = []
        added = 0
        index = 0
        while index < len(self.messages):
            message = self.messages[index]
            repaired.append(message)
            index += 1
            calls = (
                message.get("tool_calls")
                if message.get("role") == "assistant"
                else None
            )
            if not calls:
                continue
            # The results for this message are the run of tool messages
            # that follows it; anything after that run belongs to a later
            # step.
            answered: set[str] = set()
            while (
                index < len(self.messages)
                and self.messages[index].get("role") == "tool"
            ):
                answered.add(str(self.messages[index].get("tool_call_id")))
                repaired.append(self.messages[index])
                index += 1
            for tool_call in calls:
                call_id = str(tool_call.get("id"))
                if call_id in answered:
                    continue
                name = str((tool_call.get("function") or {}).get("name", ""))
                repaired.append(
                    {
                        "role": "tool",
                        "tool_call_id": call_id,
                        "name": name,
                        "content": (
                            # The loop's ``Error:`` prefix, spelled out so
                            # this module keeps importing nothing from the
                            # harness.
                            f"Error: {name} was not run: the turn ended "
                            "before this call was reached."
                        ),
                    }
                )
                added += 1
        if added:
            # In place: callers and hooks hold a reference to this list.
            self.messages[:] = repaired
        return added

    def reset(self) -> None:
        """Clear the conversation while keeping the object identity.

        Backs the CLI's ``/reset`` command. Callers hold a reference to this
        context, so clearing in place is safer than swapping in a new object
        and hoping every holder updated.

        Identity is cleared back to a fresh top-level agent. ``/reset`` means
        "forget the conversation", and a context that kept a stale
        ``parent_run_id`` afterwards would attach the next task's trace to a
        run that ended long ago.
        """
        self.messages.clear()
        self.plan = None
        self.iterations_used = 0
        self.prompt_tokens = 0
        self.completion_tokens = 0
        self.tool_failures.clear()
        self.last_call_signature = None
        self.repeated_calls = 0
        self.run_id = ""
        self.parent_run_id = None
        self.role = MAIN_ROLE
        self.depth = 0
        self.exit_reason = None
        self.final_answer = None
        self.compactions = 0
        self.compacted_calls.clear()
        self.conversation_summary = ""
