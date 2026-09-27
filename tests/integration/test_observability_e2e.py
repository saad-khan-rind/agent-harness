"""One run, every mechanism, and the telemetry that reconstructs it.

The brief asks for "at least one end-to-end scenario where an agent performs
multiple operations and the resulting telemetry demonstrates the complete
execution path". This is that scenario, and it is deliberately a *single*
run rather than a suite of small ones: the claim being tested is that the
pieces compose, and a set of isolated checks can all pass while the
composition is broken.

The run does, in order:

1. reads long-term memory (a harness-driven retrieval, no tool call),
2. calls a built-in tool successfully,
3. calls an MCP tool that fails on a dead server,
4. has a mutating call refused by the permission policy,
5. delegates to a sub-agent, which makes its own model and tool calls,
6. retries a rate-limited model request,
7. and answers.

Afterwards the trace file alone — not this process's memory of what it did
— has to be enough to reconstruct all seven, attribute the two failures to
the right layers, and show the sub-agent's work nested inside the
delegation that caused it. Everything is asserted by *reading the file back
through the public reader*, because that is what a person does at 2am and
because a test that inspected live span objects would prove the exporter
unnecessary (ADR-093, ADR-094).

Driven through the real :class:`~harness.loop.AgentLoop`, the real
:class:`~harness.tools.registry.ToolRegistry`, the real permission policy
and the real sub-agent factory, with only the model replaced (ADR-014).
"""

import json
from pathlib import Path
from typing import Any

import pytest
from harness.context import RunContext
from harness.hooks import CompositeHook
from harness.llm.protocol import LLMError, LLMResponse, ToolCall
from harness.loop import AgentLoop
from harness.observability.api import Tracer
from harness.observability.exporters.jsonl import JsonlSpanExporter
from harness.observability.exporters.metrics import MetricsSpanExporter
from harness.observability.recorder import PrometheusRecorder
from harness.observability.replay import find_trace, load_trace
from harness.observability.telemetry import TelemetryHook
from harness.permissions import RiskBasedPolicy
from harness.tools.base import Tool, ToolResult
from harness.tools.registry import ToolRegistry
from prometheus_client import CollectorRegistry
from tests.fakes import text

# --- the world the agent acts in --------------------------------------------


class _ReadLedger(Tool):
    """A built-in tool that works."""

    name = "read_ledger"
    description = "Read the ledger file and report how many entries it holds."
    parameters: dict[str, Any] = {
        "type": "object",
        "properties": {"path": {"type": "string", "description": "Ledger path."}},
        "required": ["path"],
    }
    risk = "observe"

    def _run(self, arguments: dict[str, Any]) -> ToolResult:
        return ToolResult("300 entries, all of type 9F2")


class _WebSearch(Tool):
    """An MCP tool whose server is down.

    Named with the ``web__`` prefix the adapter applies at registration
    (ADR-042), because that prefix is what the telemetry layer reads the
    server identity out of.
    """

    name = "web__web_search"
    description = "Search the public web for pages matching a query string."
    parameters: dict[str, Any] = {
        "type": "object",
        "properties": {"query": {"type": "string", "description": "What to find."}},
        "required": ["query"],
    }
    risk = "observe"

    def _run(self, arguments: dict[str, Any]) -> ToolResult:
        # Caught here rather than raised, exactly as
        # :class:`~harness.mcp.adapter.RemoteTool` does it: a remote failure
        # arrives as a ``RuntimeError``, which the shared template method in
        # ``Tool.invoke`` deliberately does not catch, so the remote class
        # owns its own mapping (ADR-006).
        try:
            raise RuntimeError("connection closed by the server")
        except RuntimeError as exc:
            return ToolResult(f"web MCP server: {exc}", ok=False)

    def _error_type_for(self, result: ToolResult) -> str:
        """Classified where the exception was, not by reading the message."""
        return "mcp.connection"


class _WriteBrief(Tool):
    """A mutating tool, so the policy has something to refuse."""

    name = "write_brief"
    description = "Write the finished brief into the workspace for the user."
    parameters: dict[str, Any] = {
        "type": "object",
        "properties": {"body": {"type": "string", "description": "Brief text."}},
        "required": ["body"],
    }
    risk = "mutate"

    def _run(self, arguments: dict[str, Any]) -> ToolResult:
        return ToolResult("written")


class _Delegate(Tool):
    """A ``delegate`` tool that runs a real child loop.

    The real factory needs nine collaborators and a catalog; what this
    scenario is testing is the *telemetry* of delegation — that a child's
    spans nest inside the parent's tool call — so the child here is a
    genuine :class:`~harness.loop.AgentLoop` over its own context and its
    own registry, sharing the parent's telemetry hook exactly as the factory
    arranges it (ADR-093). The parts that are faked are the ones
    ``tests/integration/test_week3_delegation.py`` already covers.
    """

    name = "delegate"
    description = "Hand a self-contained subtask to a specialist sub-agent."
    parameters: dict[str, Any] = {
        "type": "object",
        "properties": {
            "role": {"type": "string", "description": "Which specialist to use."},
            "task": {"type": "string", "description": "What it should do."},
        },
        "required": ["role", "task"],
    }
    risk = "observe"

    def __init__(self, root: Path, hook: Any, child_llm: Any) -> None:
        super().__init__(root)
        self.hook = hook
        self.child_llm = child_llm
        self.child_run_id = ""

    def _run(self, arguments: dict[str, Any]) -> ToolResult:
        role = str(arguments.get("role", "researcher"))
        registry = ToolRegistry()
        registry.register(_ReadLedger(self.workspace_root))
        context = RunContext(task="")
        context.ensure_system_prompt("you are a specialist")
        context.start_turn(str(arguments.get("task", "")))
        context.role = role
        context.depth = 1
        self.child_run_id = context.run_id
        answer = AgentLoop(llm=self.child_llm, registry=registry, hook=self.hook).run(
            context
        )
        return ToolResult(f"[delegation completed] {role}: {answer}")


class _FlakyModel:
    """Replays a script, and rate-limits once before one of the calls.

    The retry is the part worth arranging: it happens inside the client,
    below the loop, so nothing above ever learns about it unless the client
    is given somewhere to report — which is exactly the gap the retry
    observer closes (ADR-094).
    """

    def __init__(self, responses: list[LLMResponse], hook: TelemetryHook) -> None:
        self.responses = list(responses)
        self.hook = hook
        self.retried = False

    def chat(self, messages: Any, tools: Any) -> LLMResponse:
        if not self.retried and len(self.responses) == 2:
            # Report two failed attempts, then succeed — which is what the
            # real client does when it retries through a 429 internally.
            self.retried = True
            self.hook.record_llm_retry(1, LLMError("429 Too Many Requests"))
            self.hook.record_llm_retry(2, LLMError("503 Service Unavailable"))
        return self.responses.pop(0)


class _MemoryHook:
    """Stands in for the memory hook: opens a real retrieval span.

    The point being exercised is that the retrieval the harness performs on
    every run — which goes through no tool call and was therefore invisible
    to the Week 3 layer — now appears in the trace (ADR-094).
    """

    def __init__(self, telemetry: TelemetryHook) -> None:
        self.telemetry = telemetry

    def on_run_start(self, context: RunContext) -> None:
        with self.telemetry.memory_span("inject") as span:
            if span is not None:
                span.set("harness.memory.items", 3)
                span.set("harness.memory.backend", "qdrant (local embedder)")

    def before_llm(self, context: RunContext) -> None: ...

    def after_llm(self, context: RunContext, response: Any) -> None: ...

    def before_tool(self, name: str, arguments: dict[str, Any]) -> None: ...

    def after_permission(self, name: str, outcome: str) -> None: ...

    def after_tool(self, name: str, result: str, outcome: Any = None) -> None: ...

    def on_run_end(self, context: RunContext) -> None:
        with self.telemetry.memory_span("append", write=True):
            pass


def _call(name: str, call_id: str, **arguments: Any) -> LLMResponse:
    return LLMResponse(
        content=None,
        tool_calls=[ToolCall(id=call_id, name=name, arguments=dict(arguments))],
        prompt_tokens=120,
        completion_tokens=20,
    )


# --- the scenario -----------------------------------------------------------


@pytest.fixture
def executed(tmp_path: Path) -> dict[str, Any]:
    """Run the whole scenario once and hand back everything it produced."""
    traces = tmp_path / "traces"
    registry_metrics = CollectorRegistry()
    recorder = PrometheusRecorder(registry_metrics)
    tracer = Tracer([JsonlSpanExporter(traces), MetricsSpanExporter(recorder)])
    telemetry = TelemetryHook(
        tracer,
        model="gemma4-31b-it",
        temperature=0.2,
        permission_mode="confirm_mutations",
    )

    child_llm = _FlakyModel(
        [
            _call("read_ledger", "child-1", path="ledger.txt"),
            text("300 entries, all type 9F2", prompt_tokens=90, completion_tokens=18),
        ],
        telemetry,
    )
    delegate = _Delegate(tmp_path, telemetry, child_llm)

    tools = ToolRegistry()
    tools.register(_ReadLedger(tmp_path))
    tools.register(_WebSearch(tmp_path))
    tools.register(_WriteBrief(tmp_path))
    tools.register(delegate)

    parent_llm = _FlakyModel(
        [
            _call("read_ledger", "p-1", path="ledger.txt"),
            _call("web__web_search", "p-2", query="type 9F2 meaning"),
            _call("write_brief", "p-3", body="300 entries, all type 9F2."),
            _call("delegate", "p-4", role="researcher", task="Recount the ledger."),
            text("Done: 300 entries.", prompt_tokens=200, completion_tokens=40),
        ],
        telemetry,
    )

    context = RunContext(task="")
    context.ensure_system_prompt("you are the coordinator")
    context.start_turn("brief the ledger")
    AgentLoop(
        llm=parent_llm,
        registry=tools,
        # Telemetry first so the run span encloses everything, memory after
        # it, exactly as the CLI composes them (ADR-093).
        hook=CompositeHook(telemetry, _MemoryHook(telemetry)),
        # `deny` rather than a confirmer, so the refusal is the policy's and
        # the test needs no terminal.
        policy=RiskBasedPolicy("deny"),
    ).run(context)
    tracer.shutdown()

    path = find_trace(traces, "last")
    return {
        "traces": traces,
        "path": path,
        "records": [json.loads(line) for line in path.read_text().splitlines()],
        "run": load_trace(path),
        "context": context,
        "metrics": registry_metrics,
        "child_run_id": delegate.child_run_id,
    }


def _value(registry: CollectorRegistry, name: str, **labels: str) -> float:
    return registry.get_sample_value(name, labels or None) or 0.0


# --- the complete execution path --------------------------------------------


def test_one_file_holds_the_whole_run_including_its_child(
    executed: dict[str, Any],
) -> None:
    """ "Inspect one agent run" has to include the runs it delegated.

    One file, one trace id, named for the parent. A child writing its own
    file would put the interesting half of a delegated task somewhere nobody
    is looking (ADR-083).
    """
    records = executed["records"]
    assert executed["path"].stem == executed["context"].run_id
    assert len({record["trace_id"] for record in records}) == 1
    assert {record["run_id"] for record in records} == {
        executed["context"].run_id,
        executed["child_run_id"],
    }


def test_the_subagent_is_nested_inside_the_delegation_that_caused_it(
    executed: dict[str, Any],
) -> None:
    """The structural claim of sub-agent observability.

    The child's run span hangs off the parent's ``tool.call`` for
    ``delegate``, and its own model and tool calls hang off that. So a
    reader holding the parent's trace can descend into the child's work
    rather than being told it happened elsewhere (ADR-084, ADR-093).
    """
    by_span = {record["span_id"]: record for record in executed["records"]}
    (child,) = [r for r in executed["records"] if r["span"] == "subagent.run"]

    delegation = by_span[child["parent_span_id"]]
    assert delegation["span"] == "tool.call"
    assert delegation["tool"] == "delegate"
    assert delegation["run_id"] == executed["context"].run_id

    # And the child's own work is inside the child.
    child_ids = {
        record["span_id"]
        for record in executed["records"]
        if record["run_id"] == child["run_id"]
    }
    child_work = [
        record
        for record in executed["records"]
        if record["parent_span_id"] in child_ids
    ]
    assert {record["span"] for record in child_work} >= {"agent.iteration"}
    assert child["role"] == "researcher"


def test_the_two_failures_are_attributed_to_different_layers(
    executed: dict[str, Any],
) -> None:
    """The question §9 exists for, answered from the file alone.

    One failure is a dead MCP server and the other is the permission policy.
    Under a single ``ok`` bit these were the same evidence; now they are two
    error types blaming two layers, and the MCP one names the server
    (ADR-094).
    """
    failures = {
        record["tool"]: record
        for record in executed["records"]
        if record["span"] == "tool.call" and record["ok"] is False
    }
    assert set(failures) == {"web__web_search", "write_brief"}

    assert failures["web__web_search"]["error_type"] == "mcp.connection"
    assert failures["web__web_search"]["error_layer"] == "mcp"
    assert failures["web__web_search"]["attributes"]["harness.mcp.server"] == "web"

    assert failures["write_brief"]["error_type"] == "permission.denied"
    assert failures["write_brief"]["error_layer"] == "policy"


def test_the_refusal_is_a_decision_and_a_refused_call(
    executed: dict[str, Any],
) -> None:
    """Both halves, and the decision is inside the call it governed."""
    by_span = {record["span_id"]: record for record in executed["records"]}
    (denial,) = [
        record
        for record in executed["records"]
        if record["span"] == "permission.check" and record["permission"] == "deny"
    ]

    assert denial["ok"] is True  # the policy worked
    refused = by_span[denial["parent_span_id"]]
    assert refused["tool"] == "write_brief"
    assert refused["ok"] is False


def test_no_argument_value_reaches_the_trace(executed: dict[str, Any]) -> None:
    """Safe by default, checked against the file the agent actually wrote.

    The brief secret is a value the model passed to a tool. The trace knows
    a 26-character string was passed and not what it said (ADR-094).
    """
    body = "300 entries, all type 9F2."
    raw = executed["path"].read_text(encoding="utf-8")

    assert body not in raw
    assert "ledger.txt" not in raw
    assert "type 9F2 meaning" not in raw
    # The shape survives, which is what makes the redaction useful rather
    # than merely safe.
    written = next(
        record
        for record in executed["records"]
        if record["span"] == "tool.call" and record["tool"] == "write_brief"
    )
    assert written["attributes"]["harness.tool.args"] == f"body=str[{len(body)}]"
