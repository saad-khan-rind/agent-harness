"""Where finished spans go.

Three, and they are deliberately unaware of each other:

* :mod:`~harness.observability.exporters.jsonl` — one file per run on the
  memory volume, which is what ``/trace`` reads and what survives a
  container that no longer exists.
* :mod:`~harness.observability.exporters.metrics` — the Prometheus
  counters and histograms, whose names are frozen (ADR-081). This is the
  only exporter that also listens for a span *opening*, because
  ``harness_active_runs`` is a gauge.
* :mod:`~harness.observability.exporters.otlp` — optional, off by default,
  and a no-op when the OpenTelemetry packages are absent. It exists so the
  same spans can be read in Jaeger or Tempo without the harness depending
  on either (ADR-095).

Adding a fourth is adding a file here and one line where the CLI builds the
tracer. Nothing in ``harness/`` outside this package needs to change, which
is the property the span rewrite was for.
"""

from harness.observability.exporters.jsonl import JsonlSpanExporter
from harness.observability.exporters.metrics import MetricsSpanExporter
from harness.observability.exporters.otlp import OtlpSpanExporter, build_otlp_exporter

__all__ = [
    "JsonlSpanExporter",
    "MetricsSpanExporter",
    "OtlpSpanExporter",
    "build_otlp_exporter",
]
