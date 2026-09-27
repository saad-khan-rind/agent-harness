"""Assembling one harness session: the model client, memory, tools, hooks,
policy and sub-agent factory, wired into one :class:`AgentLoop`.

Split out of ``cli.py`` so that "what a session is made of" can be read
without the REPL around it. :class:`HarnessSession` builds everything and
owns its shutdown; :class:`harness.cli.Repl` extends it with the part that
talks to a person — the prompt, the slash commands and the printed answer.
"""

import os
import sys

try:
    # Importing this is what makes input() support arrow-key history and
    # in-line editing at all — without it, pressing Up at the "You:" prompt
    # prints a raw escape sequence instead of recalling the previous line,
    # which is the single biggest reason a plain input()-based REPL feels
    # broken to use. Not every platform ships GNU readline (Windows CPython
    # does not), so this degrades to "no history, still works" rather than
    # failing the whole harness over a terminal convenience.
    import readline
except ImportError:  # pragma: no cover - platform without GNU readline
    readline = None  # type: ignore[assignment]

from prometheus_client import CollectorRegistry

from harness import console
from harness.budget import (
    BudgetedLLMClient,
    BudgetLedger,
)
from harness.config import Settings
from harness.console import Console, ConsoleConfirmer, ConsoleTraceHook
from harness.context import RunContext
from harness.hooks import CompositeHook, Hook
from harness.llm import OpenAICompatibleClient
from harness.llm.grounding import GroundingLLMClient
from harness.llm.protocol import LLMClient
from harness.loop import AgentLoop
from harness.mcp import McpDiscovery, connect_mcp_servers
from harness.memory import (
    MemoryHook,
    MemoryStore,
    QdrantMemoryStore,
    SessionLog,
)
from harness.memory.consolidate import MemoryWriter
from harness.memory.conversation import ConversationSummaryHook
from harness.memory.embed import HashingEmbedder, build_embedder
from harness.memory.extract import MemoryExtractionHook
from harness.memory.notes import NotesHook
from harness.memory.scratchpad import Scratchpad
from harness.observability import (
    JsonlSpanExporter,
    MetricsRecorder,
    MetricsSpanExporter,
    NullRecorder,
    PrometheusRecorder,
    SpanExporter,
    TelemetryHook,
    Tracer,
    build_otlp_exporter,
    describe_mode,
    serve_metrics,
    stop_metrics,
)
from harness.permissions import (
    PermissionMode,
    PermissionPolicy,
    RiskBasedPolicy,
    build_policy,
    open_world_outcome_for,
    resolve_mode,
)
from harness.prompts import (
    AGENT_SYSTEM_PROMPT,
)
from harness.skills import (
    ActiveSkills,
    SkillLibrary,
    SkillScopedPolicy,
    SkillsHook,
    load_skills,
)
from harness.subagents import (
    ConfigurableFactory,
    DelegateTool,
    ExecutionBudget,
    build_catalog,
)
from harness.subagents.factory import ARTIFACT_DIRNAME
from harness.subagents.planner import DelegationPlanner
from harness.tools import ToolRegistry
from harness.toolset import build_agent_registry


def _build_memory_store(settings: Settings, llm: LLMClient) -> QdrantMemoryStore:
    """Assemble long-term memory from settings.

    Two decisions are made here and nowhere else, which is why this is a
    function rather than a constructor call inlined into the session:

    * **Which embedder.** ``build_embedder`` probes the configured endpoint
      once and falls back to the local lexical embedder when it does not
      serve embeddings (ADR-066). The probe is skipped entirely when
      ``memory_remote_embeddings`` is false, which is how a demo with no
      network avoids paying a connect timeout at startup.
    * **That neither failure is fatal.** Nothing here connects to Qdrant —
      the store dials lazily on first use — so a database that is still
      booting does not delay the banner, and one that never arrives
      produces a session with no memory rather than no session.

    Args:
        settings: Validated settings.
        llm: The chat client, borrowed for its SDK handle so embeddings
            reuse the same endpoint, credential and connection pool. Read
            with ``getattr`` because ``sdk`` is not part of the
            :class:`~harness.llm.protocol.LLMClient` protocol — only
            :class:`~harness.llm.openai_compatible.OpenAICompatibleClient` offers it,
            and a
            client that does not is a client with no embeddings endpoint,
            which is a supported configuration rather than an error.

    Returns:
        A store, connected or not.
    """
    sdk = getattr(llm, "sdk", None) if settings.memory_remote_embeddings else None
    embedder = build_embedder(
        client=sdk,
        model=settings.memory_embedding_model,
        local_model=settings.memory_local_embedding_model,
        local_cache_dir=settings.memory_local_embedding_cache,
    )
    # Said out loud, once, at startup. This subsystem's two failure modes —
    # no database, and no embedding model — are both silent, and the second
    # one cost a user an afternoon: the endpoint served chat but not
    # embeddings, the probe failed, retrieval quietly became word overlap,
    # and the only place that said so was a ``/memory`` command nobody had
    # a reason to run. A line here is cheaper than that afternoon.
    if isinstance(embedder, HashingEmbedder):
        print(
            f"  memory: no embedding model available, using {embedder.name}. "
            f"Retrieval matches words, not meaning — install "
            f"fastembed, or set memory_embedding_model to an "
            f"endpoint that serves /embeddings."
        )
    else:
        print(f"  memory: embedding with {embedder.name}")
    return QdrantMemoryStore(
        embedder=embedder,
        session_log=SessionLog(settings.memory_path),
        url=settings.memory_qdrant_url,
        timeout=settings.memory_qdrant_timeout,
        min_similarity=settings.memory_min_similarity,
    )


def migration_note(store: object) -> str:
    """Return what a memory collection rebuild did, or an empty string.

    ``getattr`` because the note is a property of the Qdrant store, not of
    the :class:`~harness.memory.protocol.MemoryStore` seam: a store with no
    collection to rebuild has nothing to report (ADR-110).
    """
    return str(getattr(store, "migration_note", "") or "")


def _otlp_headers_from_env() -> dict[str, str]:
    """Read OTLP collector headers from the environment.

    From the environment and never from ``config.yaml``, because a hosted
    collector's header is an API key and that file is committed (brief 8.2)
    — the same rule ``LLM_API_KEY`` follows. The variable name and its
    ``key=value,key=value`` format are OpenTelemetry's own, so a person who
    has configured any other OTel process already knows it.

    Returns:
        Parsed headers, empty when the variable is unset or unparseable. A
        malformed value is ignored rather than raised: a collector that
        rejects the request is a line in the banner, and a session that
        refuses to start over a header is not.
    """
    raw = os.environ.get("OTEL_EXPORTER_OTLP_HEADERS", "").strip()
    if not raw:
        return {}
    headers: dict[str, str] = {}
    for pair in raw.split(","):
        key, separator, value = pair.partition("=")
        if separator and key.strip():
            headers[key.strip()] = value.strip()
    return headers


def _build_recorder(settings: Settings) -> tuple[MetricsRecorder, str | None]:
    """Build the metrics recorder and start its endpoint.

    Two things happen together because they fail together: if the exporter
    cannot bind its port there is nothing to scrape, and a recorder counting
    into a registry nobody reads is worse than no recorder — it looks like
    observability and is not.

    A registry is created here rather than using ``prometheus_client``'s
    global ``REGISTRY``: the global one raises on a duplicate metric name,
    which turns a second :class:`Repl` in one process — a test, a future
    sub-agent session — into an import-time crash (ADR-081).

    Args:
        settings: Validated settings. ``metrics_enabled`` decides whether
            anything is measured; ``metrics_port`` where it is served.

    Returns:
        The recorder, and a one-line note for the banner (``None`` when
        metrics are switched off, so the banner prints "disabled").
    """
    if not settings.metrics_enabled:
        return NullRecorder(), None
    registry = CollectorRegistry()
    recorder = PrometheusRecorder(registry)
    problem = serve_metrics(settings.metrics_port, registry)
    if problem is not None:
        # The port is taken, or the container forbids it. The run continues
        # unmeasured rather than refusing to start: an agent that will not
        # answer a question because a metrics port is busy has its
        # priorities backwards.
        return recorder, problem
    return recorder, f"http://localhost:{settings.metrics_port}/metrics"


class HarnessSession:
    """Everything one agent session needs, built once and closed once.

    Owns the agent loop and one :class:`RunContext` that survives across
    turns, which is what makes the REPL a multi-turn conversation rather
    than a series of unrelated one-shot requests (ADR-011).
    """

    def __init__(self, settings: Settings, trace: bool) -> None:
        """Build the agent loop and the session context.

        Args:
            settings: Validated settings.
            trace: Whether to print the per-tool trace.
        """
        self._settings = settings
        self._trace = trace
        # ``main`` has already resolved ``auto``; resolving again is a no-op
        # for a named mode and is what gives a caller that built ``Repl``
        # directly — a test, another front end — the same answer. First,
        # because the telemetry hook records the mode on every span.
        self._mode: PermissionMode = resolve_mode(
            settings.permission_mode, console.is_interactive(sys.stdin)
        )
        # Before anything else that could be slow: the exporter should be
        # answering scrapes while Qdrant and the MCP servers are still
        # starting, so a stall during startup is visible rather than silent.
        self._recorder, self._metrics_note = _build_recorder(settings)
        # The tracer and its exporters, built before the client so the
        # client can be given somewhere to report its retries. Those happen
        # below the loop, so without this wiring three rate-limited attempts
        # and one slow response are the same forty seconds on the same span
        # (ADR-094).
        self._trace_exporter: JsonlSpanExporter | None = None
        # The trace viewer's base URL when OTLP export is on, else None. Set
        # by `_build_telemetry`; read after every answer to print a link.
        self._trace_viewer: str | None = None
        self._telemetry, self._telemetry_notes = self._build_telemetry(settings)
        chat_client = OpenAICompatibleClient(
            settings, on_retry=self._telemetry.record_llm_retry
        )

        # Long-term memory and skills are optional by configuration, and
        # "off" must mean the model is offered no such tools at all rather
        # than tools that fail when called.
        #
        # Memory takes the *unwrapped* client: it borrows the SDK handle for
        # embeddings, which are not chat calls and are not what the daily
        # budget meters.
        self._store: MemoryStore | None = (
            _build_memory_store(settings, chat_client)
            if settings.memory_enabled
            else None
        )

        # The budget wraps the client here, once, before the loop and the
        # sub-agent factory are built — so every chat call in the session,
        # a child's included, is metered by the one instance. This is the
        # whole of the enforcement; nothing downstream knows (ADR-088).
        llm: LLMClient = chat_client
        self._budget: BudgetedLLMClient | None = None
        if settings.budget_enabled:
            ledger = BudgetLedger(settings.memory_path, settings.timezone)
            if ledger.warning is not None:
                # Printed here rather than in the banner for the same reason
                # the MCP warnings are: a one-shot run prints no banner.
                print(f"Warning: {ledger.warning}")
            self._budget = BudgetedLLMClient(
                chat_client,
                ledger,
                settings.budget_daily_tokens,
                settings.budget_warn_fraction,
                settings.budget_prompt_tokens,
            )
            llm = self._budget
        # The chat client the loop uses: budgeted when the budget is on.
        # Kept so the harness's own model calls — extraction, the
        # consolidation judge, conversation summaries — are metered by the
        # same budget and traced by the same tracer as the agent's (ADR-107).
        self._llm = llm

        # Every long-term write goes through this: the tool, /remember and
        # extraction alike, so a fact is consolidated against what is stored
        # however it arrived (ADR-107).
        self._memory_writer: MemoryWriter | None = (
            MemoryWriter(
                self._store,
                self._telemetry.traced_llm(llm, "memory.consolidate")
                if settings.memory_consolidation_enabled
                else None,
            )
            if self._store is not None
            else None
        )
        self._skills: SkillLibrary | None = (
            load_skills(settings.skills_path) if settings.skills_enabled else None
        )

        # Servers are started before the registry is built, because their
        # tools have to be present when the schemas are first assembled.
        # A server that will not start costs its own tools and nothing
        # else; the warnings say which, and the session continues.
        self._mcp = (
            connect_mcp_servers(
                settings.mcp_servers,
                settings.max_tool_output_chars,
                settings.mcp_connect_timeout,
            )
            if settings.mcp_enabled
            else McpDiscovery()
        )
        for warning in self._mcp.warnings:
            # Printed here rather than in the banner so a one-shot run,
            # which prints no banner, still reports a missing server.
            print(f"Warning: {warning}")

        # Built only when there are skills *and* enforcement is on. Anything
        # else leaves it ``None``, which is what makes the shipped default
        # bit-for-bit Week 2's behaviour: no tracker, no wrapper, no
        # possibility of a skill narrowing the tool set (ADR-082).
        self._active_skills: ActiveSkills | None = (
            ActiveSkills(self._skills)
            if self._skills is not None
            and len(self._skills)
            and settings.skills_enforce_allowed_tools
            else None
        )

        # Short-term memory. Kept under the memory root so the file tools
        # cannot reach it (ADR-035), and built even when the vector store is
        # off: the two are different subsystems solving different problems,
        # and a session without long-term memory still runs long tasks
        # (ADR-090).
        self._scratchpad: Scratchpad | None = (
            Scratchpad(settings.memory_path, settings.notes_max_chars)
            if settings.notes_enabled
            else None
        )

        self._registry = build_agent_registry(
            settings.workspace_path,
            settings.max_tool_output_chars,
            settings.max_search_results,
            memory_store=self._store,
            skill_library=self._skills,
            mcp_tools=self._mcp.tools,
            active_skills=self._active_skills,
            scratchpad=self._scratchpad,
            memory_writer=self._memory_writer,
        )
        # Whether a stopped task is waiting to be picked up. Set when an
        # allowance stops a run, cleared when one finishes, and the reason
        # a bare "continue" is a resume rather than a new instruction
        # meaning whatever the model makes of the word (ADR-089).
        self._resumable = False
        # One context for the whole session: each REPL turn appends to it,
        # so follow-up questions can refer back to earlier work.
        #
        # Built *before* the loop, which it did not used to be. The
        # sub-agent factory needs the parent's identity — its ``run_id``, its
        # depth, how much of its iteration allowance is left — and it needs
        # the object, not a copy, because the id changes on every turn. That
        # is the whole of the reordering (ADR-084).
        self._context = RunContext(task="")
        # The tiering policy is built before the confirmer because the
        # prompt prints the policy's reason for asking. The skill wrapper,
        # when enforcement is on, goes around it in ``_build_policy``.
        self._tiers: RiskBasedPolicy = build_policy(
            self._mode,
            frozenset(settings.denied_tools),
            rules=settings.permission_rules.to_rules(),
            server_tiers=settings.server_tiers(),
        )
        # Shared with the children so a child's writes are confirmed by the
        # same person, on the same terminal, with the same "always" answers
        # they already gave.
        # One terminal for the confirmer and the trace hook, so a question
        # holds the trace back until it is answered (ADR-113).
        self._console = Console()
        self._confirmer = ConsoleConfirmer(
            explain=self._tiers.tier_of, console=self._console
        )
        hook = self._build_hook(trace, settings)
        self._hooks = hook
        # The agent's own calls, the parent's and each child's, are held to
        # what their turn did; the harness's internal calls above (summaries,
        # extraction, the consolidation judge) offer no tools and are not
        # wrapped (ADR-116).
        agent_llm: LLMClient = (
            GroundingLLMClient(llm) if settings.claim_check_enabled else llm
        )
        self._factory = self._build_factory(settings, agent_llm)
        if self._factory is not None:
            # Registered on the parent registry only. The factory builds
            # child registries by filtering *this* one against a
            # specialist's capabilities, and no specialist holds the
            # delegation capability — which is the structural half of the
            # recursion guard (ADR-084).
            self._registry.register(
                DelegateTool(
                    self._factory,
                    settings.workspace_path,
                    settings.max_tool_output_chars,
                )
            )
        # The coordinator's client, and only the coordinator's: at the first
        # read of a turn it asks whether the request splits into independent
        # parts, and if so hands the loop a `delegate` call for them. It plans
        # researchers only, and a researcher-only delegation is allowed in
        # every mode, a read-only session included (ADR-117, ADR-119).
        loop_llm: LLMClient = agent_llm
        if self._factory is not None and settings.subagent_auto_plan:
            loop_llm = DelegationPlanner(
                agent_llm,
                self._telemetry.traced_llm(llm, "delegation.plan"),
                self._factory.max_concurrency,
            )
        # The policy is built once per session, not once per turn, so a
        # tool approved with "always" stays approved across REPL turns.
        self._agent_loop = AgentLoop(
            llm=loop_llm,
            registry=self._registry,
            hook=hook,
            max_iterations=settings.max_iterations,
            max_tool_failures=settings.max_tool_failures,
            max_repeated_calls=settings.max_repeated_calls,
            system_prompt=AGENT_SYSTEM_PROMPT,
            max_context_chars=settings.max_context_chars,
            policy=self._build_policy(settings),
            confirmer=self._confirmer,
        )

    def _build_policy(self, settings: Settings) -> PermissionPolicy:
        """Build the permission policy, wrapped if skills may scope it.

        The configured policy is built exactly as before and then, only when
        enforcement is on, wrapped. Wrapping rather than replacing is what
        keeps the risk tiers, the deny-list and the confirmation prompt
        working unchanged: a skill can take tools away, never add one
        (ADR-057, ADR-082).

        Args:
            settings: Validated settings.

        Returns:
            The policy the loop consults before every tool call.
        """
        policy: PermissionPolicy = self._tiers
        if self._active_skills is not None:
            policy = SkillScopedPolicy(policy, self._active_skills)
        return policy

    def _build_hook(self, trace: bool, settings: Settings) -> CompositeHook:
        """Compose the hooks this session needs.

        Memory goes first: it mutates the context by injecting a section,
        and the trace hook only reports, so putting the mutating hook ahead
        of the reporting one keeps what is printed consistent with what was
        sent.

        Args:
            trace: Whether the per-tool trace is enabled.
            settings: Validated settings.

        Returns:
            One hook for the loop, however many observers it fans out to.
        """
        # Telemetry first, which it was not in Week 3 and now must be. It
        # opens the `agent.run` span in `on_run_start`, so every other
        # hook's work has to happen *inside* that call — the memory hook's
        # retrieval included. Composed after memory, memory's span would
        # open with nothing on the tracer's stack, become a root span, and
        # write a second trace file for the same run.
        #
        # `CompositeHook.on_run_end` fans out in reverse for exactly this
        # reason, so being first here also means closing last (ADR-093).
        hooks: list[Hook] = [self._telemetry]
        # Straight after telemetry, so a fold happens inside the run's span
        # and before anything else measures or injects into the context. Not
        # optional: it is the bound on a long chat, and without a model it
        # still folds into a plain summary (ADR-108).
        hooks.append(
            ConversationSummaryHook(
                settings.max_context_chars,
                self._telemetry.traced_llm(self._llm, "memory.summarise")
                if settings.conversation_summary_llm_enabled
                else None,
                keep_turns=settings.conversation_keep_turns,
                summary_max_chars=settings.conversation_summary_max_chars,
                telemetry=self._telemetry,
            )
        )
        if self._store is not None:
            hooks.append(
                MemoryHook(
                    self._store,
                    settings.workspace_path,
                    settings.project_memory_filename,
                    settings.memory_max_inject_chars,
                    settings.memory_core_fact_limit,
                    # Memory opens its own spans, because a hook cannot call
                    # itself and the retrieval that runs on every turn goes
                    # through no tool. Handed the tracer rather than a
                    # recorder so `harness/memory/` still imports no metrics
                    # library (ADR-034, ADR-094).
                    telemetry=self._telemetry,
                )
            )
        if self._memory_writer is not None and settings.memory_extraction_enabled:
            hooks.append(
                MemoryExtractionHook(
                    self._memory_writer,
                    self._telemetry.traced_llm(self._llm, "memory.extract"),
                    telemetry=self._telemetry,
                )
            )
        if self._skills is not None and len(self._skills):
            hooks.append(SkillsHook(self._skills, settings.skills_max_inject_chars))
        if self._scratchpad is not None:
            hooks.append(NotesHook(self._scratchpad))
        # Last, so its arrows stay the final thing printed for a call.
        self._console_hook = ConsoleTraceHook(
            trace,
            settings.max_iterations,
            permission_mode=self._mode,
            console=self._console,
        )
        hooks.append(self._console_hook)
        return CompositeHook(*hooks)

    def _build_telemetry(self, settings: Settings) -> tuple[TelemetryHook, list[str]]:
        """Build the tracer, its exporters, and the hook that fills it.

        Each exporter is independent and independently optional, which is
        the property the span rewrite was for: metrics off, traces off and
        OTLP off is a working tracer with an empty exporter list, costing
        one list append per span and no branches anywhere else (ADR-093).

        Args:
            settings: Validated settings.

        Returns:
            The hook, and any lines the banner should show about exporters
            that could not be started.
        """
        exporters: list[SpanExporter] = []
        notes: list[str] = []

        if settings.metrics_enabled:
            exporters.append(
                MetricsSpanExporter(self._recorder, settings.max_iterations)
            )
        if settings.traces_enabled:
            self._trace_exporter = JsonlSpanExporter(settings.traces_path)
            exporters.append(self._trace_exporter)
        if settings.otlp_enabled:
            # Never from config.yaml: that file is committed, and a hosted
            # collector's key is a secret (brief 8.2).
            headers = _otlp_headers_from_env()
            otlp, problem = build_otlp_exporter(
                settings.otlp_endpoint,
                settings.otlp_service_name,
                headers=headers,
            )
            if otlp is not None:
                exporters.append(otlp)
                # Both addresses, because they are different machines' view:
                # the endpoint is where this process sends spans (inside
                # compose, `jaeger:4318`), the viewer is what a person opens.
                # Showing only the first printed a URL nobody could open
                # (ADR-111).
                notes.append(
                    f"Traces:    OTLP to {settings.otlp_endpoint} · "
                    f"open {settings.otlp_ui_url}"
                )
                self._trace_viewer = settings.otlp_ui_url
            elif problem:
                notes.append(problem)

        self._tracer = Tracer(exporters)
        telemetry = TelemetryHook(
            self._tracer,
            model=settings.llm_model,
            provider=settings.llm_provider,
            temperature=settings.temperature,
            permission_mode=self._mode,
            redaction=settings.telemetry_redaction,
            max_iterations=settings.max_iterations,
        )
        if settings.telemetry_redaction != "shapes":
            # Said out loud on every start. A setting that quietly changes
            # what ends up in a trace backend is a setting somebody will
            # forget is on (ADR-094).
            notes.append(f"Telemetry: {describe_mode(settings.telemetry_redaction)}")
        return telemetry, notes

    def _build_factory(
        self, settings: Settings, llm: LLMClient
    ) -> ConfigurableFactory | None:
        """Build the delegation runtime, or ``None`` when it is switched off.

        Everything a child runs under is decided here, once, from settings —
        which is the brief's requirement that configuration be persistent and
        file-based applied to sub-agents as well as to the model and the
        workspace. Nothing at runtime can widen any of it: the factory
        narrows a specialist's permission mode against the session's, clamps
        its budget to the parent's remainder, and resolves its capabilities
        against the tools this session actually has (ADR-086).

        Args:
            settings: Validated settings.
            llm: The chat client, shared with every child.

        Returns:
            The factory, or ``None`` when ``subagents_enabled`` is false.
        """
        if not settings.subagents_enabled:
            return None
        budget = ExecutionBudget(
            max_iterations=settings.subagent_max_iterations,
            max_tool_calls=settings.subagent_max_tool_calls,
            timeout_seconds=settings.subagent_timeout_seconds,
            max_output_chars=settings.subagent_max_result_chars,
        )
        # Every observer is shared with children now, which used to be
        # impossible for the metrics half: it kept per-run timers on itself,
        # so a parent and a child would have crossed stopwatches. Spans keep
        # per-run state on a frame stack instead, and sharing the instance is
        # what makes a child's spans descend from the parent's rather than
        # start a trace of their own (ADR-093).
        shared: list[Hook] = [self._telemetry, self._console_hook]
        return ConfigurableFactory(
            llm=llm,
            parent_registry=self._registry,
            parent_context=self._context,
            catalog=build_catalog(budget),
            parent_permission_mode=self._mode,
            denied_tools=frozenset(settings.denied_tools),
            rules=settings.permission_rules.to_rules(),
            server_tiers=settings.server_tiers(),
            # The parent's answer, not the child's role: whether a
            # researcher's web searches prompt is the session's choice.
            open_world_outcome=open_world_outcome_for(self._mode),
            confirmer=self._confirmer,
            recorder=self._recorder,
            shared_hooks=shared,
            parent_max_iterations=settings.max_iterations,
            max_tool_failures=settings.max_tool_failures,
            max_repeated_calls=settings.max_repeated_calls,
            max_context_chars=settings.max_context_chars,
            max_depth=settings.subagent_max_depth,
            repair_attempts=settings.subagent_repair_attempts,
            artifacts_dir=(
                settings.traces_path / ARTIFACT_DIRNAME
                if settings.traces_enabled
                else None
            ),
            web_servers=tuple(settings.subagent_web_servers),
            max_concurrency=settings.subagent_max_concurrency,
        )

    @property
    def hooks(self) -> tuple[Hook, ...]:
        """The session's hooks in the order they were composed.

        Exposed for the test that pins the order, because the order is load
        bearing and was previously guaranteed only by comments: telemetry
        first (every other hook's work must happen inside the run's span),
        the conversation fold straight after it (it changes the context
        before anything measures or injects into it), and the console last
        (its answer line runs *first* on the reversed ``on_run_end``, ahead
        of slow run-end work — ADR-110).
        """
        return self._hooks.hooks

    @property
    def registry(self) -> ToolRegistry:
        """Return the tool registry (used for the banner)."""
        return self._registry

    @property
    def skills(self) -> SkillLibrary | None:
        """Return the discovered skills, or ``None`` when disabled."""
        return self._skills

    @property
    def metrics_note(self) -> str | None:
        """Return the banner's metrics line, or ``None`` when disabled."""
        return self._metrics_note

    @property
    def traces_note(self) -> str | None:
        """Return the banner's traces line, or ``None`` when disabled."""
        if not self._settings.traces_enabled:
            return None
        return f"{self._settings.traces_path}/<run_id>.jsonl"

    def reset_notes(self) -> None:
        """Start this run with an empty scratchpad.

        Called on the one-shot path only. A no-op when notes are switched
        off, so the caller does not have to know whether they are.
        """
        if self._scratchpad is not None:
            self._scratchpad.clear()

    @property
    def notes_note(self) -> str | None:
        """Return the banner's short-term memory line, or ``None`` when off."""
        if self._scratchpad is None:
            return None
        state = "empty" if self._scratchpad.is_empty else "carried over"
        return f"{self._scratchpad.describe} ({state})"

    @property
    def budget_note(self) -> str | None:
        """Return the banner's budget line, or ``None`` when disabled."""
        if self._budget is None:
            return None
        status = self._budget.status()
        resets = (
            f"resets {status.resets_at.strftime('%H:%M %Z')}"
            if status.resets_at is not None
            else "24h window starts on first use"
        )
        prompt = (
            f" · {status.prompt_limit_tokens:,}/prompt"
            if status.prompt_limit_tokens
            else ""
        )
        return (
            f"{status.used_tokens:,} / {status.limit_tokens:,} tokens in 24h"
            f"{prompt} · {resets} · {self._budget.ledger.describe}"
        )

    @property
    def budget(self) -> BudgetedLLMClient | None:
        """Return the metered client, or ``None`` when the budget is off.

        Exposed for the same reason ``factory`` is: the integration tests
        drive the real ``cli.main()`` and read what it built (ADR-014).
        """
        return self._budget

    @property
    def subagents_note(self) -> str | None:
        """Return the banner's sub-agent line, or ``None`` when disabled."""
        if self._factory is None:
            return None
        roles = ", ".join(self._factory.catalog.names())
        width = self._factory.max_concurrency
        # The width is worth a banner word: it is the difference between a
        # run that takes as long as its slowest child and one that takes as
        # long as all of them, and it is the first thing to turn down if a
        # concurrent run looks confusing.
        return roles if width < 2 else f"{roles} (up to {width} at once)"

    @property
    def telemetry_notes(self) -> list[str]:
        """Return extra banner lines about the telemetry exporters."""
        return list(self._telemetry_notes)

    @property
    def tracer(self) -> Tracer:
        """Return the tracer, for tests that assert on what was recorded."""
        return self._tracer

    @property
    def factory(self) -> ConfigurableFactory | None:
        """Return the delegation runtime, or ``None`` when disabled.

        Exposed for the same reason ``registry`` and ``store`` are: the
        integration tests drive the real ``cli.main()`` and need to look at
        what it built, not at a copy they assembled themselves (ADR-014).
        """
        return self._factory

    @property
    def mcp(self) -> McpDiscovery:
        """Return the connected MCP servers (used for the banner)."""
        return self._mcp

    @property
    def store(self) -> MemoryStore | None:
        """Return long-term memory, or ``None`` when disabled."""
        return self._store

    def close(self) -> None:
        """Stop every MCP server this session started, and release Qdrant.

        The harness had no session-level shutdown before MCP: nothing it
        owned outlived the process. A stdio server is a *child process*,
        so leaving it running would leak one per session, and a hung one
        would keep the terminal from returning. Callers run this from a
        ``finally`` so it happens on a clean exit, on Ctrl-C, and on an
        unhandled error alike. The memory client is closed here for the
        same reason: it holds an HTTP connection pool.
        """
        self._mcp.close()
        if isinstance(self._store, QdrantMemoryStore):
            self._store.close()
        # Releases the notes lock, so a session started later in the same
        # process owns the notes rather than getting a private copy. A
        # process that exits releases it anyway (ADR-110).
        if self._scratchpad is not None:
            self._scratchpad.close()
        # Closes any span still open — a run interrupted with Ctrl-C
        # leaves several — and then flushes and stops every exporter.
        # Without it an OTLP batch queued microseconds before exit is
        # lost, which is exactly the batch from the run that crashed.
        self._tracer.shutdown()
        if self._settings.metrics_enabled:
            # Published at the end rather than per span: the tracer owns
            # the running count, and a counter here would be a second
            # number that has to agree with it.
            self._recorder.spans_dropped(self._tracer.dropped_spans)
        # The exporter thread is a daemon, so the process would exit without
        # this. A test suite would not: the first session in the process
        # would keep 9091 and every later one would report the port busy —
        # a failure that is false and, worse, plausible (ADR-081).
        stop_metrics(self._settings.metrics_port)
