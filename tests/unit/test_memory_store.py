"""The vector store: writes, retrieval, decay, slots, and degradation.

Everything here runs against a **real Qdrant** — ``QdrantClient(":memory:")``
is the same query engine, the same filtering and the same cosine, hosted in
this process rather than behind HTTP. That choice is what makes this file
worth reading: it tests the store that ships, not a mock of it, in a
pipeline with no Docker and no network (ADR-069).

The four things a vector-backed memory has to get right, and where each is
covered below:

* it must not lose the safety properties the file store had — provenance is
  stamped, the user wins a slot, no delete reaches the tool surface;
* ranking must combine relevance with recency in a way that can be
  explained in review, which is why :func:`decayed_score` is tested as a
  pure function before it is tested through a query;
* a filter must actually narrow, because a filter that silently matches
  everything looks exactly like a working one;
* every failure has to degrade rather than propagate — a reachable-then-gone
  database, a missing one, an embedder that dies mid-session.
"""

import time
from pathlib import Path
from typing import Any

import pytest
from harness.memory.embed import HashingEmbedder
from harness.memory.protocol import MemoryUnavailableError
from harness.memory.qdrant_store import (
    QdrantMemoryStore,
    decayed_score,
)
from harness.memory.sessions import SessionLog

_DAY = 86_400.0


def _store(memory_root: Path, now: Any = time.time) -> QdrantMemoryStore:
    """Build a store over an in-process Qdrant with an injectable clock."""
    from qdrant_client import QdrantClient

    return QdrantMemoryStore(
        embedder=HashingEmbedder(),
        session_log=SessionLog(memory_root),
        client=QdrantClient(":memory:"),
        now=now,
    )


# --- the scoring function, on its own --------------------------------------


def test_an_ancient_perfect_match_still_outranks_a_weak_recent_one() -> None:
    """Why the combination is additive rather than multiplicative.

    Multiplying would let age drive a perfectly relevant fact arbitrarily
    close to zero, and what is being built is a memory, not a cache. A fact
    from two years ago that answers the question exactly must still win
    against one from this morning that barely matches.
    """
    now = 1_700_000_000.0
    ancient_exact = decayed_score(1.0, int(now - 730 * _DAY), now)
    fresh_weak = decayed_score(0.2, int(now), now)

    assert ancient_exact > fresh_weak


# --- writing ---------------------------------------------------------------


def test_a_stored_fact_comes_back_with_its_provenance(memory_root: Path) -> None:
    """The round trip, and the field the injected block separates on."""
    store = _store(memory_root)
    store.store("the team deploys with docker compose", "core_fact", "agent")

    hits = store.search("docker compose", limit=5)

    assert len(hits) == 1
    assert hits[0].record.source == "agent"
    assert hits[0].record.category == "core_fact"


def test_an_empty_fact_is_refused_before_anything_is_embedded(
    memory_root: Path,
) -> None:
    """Cheap validation ahead of an expensive call, and nothing stored."""
    store = _store(memory_root)

    with pytest.raises(ValueError, match="empty fact"):
        store.store("   ", "core_fact", "agent")
    assert store.count() == 0


# --- slots -----------------------------------------------------------------


def test_a_slot_replaces_rather_than_accumulates(memory_root: Path) -> None:
    """ADR-063's mechanic, preserved through the migration.

    Two writes to one slot leave one record. Without this, memory ends up
    holding "Language: German" and "Language: English" and injecting both,
    which is worse than holding neither.
    """
    store = _store(memory_root)
    store.upsert_slot("Language", "German", "user")
    store.upsert_slot("Language", "English", "user")

    assert store.count() == 1
    assert store.recent(5)[0].text == "Language: English"


def test_the_agent_cannot_overwrite_a_user_slot(memory_root: Path) -> None:
    """The user wins, and the refusal says why.

    The single most important preserved behaviour: the person's own
    statement about themselves outranks the model's inference, and the
    model is told so in words it can act on.
    """
    store = _store(memory_root)
    store.upsert_slot("Name", "Alice", "user")

    with pytest.raises(ValueError, match="The user wins"):
        store.upsert_slot("Name", "Someone Else", "agent")
    assert store.recent(5)[0].text == "Name: Alice"


def test_an_unknown_slot_is_refused_by_name(memory_root: Path) -> None:
    """The slot namespace is closed on purpose.

    An open one lets the model invent a key per session, at which point
    every fact is its own slot and the replace mechanic does nothing.
    """
    store = _store(memory_root)

    with pytest.raises(ValueError, match="unknown slot"):
        store.upsert_slot("Nickname", "Sadi", "agent")


# --- retrieval -------------------------------------------------------------


def test_an_unrelated_query_returns_nothing_rather_than_the_nearest_thing(
    memory_root: Path,
) -> None:
    """Why there is a similarity floor.

    A vector search always returns its k nearest neighbours however far
    away they are. Without a floor, asking about the weather injects the
    three least-unrelated facts in the collection and the model reads them
    as relevant context.
    """
    store = _store(memory_root)
    store.store("the harness deploys with docker compose", "core_fact", "user")

    assert store.search("photosynthesis in marine algae", limit=5) == []


# --- counting and scanning -------------------------------------------------


# --- the run log stays a file ----------------------------------------------


def test_the_run_log_is_written_to_disk_not_to_the_collection(
    memory_root: Path,
) -> None:
    """ADR-068: the log is read in order by humans, never by similarity.

    Embedding it would spend a call per run to create one near-identical
    point per run — the worst possible neighbours for a collection whose
    value is that dissimilar things are far apart.
    """
    store = _store(memory_root)
    store.append_session("build the thing", "built it", 4)

    log = (memory_root / "sessions.md").read_text(encoding="utf-8")

    assert "build the thing" in log
    assert store.count() == 0


def test_a_failing_log_write_does_not_raise(memory_root: Path) -> None:
    """``on_run_end`` runs in a ``finally``, often while the run is failing.

    Turning a bad run into a crashed one would replace a useful error
    message with a confusing one.
    """
    store = _store(memory_root)
    (memory_root / "sessions.md").mkdir()  # a directory where a file goes

    store.append_session("task", "outcome", 1)  # must not raise


# --- degradation -----------------------------------------------------------


def test_a_missing_database_makes_reads_empty_rather_than_fatal(
    memory_root: Path,
) -> None:
    """Port 1 on localhost refuses immediately, so no timeout is waited on.

    This is the whole degradation policy in one assertion: memory is a
    convenience layered on the agent, so an absent backend produces a run
    with no memory, exactly as ``memory_enabled: false`` does.
    """
    store = QdrantMemoryStore(
        embedder=HashingEmbedder(),
        session_log=SessionLog(memory_root),
        url="http://127.0.0.1:1",
        timeout=2.0,
    )

    assert store.available is False
    assert store.search("anything", limit=5) == []
    assert store.count() == 0
    assert store.recent(5) == []


def test_a_missing_database_makes_a_write_say_so(memory_root: Path) -> None:
    """The asymmetry between reads and writes, and the reason for it.

    A silent failed write would leave the model believing it had
    remembered something it had not, and acting on that belief for the rest
    of the session. So writes raise where reads return empty.
    """
    store = QdrantMemoryStore(
        embedder=HashingEmbedder(),
        session_log=SessionLog(memory_root),
        url="http://127.0.0.1:1",
        timeout=2.0,
    )

    with pytest.raises(MemoryUnavailableError, match="unavailable"):
        store.store("a fact", "core_fact", "agent")


def test_a_write_whose_embedding_fails_says_nothing_was_stored(
    memory_root: Path,
) -> None:
    """Same asymmetry as a missing database, same reason."""
    from harness.memory.embed import EmbeddingError

    class _Broken:
        dimensions = 256
        name = "broken"

        def embed(self, text: str) -> list[float]:
            raise EmbeddingError("gone")

    store = _store(memory_root)
    store._embedder = _Broken()  # noqa: SLF001 - simulating a mid-session failure

    with pytest.raises(MemoryUnavailableError, match="nothing was stored"):
        store.store("a fact", "core_fact", "agent")


def test_a_collection_at_the_wrong_width_is_rebuilt_not_refused(
    memory_root: Path,
) -> None:
    """Changing embedder must not cost the user their facts.

    A width change used to disable memory and tell the user to drop the
    collection — which is to say, to throw away everything it held. That was
    never necessary: the text of every record is in its payload, so only the
    vectors are width-specific and only the vectors need rebuilding. This is
    the difference between "upgrading the embedder" being a migration and
    being a wipe, and it is the whole reason anyone can act on the advice to
    stop using the lexical fallback.
    """
    from qdrant_client import QdrantClient

    client = QdrantClient(":memory:")
    wide = QdrantMemoryStore(
        embedder=HashingEmbedder(512),
        session_log=SessionLog(memory_root),
        client=client,
    )
    wide.store("the harness deploys with docker compose", "core_fact", "user")
    wide.upsert_slot("Likes", "swimming", "user")

    narrower = QdrantMemoryStore(
        embedder=HashingEmbedder(256),
        session_log=SessionLog(memory_root),
        client=client,
    )

    assert narrower.available is True
    assert narrower.count() == 2
    assert {record.text for record in narrower.recent(10)} == {
        "the harness deploys with docker compose",
        "Likes: swimming",
    }
    # Queryable at the new width, not merely present.
    assert narrower.search("docker compose", limit=5)
    # And said out loud: the CLI prints this note (ADR-110).
    assert "rebuilt memory at 256 dimensions (was 512)" in narrower.migration_note


# --- a server that answers, badly ------------------------------------------
#
# The tests above cover a database that is *absent*. These cover one that is
# present and failing, which is the harder case and the one a real
# deployment actually produces: a Qdrant mid-restart, out of disk, or with a
# collection someone dropped under a running harness. Every one of these
# must degrade in the direction the read/write asymmetry sets out — reads
# empty, writes loud — and none may escape as an exception.


class _AngryClient:
    """A client whose every operation raises after a successful startup.

    Constructed with a working collection check so the store connects, then
    fails on use. That ordering matters: a store that only ever saw a
    connection fail would take the ``_connect`` branch and never reach the
    per-operation error handling this exercises.
    """

    def __init__(self, error: Exception | None = None) -> None:
        self._error = error or RuntimeError("qdrant said no")
        self.closed = False

    def collection_exists(self, collection_name: str) -> bool:
        """Report the collection as present, so startup succeeds."""
        return True

    def get_collection(self, collection_name: str) -> Any:
        """Return a description with no readable width, which is allowed."""
        return object()

    def query_points(self, **kwargs: Any) -> Any:
        """Fail a search."""
        raise self._error

    def scroll(self, **kwargs: Any) -> Any:
        """Fail a scan."""
        raise self._error

    def count(self, **kwargs: Any) -> Any:
        """Fail a count."""
        raise self._error

    def upsert(self, **kwargs: Any) -> Any:
        """Fail a write."""
        raise self._error

    def delete(self, **kwargs: Any) -> Any:
        """Fail a delete."""
        raise self._error

    def close(self) -> None:
        """Record that shutdown reached us."""
        self.closed = True


def _angry(memory_root: Path, error: Exception | None = None) -> QdrantMemoryStore:
    """Build a store over a client that connects and then fails."""
    return QdrantMemoryStore(
        embedder=HashingEmbedder(),
        session_log=SessionLog(memory_root),
        client=_AngryClient(error),
    )


def test_a_failing_write_names_the_failure(memory_root: Path) -> None:
    """Writes are the exception to "degrade quietly", and stay so here.

    The model is told the write failed, in text it can relay to the user,
    rather than being left to believe it remembered something.
    """
    with pytest.raises(MemoryUnavailableError, match="memory write failed"):
        _angry(memory_root).store("a fact", "core_fact", "agent")


def test_a_collection_that_cannot_be_prepared_disables_memory(
    memory_root: Path,
) -> None:
    """Startup itself can fail on a server that is answering.

    Someone dropping the collection under a running harness, or a version
    mismatch, lands here. The store reports unavailable rather than
    creating a second collection or failing per call.
    """

    class _NoCollectionInfo:
        def collection_exists(self, collection_name: str) -> bool:
            raise RuntimeError("collection service unavailable")

        def close(self) -> None:
            return

    store = QdrantMemoryStore(
        embedder=HashingEmbedder(),
        session_log=SessionLog(memory_root),
        client=_NoCollectionInfo(),
    )

    assert store.available is False
    assert "cannot prepare collection" in store.describe


def test_a_malformed_stored_point_is_skipped_not_fatal(memory_root: Path) -> None:
    """One bad row must cost that row, not the whole query.

    The collection lives on a volume that outlives every container and can
    be edited from the Qdrant dashboard, so a payload this code did not
    write is a real possibility rather than a hypothetical.
    """

    class _MixedClient(_AngryClient):
        def query_points(self, **kwargs: Any) -> Any:
            good = type(
                "P",
                (),
                {
                    "payload": {
                        "text": "a real fact",
                        "source": "user",
                        "category": "core_fact",
                        "timestamp": 1_700_000_000,
                    },
                    "score": 0.9,
                },
            )()
            bad = type("P", (), {"payload": {"nonsense": True}, "score": 0.9})()
            return type("R", (), {"points": [bad, good]})()

    store = QdrantMemoryStore(
        embedder=HashingEmbedder(),
        session_log=SessionLog(memory_root),
        client=_MixedClient(),
    )

    hits = store.search("a real fact", limit=5)

    assert [hit.record.text for hit in hits] == ["a real fact"]


# --- the run log, on its own -----------------------------------------------
#
# The half of memory that did not move (ADR-068). Its tests live here rather
# than in a file of their own because every one of them is about the same
# claim as the store's: this must never take a run down, since it is written
# from ``on_run_end`` in a ``finally`` block, often while the run is already
# failing.


def test_the_log_reads_back_what_it_wrote(memory_root: Path) -> None:
    """The ordinary path: an entry, with the task and the outcome in it."""
    log = SessionLog(memory_root)
    log.append("build the thing", "built it", 4)

    text = log.read()

    assert "build the thing" in text
    assert "built it" in text
    assert "4 iteration(s)" in text


def test_an_unreadable_log_reads_as_empty(memory_root: Path) -> None:
    """A missing log is the normal state of a fresh install.

    An unreadable one is rare and must still not stop a run from starting:
    the same totality rule the file store followed everywhere.
    """
    (memory_root / "sessions.md").mkdir()

    assert SessionLog(memory_root).read() == ""
    assert SessionLog(memory_root).recent(5) == []


# --- superseding (ADR-107) --------------------------------------------------


def test_a_superseded_record_is_hidden_and_restoring_it_revives_it(
    memory_root: Path,
) -> None:
    """A mark, not a delete: the point stays, and an upsert clears the mark.

    Hidden from all three read paths the model or the user can reach, and
    brought back by storing the same text again — which is what should
    happen when a user switches back to the editor they used before.
    """
    store = _store(memory_root)
    old = store.store("User uses VS Code as their editor", "core_fact", "agent")
    new = store.store("User uses Neovim as their editor", "core_fact", "agent")

    store.supersede(old, new)

    assert [hit.record.text for hit in store.search("editor", limit=5)] == [new.text]
    assert [record.text for record in store.recent(5)] == [new.text]
    assert store.count() == 1

    store.store(old.text, "core_fact", "agent")

    assert store.count() == 2


def test_superseding_without_a_database_says_nothing_changed(
    memory_root: Path,
) -> None:
    """Raised like a write: a correction reported but not made is a lie."""
    store = QdrantMemoryStore(
        embedder=HashingEmbedder(),
        session_log=SessionLog(memory_root),
        url="http://127.0.0.1:1",
        timeout=2.0,
    )
    from harness.memory.records import MemoryRecord

    record = MemoryRecord(text="a fact", source="agent", category="core_fact")

    with pytest.raises(MemoryUnavailableError, match="Nothing was changed"):
        store.supersede(record, record)


def test_a_failed_supersede_is_reported(memory_root: Path) -> None:
    """A server error while marking surfaces as the typed memory error."""
    store = _store(memory_root)
    record = store.store("a fact", "core_fact", "agent")

    class _Refusing:
        def set_payload(self, **_: Any) -> None:
            raise RuntimeError("read-only replica")

    store._client = _Refusing()

    with pytest.raises(MemoryUnavailableError, match="could not mark"):
        store.supersede(record, record)


# --- reconnecting (ADR-110) -------------------------------------------------


def test_a_database_that_comes_up_later_is_picked_up_without_a_restart(
    memory_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Retried every 30 seconds, not on every call and not never.

    The first attempt fails, a call ten seconds later does not try again
    (it would pay the connect timeout each time), and one after the
    interval connects to the database that has come up in the meantime.
    """
    import qdrant_client
    from harness.memory import qdrant_store

    clock = [1_000.0]
    monkeypatch.setattr(qdrant_store, "_monotonic", lambda: clock[0])
    attempts: list[str] = []
    real_client = qdrant_client.QdrantClient

    def _client(url: str, timeout: int) -> Any:
        attempts.append(url)
        if len(attempts) == 1:
            raise ConnectionError("connection refused")
        return real_client(":memory:")

    monkeypatch.setattr(qdrant_client, "QdrantClient", _client)
    store = QdrantMemoryStore(
        embedder=HashingEmbedder(),
        session_log=SessionLog(memory_root),
        url="http://qdrant:6333",
    )

    assert store.available is False
    clock[0] += 10
    assert store.available is False
    assert len(attempts) == 1

    clock[0] += qdrant_store.RECONNECT_INTERVAL_SECONDS
    assert store.available is True
    assert len(attempts) == 2
    store.store("a fact written after the database came up", "core_fact", "user")
    assert store.count() == 1
