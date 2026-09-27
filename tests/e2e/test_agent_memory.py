"""Week 2's memory requirement, stated the way the brief states it.

*"The agent must be able to store information during one session and
retrieve relevant information in a later session, even after the application
restarts."* Three claims, and the last one is why this test needs a
container rather than a fixture: between the two runs below the agent
process does not merely end, it is destroyed. A fresh container starts with
nothing in memory, nothing on its filesystem except the volumes, and no
conversation history at all.

What survives is the Qdrant volume, which is the only channel. So if the
second agent knows something the first one was told, it came out of the
vector store — there is nowhere else it could have come from.

The store is a real Qdrant container on a real network with its own
throwaway volume, not the in-process engine the integration suite uses
(ADR-069), because "even after the application restarts" is a claim about
a service and a volume, and the in-process engine is neither.

Nothing here tells the agent which tool to use. Whether a fact is worth
storing is a decision the harness asks the model to make, and a test that
instructed it would be testing the instruction.
"""

import pytest
from tests.e2e.conftest import Stack

pytestmark = pytest.mark.e2e


def test_a_fact_learned_in_one_container_survives_into_the_next(
    stack: Stack,
) -> None:
    """The brief's sentence, in two containers over one volume.

    The fact is deliberately the kind a person mentions in passing rather
    than one they ask to have saved: the model has to judge that a
    long-standing preference is worth keeping. That judgement is the
    mechanism ADR-033 describes and it is the part that a scripted test
    cannot exercise at all. Since ADR-107 there is a second path — the
    harness extracts the preference from the user's message itself — and
    either path passing is the feature working.

    The recall prompt does not repeat the fact and does not mention memory.
    If the second agent answers with the language, it looked.
    """
    first = stack.run(
        "I am Alice, and I always want code comments written in German. "
        "Remember that for next time.",
        memory=True,
    )
    first.assert_completed()
    # No assertion that the model called a memory tool. Since ADR-107 the
    # harness also extracts durable facts from the user's message at the end
    # of the run, so a model that answers without calling one has still had
    # the preference stored. The proof is the second container's answer.

    # A different container. Same volumes, same Qdrant, no shared process
    # and no shared context.
    second = stack.run("What language do I want my code comments in?", memory=True)
    second.assert_completed()

    assert "german" in second.answer, (
        f"the stored preference did not reach the second run: {second.answer!r}"
    )


def test_the_second_run_retrieves_rather_than_being_told(stack: Stack) -> None:
    """Retrieval, specifically — the read path, not just the write path.

    A fact can reach a later run two ways: injected by the hook before the
    first token, or fetched by the model calling ``memory_search`` when it
    notices it does not know. Both are correct and the design uses both
    (ADR-065), so the assertion is on the answer rather than on the route.

    Two facts are stored and only one is asked about. That is what makes
    this a retrieval test rather than a storage test: with a single fact in
    the collection, any query at all returns it and the ranking is never
    exercised.
    """
    stack.run(
        "Two things to remember about me: my name is Alice, and the project "
        "deadline is the 27th of September.",
        memory=True,
    ).assert_completed()

    second = stack.run("When is my project deadline?", memory=True)
    second.assert_completed()

    assert "27" in second.answer, f"the deadline was not recalled: {second.answer!r}"


def test_memory_survives_the_store_being_restarted_underneath_it(
    stack: Stack,
) -> None:
    """ "Even after the application restarts" — including the database.

    The agent container is thrown away between every run already. This goes
    one further and restarts Qdrant itself, so what is being relied on is
    the *volume* rather than a process that happened to stay up. That is the
    difference between memory that persists and memory that has simply not
    been interrupted yet.
    """
    stack.run(
        "Remember that our deployment target is Kubernetes, not bare metal.",
        memory=True,
    ).assert_completed()

    stack.restart_memory()

    recalled = stack.run("What do we deploy onto?", memory=True)
    recalled.assert_completed()

    assert "kubernetes" in recalled.answer, (
        f"the fact did not survive a restart of the store: {recalled.answer!r}"
    )


def test_a_degraded_memory_backend_does_not_end_the_run(stack: Stack) -> None:
    """ADR-069: no Qdrant is a worse run, not a failed one.

    Run without ``memory=True``, so there is no store on the network at all.
    The harness is meant to notice, say so, and carry on with everything
    else working — a demo on a machine with no Docker network should still
    be able to write a file.

    Asserted through a task that has nothing to do with memory, because the
    failure this guards against is memory taking the whole run down with it.
    """
    run = stack.run("Create a file called hello.md containing the word hello.")
    run.assert_completed()

    assert stack.read("/app/workspace/hello.md").strip().lower().startswith("hello")


def test_a_fact_mentioned_in_passing_is_remembered_without_being_asked(
    stack: Stack,
) -> None:
    """The harness-driven write trigger, against the real model (ADR-107).

    The first task is arithmetic; the fact is a clause in front of it, and
    nothing says "remember". A model busy answering the question has no
    reason to call a memory tool, which is the case the extraction pass
    exists for. The trace is kept in the failure message so a reader can see
    which path wrote the fact — the tool or the extractor — because both
    are correct and this test asserts on the outcome, not the route.
    """
    first = stack.run("I'm based in Passau. What is 17 times 3?", memory=True)
    first.assert_completed()
    assert "51" in first.answer, (
        f"the question itself was not answered: {first.answer!r}"
    )

    second = stack.run("Which city am I based in?", memory=True)
    second.assert_completed()

    assert "passau" in second.answer, (
        f"the city stated in passing was not remembered: {second.answer!r} "
        f"(first run called: {first.tools_called})"
    )
