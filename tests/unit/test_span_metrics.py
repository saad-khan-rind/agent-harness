"""Prometheus: the frozen names, the label discipline, and the translation.

Three things are being defended here, in order of how expensive they are to
get wrong:

1. **The metric names.** The Grafana dashboard queries them as strings, so a
   rename is a silent break in a panel somebody else owns. They are pinned
   (ADR-081), and the span rewrite was required to preserve every one of
   them — which is the proof that it was a behaviour-preserving change to
   the instrumentation and not a new metrics scheme wearing the old names.
2. **Cardinality.** Prometheus keeps one time series per label combination.
   A ``run_id`` label, an exception message, a file path — any one of them
   makes the database unusable inside a day, and the failure is gradual
   enough that nobody notices until it is expensive.
3. **The translation itself**, from a finished span to counters.

Driven through the real loop where the assertion is about a real run, with
its own ``CollectorRegistry`` so two tests in one process cannot see each
other's numbers.
"""

from pathlib import Path
from typing import Any

import pytest
from harness.context import RunContext
from harness.llm.protocol import LLMError, LLMResponse
from harness.loop import AgentLoop
from harness.observability.api import (
    SPAN_SUBAGENT_RUN,
    Attr,
    Span,
    Tracer,
)
from harness.observability.exporters.metrics import MetricsSpanExporter
from harness.observability.recorder import PrometheusRecorder
from harness.observability.telemetry import TelemetryHook
from harness.permissions import RiskBasedPolicy
from harness.tools.base import Tool, ToolResult
from harness.tools.registry import ToolRegistry
from prometheus_client import CollectorRegistry
from tests.fakes import ScriptedLLM, call, text

# Every metric this harness publishes. The first fourteen are frozen from
# Week 3 and the dashboard queries them by name; the rest arrived with the
# span rewrite. A new metric must be added here deliberately, which is the
# point: an exporter that can quietly grow a metric is an exporter that will
# quietly grow twenty (ADR-081).
_FROZEN_WEEK3 = (
    "harness_active_runs",
    "harness_runs_total",
    "harness_run_duration_seconds",
    "harness_llm_requests_total",
    "harness_llm_tokens_total",
    "harness_llm_latency_seconds",
    "harness_tool_calls_total",
    "harness_tool_latency_seconds",
    "harness_permission_decisions_total",
    "harness_memory_ops_total",
    "harness_context_chars",
    "harness_context_compactions_total",
    "harness_subagent_runs_total",
    "harness_subagent_duration_seconds",
)

_ADDED_WITH_SPANS = (
    "harness_llm_errors_total",
    "harness_llm_retries_total",
    "harness_errors_total",
    "harness_mcp_calls_total",
    "harness_memory_operation_seconds",
    "harness_memory_items_returned",
    "harness_run_iterations",
    "harness_run_tool_calls",
    "harness_repeated_tool_calls_total",
    "harness_task_outcomes_total",
    "harness_telemetry_spans_dropped",
)

_ALL_METRICS = _FROZEN_WEEK3 + _ADDED_WITH_SPANS

# Label names that must never appear on any metric. Each is unbounded in
# practice: one series per run, per path, per message, per prompt — which is
# how a metrics database is destroyed.
_FORBIDDEN_LABELS = frozenset(
    {
        "run_id",
        "trace_id",
        "span_id",
        "parent_run_id",
        "path",
        "task",
        "prompt",
        "query",
        "message",
        "args",
        "arguments",
        "fingerprint",
        "content",
        "user",
    }
)


class _EchoTool(Tool):
    """Succeeds, or fails on demand."""

    name = "echo"
    description = "Echo the text back, or fail when asked to."
    parameters: dict[str, Any] = {
        "type": "object",
        "properties": {"text": {"type": "string", "description": "What to echo."}},
        "required": ["text"],
    }
    risk = "observe"

    def __init__(self, root: Path, ok: bool = True) -> None:
        super().__init__(root)
        self.ok = ok

    def _run(self, arguments: dict[str, Any]) -> ToolResult:
        if not self.ok:
            return ToolResult("nope", ok=False)
        return ToolResult(str(arguments.get("text", "")))


class _WriteTool(Tool):
    """A mutating tool, so the policy has something to gate."""

    name = "write_note"
    description = "Write a note into the workspace for later reading."
    parameters: dict[str, Any] = {
        "type": "object",
        "properties": {"body": {"type": "string", "description": "Note body."}},
        "required": ["body"],
    }
    risk = "mutate"

    def _run(self, arguments: dict[str, Any]) -> ToolResult:
        return ToolResult("written")


@pytest.fixture
def registry() -> CollectorRegistry:
    """A registry this test owns, so no other test can see its numbers."""
    return CollectorRegistry()


@pytest.fixture
def recorder(registry: CollectorRegistry) -> PrometheusRecorder:
    """The real recorder on that registry."""
    return PrometheusRecorder(registry)


def _tools(root: Path, ok: bool = True) -> ToolRegistry:
    tools = ToolRegistry()
    tools.register(_EchoTool(root, ok))
    tools.register(_WriteTool(root))
    return tools


def _drive(
    tmp_path: Path,
    recorder: PrometheusRecorder,
    responses: list[LLMResponse],
    *,
    ok: bool = True,
    policy: Any = None,
    llm: Any = None,
    **hook_kwargs: Any,
) -> RunContext:
    """Run a real loop with the metrics exporter attached."""
    hook = TelemetryHook(Tracer([MetricsSpanExporter(recorder)]), **hook_kwargs)
    context = RunContext(task="")
    context.ensure_system_prompt("system")
    context.start_turn("do the thing")
    AgentLoop(
        llm=llm if llm is not None else ScriptedLLM(responses),
        registry=_tools(tmp_path, ok),
        hook=hook,
        policy=policy,
    ).run(context)
    return context


def _value(registry: CollectorRegistry, name: str, **labels: str) -> float:
    """Read one sample, defaulting to zero when the series does not exist."""
    return registry.get_sample_value(name, labels or None) or 0.0


def _queryable_names(registry: CollectorRegistry) -> set[str]:
    """Return the metric names a PromQL query would actually use.

    ``prometheus_client`` reports a counter's *family* name without the
    ``_total`` suffix it exposes — ``harness_runs``, not
    ``harness_runs_total`` — so asserting on ``metric.name`` would pin
    strings that appear nowhere in the dashboard and would happily pass
    while every panel broke.

    What the dashboard queries is the exposed name, so that is what is
    reconstructed here: counters regain their suffix, gauges and histograms
    are already named as they are queried.
    """
    names: set[str] = set()
    for metric in registry.collect():
        if not metric.name.startswith("harness"):
            continue
        names.add(f"{metric.name}_total" if metric.type == "counter" else metric.name)
    return names


# --- the contract -----------------------------------------------------------


def test_every_metric_name_is_pinned(registry: CollectorRegistry) -> None:
    """The dashboard queries these as strings. Renaming one breaks a panel.

    Both directions are checked: nothing registered is missing from the
    list, and nothing in the list is missing from the registry. A one-way
    assertion would let a metric be deleted without anybody noticing.
    """
    PrometheusRecorder(registry)
    assert set(_ALL_METRICS) == _queryable_names(registry)


def test_no_metric_carries_an_unbounded_label(registry: CollectorRegistry) -> None:
    """Cardinality is the one mistake that cannot be undone cheaply.

    Prometheus keeps one time series per label combination, so a per-run
    label makes the database useless within a day — and the damage is
    already in the storage by the time anybody notices. Per-run detail
    belongs in the trace, which is a different tool for a different
    question (ADR-081).
    """
    PrometheusRecorder(registry)
    for metric in registry.collect():
        if not metric.name.startswith("harness"):
            continue
        for sample in metric.samples:
            offenders = _FORBIDDEN_LABELS & set(sample.labels)
            assert not offenders, f"{metric.name} carries {offenders}"


# --- a real run -------------------------------------------------------------


def test_a_real_run_moves_the_numbers(
    tmp_path: Path, recorder: PrometheusRecorder, registry: CollectorRegistry
) -> None:
    """The end-to-end translation, through the loop rather than around it."""
    _drive(
        tmp_path,
        recorder,
        [
            call("echo", text="hi", prompt_tokens=100, completion_tokens=10),
            text("done", prompt_tokens=150, completion_tokens=20),
        ],
    )

    assert _value(registry, "harness_runs_total", status="success", role="main") == 1.0
    assert _value(registry, "harness_llm_requests_total", role="main") == 2.0
    assert _value(registry, "harness_llm_tokens_total", direction="prompt") == 250.0
    assert _value(registry, "harness_llm_tokens_total", direction="completion") == 30.0
    assert (
        _value(
            registry, "harness_tool_calls_total", tool="echo", ok="true", kind="builtin"
        )
        == 1.0
    )
    assert (
        _value(registry, "harness_permission_decisions_total", outcome="allow") == 1.0
    )
    # The shape histograms: how much work one run took.
    assert _value(registry, "harness_run_iterations_count", role="main") == 1.0
    assert _value(registry, "harness_run_tool_calls_sum", role="main") == 1.0


def test_a_failed_llm_call_counts_as_a_failure_not_as_a_request(
    tmp_path: Path, recorder: PrometheusRecorder, registry: CollectorRegistry
) -> None:
    """``harness_llm_requests_total`` has meant "returned" since Week 3.

    Quietly widening it to include failures would move a line on somebody's
    chart without anything in the diff saying so. A failure gets its own
    counter, with the reason as a bounded label (ADR-094).
    """

    class _RateLimited:
        def chat(self, messages: Any, tools: Any) -> LLMResponse:
            raise LLMError("InnKube request failed after 4 attempts: 429")

    with pytest.raises(LLMError):
        _drive(tmp_path, recorder, [], llm=_RateLimited())

    assert _value(registry, "harness_llm_requests_total", role="main") == 0.0
    assert (
        _value(
            registry,
            "harness_llm_errors_total",
            error_type="llm.rate_limited",
            role="main",
        )
        == 1.0
    )


def test_failures_are_attributed_to_the_layer_responsible(
    tmp_path: Path, recorder: PrometheusRecorder, registry: CollectorRegistry
) -> None:
    """The metric that answers the question a person actually starts from.

    Not "did something fail" but "is this my agent, my tools, my policy or
    the model". One lookup, one panel, instead of a wall of error strings
    (ADR-094).
    """
    _drive(tmp_path, recorder, [call("echo", text="hi"), text("done")], ok=False)

    assert (
        _value(
            registry,
            "harness_errors_total",
            layer="tool",
            error_type="tool.failed",
            component="tool",
        )
        == 1.0
    )


def test_a_guard_is_not_counted_as_an_error(
    tmp_path: Path, recorder: PrometheusRecorder, registry: CollectorRegistry
) -> None:
    """``capped`` is its own status, because a guard working is not a fault.

    A dashboard that filed the iteration cap under errors would make the one
    number anybody looks at first — the success rate — wrong in the
    direction that causes a false alarm (ADR-047).
    """
    hook = TelemetryHook(Tracer([MetricsSpanExporter(recorder)]), max_iterations=2)
    context = RunContext(task="")
    context.ensure_system_prompt("system")
    context.start_turn("do the thing")
    AgentLoop(
        llm=ScriptedLLM([call("echo", text="a")] * 4),
        registry=_tools(tmp_path),
        hook=hook,
        max_iterations=2,
    ).run(context)

    assert _value(registry, "harness_runs_total", status="capped", role="main") == 1.0
    assert _value(registry, "harness_runs_total", status="error", role="main") == 0.0


def test_permission_outcomes_are_counted_apart(
    tmp_path: Path, recorder: PrometheusRecorder, registry: CollectorRegistry
) -> None:
    """Four values for three policy outcomes, because asking is a fact.

    ``require_user_confirmation`` splits by what the person said: "we asked
    and nobody answered" and "we asked and they approved" are different
    events, and the permission panel needs both (ADR-081).
    """
    _drive(
        tmp_path,
        recorder,
        [call("write_note", body="x"), text("done")],
        policy=RiskBasedPolicy("deny"),
    )

    assert _value(registry, "harness_permission_decisions_total", outcome="deny") == 1.0
    assert (
        _value(
            registry,
            "harness_tool_calls_total",
            tool="write_note",
            ok="false",
            kind="builtin",
        )
        == 1.0
    )


# --- spans the loop does not produce ----------------------------------------


def _span(name: str, **attributes: Any) -> Span:
    """Build and close one span, for the kinds the loop does not produce.

    A helper rather than eight near-identical constructor calls: the ids are
    noise in every one of these tests, and what each is actually about is
    the attributes.
    """
    span = Span(
        trace_id="a" * 32,
        span_id="b" * 16,
        parent_span_id=None,
        name=name,
    )
    span.update(attributes)
    span.end_ns = span.start_ns + 12_000_000
    return span


def _finish(span: Span) -> Span:
    """Close a hand-built span so an exporter can read it."""
    span.end_ns = span.start_ns + 12_000_000
    if span.status == "unset":
        span.status = "ok"
    return span


def test_a_subagent_run_is_counted_as_a_run_and_as_a_delegation(
    recorder: PrometheusRecorder, registry: CollectorRegistry
) -> None:
    """Both, because both panels are real.

    Keyed off the span *name* rather than off the role, so a specialist role
    that ever runs at the top level is not miscounted as a delegation.
    """
    exporter = MetricsSpanExporter(recorder)
    span = _span(
        SPAN_SUBAGENT_RUN,
        **{Attr.ROLE: "researcher", Attr.EXIT_REASON: "success"},
    )
    span.parent_span_id = "c" * 16
    exporter.export(_finish(span))

    assert (
        _value(registry, "harness_runs_total", status="success", role="researcher")
        == 1.0
    )
    assert (
        _value(
            registry, "harness_subagent_runs_total", role="researcher", status="success"
        )
        == 1.0
    )


# --- safety -----------------------------------------------------------------
