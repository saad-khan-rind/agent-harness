"""Optional OpenTelemetry export, so the same spans open in Jaeger or Tempo.

The brief asks whether a specialised trace backend would materially improve
inspection. Our answer is: the *backend* would not change what we record,
but being able to open a run in a standard viewer changes who can read it.
``/trace`` renders a tree in a terminal and is enough for the person who
wrote the harness; a flame graph with a searchable attribute panel is what
makes a trace useful to somebody who did not. So the spans are OTLP-shaped
and there is an exporter, and both are optional (ADR-095).

**Nothing is coupled to a vendor.** :mod:`harness.observability.api` has no
idea this file exists. The span model is ours, the ids are ours, and this
module is a translation at the edge — which is exactly the arrangement that
lets the OpenTelemetry packages be absent, the collector be down, or the
whole feature be off, with the agent behaving identically in all three
cases.

**Off by default, and three independent ways to end up doing nothing:**

1. ``otlp_enabled: false`` in ``config.yaml`` — the CLI never builds one.
2. The packages are not installed — :func:`build_otlp_exporter` returns
   ``None`` and says why, in the banner.
3. The collector is unreachable — the OTLP SDK retries in its own
   background thread and drops. The agent never waits on it, because spans
   go through a ``BatchSpanProcessor``: ``export`` hands over a span and
   returns, and the network happens somewhere else.

That last point is the one that matters for a live agent. An exporter that
blocked on a dead collector would add its timeout to every tool call in the
run, which is a monitoring system making the thing it monitors worse.

**Why translate rather than instrument with the SDK directly.** We already
mint our own ids and hold our own stack, and the ids are chosen to *be* the
run ids (ADR-095). Handing that to the SDK would mean either giving up the
id equality — the thing that makes a Jaeger link and a trace filename the
same string — or fighting the SDK's context propagation to force it. A
translation at the boundary is twenty lines and keeps both.
"""

from typing import Any

# What the collector is told this service is called. Also what a person
# types into Jaeger's service dropdown, so it matches the image name rather
# than being something only this file knows.
DEFAULT_SERVICE_NAME = "agent-harness"

# Default OTLP/HTTP endpoint. The collector's own default port; the path is
# appended by the SDK's exporter.
DEFAULT_ENDPOINT = "http://localhost:4318"

# Our span kinds mapped onto OTel's enum. Only two of the five, because
# those are the two we can honestly claim (see the `SpanKind` note in
# harness/observability/api.py).
_KIND_NAMES = {"internal": "INTERNAL", "client": "CLIENT"}


class OtlpSpanExporter:
    """Translate harness spans into OpenTelemetry ones and hand them over.

    Built by :func:`build_otlp_exporter`, never directly: the constructor
    takes objects that only exist once the optional packages have been
    imported successfully, and that import is the thing that has to be
    allowed to fail.
    """

    def __init__(
        self,
        processor: Any,
        resource: Any,
        scope: Any,
        readable_span: Any,
        span_context: Any,
        trace_flags: Any,
        otel_kind: Any,
        status: Any,
        status_code: Any,
    ) -> None:
        """Args are the SDK pieces resolved by :func:`build_otlp_exporter`.

        Passed in rather than imported here so this class is constructible
        in a test with fakes, and so the module imports cleanly on a machine
        with no OpenTelemetry installed.
        """
        self._processor = processor
        self._resource = resource
        self._scope = scope
        self._ReadableSpan = readable_span
        self._SpanContext = span_context
        self._TraceFlags = trace_flags
        self._Kind = otel_kind
        self._Status = status
        self._StatusCode = status_code
        self._closed = False

    def export(self, span: Any) -> None:
        """Queue one finished span for the collector.

        Returns as soon as the span is on the batch processor's queue. The
        actual HTTP request happens on the SDK's own background thread, so
        a slow or dead collector costs the agent nothing.
        """
        if self._closed:
            return
        self._processor.on_end(self._translate(span))

    def shutdown(self) -> None:
        """Flush whatever is queued, then stop. Safe to call twice."""
        if self._closed:
            return
        self._closed = True
        self._processor.shutdown()

    # -- translation -------------------------------------------------------

    def _translate(self, span: Any) -> Any:
        """Build an SDK ``ReadableSpan`` from one of ours.

        The ids are the interesting part: ours are hex strings because that
        is what a JSONL file and a URL want, and the SDK's are integers, so
        the conversion is one ``int(..., 16)`` in each direction and nothing
        is regenerated. That is what keeps the trace id in Jaeger equal to
        the run id in the banner.
        """
        context = self._SpanContext(
            trace_id=int(span.trace_id, 16),
            span_id=int(span.span_id, 16),
            is_remote=False,
            # Sampled. We do not sample: an agent run is a rare, expensive,
            # individually interesting event, and dropping a random one in
            # ten is how the run somebody wants to debug goes missing.
            trace_flags=self._TraceFlags(0x01),
        )
        parent = (
            self._SpanContext(
                trace_id=int(span.trace_id, 16),
                span_id=int(span.parent_span_id, 16),
                is_remote=False,
                trace_flags=self._TraceFlags(0x01),
            )
            if span.parent_span_id
            else None
        )

        if span.status == "error":
            status = self._Status(
                self._StatusCode.ERROR,
                description=span.attributes.get("harness.error.message"),
            )
        elif span.status == "ok":
            status = self._Status(self._StatusCode.OK)
        else:
            status = self._Status(self._StatusCode.UNSET)

        return self._ReadableSpan(
            name=span.name,
            context=context,
            parent=parent,
            resource=self._resource,
            attributes=_flatten(span.attributes),
            events=[],
            links=[],
            kind=getattr(self._Kind, _KIND_NAMES.get(span.kind, "INTERNAL")),
            instrumentation_scope=self._scope,
            status=status,
            start_time=span.start_wall_ns,
            # Wall-clock end derived from the monotonic duration rather than
            # read from a second clock: the duration is the number we trust,
            # and `time.time_ns()` twice around a span can disagree with it
            # if the system clock is adjusted mid-run.
            end_time=span.start_wall_ns + span.duration_ns,
        )


def build_otlp_exporter(
    endpoint: str = DEFAULT_ENDPOINT,
    service_name: str = DEFAULT_SERVICE_NAME,
    *,
    headers: dict[str, str] | None = None,
    timeout: int = 10,
) -> tuple[OtlpSpanExporter | None, str | None]:
    """Build the exporter, or explain in one line why there is none.

    Returns a pair rather than raising, for the same reason
    :func:`~harness.observability.server.serve_metrics` does: a missing
    package or an unreachable collector is a reason to run without traces,
    never a reason to refuse to run an agent (ADR-069, ADR-081).

    Args:
        endpoint: OTLP/HTTP base URL, e.g. ``http://localhost:4318``.
        service_name: What the collector files these spans under.
        headers: Extra headers, for a hosted collector that wants a key.
            Read from the environment by the caller — never from
            ``config.yaml``, which is committed.
        timeout: Seconds the SDK allows for one export attempt. Bounded and
            short: this runs on a background thread, but a thread stuck for
            a minute per batch still ends up dropping everything behind it.

    Returns:
        ``(exporter, None)`` when it was built, or ``(None, reason)``.
    """
    try:
        from opentelemetry.exporter.otlp.proto.http.trace_exporter import (
            OTLPSpanExporter,
        )
        from opentelemetry.sdk.resources import Resource
        from opentelemetry.sdk.trace import ReadableSpan
        from opentelemetry.sdk.trace.export import BatchSpanProcessor
        from opentelemetry.sdk.util.instrumentation import InstrumentationScope
        from opentelemetry.trace import SpanContext, SpanKind, TraceFlags
        from opentelemetry.trace.status import Status, StatusCode
    except ImportError as exc:
        return None, (
            f"OTLP export off: {exc.name or 'opentelemetry'} is not installed "
            f"(pip install opentelemetry-sdk opentelemetry-exporter-otlp-proto-http)"
        )

    try:
        exporter = OTLPSpanExporter(
            endpoint=f"{endpoint.rstrip('/')}/v1/traces",
            headers=headers or {},
            timeout=timeout,
        )
        processor = BatchSpanProcessor(exporter)
        resource = Resource.create(
            {
                "service.name": service_name,
                # Declares which conventions the `gen_ai.*` attributes
                # follow, so a viewer that renders LLM spans specially knows
                # it may.
                "telemetry.sdk.language": "python",
            }
        )
        scope = InstrumentationScope(name="harness.observability", version="2")
    except Exception as exc:  # noqa: BLE001 - see the module docstring
        return None, f"OTLP export off: {type(exc).__name__}: {exc}"

    return (
        OtlpSpanExporter(
            processor=processor,
            resource=resource,
            scope=scope,
            readable_span=ReadableSpan,
            span_context=SpanContext,
            trace_flags=TraceFlags,
            otel_kind=SpanKind,
            status=Status,
            status_code=StatusCode,
        ),
        None,
    )


def _flatten(attributes: dict[str, Any]) -> dict[str, Any]:
    """Coerce attribute values to the scalar types OTLP accepts.

    OTLP takes strings, numbers, booleans and homogeneous lists of those.
    Everything the harness records is already one of them — the redaction
    layer sees to that — so this is a guard rather than a conversion, and it
    stringifies rather than dropping so a future attribute of an unexpected
    type still arrives readable instead of vanishing.
    """
    flat: dict[str, Any] = {}
    for key, value in attributes.items():
        if isinstance(value, (str, bool, int, float)):
            flat[key] = value
        elif isinstance(value, (list, tuple)) and all(
            isinstance(item, (str, bool, int, float)) for item in value
        ):
            flat[key] = list(value)
        else:
            flat[key] = str(value)
    return flat
