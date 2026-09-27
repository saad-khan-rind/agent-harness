"""The consolidating write path: repeats, corrections, and who may correct whom.

Runs against the real in-process Qdrant (the ``store`` fixture) with only
the judge scripted, so what is tested is the shipped search, the shipped
supersede mark and the shipped read filter — the judge's answer is the one
input a test has to choose (ADR-107).
"""

from harness.llm.protocol import LLMError
from harness.memory.consolidate import MemoryWriter, _parse_decision
from harness.memory.qdrant_store import QdrantMemoryStore
from tests.fakes import FailingLLM, ScriptedLLM, text

_OLD = "User uses VS Code as their editor"
_NEW = "User uses Neovim as their editor"


def _judge(decision: str, target: object = None) -> ScriptedLLM:
    """A judge that answers once, with the given decision."""
    target_json = "null" if target is None else repr(target).replace("'", '"')
    return ScriptedLLM([text(f'{{"decision": "{decision}", "target": {target_json}}}')])


def _active(store: QdrantMemoryStore) -> list[str]:
    """Every active fact's text, via the scan ``/memory`` uses."""
    return [record.text for record in store.recent(50)]


# --- without a judge --------------------------------------------------------


def test_an_exact_repeat_is_not_stored_twice(store: QdrantMemoryStore) -> None:
    """Case and spacing differences are still the same fact."""
    writer = MemoryWriter(store)

    first = writer.write("User prefers answers in German", "core_fact", "agent")
    second = writer.write("user prefers   answers in german", "core_fact", "agent")

    assert first.action == "added"
    assert second.action == "unchanged"
    assert store.count() == 1


def test_no_judge_means_a_near_repeat_is_added(store: QdrantMemoryStore) -> None:
    """With consolidation off, only exact repeats are caught."""
    writer = MemoryWriter(store)
    writer.write(_OLD, "core_fact", "agent")

    outcome = writer.write(_NEW, "core_fact", "agent")

    assert outcome.action == "added"
    assert store.count() == 2


def test_the_judge_is_not_asked_when_nothing_is_similar(
    store: QdrantMemoryStore,
) -> None:
    """An empty store costs no model call: ``ScriptedLLM([])`` would raise."""
    judge = ScriptedLLM([])

    outcome = MemoryWriter(store, judge).write(_OLD, "core_fact", "agent")

    assert outcome.action == "added"
    assert judge.calls == []


# --- with a judge -----------------------------------------------------------


def test_a_correction_supersedes_the_old_fact(store: QdrantMemoryStore) -> None:
    """The contradiction this module exists for: only the new fact stays live."""
    store.store(_OLD, "core_fact", "agent")

    outcome = MemoryWriter(store, _judge("update", 1)).write(_NEW, "core_fact", "agent")

    assert outcome.action == "updated"
    assert outcome.replaced is not None and outcome.replaced.text == _OLD
    assert _active(store) == [_NEW]
    assert [hit.record.text for hit in store.search("editor", limit=5)] == [_NEW]
    assert store.count() == 1


def test_a_duplicate_in_other_words_is_not_stored(store: QdrantMemoryStore) -> None:
    """The second half of the problem: two phrasings reading as two sources."""
    store.store("User lives in Passau", "core_fact", "agent")

    outcome = MemoryWriter(store, _judge("duplicate", 1)).write(
        "User is based in Passau", "core_fact", "agent"
    )

    assert outcome.action == "unchanged"
    assert _active(store) == ["User lives in Passau"]


def test_the_judge_sees_numbered_facts_and_no_tools(store: QdrantMemoryStore) -> None:
    """A narrow question: numbered options, and nothing it could call."""
    store.store(_OLD, "core_fact", "agent")
    judge = _judge("add")

    MemoryWriter(store, judge).write(_NEW, "core_fact", "agent")

    messages, tools = judge.calls[0]
    assert tools == []
    assert f"1. {_OLD}" in messages[1]["content"]
    assert f"NEW: {_NEW}" in messages[1]["content"]


def test_an_unusable_answer_falls_back_to_add(store: QdrantMemoryStore) -> None:
    """Prose, an out-of-range target and a failed call all mean *add*."""
    store.store(_OLD, "core_fact", "agent")
    judges = [
        ScriptedLLM([text("I think it is an update.")]),
        _judge("update", 7),
        FailingLLM(LLMError("endpoint down")),
    ]
    for judge in judges:
        outcome = MemoryWriter(store, judge).write(_NEW, "core_fact", "agent")
        assert outcome.action in {"added", "unchanged"}

    # The first write added the new fact; the later ones saw it as an exact
    # repeat. Either way the old fact was never hidden.
    assert sorted(_active(store)) == sorted([_OLD, _NEW])


# --- provenance -------------------------------------------------------------


def test_the_agent_cannot_supersede_what_the_user_recorded(
    store: QdrantMemoryStore,
) -> None:
    """User wins, whatever the judge says: the rank is checked in code."""
    store.store(_OLD, "core_fact", "user")

    outcome = MemoryWriter(store, _judge("update", 1)).write(_NEW, "core_fact", "agent")

    assert outcome.action == "refused"
    assert outcome.record.text == _OLD
    assert _active(store) == [_OLD]


def test_extraction_cannot_supersede_what_the_user_recorded(
    store: QdrantMemoryStore,
) -> None:
    """The harness ranks above the agent and still below the user."""
    store.store(_OLD, "core_fact", "user")

    outcome = MemoryWriter(store, _judge("update", 1)).write(
        _NEW, "core_fact", "harness"
    )

    assert outcome.action == "refused"


def test_the_user_can_correct_anything(store: QdrantMemoryStore) -> None:
    """``/remember`` is how a user fixes a fact, whoever recorded it."""
    store.store(_OLD, "core_fact", "agent")

    outcome = MemoryWriter(store, _judge("update", 1)).write(_NEW, "core_fact", "user")

    assert outcome.action == "updated"
    assert [record.source for record in store.recent(5)] == ["user"]


def test_a_user_restating_an_agent_fact_takes_ownership(
    store: QdrantMemoryStore,
) -> None:
    """A duplicate from a higher source is recorded under that source."""
    store.store("User lives in Passau", "core_fact", "agent")

    outcome = MemoryWriter(store, _judge("duplicate", 1)).write(
        "User is based in Passau", "core_fact", "user"
    )

    assert outcome.action == "updated"
    records = store.recent(5)
    assert [(r.text, r.source) for r in records] == [
        ("User is based in Passau", "user")
    ]


def test_an_exact_repeat_from_the_user_upgrades_provenance(
    store: QdrantMemoryStore,
) -> None:
    """Same text, same point: the source changes in place, no second record."""
    store.store("User prefers answers in German", "core_fact", "agent")

    outcome = MemoryWriter(store).write(
        "User prefers answers in German", "core_fact", "user"
    )

    assert outcome.action == "unchanged"
    assert [record.source for record in store.recent(5)] == ["user"]
    assert store.count() == 1


def test_an_exact_repeat_from_the_agent_keeps_user_provenance(
    store: QdrantMemoryStore,
) -> None:
    """Re-storing a user's fact as the agent's would demote it."""
    store.store("User prefers answers in German", "core_fact", "user")

    MemoryWriter(store).write("User prefers answers in German", "core_fact", "agent")

    assert [record.source for record in store.recent(5)] == ["user"]


def test_slots_are_never_candidates(store: QdrantMemoryStore) -> None:
    """A prose fact must not supersede a slot and empty it."""
    store.upsert_slot("Name", "Alice", "user")
    judge = ScriptedLLM([])

    MemoryWriter(store, judge).write("User's name is Alice Smith", "core_fact", "agent")

    assert judge.calls == []
    assert [record.text for record in store.slots()] == ["Name: Alice"]


# --- the answer parser ------------------------------------------------------


def test_the_parser_accepts_a_numeric_string_and_rejects_booleans() -> None:
    """Models send ``"1"`` as often as ``1``; ``true`` is not a fact number."""
    assert _parse_decision({"decision": "update", "target": "1"}, 2).index == 0
    assert _parse_decision({"decision": "update", "target": True}, 2).kind == "add"
    assert _parse_decision({"decision": "delete", "target": 1}, 2).kind == "add"
    assert _parse_decision(None, 2).kind == "add"
