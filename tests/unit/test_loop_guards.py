"""The three guards that bound the loop, tested one at a time.

Week 1 bounded the machine with one fixed number — twenty iterations — which
is the brief's "iteration caps" example of a guard condition. It also cut off
long, healthy tasks, and the number was arbitrary.

The cap is now optional (``max_iterations: 0``, the shipped default) and the
job of stopping a runaway moved to a guard aimed at the actual failure mode:
a model that calls a broken tool, reads the error, and calls it again
unchanged. After ``max_tool_failures`` consecutive failures that tool is not
executed again for the rest of the task (ADR-047, ADR-048).

A third guard catches the half the failure limit cannot see: a model that
repeats a call which *works*. Three identical calls in a row — same tool,
same arguments, same observation — stop the run as making no progress
(ADR-049).

All three are tested here against the real ``AgentLoop`` and a real
``ToolRegistry``, with only the model scripted.

The guards compose, which means a test of one has to switch the others off
to stay a test of one thing. The cap and failure-limit tests below drive the
same call repeatedly on purpose, so they pass ``max_repeated_calls=0``; the
last section then tests the composition deliberately, with everything on.
"""

from typing import Any

from harness.context import RunContext
from harness.loop import AgentLoop
from harness.tools.base import ERROR_PREFIX, Tool, ToolResult
from harness.tools.registry import ToolRegistry
from tests.fakes import ScriptedLLM, call, malformed_call, parallel_calls, text


class AlwaysFails(Tool):
    """A tool that never works, and counts how often it was asked to."""

    name = "always_fails"
    description = "Fails every time it is called, for testing the guard."
    parameters: dict[str, Any] = {"type": "object", "properties": {}}
    risk = "observe"

    def __init__(self) -> None:
        """Build the tool with a sentinel root it never touches."""
        super().__init__(__import__("pathlib").Path("/nonexistent"))
        self.calls = 0

    def _run(self, arguments: dict[str, Any]) -> ToolResult:
        """Record the attempt and fail."""
        self.calls += 1
        return ToolResult("the upstream service is down", ok=False)


class FlakyTool(Tool):
    """Fails or succeeds according to a script, so a streak can be broken."""

    name = "flaky"
    description = "Succeeds or fails according to a fixed script, for tests."
    parameters: dict[str, Any] = {"type": "object", "properties": {}}
    risk = "observe"

    def __init__(self, outcomes: list[bool]) -> None:
        """Args:
        outcomes: One entry per call, ``True`` for success.
        """
        super().__init__(__import__("pathlib").Path("/nonexistent"))
        self._outcomes = list(outcomes)
        self.calls = 0

    def _run(self, arguments: dict[str, Any]) -> ToolResult:
        """Return the next scripted outcome."""
        ok = self._outcomes[self.calls] if self.calls < len(self._outcomes) else False
        self.calls += 1
        return ToolResult("worked" if ok else "broke", ok=ok)


def _registry(tool: Tool) -> ToolRegistry:
    """Return a registry holding exactly one tool."""
    registry = ToolRegistry()
    registry.register(tool)
    return registry


def _context(task: str = "do the thing") -> RunContext:
    """Return a context with the task recorded, as the CLI would."""
    context = RunContext(task="")
    context.start_turn(task)
    return context


# --- the optional iteration cap --------------------------------------------


def test_no_cap_lets_a_long_task_finish() -> None:
    """Thirty tool calls used to be impossible; with no cap they are not.

    The old default stopped at twenty, so a task genuinely needing more was
    reported as incomplete even though nothing had gone wrong.
    """
    tool = FlakyTool([True] * 30)
    script = ScriptedLLM(
        [call("flaky", call_id=str(i)) for i in range(30)] + [text("Done.")]
    )
    loop = AgentLoop(script, _registry(tool), max_iterations=0, max_repeated_calls=0)

    context = _context()
    assert loop.run(context) == "Done."
    assert tool.calls == 30
    assert context.iterations_used == 31


def test_a_configured_cap_still_stops_the_run() -> None:
    """The ceiling is optional, not gone — an unattended run can set one."""
    tool = FlakyTool([True] * 10)
    script = ScriptedLLM(
        [call("flaky", call_id=str(i)) for i in range(10)] + [text("Done.")]
    )
    loop = AgentLoop(script, _registry(tool), max_iterations=3, max_repeated_calls=0)

    context = _context()
    answer = loop.run(context)

    assert "Stopped after the maximum of 3 iterations" in answer
    assert context.iterations_used == 3


# --- the per-tool failure limit --------------------------------------------


def test_a_tool_is_retired_after_five_consecutive_failures() -> None:
    """The sixth call is never executed, and the model is told why."""
    tool = AlwaysFails()
    script = ScriptedLLM(
        [call("always_fails", call_id=str(i)) for i in range(8)] + [text("I gave up.")]
    )
    loop = AgentLoop(
        script,
        _registry(tool),
        max_iterations=0,
        max_tool_failures=5,
        max_repeated_calls=0,
    )

    context = _context()
    loop.run(context)

    # Five attempts reached the tool; everything after that was refused by
    # the loop without spending a call.
    assert tool.calls == 5

    observations = [m["content"] for m in context.messages if m.get("role") == "tool"]
    assert len(observations) == 8
    assert all(str(o).startswith(ERROR_PREFIX) for o in observations)
    assert "failed 5 times in a row" in str(observations[4])
    assert "treated as not working" in str(observations[7])


def test_a_success_clears_the_streak() -> None:
    """Four failures, a success, four more failures: still not retired.

    The guard counts a *streak*, not a total. A tool that works intermittently
    — a rate-limited server, say — must not be retired for the rest of the
    task on the strength of failures it has already recovered from.
    """
    outcomes = [False] * 4 + [True] + [False] * 4
    tool = FlakyTool(outcomes)
    script = ScriptedLLM(
        [call("flaky", call_id=str(i)) for i in range(9)] + [text("Done.")]
    )
    loop = AgentLoop(
        script,
        _registry(tool),
        max_iterations=0,
        max_tool_failures=5,
        max_repeated_calls=0,
    )

    context = _context()
    loop.run(context)

    assert tool.calls == 9  # every call reached the tool


# --- the no-progress guard -------------------------------------------------


class EchoTool(Tool):
    """Returns its arguments, so a call's output depends on its input."""

    name = "echo"
    description = "Echo the arguments back, for testing the repeat guard."
    parameters: dict[str, Any] = {
        "type": "object",
        "properties": {"a": {"type": "string"}, "b": {"type": "string"}},
    }
    risk = "observe"

    def __init__(self) -> None:
        """Build the tool with a sentinel root it never touches."""
        super().__init__(__import__("pathlib").Path("/nonexistent"))
        self.calls = 0

    def _run(self, arguments: dict[str, Any]) -> ToolResult:
        """Echo the arguments in a stable order."""
        self.calls += 1
        body = ",".join(f"{k}={arguments[k]}" for k in sorted(arguments))
        return ToolResult(f"echo {body}")


class ChangingTool(Tool):
    """Succeeds every time, with a different answer every time.

    Stands in for a clock, a queue, or a directory being written to: the
    call is identical, the world is not, and the run is making progress.
    """

    name = "changing"
    description = "Return a different answer on each call, for tests."
    parameters: dict[str, Any] = {"type": "object", "properties": {}}
    risk = "observe"

    def __init__(self) -> None:
        """Build the tool with a sentinel root it never touches."""
        super().__init__(__import__("pathlib").Path("/nonexistent"))
        self.calls = 0

    def _run(self, arguments: dict[str, Any]) -> ToolResult:
        """Return a fresh answer."""
        self.calls += 1
        return ToolResult(f"reading {self.calls}")


def test_three_identical_calls_stop_the_run() -> None:
    """The user-facing behaviour: same call, same result, three times, stop.

    Without this the loop has no answer to a model that is stuck in a
    groove, because the tool is not failing and (with no cap) nothing is
    counting down.
    """
    tool = FlakyTool([True] * 10)
    script = ScriptedLLM(
        [call("flaky", call_id=str(i)) for i in range(10)] + [text("Done.")]
    )
    loop = AgentLoop(script, _registry(tool), max_iterations=0, max_repeated_calls=3)

    context = _context()
    answer = loop.run(context)

    assert tool.calls == 3
    assert answer.startswith("Stopped:")
    assert "3 times in a row" in answer
    assert "flaky" in answer


def test_a_changing_result_is_progress() -> None:
    """Identical arguments are not enough: the observation must match too.

    A clock, a queue length or a file being written to answers differently
    each time it is asked, and asking again is exactly the right thing to
    do. Keying the guard on the observation as well as the call is what
    keeps those tasks working.
    """
    tool = ChangingTool()
    script = ScriptedLLM(
        [call("changing", call_id=str(i)) for i in range(6)] + [text("Done.")]
    )
    loop = AgentLoop(script, _registry(tool), max_iterations=0, max_repeated_calls=3)

    assert loop.run(_context()) == "Done."
    assert tool.calls == 6


def test_the_failure_limit_gets_to_act_before_the_repeat_guard() -> None:
    """With both guards at their shipped values, five happens before ten.

    This is why ``max_repeated_calls`` is 10 and not 3. A tool failing
    identically every time is ADR-048's case, and it must be allowed to run
    its course: five real attempts, then a retirement verdict the model can
    act on. Setting the repeat guard near the failure limit would cut that
    short and stop the whole run where retiring one tool would have done.

    Note the streak never even reaches 10 here, because the fifth
    observation carries the verdict and so differs from the four before it.
    The repeat guard is a backstop for a model that ignores the verdict,
    not the thing that handles a broken tool.
    """
    tool = AlwaysFails()
    script = ScriptedLLM(
        [call("always_fails", call_id=str(i)) for i in range(8)] + [text("Gave up.")]
    )
    loop = AgentLoop(
        script,
        _registry(tool),
        max_iterations=0,
        max_tool_failures=5,
        max_repeated_calls=10,
    )

    context = _context()
    answer = loop.run(context)

    assert tool.calls == 5  # ADR-048 retired it; ADR-049 never intervened
    assert answer == "Gave up."
    # The counter stops at the limit: past it the tool is not executed, so
    # there is no outcome to record.
    assert context.tool_failures["always_fails"] == 5


# --- the context bookkeeping under it --------------------------------------


# --- arguments that never parsed -------------------------------------------


def test_malformed_arguments_are_answered_without_running_the_tool() -> None:
    """A call whose JSON did not parse never became a call.

    Running the tool with ``{}`` produced "missing required argument 'text'"
    for a model that *had* sent the text — a correction for the wrong
    mistake, which is what turned one malformed call into a repeating one.
    """
    tool = FlakyTool([True])
    script = ScriptedLLM([malformed_call("flaky"), text("Done.")])
    loop = AgentLoop(script, _registry(tool), max_iterations=0)

    loop.run(_context())

    assert tool.calls == 0


def test_repeatedly_malformed_arguments_retire_the_tool() -> None:
    """The failure guard still applies, so a model that cannot emit valid
    JSON for one tool stops being offered the chance.

    Without this, a malformed call costs nothing to repeat and the run only
    ends at the iteration cap — which is exactly the shape of the observed
    failure, and why these are counted as failures of that tool.
    """
    tool = FlakyTool([True] * 10)
    script = ScriptedLLM(
        [malformed_call("flaky", call_id=str(index)) for index in range(6)]
        + [text("Done.")]
    )
    loop = AgentLoop(
        script,
        _registry(tool),
        max_iterations=0,
        max_tool_failures=3,
        max_repeated_calls=0,
    )
    context = _context()

    loop.run(context)

    observations = [
        str(message.get("content", ""))
        for message in context.messages
        if message.get("role") == "tool"
    ]
    assert any(
        "unavailable" in o.lower() or "failed" in o.lower() for o in observations
    )
    assert tool.calls == 0


# --- the answer is on the context before on_run_end (ADR-110) --------------


class _AnswerAtRunEnd:
    """A hook that records what ``on_run_end`` could see of the answer."""

    def __init__(self) -> None:
        self.seen: list[tuple[str | None, str | None]] = []

    def __getattr__(self, name: str) -> Any:
        """Every other callback is a no-op."""
        return lambda *args, **kwargs: None

    def on_run_end(self, context: RunContext) -> None:
        self.seen.append((context.exit_reason, context.final_answer))


def test_every_normal_exit_leaves_its_answer_for_the_run_end_hooks() -> None:
    """Success, the cap and the no-progress stop all set ``final_answer``.

    A hook receives the context, never the loop's return value, so the
    answer has to be on the context by the time ``on_run_end`` fires — that
    is what lets the console print it before memory extraction runs.
    """
    hook = _AnswerAtRunEnd()
    success = AgentLoop(
        ScriptedLLM([text("Done.")]), _registry(FlakyTool([])), hook=hook
    )
    capped = AgentLoop(
        ScriptedLLM([call("flaky", call_id=str(i)) for i in range(3)]),
        _registry(FlakyTool([True] * 3)),
        hook=hook,
        max_iterations=2,
        max_repeated_calls=0,
    )
    stuck = AgentLoop(
        ScriptedLLM([call("always_fails", call_id=str(i)) for i in range(3)]),
        _registry(AlwaysFails()),
        hook=hook,
        max_repeated_calls=3,
    )

    answers = [loop.run(_context()) for loop in (success, capped, stuck)]

    assert [reason for reason, _ in hook.seen] == ["success", "capped", "no_progress"]
    assert [answer for _, answer in hook.seen] == answers


def test_a_run_that_fails_has_no_answer() -> None:
    """An endpoint error ends the run without one; the caller reports it."""
    hook = _AnswerAtRunEnd()
    loop = AgentLoop(ScriptedLLM([]), _registry(FlakyTool([])), hook=hook)

    try:
        loop.run(_context())
    except AssertionError:
        pass

    assert hook.seen == [("error", None)]


def test_a_no_progress_stop_mid_reply_leaves_a_valid_history() -> None:
    """Calls the guard skipped are answered, so the next turn can be sent.

    The repetition guard returns from inside the loop over one reply's tool
    calls. Any call after the one that tripped it was never run and has no
    result, and a history with an unanswered call is rejected by the
    endpoint on the next REPL turn.
    """
    tool = FlakyTool([True] * 10)
    registry = _registry(tool)
    llm = ScriptedLLM(
        [
            call("flaky", call_id="c1"),
            parallel_calls(("flaky", {}), ("flaky", {})),
            text("second answer"),
        ]
    )
    loop = AgentLoop(llm, registry, max_repeated_calls=2)
    context = RunContext(task="")
    context.ensure_system_prompt("SYSTEM")
    context.start_turn("first")
    assert loop.run(context).startswith("Stopped:")

    context.start_turn("second")
    assert loop.run(context) == "second answer"

    sent = llm.calls[-1][0]
    answered = {m.get("tool_call_id") for m in sent if m["role"] == "tool"}
    requested = {
        tool_call["id"]
        for m in sent
        if m["role"] == "assistant"
        for tool_call in m.get("tool_calls") or []
    }
    assert requested <= answered
