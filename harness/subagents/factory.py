"""The delegation runtime: build a child, run it, validate it, record it.

This module is the answer to the question the whole slice turns on — *is a
sub-agent a second kind of thing, or the same thing under different terms?*
It is the same thing. A child is a :class:`~harness.loop.AgentLoop` over a
:class:`~harness.context.RunContext`, exactly like the parent, and every
difference between them is a *parameter*: a different system prompt, a
narrower registry, a stricter policy, a smaller budget, its own identifiers.
``loop.py`` contains no branch for sub-agents and never learns that they
exist (ADR-084).

What the factory does, in the order it does it, is the delegation contract:

1.  **Look the specialist up.** An unknown id is the model's mistake, so it
    comes back as a failed result naming the real ids — never an exception.
2.  **Check the depth.** Structurally a child cannot delegate, because its
    registry has no ``delegate`` tool; this is the second lock on the same
    door, and it is the one that produces a readable refusal.
3.  **Derive the terms.** Permissions narrowed against the parent's,
    capabilities resolved against the tools that actually exist, budget
    clamped to what the parent has left.
4.  **Build an isolated context.** A new ``RunContext`` with its own system
    prompt and one message: the envelope. Nothing is copied from the parent.
5.  **Run it**, under its own hooks, so its model calls and tool calls land
    in the parent's trace as a nested run.
6.  **Validate the result** against the specialist's declared contract, with
    one repair attempt.
7.  **Record it** — a full artifact on disk, a condensed summary to the
    parent.

**Nothing crosses back except the result.** The child holds a reference to
no parent state: not the parent's context, not its message list, not its
tool failure counters. The one object they share is the tool *instances*,
which are stateless request handlers bound to a workspace, and the LLM
client, which is stateless by protocol. So "the child cannot mutate the
parent's context" is not a rule anybody has to remember — there is no
reference through which it could (ADR-084).
"""

import json
import threading
import time
import uuid
from collections.abc import Callable, Sequence
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from harness.context import RunContext
from harness.hooks import CompositeHook, Hook
from harness.llm.protocol import LLMClient, LLMError
from harness.loop import AgentLoop
from harness.observability.recorder import MetricsRecorder, NullRecorder
from harness.permissions import (
    Confirmer,
    DenyingConfirmer,
    PermissionMode,
    PermissionOutcome,
    PermissionRules,
    PermissionTier,
    build_policy,
)
from harness.subagents.confirm import AttributedConfirmer
from harness.subagents.contracts import (
    INVALID_OUTPUT_PREVIEW_CHARS,
    BatchResult,
    DelegationEnvelope,
    DelegationResult,
    DelegationStatus,
    DelegationTask,
    ExecutionBudget,
    RunMetadata,
    render_payload,
    validate_payload,
)
from harness.subagents.deadline import ChildDeadlineExceeded, DeadlineLLMClient
from harness.subagents.policy import ChildPolicy, budget_notice
from harness.subagents.prompts import (
    child_system_prompt,
    render_envelope,
    repair_request,
)
from harness.subagents.roles import (
    DEFAULT_WEB_SERVERS,
    AgentCatalog,
    AgentSpec,
    capability_report,
    tools_for,
)
from harness.tools.base import Tool, truncate
from harness.tools.registry import ToolRegistry

# Ranking of the permission modes, strictest first. Used to take the minimum
# of the parent's mode and the specialist's, which is the rule that makes
# delegation unable to escalate: a `deny_mutations` session cannot acquire a
# writing agent by asking for one (ADR-086).
_MODE_RANK: dict[PermissionMode, int] = {
    "deny_mutations": 0,
    "confirm_mutations": 1,
    "allow_all": 2,
}

# Subdirectory of the traces directory that full delegation records are
# written to. Beside the JSONL traces because it answers the same question —
# what happened in this run — for the same reader, and on the same volume,
# which is the one place in the container that outlives it (ADR-083).
ARTIFACT_DIRNAME = "delegations"


def narrow_mode(parent: PermissionMode, child: PermissionMode) -> PermissionMode:
    """Return the stricter of two permission modes.

    Args:
        parent: The mode the delegating session runs under.
        child: The mode the specialist asks for.

    Returns:
        Whichever of the two is stricter.
    """
    return parent if _MODE_RANK[parent] <= _MODE_RANK[child] else child


@dataclass
class PreparedChild:
    """A child that has been built but not yet run.

    Exists so building and running are separable. ``create(role)`` — the
    published seam — needs the loop and nothing else, tests need to inspect
    the registry and the policy without spending a model call, and
    :meth:`ConfigurableFactory.delegate` needs all of it. One construction
    path serves all three, which is what keeps the thing tests examine the
    same thing production runs.

    Attributes:
        spec: The specialist this child was built from.
        runtime: The terms the runtime set for it.
        loop: The child's agent loop.
        context: Its isolated context, with the system prompt already
            seeded and no turn started.
        policy: Its permission policy, which is also its budget meter.
        registry: The tools it holds.
        deadline: The monotonic instant it must stop acting at, or ``None``
            when the timeout is disabled. Carried here as well as inside the
            policy and the client because the *caller* needs it too: a
            batched delegation waits on a worker with this as its timeout, so
            a child that wedges somewhere neither of the other two can see
            still cannot hold the batch open forever.
    """

    spec: AgentSpec
    runtime: RunMetadata
    loop: AgentLoop
    context: RunContext
    policy: ChildPolicy
    registry: ToolRegistry
    deadline: float | None = None


@dataclass(frozen=True)
class _Admitted:
    """One task of a batch that passed admission and is going to run.

    A named record rather than a tuple because admission, budget division
    and execution all pass it around, and "which element was the index
    again" is exactly the question a dataclass stops anyone asking.

    Attributes:
        index: Where this task sat in the parent's list. Carried because the
            results are reassembled in the order asked for, not the order
            finished.
        spec: The specialist it resolved to.
        task: The envelope itself.
        transitions: Its lifecycle so far, which already holds ``CREATED``.
    """

    index: int
    spec: AgentSpec
    task: DelegationTask
    transitions: list[DelegationStatus]


class ConfigurableFactory:
    """Build and run specialists that reuse the parent's harness.

    Implements :class:`~harness.subagents.protocol.SubAgentFactory` for the
    build half and :class:`~harness.subagents.protocol.Delegator` for the run
    half. One object, because the two halves need the same nine collaborators
    and splitting them would mean passing all nine twice.

    What it *shares* with the parent, and why each is safe to share: the LLM
    client (stateless by protocol), the tool instances (stateless handlers
    bound to a workspace root), the confirmer (so the person at the terminal
    is asked about a child's writes exactly as they are asked about the
    parent's), and the observability hooks (so child events land in the
    parent's trace file). What it never shares: the context, the registry
    object, the policy, the budget, the identifiers.
    """

    def __init__(
        self,
        llm: LLMClient,
        parent_registry: ToolRegistry,
        parent_context: RunContext,
        catalog: AgentCatalog,
        *,
        parent_permission_mode: PermissionMode = "allow_all",
        denied_tools: frozenset[str] = frozenset(),
        rules: PermissionRules | None = None,
        server_tiers: dict[str, PermissionTier] | None = None,
        open_world_outcome: PermissionOutcome | None = None,
        confirmer: Confirmer | None = None,
        recorder: MetricsRecorder | None = None,
        shared_hooks: Sequence[Hook] = (),
        parent_max_iterations: int = 0,
        max_tool_failures: int = 5,
        max_repeated_calls: int = 10,
        max_context_chars: int = 24_000,
        max_depth: int = 1,
        repair_attempts: int = 1,
        artifacts_dir: Path | None = None,
        web_servers: Sequence[str] = DEFAULT_WEB_SERVERS,
        max_concurrency: int = 1,
        clock: Callable[[], float] | None = None,
    ) -> None:
        """Args:
        llm: The chat client, shared with the parent. One endpoint, one
            connection pool, one credential — a child does not get its
            own, because the client holds no per-run state.
        parent_registry: Every tool the parent holds. Children get
            filtered *copies of the same instances*, never new tools, so
            a child cannot reach a capability the parent never had.
        parent_context: The delegating run. Read for its ``run_id`` and
            its iteration count, and never written to — this is the
            reference that must not become a write path.
        catalog: The registered specialists.
        parent_permission_mode: The mode the session runs under. The
            ceiling every child's mode is narrowed against.
        denied_tools: Extra names refused outright, passed through to the
            policy so a child inherits the session's deny-list.
        rules: The session's ``permission_rules``, inherited unchanged: a
            pattern the operator wrote applies to a child's calls too.
        server_tiers: Per-server default tiers, likewise inherited.
        open_world_outcome: What an open-world *read* gets in a child. The
            CLI passes the parent session's answer, so a researcher under
            a ``confirm_mutations`` session prompts before searching the
            web exactly as the parent would, and one under ``allow_all``
            does not. ``None`` derives it from the child's narrowed mode,
            which is right for callers with no parent session to defer to
            (ADR-102).

            Note what is *not* inherited: the parent's
            :class:`~harness.skills.policy.SkillScopedPolicy`. A skill
            narrows the tool set of the turn that read it (ADR-082), and a
            child is not that turn — it has its own task, its own tool set
            and its own instructions, and a skill the parent opened says
            nothing about what a specialist may do. Inheriting it would
            silently strand a child whose own capabilities happened not to
            appear in somebody else's frontmatter.
        confirmer: How to reach a person when a child's call needs
            approval. Shared with the parent; defaults to refusing, which
            is the right answer when nobody is there to ask.
        recorder: Where child runs report. Kept for callers that build a
            factory directly; the shipped path reports through the shared
            telemetry hook in ``shared_hooks``, whose metrics exporter
            already holds this recorder. A child no longer needs a metrics
            hook of its own (ADR-093).
        shared_hooks: Observers shared with every child — the telemetry
            hook and the console trace. Sharing is what puts a child's
            spans inside the parent's ``tool.call`` and its tool calls on
            the same terminal (ADR-083, ADR-093).
        parent_max_iterations: The session's cap, or ``0`` for none. Used
            to work out what the parent has left to give.
        max_tool_failures: Inherited failure guard for children.
        max_repeated_calls: Inherited repetition guard for children.
        max_context_chars: Compaction budget for a child's own window.
        max_depth: How deep delegation may go. ``1`` means the parent may
            delegate and a child may not.
        repair_attempts: How many times a child whose result did not
            validate is asked to fix it. ``0`` disables the repair.
        artifacts_dir: Where full delegation records are written, or
            ``None`` to keep them in memory only.
        web_servers: MCP server names whose tools count as web research.
        max_concurrency: How many children one batched delegation may run at
            once. ``1`` — the default, so a caller that builds a factory by
            hand keeps the old behaviour — runs them one after another on
            the calling thread and starts no pool at all.
        clock: Monotonic clock for child deadlines, injectable so a test can
            expire a child without sleeping. Shared by a child's policy and
            its deadline-checked client, so the two halves of one timeout
            cannot disagree.
        """
        self._llm = llm
        self._parent_registry = parent_registry
        self._parent_context = parent_context
        self._catalog = catalog
        self._parent_mode = parent_permission_mode
        self._denied_tools = denied_tools
        self._rules = rules
        self._server_tiers = server_tiers
        self._open_world_outcome = open_world_outcome
        self._confirmer = confirmer if confirmer is not None else DenyingConfirmer()
        self._recorder = recorder if recorder is not None else NullRecorder()
        self._shared_hooks = tuple(shared_hooks)
        self._parent_max_iterations = parent_max_iterations
        self._max_tool_failures = max_tool_failures
        self._max_repeated_calls = max_repeated_calls
        self._max_context_chars = max_context_chars
        self._max_depth = max_depth
        self._repair_attempts = repair_attempts
        self._artifacts_dir = artifacts_dir
        self._web_servers = tuple(web_servers)
        self._max_concurrency = max(1, max_concurrency)
        self._clock = clock if clock is not None else time.monotonic
        # One lock for the whole session, handed to every child's confirmer.
        # It is what keeps `require_user_confirmation` answerable when two
        # children reach a mutation at the same moment (ADR-106).
        self._confirm_lock = threading.Lock()

    # -- the published seam ------------------------------------------------

    @property
    def catalog(self) -> AgentCatalog:
        """The registered specialists, for the ``delegate`` tool's schema."""
        return self._catalog

    def create(self, role: str) -> AgentLoop:
        """Return a loop configured for ``role``.

        The seam ``harness/subagents/protocol.py`` has promised since Week 1,
        and the shape the architecture document has claimed since Week 1:
        a sub-agent is another ``AgentLoop``, so a factory returns one.
        :meth:`delegate` is the operation that runs it and handles the
        contract around it; this is the construction on its own, which is
        what a caller driving a child by hand — a test, a future
        orchestrator — needs.

        Args:
            role: A registered specialist id.

        Returns:
            The child's loop.

        Raises:
            KeyError: If ``role`` is not registered. A raise is right *here*
                and wrong in :meth:`delegate`: this is a programming error
                by a Python caller, whereas an unknown role in a delegation
                is a model's mistake and comes back as a failed result.
        """
        spec = self._catalog.get(role)
        if spec is None:
            raise KeyError(f"unknown sub-agent role: {role}")
        return self.prepare(spec).loop

    # -- building ----------------------------------------------------------

    def prepare(
        self, spec: AgentSpec, ceiling: ExecutionBudget | None = None
    ) -> PreparedChild:
        """Build a child run without starting it.

        Args:
            spec: The specialist to build.
            ceiling: What this particular child may be given, overriding the
                parent's whole remainder. A batched delegation passes each
                child its *share*, because N children clamped to the same
                remainder would collectively spend N times it — which is the
                iteration guard failing open (ADR-106). ``None`` keeps the
                single-child behaviour of clamping to the whole remainder.

        Returns:
            Everything the child needs, assembled but not run.
        """
        child_run_id = uuid.uuid4().hex
        mode = narrow_mode(self._parent_mode, spec.permission_mode)
        budget = spec.budget.narrowed_to(
            self._parent_ceiling() if ceiling is None else ceiling
        )
        names = tools_for(spec, self._tools_by_name(), self._web_servers)
        registry = self._child_registry(names)

        runtime = RunMetadata(
            agent_id=spec.agent_id,
            role=spec.role,
            child_run_id=child_run_id,
            parent_run_id=self._parent_run_id(),
            depth=self._parent_context.depth + 1,
            permission_mode=mode,
            capabilities=tuple(
                sorted(capability.value for capability in spec.capabilities)
            ),
            tools=tuple(names),
            budget=budget,
            deadline_epoch=(
                None
                if budget.timeout_seconds <= 0
                else time.time() + budget.timeout_seconds
            ),
        )

        # One deadline, computed once and handed to all three places that
        # need it: the policy (which bounds the tool calls a child starts),
        # the client (which bounds the model calls), and the caller (which
        # bounds the wait). Computing it three times from three clocks is
        # how a timeout comes to mean three slightly different things.
        deadline = (
            None
            if budget.timeout_seconds <= 0
            else self._clock() + budget.timeout_seconds
        )

        policy = ChildPolicy(
            inner=build_policy(
                mode,
                self._denied_tools,
                rules=self._rules,
                server_tiers=self._server_tiers,
                open_world_outcome=self._open_world_outcome,
            ),
            allowed_tools=frozenset(names),
            budget=budget,
            deadline=deadline,
            clock=self._clock,
        )

        prompt = child_system_prompt(spec, runtime)
        context = RunContext(
            task="",
            run_id=child_run_id,
            parent_run_id=runtime.parent_run_id,
            role=spec.role,
            depth=runtime.depth,
        )
        # Seeded here rather than in the loop, exactly as the CLI seeds the
        # parent's: `AgentLoop.run` takes a context that is already prepared,
        # which is what let a child be a fresh one with no change to the loop.
        context.ensure_system_prompt(prompt)

        loop = AgentLoop(
            llm=self._child_llm(deadline),
            registry=registry,
            hook=self._child_hook(budget.max_iterations),
            max_iterations=budget.max_iterations,
            max_tool_failures=self._max_tool_failures,
            max_repeated_calls=self._max_repeated_calls,
            system_prompt=prompt,
            max_context_chars=self._max_context_chars,
            policy=policy,
            # Wrapped so the prompt says which specialist is asking and so
            # two concurrent children cannot interleave their questions on
            # one terminal. The person and the policy are unchanged.
            confirmer=AttributedConfirmer(
                self._confirmer, self._confirm_lock, spec.role
            ),
        )
        return PreparedChild(
            spec, runtime, loop, context, policy, registry, deadline=deadline
        )

    def _child_llm(self, deadline: float | None) -> LLMClient:
        """Return the client this child reasons through.

        The shared one when it has no deadline to keep, and a
        :class:`~harness.subagents.deadline.DeadlineLLMClient` when it does.
        Wrapping only when there is something to enforce means a session with
        ``subagent_timeout_seconds: 0`` runs exactly the code it ran before.

        Args:
            deadline: The child's monotonic deadline, or ``None``.
        """
        if deadline is None:
            return self._llm
        return DeadlineLLMClient(self._llm, deadline, clock=self._clock)

    def _tools_by_name(self) -> dict[str, Tool]:
        """Return the parent's tools by name, for the capability filter."""
        return {
            name: self._parent_registry.get(name)
            for name in self._parent_registry.names()
        }

    def _child_registry(self, names: Sequence[str]) -> ToolRegistry:
        """Return a registry holding exactly the named tools.

        The same *instances* the parent holds, in a registry of their own.
        Copying the instances rather than constructing new tools is what
        makes "a child cannot reach a capability the parent never had" true
        by construction, and it also means a child does not start a second
        MCP server process for a tool the parent already has connected.

        Args:
            names: Tool names the capability filter resolved to.

        Returns:
            The child's registry.
        """
        registry = ToolRegistry()
        for name in names:
            registry.register(self._parent_registry.get(name))
        return registry

    def _child_hook(self, max_iterations: int) -> Hook:
        """Compose the hooks one child run reports through.

        This method used to build a *fresh* metrics hook per child and
        share only the trace hooks, because ``MetricsHook`` kept its timers
        as attributes on itself: a child sharing the parent's instance would
        have stopped the parent's clock when it finished its own model call.

        That distinction is gone, and its disappearance is the clearest
        practical benefit of the span rewrite. ``TelemetryHook`` keeps
        per-run state on a stack of frames, so there is nothing to cross —
        and sharing it is now *required* rather than merely safe, because
        the tracer's span stack is what makes the child's ``agent.run`` a
        descendant of the parent's ``tool.call`` instead of the root of a
        trace nobody would find (ADR-093).

        So every observer is shared, and this method is one line. It is kept
        as a method rather than inlined because the *absences* below are a
        decision worth having somewhere to write down.

        Memory and skills hooks are deliberately *absent*. A child is not
        the user's agent: injecting the user's remembered facts into a
        specialist's window would leak parent-private context across the
        isolation boundary and spend the tokens delegation exists to save.

        Args:
            max_iterations: The child's cap. No longer needed to classify a
                capped child — the loop records ``exit_reason`` on the
                context and the run span copies it (ADR-084) — and kept in
                the signature because the catalog passes it and a parameter
                removed today is a merge conflict tomorrow.

        Returns:
            One hook for the child's loop.
        """
        return CompositeHook(*self._shared_hooks)

    def _finalize_span(self, role: str) -> Any:
        """Open a span over a child's contract validation, if telemetry is on.

        Found by duck-typing the shared hooks rather than by taking a tracer
        as a constructor argument. That is deliberate: this module already
        receives its observers through ``shared_hooks``, and adding a tenth
        collaborator so one span can be opened would be a worse trade than
        one ``getattr`` — especially since the answer when nobody is
        watching has to be "do nothing" either way.

        Args:
            role: The specialist whose result is being validated.

        Returns:
            A context manager yielding a span, or one that does nothing.
        """
        for hook in self._shared_hooks:
            factory = getattr(hook, "finalization", None)
            if factory is None:
                continue
            try:
                return factory(**{"harness.subagent.role": role})
            except Exception:  # noqa: BLE001 - an observer must not fail a run
                break
        return _NoSpan()

    def _parent_run_id(self) -> str:
        """Return the delegating run's id, minting one if it has none.

        A parent that was never started through ``start_turn`` — a test
        driving the loop directly — still has to produce a *linked* child,
        because the link is the thing the observability requirement is
        about. So an id is assigned to the parent rather than left empty,
        and the child hangs off it.
        """
        if not self._parent_context.run_id:
            self._parent_context.run_id = uuid.uuid4().hex
        return self._parent_context.run_id

    def _parent_ceiling(self) -> ExecutionBudget:
        """Return what the parent still has to give.

        The "child allocation ≤ parent's remaining budget" rule, made
        concrete against the one parent limit this harness actually tracks:
        iterations. With no cap configured the parent has no remainder to
        run out of and the specialist's own budget stands; with a cap, a
        child is never handed more steps than the parent has left, because a
        child that outlives its parent's allowance has escaped the guard
        that was supposed to bound the whole run.

        Returns:
            The ceiling to clamp a specialist's budget against.
        """
        return ExecutionBudget(
            max_iterations=self._remaining_iterations(),
            max_tool_calls=0,
            timeout_seconds=0.0,
            max_output_chars=ExecutionBudget().max_output_chars,
        )

    def _remaining_iterations(self) -> int:
        """Return how many of the parent's iterations are still unspent.

        ``0`` when the session has no cap at all, which
        :class:`ExecutionBudget` reads as "no ceiling here" — correct, since
        an uncapped parent has no remainder to run out of. When the session
        *is* capped, ``delegate`` checks this directly before clamping, so
        the two meanings of zero never meet.
        """
        if not self._parent_max_iterations:
            return 0
        return max(
            0, self._parent_max_iterations - self._parent_context.iterations_used
        )

    # -- delegating --------------------------------------------------------

    @property
    def max_concurrency(self) -> int:
        """How many children one batched delegation may run at once."""
        return self._max_concurrency

    def delegate(self, task: DelegationTask) -> DelegationResult:
        """Run one delegation and return its structured outcome.

        Kept as the single-task entry point because it is what
        :class:`~harness.subagents.protocol.Delegator` promises and what
        every existing caller uses. It is now a batch of one, so there is
        one code path rather than two that must agree.

        Args:
            task: The parent's envelope.

        Returns:
            The outcome, always.
        """
        return self.delegate_batch([task]).results[0]

    def delegate_batch(self, tasks: Sequence[DelegationTask]) -> BatchResult:
        """Run several delegations, concurrently, and return all their outcomes.

        Never raises for anything the model can cause. An unknown role, a
        depth limit, an exhausted budget, an endpoint failure and an
        unparseable answer all come back as a :class:`DelegationResult` with
        a status the parent can read and act on, because the alternative —
        an exception crossing back into the parent's tool dispatch — turns
        one child's bad turn into the end of the parent's run (ADR-085).

        **Rejections are decided before anything starts.** A task naming an
        unknown role does not consume a share of the iteration budget, so
        the children that *can* run are not punished for a sibling's typo.

        **Independence is assumed, not enforced.** Children share no mutable
        state — separate contexts, separate policies, separate registries —
        so running them together is safe in the only sense this module can
        guarantee. Whether two tasks are *semantically* independent is the
        coordinating model's judgement, and the system prompt is where it is
        asked to exercise it.

        Args:
            tasks: The parent's envelopes, in the order it asked for them.

        Returns:
            One result per task, in that same order.
        """
        admitted: list[_Admitted] = []
        results: dict[int, DelegationResult] = {}

        # Resolve the parent's id here, on the calling thread, before any
        # worker can. It is the one read of the parent context that can also
        # *write* to it — it mints an id when the parent has none — and three
        # workers doing that at once would give sibling children three
        # different parents. After this call it is a plain read.
        self._parent_run_id()

        # --- admission: decide every rejection first ------------------------
        for index, task in enumerate(tasks):
            transitions = [DelegationStatus.CREATED]
            refusal = self._refusal_for(task)
            if refusal is not None:
                results[index] = self._rejected(task, transitions, refusal)
                continue
            spec = self._catalog.get(task.role)
            if spec is None:  # pragma: no cover - _refusal_for already checked
                continue
            admitted.append(_Admitted(index, spec, task, transitions))

        # --- allocate what is left, then run --------------------------------
        if admitted:
            ceiling = self._share_of_ceiling(len(admitted))
            if ceiling is None:
                # The parent cannot afford one iteration each. Refusing the
                # whole batch beats starting some of it: a partial fan-out
                # decided by list order is a confusing thing to explain to
                # the model, and it has a better move available — ask for
                # fewer things.
                for entry in admitted:
                    results[entry.index] = self._rejected(
                        entry.task,
                        entry.transitions,
                        f"not enough iteration budget left to run "
                        f"{len(admitted)} sub-agents. Delegate fewer tasks, "
                        f"or answer with what you have.",
                    )
            else:
                results.update(self._run_all(admitted, ceiling))

        return BatchResult(results=tuple(results[i] for i in sorted(results)))

    def _refusal_for(self, task: DelegationTask) -> str | None:
        """Return why this task cannot run at all, or ``None`` if it can.

        Separated from :meth:`delegate_batch` so admission is one decision
        per task with no side effects, which is what makes it safe to run
        over the whole batch before anything starts.

        Args:
            task: One of the parent's envelopes.

        Returns:
            The refusal to report, or ``None``.
        """
        if self._catalog.get(task.role) is None:
            known = ", ".join(self._catalog.names()) or "none"
            return f"unknown sub-agent role {task.role!r}. Available roles: {known}."
        if self._parent_context.depth >= self._max_depth:
            return (
                f"delegation is not available here: the maximum delegation "
                f"depth of {self._max_depth} has been reached. Do this part "
                f"of the task yourself."
            )
        if self._parent_max_iterations and self._remaining_iterations() <= 0:
            # The parent is out of its own allowance, so there is nothing to
            # allocate. Checked here rather than on the clamped budget,
            # because ``0`` means *no limit* everywhere in
            # :class:`ExecutionBudget` — so an exhausted parent read through
            # the clamp would look like permission to run an unbounded child,
            # which is the opposite of what it is.
            return (
                "no iteration budget is left to delegate with. "
                "Answer with what you have."
            )
        return None

    def _share_of_ceiling(self, children: int) -> ExecutionBudget | None:
        """Return what *each* of ``children`` concurrent children may be given.

        The fix for the one guard that fan-out would otherwise break. A
        child's budget is clamped to the parent's remaining iterations; run
        three children against that same remainder and between them they can
        spend three times it, so the bound that was supposed to hold for the
        whole run holds for none of it.

        Args:
            children: How many children are about to run.

        Returns:
            The per-child ceiling, or ``None`` when the parent cannot afford
            one iteration each.
        """
        remaining = self._remaining_iterations()
        if not remaining:
            # No cap configured: `0` means "no ceiling here" and dividing it
            # would turn "unlimited" into "zero", which is the opposite.
            return self._parent_ceiling()
        share = remaining // children
        if share < 1:
            return None
        return ExecutionBudget(
            max_iterations=share,
            max_tool_calls=0,
            timeout_seconds=0.0,
            max_output_chars=ExecutionBudget().max_output_chars,
        )

    def _run_all(
        self,
        admitted: Sequence[_Admitted],
        ceiling: ExecutionBudget,
    ) -> dict[int, DelegationResult]:
        """Run the admitted tasks, concurrently when there is more than one.

        A batch of one — which every non-batched call is — runs on the
        calling thread with no pool involved, so the common path keeps the
        stack trace and the behaviour it always had.

        Args:
            admitted: The tasks that passed admission.
            ceiling: Each child's share of the parent's remainder.

        Returns:
            Results by their position in the parent's list.
        """
        if len(admitted) == 1 or self._max_concurrency == 1:
            return {entry.index: self._run_one(entry, ceiling) for entry in admitted}

        # The span the children must hang beneath. Captured on *this* thread,
        # before any worker starts, because it is this thread's open
        # `tool.call` — the one the parent's `delegate` is running inside.
        # Without it each child's spans would be a root and the execution
        # tree the assignment asks us to record would not exist.
        tracer = self._tracer()
        parent_span = None if tracer is None else tracer.current
        workers = min(self._max_concurrency, len(admitted))
        with ThreadPoolExecutor(
            max_workers=workers, thread_name_prefix="subagent"
        ) as pool:
            futures = {
                pool.submit(self._run_one, entry, ceiling, parent_span): entry.index
                for entry in admitted
            }
            # `_run_one` does not raise for anything a model can cause, so a
            # plain `result()` here is not hiding a failure mode; the one
            # thing it could still do is block, and the deadline inside the
            # child is what bounds that.
            return {futures[future]: future.result() for future in futures}

    def _tracer(self) -> Any:
        """Return the tracer one of the shared hooks reports through, if any.

        Duck-typed for the same reason :meth:`_finalize_span` is: the factory
        is handed a sequence of hooks and does not know which of them, if
        any, traces. A session with observability off has none, and that is
        an ordinary configuration rather than a missing collaborator.
        """
        for hook in self._shared_hooks:
            tracer = getattr(hook, "tracer", None)
            if tracer is not None:
                return tracer
        return None

    def _run_one(
        self,
        entry: _Admitted,
        ceiling: ExecutionBudget,
        parent_span: Any = None,
    ) -> DelegationResult:
        """Build and run one child, whatever thread we are on.

        Args:
            entry: The admitted task.
            ceiling: Its share of the parent's remaining iterations.
            parent_span: The span to nest this child's spans beneath when it
                runs on a worker thread. ``None`` on the calling thread,
                where the tracer's own stack already has the right parent.

        Returns:
            The outcome, always.
        """
        tracer = self._tracer()
        if parent_span is None or tracer is None:
            return self._execute_child(entry, ceiling)
        # Adopt the parent's open span for the life of this worker, so the
        # child's spans join the parent's trace rather than starting one.
        with tracer.branch(parent_span):
            return self._execute_child(entry, ceiling)

    def _execute_child(
        self, entry: _Admitted, ceiling: ExecutionBudget
    ) -> DelegationResult:
        """Run one prepared child through its whole lifecycle.

        This is the body the sequential ``delegate`` always had, unchanged
        except that the budget ceiling is now passed in rather than derived.

        Args:
            entry: The admitted task.
            ceiling: Its share of the parent's remaining iterations.

        Returns:
            The outcome, always.
        """
        spec, task, transitions = entry.spec, entry.task, entry.transitions
        child = self.prepare(spec, ceiling)
        envelope = DelegationEnvelope(task=task, runtime=child.runtime)
        child.context.start_turn(render_envelope(envelope))
        # ``start_turn`` mints a fresh id for the turn it starts. The child's
        # id was decided when it was prepared — the prompt it is running
        # under names that id — so it is restored rather than left to drift.
        child.context.run_id = child.runtime.child_run_id

        transitions.append(DelegationStatus.RUNNING)
        started = time.monotonic()
        try:
            answer = child.loop.run(child.context)
        except ChildDeadlineExceeded:
            # The half of the timeout the permission gate cannot see: this
            # child stopped calling tools and kept reasoning. Reported as
            # TIMED_OUT rather than FAILED because the endpoint is healthy
            # and the parent's useful next move is a narrower task, not a
            # retry of the same one.
            return self._finish(
                envelope,
                child,
                transitions,
                DelegationStatus.TIMED_OUT,
                summary="",
                error=(
                    "the sub-agent ran out of time before it reached an "
                    "answer. Ask for something narrower."
                ),
                duration_ms=_ms(started),
            )
        except LLMError as exc:
            return self._finish(
                envelope,
                child,
                transitions,
                DelegationStatus.FAILED,
                summary="",
                error=f"the sub-agent's model endpoint failed: {exc}",
                duration_ms=_ms(started),
            )
        except KeyboardInterrupt:
            # Recorded, then re-raised. A cancelled child is a cancelled
            # turn: swallowing it here would leave the parent working on
            # behalf of a person who has already pressed Ctrl-C.
            self._finish(
                envelope,
                child,
                transitions,
                DelegationStatus.CANCELLED,
                summary="",
                error="the sub-agent run was cancelled.",
                duration_ms=_ms(started),
            )
            raise

        # Contract validation and repair, inside a span of their own.
        #
        # This is the one place in the harness where the brief's
        # `finalization` node is real work rather than decoration. For the
        # top-level agent, finalization is the loop returning the model's
        # text and there is nothing to measure. Here the answer is parsed,
        # checked against the specialist's declared output contract, and
        # possibly re-requested — and every one of those can fail while the
        # child's model calls and tool calls all succeeded, which is exactly
        # the kind of failure a run-level `ok` bit cannot express (ADR-085,
        # ADR-093).
        with self._finalize_span(spec.role) as span:
            payload, reason = validate_payload(spec.result_model, answer)
            repairs = 0
            if payload is None and self._repair_attempts > 0:
                answer, payload, reason = self._repair(child, spec, answer, reason)
                repairs = 1
            _annotate_finalize(span, payload is not None, repairs, reason)

        duration_ms = _ms(started)
        if payload is not None:
            notice = budget_notice(child.policy)
            summary = render_payload(payload)
            return self._finish(
                envelope,
                child,
                transitions,
                DelegationStatus.COMPLETED,
                summary=f"{notice}\n{summary}" if notice else summary,
                error=None,
                duration_ms=duration_ms,
                payload=payload,
            )

        return self._finish(
            envelope,
            child,
            transitions,
            self._failure_status(child),
            summary=truncate(answer.strip(), INVALID_OUTPUT_PREVIEW_CHARS),
            error=(
                f"the {spec.role} sub-agent did not return a "
                f"usable result. {reason} What it said is below, unvalidated - "
                f"treat it as a report of a failed attempt, not as a finding."
            ),
            duration_ms=duration_ms,
        )

    def _repair(
        self,
        child: PreparedChild,
        spec: AgentSpec,
        answer: str,
        reason: str | None,
    ) -> tuple[str, dict[str, object] | None, str | None]:
        """Give a child one chance to restate a result in the agreed shape.

        Worth a turn because the two failures it fixes are the common ones
        and neither means the work was wrong: a model that wrapped the object
        in prose, and one that got a field name slightly off. Throwing away
        a correct investigation over its packaging is the expensive outcome;
        one extra model call is the cheap one.

        The repair runs in the child's *own* context, so it can see what it
        already said and does not redo the work. It is not given fresh
        budget: the same loop, the same policy, the same meters.

        Args:
            child: The prepared child, after its first run.
            spec: Its specification.
            answer: What it said.
            reason: Why that did not validate.

        Returns:
            ``(answer, payload, reason)`` after the attempt. On failure the
            *original* answer comes back, not the failed repair: the first
            one holds the work, and the preview shown to the parent should
            be the substantive attempt rather than a second failed
            formatting of it.
        """
        original = answer
        for _ in range(self._repair_attempts):
            child.context.start_turn(repair_request(reason or "It was not valid."))
            child.context.run_id = child.runtime.child_run_id
            try:
                answer = child.loop.run(child.context)
            except (LLMError, ChildDeadlineExceeded):
                # The endpoint died, or the clock ran out, during the repair.
                # Either way the first answer is still the best evidence
                # there is, so it is kept and the delegation fails on its
                # original reason.
                return original, None, reason
            payload, reason = validate_payload(spec.result_model, answer)
            if payload is not None:
                return answer, payload, None
        return original, None, reason

    def _failure_status(self, child: PreparedChild) -> DelegationStatus:
        """Classify a delegation that produced no validated payload.

        Three different things have gone wrong and the parent's next move
        differs for each: a child that ran out of clock might succeed if the
        task is narrowed, a child whose endpoint failed should simply be
        retried, and a child that answered but could not format it has
        probably done the work and needs asking differently.

        The clock is read two ways on purpose. ``policy.expired`` is set when
        a *tool call* was refused for being late; the deadline comparison
        catches the child that went quiet and reasoned past it without asking
        for anything. Before the deadline was enforced at the model call, the
        second case was unreachable and ``TIMED_OUT`` was a status the
        runtime could not produce (ADR-106).

        Args:
            child: The prepared child, after its run.

        Returns:
            The terminal status.
        """
        if child.policy.expired:
            return DelegationStatus.TIMED_OUT
        if child.deadline is not None and self._clock() >= child.deadline:
            return DelegationStatus.TIMED_OUT
        if child.context.exit_reason == "error":
            return DelegationStatus.FAILED
        return DelegationStatus.INVALID_OUTPUT

    # -- recording ---------------------------------------------------------

    def _rejected(
        self,
        task: DelegationTask,
        transitions: list[DelegationStatus],
        error: str,
    ) -> DelegationResult:
        """Return a delegation that was refused before anything ran.

        Deliberately produces no child run id and never reaches ``RUNNING``:
        a rejection must be distinguishable in the record from a child that
        started and failed, or the execution tree gains runs that never
        existed.

        Args:
            task: What was asked for.
            transitions: The lifecycle so far.
            error: Why it was refused.

        Returns:
            The rejected result.
        """
        transitions.append(DelegationStatus.REJECTED)
        return DelegationResult(
            status=DelegationStatus.REJECTED,
            agent_id=task.role,
            role=task.role,
            child_run_id="",
            parent_run_id=self._parent_run_id(),
            error=error,
            transitions=tuple(transitions),
        )

    def _finish(
        self,
        envelope: DelegationEnvelope,
        child: PreparedChild,
        transitions: list[DelegationStatus],
        status: DelegationStatus,
        *,
        summary: str,
        error: str | None,
        duration_ms: int,
        payload: dict[str, object] | None = None,
    ) -> DelegationResult:
        """Assemble the result, write the artifact, and return the summary.

        The summary is clipped to the child's own ``max_output_chars``
        budget *here*, at the boundary, rather than being trusted to be
        short. The one thing a delegation must guarantee the parent is that
        a child's verbosity cannot become the parent's problem.

        Args:
            envelope: What the child was started with.
            child: The prepared child, after its run.
            transitions: The lifecycle so far.
            status: The terminal state.
            summary: Condensed text for the parent.
            error: Why it failed, or ``None``.
            duration_ms: Wall-clock duration.
            payload: The validated result, when there is one.

        Returns:
            The finished result.
        """
        transitions.append(status)
        # The path is decided before the record is built, not after it is
        # written, so the stored copy of the result names the file it is in.
        # A record whose own `artifact_path` is null is a small thing and an
        # actively confusing one to read.
        path = (
            None
            if self._artifacts_dir is None
            else str(self._artifacts_dir / f"{child.runtime.child_run_id}.json")
        )
        result = DelegationResult(
            status=status,
            agent_id=child.spec.agent_id,
            role=child.spec.role,
            child_run_id=child.runtime.child_run_id,
            parent_run_id=child.runtime.parent_run_id,
            summary=truncate(summary.strip(), child.runtime.budget.max_output_chars),
            payload=dict(payload) if payload is not None else None,
            error=error,
            iterations=child.context.iterations_used,
            tool_calls=child.policy.calls,
            prompt_tokens=child.context.prompt_tokens,
            completion_tokens=child.context.completion_tokens,
            duration_ms=duration_ms,
            exit_reason=child.context.exit_reason,
            transitions=tuple(transitions),
            artifact_path=path,
        )
        if path is not None and not self._write_artifact(envelope, child, result):
            # The record could not be written, so the pointer would be a lie.
            return result.model_copy(update={"artifact_path": None})
        return result

    def _write_artifact(
        self,
        envelope: DelegationEnvelope,
        child: PreparedChild,
        result: DelegationResult,
    ) -> bool:
        """Store the full record of one delegation, and return its path.

        This is the data plane. What the parent's context receives is a
        summary and this path; what a person debugging the run needs is the
        envelope that was sent, the terms it ran under, and the whole result
        — and that belongs in a file, not in a model's window.

        Best-effort, on the same terms as the JSONL trace: a directory that
        cannot be created or a file that cannot be written costs the record
        and not the run. A failed task because a log file was read-only is a
        harness with its priorities backwards (ADR-083).

        Args:
            envelope: What the child was started with.
            child: The prepared child.
            result: The finished result.

        Returns:
            Whether the record was written.
        """
        if self._artifacts_dir is None:
            return False
        record = {
            "envelope": envelope.model_dump(mode="json"),
            "result": result.model_dump(mode="json"),
            "capabilities": capability_report(child.spec),
            "tool_calls_attempted": child.policy.attempts,
            "tool_calls_refused_for_capability": child.policy.refused_for_capability,
            "budget_exhausted": child.policy.budget_exhausted,
            "expired": child.policy.expired,
        }
        path = self._artifacts_dir / f"{child.runtime.child_run_id}.json"
        try:
            self._artifacts_dir.mkdir(parents=True, exist_ok=True)
            path.write_text(
                json.dumps(record, indent=2, ensure_ascii=False), encoding="utf-8"
            )
        except OSError:
            return False
        return True


def _ms(started: float) -> int:
    """Whole milliseconds since a monotonic stamp."""
    return max(0, round((time.monotonic() - started) * 1000))


class _NoSpan:
    """A span-shaped context manager for when telemetry is switched off.

    One code path instead of two. A factory that checked for a tracer at
    each call site is a factory where one of those checks is eventually
    written without the guard (ADR-081's argument for ``NullRecorder``,
    applied to spans).
    """

    def __enter__(self) -> None:
        """Enter, yielding no span."""
        return None

    def __exit__(self, *exc_info: object) -> None:
        """Exit without suppressing anything.

        ``None`` rather than ``False``: they mean the same thing to Python,
        and mypy reads a ``bool`` return as "this may swallow exceptions",
        which is the one thing a null context manager must never do.
        """


def _annotate_finalize(
    span: Any, valid: bool, repairs: int, reason: str | None
) -> None:
    """Record how a child's result validation went.

    Marks the span as failed when the contract was not met, which is what
    puts ``subagent.invalid_output`` on the failure-attribution panel rather
    than leaving an invalid result looking like a successful child run — the
    distinction ADR-085 introduced the status for, now visible in telemetry
    as well as in the parent's context (ADR-094).

    Args:
        span: The open span, or ``None`` when telemetry is off.
        valid: Whether the result matched the declared contract.
        repairs: How many repair attempts were spent.
        reason: Why it did not validate, when it did not.
    """
    if span is None:
        return
    try:
        span.set("harness.subagent.result_status", "valid" if valid else "invalid")
        if repairs:
            span.set("harness.subagent.repair_attempts", repairs)
        if not valid:
            from harness.observability.errors import SUBAGENT_INVALID_OUTPUT

            span.fail(SUBAGENT_INVALID_OUTPUT, reason)
    except Exception:  # noqa: BLE001 - an observer must not fail a run
        return
