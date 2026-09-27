"""Optional OTLP export: the translation, and the three ways it does nothing.

The value of this exporter is not that it works — an SDK does the hard part
— but that it is *optional in three independent ways*, and each of those is
a claim that has to be checked rather than asserted in a docstring
(ADR-095):

1. ``otlp_enabled: false`` means the CLI never builds one.
2. The OpenTelemetry packages being absent means :func:`build_otlp_exporter`
   returns ``None`` and a reason for the banner.
3. The collector being unreachable means the SDK retries on its own thread
   and drops; the agent never waits.

Only the translation itself is skipped when the packages are missing.
Everything else here runs either way, because "it degrades" is the property
worth defending on a machine that does not have them.
"""

import builtins
from typing import Any

import pytest
from harness.observability.api import (
    SPAN_AGENT_RUN,
    SPAN_LLM_CALL,
    Attr,
    Span,
    Tracer,
)
from harness.observability.exporters.otlp import (
    OtlpSpanExporter,
    build_otlp_exporter,
)

otel = pytest.importorskip(
    "opentelemetry.sdk.trace",
    reason="OpenTelemetry is an optional dependency; the degraded path is "
    "tested unconditionally below.",
)


def _span(name: str = SPAN_AGENT_RUN, **attributes: Any) -> Span:
    """A finished span with a realistic id and duration."""
    span = Span(
        trace_id="9b06f1e2c4a74d5e8f0a1b2c3d4e5f60",
        span_id="a1b2c3d4e5f60789",
        parent_span_id=None,
        name=name,
    )
    span.update(attributes)
    span.end_ns = span.start_ns + 1_500_000_000
    span.status = "ok"
    return span


# --- it degrades ------------------------------------------------------------


def test_a_missing_package_is_a_banner_line_not_a_crash(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The same rule as a closed metrics port and a missing Qdrant.

    Observability is how you watch an agent work, not part of the work
    (ADR-069, ADR-081). A missing optional package must produce a sentence
    a person can read, naming what to install, and a session that runs.
    """
    real_import = builtins.__import__

    def _no_otel(name: str, *args: Any, **kwargs: Any) -> Any:
        if name.startswith("opentelemetry"):
            raise ImportError("No module named 'opentelemetry'", name="opentelemetry")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", _no_otel)
    exporter, problem = build_otlp_exporter()

    assert exporter is None
    assert problem is not None
    assert "not installed" in problem
    # The message names the command, because a reason nobody can act on is
    # the same as no reason.
    assert "pip install opentelemetry-sdk" in problem


def test_a_bad_endpoint_is_reported_rather_than_raised(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Anything the SDK objects to at construction becomes a banner line."""

    def _explode(*args: Any, **kwargs: Any) -> Any:
        raise ValueError("endpoint must be a URL")

    monkeypatch.setattr(
        "opentelemetry.exporter.otlp.proto.http.trace_exporter.OTLPSpanExporter",
        _explode,
    )
    exporter, problem = build_otlp_exporter("http://nowhere.invalid:4318")

    assert exporter is None
    assert problem is not None and "OTLP export off" in problem


def test_an_exporter_whose_processor_raises_does_not_fail_a_run() -> None:
    """Third-party code is still code that can throw.

    The tracer swallows it — an observer must not be able to fail a task —
    and counts the loss, so the failure is visible rather than silent.
    """

    class _Hostile:
        def on_end(self, span: Any) -> None:
            raise RuntimeError("collector exploded")

        def shutdown(self) -> None:
            raise RuntimeError("collector exploded")

    exporter = OtlpSpanExporter(
        processor=_Hostile(),
        resource=None,
        scope=None,
        readable_span=lambda **kwargs: kwargs,
        span_context=lambda **kwargs: kwargs,
        trace_flags=lambda value: value,
        otel_kind=type("Kind", (), {"INTERNAL": 0, "CLIENT": 1}),
        status=lambda code, description=None: (code, description),
        status_code=type("Code", (), {"OK": 1, "ERROR": 2, "UNSET": 0}),
    )
    tracer = Tracer([exporter])

    with tracer.span(SPAN_AGENT_RUN):
        pass

    assert tracer.dropped_spans == 1
    tracer.shutdown()


# --- the translation --------------------------------------------------------


def test_the_trace_id_reaching_jaeger_is_the_run_id() -> None:
    """The decision that makes an external viewer usable with this harness.

    Our ids are hex strings because a JSONL filename and a URL want them
    that way; the SDK's are integers. The conversion is one ``int(..., 16)``
    and nothing is regenerated, so the id in the banner, the id naming the
    trace file and the id in a Jaeger URL are the same string (ADR-095).
    """
    exporter, problem = build_otlp_exporter()
    assert problem is None and exporter is not None

    run_id = "9b06f1e2c4a74d5e8f0a1b2c3d4e5f60"
    readable = exporter._translate(_span(**{Attr.RUN_ID: run_id}))

    assert format(readable.context.trace_id, "032x") == run_id
    assert format(readable.context.span_id, "016x") == "a1b2c3d4e5f60789"
    exporter.shutdown()


def test_parentage_survives_the_translation() -> None:
    """A child span must arrive with its parent, or the tree is lost."""
    exporter, _ = build_otlp_exporter()
    assert exporter is not None

    child = _span(SPAN_LLM_CALL)
    child.span_id = "0f0f0f0f0f0f0f0f"
    child.parent_span_id = "a1b2c3d4e5f60789"
    readable = exporter._translate(child)

    assert readable.parent is not None
    assert format(readable.parent.span_id, "016x") == "a1b2c3d4e5f60789"
    assert readable.parent.trace_id == readable.context.trace_id
    exporter.shutdown()


def test_a_failure_arrives_as_an_error_status_with_its_reason() -> None:
    """A trace viewer colours by status, so a failed span must say so."""
    from opentelemetry.trace.status import StatusCode

    exporter, _ = build_otlp_exporter()
    assert exporter is not None

    span = _span()
    span.fail("mcp.connection", "McpConnectionError: server died")
    readable = exporter._translate(span)

    assert readable.status.status_code is StatusCode.ERROR
    assert readable.status.description == "McpConnectionError: server died"
    # The taxonomy entry travels as an attribute too, under OTel's own
    # `error.type` key, so a viewer that groups by it does so without being
    # configured.
    assert readable.attributes["error.type"] == "mcp.connection"
    exporter.shutdown()


def test_gen_ai_attributes_use_the_semantic_conventions() -> None:
    """Adopting the names is what makes the trace readable by other tools.

    Using our own keys here would have produced a trace only our own reader
    could interpret, which defeats the point of adopting the shape at all.
    """
    exporter, _ = build_otlp_exporter()
    assert exporter is not None

    readable = exporter._translate(
        _span(
            SPAN_LLM_CALL,
            **{
                Attr.GEN_AI_REQUEST_MODEL: "gemma4-31b-it",
                Attr.GEN_AI_USAGE_INPUT_TOKENS: 250,
                Attr.GEN_AI_USAGE_OUTPUT_TOKENS: 30,
            },
        )
    )

    assert readable.attributes["gen_ai.request.model"] == "gemma4-31b-it"
    assert readable.attributes["gen_ai.usage.input_tokens"] == 250
    assert readable.attributes["gen_ai.usage.output_tokens"] == 30
    exporter.shutdown()
