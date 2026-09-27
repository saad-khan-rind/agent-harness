"""Delegation: the runtime, the isolation, the limits, the lifecycle.

What is under test here is the claim §8.4 actually makes — that a sub-agent
is "a separately agent instance with its own role or instructions, context,
execution limits, and permissions". Every one of those five words gets an
assertion, and the ones about limits are written as *attempts*: it is worth
very little to assert that a researcher has no ``write_file`` in its
registry, and a great deal to assert that a researcher which asks for
``write_file`` anyway is refused by the runtime and counted.

The model is scripted (ADR-014) and everything else is real: a real
``AgentLoop``, a real ``ToolRegistry`` over real file tools in a real
temporary workspace, the real permission policy, the real factory. A test
that faked the loop would be testing a second implementation of the thing
the design says does not exist.
"""

import json
from pathlib import Path
from typing import Any

import pytest
from harness.context import RunContext
from harness.loop import AgentLoop
from harness.permissions import PermissionMode, build_policy
from harness.subagents import (
    Capability,
    ConfigurableFactory,
    DelegateTool,
    DelegationStatus,
    DelegationTask,
    ExecutionBudget,
    build_catalog,
    capability_of,
)
from harness.subagents.roles import AgentCatalog, builtin_specs
from harness.tools import ToolRegistry, build_registry
from tests.fakes import BlockingLLM, RoutedLLM, ScriptedLLM, call, text

# A result the researcher's contract accepts, as a model would emit it.
RESEARCH_JSON = (
    '{"answer": "two lines", "evidence": [{"claim": "two lines", '
    '"source": "notes.md", "kind": "fact"}], "uncertainties": [], '
    '"confidence": "high"}'
)

# The same for the writer.
WRITE_JSON = '{"summary": "wrote it", "files_written": ["out.md"], "verified": true}'


def fenced(payload: str, prose: str = "Done.") -> str:
    """Return a child answer in the shape the contract asks for."""
    return f"{prose}\n```json\n{payload}\n```"


@pytest.fixture
def parent(workspace: Path) -> RunContext:
    """A parent run that has already started a turn, as the CLI leaves it."""
    context = RunContext(task="")
    context.start_turn("the user's request")
    return context


@pytest.fixture
def parent_registry(workspace: Path) -> ToolRegistry:
    """The parent's tools: the eight built-ins over a real workspace."""
    (workspace / "notes.md").write_text("alpha\nbeta\n", encoding="utf-8")
    return build_registry(workspace)


def make_factory(
    registry: ToolRegistry,
    parent: RunContext,
    responses: list[Any],
    *,
    mode: PermissionMode = "allow_all",
    budget: ExecutionBudget | None = None,
    catalog: AgentCatalog | None = None,
    artifacts_dir: Path | None = None,
    parent_max_iterations: int = 0,
    repair_attempts: int = 1,
    max_depth: int = 1,
    max_concurrency: int = 1,
    clock: Any = None,
) -> ConfigurableFactory:
    """Build a factory over a scripted model and the real everything else."""
    return ConfigurableFactory(
        llm=ScriptedLLM(list(responses)),
        parent_registry=registry,
        parent_context=parent,
        catalog=catalog if catalog is not None else build_catalog(budget),
        parent_permission_mode=mode,
        artifacts_dir=artifacts_dir,
        parent_max_iterations=parent_max_iterations,
        repair_attempts=repair_attempts,
        max_depth=max_depth,
        max_concurrency=max_concurrency,
        clock=clock,
    )


# -- the catalog is configuration, not code --------------------------------


def test_the_specialists_differ_in_role_tools_permissions_and_limits() -> None:
    """ "Specialized" has to mean something on every axis the brief lists."""
    researcher, writer, critic = builtin_specs()

    assert researcher.instructions != writer.instructions != critic.instructions
    assert researcher.capabilities != writer.capabilities != critic.capabilities
    assert writer.permission_mode == "allow_all"
    assert researcher.permission_mode == critic.permission_mode == "deny_mutations"
    assert researcher.result_model is not writer.result_model
    # The critic is given a smaller allowance than the researcher: it reads a
    # bounded artifact rather than searching.
    assert critic.budget.max_iterations < researcher.budget.max_iterations


# -- capabilities classify every tool that can exist -----------------------


def test_every_registered_tool_has_exactly_one_capability(
    agent_registry: ToolRegistry,
) -> None:
    """The classification is total over the shipped tool set.

    A capability system with a hole in it is not a weaker guarantee, it is
    no guarantee: the hole is a tool that no specification mentions and
    therefore nothing refuses. Asserted against the production assembler's
    registry so a tool added later is covered without editing this test.
    """
    for name in agent_registry.names():
        assert isinstance(capability_of(agent_registry.get(name)), Capability)


# -- what a child is built with --------------------------------------------


def test_a_child_is_an_agent_loop(
    parent_registry: ToolRegistry, parent: RunContext
) -> None:
    """The seam's promise, checked against the thing it returns.

    ``create`` returning an ``AgentLoop`` is the assertion that nobody wrote
    a second agent runtime for sub-agents.
    """
    factory = make_factory(parent_registry, parent, [])

    assert isinstance(factory.create("researcher"), AgentLoop)


def test_a_child_registry_never_contains_delegate(
    parent_registry: ToolRegistry, parent: RunContext, workspace: Path
) -> None:
    """The recursion guard, asserted where it actually holds.

    ``delegate`` is on the parent registry, and the capability filter
    removes it from every child, because no specification grants the
    capability that governs it.
    """
    factory = make_factory(parent_registry, parent, [])
    parent_registry.register(DelegateTool(factory, workspace))

    for role in ("researcher", "writer", "critic"):
        spec = factory.catalog.get(role)
        assert spec is not None
        assert "delegate" not in factory.prepare(spec).registry.names()


def test_the_researcher_holds_no_tool_that_can_change_anything(
    parent_registry: ToolRegistry, parent: RunContext
) -> None:
    """Its registry, not its prompt, is what stops it writing."""
    factory = make_factory(parent_registry, parent, [])
    spec = factory.catalog.get("researcher")
    assert spec is not None

    child = factory.prepare(spec)

    assert all(
        child.registry.get(name).risk == "observe" for name in child.registry.names()
    )


# -- isolation --------------------------------------------------------------


def test_a_child_receives_the_envelope_and_nothing_else(
    parent_registry: ToolRegistry, parent: RunContext
) -> None:
    """The isolation boundary, asserted on what the model was actually sent.

    The parent's conversation contains a secret the child has no business
    knowing. What the child's first request carries is its own system
    prompt and the envelope — so the check is not "we did not mean to copy
    the history" but "the history is not in the bytes that left".
    """
    parent.add_assistant_message("the user's API key is hunter2")
    script = ScriptedLLM([text(fenced(RESEARCH_JSON))])
    factory = ConfigurableFactory(
        llm=script,
        parent_registry=parent_registry,
        parent_context=parent,
        catalog=build_catalog(),
    )

    factory.delegate(DelegationTask(role="researcher", task="count the lines"))

    messages, _ = script.calls[0]
    assert len(messages) == 2
    assert messages[0]["role"] == "system"
    assert messages[1]["content"].startswith("TASK: count the lines")
    assert "hunter2" not in json.dumps(messages)


def test_a_child_cannot_mutate_the_parents_context(
    parent_registry: ToolRegistry, parent: RunContext
) -> None:
    """Communication is task down, result up — never a shared buffer.

    The parent's message list is untouched by the delegation itself. What
    eventually reaches it is the tool observation the *loop* appends, which
    is the parent's own machinery doing what it does for every tool.
    """
    before = list(parent.messages)
    factory = make_factory(
        parent_registry,
        parent,
        [call("read_file", path="notes.md"), text(fenced(RESEARCH_JSON))],
    )

    factory.delegate(DelegationTask(role="researcher", task="count the lines"))

    assert parent.messages == before
    assert parent.prompt_tokens == 0
    assert parent.role == "main"


def test_only_the_condensed_result_reaches_the_parent(
    parent_registry: ToolRegistry, parent: RunContext, workspace: Path
) -> None:
    """The economic argument for delegating, made checkable.

    The child reads a file whose contents are bulky and irrelevant to the
    parent. What the parent's observation contains is the finding; what it
    does not contain is a single line of the file.
    """
    (workspace / "notes.md").write_text("SECRET-BULK\n" * 200, encoding="utf-8")
    factory = make_factory(
        parent_registry,
        parent,
        [call("read_file", path="notes.md"), text(fenced(RESEARCH_JSON))],
    )
    tool = DelegateTool(factory, workspace)

    observation = tool.invoke({"role": "researcher", "task": "count the lines"})

    assert observation.ok
    assert "SECRET-BULK" not in observation.text
    assert "two lines" in observation.text


# -- identity and the execution tree ---------------------------------------


# -- the happy path ---------------------------------------------------------


# -- permissions ------------------------------------------------------------


def test_a_child_cannot_call_a_tool_its_capabilities_exclude(
    parent_registry: ToolRegistry, parent: RunContext, workspace: Path
) -> None:
    """The refusal is the runtime's, and it is counted.

    The researcher is *given* the chance to misbehave: the script has it
    call ``write_file``. The assertions are that the file does not appear
    and that the model was told why, which is what turns a refusal into a
    recoverable turn rather than a silent nothing.

    Note *which* lock stops it. The tool is not in the child's registry, so
    the refusal comes from the registry before the policy is ever
    consulted — the capability filter is the outer one and it is the one
    that fires. The policy's redundant check is exercised in the test
    below, where a tool is present and still ungranted.
    """
    factory = make_factory(
        parent_registry,
        parent,
        [
            call("write_file", path="sneaky.md", content="I was here"),
            text(fenced(RESEARCH_JSON)),
        ],
    )
    spec = factory.catalog.get("researcher")
    assert spec is not None
    child = factory.prepare(spec)
    child.context.start_turn("write a file")

    child.loop.run(child.context)

    assert not (workspace / "sneaky.md").exists()
    assert "write_file" not in child.registry.names()
    assert "unknown tool" in str(child.context.messages[-2]["content"])


def test_a_specialist_cannot_be_wider_than_the_session(
    parent_registry: ToolRegistry, parent: RunContext, workspace: Path
) -> None:
    """Delegation never escalates.

    The writer asks for ``confirm_mutations``; the session is
    ``deny_mutations``. The narrower of the two wins, so a read-only
    session cannot acquire a writing agent by delegating to one — which is
    the whole point of taking a minimum rather than letting a specification
    state its own mode absolutely (ADR-086).
    """
    factory = make_factory(
        parent_registry,
        parent,
        [call("write_file", path="out.md", content="hi"), text(fenced(WRITE_JSON))],
        mode="deny_mutations",
    )
    spec = factory.catalog.get("writer")
    assert spec is not None
    child = factory.prepare(spec)
    child.context.start_turn("write out.md")

    child.loop.run(child.context)

    assert child.runtime.permission_mode == "deny_mutations"
    assert not (workspace / "out.md").exists()


def test_a_writers_change_is_put_to_the_person_at_the_terminal(
    parent_registry: ToolRegistry, parent: RunContext, workspace: Path
) -> None:
    """A child's writes are confirmed by the same person, on the same terms.

    The session is ``confirm_mutations``, so the writer is too — and the
    confirmer is the parent's own. Delegating therefore does not route a
    change around the human gate; it puts the child's change through it.
    That is the property that would be easy to lose, because a sub-agent
    with its own confirmer, or none, would be a way to write files without
    anybody agreeing to it.
    """
    approved: list[str] = []

    class Approving:
        """A confirmer that says yes and records what it was asked."""

        def confirm(self, tool: Any, arguments: dict[str, object]) -> bool:
            """Approve, and remember the tool name."""
            approved.append(tool.name)
            return True

    factory = ConfigurableFactory(
        llm=ScriptedLLM(
            [
                call("write_file", path="out.md", content="hi"),
                text(fenced(WRITE_JSON)),
            ]
        ),
        parent_registry=parent_registry,
        parent_context=parent,
        catalog=build_catalog(),
        parent_permission_mode="confirm_mutations",
        confirmer=Approving(),
    )

    result = factory.delegate(DelegationTask(role="writer", task="write hi to out.md"))

    assert result.status is DelegationStatus.COMPLETED
    assert approved == ["write_file"]
    assert (workspace / "out.md").read_text(encoding="utf-8") == "hi"


def test_a_researchers_web_search_is_put_to_the_same_person(
    parent_registry: ToolRegistry, parent: RunContext, workspace: Path
) -> None:
    """Delegating cannot make the web quieter than the session it runs in.

    The researcher's own mode is ``deny_mutations`` — it may not write —
    and on its own that lets open-world reads through. The factory is
    handed the parent session's answer for open-world reads, so under a
    ``confirm_mutations`` session the child's search prompts exactly as the
    parent's would (ADR-102).
    """
    from harness.permissions import open_world_outcome_for
    from harness.tools.base import Tool, ToolResult

    class FakeWebSearch(Tool):
        """A read-only tool that reaches outside the sandbox."""

        name = "web__web_search"
        description = "Searches the web."
        parameters: dict[str, Any] = {"type": "object", "properties": {}}
        risk = "observe"
        reach = "open_world"

        def _run(self, arguments: dict[str, Any]) -> ToolResult:
            return ToolResult("one result")

    parent_registry.register(FakeWebSearch(workspace))
    asked: list[str] = []

    class Approving:
        """A confirmer that says yes and records what it was asked."""

        def confirm(self, tool: Any, arguments: dict[str, object]) -> bool:
            """Approve, and remember the tool name."""
            asked.append(tool.name)
            return True

    factory = ConfigurableFactory(
        llm=ScriptedLLM([call("web__web_search"), text(fenced(RESEARCH_JSON))]),
        parent_registry=parent_registry,
        parent_context=parent,
        catalog=build_catalog(),
        parent_permission_mode="confirm_mutations",
        open_world_outcome=open_world_outcome_for("confirm_mutations"),
        confirmer=Approving(),
    )

    result = factory.delegate(DelegationTask(role="researcher", task="look it up"))

    assert result.status is DelegationStatus.COMPLETED
    assert asked == ["web__web_search"]


def test_a_writer_whose_change_is_declined_does_not_write(
    parent_registry: ToolRegistry, parent: RunContext, workspace: Path
) -> None:
    """Nobody to ask means no, for a child exactly as for the parent.

    This is the shipped default in an unattended run, and it is the half of
    the confirmation gate that would be easy to get wrong: a sub-agent that
    treated "no terminal" as approval would be a way of writing files
    without anyone agreeing to it.
    """
    factory = make_factory(
        parent_registry,
        parent,
        [call("write_file", path="out.md", content="hi"), text(fenced(WRITE_JSON))],
        mode="confirm_mutations",
    )

    factory.delegate(DelegationTask(role="writer", task="write hi to out.md"))

    assert not (workspace / "out.md").exists()


# -- budgets ----------------------------------------------------------------


def build_policy_allowing_everything() -> Any:
    """Return a permission policy that allows every call."""
    from harness.permissions import AllowAllPolicy

    return AllowAllPolicy()


def test_a_childs_budget_is_clamped_to_the_parents_remainder(
    parent_registry: ToolRegistry, parent: RunContext
) -> None:
    """A child never outlives the guard that bounds the whole run.

    The specialist asks for eight steps; the parent is capped at ten and has
    used seven. Three is what is left, and three is what the child gets.
    """
    parent.iterations_used = 7
    factory = make_factory(
        parent_registry,
        parent,
        [],
        budget=ExecutionBudget(max_iterations=8),
        parent_max_iterations=10,
    )
    spec = factory.catalog.get("researcher")
    assert spec is not None

    assert factory.prepare(spec).runtime.budget.max_iterations == 3


# -- running several at once ------------------------------------------------


def test_independent_delegations_run_concurrently(
    parent_registry: ToolRegistry, parent: RunContext
) -> None:
    """The point of the whole change, proved rather than assumed.

    Two results coming back says nothing about *how* they came back: fast
    sequential execution looks identical. So the model waits at a barrier
    that only releases when two children have reached it. If the runtime ran
    them one after another the first would wait alone until the barrier
    timed out, and this test would fail rather than quietly pass.
    """
    routed = RoutedLLM(
        {
            "count the lines": [text(fenced(RESEARCH_JSON))],
            "name the file": [text(fenced(RESEARCH_JSON))],
        }
    )
    factory = ConfigurableFactory(
        llm=BlockingLLM(routed, parties=2),
        parent_registry=parent_registry,
        parent_context=parent,
        catalog=build_catalog(),
        max_concurrency=2,
    )

    batch = factory.delegate_batch(
        [
            DelegationTask(role="researcher", task="count the lines"),
            DelegationTask(role="researcher", task="name the file"),
        ]
    )

    assert [result.status for result in batch.results] == [
        DelegationStatus.COMPLETED,
        DelegationStatus.COMPLETED,
    ]
    assert set(routed.seen) == {"count the lines", "name the file"}
    # Distinct runs, not one run reported twice.
    assert len({result.child_run_id for result in batch.results}) == 2


def test_results_come_back_in_the_order_they_were_asked_for(
    parent_registry: ToolRegistry, parent: RunContext
) -> None:
    """Whoever finishes first, the parent reads its own list back.

    Ordering by completion would make the same call produce different text
    on different runs, which is a bad property for something a model reads
    and a worse one for a test suite.
    """
    routed = RoutedLLM(
        {
            "first task": [text(fenced(RESEARCH_JSON))],
            "second task": [text(fenced(RESEARCH_JSON))],
            "third task": [text(fenced(RESEARCH_JSON))],
        }
    )
    factory = ConfigurableFactory(
        llm=routed,
        parent_registry=parent_registry,
        parent_context=parent,
        catalog=build_catalog(),
        max_concurrency=3,
    )

    batch = factory.delegate_batch(
        [
            DelegationTask(role="researcher", task="first task"),
            DelegationTask(role="researcher", task="second task"),
            DelegationTask(role="researcher", task="third task"),
        ]
    )

    observation = batch.as_observation()
    assert observation.index("1 of 3") < observation.index("2 of 3")
    assert observation.index("2 of 3") < observation.index("3 of 3")


def test_the_batch_shares_the_parents_remaining_iterations(
    parent_registry: ToolRegistry, parent: RunContext
) -> None:
    """The guard fan-out would otherwise break, and the reason it must not.

    A child's budget is clamped to what the parent has left. Clamp three
    children to the *same* remainder and between them they can spend three
    times it, so the bound that was supposed to hold for the whole run holds
    for none of it. Nine steps left, three children, three steps each.
    """
    parent.iterations_used = 1
    routed = RoutedLLM(
        {f"task {index}": [text(fenced(RESEARCH_JSON))] for index in range(1, 4)}
    )
    factory = ConfigurableFactory(
        llm=routed,
        parent_registry=parent_registry,
        parent_context=parent,
        catalog=build_catalog(ExecutionBudget(max_iterations=8)),
        parent_max_iterations=10,
        max_concurrency=3,
        artifacts_dir=None,
    )

    ceiling = factory._share_of_ceiling(3)

    assert ceiling is not None
    assert ceiling.max_iterations == 3
    # And the sum of what the children may spend does not exceed what the
    # parent had left, which is the property the division exists for.
    assert ceiling.max_iterations * 3 <= 9


def test_a_batch_the_parent_cannot_afford_is_refused_whole(
    parent_registry: ToolRegistry, parent: RunContext
) -> None:
    """Two steps left and three children asked for: none of them run.

    Starting some of the batch and refusing the rest by list position would
    be arbitrary and hard to explain to the model. Refusing all three tells
    it the one thing it can act on: ask for fewer.
    """
    parent.iterations_used = 8
    factory = make_factory(
        parent_registry, parent, [], parent_max_iterations=10, max_concurrency=3
    )

    batch = factory.delegate_batch(
        [DelegationTask(role="researcher", task=f"task {n}") for n in range(3)]
    )

    assert all(result.status is DelegationStatus.REJECTED for result in batch.results)
    assert "Delegate fewer tasks" in str(batch.results[0].error)
    assert not batch.ok


def test_one_bad_role_does_not_stop_its_siblings(
    parent_registry: ToolRegistry, parent: RunContext
) -> None:
    """Partial failure is a normal outcome of a batch, not a failed batch.

    The rejection is decided before anything starts, so the task naming a
    role that does not exist costs the others nothing — not a share of the
    budget, not a worker, not their results.
    """
    routed = RoutedLLM({"real work": [text(fenced(RESEARCH_JSON))]})
    factory = ConfigurableFactory(
        llm=routed,
        parent_registry=parent_registry,
        parent_context=parent,
        catalog=build_catalog(),
        max_concurrency=2,
    )

    batch = factory.delegate_batch(
        [
            DelegationTask(role="archaeologist", task="dig"),
            DelegationTask(role="researcher", task="real work"),
        ]
    )

    assert batch.results[0].status is DelegationStatus.REJECTED
    assert "unknown sub-agent role" in str(batch.results[0].error)
    assert batch.results[1].status is DelegationStatus.COMPLETED
    # A rejection never entered RUNNING and never got an id.
    assert batch.results[0].child_run_id == ""
    assert DelegationStatus.RUNNING not in batch.results[0].transitions
    # One failure makes the whole observation a failure for the loop's guard.
    assert not batch.ok


def test_a_width_of_one_is_the_sequential_path(
    parent_registry: ToolRegistry, parent: RunContext
) -> None:
    """The rollback, and the promise that it is a real one.

    ``subagent_max_concurrency: 1`` must not be "concurrency with one
    worker": it is the code that ran before there was a pool, on the calling
    thread, which is what makes it a safe thing to fall back to.
    """
    routed = RoutedLLM(
        {"only task": [text(fenced(RESEARCH_JSON))]},
    )
    factory = ConfigurableFactory(
        llm=routed,
        parent_registry=parent_registry,
        parent_context=parent,
        catalog=build_catalog(),
        max_concurrency=1,
    )

    batch = factory.delegate_batch(
        [DelegationTask(role="researcher", task="only task")]
    )

    assert len(batch.results) == 1
    assert batch.results[0].status is DelegationStatus.COMPLETED
    # One result renders exactly as it did before batching existed: no
    # header, no numbering, because the traces and the prompt describe that.
    assert batch.as_observation() == batch.results[0].as_observation()
    assert "delegation batch" not in batch.as_observation()


def test_concurrent_children_keep_their_own_contexts(
    parent_registry: ToolRegistry, parent: RunContext
) -> None:
    """Isolation survives the pool.

    The property delegation is worth having for — a child sees only its
    envelope — is structural, but "structural" was argued when children ran
    one at a time. This asserts it on the bytes each concurrent child was
    actually sent.
    """
    routed = RoutedLLM(
        {
            "alpha task": [text(fenced(RESEARCH_JSON))],
            "beta task": [text(fenced(RESEARCH_JSON))],
        }
    )
    factory = ConfigurableFactory(
        llm=BlockingLLM(routed, parties=2),
        parent_registry=parent_registry,
        parent_context=parent,
        catalog=build_catalog(),
        max_concurrency=2,
    )

    factory.delegate_batch(
        [
            DelegationTask(role="researcher", task="alpha task", context="SECRET-A"),
            DelegationTask(role="researcher", task="beta task", context="SECRET-B"),
        ]
    )

    for messages, _tools in routed.calls:
        rendered = json.dumps(messages)
        # Each conversation holds its own secret and never its sibling's.
        assert ("SECRET-A" in rendered) != ("SECRET-B" in rendered)
    # And the parent is untouched by either.
    assert "SECRET-A" not in json.dumps(parent.messages)


# -- failures ---------------------------------------------------------------


def test_a_child_cannot_delegate_again(
    parent_registry: ToolRegistry, parent: RunContext
) -> None:
    """The depth guard, which is the second lock on the recursion door.

    The first is structural and cannot be reached from a child at all. This
    one exists so that a caller which somehow does reach the runtime gets a
    readable refusal instead of an unknown-tool error.
    """
    parent.depth = 1
    factory = make_factory(parent_registry, parent, [], max_depth=1)

    result = factory.delegate(DelegationTask(role="researcher", task="count"))

    assert result.status is DelegationStatus.REJECTED
    assert "maximum delegation depth" in str(result.error)


def test_an_unvalidated_answer_never_becomes_a_finding(
    parent_registry: ToolRegistry, parent: RunContext
) -> None:
    """The contract gate, and the wording that makes it safe.

    The child produced prose with no JSON. What reaches the parent says
    plainly that this is a failed attempt rather than a result — because
    the failure mode being prevented is a coordinator quoting a sub-agent's
    unvalidated guess as evidence.
    """
    factory = make_factory(
        parent_registry,
        parent,
        [text("I think it is about two lines."), text("Still no JSON, sorry.")],
    )

    result = factory.delegate(DelegationTask(role="researcher", task="count"))

    assert result.status is DelegationStatus.INVALID_OUTPUT
    assert result.payload is None
    assert not result.ok
    assert "unvalidated" in str(result.error)
    assert "I think it is about two lines." in result.summary


def test_a_child_that_reasons_past_its_deadline_times_out(
    parent_registry: ToolRegistry, parent: RunContext
) -> None:
    """The half of the timeout the permission gate could never see.

    The deadline used to be checked in one place: on the way into a tool
    call. A child that stopped calling tools and kept reasoning was never
    checked again, so it ran until its iteration cap and ``TIMED_OUT`` was a
    status the runtime could not produce. This child calls no tools at all.
    """

    class _Clock:
        """A monotonic clock that jumps forward on its second reading."""

        def __init__(self) -> None:
            self.now = 1_000.0
            self.reads = 0

        def __call__(self) -> float:
            self.reads += 1
            # The first read sets the deadline; the next one is already past
            # it, which is a timeout expressed without sleeping.
            if self.reads > 1:
                self.now += 500.0
            return self.now

    factory = make_factory(
        parent_registry,
        parent,
        [text(fenced(RESEARCH_JSON))],
        budget=ExecutionBudget(max_iterations=8, timeout_seconds=30.0),
        clock=_Clock(),
    )

    result = factory.delegate(DelegationTask(role="researcher", task="count"))

    assert result.status is DelegationStatus.TIMED_OUT
    assert result.payload is None
    assert not result.ok
    assert "ran out of time" in str(result.error)


def test_a_disabled_timeout_does_not_expire_anything(
    parent_registry: ToolRegistry, parent: RunContext
) -> None:
    """``timeout_seconds: 0`` leaves the client unwrapped, as it always was."""
    factory = make_factory(
        parent_registry,
        parent,
        [text(fenced(RESEARCH_JSON))],
        budget=ExecutionBudget(max_iterations=8, timeout_seconds=0.0),
    )
    spec = factory.catalog.get("researcher")
    assert spec is not None

    assert factory.prepare(spec).deadline is None
    result = factory.delegate(DelegationTask(role="researcher", task="count"))
    assert result.status is DelegationStatus.COMPLETED


# -- the artifact plane -----------------------------------------------------


def test_every_child_of_a_batch_gets_its_own_record(
    parent_registry: ToolRegistry, parent: RunContext, workspace: Path
) -> None:
    """The data plane keeps up with the fan-out.

    Each child writes its own file, named by its own run id, holding its own
    envelope and terms. A batch that wrote one merged record would lose the
    per-child accounting the observability requirement asks for.
    """
    artifacts = workspace / "delegations"
    routed = RoutedLLM(
        {
            "alpha task": [text(fenced(RESEARCH_JSON))],
            "beta task": [text(fenced(RESEARCH_JSON))],
        }
    )
    factory = ConfigurableFactory(
        llm=routed,
        parent_registry=parent_registry,
        parent_context=parent,
        catalog=build_catalog(),
        artifacts_dir=artifacts,
        max_concurrency=2,
    )

    batch = factory.delegate_batch(
        [
            DelegationTask(role="researcher", task="alpha task"),
            DelegationTask(role="researcher", task="beta task"),
        ]
    )

    written = sorted(path.name for path in artifacts.glob("*.json"))
    assert written == sorted(f"{result.child_run_id}.json" for result in batch.results)
    for result in batch.results:
        assert result.artifact_path is not None
        record = json.loads(Path(result.artifact_path).read_text(encoding="utf-8"))
        assert record["result"]["child_run_id"] == result.child_run_id
        assert "tool_calls_refused_for_capability" in record


# -- the tool the model sees ------------------------------------------------


def test_the_tool_refuses_a_batch_wider_than_the_limit(
    parent_registry: ToolRegistry, parent: RunContext, workspace: Path
) -> None:
    """The cap is enforced where the model can read the reason.

    Refusing at the schema would fail validation with a bare field name.
    Refused here, the model is told the number and what to do instead.
    """
    factory = make_factory(parent_registry, parent, [], max_concurrency=2)
    tool = DelegateTool(factory, workspace)

    outcome = tool.invoke(
        {"tasks": [{"role": "researcher", "task": f"task {n}"} for n in range(3)]}
    )

    assert not outcome.ok
    assert "at most 2 sub-agents" in outcome.text


def test_the_tool_refuses_both_shapes_at_once(
    parent_registry: ToolRegistry, parent: RunContext, workspace: Path
) -> None:
    """A call that says two things is a call the runtime must not guess at."""
    factory = make_factory(parent_registry, parent, [], max_concurrency=2)
    tool = DelegateTool(factory, workspace)

    outcome = tool.invoke(
        {
            "role": "researcher",
            "task": "one thing",
            "tasks": [{"role": "researcher", "task": "another thing"}],
        }
    )

    assert not outcome.ok
    assert "not both" in outcome.text


def test_the_batch_field_is_hidden_when_it_could_not_be_used(
    parent_registry: ToolRegistry, parent: RunContext, workspace: Path
) -> None:
    """At width one the model is not offered a list it cannot fan out.

    The tool set is the model's contract, and an argument that silently has
    no effect is worse than an absent one: it invites a call shape that
    looks accepted and is not what the session will do.
    """
    narrow = DelegateTool(
        make_factory(parent_registry, parent, [], max_concurrency=1), workspace
    )
    wide = DelegateTool(
        make_factory(parent_registry, parent, [], max_concurrency=3), workspace
    )

    assert "tasks" not in narrow.parameters["properties"]
    assert narrow.parameters["required"] == ["role", "task"]
    assert "tasks" in wide.parameters["properties"]
    assert wide.parameters["properties"]["tasks"]["maxItems"] == 3
    assert "at once" in wide.description


def test_delegation_passes_the_parents_permission_gate(
    parent_registry: ToolRegistry, parent: RunContext, workspace: Path
) -> None:
    """Handing work to a writer is a mutating act, and is policed as one.

    ``delegate`` is dispatched through ``AgentLoop._execute`` like every
    other tool, so a ``deny_mutations`` session refuses it at the parent's
    own gate — before any child exists. That is the consistency §8.4 asks
    for rather than a sub-agent-shaped exception (ADR-057).
    """
    from harness.permissions import build_policy

    factory = make_factory(
        parent_registry, parent, [call("delegate", role="writer", task="x"), text("ok")]
    )
    parent_registry.register(DelegateTool(factory, workspace))
    loop = AgentLoop(
        llm=ScriptedLLM(
            [call("delegate", role="writer", task="write out.md"), text("refused")]
        ),
        registry=parent_registry,
        policy=build_policy("deny_mutations"),
    )
    context = RunContext(task="")
    context.start_turn("delegate a write")

    loop.run(context)

    assert "not permitted" in str(context.messages[-2]["content"])
    assert not (workspace / "out.md").exists()


# -- how the loop reports itself -------------------------------------------


def test_concurrent_children_agree_on_who_their_parent_is(
    parent_registry: ToolRegistry, parent: RunContext
) -> None:
    """Sibling children must name the same parent run.

    Reading the parent's id is the one place a child touches the parent
    context, and it is not a pure read: it mints an id when the parent has
    none, which the CLI's first turn does have but a directly built factory
    may not. Three workers racing that would produce three different parents
    for three siblings, and the execution tree §8.4 asks for would be three
    trees.
    """
    bare = RunContext(task="")
    bare.start_turn("go")
    bare.run_id = ""  # no id yet, as a caller that built one by hand leaves it
    routed = RoutedLLM({f"task {n}": [text(fenced(RESEARCH_JSON))] for n in range(3)})
    factory = ConfigurableFactory(
        llm=routed,
        parent_registry=parent_registry,
        parent_context=bare,
        catalog=build_catalog(),
        max_concurrency=3,
    )

    batch = factory.delegate_batch(
        [DelegationTask(role="researcher", task=f"task {n}") for n in range(3)]
    )

    parents = {result.parent_run_id for result in batch.results}
    assert len(parents) == 1, f"siblings disagreed about their parent: {parents}"
    assert parents == {bare.run_id}


# --- a read-only hand-over needs no approval (ADR-119) -----------------------


@pytest.mark.parametrize(
    ("arguments", "risk"),
    [
        ({"role": "researcher", "task": "t"}, "observe"),
        ({"role": "critic", "task": "t"}, "observe"),
        (
            {
                "tasks": [
                    {"role": "researcher", "task": "a"},
                    {"role": "critic", "task": "b"},
                ]
            },
            "observe",
        ),
        ({"role": "writer", "task": "t"}, "mutate"),
        (
            {
                "tasks": [
                    {"role": "researcher", "task": "a"},
                    {"role": "writer", "task": "b"},
                ]
            },
            "mutate",
        ),
        ({"role": "nobody", "task": "t"}, "mutate"),
        ({"tasks": ["not an object"]}, "mutate"),
        ({}, "mutate"),
    ],
)
def test_a_delegation_is_as_risky_as_its_riskiest_specialist(
    parent_registry: ToolRegistry,
    parent: RunContext,
    workspace: Path,
    arguments: dict[str, Any],
    risk: str,
) -> None:
    """Read-only specialists make a read-only call; anything unclear does not."""
    tool = DelegateTool(make_factory(parent_registry, parent, []), workspace)

    assert tool.risk_for(arguments) == risk
    assert tool.risk == "mutate", "what the tool CAN do is unchanged"


@pytest.mark.parametrize("mode", ["confirm_mutations", "deny_mutations"])
def test_the_gate_asks_about_a_writer_but_not_about_researchers(
    parent_registry: ToolRegistry,
    parent: RunContext,
    workspace: Path,
    mode: PermissionMode,
) -> None:
    """The prompt that stood in front of every run-time fan-out is gone.

    A researcher's mode is ``min(session, deny_mutations)``, so asking
    before handing it a reading task protected nothing. A writer still gets
    the session's own answer, whatever the mode (ADR-119).
    """
    tool = DelegateTool(make_factory(parent_registry, parent, []), workspace)
    policy = build_policy(mode)

    assert policy.decide(tool, {"role": "researcher", "task": "t"}) == "allow"
    assert policy.decide(tool, {"role": "writer", "task": "t"}) == (
        "require_user_confirmation" if mode == "confirm_mutations" else "deny"
    )


def test_a_configured_rule_still_outranks_the_arguments(
    parent_registry: ToolRegistry, parent: RunContext, workspace: Path
) -> None:
    """An operator who tiered ``delegate`` themselves keeps that answer."""
    from harness.permissions import PermissionRules

    tool = DelegateTool(make_factory(parent_registry, parent, []), workspace)
    policy = build_policy("confirm_mutations", rules=PermissionRules(ask=["delegate"]))

    assert policy.decide(tool, {"role": "researcher", "task": "t"}) == (
        "require_user_confirmation"
    )


# --- a writer never shares a batch (ADR-120) ---------------------------------


@pytest.mark.parametrize(
    "others",
    [["critic"], ["researcher"], ["critic", "researcher"]],
)
def test_a_writer_cannot_run_alongside_other_tasks(
    parent_registry: ToolRegistry,
    parent: RunContext,
    workspace: Path,
    others: list[str],
) -> None:
    """The critic reviewed summary.md before the writer had created it.

    Observed on the real model; refused now before any child starts, with
    an instruction the model can follow.
    """
    factory = make_factory(parent_registry, parent, [], max_concurrency=3)
    tool = DelegateTool(factory, workspace)
    tasks = [{"role": "writer", "task": "write summary.md"}] + [
        {"role": role, "task": f"{role} work"} for role in others
    ]

    outcome = tool.invoke({"tasks": tasks})

    assert not outcome.ok
    assert "cannot run in the same batch" in outcome.text
    assert "on its own first" in outcome.text
    assert not (workspace / "summary.md").exists()


def test_read_only_specialists_may_still_share_a_batch(
    parent_registry: ToolRegistry, parent: RunContext, workspace: Path
) -> None:
    """The rule is about writing, not about batching: readers still fan out."""
    factory = make_factory(parent_registry, parent, [], max_concurrency=3)
    tool = DelegateTool(factory, workspace)

    tasks, error = tool._envelopes(
        {
            "tasks": [
                {"role": "researcher", "task": "read a"},
                {"role": "critic", "task": "review b"},
            ]
        }
    )

    assert error is None and len(tasks) == 2
