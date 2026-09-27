"""Observability: one span model, several exporters, and the seam they use.

Week 3 built this package as *metrics plus a trace file*. It is now built as
a **span model with exporters**, and the difference is worth stating because
it is the whole architecture:

    lifecycle hooks  ->  TelemetryHook  ->  Tracer  ->  [ exporters ]
                                                         ├── JSONL   (one file per run)
                                                         ├── Metrics (Prometheus)
                                                         └── OTLP    (optional, off)

Previously two observers each timed the same run, each kept their own
stamps, and each had to be edited to add a field. Now one hook opens spans
and every exporter reads the same finished span, so a new telemetry field is
one attribute in one place (ADR-093).

The pieces, in dependency order:

* :mod:`~harness.observability.api` — ``Span``, ``Tracer``, the ``Attr``
  vocabulary. **Standard library only**, which is what lets
  ``harness/tools/`` and ``harness/memory/`` open a span without acquiring a
  metrics library (ADR-034).
* :mod:`~harness.observability.errors` — the bounded ``error.type``
  vocabulary and the layer each one blames (ADR-094).
* :mod:`~harness.observability.redaction` — what is safe to record about a
  tool call, and the argument fingerprint that makes thrash visible without
  disclosing anything.
* :mod:`~harness.observability.telemetry` — ``TelemetryHook``: the
  :class:`~harness.hooks.Hook` implementation that turns lifecycle callbacks
  into spans.
* :mod:`~harness.observability.exporters` — where finished spans go.
* :mod:`~harness.observability.recorder` — the metric definitions and the
  one import of ``prometheus_client`` in the harness.
* :mod:`~harness.observability.server` — the ``/metrics`` endpoint, on a
  daemon thread, whose failure is a banner line rather than a crash.
* :mod:`~harness.observability.evals` — the seam that attaches a *task
  quality* verdict to a run, kept apart from system and agent success
  (ADR-096).
* :mod:`~harness.observability.replay` — reads a trace file back as a span
  tree: ``/trace`` in the REPL and ``python -m
  harness.observability.replay`` on the command line.

**Why the imports below are split in two.** Everything above the line is
dependency-free and safe for any module to import. Everything below it
either imports ``prometheus_client`` or imports back into ``harness``, and
eagerly importing those here would create two problems at once: a cycle
(``harness.tools.base`` needs the error vocabulary, and the telemetry hook
needs ``harness.tools.base``), and a rule that anything touching an error
type must have a metrics library installed. So the heavy names are resolved
on first access through :pep:`562`'s module ``__getattr__``. The public API
is unchanged — ``from harness.observability import PrometheusRecorder``
works exactly as before — and the import graph stays honest.
"""

from typing import Any

# --- dependency-free core (safe for anything in `harness/` to import) -------
from harness.observability.api import (
    SPAN_AGENT_RUN,
    SPAN_FINALIZE,
    SPAN_ITERATION,
    SPAN_LLM_CALL,
    SPAN_MEMORY_READ,
    SPAN_MEMORY_WRITE,
    SPAN_PERMISSION,
    SPAN_SUBAGENT_RUN,
    SPAN_TOOL_CALL,
    Attr,
    Span,
    SpanExporter,
    SpanKind,
    SpanStatus,
    Tracer,
    as_trace_id,
    new_span_id,
    new_trace_id,
)
from harness.observability.errors import (
    ERROR_TYPES,
    LAYERS,
    RESPONSIBLE_LAYER,
    classify_exception,
    classify_tool_failure,
    layer_for,
    safe_message,
)
from harness.observability.redaction import (
    RedactionMode,
    argument_digest,
    argument_fingerprint,
    describe_mode,
    is_secret_key,
)

# --- lazily resolved (imports prometheus_client, or imports back into
# --- harness; see the module docstring) -------------------------------------
_LAZY: dict[str, str] = {
    "JsonlSpanExporter": "harness.observability.exporters.jsonl",
    "MetricsSpanExporter": "harness.observability.exporters.metrics",
    "OtlpSpanExporter": "harness.observability.exporters.otlp",
    "build_otlp_exporter": "harness.observability.exporters.otlp",
    "MetricsRecorder": "harness.observability.recorder",
    "NullRecorder": "harness.observability.recorder",
    "PermissionOutcomeLabel": "harness.observability.recorder",
    "PrometheusRecorder": "harness.observability.recorder",
    "RunStatus": "harness.observability.recorder",
    "ToolKind": "harness.observability.recorder",
    "serve_metrics": "harness.observability.server",
    "stop_metrics": "harness.observability.server",
    "TelemetryHook": "harness.observability.telemetry",
    "classify_tool": "harness.observability.telemetry",
    "mcp_server_of": "harness.observability.telemetry",
    "RunQuality": "harness.observability.evals",
    "QualityStore": "harness.observability.evals",
    "derive_signals": "harness.observability.evals",
    "RunSummary": "harness.observability.replay",
    "SpanNode": "harness.observability.replay",
    "TraceNotFoundError": "harness.observability.replay",
    "find_trace": "harness.observability.replay",
    "list_traces": "harness.observability.replay",
    "load_trace": "harness.observability.replay",
    "render_list": "harness.observability.replay",
    "render_run": "harness.observability.replay",
    "summarise": "harness.observability.replay",
}


def __getattr__(name: str) -> Any:
    """Resolve a heavy public name on first access.

    :pep:`562`. Called only for names this module does not already define,
    so the dependency-free imports above are unaffected and cost nothing
    extra.

    Args:
        name: The attribute being read.

    Returns:
        The resolved object, which is also cached in this module's globals
        so the lookup happens once.

    Raises:
        AttributeError: For a name that is not part of the public API —
            with the same message Python would have produced, so a typo
            still reads like a typo.
    """
    module_path = _LAZY.get(name)
    if module_path is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    from importlib import import_module

    value = getattr(import_module(module_path), name)
    globals()[name] = value
    return value


def __dir__() -> list[str]:
    """Include the lazy names in ``dir()`` and in tab completion."""
    return sorted([*globals(), *_LAZY])


__all__ = [
    "ERROR_TYPES",
    "LAYERS",
    "RESPONSIBLE_LAYER",
    "SPAN_AGENT_RUN",
    "SPAN_FINALIZE",
    "SPAN_ITERATION",
    "SPAN_LLM_CALL",
    "SPAN_MEMORY_READ",
    "SPAN_MEMORY_WRITE",
    "SPAN_PERMISSION",
    "SPAN_SUBAGENT_RUN",
    "SPAN_TOOL_CALL",
    "Attr",
    "JsonlSpanExporter",
    "MetricsRecorder",
    "MetricsSpanExporter",
    "NullRecorder",
    "OtlpSpanExporter",
    "PermissionOutcomeLabel",
    "PrometheusRecorder",
    "QualityStore",
    "RedactionMode",
    "RunQuality",
    "RunStatus",
    "RunSummary",
    "Span",
    "SpanExporter",
    "SpanKind",
    "SpanNode",
    "SpanStatus",
    "TelemetryHook",
    "ToolKind",
    "TraceNotFoundError",
    "Tracer",
    "argument_digest",
    "argument_fingerprint",
    "as_trace_id",
    "build_otlp_exporter",
    "classify_exception",
    "classify_tool",
    "classify_tool_failure",
    "derive_signals",
    "describe_mode",
    "find_trace",
    "is_secret_key",
    "layer_for",
    "list_traces",
    "load_trace",
    "mcp_server_of",
    "new_span_id",
    "new_trace_id",
    "render_list",
    "render_run",
    "safe_message",
    "serve_metrics",
    "stop_metrics",
    "summarise",
]
