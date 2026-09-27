"""The span model: one instrumentation vocabulary, no dependencies.

Week 3 shipped two observers side by side. :class:`MetricsHook` timed a run
and reported numbers; :class:`JsonlTraceHook` timed the *same* run and wrote
lines. Both kept their own ``time.monotonic()`` stamps, both derived tokens
as a difference off the context, both classified an ending, and neither knew
the other existed. That worked, and it cost us the two things a real
observability layer is for:

* **Structure.** A flat event log cannot say that a ``tool`` event happened
  *inside* iteration 4, or that a ``permission`` line belongs to the tool
  call on the next line rather than the one before. The reader recovered
  some of that by adjacency, which is a guess that breaks the moment a model
  requests two tool calls in one turn (ADR-093).
* **One place to add a field.** Recording an MCP server's identity meant
  editing two hooks, two sets of timers and two schemas.

So the instrumentation point is now a **span**, and everything downstream is
an exporter. One hook opens spans; the metrics exporter turns finished spans
into counters, the JSONL exporter turns them into lines, and the OTLP
exporter — when it is switched on — turns them into something Jaeger can
draw. Adding a telemetry field is one attribute in one place, and every
exporter gets it.

**Deliberately importable by anything.** This module imports nothing outside
the standard library. That is not tidiness: ADR-034 says ``harness/memory/``
reaches a run through seams rather than through everybody's imports, and
``harness/observability/recorder.py`` imports ``prometheus_client`` at module
level. Memory needs to open a ``memory.search`` span; it must not acquire a
metrics library to do it. The rule is that this file stays dependency-free,
and :mod:`harness.observability.exporters` is where third-party code lives.

**Identifiers are OpenTelemetry-shaped.** A trace id is 32 hex characters
and a span id is 16, because that is what the OTLP wire format accepts and
because a bespoke id would have to be rewritten at the boundary anyway. The
trace id of a run is its root ``run_id``, so the id already printed in the
banner, already used to name the trace file, and already stored on the
delegation artifacts is the same id an external trace viewer shows
(ADR-095). We adopt the shape and the ``gen_ai.*`` attribute names; we do
not adopt the SDK.

**A tracer must never break a run.** Every public method here is written so
that a mistake anywhere else — an ``end`` with no matching ``start``, an
exporter that raises, a clock that goes backwards — is recorded as a gap in
the evidence rather than raised into the loop. That rule is older than this
module (ADR-069, ADR-081); what is new is that it is enforced in one place
instead of in each observer.
"""

import hashlib
import os
import threading
import time
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Any, Literal, Protocol

# --- span names -------------------------------------------------------------
# Frozen for the same reason the metric names are (ADR-081): the replay
# reader groups by them, the dashboard's derived panels query them, and a
# rename is a silent break in somebody else's view. Dotted and lowercase,
# which is the OpenTelemetry convention for operation names.
SPAN_AGENT_RUN = "agent.run"
SPAN_ITERATION = "agent.iteration"
SPAN_LLM_CALL = "llm.call"
SPAN_TOOL_CALL = "tool.call"
SPAN_PERMISSION = "permission.check"
SPAN_MEMORY_READ = "memory.read"
SPAN_MEMORY_WRITE = "memory.write"
SPAN_SUBAGENT_RUN = "subagent.run"
SPAN_FINALIZE = "agent.finalize"

# What a span is doing, in OTel's vocabulary. Only the two values we can
# honestly claim: INTERNAL for work inside the process, CLIENT for a call
# that leaves it (the model endpoint, an MCP server, Qdrant). A trace viewer
# uses this to colour and group, and claiming SERVER for work that never
# accepted a request would be a lie in somebody else's UI.
SpanKind = Literal["internal", "client"]

# Whether the operation the span covers succeeded. `unset` is OTel's default
# and means nobody said — which is different from "it worked", and the
# distinction matters: a span that is still open when the process dies
# should not be counted as a success.
SpanStatus = Literal["unset", "ok", "error"]


# --- attribute keys ---------------------------------------------------------
# Every attribute this harness can emit is named here, once. Two families:
#
# `gen_ai.*` and `error.*` come from the OpenTelemetry semantic conventions,
# so a trace viewer that already understands LLM spans renders ours without
# being configured. Using our own names here would have produced a trace
# that only our own reader could interpret, which defeats the point of
# adopting the shape at all.
#
# `harness.*` is everything the conventions have no name for: a permission
# decision, a delegation depth, an iteration number. Prefixed so it is
# obvious at a glance which half of the schema a field belongs to and which
# half we are free to change (ADR-094).


class Attr:
    """The attribute vocabulary, as constants rather than string literals.

    A class rather than module-level names purely for namespacing at the call
    site: ``Attr.TOOL_NAME`` reads as a vocabulary lookup, and a typo is an
    ``AttributeError`` at import rather than a silently-empty dashboard
    filter six hours later.
    """

    # -- identity ------------------------------------------------------
    RUN_ID = "harness.run_id"
    PARENT_RUN_ID = "harness.parent_run_id"
    ROLE = "harness.role"
    DEPTH = "harness.depth"
    ITERATION = "harness.iteration"
    COMPONENT = "harness.component"

    # -- model (OTel gen_ai semantic conventions) ----------------------
    GEN_AI_SYSTEM = "gen_ai.system"
    GEN_AI_OPERATION = "gen_ai.operation.name"
    GEN_AI_REQUEST_MODEL = "gen_ai.request.model"
    GEN_AI_REQUEST_TEMPERATURE = "gen_ai.request.temperature"
    GEN_AI_RESPONSE_FINISH_REASON = "gen_ai.response.finish_reasons"
    GEN_AI_USAGE_INPUT_TOKENS = "gen_ai.usage.input_tokens"
    GEN_AI_USAGE_OUTPUT_TOKENS = "gen_ai.usage.output_tokens"

    # Our own, because the conventions have no word for them.
    LLM_TOOL_CALLS = "harness.llm.tool_calls"
    LLM_RESULTED_IN_ANSWER = "harness.llm.final_answer"
    LLM_RETRIES = "harness.llm.retries"
    # Why the harness asked, on a model call it made outside the loop —
    # ``memory.extract``, ``memory.consolidate``, ``memory.summarise``.
    # Absent on the agent's own calls (ADR-107).
    LLM_PURPOSE = "harness.llm.purpose"
    CONTEXT_CHARS = "harness.context.chars"
    CONTEXT_COMPACTIONS = "harness.context.compactions"

    # -- tools ---------------------------------------------------------
    TOOL_NAME = "harness.tool.name"
    TOOL_KIND = "harness.tool.kind"
    TOOL_RISK = "harness.tool.risk"
    TOOL_ARGS_DIGEST = "harness.tool.args"
    TOOL_ARGS_FINGERPRINT = "harness.tool.args_fingerprint"
    TOOL_RESULT_CHARS = "harness.tool.result_chars"
    TOOL_RETRIES = "harness.tool.retries"
    TOOL_FAILURE_STREAK = "harness.tool.failure_streak"

    # -- MCP -----------------------------------------------------------
    MCP_SERVER = "harness.mcp.server"
    MCP_REMOTE_TOOL = "harness.mcp.tool"
    MCP_TRANSPORT = "harness.mcp.transport"

    # -- permissions ---------------------------------------------------
    PERMISSION_DECISION = "harness.permission.decision"
    PERMISSION_POLICY = "harness.permission.policy"
    PERMISSION_MODE = "harness.permission.mode"
    PERMISSION_CONFIRMED = "harness.permission.user_confirmed"

    # -- memory --------------------------------------------------------
    MEMORY_OPERATION = "harness.memory.operation"
    MEMORY_ITEMS = "harness.memory.items"
    MEMORY_BACKEND = "harness.memory.backend"
    MEMORY_QUERY_CHARS = "harness.memory.query_chars"

    # -- sub-agents ----------------------------------------------------
    SUBAGENT_ROLE = "harness.subagent.role"
    SUBAGENT_MAX_ITERATIONS = "harness.subagent.max_iterations"
    SUBAGENT_MAX_TOOL_CALLS = "harness.subagent.max_tool_calls"
    SUBAGENT_RESULT_STATUS = "harness.subagent.result_status"
    SUBAGENT_REPAIRS = "harness.subagent.repair_attempts"

    # -- outcome -------------------------------------------------------
    EXIT_REASON = "harness.exit_reason"
    ERROR_TYPE = "error.type"
    ERROR_MESSAGE = "harness.error.message"

    # -- quality (attached after the fact; see evals.py) ----------------
    TASK_SUCCESS = "harness.task.success"
    TASK_FAILURE_REASON = "harness.task.failure_reason"


@dataclass
class Span:
    """One timed operation, with a parent and some attributes.

    The only mutable field that matters is :attr:`attributes`: a span is
    opened when an operation starts, before most of what is interesting
    about it is known, and filled in as it goes. Everything else is set once.

    Attributes:
        trace_id: The whole execution this span belongs to — the top-level
            run's id, shared by every child run underneath it. 32 hex.
        span_id: This span's own id. 16 hex.
        parent_span_id: The span this one happened inside, or ``None`` for
            the root ``agent.run``.
        name: One of the ``SPAN_*`` constants.
        kind: ``internal`` or ``client``.
        start_ns: Monotonic nanoseconds at open. Monotonic because it is
            used for arithmetic; :attr:`start_wall_ns` is what a reader
            sees.
        start_wall_ns: Unix nanoseconds at open, for display and for OTLP,
            which wants wall-clock.
        end_ns: Monotonic nanoseconds at close, or ``None`` while open.
        status: ``unset`` until somebody says, then ``ok`` or ``error``.
        attributes: The structured detail. Keys come from :class:`Attr`.
        events: Timestamped notes inside the span — a retry, a guard firing.
            Kept separate from attributes because they can occur more than
            once and an attribute cannot.
    """

    trace_id: str
    span_id: str
    parent_span_id: str | None
    name: str
    kind: SpanKind = "internal"
    start_ns: int = field(default_factory=time.monotonic_ns)
    start_wall_ns: int = field(default_factory=time.time_ns)
    end_ns: int | None = None
    status: SpanStatus = "unset"
    attributes: dict[str, Any] = field(default_factory=dict)
    events: list[tuple[int, str, dict[str, Any]]] = field(default_factory=list)

    @property
    def duration_ns(self) -> int:
        """Nanoseconds between open and close, or ``0`` while still open.

        Zero rather than ``None`` so every arithmetic consumer — a histogram
        observation, a percentage of run time — can use it without a guard.
        A span that is still open has no duration to report, and reporting
        one would be worse than reporting none.
        """
        if self.end_ns is None:
            return 0
        return max(0, self.end_ns - self.start_ns)

    @property
    def duration_ms(self) -> int:
        """Whole milliseconds, which is the resolution a reader wants."""
        return self.duration_ns // 1_000_000

    @property
    def duration_seconds(self) -> float:
        """Fractional seconds, which is what a Prometheus histogram wants."""
        return self.duration_ns / 1_000_000_000

    @property
    def ok(self) -> bool:
        """Whether this span is explicitly not an error.

        ``unset`` counts as not-an-error here, because most spans never have
        anything go wrong and forcing every call site to say so would mean
        the one that forgets looks like a failure.
        """
        return self.status != "error"

    @property
    def error_type(self) -> str | None:
        """The taxonomy entry for how this span failed, if it did."""
        value = self.attributes.get(Attr.ERROR_TYPE)
        return str(value) if value else None

    def set(self, key: str, value: Any) -> "Span":
        """Record one attribute, ignoring ``None``.

        ``None`` is dropped rather than stored so a caller can write
        ``span.set(Attr.MCP_SERVER, server_of(name))`` without first checking
        whether the tool was an MCP tool at all. An absent key means "does
        not apply", which is exactly what a ``None`` there would have meant.

        Args:
            key: An :class:`Attr` constant.
            value: Anything JSON-serialisable, or ``None`` to skip.

        Returns:
            ``self``, so attributes can be chained at a call site.
        """
        if value is not None:
            self.attributes[key] = value
        return self

    def update(self, values: dict[str, Any]) -> "Span":
        """Record several attributes, ignoring the ``None`` ones."""
        for key, value in values.items():
            self.set(key, value)
        return self

    def add_event(self, name: str, **fields: Any) -> "Span":
        """Note something that happened *during* this span.

        The case this exists for is a retry: one ``llm.call`` span may cover
        three HTTP attempts, and an attribute called ``retries`` tells you
        the count but not when they happened or why. An event carries both
        and can repeat.

        Args:
            name: Short event name, e.g. ``retry`` or ``guard.tripped``.
            **fields: Structured detail. Subject to the same redaction rules
                as attributes — this is not a place for prompt text.
        """
        self.events.append((time.time_ns(), name, dict(fields)))
        return self

    def fail(self, error_type: str, message: str | None = None) -> "Span":
        """Mark this span as failed, with a taxonomy entry.

        ``error_type`` is deliberately not free text: it is one of the
        bounded values in :mod:`harness.observability.errors`, because it
        becomes a Prometheus label and an unbounded label is how a metrics
        database is destroyed (ADR-081). The free text goes in
        ``harness.error.message``, which no metric reads.

        Args:
            error_type: A value from
                :data:`~harness.observability.errors.ERROR_TYPES`.
            message: Short human-readable detail. Truncated — see
                :func:`~harness.observability.redaction.error_message`.
        """
        self.status = "error"
        self.set(Attr.ERROR_TYPE, error_type)
        self.set(Attr.ERROR_MESSAGE, message)
        return self

    def succeed(self) -> "Span":
        """Mark this span as explicitly successful."""
        self.status = "ok"
        return self


class SpanExporter(Protocol):
    """Somewhere finished spans go.

    Three ship with the harness — JSONL, Prometheus and OTLP — and they have
    nothing in common except these methods, which is the point. An exporter
    that is not installed costs nothing, and one that fails is dropped
    rather than raised (see :meth:`Tracer._emit`).
    """

    def export(self, span: Span) -> None:
        """Handle one finished span. Must not raise; must not block long."""

    def shutdown(self) -> None:
        """Flush and release anything held open. Must be safe to call twice."""


class StartAwareExporter(Protocol):
    """An exporter that also wants to hear when a span *opens*.

    Almost nothing needs this, and the one thing that does needs it badly:
    ``harness_active_runs`` is a gauge, so it has to be incremented while a
    run is in flight rather than after it finishes. An exporter that only
    saw finished spans could never make that number anything but zero.

    Optional, and detected by ``hasattr`` rather than by registration, so
    the two exporters that do not care do not have to implement an empty
    method to say so.
    """

    def on_start(self, span: Span) -> None:
        """Handle one span that has just opened. Must not raise."""


# Attributes a child span inherits from its parent unless it sets its own.
# Only *identity*: which run this is, whose child it is, what role it is
# running under. Never timings, never outcomes, never anything a child could
# be wrong about.
#
# The reason is a person with a terminal. A run id is the one string that
# joins the banner, the trace filename, the delegation artifacts and a
# Jaeger URL, so ``grep <run_id> traces/*.jsonl`` should return the whole
# run — and before this, it returned only the spans that happened to be
# opened with the id in hand. A memory span or a permission span carried
# ``run_id: null`` and fell out of the one search a person actually performs
# (ADR-095).
_INHERITED = (
    "harness.run_id",
    "harness.parent_run_id",
    "harness.role",
    "harness.depth",
)


def _inherit(parent: Span, child: Span) -> None:
    """Copy the identity attributes a child has not set for itself.

    Args:
        parent: The enclosing span.
        child: The span just opened.
    """
    for key in _INHERITED:
        if key not in child.attributes and key in parent.attributes:
            child.attributes[key] = parent.attributes[key]


def new_trace_id() -> str:
    """Return a fresh 32-hex-character trace id.

    ``os.urandom`` rather than ``uuid4().hex`` because that is what the id
    is: 16 random bytes, in the shape OTLP accepts. They are the same length
    and the same entropy; this spelling just does not imply a UUID structure
    that the wire format does not have.
    """
    return os.urandom(16).hex()


def new_span_id() -> str:
    """Return a fresh 16-hex-character span id."""
    return os.urandom(8).hex()


def as_trace_id(run_id: str) -> str:
    """Turn a run id into a trace id of the right width.

    The harness has minted run ids as ``uuid4().hex`` since Week 1 — 32 hex
    characters, which is already exactly an OTLP trace id. So the common case
    is the identity function, and that is *why* the ids line up: the id in
    the banner, the id in the trace filename and the id in a Jaeger URL are
    one string, and nobody has to translate between them while debugging
    (ADR-095).

    Anything else — a short id from a test, an id with a prefix — is padded
    or truncated deterministically rather than replaced, so the same run id
    always yields the same trace id.

    Args:
        run_id: The run's identifier, whatever shape it arrived in.

    Returns:
        32 lowercase hex characters.
    """
    cleaned = "".join(char for char in run_id.lower() if char in "0123456789abcdef")
    if len(cleaned) == 32:
        return cleaned
    # Anything else is hashed rather than padded or truncated. Padding is
    # the tempting option and it is wrong twice over: zero-padding collides
    # ``abc`` with ``abc0``, and truncating collides any two ids sharing a
    # prefix — and two runs sharing a trace id is the single failure this
    # function exists to prevent.
    #
    # Hashing is deterministic, so the same run id always yields the same
    # trace id and a person can re-derive it; it is total, so an id with no
    # hex characters at all still works; and it cannot collide by accident.
    # The common case never reaches here, because a run id is already
    # exactly 32 hex characters (ADR-095).
    return hashlib.sha256(run_id.encode("utf-8")).hexdigest()[:32]


class _SpanStack(threading.local):
    """One open-span stack per thread.

    Subclassing :class:`threading.local` rather than using a bare instance so
    that ``__init__`` runs once in each thread that touches it, which is the
    documented way to give every thread its own empty list instead of an
    ``AttributeError`` on first access.
    """

    def __init__(self) -> None:
        self.spans: list[Span] = []
        # How many spans at the bottom of this thread's stack belong to
        # somebody else. A worker adopts its parent's open span so its own
        # spans nest correctly, but it must never *close* it: the parent is
        # still running, and closing its `tool.call` from a child thread
        # would end the delegation in the trace while it is still going.
        self.floor = 0


class Tracer:
    """Opens spans, closes them, and hands the finished ones to exporters.

    One instance per session, shared by the parent agent and every sub-agent
    it delegates to. Sharing is not an optimisation — it is what makes a
    child's ``llm.call`` a descendant of the parent's ``tool.call`` instead
    of a root span in a file of its own (ADR-093).

    **The stack is the parentage, and it is per thread.** Runs nest, so a
    stack expresses the tree exactly and popping restores the parent's
    context without the parent having to remember anything. Delegation used
    to make that nesting *synchronous* as well, and one shared stack was
    enough. Now a batched ``delegate`` runs its children on worker threads,
    and a single stack would hand child B's ``llm.call`` whichever span
    happened to be on top — which is to say, child C's. Each thread
    therefore keeps its own stack, and a worker adopts its parent's open
    span through :meth:`branch` so the tree still joins up across the
    boundary (ADR-106).

    **Nothing here raises.** An unmatched close, an exporter that throws, a
    span ended twice: each is absorbed. The rule the whole layer is built on
    is that an observer must not be able to fail a task (ADR-069, ADR-081),
    and centralising it here means each exporter does not have to be careful
    on its own.
    """

    def __init__(self, exporters: Sequence[SpanExporter] = ()) -> None:
        """Args:
        exporters: Where finished spans go, in order. An empty sequence is
            a working tracer that measures and discards — which is what
            ``metrics_enabled: false`` with ``traces_enabled: false``
            produces, and it costs one list append per span.
        """
        self._exporters = list(exporters)
        self._local = _SpanStack()
        self._dropped = 0
        # Guards the exporter fan-out and the dropped counter, both of which
        # are genuinely shared. The stacks above are not: they are the one
        # piece of per-thread state, and locking them would be both useless
        # and a deadlock waiting for an exporter that traces.
        self._export_lock = threading.Lock()

    @property
    def _stack(self) -> list[Span]:
        """This thread's open spans, innermost last."""
        return self._local.spans

    @contextmanager
    def branch(self, parent: Span | None) -> Iterator[None]:
        """Run a block on this thread as a child of ``parent``.

        The seam that keeps a delegated child's spans attached to the run
        that delegated them. A worker thread starts with an empty stack, so
        without this every child span would be a root and the execution tree
        the assignment asks us to record would be a row of unrelated runs.

        The previous stack is restored on exit, because a pool reuses its
        threads and the next task must not inherit this one's parentage.

        Args:
            parent: The span to hang this thread's spans beneath, normally
                the parent's open ``tool.call``. ``None`` is legal and means
                "start from nothing", which is what an untraced session does.
        """
        previous = self._local.spans
        previous_floor = self._local.floor
        self._local.spans = [parent] if parent is not None else []
        self._local.floor = len(self._local.spans)
        try:
            yield
        finally:
            self._local.spans = previous
            self._local.floor = previous_floor

    @property
    def dropped_spans(self) -> int:
        """How many spans an exporter refused or lost this session.

        Surfaced because the alternative to counting is silence, and a
        telemetry layer that quietly stops recording is worse than one that
        is visibly off. The CLI shows it in ``/metrics``-adjacent output and
        :mod:`harness.observability.exporters.metrics` publishes it.
        """
        return self._dropped

    @property
    def current(self) -> Span | None:
        """The innermost open span, or ``None`` outside any run."""
        return self._stack[-1] if self._stack else None

    def find_open(self, name: str) -> Span | None:
        """Return the innermost open span with ``name``, if any.

        Used where a call site knows *what* it wants to annotate but not how
        deep it is — the loop's iteration span while a tool call is open, for
        instance. Searching inward-out means the nearest enclosing one wins,
        which is the one a caller means.

        Args:
            name: A ``SPAN_*`` constant.
        """
        for span in reversed(self._stack):
            if span.name == name:
                return span
        return None

    def start(
        self,
        name: str,
        *,
        kind: SpanKind = "internal",
        trace_id: str | None = None,
        attributes: dict[str, Any] | None = None,
    ) -> Span:
        """Open a span as a child of whatever is currently open.

        Args:
            name: A ``SPAN_*`` constant.
            kind: ``internal`` or ``client``.
            trace_id: Forced trace id, used only by the root span of a run
                so the trace id equals the run id. Children inherit.
            attributes: Initial attributes; ``None`` values are dropped.

        Returns:
            The open span. The caller is responsible for ending it, which in
            practice means using :meth:`span` instead of this.
        """
        parent = self.current
        span = Span(
            trace_id=trace_id or (parent.trace_id if parent else new_trace_id()),
            span_id=new_span_id(),
            parent_span_id=parent.span_id if parent else None,
            name=name,
            kind=kind,
        )
        if attributes:
            span.update(attributes)
        # After the span's own attributes, so "unless it sets its own" is
        # true rather than aspirational: inheriting first would copy the
        # parent's values into keys the caller is about to overwrite, which
        # works by luck and stops working the moment a caller passes `None`
        # for one of them.
        if parent is not None:
            _inherit(parent, span)
        self._stack.append(span)
        self._emit_start(span)
        return span

    def end(self, span: Span | None = None) -> Span | None:
        """Close a span and export it.

        Args:
            span: The span to close. ``None`` closes the innermost one,
                which is the normal case. Passing one explicitly is checked:
                closing a span that is not on top means something above it
                was never closed, so the strays are closed first rather than
                leaking. That turns a composition mistake into slightly odd
                durations instead of a stack that never empties and a
                process that slowly grows.

        Returns:
            The span that was closed, or ``None`` if there was nothing open.
        """
        if len(self._stack) <= self._local.floor:
            return None
        if span is not None and self._stack[-1] is not span:
            if span not in self._stack[self._local.floor :]:
                # Already closed, never ours, or an adopted parent that this
                # thread is not allowed to close. Nothing safe to do.
                return None
            while len(self._stack) > self._local.floor and self._stack[-1] is not span:
                self._close_top()
        return self._close_top()

    def _close_top(self) -> Span | None:
        """Pop the innermost span, stamp it, and emit it.

        Refuses to pop below this thread's floor, so a worker cannot close
        the parent span it merely adopted. Returning ``None`` rather than
        raising keeps the "an observer never breaks a run" rule.
        """
        if len(self._stack) <= self._local.floor:
            return None
        span = self._stack.pop()
        if span.end_ns is None:
            span.end_ns = time.monotonic_ns()
        if span.status == "unset":
            span.status = "ok"
        self._emit(span)
        return span

    @contextmanager
    def span(
        self,
        name: str,
        *,
        kind: SpanKind = "internal",
        trace_id: str | None = None,
        attributes: dict[str, Any] | None = None,
    ) -> Iterator[Span]:
        """Open a span for the duration of a ``with`` block.

        The block's exception, if any, is classified and recorded on the
        span before being re-raised. Re-raised, not swallowed: the tracer
        must not change what the program does, and a caller that wanted the
        exception handled would have handled it.
        """
        started = self.start(name, kind=kind, trace_id=trace_id, attributes=attributes)
        try:
            yield started
        except BaseException as exc:  # noqa: BLE001 - classified, then re-raised
            # Imported here rather than at module scope: `errors` needs the
            # `Attr` vocabulary from this module, and a module-level import
            # in both directions is a cycle.
            from harness.observability.errors import classify_exception, safe_message

            if started.status != "error":
                started.fail(classify_exception(exc), safe_message(exc))
            raise
        finally:
            self.end(started)

    def unwind(self) -> None:
        """Close every open span.

        Called when a session ends with spans still on the stack, which is
        what a ``KeyboardInterrupt`` mid-run looks like. Closing them means
        the trace file ends with a complete record of a run that was
        interrupted, rather than a file whose last span never closed and
        whose reader cannot tell interruption from a crash.

        Only this thread's own spans: an adopted parent belongs to the thread
        that opened it and is left alone.
        """
        while len(self._stack) > self._local.floor:
            self._close_top()

    def shutdown(self) -> None:
        """Close open spans, then flush and release every exporter."""
        self.unwind()
        for exporter in self._exporters:
            try:
                exporter.shutdown()
            except Exception:  # noqa: BLE001 - shutdown must not raise
                self._dropped += 1

    def _emit_start(self, span: Span) -> None:
        """Tell the exporters that care that a span has opened.

        Only :class:`StartAwareExporter` implementations hear this, and only
        one of the three shipped exporters is one. Same swallow rule as
        :meth:`_emit`: an exporter's mistake costs a gauge, not a run.
        """
        with self._export_lock:
            for exporter in self._exporters:
                callback = getattr(exporter, "on_start", None)
                if callback is None:
                    continue
                try:
                    callback(span)
                except Exception:  # noqa: BLE001 - see _emit
                    self._dropped += 1

    def _emit(self, span: Span) -> None:
        """Hand one finished span to every exporter, surviving all of them.

        A broad ``except`` on purpose, and it is the one place in the
        harness where that is right. An exporter is third-party code (OTLP),
        or code touching a disk that may be full (JSONL), or arithmetic on a
        label that may be new (Prometheus). Any of those failing is a reason
        to lose a span, and none of them is a reason to fail the user's
        task. The loss is counted so it is visible rather than silent.

        Serialised, because concurrent children finish spans concurrently
        and the exporters underneath are not all safe for that: the JSONL
        one appends to a shared file, and a line interleaved with another
        line is a corrupt record rather than a lost one. The lock is held
        only for the fan-out, never across a span's own work.
        """
        with self._export_lock:
            for exporter in self._exporters:
                try:
                    exporter.export(span)
                except Exception:  # noqa: BLE001 - see docstring
                    self._dropped += 1
