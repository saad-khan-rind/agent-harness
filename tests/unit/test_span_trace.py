"""The span model: shape, nesting, safety, and the promise never to crash.

These are the tests for the instrumentation *core* — the tracer, the
telemetry hook and the JSONL exporter. The Prometheus half is
``test_span_metrics.py`` and the reader is ``test_trace_reader.py``, which
is the same three-way split the code has.

Driven through the **real** :class:`~harness.loop.AgentLoop` wherever the
assertion is about a real run, with only the model replaced (ADR-014). A
test that drove the hook directly could prove the hook consistent with
itself and nothing about whether the loop fires the callbacks it assumes.
"""

import json
from pathlib import Path
from typing import Any

import pytest
from harness.context import RunContext
from harness.llm.protocol import LLMError, LLMResponse
from harness.loop import AgentLoop
from harness.observability.api import (
    SPAN_AGENT_RUN,
    SPAN_ITERATION,
    SPAN_LLM_CALL,
    SPAN_PERMISSION,
    SPAN_TOOL_CALL,
    Attr,
    Span,
    Tracer,
    as_trace_id,
)
from harness.observability.exporters.jsonl import JsonlSpanExporter
from harness.observability.telemetry import TelemetryHook
from harness.permissions import RiskBasedPolicy
from harness.tools.base import ERROR_PREFIX, Tool, ToolOutcome, ToolResult
from harness.tools.registry import ToolRegistry
from tests.fakes import ScriptedLLM, call, text

# --- helpers ----------------------------------------------------------------


class _Collector:
    """An exporter that keeps every span, so tests can look at them."""

    def __init__(self) -> None:
        self.spans: list[Span] = []
        self.started: list[Span] = []
        self.closed = False

    def on_start(self, span: Span) -> None:
        self.started.append(span)

    def export(self, span: Span) -> None:
        self.spans.append(span)

    def shutdown(self) -> None:
        self.closed = True

    def named(self, name: str) -> list[Span]:
        """Every finished span with this name, in completion order."""
        return [span for span in self.spans if span.name == name]

    def only(self, name: str) -> Span:
        """The single finished span with this name."""
        found = self.named(name)
        assert len(found) == 1, f"expected one {name}, got {len(found)}"
        return found[0]


class _EchoTool(Tool):
    """A tool that succeeds, fails or raises on demand."""

    name = "echo"
    description = "Echo the text back, or fail when asked to."
    parameters: dict[str, Any] = {
        "type": "object",
        "properties": {"text": {"type": "string", "description": "What to echo."}},
        "required": ["text"],
    }
    risk = "observe"

    def __init__(self, root: Path, mode: str = "ok") -> None:
        super().__init__(root)
        self.mode = mode

    def _run(self, arguments: dict[str, Any]) -> ToolResult:
        if self.mode == "raise":
            raise FileNotFoundError("no such file: ledger.txt")
        if self.mode == "fail":
            return ToolResult("the remote end said no", ok=False)
        return ToolResult(str(arguments.get("text", "")))


class _WriteTool(Tool):
    """A mutating tool, so the permission policy has something to gate."""

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


def _registry(root: Path, mode: str = "ok") -> ToolRegistry:
    registry = ToolRegistry()
    registry.register(_EchoTool(root, mode))
    registry.register(_WriteTool(root))
    return registry


def _wire(tmp_path: Path, **kwargs: Any) -> tuple[TelemetryHook, _Collector]:
    """Build a telemetry hook over a collecting tracer."""
    collector = _Collector()
    hook = TelemetryHook(Tracer([collector]), **kwargs)
    return hook, collector


def _run(
    tmp_path: Path,
    responses: list[LLMResponse],
    *,
    mode: str = "ok",
    telemetry: TelemetryHook | None = None,
    policy: Any = None,
    llm: Any = None,
) -> RunContext:
    """Drive a real loop with a scripted model and return its context."""
    context = RunContext(task="")
    context.ensure_system_prompt("system")
    context.start_turn("do the thing")
    loop = AgentLoop(
        llm=llm if llm is not None else ScriptedLLM(responses),
        registry=_registry(tmp_path, mode),
        hook=telemetry,
        policy=policy,
    )
    loop.run(context)
    return context


# --- identifiers ------------------------------------------------------------


def test_trace_id_of_a_run_is_the_run_id() -> None:
    """The one decision that makes an external trace viewer usable here.

    A run id has been ``uuid4().hex`` since Week 1 — 32 hex characters,
    which is already exactly an OTLP trace id. So the id in the banner, the
    id naming the trace file and the id in a Jaeger URL are one string, and
    nobody has to translate between them while debugging (ADR-095).
    """
    run_id = "9b06f1e2c4a74d5e8f0a1b2c3d4e5f60"
    assert as_trace_id(run_id) == run_id


# --- the tree ---------------------------------------------------------------


def test_a_successful_run_produces_the_expected_tree(tmp_path: Path) -> None:
    """The structural promise, checked end to end through the real loop.

    One ``agent.run``; one ``agent.iteration`` per reason-act cycle; an
    ``llm.call`` inside each; the ``tool.call`` inside the iteration that
    requested it; and ``permission.check`` inside the call it decided.

    The nesting is the point. A flat event log could say all six things
    happened; only parentage says the permission decision belongs to *that*
    tool call, which stops being inferable the moment a model requests two
    tools in one turn (ADR-093).
    """
    hook, collector = _wire(tmp_path, model="test-model")
    _run(
        tmp_path,
        [call("echo", text="hi"), text("done")],
        telemetry=hook,
    )

    run = collector.only(SPAN_AGENT_RUN)
    assert run.parent_span_id is None
    assert len(collector.named(SPAN_ITERATION)) == 2
    assert len(collector.named(SPAN_LLM_CALL)) == 2

    tool = collector.only(SPAN_TOOL_CALL)
    permission = collector.only(SPAN_PERMISSION)
    first_iteration = collector.named(SPAN_ITERATION)[0]

    # permission -> tool -> iteration -> run, each link read from the
    # record rather than inferred from adjacency.
    assert permission.parent_span_id == tool.span_id
    assert tool.parent_span_id == first_iteration.span_id
    assert first_iteration.parent_span_id == run.span_id
    # One trace for the whole thing.
    assert {span.trace_id for span in collector.spans} == {run.trace_id}


# --- failures ---------------------------------------------------------------


def test_a_failed_llm_call_is_recorded_as_a_failed_llm_call(tmp_path: Path) -> None:
    """The gap this release closes, stated as a test.

    ``after_llm`` fires only on success, so before ``on_llm_error`` existed
    a run killed by a 429 produced a failed *run* and no model-call evidence
    at all: the infrastructure failure and the agent failure were
    indistinguishable because only one of them was recorded (ADR-094).
    """

    class _Dead:
        def chat(self, messages: Any, tools: Any) -> LLMResponse:
            raise LLMError("InnKube request failed: 429 Too Many Requests")

    hook, collector = _wire(tmp_path)
    context = RunContext(task="")
    context.ensure_system_prompt("system")
    context.start_turn("do the thing")
    loop = AgentLoop(llm=_Dead(), registry=_registry(tmp_path), hook=hook)

    with pytest.raises(LLMError):
        loop.run(context)

    llm = collector.only(SPAN_LLM_CALL)
    assert llm.status == "error"
    assert llm.error_type == "llm.rate_limited"
    # The run span names the cause too, so the top of the trace says why
    # rather than making a reader descend to find out.
    run = collector.only(SPAN_AGENT_RUN)
    assert run.status == "error"
    assert run.error_type == "llm.rate_limited"


def test_a_permission_denial_is_a_decision_and_a_failed_call(tmp_path: Path) -> None:
    """Both halves, because they are different facts.

    The ``permission.check`` span succeeds — the policy worked — while the
    ``tool.call`` it refused fails with ``permission.denied``. A layer that
    marked the decision itself as an error would report a correctly
    defended harness as a broken one (ADR-057).
    """
    hook, collector = _wire(tmp_path, permission_mode="deny_mutations")
    _run(
        tmp_path,
        [call("write_note", body="x"), text("done")],
        telemetry=hook,
        policy=RiskBasedPolicy("deny"),
    )

    permission = collector.only(SPAN_PERMISSION)
    assert permission.status == "ok"
    assert permission.attributes[Attr.PERMISSION_DECISION] == "deny"
    assert permission.attributes[Attr.PERMISSION_MODE] == "deny_mutations"
    # `user_confirmed` is absent, because nobody was asked. Absent is not
    # the same fact as `False`, which would mean somebody said no.
    assert Attr.PERMISSION_CONFIRMED not in permission.attributes

    tool = collector.only(SPAN_TOOL_CALL)
    assert tool.status == "error"
    assert tool.error_type == "permission.denied"


def test_a_guard_is_not_filed_as_an_error(tmp_path: Path) -> None:
    """A working safety net must not show up red.

    ``capped`` and ``no_progress`` record their reason without an error
    status, so a dashboard filtering on failures does not count the
    iteration cap doing its job (ADR-047, ADR-049).
    """
    hook, collector = _wire(tmp_path, max_iterations=2)
    context = RunContext(task="")
    context.ensure_system_prompt("system")
    context.start_turn("do the thing")
    AgentLoop(
        llm=ScriptedLLM([call("echo", text="a")] * 4),
        registry=_registry(tmp_path),
        hook=hook,
        max_iterations=2,
    ).run(context)

    run = collector.only(SPAN_AGENT_RUN)
    assert run.attributes[Attr.EXIT_REASON] == "capped"
    assert run.status == "ok"
    assert run.error_type == "guard.iteration_cap"


# --- redaction --------------------------------------------------------------


def test_argument_values_never_reach_a_span_by_default(tmp_path: Path) -> None:
    """Safe by default, and checked on the real path rather than in isolation.

    The digest says a string of 11 characters was passed; it does not say
    which. That is the whole redaction contract, and a test that only
    exercised the redaction function would not catch a hook that forgot to
    call it (ADR-094).
    """
    secret = "hunter2-key"
    hook, collector = _wire(tmp_path)
    _run(tmp_path, [call("echo", text=secret), text("done")], telemetry=hook)

    tool = collector.only(SPAN_TOOL_CALL)
    assert tool.attributes[Attr.TOOL_ARGS_DIGEST] == f"text=str[{len(secret)}]"
    assert secret not in json.dumps(tool.attributes)
    # And nowhere else in the whole trace either.
    for span in collector.spans:
        assert secret not in json.dumps(span.attributes, default=str)


def test_a_secret_looking_argument_is_removed_key_and_all(tmp_path: Path) -> None:
    """Even the *name* goes, because which credential a call carried is a fact."""
    hook, collector = _wire(tmp_path, redaction="values")
    context = RunContext(task="")
    context.ensure_system_prompt("system")
    context.start_turn("do the thing")
    hook.on_run_start(context)
    hook.before_tool("echo", {"api_key": "sk-live-abcdef", "text": "hello"})
    hook.after_tool("echo", "ok", ToolOutcome("ok"))
    hook.on_run_end(context)

    digest = collector.only(SPAN_TOOL_CALL).attributes[Attr.TOOL_ARGS_DIGEST]
    assert "sk-live-abcdef" not in digest
    assert "api_key" not in digest
    assert "[redacted]=[redacted]" in digest
    # The non-secret argument is still described, so the mode is useful.
    assert "text=hello" in digest


# --- the file ---------------------------------------------------------------


def test_one_file_per_run_named_for_the_run(tmp_path: Path) -> None:
    """Which file a span belongs to is derived, not remembered.

    The exporter holds no per-run state at all — the path comes from
    ``trace_id``, which every span in a run shares including a delegated
    child's — so nothing from one REPL turn can leak into the next.
    """
    exporter = JsonlSpanExporter(tmp_path / "traces")
    hook = TelemetryHook(Tracer([exporter]))
    context = _run(tmp_path, [text("done")], telemetry=hook)

    (path,) = (tmp_path / "traces").glob("*.jsonl")
    assert path.stem == context.run_id
    records = [json.loads(line) for line in path.read_text().splitlines()]
    assert records[-1]["span"] == SPAN_AGENT_RUN
    assert all(record["trace_id"] == path.stem for record in records)


# --- the promise never to crash ---------------------------------------------


def test_an_exporter_that_always_raises_does_not_fail_the_run(
    tmp_path: Path,
) -> None:
    """The rule the whole layer is built on, tested at its weakest point.

    An exporter is third-party code, or code touching a disk that may be
    full, or arithmetic on a label that may be new. Any of those failing is
    a reason to lose a span; none is a reason to fail the user's task
    (ADR-069, ADR-081).
    """

    class _Hostile:
        def on_start(self, span: Span) -> None:
            raise RuntimeError("boom")

        def export(self, span: Span) -> None:
            raise RuntimeError("boom")

        def shutdown(self) -> None:
            raise RuntimeError("boom")

    tracer = Tracer([_Hostile()])
    hook = TelemetryHook(tracer)
    context = _run(tmp_path, [call("echo", text="hi"), text("done")], telemetry=hook)

    assert context.exit_reason == "success"
    # The losses are counted rather than swallowed: a telemetry layer that
    # quietly stops recording is worse than one that is visibly off.
    assert tracer.dropped_spans > 0
    tracer.shutdown()


# --- classification ---------------------------------------------------------


def test_an_mcp_failure_names_its_server(tmp_path: Path) -> None:
    """The end-to-end version of the above, on a real tool call."""
    hook, collector = _wire(tmp_path)
    context = RunContext(task="")
    context.ensure_system_prompt("system")
    context.start_turn("do the thing")
    hook.on_run_start(context)
    hook.before_tool("web__web_search", {"query": "x"})
    hook.after_tool(
        "web__web_search",
        f"{ERROR_PREFIX} web MCP server: connection closed",
        ToolOutcome("boom", ok=False, error_type="mcp.connection"),
    )
    hook.on_run_end(context)

    tool = collector.only(SPAN_TOOL_CALL)
    assert tool.attributes[Attr.MCP_SERVER] == "web"
    assert tool.attributes[Attr.MCP_REMOTE_TOOL] == "web_search"
    assert tool.error_type == "mcp.connection"
    assert tool.kind == "client"


# --- retries and finish reasons ---------------------------------------------


def test_a_truncated_answer_is_visible(tmp_path: Path) -> None:
    """``length`` means the answer stops mid-sentence.

    Indistinguishable from a correct one on every other signal: the run
    succeeded, the request returned, no tool failed, and the user got half
    a sentence (ADR-094).
    """
    hook, collector = _wire(tmp_path)
    context = RunContext(task="")
    context.ensure_system_prompt("system")
    context.start_turn("do the thing")
    hook.on_run_start(context)
    hook.before_llm(context)
    hook.after_llm(context, LLMResponse(content="half a sen", finish_reason="length"))
    hook.on_run_end(context)

    llm = collector.only(SPAN_LLM_CALL)
    assert llm.attributes[Attr.GEN_AI_RESPONSE_FINISH_REASON] == "length"
    assert llm.attributes[Attr.LLM_RESULTED_IN_ANSWER] is True


# --- composition ------------------------------------------------------------
