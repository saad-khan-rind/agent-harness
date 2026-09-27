"""What gets measured, and the one place Prometheus is imported.

Section 8.4 asks for metrics collected "through reusable lifecycle hooks or
an equivalent decoupled instrumentation mechanism". This module is the
decoupling. :class:`MetricsRecorder` is a protocol of plain method calls —
``run_started``, ``tool_finished`` — and :class:`PrometheusRecorder` is the
only code in the harness that knows what a ``Counter`` is.

The split earns its keep three times:

* ``harness/memory/`` can report an operation without importing a metrics
  library, which keeps ADR-034's rule that memory reaches the run through
  seams rather than through everybody's imports.
* Tests assert on counters without an HTTP server, a port, or a global
  registry — :class:`PrometheusRecorder` takes its own ``CollectorRegistry``,
  so two tests in one process cannot see each other's numbers.
* A run with metrics switched off pays nothing: :class:`NullRecorder` is the
  same protocol with empty bodies, so the hook is never conditional.

**Metric names are frozen.** The team agreed the first twelve on Monday and
the Grafana dashboard queries them by name, so renaming one silently breaks a
panel somebody else owns. They are defined here, once, and
``tests/unit/test_span_metrics.py`` pins the list (ADR-081).

**``run_id`` is never a label.** Prometheus keeps one time series per label
combination, so a per-run label would create an unbounded number of them and
make the database useless within a day. Per-run detail belongs in the JSONL
trace, which is a different tool for a different question: aggregates here,
one run there.
"""

from typing import Literal, Protocol

from prometheus_client import CollectorRegistry, Counter, Gauge, Histogram

# The kind of thing a tool is, as a label. Coarser than the tool name on
# purpose: `tool` already carries the name, and `kind` is what lets a panel
# ask "how much of this run was MCP?" without listing every server's tools.
ToolKind = Literal["builtin", "mcp", "memory", "skill", "delegate"]

# How a run ended. `no_progress` and `capped` are separated from `error`
# because they are not failures of the model or the tools — they are the loop
# guards doing their job (ADR-047, ADR-049), and a dashboard that files them
# under "errors" would make a working safety net look like a fault.
RunStatus = Literal["success", "error", "no_progress", "capped"]

# What the permission policy decided, as seen from outside. Four values for
# the policy's three outcomes: `require_user_confirmation` splits by what the
# person said (ADR-081).
PermissionOutcomeLabel = Literal["allow", "deny", "confirmed", "declined"]

# Buckets in seconds. The defaults prometheus_client ships top out at 10s,
# which is useless here: a single LLM call against the lab models regularly
# takes longer than that, and every run would land in `+Inf`. These reach
# five minutes, which is the shape of the data we actually have.
_LATENCY_BUCKETS = (0.1, 0.5, 1.0, 2.5, 5.0, 10.0, 30.0, 60.0, 120.0, 300.0)

# Characters of conversation. `max_context_chars` ships at 64000 (ADR-109),
# so the buckets straddle it: the question a reader has is "how close to the
# budget is a typical run, and how often does it cross", and buckets that
# stopped below the cap could not answer either half.
_CONTEXT_BUCKETS = (
    4_000,
    8_000,
    16_000,
    24_000,
    32_000,
    48_000,
    64_000,
    80_000,
    96_000,
    128_000,
)

# Memory operations are a local container on the same network, and
# `memory_qdrant_timeout` ships at 10s. Sub-second buckets are the whole
# story here: anything above one second is already a problem, and the
# latency buckets above would put every healthy call in the first bucket.
_MEMORY_BUCKETS = (0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0)

# Counts of things a run did — iterations, tool calls, records retrieved.
# The top bucket is deliberately above anything healthy: the question these
# answer is "how often does a run go long", and buckets that stopped at a
# typical value could not.
_COUNT_BUCKETS = (1, 2, 3, 5, 8, 12, 20, 30, 50)
_ITEM_BUCKETS = (0, 1, 2, 3, 5, 10, 20, 50)


class MetricsRecorder(Protocol):
    """Somewhere to report what the harness did.

    Deliberately not a thin wrapper over Prometheus types: the methods are
    named after *events in a run*, not after counters. That is what lets the
    implementation change — or do nothing — without the caller knowing.
    """

    def run_started(self, role: str) -> None:
        """A run began, under ``role`` (``main`` or a sub-agent role)."""

    def run_finished(self, role: str, status: RunStatus, seconds: float) -> None:
        """A run ended: how, and how long it took."""

    def llm_call(self, role: str, seconds: float, prompt: int, completion: int) -> None:
        """One model request completed, with its latency and token counts."""

    def tool_finished(
        self, tool: str, kind: ToolKind, ok: bool, seconds: float
    ) -> None:
        """One tool call completed."""

    def permission_decided(self, outcome: PermissionOutcomeLabel) -> None:
        """The policy decided one tool call."""

    def memory_operation(self, op: str) -> None:
        """A long-term memory operation happened (store, search, inject…)."""

    def context_measured(self, role: str, chars: int, compactions: int) -> None:
        """One model request's context window, and any compaction before it."""

    def subagent_finished(self, role: str, status: RunStatus, seconds: float) -> None:
        """A delegated child run ended."""

    # -- added with the span rewrite (ADR-093, ADR-094, ADR-096) -----------

    def llm_failed(self, role: str, error_type: str) -> None:
        """One model request failed instead of returning.

        The metric this harness most conspicuously lacked. ``after_llm``
        fires only on success, so a run killed by a 429 incremented no LLM
        metric at all and showed up purely as a failed *run* — which is the
        one thing §2 of the brief tells us not to conflate.
        """

    def llm_retried(self, error_type: str) -> None:
        """The client retried a request after a transient failure."""

    def error_observed(self, layer: str, error_type: str, component: str) -> None:
        """One failure, attributed to the layer responsible for it.

        The single most useful new metric here, because it is the only one
        that answers the question a person actually starts from: *is this my
        agent, my tools, my policy, or the model?* Everything else tells you
        that something failed; this tells you where to look (ADR-094).
        """

    def mcp_call(self, server: str, ok: bool) -> None:
        """One call to a tool on a named MCP server."""

    def memory_timed(self, op: str, seconds: float, items: int | None) -> None:
        """One memory operation, with how long it took and what it returned.

        Separate from :meth:`memory_operation` because that one is a bare
        counter derived from a tool *name* and therefore never saw the
        retrieval the harness performs on every run without a tool call.
        """

    def run_shape(self, role: str, iterations: int, tool_calls: int) -> None:
        """How much work one finished run took.

        Two histograms rather than two counters: "how many tools does an
        average run invoke" is a distribution question, and a mean computed
        from two counters would hide the run that called forty.
        """

    def repeated_call(self, tool: str) -> None:
        """A tool was called again with byte-identical arguments."""

    def task_outcome(self, outcome: str, reason: str) -> None:
        """A quality verdict was attached to a finished run (ADR-096)."""

    def spans_dropped(self, count: int) -> None:
        """Set how many spans the telemetry layer has lost this session."""


class NullRecorder:
    """A recorder that measures nothing.

    Used when ``metrics_enabled`` is false, and kept even though the span
    rewrite gave the CLI a second way to switch metrics off — not building
    the exporter at all. Both are real: a caller that wants no metrics omits
    the exporter, and a caller that wants the exporter's translation exercised
    without any numbers moving passes this. The tests use the second to prove
    the translation itself never raises.

    Either way the rule from ADR-081 holds: a recorder that checks a flag on
    every call is a recorder that will eventually check it wrong; one that
    always records into empty methods cannot.
    """

    def run_started(self, role: str) -> None:
        """Ignore."""

    def run_finished(self, role: str, status: RunStatus, seconds: float) -> None:
        """Ignore."""

    def llm_call(self, role: str, seconds: float, prompt: int, completion: int) -> None:
        """Ignore."""

    def tool_finished(
        self, tool: str, kind: ToolKind, ok: bool, seconds: float
    ) -> None:
        """Ignore."""

    def permission_decided(self, outcome: PermissionOutcomeLabel) -> None:
        """Ignore."""

    def memory_operation(self, op: str) -> None:
        """Ignore."""

    def context_measured(self, role: str, chars: int, compactions: int) -> None:
        """Ignore."""

    def subagent_finished(self, role: str, status: RunStatus, seconds: float) -> None:
        """Ignore."""

    def llm_failed(self, role: str, error_type: str) -> None:
        """Ignore."""

    def llm_retried(self, error_type: str) -> None:
        """Ignore."""

    def error_observed(self, layer: str, error_type: str, component: str) -> None:
        """Ignore."""

    def mcp_call(self, server: str, ok: bool) -> None:
        """Ignore."""

    def memory_timed(self, op: str, seconds: float, items: int | None) -> None:
        """Ignore."""

    def run_shape(self, role: str, iterations: int, tool_calls: int) -> None:
        """Ignore."""

    def repeated_call(self, tool: str) -> None:
        """Ignore."""

    def task_outcome(self, outcome: str, reason: str) -> None:
        """Ignore."""

    def spans_dropped(self, count: int) -> None:
        """Ignore."""


class PrometheusRecorder:
    """The frozen metrics, on a registry this object owns.

    The registry is a constructor argument rather than the library's global
    ``REGISTRY``. Prometheus refuses to register the same metric name twice,
    so a module-level definition makes a second instance — a second test, a
    sub-agent, a REPL that reloads — raise ``ValueError`` on import. Owning
    the registry turns that from a footgun into a parameter.
    """

    def __init__(self, registry: CollectorRegistry) -> None:
        """Args:
        registry: The collector registry these metrics belong to. The CLI
            passes a process-wide one; every test passes its own.
        """
        self._registry = registry

        # --- throughput and active runs -------------------------------
        self.active_runs = Gauge(
            "harness_active_runs",
            "Agent runs currently executing.",
            ["role"],
            registry=registry,
        )
        self.runs_total = Counter(
            "harness_runs_total",
            "Agent runs completed, by how they ended.",
            ["status", "role"],
            registry=registry,
        )
        self.run_duration = Histogram(
            "harness_run_duration_seconds",
            "Wall-clock duration of one agent run.",
            ["role"],
            buckets=_LATENCY_BUCKETS,
            registry=registry,
        )

        # --- LLM usage ------------------------------------------------
        self.llm_requests = Counter(
            "harness_llm_requests_total",
            "Requests sent to the model endpoint.",
            ["role"],
            registry=registry,
        )
        self.llm_tokens = Counter(
            "harness_llm_tokens_total",
            "Tokens billed, by direction.",
            ["direction"],
            registry=registry,
        )
        self.llm_latency = Histogram(
            "harness_llm_latency_seconds",
            "Time spent waiting for one model response.",
            buckets=_LATENCY_BUCKETS,
            registry=registry,
        )

        # --- tools ----------------------------------------------------
        self.tool_calls = Counter(
            "harness_tool_calls_total",
            "Tool calls dispatched, by tool, outcome and kind.",
            ["tool", "ok", "kind"],
            registry=registry,
        )
        self.tool_latency = Histogram(
            "harness_tool_latency_seconds",
            "Time spent inside one tool call.",
            ["kind"],
            buckets=_LATENCY_BUCKETS,
            registry=registry,
        )

        # --- permissions ----------------------------------------------
        self.permission_decisions = Counter(
            "harness_permission_decisions_total",
            "Permission decisions, by outcome.",
            ["outcome"],
            registry=registry,
        )

        # --- memory ---------------------------------------------------
        self.memory_ops = Counter(
            "harness_memory_ops_total",
            "Long-term memory operations, by kind.",
            ["op"],
            registry=registry,
        )

        # --- context window -------------------------------------------
        # Buckets are characters, not tokens, and deliberately so: InnKube
        # often omits `usage`, so the harness compacts on a character budget
        # (ADR-032) and a histogram in tokens would be measuring something
        # other than the thing that fires. The top bucket sits above the
        # shipped 24k budget, so "how close to the cap do runs get" is
        # answerable rather than everything piling into +Inf.
        self.context_chars = Histogram(
            "harness_context_chars",
            "Size of the conversation sent to the model, in characters.",
            ["role"],
            buckets=_CONTEXT_BUCKETS,
            registry=registry,
        )
        self.context_compactions = Counter(
            "harness_context_compactions_total",
            "Times old tool observations were stubbed to fit the budget.",
            ["role"],
            registry=registry,
        )

        # --- sub-agents -----------------------------------------------
        # Defined here although `delegate` does not exist yet. The dashboard
        # is built against these names this week, and a panel querying a
        # metric that has never been registered shows "No data" rather than
        # an error — so shipping the name early costs nothing and shipping
        # it late costs a rebuild of someone else's work (plan section 6).
        self.subagent_runs = Counter(
            "harness_subagent_runs_total",
            "Delegated sub-agent runs, by role and how they ended.",
            ["role", "status"],
            registry=registry,
        )
        self.subagent_duration = Histogram(
            "harness_subagent_duration_seconds",
            "Wall-clock duration of one delegated sub-agent run.",
            ["role"],
            buckets=_LATENCY_BUCKETS,
            registry=registry,
        )

        # --- added with the span rewrite ------------------------------
        # The fourteen names above stay frozen (ADR-081). These eleven are
        # additions, and each one exists because a question in the brief
        # could not be answered without it — not because the number was
        # available. A dashboard of everything measurable is a dashboard
        # nobody reads.

        # "Do not assume HTTP 200 means the agent succeeded", read the other
        # way: until this existed, a failed request incremented nothing.
        self.llm_errors = Counter(
            "harness_llm_errors_total",
            "Model requests that failed instead of returning, by kind.",
            ["error_type", "role"],
            registry=registry,
        )
        self.llm_retries = Counter(
            "harness_llm_retries_total",
            "Transient model failures the client retried, by kind.",
            ["error_type"],
            registry=registry,
        )

        # The failure-attribution metric. `layer` is the legend on the panel
        # a person opens first; `error_type` is the drill-down; `component`
        # says which span raised it. All three are bounded vocabularies
        # defined in harness/observability/errors.py, which is what makes
        # this safe as a label set at all (ADR-094).
        self.errors = Counter(
            "harness_errors_total",
            "Failures observed anywhere in a run, by responsible layer.",
            ["layer", "error_type", "component"],
            registry=registry,
        )

        # Server identity as a dimension rather than a prefix on a tool
        # name. Bounded by config.yaml, so a label rather than a series
        # explosion.
        self.mcp_calls = Counter(
            "harness_mcp_calls_total",
            "Tool calls dispatched to an MCP server, by server and outcome.",
            ["server", "ok"],
            registry=registry,
        )

        # Memory, measured directly rather than inferred from tool names.
        # The operation the old counter could never see is the one that runs
        # on every single turn: the retrieval the harness performs itself.
        self.memory_duration = Histogram(
            "harness_memory_operation_seconds",
            "Time spent inside one long-term memory operation.",
            ["op"],
            buckets=_MEMORY_BUCKETS,
            registry=registry,
        )
        self.memory_items = Histogram(
            "harness_memory_items_returned",
            "Records returned by one memory retrieval.",
            ["op"],
            buckets=_ITEM_BUCKETS,
            registry=registry,
        )

        # The shape of a run. Histograms because "how many tool calls does
        # an average run make" is a distribution: a mean over two counters
        # hides the one run that made forty, which is the run worth finding.
        self.run_iterations = Histogram(
            "harness_run_iterations",
            "Reason-act cycles used by one finished run.",
            ["role"],
            buckets=_COUNT_BUCKETS,
            registry=registry,
        )
        self.run_tool_calls = Histogram(
            "harness_run_tool_calls",
            "Tool calls dispatched by one finished run.",
            ["role"],
            buckets=_COUNT_BUCKETS,
            registry=registry,
        )

        # Thrash. A rising rate here means models are re-issuing identical
        # calls, which is the early warning for the no-progress guard firing
        # and usually a prompt or tool-description problem.
        self.repeated_calls = Counter(
            "harness_repeated_tool_calls_total",
            "Tool calls repeated with byte-identical arguments.",
            ["tool"],
            registry=registry,
        )

        # Task quality, kept apart from run status on purpose: a run can end
        # `success` and still not have done what was asked (ADR-096).
        self.task_outcomes = Counter(
            "harness_task_outcomes_total",
            "Quality verdicts attached to finished runs.",
            ["outcome", "reason"],
            registry=registry,
        )

        # Self-observability. A telemetry layer that quietly stops recording
        # is worse than one that is visibly off, so the losses are published
        # rather than swallowed.
        self.dropped_spans = Gauge(
            "harness_telemetry_spans_dropped",
            "Spans an exporter refused or lost this session.",
            registry=registry,
        )

    @property
    def registry(self) -> CollectorRegistry:
        """The registry these metrics are registered on."""
        return self._registry

    # -- the protocol ------------------------------------------------------

    def run_started(self, role: str) -> None:
        """Increment the active-run gauge for ``role``."""
        self.active_runs.labels(role=role).inc()

    def run_finished(self, role: str, status: RunStatus, seconds: float) -> None:
        """Record the outcome and duration, and release the gauge."""
        self.active_runs.labels(role=role).dec()
        self.runs_total.labels(status=status, role=role).inc()
        self.run_duration.labels(role=role).observe(seconds)

    def llm_call(self, role: str, seconds: float, prompt: int, completion: int) -> None:
        """Record one model request: its latency and both token counts."""
        self.llm_requests.labels(role=role).inc()
        self.llm_latency.observe(seconds)
        # Guarded because the endpoint may omit `usage` entirely, in which
        # case the harness records nothing rather than inventing a number
        # (ADR-031). A negative would corrupt a counter permanently.
        if prompt > 0:
            self.llm_tokens.labels(direction="prompt").inc(prompt)
        if completion > 0:
            self.llm_tokens.labels(direction="completion").inc(completion)

    def tool_finished(
        self, tool: str, kind: ToolKind, ok: bool, seconds: float
    ) -> None:
        """Record one tool call and how long it took."""
        # "true"/"false" rather than Python's "True"/"False": PromQL is
        # matched as text and lowercase is what every other exporter uses.
        self.tool_calls.labels(tool=tool, ok="true" if ok else "false", kind=kind).inc()
        self.tool_latency.labels(kind=kind).observe(seconds)

    def permission_decided(self, outcome: PermissionOutcomeLabel) -> None:
        """Record one permission decision."""
        self.permission_decisions.labels(outcome=outcome).inc()

    def memory_operation(self, op: str) -> None:
        """Record one long-term memory operation."""
        self.memory_ops.labels(op=op).inc()

    def context_measured(self, role: str, chars: int, compactions: int) -> None:
        """Record the context size, and any compactions since the last call."""
        self.context_chars.labels(role=role).observe(chars)
        if compactions > 0:
            self.context_compactions.labels(role=role).inc(compactions)

    def subagent_finished(self, role: str, status: RunStatus, seconds: float) -> None:
        """Record one delegated child run."""
        self.subagent_runs.labels(role=role, status=status).inc()
        self.subagent_duration.labels(role=role).observe(seconds)

    # -- added with the span rewrite --------------------------------------

    def llm_failed(self, role: str, error_type: str) -> None:
        """Record one model request that failed instead of returning."""
        self.llm_errors.labels(error_type=error_type, role=role).inc()

    def llm_retried(self, error_type: str) -> None:
        """Record one retried model request."""
        self.llm_retries.labels(error_type=error_type).inc()

    def error_observed(self, layer: str, error_type: str, component: str) -> None:
        """Record one failure against the layer responsible for it."""
        self.errors.labels(
            layer=layer, error_type=error_type, component=component
        ).inc()

    def mcp_call(self, server: str, ok: bool) -> None:
        """Record one call dispatched to a named MCP server."""
        # "true"/"false" rather than Python's "True"/"False", matching
        # `tool_calls` above: PromQL matches label values as text and
        # lowercase is what every other exporter uses.
        self.mcp_calls.labels(server=server, ok="true" if ok else "false").inc()

    def memory_timed(self, op: str, seconds: float, items: int | None) -> None:
        """Record one memory operation's latency, and its yield if it had one.

        ``items`` is ``None`` for a write, which has no count to report.
        Observing a zero there would make the "how much does retrieval
        return" histogram read as though half of all retrievals came back
        empty.
        """
        self.memory_duration.labels(op=op).observe(seconds)
        if items is not None:
            self.memory_items.labels(op=op).observe(items)

    def run_shape(self, role: str, iterations: int, tool_calls: int) -> None:
        """Record how much work one finished run took."""
        self.run_iterations.labels(role=role).observe(max(0, iterations))
        self.run_tool_calls.labels(role=role).observe(max(0, tool_calls))

    def repeated_call(self, tool: str) -> None:
        """Record one tool call repeated with identical arguments."""
        self.repeated_calls.labels(tool=tool).inc()

    def task_outcome(self, outcome: str, reason: str) -> None:
        """Record one quality verdict."""
        self.task_outcomes.labels(outcome=outcome, reason=reason).inc()

    def spans_dropped(self, count: int) -> None:
        """Publish how many spans have been lost this session.

        A gauge set to a total rather than a counter incremented per loss:
        the tracer already owns the running count, and a counter here would
        have to be kept in step with it — two numbers that must agree, which
        is one more than necessary.
        """
        self.dropped_spans.set(max(0, count))
