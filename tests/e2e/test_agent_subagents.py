"""**Real model.** The shipped image delegates, and the record proves it.

Everything below the model is already proven elsewhere: ``test_subagents.py``
that the factory is correct, ``test_week3_delegation.py`` that the real
``cli.main()`` wires it up and records it. Between those and a demo sit the
Dockerfile, the non-root user, the ``agent-harness-memory`` volume, the tool schema
as a real endpoint renders it — and a model nobody scripted, which is the
part that decides whether ``delegate`` is a tool a mid-sized model can
actually use. A ``delegate`` tool that only a scripted model ever calls is a
feature with no evidence behind it (ADR-074, ADR-084).

What is asserted, and what is deliberately not: the tests below check that
the *work* got done, and — where delegation did happen — that the record is
a well-formed parent/child tree. They do not require the model to delegate on
any particular turn. A coordinator that reads two small files itself instead
of delegating has made a reasonable call, and a test that failed it would be
testing the model's taste rather than the harness's correctness. The one
place a hard assertion belongs is the *shape of the evidence*, because that
is the harness's job and not the model's (ADR-078).
"""

import json
from typing import Any

import pytest
from tests.e2e.conftest import MEMORY, WORKSPACE, Stack

pytestmark = pytest.mark.e2e

TRACES = f"{MEMORY}/traces"
DELEGATIONS = f"{TRACES}/delegations"

# Enough source material that a coordinator has a reason to hand the reading
# to someone else, and one distinctive token so "this never entered the
# parent's answer" is checkable.
LEDGER = "ENTRY 9F2 amount 10 currency EUR\n" * 120


def events(stack: Stack) -> list[dict[str, Any]]:
    """Return the parsed events of the single trace file this run wrote."""
    files = [path for path in stack.listing(TRACES) if path.endswith(".jsonl")]
    assert len(files) == 1, f"expected one trace file, found {files}"
    return [
        json.loads(line) for line in stack.read(files[0]).splitlines() if line.strip()
    ]


def delegation_records(stack: Stack) -> list[dict[str, Any]]:
    """Return every stored delegation record, oldest first."""
    paths = sorted(
        path for path in stack.listing(DELEGATIONS) if path.endswith(".json")
    )
    return [json.loads(stack.read(path)) for path in paths]


def test_the_agent_can_carry_out_a_task_that_invites_delegation(
    stack: Stack,
) -> None:
    """A real task, done, in the image, with sub-agents available.

    The assertion is on the outcome the user asked for. Whether the
    coordinator delegated the reading or did it itself is the model's call
    on the day; what must hold either way is that offering it a
    ``delegate`` tool did not break its ability to finish the job — which is
    the first thing a new tool in the schema can quietly do.
    """
    stack.write(f"{WORKSPACE}/ledger.txt", LEDGER)

    run = stack.run(
        "Read ledger.txt, then write a short summary of what it contains "
        "to brief.md. Say how many entries there are.",
        permissions="allow_all",
    )

    run.assert_completed()
    assert stack.exists(f"{WORKSPACE}/brief.md"), (
        f"no brief.md was written\n{stack.tree()}"
    )
    assert "120" in stack.read(f"{WORKSPACE}/brief.md") or "120" in run.answer


def test_a_delegated_run_is_recorded_under_its_parent(stack: Stack) -> None:
    """When the model does delegate, the evidence is a tree.

    The harness's half of the requirement, checked on the file a TA would
    open after the demo: every child line carries the parent's ``run_id``,
    every child run is nested inside the parent's ``delegate`` call, every
    run that appears in the file also closed, and no child claims to be the
    top-level agent.

    A line is a finished span (ADR-093), so a run is recognised by its
    closing ``run_end`` line — ``agent.run`` for the parent, ``subagent.run``
    for a child — and by ``parent_span_id``, not by a ``run_start`` line,
    which v2 does not write. Asserting on ``run_start`` made this test skip
    on every run, delegation or not, and a test that always skips is a
    requirement with no evidence behind it.

    Skipped rather than failed when the model chose not to delegate. The
    requirement is that delegation *is recorded properly*; a run with no
    delegation is not evidence against that, and turning the model's
    judgement into a red build would make this suite report on the wrong
    thing.
    """
    stack.write(f"{WORKSPACE}/ledger.txt", LEDGER)

    run = stack.run(
        "Use your researcher sub-agent to find out how many entries "
        "ledger.txt has, then tell me the number.",
        permissions="allow_all",
    )
    run.assert_completed()

    recorded = events(stack)
    runs = [event for event in recorded if event["event"] == "run_end"]
    children = [event for event in runs if event["span"] == "subagent.run"]
    if not children:
        pytest.skip(f"the model did not delegate; tools: {run.tools_called}")

    parents = [event for event in runs if event["span"] == "agent.run"]
    assert len(parents) == 1, parents
    parent = parents[0]
    assert parent["parent_run_id"] is None
    assert parent["role"] == "main"

    by_span = {event["span_id"]: event for event in recorded}
    for child in children:
        assert child["parent_run_id"] == parent["run_id"]
        assert child["run_id"] != parent["run_id"]
        assert child["role"] != "main"
        # Structurally the parent's: the child run is a span *inside* the
        # parent's `delegate` tool call, which is what makes "who started
        # this" a fact in the record rather than an inference.
        caller = by_span.get(child["parent_span_id"])
        assert caller is not None, f"child run {child['run_id']} has no parent span"
        assert caller["event"] == "tool" and caller["tool"] == "delegate", caller
        assert caller["run_id"] == parent["run_id"]

    # Identity is inherited down to every span of a child run (ADR-095), so
    # nothing a child did is filed under the parent or under nobody.
    child_ids = {child["run_id"] for child in children}
    for event in recorded:
        if event["run_id"] in child_ids:
            assert event["parent_run_id"] == parent["run_id"], event
            assert event["role"] != "main", event

    # Every run that left any line also left its closing line.
    seen = {event["run_id"] for event in recorded}
    closed = {event["run_id"] for event in runs}
    assert seen == closed, "a child run opened and never closed"


def test_a_delegation_leaves_a_full_record(stack: Stack) -> None:
    """The data plane exists on the volume, not only in a unit test.

    The record is what makes a delegation auditable after the fact: the
    envelope the parent sent, the permissions and tools the child ran
    under, and the validated result. Its absence would mean the parent's
    one-line summary is the only surviving account of what a sub-agent did.
    """
    stack.write(f"{WORKSPACE}/ledger.txt", LEDGER)

    run = stack.run(
        "Use your researcher sub-agent to summarise ledger.txt, then tell "
        "me what it said.",
        permissions="allow_all",
    )
    run.assert_completed()

    records = delegation_records(stack)
    if not records:
        pytest.skip(f"the model did not delegate; tools: {run.tools_called}")

    first = records[0]
    assert first["envelope"]["task"]["task"], "the envelope carried no task"
    assert first["envelope"]["runtime"]["parent_run_id"]
    assert first["envelope"]["runtime"]["role"] in ("researcher", "writer", "critic")
    assert first["result"]["status"] in (
        "completed",
        "invalid_output",
        "timed_out",
        "failed",
    )
    # Whatever the outcome, the child ran under narrower terms than the
    # session: a researcher never holds a mutating tool.
    if first["envelope"]["runtime"]["role"] == "researcher":
        assert "write_file" not in first["envelope"]["runtime"]["tools"]


def test_a_sub_agent_cannot_write_when_the_session_is_read_only(
    stack: Stack,
) -> None:
    """Delegation is not a way around the session's permission mode.

    The strongest safety claim this slice makes, and the one worth spending
    a real model call on: under ``--permissions deny_mutations`` the agent
    is asked to do something that can only be done by writing, and is
    offered a sub-agent that would normally be allowed to write. Nothing is
    written — by the parent or by any child.
    """
    stack.write(f"{WORKSPACE}/ledger.txt", LEDGER)

    run = stack.run(
        "Write a summary of ledger.txt into summary.md. Delegate the "
        "writing to your writer sub-agent if that helps.",
        permissions="deny_mutations",
    )

    assert run.exit_code == 0
    assert not stack.exists(f"{WORKSPACE}/summary.md"), (
        f"a file was written under deny_mutations\n{stack.tree()}"
    )


def test_no_child_run_is_offered_the_delegation_tool(stack: Stack) -> None:
    """The recursion guard, on a real run rather than a scripted one.

    A child's registry is built by intersecting its capabilities with the
    parent's tools, and no specialist holds the delegation capability. The
    trace is where that becomes checkable from outside the process: a
    ``delegate`` tool event under any role but ``main`` would mean a child
    reached it.
    """
    stack.write(f"{WORKSPACE}/ledger.txt", LEDGER)

    run = stack.run(
        "Use your researcher sub-agent to summarise ledger.txt.",
        permissions="allow_all",
    )
    run.assert_completed()

    delegating = {
        event["role"]
        for event in events(stack)
        if event["event"] == "tool" and event["tool"] == "delegate"
    }
    assert delegating <= {"main"}, f"a child delegated: {delegating}"


def test_concurrent_children_are_still_recorded_under_their_parent(
    stack: Stack,
) -> None:
    """The execution tree survives a real fan-out, in the shipped image.

    ``test_week3_delegation.py`` proves this against a scripted model, which
    is where the assertion belongs — it can force the batch. What it cannot
    show is that the *image* does it: a real model choosing the batched shape
    on its own, three children genuinely interleaved on three threads, and
    the trace on the volume still a tree rather than three unrelated runs.

    That distinction is the whole reason the tracer's span stack had to
    become per-thread. With one shared stack every assertion in this file
    still passed for a sequential delegation and quietly produced fiction for
    a concurrent one, because "the innermost open span" stops meaning "mine"
    the moment two children are open at once.

    Skipped rather than failed when the model delegated fewer than two
    children, for the reason this module's docstring gives: what is under
    test is the shape of the evidence, not the model's taste.
    """
    stack.write(f"{WORKSPACE}/alpha.txt", "ENTRY 9F2\n" * 40)
    stack.write(f"{WORKSPACE}/beta.txt", "ENTRY 7C1\n" * 25)
    stack.write(f"{WORKSPACE}/gamma.txt", "ENTRY 4B8\n" * 10)

    run = stack.run(
        "alpha.txt, beta.txt and gamma.txt are three unrelated ledgers. "
        "Have a separate researcher sub-agent count the entries in each of "
        "them at the same time, then tell me all three numbers.",
        permissions="allow_all",
    )
    run.assert_completed()

    recorded = events(stack)
    runs = [event for event in recorded if event["event"] == "run_end"]
    children = [event for event in runs if event["span"] == "subagent.run"]
    if len(children) < 2:
        pytest.skip(
            f"the model delegated {len(children)} children, not a batch; "
            f"tools: {run.tools_called}"
        )

    by_span = {event["span_id"]: event for event in recorded}
    delegations = {
        child["parent_span_id"]
        for child in children
        if by_span.get(child["parent_span_id"], {}).get("tool") == "delegate"
    }
    assert len(delegations) == len({child["parent_span_id"] for child in children}), (
        "a child run hung off something that was not a delegate call"
    )

    # Distinct runs with distinct ids, every one of them filed under the one
    # top-level run. A shared span stack breaks this by nesting a child
    # inside whichever sibling started last.
    parents = [event for event in runs if event["span"] == "agent.run"]
    assert len(parents) == 1, parents
    assert len({child["run_id"] for child in children}) == len(children)
    for child in children:
        assert child["parent_run_id"] == parents[0]["run_id"], child

    # One trace file for the whole tree, and a stored record per child.
    assert len({event["trace_id"] for event in recorded}) == 1
    records = delegation_records(stack)
    assert len(records) >= len(children)


def test_delegation_permission_tools_and_observability_in_one_run(
    stack: Stack,
) -> None:
    """§8.4's combined case: one real run exercises all four at once.

    The brief asks for "at least one end-to-end test covering sub-agent
    delegation, permission enforcement, tool execution, and observability".
    Each is proven on its own above; this is the run where they meet, which
    is where a bug between them would show — a child's tool call missing
    from the parent's metrics, or a denial the trace files under the wrong
    run.

    Delegation has to be allowed for the run to delegate, so the session is
    ``allow_all`` and the write tools are refused by the configured
    deny-list instead. That is the same gate a read-only session uses, and it
    is how an operator withholds individual tools (ADR-057). ``deny_mutations``
    itself refuses ``delegate``, which is ``mutate``-tiered (ADR-084).

    Everything is checked from outside the process, the way a TA would: the
    scrape endpoint while the REPL is alive, then the trace and the
    workspace on the volumes.
    """
    stack.write(f"{WORKSPACE}/ledger.txt", LEDGER)
    denied = '["write_file", "append_file", "edit_file", "create_folder"]'

    with stack.session(permissions="allow_all", env={"DENIED_TOOLS": denied}) as live:
        live.wait_for("Metrics:")
        live.ask(
            "Use your researcher sub-agent to count how many lines of "
            "ledger.txt contain ENTRY. Then save that number into a new file "
            "called count.md."
        )
        # Delegation, seen by a scraper: the child's run is counted by role.
        live.scrape_until('harness_subagent_runs_total{role="researcher"')
        # Permission enforcement, seen by a scraper: the write was refused.
        live.scrape_until('harness_permission_decisions_total{outcome="deny"}')
        # The REPL prompt returning is the run's end; the trace file is
        # complete only after that.
        live.wait_for_prompt(2)
        body = live.scrape()

    assert 'harness_subagent_runs_total{role="researcher"' in body
    assert 'harness_permission_decisions_total{outcome="deny"}' in body
    assert 'ok="true"' in body, "no tool call succeeded anywhere in the run"
    assert not stack.exists(f"{WORKSPACE}/count.md"), (
        f"a file was written past the deny-list\n{stack.tree()}"
    )

    # Observability, per run: the child is filed under its parent in the one
    # trace file, it ran a tool itself, and the denial is in it too.
    recorded = events(stack)
    runs = [event for event in recorded if event["event"] == "run_end"]
    parents = [event for event in runs if event["span"] == "agent.run"]
    assert len(parents) == 1, parents
    children = [event for event in runs if event["span"] == "subagent.run"]
    assert children, "the trace holds no sub-agent run"
    for child in children:
        assert child["parent_run_id"] == parents[0]["run_id"], child
    assert any(
        event["event"] == "tool" and event.get("ok") and event["role"] == "researcher"
        for event in recorded
    ), "the child ran no tool successfully"
    assert any(
        event["event"] == "permission" and event["permission"] == "deny"
        for event in recorded
    ), "the refused write is not in the trace"
