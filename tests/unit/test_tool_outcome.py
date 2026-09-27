"""Dispatch carries the failure bit; nothing downstream re-derives it.

Before ADR-058 a tool call flattened to a string on the way out and the loop
recovered ``ok`` by testing ``observation.startswith(ERROR_PREFIX)`` — a
typed value discarded and then reconstructed by sniffing text. Three things
depend on that bit being right: the failure guard (ADR-048), the no-progress
guard (ADR-049), and the permission refusals (ADR-057).

The test that matters most here is the one with a tool whose *successful*
output begins with "Error:". Under the old design it was counted as a
failure and, five turns in, retired a tool that had worked every time.
"""

from pathlib import Path
from typing import Any

from harness.context import RunContext
from harness.loop import AgentLoop
from harness.tools import ToolRegistry, build_registry
from harness.tools.base import ERROR_PREFIX, Tool, ToolOutcome, ToolResult
from tests.fakes import ScriptedLLM, call, text


class QuotesAnErrorLog(Tool):
    """A tool that succeeds while returning text beginning with ``Error:``.

    Not a contrived case: a tool that reads a log file, greps a build output
    or fetches a page is one plausible input away from this. The point is
    that success and failure are decided by the tool, not by what its output
    happens to look like.
    """

    name = "read_build_log"
    description = "Returns a log line that starts with the word Error."
    parameters: dict[str, Any] = {"type": "object", "properties": {}}
    risk = "observe"

    def __init__(self) -> None:
        """Build the tool with a sentinel root it never touches."""
        super().__init__(Path("/nonexistent"))
        self.calls = 0

    def _run(self, arguments: dict[str, Any]) -> ToolResult:
        """Succeed, with output that looks like a failure."""
        self.calls += 1
        return ToolResult(f"{ERROR_PREFIX} the build failed on line 12")


def test_a_successful_tool_reports_ok_and_unprefixed_text(workspace: Path) -> None:
    """``invoke`` carries both halves: the text for the model, the bit for us."""
    (workspace / "a.txt").write_text("contents")
    outcome = build_registry(workspace).invoke("read_file", {"path": "a.txt"})

    assert outcome.ok is True
    assert "contents" in outcome.text
    assert not outcome.text.startswith(ERROR_PREFIX)


def test_a_failing_tool_reports_not_ok_and_keeps_the_prefix(workspace: Path) -> None:
    """The prefix stays: it is the model's only signal that the call failed."""
    outcome = build_registry(workspace).invoke("read_file", {"path": "missing.txt"})

    assert outcome.ok is False
    assert outcome.text.startswith(ERROR_PREFIX)


def test_an_unknown_tool_name_is_a_failed_outcome(workspace: Path) -> None:
    """A hallucinated name is counted as a failure, not silently tolerated."""
    outcome = build_registry(workspace).invoke("teleport", {})

    assert outcome.ok is False
    assert "unknown tool" in outcome.text


def test_execute_is_the_text_of_invoke(workspace: Path) -> None:
    """The string form stays exactly the observation, so no caller sees a change."""
    registry = build_registry(workspace)
    for arguments in ({"path": "a.txt"}, {}):
        assert registry.execute("read_file", arguments) == (
            registry.invoke("read_file", arguments).text
        )


def test_output_that_looks_like_an_error_is_not_counted_as_one() -> None:
    """The regression the type exists to prevent.

    Five successful calls whose output begins with "Error:". Under text
    sniffing this hit ``max_tool_failures`` and retired a working tool; with
    the bit carried, the streak stays at zero and the tool keeps running.
    """
    tool = QuotesAnErrorLog()
    registry = ToolRegistry()
    registry.register(tool)
    loop = AgentLoop(
        llm=ScriptedLLM(
            [call("read_build_log", call_id=f"c{index}") for index in range(5)]
            + [text("done")]
        ),
        registry=registry,
        max_iterations=0,
        max_tool_failures=5,
        # Identical calls with identical results are what this tool does by
        # design, so the no-progress guard is off: it is not under test.
        max_repeated_calls=0,
    )
    context = RunContext(task="read the log")

    assert loop.run(context) == "done"
    assert tool.calls == 5
    assert context.tool_failures.get("read_build_log", 0) == 0


def test_tool_outcome_defaults_to_ok() -> None:
    """The common case is success, so it is the default, matching ToolResult."""
    assert ToolOutcome("some output").ok is True
