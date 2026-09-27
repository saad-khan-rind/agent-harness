"""The ReAct control plane.

Every test here runs the real ``AgentLoop`` against a scripted model, so the
state machine, the context bookkeeping, and the tool wiring are all covered
without a network call or an API key.
"""

from pathlib import Path
from typing import Any

import pytest
from harness.context import RunContext
from harness.llm.protocol import LLMError
from harness.loop import AgentLoop
from harness.tools import ToolRegistry
from tests.fakes import FailingLLM, ScriptedLLM, call, text


def _context(task: str) -> RunContext:
    """Build a context the way the CLI does, ready for one turn."""
    context = RunContext(task="")
    context.ensure_system_prompt("SYSTEM")
    context.start_turn(task)
    return context


def test_text_only_response_ends_the_run(registry: ToolRegistry) -> None:
    """No tool calls is the loop's only success exit."""
    llm = ScriptedLLM([text("All done.")])
    assert AgentLoop(llm, registry).run(_context("hi")) == "All done."


def test_tool_call_then_answer(registry: ToolRegistry, workspace: Path) -> None:
    """The canonical reason-act-observe-reason cycle."""
    llm = ScriptedLLM(
        [
            call("create_folder", path="output"),
            text("Created the folder."),
        ]
    )
    context = _context("make a folder called output")

    assert AgentLoop(llm, registry).run(context) == "Created the folder."
    assert (workspace / "output").is_dir()
    assert context.iterations_used == 2


def test_tool_error_is_fed_back_instead_of_raising(
    registry: ToolRegistry, workspace: Path
) -> None:
    """A failed tool call must not end the run.

    The model reads the error and corrects itself — the brief's 'errors must
    be communicated upstream effectively'.
    """
    llm = ScriptedLLM(
        [
            call("read_file", call_id="1", path="missing.txt"),
            call("write_file", call_id="2", path="missing.txt", content="now here"),
            text("Recovered."),
        ]
    )
    assert AgentLoop(llm, registry).run(_context("read then fix")) == "Recovered."
    assert (workspace / "missing.txt").read_text(encoding="utf-8") == "now here"

    observations = [m for m in llm.calls[1][0] if m["role"] == "tool"]
    assert observations[0]["content"].startswith("Error:")


def test_path_escape_is_fed_back_and_the_run_continues(
    registry: ToolRegistry,
) -> None:
    """A refused escape is an observation, so the agent can retry legally."""
    llm = ScriptedLLM(
        [
            call("write_file", call_id="1", path="../evil.txt", content="x"),
            text("I cannot write outside the workspace."),
        ]
    )
    AgentLoop(llm, registry).run(_context("write outside"))
    observation = [m for m in llm.calls[1][0] if m["role"] == "tool"][0]
    assert "escapes the workspace" in observation["content"]


def test_unknown_tool_is_fed_back(registry: ToolRegistry) -> None:
    """A hallucinated tool name is recoverable within the same run."""
    llm = ScriptedLLM([call("delete_file", path="a.txt"), text("No such tool.")])
    AgentLoop(llm, registry).run(_context("delete something"))
    observation = [m for m in llm.calls[1][0] if m["role"] == "tool"][0]
    assert "unknown tool" in observation["content"]


def test_several_tool_calls_in_one_turn(
    registry: ToolRegistry, workspace: Path
) -> None:
    """Models may batch calls; all of them must run, in order."""
    from harness.llm.protocol import LLMResponse, ToolCall

    batched = LLMResponse(
        content=None,
        tool_calls=[
            ToolCall(id="1", name="create_folder", arguments={"path": "a"}),
            ToolCall(id="2", name="create_folder", arguments={"path": "b"}),
        ],
    )
    llm = ScriptedLLM([batched, text("both made")])
    AgentLoop(llm, registry).run(_context("make two folders"))

    assert (workspace / "a").is_dir()
    assert (workspace / "b").is_dir()
    assert len([m for m in llm.calls[1][0] if m["role"] == "tool"]) == 2


def test_llm_error_propagates(registry: ToolRegistry) -> None:
    """An endpoint failure is the user's problem, not the agent's to retry."""
    loop = AgentLoop(FailingLLM(LLMError("innkube is down")), registry)
    with pytest.raises(LLMError, match="innkube is down"):
        loop.run(_context("hi"))


def test_multi_turn_context_is_preserved(registry: ToolRegistry) -> None:
    """A second task sees the first one's history — the multi-turn requirement."""
    llm = ScriptedLLM([text("first"), text("second")])
    loop = AgentLoop(llm, registry)
    context = _context("one")
    loop.run(context)

    context.start_turn("two")
    loop.run(context)

    roles = [m["role"] for m in llm.calls[1][0]]
    assert roles == ["system", "user", "assistant", "user"]


def test_loop_compacts_before_the_llm_call(registry: ToolRegistry) -> None:
    """The model must see stubbed old dumps, not the pre-compact transcript."""
    context = _context("go")
    for index in range(6):
        call_id = f"c{index}"
        context.add_assistant_message(
            None,
            [
                {
                    "id": call_id,
                    "type": "function",
                    "function": {"name": "read_file", "arguments": "{}"},
                }
            ],
        )
        context.add_tool_result(call_id, "read_file", ("x" * 400) + f" file{index}")

    llm = ScriptedLLM([text("done")])
    AgentLoop(llm, registry, max_context_chars=1200).run(context)

    tools = [message for message in llm.calls[0][0] if message["role"] == "tool"]
    assert any("[compacted]" in str(message["content"]) for message in tools)
    # Pairing must survive: every tool message still has its id.
    assert all("tool_call_id" in message for message in tools)


def test_hook_fires_around_the_run_and_each_tool(registry: ToolRegistry) -> None:
    """The hook seam is what Week 3 observability attaches to."""
    events: list[str] = []

    class _Recorder:
        def on_run_start(self, context: RunContext) -> None:
            events.append("run_start")

        def before_llm(self, context: RunContext) -> None:
            events.append("before_llm")

        def after_llm(self, context: RunContext, response: Any) -> None:
            events.append("after_llm")

        def before_tool(self, name: str, arguments: dict[str, Any]) -> None:
            events.append(f"before_tool:{name}")

        def after_permission(self, name: str, outcome: str) -> None:
            events.append(f"after_permission:{outcome}")

        def after_tool(self, name: str, result: str) -> None:
            events.append("after_tool")

        def on_run_end(self, context: RunContext) -> None:
            events.append("run_end")

    llm = ScriptedLLM([call("list_files", path="."), text("done")])
    AgentLoop(llm, registry, hook=_Recorder()).run(_context("look"))

    assert events[0] == "run_start"
    assert events[-1] == "run_end"
    assert "before_tool:list_files" in events
    # The decision is reported between asking about the tool and running it,
    # which is what lets an observer tell a refusal from a tool that failed
    # by itself (ADR-081).
    assert (
        events.index("before_tool:list_files")
        < events.index("after_permission:allow")
        < events.index("after_tool")
    )


def _with_content(response: Any, content: str) -> Any:
    """Attach assistant text to a scripted tool-call response."""
    response.content = content
    return response
