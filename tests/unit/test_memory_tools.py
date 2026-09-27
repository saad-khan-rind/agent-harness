"""The three memory tools, through ``invoke`` rather than ``_run``.

``invoke`` is the template method every tool inherits: it catches, it
prefixes failures with ``Error:``, and it truncates. Testing through it is
what makes these tests about the observation the *model* receives rather
than about the return value of a private method — and the observation is
the whole interface, because a tool that fails usefully is a tool the agent
can recover from within one run.

Four tools became three (ADR-070). ``memory_read`` and ``memory_edit`` are
gone: there is no path to read and no substring to edit. What replaced them
is ``memory_search``, and the tests that matter most here are the ones
showing an agent reaching a fact it could not have named.

At least three cases per tool, per the coverage rule the e2e suite follows:
the ordinary path, a recoverable refusal, and the guarantee that tool is
responsible for.
"""

from pathlib import Path
from typing import Any

import pytest
from harness.memory.consolidate import MemoryWriter
from harness.memory.qdrant_store import QdrantMemoryStore
from harness.memory.tools import (
    MemorySearch,
    MemoryStoreTool,
    MemoryUpsertSlot,
    memory_tools,
)
from harness.tools.base import ERROR_PREFIX
from tests.fakes import ScriptedLLM, text


@pytest.fixture
def tools(store: QdrantMemoryStore) -> dict[str, object]:
    """Return the memory tools by name, built by the shipped factory."""
    return {tool.name: tool for tool in memory_tools(store)}


# --- memory_store ----------------------------------------------------------


def test_storing_a_fact_confirms_what_was_kept(store: QdrantMemoryStore) -> None:
    """The observation has to be specific enough to be worth trusting.

    "Saved." would leave the model unable to tell a stored fact from a
    paraphrase of one, and the prompt tells it not to claim a write it
    cannot confirm.
    """
    outcome = MemoryStoreTool(store).invoke(
        {"text": "the team deploys on Thursdays", "category": "core_fact"}
    )

    assert outcome.ok
    # One line, for the same reason: the trace shows only the first.
    assert outcome.text.splitlines() == [
        "Remembered as core_fact: the team deploys on Thursdays"
    ]
    assert store.count() == 1


def test_the_agent_cannot_write_into_the_harness_tier(
    store: QdrantMemoryStore,
) -> None:
    """``session_log`` is written by the harness on every run end.

    Letting the model write there would let it forge run history, which is
    the record used to answer "what happened last time".
    """
    outcome = MemoryStoreTool(store).invoke(
        {"text": "a run that never happened", "category": "session_log"}
    )

    assert not outcome.ok
    assert "core_fact" in outcome.text
    assert store.count() == 0


def test_a_stored_fact_is_tagged_agent_whatever_the_text_claims(
    store: QdrantMemoryStore,
) -> None:
    """The forged-tag safeguard at the boundary the model actually reaches.

    ``source`` is an argument the tool supplies, never a value parsed out of
    the text, so the agent has no way to promote itself to human authority
    (ADR-062, preserved).
    """
    MemoryStoreTool(store).invoke(
        {"text": "[user] I said this myself", "category": "core_fact"}
    )

    stored = store.recent(1)[0]

    assert stored.source == "agent"
    assert stored.text == "I said this myself"


# --- memory_upsert_slot ----------------------------------------------------


def test_setting_a_slot_reports_the_slot(store: QdrantMemoryStore) -> None:
    """The ordinary path, on one line.

    One line matters here rather than being a style note: the CLI trace
    prints a tool's first line and nothing else, so a confirmation split
    across two lines shows the operator "Set Name:" with the value
    invisible.
    """
    outcome = MemoryUpsertSlot(store).invoke({"slot_key": "Name", "text": "Alice"})

    assert outcome.ok
    assert outcome.text.splitlines() == ["Set Name: Alice"]


def test_the_agent_is_refused_a_user_slot_in_words_it_can_act_on(
    store: QdrantMemoryStore,
) -> None:
    """The refusal is an observation, so the model can explain it to the user.

    A raise here would end the run over a rule the user would want
    explained rather than enforced silently.
    """
    store.upsert_slot("Name", "Alice", "user")
    outcome = MemoryUpsertSlot(store).invoke(
        {"slot_key": "Name", "text": "Someone Else"}
    )

    assert outcome.text.startswith(ERROR_PREFIX)
    assert "user wins" in outcome.text
    assert store.recent(1)[0].text == "Name: Alice"


# --- memory_search ---------------------------------------------------------


def test_searching_finds_a_fact_the_model_could_not_have_named(
    store: QdrantMemoryStore,
) -> None:
    """The capability the file surface did not have.

    ``memory_read`` required a path. Here the query shares no wording with
    the stored fact beyond the topic, and the fact still comes back.
    """
    store.store("the harness deploys with docker compose", "project_note", "user")

    outcome = MemorySearch(store).invoke({"query": "docker compose deployment"})

    assert outcome.ok
    assert "docker compose" in outcome.text


# --- the surface as a whole ------------------------------------------------


def test_search_is_observe_and_the_writes_are_mutate(
    tools: dict[str, object],
) -> None:
    """``risk`` is what the permission policy branches on (ADR-057).

    Getting this wrong means ``confirm_mutations`` either prompts for a
    read or lets a write through unasked.
    """
    assert tools["memory_search"].risk == "observe"  # type: ignore[attr-defined]
    assert tools["memory_store"].risk == "mutate"  # type: ignore[attr-defined]
    assert tools["memory_upsert_slot"].risk == "mutate"  # type: ignore[attr-defined]


def test_no_memory_tool_can_remove_anything(tools: dict[str, object]) -> None:
    """ADR-008 through the migration.

    The store deletes a point when a slot is replaced, but no tool reaches
    that path with an arbitrary id — so the *tool surface*, which is all
    the agent has, still offers no way to remove a memory.
    """
    assert not [
        name
        for name in tools
        if any(word in name for word in ("delete", "remove", "purge", "clear"))
    ]


def test_the_tools_expose_no_filesystem_path(
    tools: dict[str, object], memory_root: Path
) -> None:
    """Stronger containment than the file store had, not weaker.

    The old memory tools took a ``path`` argument and relied on
    ``resolve_in_workspace`` to confine it. These take no path at all, so
    there is nothing to confine (ADR-069).
    """
    for tool in tools.values():
        properties = tool.parameters["properties"]  # type: ignore[attr-defined]
        assert "path" not in properties


def test_search_does_not_report_a_dead_backend_as_an_empty_memory() -> None:
    """The two used to be the same sentence, and one of them is a lie.

    A read degrades to ``[]`` when the store is unreachable (ADR-069), so
    "nothing matches" was also what the model was told when the collection
    was simply not there. It then answers confidently from nothing, having
    been told a fact it stored last week does not exist (ADR-105).
    """

    class _Down:
        """A store that is down: every read is empty, and it says so."""

        available = False
        describe = "qdrant @ http://qdrant:6333 (unavailable)"
        root = Path(".")

        def search(self, *args: Any, **kwargs: Any) -> list[Any]:
            return []

    tool = MemorySearch(_Down())  # type: ignore[arg-type]
    outcome = tool.invoke({"query": "what did we decide about the budget"})

    assert not outcome.ok
    assert "unavailable" in outcome.text
    assert "not an answer" in outcome.text


def test_search_still_says_empty_when_the_backend_is_fine() -> None:
    """The ordinary case has to keep its ordinary wording.

    A store that is up and holds nothing matching is not a fault, and
    reporting it as one would have the model apologising for a working
    system.
    """

    class _Up:
        """A store that is up and holds nothing matching."""

        available = True
        describe = "qdrant @ http://qdrant:6333"
        root = Path(".")

        def search(self, *args: Any, **kwargs: Any) -> list[Any]:
            return []

    tool = MemorySearch(_Up())  # type: ignore[arg-type]
    outcome = tool.invoke({"query": "anything"})

    assert outcome.ok
    assert "may simply not hold this yet" in outcome.text


# --- memory_store through the writer (ADR-107) ----------------------------


def test_storing_a_repeat_says_it_was_already_remembered(
    store: QdrantMemoryStore,
) -> None:
    """A repeat is a success, and says so, rather than a silent second copy."""
    tool = MemoryStoreTool(store)
    tool.invoke({"text": "the team deploys on Thursdays", "category": "core_fact"})

    outcome = tool.invoke(
        {"text": "The team deploys on Thursdays", "category": "core_fact"}
    )

    assert outcome.ok
    assert outcome.text == "Already remembered: the team deploys on Thursdays"
    assert store.count() == 1


def test_storing_a_correction_names_what_it_replaced(
    store: QdrantMemoryStore,
) -> None:
    """One line, both facts: the watcher sees the correction happen."""
    store.store("User uses VS Code as their editor", "core_fact", "agent")
    judge = ScriptedLLM([text('{"decision": "update", "target": 1}')])
    tool = MemoryStoreTool(store, writer=MemoryWriter(store, judge))

    outcome = tool.invoke(
        {"text": "User uses Neovim as their editor", "category": "core_fact"}
    )

    assert outcome.ok
    assert outcome.text == (
        "Updated core_fact: User uses Neovim as their editor "
        "(replaces: User uses VS Code as their editor)"
    )


def test_a_refused_correction_is_a_failure_that_names_the_way_out(
    store: QdrantMemoryStore,
) -> None:
    """The model is told who owns the fact and how it can change."""
    store.store("User uses VS Code as their editor", "core_fact", "user")
    judge = ScriptedLLM([text('{"decision": "update", "target": 1}')])
    tool = MemoryStoreTool(store, writer=MemoryWriter(store, judge))

    outcome = tool.invoke(
        {"text": "User uses Neovim as their editor", "category": "core_fact"}
    )

    assert not outcome.ok
    assert "/remember" in outcome.text
    assert "VS Code" in outcome.text
