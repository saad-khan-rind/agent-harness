"""Reading a trace back: the tree, the diagnosis, and old files.

:mod:`harness.observability.replay` is the half of observability a person
uses when something has already gone wrong, so these tests are written as
the questions somebody actually asks at that moment — *which layer failed*,
*where did the time go*, *what did the sub-agent do* — rather than as
assertions on formatting.

Two properties get most of the attention because they are the ones that are
easy to lose:

* **The tree is read, not guessed.** The Week 3 reader paired a permission
  line with the tool line after it by adjacency. That is correct until a
  model requests two tool calls in one turn, at which point it silently
  attributes a decision to the wrong call (ADR-093).
* **Old files still open.** There are v1 traces on the memory volume we
  demo from. A reader that could not open them would have discarded the
  evidence from every run before this week (ADR-094).
"""

import json
from pathlib import Path
from typing import Any

import pytest
from harness.context import RunContext
from harness.loop import AgentLoop
from harness.observability.api import Tracer
from harness.observability.evals import (
    QualityStore,
    RunQuality,
)
from harness.observability.exporters.jsonl import JsonlSpanExporter
from harness.observability.replay import (
    TraceNotFoundError,
    find_trace,
    load_trace,
    render_run,
)
from harness.observability.telemetry import TelemetryHook
from harness.tools.base import Tool, ToolResult
from harness.tools.registry import ToolRegistry
from tests.fakes import ScriptedLLM, call, parallel_calls, text


class _FlakyTool(Tool):
    """Succeeds or fails per call, so a run can have a mixed history."""

    name = "lookup"
    description = "Look a value up in the ledger and return what it says."
    parameters: dict[str, Any] = {
        "type": "object",
        "properties": {"key": {"type": "string", "description": "What to look up."}},
        "required": ["key"],
    }
    risk = "observe"

    def __init__(self, root: Path, failures: set[str] | None = None) -> None:
        super().__init__(root)
        self.failures = failures or set()

    def _run(self, arguments: dict[str, Any]) -> ToolResult:
        key = str(arguments.get("key", ""))
        if key in self.failures:
            raise FileNotFoundError(f"no such key: {key}")
        return ToolResult(f"{key} = 42")


def _trace(
    tmp_path: Path,
    responses: list[Any],
    *,
    failures: set[str] | None = None,
    **hook_kwargs: Any,
) -> Path:
    """Run a real loop into a real trace file and return the directory."""
    traces = tmp_path / "traces"
    hook = TelemetryHook(Tracer([JsonlSpanExporter(traces)]), **hook_kwargs)
    registry = ToolRegistry()
    registry.register(_FlakyTool(tmp_path, failures))
    context = RunContext(task="")
    context.ensure_system_prompt("system")
    context.start_turn("do the thing")
    AgentLoop(llm=ScriptedLLM(responses), registry=registry, hook=hook).run(context)
    return traces


# --- the tree ---------------------------------------------------------------


def test_the_tree_is_read_from_parentage_not_from_adjacency(
    tmp_path: Path,
) -> None:
    """Two tool calls in one turn is where the old reader went wrong.

    The model requests both at once, so the events interleave in a way no
    adjacency rule survives: each decision has to be attached to the call it
    actually decided, and since the span rewrite that is a field rather than
    a guess (ADR-093).
    """
    traces = _trace(
        tmp_path,
        [
            parallel_calls(("lookup", {"key": "a"}), ("lookup", {"key": "b"})),
            text("done"),
        ],
    )
    run = load_trace(find_trace(traces, "last"))

    assert run.root is not None
    tools = [node for node in run.root.walk() if node.name == "tool.call"]
    assert len(tools) == 2
    for tool in tools:
        # Each call owns exactly one decision, and it is its own child.
        decisions = [
            child for child in tool.children if child.name == "permission.check"
        ]
        assert len(decisions) == 1
        assert decisions[0].record["tool"] == tool.record["tool"]


def test_a_run_renders_with_its_totals_and_time_split(tmp_path: Path) -> None:
    """The three numbers a person wants without counting lines.

    The third — time that was neither the model nor a tool — is the
    harness's own cost, and printing it is how "observability is low
    overhead" stops being a claim and becomes a measurement (ADR-093).
    """
    traces = _trace(
        tmp_path,
        [
            call("lookup", key="a", prompt_tokens=100, completion_tokens=10),
            text("done", prompt_tokens=120, completion_tokens=15),
        ],
    )
    rendered = render_run(load_trace(find_trace(traces, "last")))

    assert "answered" in rendered
    assert "2 model calls" in rendered
    assert "1 tool call" in rendered
    assert "245 tokens" in rendered
    assert "model" in rendered and "tools" in rendered and "harness" in rendered


# --- diagnosis --------------------------------------------------------------


def test_failures_are_reported_by_layer(tmp_path: Path) -> None:
    """The question §9 is really asking: *whose fault*, in one line."""
    traces = _trace(
        tmp_path,
        [call("lookup", key="missing"), text("done")],
        failures={"missing"},
    )
    rendered = render_run(load_trace(find_trace(traces, "last")))

    assert "failures by layer: tool (1)" in rendered
    assert "tool.not_found" in rendered


# --- quality ----------------------------------------------------------------


def test_a_verdict_is_stored_against_the_run_that_earned_it(
    tmp_path: Path,
) -> None:
    """The architectural requirement: evaluation results join to a trace.

    Where the verdict *comes from* is left open on purpose — a person, a
    test with a known answer, a future scorer — and the ``source`` field is
    what keeps them distinguishable when they are mixed (ADR-096).
    """
    traces = _trace(tmp_path, [text("done")])
    run = load_trace(find_trace(traces, "last"))
    store = QualityStore(traces)

    assert store.record(
        RunQuality(
            run_id=run.run_id,
            task_success=False,
            failure_reason="wrong_output",
            source="test",
            note="wrote the file in the wrong folder",
            signals=run.signals,
        )
    )

    reloaded = load_trace(find_trace(traces, "last"), store)
    assert reloaded.quality is not None
    assert reloaded.quality.outcome == "failure"
    assert reloaded.quality.source == "test"
    assert "task: failure [wrong_output] by test" in render_run(reloaded)


def test_system_agent_and_task_success_are_three_different_things(
    tmp_path: Path,
) -> None:
    """The distinction the whole quality layer exists for.

    Every request returned and the loop answered, so system and agent
    success are both true — and the task was still done wrong. Nothing in
    the harness can know the third one, which is exactly why it is recorded
    rather than derived (ADR-096).
    """
    traces = _trace(tmp_path, [text("done")])
    run = load_trace(find_trace(traces, "last"))

    assert run.signals.system_ok is True
    assert run.signals.agent_ok is True

    store = QualityStore(traces)
    store.record(RunQuality(run_id=run.run_id, task_success=False, source="test"))
    assert store.load(run.run_id).task_success is False  # type: ignore[union-attr]


# --- old files --------------------------------------------------------------


_V1_TRACE = [
    {
        "ts": "2026-09-14T10:00:00.000Z",
        "run_id": "a" * 32,
        "parent_run_id": None,
        "role": "main",
        "event": "run_start",
        "tool": None,
        "permission": None,
        "ok": None,
        "latency_ms": None,
        "tokens_prompt": None,
        "tokens_completion": None,
        "exit_reason": None,
    },
    {
        "ts": "2026-09-14T10:00:02.000Z",
        "run_id": "a" * 32,
        "parent_run_id": None,
        "role": "main",
        "event": "llm",
        "tool": None,
        "permission": None,
        "ok": None,
        "latency_ms": 1800,
        "tokens_prompt": 120,
        "tokens_completion": 30,
        "exit_reason": None,
    },
    {
        "ts": "2026-09-14T10:00:02.100Z",
        "run_id": "a" * 32,
        "parent_run_id": None,
        "role": "main",
        "event": "permission",
        "tool": "write_file",
        "permission": "deny",
        "ok": None,
        "latency_ms": None,
        "tokens_prompt": None,
        "tokens_completion": None,
        "exit_reason": None,
    },
    {
        "ts": "2026-09-14T10:00:02.200Z",
        "run_id": "a" * 32,
        "parent_run_id": None,
        "role": "main",
        "event": "tool",
        "tool": "write_file",
        "permission": None,
        "ok": False,
        "latency_ms": 12,
        "tokens_prompt": None,
        "tokens_completion": None,
        "exit_reason": None,
    },
    {
        "ts": "2026-09-14T10:00:05.000Z",
        "run_id": "a" * 32,
        "parent_run_id": None,
        "role": "main",
        "event": "run_end",
        "tool": None,
        "permission": None,
        "ok": True,
        "latency_ms": 5000,
        "tokens_prompt": 120,
        "tokens_completion": 30,
        "exit_reason": "success",
    },
]


def test_a_week3_trace_still_opens(tmp_path: Path) -> None:
    """There are v1 files on the volume we demo from.

    A reader that could not open them would have thrown away the evidence
    from every run before this week — which is a strange way to ship an
    improvement to observability (ADR-094).
    """
    path = tmp_path / f"{'a' * 32}.jsonl"
    path.write_text(
        "\n".join(json.dumps(record) for record in _V1_TRACE) + "\n", encoding="utf-8"
    )

    run = load_trace(path)
    assert run.schema_version == 1
    assert run.exit_reason == "success"
    assert run.signals.tool_calls == 1
    assert run.signals.tool_failures == 1
    assert run.signals.permission_refusals == 1
    assert run.total_tokens == 150

    rendered = render_run(run)
    assert "[v1 trace]" in rendered
    assert "write_file" in rendered


# --- finding a run ----------------------------------------------------------


def test_a_run_is_found_by_any_unique_prefix(tmp_path: Path) -> None:
    """Enough of the id to be unique, as ``git`` does it."""
    traces = _trace(tmp_path, [text("done")])
    (path,) = traces.glob("*.jsonl")

    assert find_trace(traces, path.stem[:6]) == path
    assert find_trace(traces, path.stem) == path
    assert find_trace(traces, "last") == path


def test_a_missing_run_says_what_was_looked_for(tmp_path: Path) -> None:
    """An error a person can act on rather than a stack trace."""
    traces = _trace(tmp_path, [text("done")])

    with pytest.raises(TraceNotFoundError, match="no run id starts with"):
        find_trace(traces, "zzzz")
    with pytest.raises(TraceNotFoundError, match="no traces directory"):
        find_trace(tmp_path / "nowhere", "last")


# --- signals ----------------------------------------------------------------
