"""The harness's own model calls reach the dashboard like the agent's do.

Extraction, the consolidation judge and conversation summaries call the
model outside the loop, so ``before_llm``/``after_llm`` never see them.
``TelemetryHook.traced_llm`` puts each one on an ``llm.call`` span, and
these tests pin the two things that makes true: the span carries tokens and
a purpose, and the metrics exporter counts it with no change of its own
(ADR-107).
"""

import pytest
from harness.llm.protocol import LLMError
from harness.observability import (
    SPAN_AGENT_RUN,
    SPAN_LLM_CALL,
    Attr,
    MetricsSpanExporter,
    NullRecorder,
    Span,
    Tracer,
)
from harness.observability.telemetry import TelemetryHook
from tests.fakes import FailingLLM, ScriptedLLM, text

_MESSAGES = [{"role": "user", "content": "hello"}]


class _Collector:
    """An exporter that keeps every finished span."""

    def __init__(self) -> None:
        self.spans: list[Span] = []

    def on_start(self, span: Span) -> None:
        """Ignore starts."""

    def export(self, span: Span) -> None:
        self.spans.append(span)

    def shutdown(self) -> None:
        """Nothing to release."""

    def only(self, name: str) -> Span:
        found = [span for span in self.spans if span.name == name]
        assert len(found) == 1
        return found[0]


class _LLMCalls(NullRecorder):
    """A recorder that keeps only what ``llm_call`` was told."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, int, int]] = []

    def llm_call(self, role: str, seconds: float, prompt: int, completion: int) -> None:
        self.calls.append((role, prompt, completion))


def test_a_traced_call_is_an_llm_span_with_tokens_and_purpose() -> None:
    """Same span, same token attributes, plus why the harness asked."""
    collector = _Collector()
    hook = TelemetryHook(Tracer([collector]), model="test-model", temperature=0.0)
    llm = hook.traced_llm(ScriptedLLM([text("{}", 12, 3)]), "memory.extract")

    llm.chat(_MESSAGES, [])

    span = collector.only(SPAN_LLM_CALL)
    assert span.attributes[Attr.LLM_PURPOSE] == "memory.extract"
    assert span.attributes[Attr.GEN_AI_USAGE_INPUT_TOKENS] == 12
    assert span.attributes[Attr.GEN_AI_USAGE_OUTPUT_TOKENS] == 3
    assert span.attributes[Attr.GEN_AI_REQUEST_MODEL] == "test-model"
    assert span.status == "ok"


def test_a_failed_traced_call_is_recorded_and_reraised() -> None:
    """The tracer records the error; the caller still decides what it means."""
    collector = _Collector()
    hook = TelemetryHook(Tracer([collector]))
    llm = hook.traced_llm(FailingLLM(LLMError("429")), "memory.summarise")

    with pytest.raises(LLMError):
        llm.chat(_MESSAGES, [])

    assert collector.only(SPAN_LLM_CALL).status == "error"


def test_the_metrics_exporter_counts_it_under_the_enclosing_run_role() -> None:
    """The dashboard's token panel includes memory's calls, as the run's."""
    recorder = _LLMCalls()
    tracer = Tracer([MetricsSpanExporter(recorder)])
    llm = TelemetryHook(tracer).traced_llm(
        ScriptedLLM([text("{}", 7, 2)]), "memory.consolidate"
    )

    with tracer.span(SPAN_AGENT_RUN, attributes={Attr.ROLE: "main"}):
        llm.chat(_MESSAGES, [])

    assert recorder.calls == [("main", 7, 2)]


def test_a_call_made_during_a_run_is_counted_on_that_runs_span() -> None:
    """The run span and the dashboard now agree on how many calls a run made.

    Before ADR-110 the run span's count came only from ``after_llm``, which
    the harness's own calls never reach, while the metrics counter saw every
    ``llm.call`` span.
    """
    from harness.context import RunContext

    collector = _Collector()
    hook = TelemetryHook(Tracer([collector]))
    llm = hook.traced_llm(ScriptedLLM([text("{}", 9, 4)]), "memory.extract")
    context = RunContext(task="")
    context.start_turn("hello")

    hook.on_run_start(context)
    llm.chat(_MESSAGES, [])
    context.exit_reason = "success"
    hook.on_run_end(context)

    run = collector.only(SPAN_AGENT_RUN)
    assert run.attributes["harness.count.llm_calls"] == 1
    assert run.attributes["harness.count.prompt_tokens"] == 9
    assert run.attributes["harness.count.completion_tokens"] == 4
