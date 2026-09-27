"""Spans in, Prometheus counters out.

Week 3 had a hook that computed durations and called a recorder. This is the
same recorder — the fourteen metric names are still frozen and the Grafana
dashboard still queries them by the same strings — reached from a different
direction: a span already knows its duration, its role, its outcome and its
error type, so translating one into metrics is a lookup rather than a second
set of stopwatches (ADR-093).

What that buys, concretely: the day somebody adds ``harness.mcp.server`` to
a tool span, this file can publish a per-server counter without the
instrumentation side being touched at all. Under the old shape the same
change meant editing the metrics hook, the trace hook, and both sets of
tests, and hoping they still agreed.

**Cardinality is still the constraint.** ``run_id`` is not a label and never
will be: Prometheus keeps one time series per label combination, and a
per-run label makes the database useless within a day. Every label this
exporter emits comes from a bounded vocabulary — the role catalogue, the
five tool kinds, the configured MCP server names, the error types in
:mod:`harness.observability.errors` — and :meth:`_label` is the one place
that enforces it, so an attribute somebody adds later cannot quietly become
an unbounded series (ADR-081, ADR-094).

**This is the only exporter that hears a span open.** ``harness_active_runs``
is a gauge, so it has to move when a run starts rather than when it ends.
"""

from harness.observability.api import (
    SPAN_AGENT_RUN,
    SPAN_ITERATION,
    SPAN_LLM_CALL,
    SPAN_MEMORY_READ,
    SPAN_MEMORY_WRITE,
    SPAN_PERMISSION,
    SPAN_SUBAGENT_RUN,
    SPAN_TOOL_CALL,
    Attr,
    Span,
)
from harness.observability.errors import layer_for
from harness.observability.recorder import (
    MetricsRecorder,
    PermissionOutcomeLabel,
    RunStatus,
    ToolKind,
)

# The two span names that mean "a run finished".
_RUN_SPANS = frozenset({SPAN_AGENT_RUN, SPAN_SUBAGENT_RUN})
_MEMORY_SPANS = frozenset({SPAN_MEMORY_READ, SPAN_MEMORY_WRITE})

_MAIN_ROLE = "main"

# Label values accepted as-is. Anything else is folded to the fallback, so a
# new value invented upstream shows as an obviously-wrong bucket rather than
# as a new time series nobody planned for.
_RUN_STATUSES = frozenset({"success", "error", "no_progress", "capped"})
_TOOL_KINDS = frozenset({"builtin", "mcp", "memory", "skill", "delegate"})
_PERMISSION_OUTCOMES = frozenset({"allow", "deny", "confirmed", "declined"})

# Ceiling on any label value's length, as a last defence against a label
# derived from something the model chose. Nothing should reach it; a label
# that does is a bug this truncation makes survivable rather than fatal.
_MAX_LABEL_CHARS = 64


class MetricsSpanExporter:
    """Translate finished spans into the recorder's vocabulary.

    Args:
        recorder: Where to report. Pass
            :class:`~harness.observability.recorder.NullRecorder` and this
            exporter measures nothing while still being installed — which is
            what tests use to prove the translation itself never raises.
        max_iterations: The loop's cap, or ``0``. Unused for classification
            (the run span carries ``harness.exit_reason``) and kept only so
            the constructor matches the hook it replaced, easing the
            transition for anything that built one.
    """

    def __init__(self, recorder: MetricsRecorder, max_iterations: int = 0) -> None:
        self._recorder = recorder
        self._max_iterations = max_iterations

    # -- the exporter protocol --------------------------------------------

    def on_start(self, span: Span) -> None:
        """Mark a run active the moment it begins.

        The only thing that cannot wait for a span to close. Everything else
        this exporter publishes is a property of a finished operation.
        """
        if span.name in _RUN_SPANS:
            self._recorder.run_started(self._role(span))

    def export(self, span: Span) -> None:
        """Publish one finished span as metrics."""
        if span.name in _RUN_SPANS:
            self._export_run(span)
        elif span.name == SPAN_LLM_CALL:
            self._export_llm(span)
        elif span.name == SPAN_TOOL_CALL:
            self._export_tool(span)
        elif span.name == SPAN_PERMISSION:
            self._export_permission(span)
        elif span.name in _MEMORY_SPANS:
            self._export_memory(span)
        elif span.name == SPAN_ITERATION:
            self._export_iteration(span)

        # Failure attribution, published for *every* span rather than per
        # kind. A failure is worth counting wherever it happened, and
        # keeping this in one place means a span type added later is
        # attributed without anybody remembering to wire it up (ADR-094).
        if span.status == "error" and span.error_type:
            self._recorder.error_observed(
                layer=layer_for(span.error_type),
                error_type=self._label(span.error_type),
                component=self._label(
                    str(span.attributes.get(Attr.COMPONENT) or "unknown")
                ),
            )

    def shutdown(self) -> None:
        """Nothing is held open; the registry outlives this object."""

    # -- per span kind -----------------------------------------------------

    def _export_run(self, span: Span) -> None:
        """Publish a finished agent or sub-agent run."""
        role = self._role(span)
        status = self._status(span)
        seconds = span.duration_seconds

        self._recorder.run_finished(role, status, seconds)
        self._recorder.run_shape(
            role,
            iterations=_count(span, "iterations"),
            tool_calls=_count(span, "tool_calls"),
        )
        # A delegated child is also a run: the same exporter sees both, and
        # the sub-agent series is what the dashboard's delegation panel
        # draws. Keyed off the span *name* rather than off the role, so a
        # specialist role that ever runs at the top level is not miscounted.
        if span.name == SPAN_SUBAGENT_RUN:
            self._recorder.subagent_finished(role, status, seconds)

    def _export_llm(self, span: Span) -> None:
        """Publish one model request, successful or not."""
        role = self._role(span)
        if span.status == "error":
            # Counted as a failure and *not* as a request, because
            # `harness_llm_requests_total` has meant "requests that returned"
            # since Week 3 and the dashboard's rate panel is built on that.
            # Quietly widening it would move a line on somebody's chart
            # without anything in the diff saying so.
            self._recorder.llm_failed(role, self._label(span.error_type or "unknown"))
        else:
            self._recorder.llm_call(
                role,
                span.duration_seconds,
                prompt=_int(span, Attr.GEN_AI_USAGE_INPUT_TOKENS),
                completion=_int(span, Attr.GEN_AI_USAGE_OUTPUT_TOKENS),
            )

        for _, name, fields in span.events:
            if name == "retry":
                self._recorder.llm_retried(
                    self._label(str(fields.get("error_type") or "unknown"))
                )

    def _export_tool(self, span: Span) -> None:
        """Publish one tool call, plus its MCP server when it had one."""
        name = str(span.attributes.get(Attr.TOOL_NAME) or "unknown")
        kind = self._tool_kind(span)
        ok = span.status != "error"

        # The tool *name* is a label, and it is the one place this exporter
        # accepts a value it does not choose. It is bounded in practice — a
        # registry is built at startup and cannot grow during a run — but
        # the truncation in `_label` is what keeps a malformed remote tool
        # name from becoming a pathological series.
        self._recorder.tool_finished(self._label(name), kind, ok, span.duration_seconds)

        server = span.attributes.get(Attr.MCP_SERVER)
        if server:
            self._recorder.mcp_call(self._label(str(server)), ok)

        if kind == "memory":
            # Kept for continuity: `harness_memory_ops_total` has counted
            # memory *tools* since Week 3 and the dashboard queries it. The
            # richer per-operation timing comes from `memory.*` spans, which
            # also cover the retrieval no tool call is involved in.
            self._recorder.memory_operation(self._label(name[len("memory_") :]))

        if _int(span, Attr.TOOL_FAILURE_STREAK) > 1:
            self._recorder.repeated_call(self._label(name))

    def _export_permission(self, span: Span) -> None:
        """Publish one permission decision."""
        decision = str(span.attributes.get(Attr.PERMISSION_DECISION) or "")
        self._recorder.permission_decided(self._permission(decision))

    def _export_memory(self, span: Span) -> None:
        """Publish one long-term memory operation, with its latency."""
        operation = self._label(
            str(span.attributes.get(Attr.MEMORY_OPERATION) or "unknown")
        )
        items = span.attributes.get(Attr.MEMORY_ITEMS)
        self._recorder.memory_timed(
            operation,
            span.duration_seconds,
            int(items) if isinstance(items, int) else None,
        )
        # Also counted on the Week 3 counter, so a panel built against it
        # keeps working and now finally sees the harness-driven retrieval
        # that never went through a tool (ADR-094).
        self._recorder.memory_operation(operation)

    def _export_iteration(self, span: Span) -> None:
        """Publish the context window measured for one reasoning step."""
        chars = span.attributes.get(Attr.CONTEXT_CHARS)
        if isinstance(chars, int):
            self._recorder.context_measured(
                self._role(span),
                chars,
                _int(span, Attr.CONTEXT_COMPACTIONS),
            )

    # -- label discipline --------------------------------------------------

    @staticmethod
    def _label(value: str) -> str:
        """Clip one label value, as a last defence against a long one."""
        text = value.strip() or "unknown"
        return text[:_MAX_LABEL_CHARS]

    def _role(self, span: Span) -> str:
        """Return the role label for a span, defaulting to ``main``."""
        return self._label(str(span.attributes.get(Attr.ROLE) or _MAIN_ROLE))

    def _status(self, span: Span) -> RunStatus:
        """Return the run status recorded on a run span.

        Read from ``harness.exit_reason`` rather than from the span's own
        ``status``, because the two answer different questions and the
        difference is the point of ADR-047: a capped run has ``status: ok``
        (no guard is a fault) and ``exit_reason: capped`` (it did not
        answer). A dashboard that took the span status would report a cut-off
        run as a success.
        """
        reason = str(span.attributes.get(Attr.EXIT_REASON) or "")
        if reason in _RUN_STATUSES:
            return reason  # type: ignore[return-value]
        return "error" if span.status == "error" else "success"

    def _tool_kind(self, span: Span) -> ToolKind:
        """Return the tool kind label, folding anything unexpected."""
        kind = str(span.attributes.get(Attr.TOOL_KIND) or "")
        return kind if kind in _TOOL_KINDS else "builtin"  # type: ignore[return-value]

    @staticmethod
    def _permission(decision: str) -> PermissionOutcomeLabel:
        """Narrow a decision to a known label.

        An unrecognised value becomes ``deny``: a new outcome nobody told
        the dashboard about should look restrictive rather than permissive,
        and a label this module invented would be a series nobody can query.
        """
        if decision in _PERMISSION_OUTCOMES:
            return decision  # type: ignore[return-value]
        return "deny"


def _int(span: Span, key: str) -> int:
    """Read an integer attribute, defaulting to zero."""
    value = span.attributes.get(key)
    return value if isinstance(value, int) and not isinstance(value, bool) else 0


def _count(span: Span, name: str) -> int:
    """Read one of the run-summary counters the telemetry hook attaches."""
    value = span.attributes.get(f"harness.count.{name}")
    return value if isinstance(value, int) and not isinstance(value, bool) else 0
