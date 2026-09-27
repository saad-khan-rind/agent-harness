"""The one hook: lifecycle callbacks in, spans out.

This replaces the two Week 3 observers. ``MetricsHook`` and
``JsonlTraceHook`` each kept their own ``time.monotonic()`` stamps, each
derived per-call tokens as a difference off the context, each classified how
a run ended, and each had to be edited to record a new field. They were the
same arithmetic written twice, and the second copy was always one commit
behind the first.

Now there is one instrumentation point. It opens spans; the exporters decide
what a span becomes. Prometheus counters, JSONL lines and OTLP payloads are
all read off the same finished object, so a new attribute is added in one
place and appears in three (ADR-093).

**The tree this produces**, for a run that delegates once:

    agent.run                          role=main, exit_reason=success
    ├── agent.iteration                iteration=1
    │   ├── llm.call                   model, tokens, finish reason
    │   └── tool.call                  tool=memory_search, kind=memory
    │       └── permission.check       decision=allow
    ├── agent.iteration                iteration=2
    │   ├── llm.call
    │   └── tool.call                  tool=delegate, kind=delegate
    │       ├── permission.check
    │       └── subagent.run           role=researcher, parent linked
    │           ├── agent.iteration
    │           │   ├── llm.call
    │           │   └── tool.call      tool=web__web_search, kind=mcp
    │           │       └── permission.check
    │           └── agent.finalize     contract validation, repairs
    └── agent.iteration                iteration=3
        └── llm.call                   final_answer=true

Three things in that picture are new and each answers a question the flat
event log could not:

* **``agent.iteration`` exists.** A tool call now *belongs* to a reasoning
  step rather than sitting next to one, so "it went wrong on iteration 7"
  is a subtree and not an exercise in counting lines. It costs the loop
  nothing: the span is opened lazily from ``before_llm``, which is the only
  callback that fires exactly once per iteration, so ``loop.py`` gained no
  instrumentation for it (ADR-093).
* **``permission.check`` is a child of ``tool.call``.** The Week 3 reader
  paired a permission line with the tool line after it by *adjacency*, which
  is a guess that stops being true the first time a model requests two tool
  calls in one turn. Now it is parentage, and the pairing is a fact.
* **``subagent.run`` hangs under the parent's ``tool.call``.** The child's
  work is inside the delegation that caused it, so a parent's trace contains
  its children rather than referring to them.

**One instance, shared by every run.** Sub-agents used to get a fresh
``MetricsHook`` (its timers lived on the instance, so sharing would have
crossed a parent's and a child's stopwatches) and a shared trace hook. With
spans there is nothing to cross: per-run state lives on a stack of frames,
and sharing the instance is what makes a child's spans descend from the
parent's (ADR-093). The sub-agent factory is simpler for it.

**Nothing here raises.** Every callback is written so that a surprise —
an ``after_llm`` with no ``before_llm``, a context missing an attribute, a
clock that jumps — is a gap in the evidence rather than a failed task. The
rule predates this module (ADR-069, ADR-081); what is new is that the
tracer enforces the exporter half of it centrally, so this file only has to
be careful about its own arithmetic.
"""

import threading
import time
from dataclasses import dataclass, field
from typing import Any

from harness.context import RunContext, message_chars
from harness.observability.api import (
    SPAN_AGENT_RUN,
    SPAN_FINALIZE,
    SPAN_ITERATION,
    SPAN_LLM_CALL,
    SPAN_PERMISSION,
    SPAN_SUBAGENT_RUN,
    SPAN_TOOL_CALL,
    Attr,
    Span,
    Tracer,
    as_trace_id,
)
from harness.observability.errors import (
    AGENT_INCOMPLETE,
    GUARD_ITERATION_CAP,
    GUARD_NO_PROGRESS,
    classify_exception,
    classify_tool_failure,
    safe_message,
)
from harness.observability.redaction import (
    RedactionMode,
    argument_digest,
    argument_fingerprint,
    result_summary,
)
from harness.tools.base import ERROR_PREFIX, ToolOutcome

# Prefixes that say where a tool came from. Unchanged from Week 3 — the
# `kind` label is what lets a panel ask "how much of this run was MCP?"
# without listing every server's tools — and moved here because the module
# that owned it no longer exists.
_MEMORY_PREFIX = "memory_"
_SKILL_PREFIX = "skill_"
_DELEGATE = "delegate"
_MCP_SEPARATOR = "__"

_MAIN_ROLE = "main"

# How a run ended, mapped onto the span status. `capped` and `no_progress`
# are guards working as designed (ADR-047, ADR-049), so the span records the
# reason without being marked an error — a working safety net must not show
# up red in a trace viewer. `error` is the only one that does.
_EXIT_TO_ERROR_TYPE = {
    "capped": GUARD_ITERATION_CAP,
    "no_progress": GUARD_NO_PROGRESS,
}


def classify_tool(name: str) -> str:
    """Return the ``kind`` label for a tool name.

    Args:
        name: The registered tool name, as the model sees it.

    Returns:
        ``delegate``, ``memory``, ``skill``, ``mcp`` or ``builtin``.
    """
    if name == _DELEGATE:
        return "delegate"
    if name.startswith(_MEMORY_PREFIX):
        return "memory"
    if name.startswith(_SKILL_PREFIX):
        return "skill"
    if _MCP_SEPARATOR in name:
        return "mcp"
    return "builtin"


def mcp_server_of(name: str) -> str | None:
    """Return the MCP server a tool name belongs to, or ``None``.

    The server label has been the tool's name prefix since ADR-042, which
    made it recoverable but not *readable*: answering "which MCP server is
    responsible for this failure" meant string surgery in a PromQL query or
    in a person's head. Recording it as its own attribute makes it a
    dimension (ADR-094).

    Safe as a metric label, unlike the tool name it is derived from: server
    labels come from ``config.yaml`` and there are three of them, so the
    cardinality is bounded by a file somebody edits rather than by what a
    model invents.

    Args:
        name: The registered tool name.

    Returns:
        The configured server label, or ``None`` for a local tool.
    """
    if _MCP_SEPARATOR not in name:
        return None
    prefix = name.split(_MCP_SEPARATOR, 1)[0]
    return prefix or None


@dataclass
class _Frame:
    """Per-run state. One is pushed per run, including delegated ones.

    Everything here is either a baseline for a difference or a counter for
    the run summary. Nothing is a timer: durations come from spans now, and
    that is the point of the rewrite.

    Attributes:
        run_span: The ``agent.run`` or ``subagent.run`` span for this run.
        iteration_span: The reasoning step currently open, if any.
        llm_span: The model call currently in flight, if any.
        tool_span: The tool call currently in flight, if any.
        tool_started_ns: When the current tool call's dispatch began, so the
            ``permission.check`` span can be backdated to cover the decision
            rather than reporting zero.
        prompt_seen: Context prompt-token total at the last ``llm.call``, so
            each call reports its own tokens rather than a running sum. The
            context keeps cumulative session figures (ADR-031).
        completion_seen: Same, for completion tokens.
        prompt_at_start: Totals when this run began, so the run span can
            report the whole run's spend.
        completion_at_start: Same, for completion tokens.
        compactions_seen: Baseline for the same reason (ADR-114).
        llm_retries: Retries reported by the client for the call in flight.
        answered: Whether the last response carried no tool calls.
        counts: Aggregates for the run summary and for quality signals —
            see :meth:`TelemetryHook._finish_run`.
        last_fingerprint: The previous tool call's argument fingerprint, so
            a repeat can be counted without re-reading the arguments.
        repeat_run: How many identical calls have now been seen in a row.
    """

    run_span: Span
    iteration_span: Span | None = None
    llm_span: Span | None = None
    tool_span: Span | None = None
    tool_started_ns: int | None = None
    prompt_seen: int = 0
    completion_seen: int = 0
    prompt_at_start: int = 0
    completion_at_start: int = 0
    compactions_seen: int = 0
    llm_retries: int = 0
    answered: bool = False
    counts: dict[str, int] = field(default_factory=dict)
    last_fingerprint: str | None = None
    repeat_run: int = 0

    def bump(self, key: str, amount: int = 1) -> None:
        """Increment one aggregate."""
        self.counts[key] = self.counts.get(key, 0) + amount


class _FrameStack(threading.local):
    """One frame stack per thread, for the same reason the tracer has one.

    :class:`threading.local` subclass so ``__init__`` runs once per thread
    and every thread starts with its own empty list.
    """

    def __init__(self) -> None:
        self.frames: list[_Frame] = []


class TelemetryHook:
    """Turn the loop's lifecycle into a span tree.

    Implements :class:`~harness.hooks.Hook` structurally. One instance per
    session, shared with every sub-agent, composed by the CLI alongside the
    memory, skills and console hooks exactly as its two predecessors were.

    **Shared between runs, but its bookkeeping is per thread.** The frame
    stack pairs each ``on_run_start`` with the ``before_llm`` and
    ``before_tool`` calls that follow it, and it does so by position:
    ``_current()`` is the innermost frame. Once a batched ``delegate`` runs
    children concurrently, "innermost" stops meaning "mine" — child B's
    model call would be recorded against whichever child last started. So
    the stack lives in thread-local storage and each concurrent child keeps
    its own, exactly as :class:`~harness.observability.api.Tracer` does
    (ADR-106).
    """

    def __init__(
        self,
        tracer: Tracer,
        *,
        model: str | None = None,
        provider: str = "openai_compatible",
        temperature: float | None = None,
        permission_mode: str | None = None,
        redaction: RedactionMode = "shapes",
        max_iterations: int = 0,
    ) -> None:
        """Args:
        tracer: Where spans go. Its exporter list decides whether that
            means Prometheus, a file, an OTLP collector, or nothing.
        model: Configured model name, recorded on every ``llm.call`` as
            ``gen_ai.request.model``. Bounded cardinality — it comes from
            ``config.yaml`` — so it is safe as a metric label, and it is
            what makes "did the p95 move because we switched models"
            answerable at all.
        provider: Configured ``llm_provider``, recorded as
            ``gen_ai.system`` so runs against different providers can be
            told apart.
        temperature: Configured sampling temperature, recorded for the
            same reason: a comparison between two runs is meaningless
            without it.
        permission_mode: The active mode, recorded on every
            ``permission.check`` so a denial can be read as policy rather
            than as mystery.
        redaction: What may be recorded about tool arguments. See
            :mod:`harness.observability.redaction`.
        max_iterations: The loop's cap, or ``0`` for none. Used only to
            tell a capped run from a failed one when the context carries
            no ``exit_reason``.
        """
        self._tracer = tracer
        self._model = model
        self._provider = provider
        self._temperature = temperature
        self._permission_mode = permission_mode
        self._redaction: RedactionMode = redaction
        self._max_iterations = max_iterations
        self._local = _FrameStack()

    @property
    def _frames(self) -> list[_Frame]:
        """This thread's open run frames, innermost last."""
        return self._local.frames

    @property
    def tracer(self) -> Tracer:
        """The tracer this hook reports through."""
        return self._tracer

    # -- run ---------------------------------------------------------------

    def on_run_start(self, context: RunContext) -> None:
        """Open the span that covers this whole run.

        Which span depends on the tracer's stack, not on a flag: a run that
        begins while another span is open can only be a delegated one,
        because ``delegate`` is a tool and a child's ``on_run_start``
        therefore arrives between the parent's ``before_tool`` and
        ``after_tool``. So the nesting *is* the evidence, and no caller has
        to tell this hook which kind of run it is about to instrument.
        """
        delegated = self._tracer.current is not None
        run_id = _identifier(context, "run_id") or ""
        role = _identifier(context, "role") or _MAIN_ROLE

        attributes: dict[str, Any] = {
            Attr.RUN_ID: run_id or None,
            Attr.PARENT_RUN_ID: _identifier(context, "parent_run_id"),
            Attr.ROLE: role,
            Attr.DEPTH: getattr(context, "depth", 0),
            Attr.COMPONENT: "agent",
            Attr.GEN_AI_REQUEST_MODEL: self._model,
            Attr.PERMISSION_MODE: self._permission_mode,
        }
        if delegated:
            attributes[Attr.SUBAGENT_ROLE] = role
            span = self._tracer.start(SPAN_SUBAGENT_RUN, attributes=attributes)
        else:
            # The trace id *is* the run id. That is the single decision that
            # makes an external trace viewer usable with this harness: the id
            # in the banner, the id naming the trace file and the id in a
            # Jaeger URL are one string (ADR-095).
            span = self._tracer.start(
                SPAN_AGENT_RUN,
                trace_id=as_trace_id(run_id) if run_id else None,
                attributes=attributes,
            )

        self._frames.append(
            _Frame(
                run_span=span,
                prompt_seen=context.prompt_tokens,
                completion_seen=context.completion_tokens,
                prompt_at_start=context.prompt_tokens,
                completion_at_start=context.completion_tokens,
                compactions_seen=context.compactions,
            )
        )

    def on_run_end(self, context: RunContext) -> None:
        """Close the run span, with how it ended and what it cost.

        Fires from the loop's ``finally``, so it runs on a clean answer, on
        a guard, and on a dead endpoint alike. Anything still open beneath
        it — an iteration, a tool call the process died inside — is closed
        first, because a span left open is a span no exporter ever sees.
        """
        frame = self._pop()
        if frame is None:
            return

        self._close_iteration(frame)
        exit_reason = _identifier(context, "exit_reason") or self._infer_exit(
            frame, context
        )
        span = frame.run_span
        span.update(
            {
                Attr.EXIT_REASON: exit_reason,
                Attr.ITERATION: getattr(context, "iterations_used", 0) or None,
                Attr.GEN_AI_USAGE_INPUT_TOKENS: max(
                    0, context.prompt_tokens - frame.prompt_at_start
                ),
                Attr.GEN_AI_USAGE_OUTPUT_TOKENS: max(
                    0, context.completion_tokens - frame.completion_at_start
                ),
                Attr.CONTEXT_COMPACTIONS: max(
                    0, context.compactions - frame.compactions_seen
                )
                or None,
            }
        )
        self._attach_counts(span, frame)

        if exit_reason == "success":
            span.succeed()
        elif exit_reason in _EXIT_TO_ERROR_TYPE:
            # A guard, not a fault. Recorded as the reason without an error
            # status, so a dashboard filtering on `status=error` does not
            # count a working safety net as a failure (ADR-047, ADR-049).
            span.set(Attr.ERROR_TYPE, _EXIT_TO_ERROR_TYPE[exit_reason])
            span.succeed()
        else:
            # Only mark the run itself as the failure when nothing beneath
            # it already claimed one. A run that died because its MCP server
            # died should read `mcp.connection` at the top, not a generic
            # `agent.incomplete` that hides the cause behind one more click.
            span.fail(span.error_type or AGENT_INCOMPLETE)

        self._tracer.end(span)

    # -- model -------------------------------------------------------------

    def before_llm(self, context: RunContext) -> None:
        """Open the iteration, then the model call inside it.

        The iteration span is opened here because ``before_llm`` is the only
        callback that fires exactly once per reason-act cycle. Deriving it
        rather than asking the loop to announce it is what keeps ``loop.py``
        free of instrumentation it does not need (ADR-093).

        The context is measured here and nowhere else, because this is the
        one moment it *is* the thing about to be sent: the loop compacts
        immediately before this fires, so what is recorded is the window the
        model actually gets (ADR-032, ADR-114).
        """
        frame = self._current()
        if frame is None:
            return

        iteration = getattr(context, "iterations_used", 0)
        self._close_iteration(frame)
        frame.iteration_span = self._tracer.start(
            SPAN_ITERATION,
            attributes={
                Attr.ITERATION: iteration,
                Attr.ROLE: _identifier(context, "role") or _MAIN_ROLE,
                Attr.COMPONENT: "loop",
            },
        )

        compacted = max(0, context.compactions - frame.compactions_seen)
        frame.compactions_seen = context.compactions
        frame.iteration_span.update(
            {
                Attr.CONTEXT_CHARS: message_chars(context.messages),
                Attr.CONTEXT_COMPACTIONS: compacted or None,
            }
        )
        if compacted:
            frame.bump("compactions", compacted)

        frame.llm_retries = 0
        frame.llm_span = self._tracer.start(
            SPAN_LLM_CALL,
            # `client`, because the request leaves this process. A trace
            # viewer groups and colours by this, and claiming `internal` for
            # a network call would misdraw the critical path.
            kind="client",
            attributes={
                Attr.COMPONENT: "llm",
                Attr.GEN_AI_SYSTEM: self._provider,
                Attr.GEN_AI_OPERATION: "chat",
                Attr.GEN_AI_REQUEST_MODEL: self._model,
                Attr.GEN_AI_REQUEST_TEMPERATURE: self._temperature,
                Attr.ITERATION: iteration,
                Attr.ROLE: _identifier(context, "role") or _MAIN_ROLE,
            },
        )

    def after_llm(self, context: RunContext, response: Any) -> None:
        """Close the model call with what it cost and what it asked for.

        Token counts come from the context rather than the response, and as
        a *difference*: the context is the one place that has normalised
        them across endpoints that report usage differently or not at all
        (ADR-031).

        ``harness.llm.final_answer`` is the field that separates the brief's
        first two notions of success. A request that returns HTTP 200 sets
        it to ``false`` every time the model asked for another tool, which
        is normal; a *run* whose last call never sets it to ``true`` is a
        run that never answered, however healthy every individual request
        was (ADR-096).
        """
        frame = self._current()
        if frame is None:
            return
        span = frame.llm_span
        frame.llm_span = None
        if span is None:
            return

        tool_calls = list(getattr(response, "tool_calls", None) or [])
        frame.answered = not tool_calls
        prompt = max(0, context.prompt_tokens - frame.prompt_seen)
        completion = max(0, context.completion_tokens - frame.completion_seen)
        frame.prompt_seen = context.prompt_tokens
        frame.completion_seen = context.completion_tokens

        span.update(
            {
                Attr.GEN_AI_USAGE_INPUT_TOKENS: prompt,
                Attr.GEN_AI_USAGE_OUTPUT_TOKENS: completion,
                Attr.LLM_TOOL_CALLS: len(tool_calls),
                Attr.LLM_RESULTED_IN_ANSWER: frame.answered,
                Attr.LLM_RETRIES: frame.llm_retries or None,
                # Present on some OpenAI-compatible endpoints and absent on
                # others, so it is read rather than assumed. When it is
                # there it is worth having: `length` on the final call means
                # the answer was cut off mid-sentence, which looks exactly
                # like a successful run from every other angle.
                Attr.GEN_AI_RESPONSE_FINISH_REASON: _finish_reason(response),
            }
        )
        span.succeed()
        frame.bump("llm_calls")
        frame.bump("prompt_tokens", prompt)
        frame.bump("completion_tokens", completion)
        if frame.llm_retries:
            frame.bump("llm_retries", frame.llm_retries)
        self._tracer.end(span)

        # A response with no tool calls ends the reasoning step as well as
        # the run, so the iteration closes here rather than waiting for
        # `on_run_end`. Without this, anything another hook does on the way
        # out — long-term memory writes its session line, and
        # `CompositeHook` unwinds in reverse so it runs first — would be
        # recorded *inside* the last iteration. Appending to the run log is
        # not part of the model's final reasoning step, and a tree that
        # said it was would be quietly wrong in the one place a reader
        # trusts most (ADR-093).
        if frame.answered:
            self._close_iteration(frame)

    def on_llm_error(self, context: RunContext, error: BaseException) -> None:
        """Close the model call as a failure, classified.

        The gap this closes is the one §2 of the brief is really about.
        ``after_llm`` fires only on success, so before this callback existed
        a run killed by a 429 produced a run span marked failed and no
        ``llm.call`` evidence at all — the infrastructure failure and the
        agent failure were indistinguishable because only one of them was
        recorded (ADR-094).

        The failure is also stamped on the enclosing run span, so the top of
        the trace names the cause rather than saying ``agent.incomplete``
        and making a reader descend to find out why.
        """
        frame = self._current()
        if frame is None:
            return
        error_type = classify_exception(error)
        message = safe_message(error)

        span = frame.llm_span
        frame.llm_span = None
        if span is not None:
            span.set(Attr.LLM_RETRIES, frame.llm_retries or None)
            span.fail(error_type, message)
            self._tracer.end(span)

        frame.bump("llm_errors")
        frame.run_span.fail(error_type, message)

    def record_llm_retry(self, attempt: int, error: BaseException) -> None:
        """Note that the client is retrying the request in flight.

        Not a :class:`~harness.hooks.Hook` callback: retries happen inside
        :class:`~harness.llm.openai_compatible.OpenAICompatibleClient`, below the
        loop, and the loop never learns about them. Without this, three rate-limited
        attempts and one slow response are the same forty seconds on the
        same span, and "excessive retries" — which §8 of the brief asks us
        to surface — is invisible by construction.

        Wired by the CLI as the client's retry observer. A session that does
        not wire it simply records no retries.

        Args:
            attempt: 1-based attempt number that just failed.
            error: Why it failed, classified for the event.
        """
        frame = self._current()
        if frame is None:
            return
        frame.llm_retries += 1
        if frame.llm_span is not None:
            frame.llm_span.add_event(
                "retry",
                attempt=attempt,
                error_type=classify_exception(error),
            )

    # -- tools -------------------------------------------------------------

    def before_tool(self, name: str, arguments: dict[str, Any]) -> None:
        """Open the tool call, described but not disclosed.

        The arguments never reach telemetry as values at the default
        setting. What is recorded is their *shape* and a fingerprint — see
        :mod:`harness.observability.redaction` — which between them answer
        "how big was the write" and "is this the same call as last time"
        without a byte of workspace content leaving the process (ADR-094).
        """
        frame = self._current()
        if frame is None:
            return
        kind = classify_tool(name)
        server = mcp_server_of(name)
        fingerprint = argument_fingerprint(arguments)

        # Counted here rather than in the exporter: repetition is a property
        # of a *sequence* of calls, and an exporter sees one span at a time.
        if fingerprint is not None and fingerprint == frame.last_fingerprint:
            frame.repeat_run += 1
            frame.bump("repeated_calls")
        else:
            frame.repeat_run = 1
        frame.last_fingerprint = fingerprint

        frame.tool_started_ns = time.monotonic_ns()
        frame.tool_span = self._tracer.start(
            SPAN_TOOL_CALL,
            # Remote tools leave the process; local ones do not, and drawing
            # them the same way would hide which half of a slow run was the
            # network.
            kind="client" if kind == "mcp" else "internal",
            attributes={
                Attr.COMPONENT: "tool",
                Attr.TOOL_NAME: name,
                Attr.TOOL_KIND: kind,
                Attr.TOOL_ARGS_DIGEST: argument_digest(arguments, self._redaction),
                Attr.TOOL_ARGS_FINGERPRINT: fingerprint,
                Attr.MCP_SERVER: server,
                Attr.MCP_REMOTE_TOOL: (
                    name.split(_MCP_SEPARATOR, 1)[1] if server else None
                ),
                Attr.MCP_TRANSPORT: "stdio" if server else None,
                Attr.ITERATION: _iteration_of(frame),
            },
        )

    def after_permission(self, name: str, outcome: str) -> None:
        """Record the decision as a span inside the call it governs.

        Backdated to when the dispatch began, which makes the duration the
        thing worth measuring: for ``require_user_confirmation`` it is the
        seconds a person spent reading the prompt. That number used to be
        charged to the tool, making a declined call look like a slow one
        (ADR-057, ADR-087).

        A child of ``tool.call`` rather than a sibling, so the pairing of a
        decision with the call it decided is structural. The Week 3 reader
        inferred it from adjacency, which was correct until a model
        requested two tools in one turn.
        """
        frame = self._current()
        if frame is None:
            return
        span = self._tracer.start(
            SPAN_PERMISSION,
            attributes={
                Attr.COMPONENT: "permissions",
                Attr.TOOL_NAME: name,
                Attr.TOOL_KIND: classify_tool(name),
                Attr.PERMISSION_DECISION: outcome,
                Attr.PERMISSION_MODE: self._permission_mode,
                # Only meaningful for the two outcomes that involved asking.
                # `None` elsewhere, so "we never asked" and "they said no"
                # are not the same absent field.
                Attr.PERMISSION_CONFIRMED: (
                    outcome == "confirmed"
                    if outcome in ("confirmed", "declined")
                    else None
                ),
                Attr.ITERATION: _iteration_of(frame),
            },
        )
        if frame.tool_started_ns is not None:
            span.start_ns = frame.tool_started_ns
        if outcome in ("deny", "declined"):
            frame.bump("permission_refusals")
        frame.bump("permission_checks")
        # The decision is a fact, not a failure: a denial is the policy
        # working. The *call* it refused is marked failed a moment later by
        # `after_tool`, which is where the consequence belongs.
        span.succeed()
        self._tracer.end(span)

    def after_tool(
        self, name: str, result: str, outcome: ToolOutcome | None = None
    ) -> None:
        """Close the tool call with its outcome and, on failure, its reason.

        ``outcome.error_type`` is used when the caller supplied one, because
        it was set where the exception was still in scope. The text
        classifier is the fallback for the one case that has no outcome — a
        tool the failure guard retired before dispatch — and for any caller
        driving this hook directly (ADR-094).
        """
        frame = self._current()
        if frame is None:
            return
        span = frame.tool_span
        frame.tool_span = None
        frame.tool_started_ns = None
        if span is None:
            return

        kind = classify_tool(name)
        failed = (
            not outcome.ok if outcome is not None else result.startswith(ERROR_PREFIX)
        )
        span.update(
            {
                Attr.TOOL_RESULT_CHARS: result_summary(result, self._redaction),
                Attr.TOOL_FAILURE_STREAK: (
                    frame.repeat_run if frame.repeat_run > 1 else None
                ),
            }
        )

        frame.bump("tool_calls")
        frame.bump(f"tool_calls.{kind}")
        if failed:
            error_type = (
                outcome.error_type
                if outcome is not None and outcome.error_type
                else classify_tool_failure(result, mcp=kind == "mcp")
            )
            span.fail(error_type)
            frame.bump("tool_failures")
            frame.bump(f"error.{error_type}")
        else:
            span.succeed()
        self._tracer.end(span)

    # -- delegation --------------------------------------------------------

    def finalization(self, **attributes: Any) -> Any:
        """Return a context manager covering a run's finalization step.

        The brief's trace sketch ends every agent run with a
        ``finalization`` node, and for the top-level agent that node would
        be decorative: the loop returns the model's text and there is
        nothing to measure. For a **sub-agent** it is real work — the result
        is parsed, validated against the role's declared output contract,
        and re-requested if it does not fit (ADR-085) — and it is work that
        can fail while every model call and every tool call succeeded.

        So the span exists where it means something and is not emitted where
        it would not. The sub-agent factory opens it; nothing else does.

        Args:
            **attributes: Extra attributes, e.g. the result status and how
                many repair attempts were spent.

        Returns:
            A context manager yielding the open :class:`Span`.
        """
        return self._tracer.span(
            SPAN_FINALIZE,
            attributes={Attr.COMPONENT: "subagent", **attributes},
        )

    def traced_llm(self, llm: Any, purpose: str) -> "TracedLLM":
        """Wrap a chat client so each call it makes is an ``llm.call`` span.

        For the model calls the harness makes on its own behalf — memory
        extraction, the consolidation judge, conversation summaries. They
        do not go through the loop, so ``before_llm``/``after_llm`` never
        see them, and without this they would spend tokens the dashboard's
        LLM panels do not show. The span carries the same attributes as a
        loop call plus ``harness.llm.purpose``, and inherits the role of
        the run it happens inside, so the metrics exporter counts it with no
        change there (ADR-107).

        Args:
            llm: The client to wrap — in the CLI, the budgeted one.
            purpose: A short fixed label, never user input.

        Returns:
            A client with the same ``chat`` signature.
        """
        return TracedLLM(
            llm,
            self._tracer,
            purpose,
            self._model,
            self._temperature,
            on_finished=self._count_auxiliary_call,
            provider=self._provider,
        )

    def _count_auxiliary_call(self, prompt: int, completion: int) -> None:
        """Add one harness-made model call to the run it happened in.

        The metrics exporter counts every ``llm.call`` span, including these
        calls; the run span's ``llm_calls`` and token counts are bumped in
        ``after_llm``, which these calls never reach. Bumping the frame here
        keeps the two in agreement. A call made outside any run — none
        is today — is left to the span alone (ADR-110).
        """
        frame = self._current()
        if frame is None:
            return
        frame.bump("llm_calls")
        frame.bump("prompt_tokens", max(0, prompt))
        frame.bump("completion_tokens", max(0, completion))

    def memory_span(self, operation: str, *, write: bool = False) -> Any:
        """Return a context manager covering one long-term memory operation.

        Memory is instrumented through the tracer rather than through the
        hook seam for the obvious reason that
        :class:`~harness.memory.hook.MemoryHook` *is* a hook and cannot call
        itself. Until now that meant memory telemetry was derived from tool
        *names* in ``after_tool`` — which counted ``memory_search`` when the
        model called it and missed the retrieval the harness performs on
        every single run, because that one goes through no tool at all. The
        most frequent memory operation in the system was the one nothing
        measured (ADR-094).

        :mod:`harness.observability.api` is dependency-free precisely so
        this call does not make ``harness/memory/`` import a metrics library
        (ADR-034).

        Args:
            operation: ``search``, ``inject``, ``store``, ``append`` …
            write: Whether this operation changes stored state, which picks
                ``memory.write`` over ``memory.read``.

        Returns:
            A context manager yielding the open :class:`Span`.
        """
        from harness.observability.api import SPAN_MEMORY_READ, SPAN_MEMORY_WRITE

        return self._tracer.span(
            SPAN_MEMORY_WRITE if write else SPAN_MEMORY_READ,
            kind="client",
            attributes={
                Attr.COMPONENT: "memory",
                Attr.MEMORY_OPERATION: operation,
            },
        )

    # -- frames ------------------------------------------------------------

    def _current(self) -> _Frame | None:
        """The innermost run in progress, or ``None`` between runs.

        ``None`` rather than an error: a ``before_llm`` with no
        ``on_run_start`` is a composition mistake somewhere else, and this
        hook's answer to somebody else's mistake is to record nothing rather
        than to fail their task.
        """
        return self._frames[-1] if self._frames else None

    def _pop(self) -> _Frame | None:
        """Close the innermost frame."""
        return self._frames.pop() if self._frames else None

    def _close_iteration(self, frame: _Frame) -> None:
        """End the reasoning step currently open, if there is one."""
        if frame.iteration_span is not None:
            self._tracer.end(frame.iteration_span)
            frame.iteration_span = None
            frame.bump("iterations")

    def _infer_exit(self, frame: _Frame, context: RunContext) -> str:
        """Classify how a run ended when the loop left no reason.

        The loop sets ``RunContext.exit_reason`` at each of its terminal
        returns (ADR-084), so this is the fallback for a context the loop
        never drove: a hook exercised directly by a test, or one composed in
        a new order. A telemetry layer that raised on that would be an
        observer that can fail a run.
        """
        if frame.answered:
            return "success"
        if self._max_iterations and getattr(context, "iterations_used", 0) >= (
            self._max_iterations
        ):
            return "capped"
        return "error"

    @staticmethod
    def _attach_counts(span: Span, frame: _Frame) -> None:
        """Copy the run's aggregates onto its span.

        These are the numbers §8 of the brief asks to have available for
        later evaluation — iterations, tool calls, repeated calls, retries —
        and they live on the run span rather than being recomputed from the
        trace because the alternative is every reader re-deriving them and
        two readers disagreeing.
        """
        for key, value in sorted(frame.counts.items()):
            if value:
                span.set(f"harness.count.{key}", value)


class TracedLLM:
    """A chat client whose every call is recorded as an ``llm.call`` span.

    Satisfies :class:`~harness.llm.protocol.LLMClient` structurally. Built
    only by :meth:`TelemetryHook.traced_llm`.
    """

    def __init__(
        self,
        inner: Any,
        tracer: Tracer,
        purpose: str,
        model: str | None,
        temperature: float | None,
        on_finished: Any = None,
        provider: str = "openai_compatible",
    ) -> None:
        """Args:
        inner: The client that actually sends the request.
        tracer: Where the span goes.
        purpose: Recorded as ``harness.llm.purpose``.
        model: Recorded as ``gen_ai.request.model``.
        temperature: Recorded as ``gen_ai.request.temperature``.
        on_finished: Called with ``(prompt, completion)`` tokens after a
            successful call, so the enclosing run can count it.
        provider: Recorded as ``gen_ai.system``.
        """
        self._inner = inner
        self._provider = provider
        self._tracer = tracer
        self._purpose = purpose
        self._model = model
        self._temperature = temperature
        self._on_finished = on_finished

    def chat(self, messages: list[dict[str, Any]], tools: list[dict[str, Any]]) -> Any:
        """Send the request inside a span, recording what it cost.

        An exception is recorded on the span by the tracer and re-raised
        unchanged; the caller decides what a failure means.
        """
        with self._tracer.span(
            SPAN_LLM_CALL,
            kind="client",
            attributes={
                Attr.COMPONENT: "llm",
                Attr.GEN_AI_SYSTEM: self._provider,
                Attr.GEN_AI_OPERATION: "chat",
                Attr.GEN_AI_REQUEST_MODEL: self._model,
                Attr.GEN_AI_REQUEST_TEMPERATURE: self._temperature,
                Attr.LLM_PURPOSE: self._purpose,
            },
        ) as span:
            response = self._inner.chat(messages, tools)
            span.update(
                {
                    Attr.GEN_AI_USAGE_INPUT_TOKENS: getattr(
                        response, "prompt_tokens", 0
                    ),
                    Attr.GEN_AI_USAGE_OUTPUT_TOKENS: getattr(
                        response, "completion_tokens", 0
                    ),
                    Attr.GEN_AI_RESPONSE_FINISH_REASON: _finish_reason(response),
                }
            )
            if self._on_finished is not None:
                self._on_finished(
                    int(getattr(response, "prompt_tokens", 0) or 0),
                    int(getattr(response, "completion_tokens", 0) or 0),
                )
            return response


def _identifier(context: RunContext, name: str) -> str | None:
    """Read an optional identifier attribute off the context.

    ``getattr`` rather than a plain attribute access, deliberately: a hook
    is handed whatever object the caller passes, and an observer that
    raises ``AttributeError`` on an unexpected context would fail a run to
    protect a graph. Empty strings become ``None`` so a context before its
    first turn — ``run_id`` is ``""`` until ``start_turn`` mints one — is
    treated as having no id rather than as having a blank one.
    """
    value = getattr(context, name, None)
    return str(value) if value else None


def _iteration_of(frame: _Frame) -> int | None:
    """Return the iteration number a frame is currently on, if known."""
    if frame.iteration_span is None:
        return None
    value = frame.iteration_span.attributes.get(Attr.ITERATION)
    return int(value) if isinstance(value, int) else None


def _finish_reason(response: Any) -> str | None:
    """Read a finish reason off a response, if the client normalised one.

    ``getattr`` because :class:`~harness.llm.protocol.LLMResponse` gained
    this field in the same release as this module and a test fake may not
    have it. Returned as a single string rather than OTel's list, since one
    response has one reason here; the attribute keeps the plural name the
    conventions use so a viewer recognises it.
    """
    value = getattr(response, "finish_reason", None)
    return str(value) if value else None
