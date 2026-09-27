"""Vector-backed long-term memory.

One Qdrant collection, ``agent_memory``, holding every durable fact as a
point with the payload defined in :mod:`harness.memory.records`. Cosine
distance, because the embedders here return L2-normalised vectors and
cosine is then a dot product with a bounded, interpretable range.

Three properties this class is responsible for, none of which Qdrant
provides on its own:

**It never takes a run down.** The database is a service on a network, so
it can be absent, starting up, or mid-restart. Every read path here catches
and returns empty; the write paths raise a single typed error the tools
turn into an ordinary observation. A missing Qdrant produces an agent with
no memory, which is exactly what ``memory_enabled: false`` produces, and
that has always been a supported way to run (ADR-069).

**It re-ranks.** Qdrant orders by cosine alone. The read path wants recency
too, so the store over-fetches and applies the decay in
:func:`decayed_score` before truncating to the caller's limit. Doing this
client-side rather than with a Qdrant formula query keeps the scoring
function in code that can be unit-tested without a server, which is the
only way it gets tested at all in a pipeline with no Docker (ADR-067).

**It stamps provenance.** ``source`` is an argument, never something read
out of the text. :func:`clean_text` runs before the text is embedded as
well as before it is stored, so a forged ``[user]`` tag cannot reach the
prompt *or* move the point.

Connection ownership: the client is built here and closed here. It is
constructed lazily on first use rather than in ``__init__`` so that
building the store — which the CLI does at startup, before anything is
known about whether the user's task needs memory at all — cannot block on
a TCP connect to a container that is still booting.
"""

import json
import time
import uuid
from pathlib import Path
from typing import Any

from harness.memory.embed import Embedder, EmbeddingError
from harness.memory.protocol import Hit, MemoryUnavailableError
from harness.memory.records import (
    FIELD_CATEGORY,
    FIELD_SLOT_KEY,
    FIELD_SUPERSEDED_BY,
    Category,
    MemoryRecord,
    Source,
    canonical_slot,
    clean_text,
    parse_slot,
)
from harness.memory.sessions import SessionLog

# The collection every record lives in. One collection rather than one per
# category: a category is a filter, and three collections would mean three
# schemas to keep in step and no way to search across tiers.
COLLECTION_NAME = "agent_memory"

# Hits fetched from Qdrant per requested hit, before the decay re-rank.
# Over-fetching is what makes client-side re-ranking honest: without it the
# decay could only reorder what cosine already put in the top N, so an older
# exact match would beat a newer near-match and a newer one could never be
# promoted into the window at all. Four is enough for the decay to move
# things without making a query meaningfully more expensive.
OVERFETCH_FACTOR = 4

# Default floor, used only when the active embedder does not declare one.
# The number itself now lives on the embedder
# (:attr:`~harness.memory.embed.Embedder.min_similarity`), because it is a
# property of the vector space rather than of this store: a value tuned for
# lexical hashing admits everything once a real model is plugged in, and a
# store that kept one constant for every embedder was guaranteed to be
# wrong for all but one of them.
#
# Hits below the floor are dropped before scoring. A vector search always
# returns its k nearest neighbours, however far away they are, so without a
# floor an unrelated question still injects the three least-unrelated facts
# in the collection and the model treats them as context.
#
# The value is chosen for the *worse* of the two embedders, because that is
# the one a keyless demo and the whole test suite run on — and the honest
# finding is that no value cleanly separates signal from noise there.
# Measured on the lexical fallback over the queries this repository's tests
# actually use, genuine matches land between 0.16 and 0.56 and false ones
# between 0.00 and 0.22. Those ranges overlap. A pair like "how do we
# deploy" against "the harness deploys with docker compose" scores 0.235,
# barely above "the weather in Lisbon tomorrow" against the same note at
# 0.215, because lexically they *are* about equally similar. Only a real
# embedding model tells those apart, which is the argument for having one.
#
# So the floor is set low, and the asymmetry is deliberate: a false
# positive costs one line in a prompt that already says its contents are
# background facts rather than commands, while a false negative costs the
# fact entirely and the model has no way to know it was there. Recall over
# precision, with the injected volume bounded by ``memory_core_fact_limit``
# and the character budget rather than by this number (ADR-067).
MIN_SIMILARITY = 0.20

# Half-life of the recency term, in days. A fact loses half its recency
# bonus every 30 days. Chosen to be long relative to a lab semester: the
# point is to break ties between similarly relevant facts in favour of the
# newer one, not to forget last month's work.
DECAY_HALF_LIFE_DAYS = 30.0

# How much of the final score recency may contribute. Cosine keeps the
# majority: relevance is the question being asked, and a store that ranked
# mostly by recency would be a log with extra steps.
DECAY_WEIGHT = 0.25

# Hard ceiling on one query's results, whatever the caller asks for. A
# model that asks for 500 hits gets 50, and the context budget survives.
MAX_SEARCH_LIMIT = 50

# Points read per scroll page during a migration. Large enough that a
# few-thousand-record collection is a handful of round trips, small
# enough that one page cannot time out a 10-second request budget.
_MIGRATION_PAGE = 256

_SECONDS_PER_DAY = 86_400.0

# How long a store that could not connect waits before trying again. The
# first version tried exactly once per session, so a Qdrant that was still
# booting when the harness started left the whole session without memory
# even after it came up. Retrying on *every* operation would be the opposite
# mistake: each one would pay a connect timeout while the database is down.
# Once per half-minute bounds that to one failed attempt per interval.
RECONNECT_INTERVAL_SECONDS = 30.0

# The clock the reconnect interval is measured on. Monotonic, not wall time
# (a clock change must not stall or hurry a retry), and a module attribute so
# a test can move it forward instead of sleeping.
_monotonic = time.monotonic


def decayed_score(similarity: float, timestamp: int, now: float) -> float:
    """Combine cosine similarity with an exponential recency bonus.

    The formula, stated plainly because a scoring function that cannot be
    explained in review should not ship (ADR-067)::

        recency = 0.5 ** (age_in_days / half_life)
        score   = (1 - w) * similarity + w * recency

    Both terms are in ``[0, 1]`` for normalised vectors, so the result is
    too, and the weight ``w`` reads directly as "recency is a quarter of the
    decision".

    An additive combination rather than a multiplicative one: multiplying
    would let age drive a perfectly relevant old fact arbitrarily close to
    zero, and the thing being built is a memory, not a cache.

    Args:
        similarity: Cosine similarity from Qdrant, expected in ``[0, 1]``.
        timestamp: The record's Unix seconds.
        now: Current Unix seconds. Injected so tests can age a record
            without waiting.

    Returns:
        The final rank score.
    """
    age_days = max(0.0, (now - timestamp) / _SECONDS_PER_DAY)
    recency: float = 0.5 ** (age_days / DECAY_HALF_LIFE_DAYS)
    return (1.0 - DECAY_WEIGHT) * similarity + DECAY_WEIGHT * recency


class QdrantMemoryStore:
    """Long-term memory in one Qdrant collection."""

    def __init__(
        self,
        embedder: Embedder,
        session_log: SessionLog,
        url: str = "http://localhost:6333",
        timeout: float = 10.0,
        collection: str = COLLECTION_NAME,
        client: Any | None = None,
        now: Any = time.time,
        min_similarity: float | None = None,
    ) -> None:
        """Args:
        embedder: Turns text into vectors. Its ``dimensions`` decides
            the collection's vector size, so a collection built with
            one embedder cannot be queried with another of a
            different width — the store detects that and refuses to
            use the collection rather than returning nonsense.
        session_log: The file-backed run log (ADR-068).
        url: Qdrant endpoint. Ignored when ``client`` is supplied.
        timeout: Seconds allowed for one Qdrant request.
        collection: Collection name, overridable for tests.
        client: A pre-built client, injected by the tests. Passing
            ``QdrantClient(":memory:")`` runs the whole store
            in-process, which is what lets the suite exercise this
            code with no server and no Docker in CI.
        now: Clock, injected so decay can be tested deterministically.
        min_similarity: Override for the cosine floor. ``None`` — the
            default — takes the active embedder's own value, which is
            the right behaviour: the floor belongs to the vector space,
            not to this store. Supplied only when a deployment has
            measured a better number for its own facts.
        """
        self._embedder = embedder
        # ``getattr`` so an embedder written before this property existed —
        # or a test double that implements only the three original members —
        # still satisfies the seam. The protocol grew; the implementations
        # are not all this module's to change.
        self._min_similarity = (
            min_similarity
            if min_similarity is not None
            else float(getattr(embedder, "min_similarity", MIN_SIMILARITY))
        )
        self._sessions = session_log
        self._url = url
        self._timeout = timeout
        self._collection = collection
        self._client = client
        self._now = now
        self._checked = client is not None
        self._ready = client is not None
        # A client handed in by a test is used as given: there is no URL to
        # reconnect to, so a failed one is not retried.
        self._injected = client is not None
        self._retry_at = 0.0
        self._failure = ""
        # Set by a successful rebuild, printed by the CLI through
        # :attr:`migration_note`. A migration that silently rewrote the
        # collection would be the same class of invisible behaviour as the
        # silent embedder fallback this release exists to fix.
        self._migration_note = ""
        if client is not None:
            self._ready = self._ensure_collection(client)

    # -- lifecycle ----------------------------------------------------------

    @property
    def root(self) -> Path:
        """Return the memory volume, which now holds only the session log."""
        return self._sessions.path.parent

    @property
    def available(self) -> bool:
        """Return whether the collection is usable, connecting if needed."""
        return self._connect() is not None

    @property
    def describe(self) -> str:
        """Return a one-line summary of the backend and embedder.

        Both silent failure modes of this subsystem are named here: a
        missing database and a fallback embedder. A demo where retrieval is
        quietly lexical should say so on screen rather than in a log file.
        """
        if not self.available:
            return f"unavailable ({self._failure or 'not connected'})"
        # Asked of the client itself, after connecting: a server client is a
        # client too, so whether one exists does not say which kind it is.
        backend = (
            "qdrant (in-process)" if _is_local(self._client) else f"qdrant {self._url}"
        )
        return f"{backend}, embeddings: {self._embedder.name}"

    @property
    def migration_note(self) -> str:
        """Return what a collection rebuild did, or an empty string.

        A rebuild re-embeds every record and backs up the old collection, so
        it must not happen with nothing on screen; the CLI prints this note
        (ADR-110).
        """
        return self._migration_note

    def close(self) -> None:
        """Release the client. Safe to call when never connected."""
        client = self._client
        self._client = None
        self._ready = False
        self._checked = False
        if client is None:
            return
        try:
            client.close()
        except Exception:  # noqa: BLE001 - shutdown must not raise
            return

    def _connect(self) -> Any | None:
        """Return a ready client, or ``None``.

        A failed connection is retried, but at most once per
        :data:`RECONNECT_INTERVAL_SECONDS`. Retrying on every call would add
        the connect timeout to every memory operation while the database is
        down, turning a degraded session into an unusably slow one; never
        retrying would leave a whole session without memory because Qdrant
        finished booting a few seconds after the harness (ADR-110).
        """
        if self._ready:
            return self._client
        if self._checked and (self._injected or _monotonic() < self._retry_at):
            return None
        self._checked = True
        # Set before the attempt, so every failure path below — there are
        # several, and a missed one would retry on every call — waits.
        self._retry_at = _monotonic() + RECONNECT_INTERVAL_SECONDS
        try:
            from qdrant_client import QdrantClient
        except ImportError as exc:  # pragma: no cover - dependency is pinned
            self._failure = f"qdrant-client not installed: {exc}"
            return None
        try:
            client = QdrantClient(url=self._url, timeout=int(self._timeout))
        except Exception as exc:  # noqa: BLE001 - client construction is open-ended
            self._failure = f"cannot reach {self._url}: {exc}"
            return None
        if not self._ensure_collection(client):
            try:
                client.close()
            except Exception:  # noqa: BLE001 - shutdown must not raise
                pass
            return None
        self._client = client
        self._ready = True
        self._failure = ""
        return client

    def _ensure_collection(self, client: Any) -> bool:
        """Create the collection if absent, and check its width if present.

        The width check is the important half. A collection created while
        the embedding endpoint was reachable holds 1536-dimensional points;
        the same volume mounted into a run that fell back to the local
        embedder would be queried with 256-dimensional vectors. Qdrant
        rejects the query, but only at query time, once per call. Detecting
        it here turns a stream of confusing per-call errors into one clear
        startup message.
        """
        from qdrant_client import models

        try:
            if not client.collection_exists(self._collection):
                client.create_collection(
                    collection_name=self._collection,
                    vectors_config=models.VectorParams(
                        size=self._embedder.dimensions,
                        distance=models.Distance.COSINE,
                    ),
                )
                # Payload indexes on the two fields every filter uses.
                # Without them Qdrant still filters correctly, by scanning
                # every point — fine at a hundred facts, not at a hundred
                # thousand.
                #
                # Skipped in local mode, where the engine has no indexes and
                # warns on every call. The tests run in local mode, so
                # without this guard the suite emits two warnings per store
                # it builds, and a suite that is noisy by default is a suite
                # whose real warnings go unread.
                if not _is_local(client):
                    for field in (FIELD_CATEGORY, FIELD_SLOT_KEY):
                        client.create_payload_index(
                            collection_name=self._collection,
                            field_name=field,
                            field_schema=models.PayloadSchemaType.KEYWORD,
                        )
                return True
            info = client.get_collection(self._collection)
            width = _collection_width(info)
            if width is not None and width != self._embedder.dimensions:
                return self._migrate_collection(client, width)
        except Exception as exc:  # noqa: BLE001 - server errors are open-ended
            self._failure = f"cannot prepare collection: {exc}"
            return False
        return True

    def _migrate_collection(self, client: Any, old_width: int) -> bool:
        """Rebuild the collection at the active embedder's width.

        A width change used to disable memory for the run and tell the user
        to drop the collection — which is to say, to throw their facts away.
        That was the wrong trade, and it was never necessary: the *text* of
        every record is in its payload, so the vectors can be recomputed.
        Only the vectors are width-specific; nothing that was actually
        remembered is.

        The order is chosen so that an interruption cannot lose data:

        1. Read every payload out and write it to a JSON file beside the
           session log. That file is the thing that makes the rest safe —
           after it exists, the collection is reconstructible by hand.
        2. Re-embed. This is the slow step and the one that can fail (a
           local model can OOM, an endpoint can drop), so it happens
           *before* anything is destroyed. A failure here leaves the old
           collection untouched and the run degrades as it did before.
        3. Only once every new vector is in hand: drop and recreate.

        The backup is deliberately not deleted on success. It costs a few
        kilobytes and it is the only copy of what the collection held before
        a rebuild nobody explicitly asked for.

        Args:
            client: Connected Qdrant client.
            old_width: The width the collection was built at, for the
                message.

        Returns:
            ``True`` when the collection is usable afterwards. ``False``
            leaves ``_failure`` set and memory off for this run, which is
            the same degradation as an unreachable database.
        """
        from qdrant_client import models

        try:
            records = self._read_all_payloads(client)
        except Exception as exc:  # noqa: BLE001 - server errors are open-ended
            self._failure = (
                f"collection {self._collection!r} is {old_width}-dimensional "
                f"but the active embedder produces {self._embedder.dimensions}, "
                f"and its contents could not be read to rebuild it: {exc}. "
                f"Memory is off for this run; nothing was changed."
            )
            return False

        backup = self._write_backup(records)

        try:
            points = [
                models.PointStruct(
                    id=_point_id(record),
                    vector=self._embedder.embed(record.text),
                    payload=record.as_payload(),
                )
                for record in records
            ]
        except Exception as exc:  # noqa: BLE001 - embedder errors are open-ended
            self._failure = (
                f"collection {self._collection!r} needs rebuilding from "
                f"{old_width} to {self._embedder.dimensions} dimensions, but "
                f"re-embedding failed: {exc}. Memory is off for this run; the "
                f"collection was not touched"
                + (f" and its contents are saved at {backup}." if backup else ".")
            )
            return False

        try:
            client.delete_collection(self._collection)
            client.create_collection(
                collection_name=self._collection,
                vectors_config=models.VectorParams(
                    size=self._embedder.dimensions,
                    distance=models.Distance.COSINE,
                ),
            )
            if not _is_local(client):
                for field in (FIELD_CATEGORY, FIELD_SLOT_KEY):
                    client.create_payload_index(
                        collection_name=self._collection,
                        field_name=field,
                        field_schema=models.PayloadSchemaType.KEYWORD,
                    )
            if points:
                client.upsert(collection_name=self._collection, points=points)
        except Exception as exc:  # noqa: BLE001 - server errors are open-ended
            self._failure = (
                f"rebuilding collection {self._collection!r} failed partway: "
                f"{exc}. Memory is off for this run"
                + (
                    f"; its contents are saved at {backup} and can be restored by hand."
                    if backup
                    else "."
                )
            )
            return False

        self._migration_note = (
            f"rebuilt memory at {self._embedder.dimensions} dimensions "
            f"(was {old_width}), re-embedding {len(points)} record(s)"
            + (f"; previous contents saved at {backup}" if backup else "")
        )
        return True

    def _read_all_payloads(self, client: Any) -> list[MemoryRecord]:
        """Return every record in the collection, paging through the scroll.

        Pages rather than asking for everything at once: the caller is a
        migration, so the collection is however large the user let it get,
        and one unbounded request is how that becomes a timeout.
        """
        records: list[MemoryRecord] = []
        offset: Any = None
        while True:
            points, offset = client.scroll(
                collection_name=self._collection,
                limit=_MIGRATION_PAGE,
                offset=offset,
                with_payload=True,
                with_vectors=False,
            )
            for point in points:
                record = MemoryRecord.from_payload(getattr(point, "payload", None))
                if record is not None:
                    records.append(record)
            if offset is None:
                return records

    def _write_backup(self, records: list[MemoryRecord]) -> Path | None:
        """Write the records to a timestamped JSON file, or return ``None``.

        Best-effort by design: a read-only memory volume is a reason to
        proceed without a safety net, not a reason to refuse a migration the
        user cannot otherwise get past. The caller reports whether a backup
        exists, so the failure is visible rather than assumed away.
        """
        if not records:
            return None
        path = self.root / f"memory-backup-{int(self._now())}.json"
        try:
            path.write_text(
                json.dumps(
                    [record.as_payload() for record in records],
                    ensure_ascii=False,
                    indent=2,
                ),
                encoding="utf-8",
            )
        except OSError:
            return None
        return path

    # -- writes -------------------------------------------------------------

    def store(
        self,
        text: str,
        category: Category,
        source: Source,
        slot_key: str | None = None,
    ) -> MemoryRecord:
        """Embed one fact and insert it. See :meth:`MemoryStore.store`."""
        record = MemoryRecord(
            text=_require_text(text),
            source=source,
            category=category,
            timestamp=int(self._now()),
            slot_key=slot_key,
        )
        self._insert(record)
        return record

    def upsert_slot(self, slot_key: str, text: str, source: Source) -> MemoryRecord:
        """Replace one slot's fact. See :meth:`MemoryStore.upsert_slot`."""
        key = canonical_slot(slot_key)
        if key is None:
            from harness.memory.records import SLOT_KEYS

            raise ValueError(
                f"unknown slot {slot_key!r}. Known slots are "
                f"{', '.join(SLOT_KEYS)}. Use memory_store for anything else."
            )
        # ``Name: Saad`` passed as the text of a Name upsert is the model
        # repeating the key. Storing it would inject "Name: Name: Saad".
        parsed = parse_slot(text)
        value = parsed[1] if parsed is not None and parsed[0] == key else text
        cleaned = _require_text(value, f"cannot remember an empty {key}")

        existing = self._find_slot(key)
        if existing is not None:
            point_id, holder = existing
            if holder == "user" and source != "user":
                raise ValueError(
                    f"refusing to replace {key}: that slot was recorded by "
                    f"the user with /remember. The user wins."
                )
            self._delete_point(point_id)

        record = MemoryRecord(
            text=f"{key}: {cleaned}",
            source=source,
            category="core_fact",
            timestamp=int(self._now()),
            slot_key=key,
        )
        self._insert(record)
        return record

    def append_session(self, task: str, outcome: str, iterations: int) -> None:
        """Append one run to the file-backed log (ADR-068). Never raises."""
        self._sessions.append(task, outcome, iterations)

    def supersede(self, old: MemoryRecord, new: MemoryRecord) -> None:
        """Hide ``old`` from every read path because ``new`` corrects it.

        A payload write, not a delete. The point keeps its vector and its
        text and gains ``superseded_by``; every read filters on that key
        being absent, so the fact stops reaching prompts and searches while
        staying on disk for anyone auditing what the agent once believed.
        That is what keeps ADR-008 true of this path: nothing is removed,
        and storing the old text again clears the mark, because an upsert
        replaces the payload (ADR-107).

        The point is addressed by :func:`_point_id`, which is derived from
        content. The caller holds a record, not an id, and the id it would
        need is exactly the one the record hashes to.

        Args:
            old: The record being corrected, as a read path returned it.
            new: The record that replaces it, already stored.

        Raises:
            MemoryUnavailableError: If the collection cannot be written.
                Raised for the same reason :meth:`store` raises: a caller
                told "updated" when the old fact is still live would report
                a correction that did not happen.
        """
        client = self._connect()
        if client is None:
            reason = self._failure or "not connected"
            raise MemoryUnavailableError(
                f"long-term memory is unavailable ({reason}). Nothing was changed."
            )
        try:
            client.set_payload(
                collection_name=self._collection,
                payload={FIELD_SUPERSEDED_BY: _point_id(new)},
                points=[_point_id(old)],
            )
        except Exception as exc:  # noqa: BLE001 - server errors are open-ended
            raise MemoryUnavailableError(
                f"could not mark the old fact as replaced: {exc}"
            ) from exc

    # -- reads --------------------------------------------------------------

    def search(
        self,
        query: str,
        limit: int,
        category: Category | None = None,
    ) -> list[Hit]:
        """Return the best-scoring records. See :meth:`MemoryStore.search`.

        Total: any failure returns an empty list. This is the path the hook
        calls on every run, and a memory backend that is briefly unreachable
        must produce a run without memory, not a run that fails to start.
        """
        capped = max(0, min(limit, MAX_SEARCH_LIMIT))
        if capped == 0:
            return []
        client = self._connect()
        if client is None:
            return []
        try:
            vector = self._embedder.embed(clean_text(query))
        except EmbeddingError:
            # The endpoint went away mid-session. Records already stored
            # remain queryable once it returns; this run gets no memory.
            return []

        try:
            response = client.query_points(
                collection_name=self._collection,
                query=vector,
                limit=capped * OVERFETCH_FACTOR,
                query_filter=_active_filter(category),
                with_payload=True,
            )
        except Exception:  # noqa: BLE001 - server errors are open-ended
            return []

        now = float(self._now())
        hits: list[Hit] = []
        for point in getattr(response, "points", []):
            record = MemoryRecord.from_payload(getattr(point, "payload", None))
            if record is None:
                continue
            similarity = float(getattr(point, "score", 0.0) or 0.0)
            if similarity < self._min_similarity:
                continue
            hits.append(
                Hit(
                    record=record,
                    similarity=similarity,
                    score=decayed_score(similarity, record.timestamp, now),
                )
            )
        hits.sort(key=lambda hit: hit.score, reverse=True)
        return hits[:capped]

    def count(self, category: Category | None = None) -> int:
        """Return how many active records are stored. Never raises.

        Superseded records are not counted: ``/memory`` answers "what will
        the next session know", and a corrected fact is not part of that.
        """
        client = self._connect()
        if client is None:
            return 0

        try:
            return int(
                client.count(
                    collection_name=self._collection,
                    count_filter=_active_filter(category),
                    exact=True,
                ).count
            )
        except Exception:  # noqa: BLE001 - server errors are open-ended
            return 0

    def recent(
        self, limit: int, category: Category | None = None
    ) -> list[MemoryRecord]:
        """Return the newest records, newest first. For ``/memory`` only.

        A scan, not a search: the REPL command shows what is stored, and
        "what is stored" is not a question with a query vector.
        """
        client = self._connect()
        if client is None:
            return []

        try:
            points, _ = client.scroll(
                collection_name=self._collection,
                scroll_filter=_active_filter(category),
                limit=max(0, min(limit, MAX_SEARCH_LIMIT)) * OVERFETCH_FACTOR,
                with_payload=True,
                with_vectors=False,
            )
        except Exception:  # noqa: BLE001 - server errors are open-ended
            return []
        records = [
            record
            for record in (
                MemoryRecord.from_payload(getattr(point, "payload", None))
                for point in points
            )
            if record is not None
        ]
        records.sort(key=lambda record: record.timestamp, reverse=True)
        return records[:limit]

    def slots(self) -> list[MemoryRecord]:
        """Return every slotted fact, in :data:`SLOT_KEYS` order. Never raises.

        A filtered scan on ``slot_key``, not a search: the four slots are a
        closed set of facts that are *always* worth knowing, so "which of
        them are set" has no query vector any more than ``/memory`` does.

        This exists because the vector path cannot be relied on to surface
        them. With the lexical fallback embedder, "what do I like" against a
        stored ``Likes: swimming, going to the gym, and casual morning runs``
        scores 0.115 — below :data:`MIN_SIMILARITY` — because the two share
        no trigrams; ``preferences`` against ``likes`` scores exactly 0.0. A
        real embedding model raises that hit rate but does not make it
        certain, and for four short facts bounded by a closed key set, a
        deterministic read is strictly better than a probabilistic one at a
        cost of roughly forty characters of context (ADR-063's slots, given
        the read path they always implied).

        Returns:
            One record per set slot, ordered by :data:`SLOT_KEYS` rather
            than by recency, so the injected block is stable across turns.
            Empty when no slot is set or the collection is unreachable.
        """
        client = self._connect()
        if client is None:
            return []

        from qdrant_client import models

        from harness.memory.records import SLOT_KEYS

        try:
            points, _ = client.scroll(
                collection_name=self._collection,
                scroll_filter=models.Filter(
                    must=[
                        models.FieldCondition(
                            key=FIELD_SLOT_KEY,
                            match=models.MatchAny(any=list(SLOT_KEYS)),
                        ),
                        _not_superseded(),
                    ]
                ),
                limit=len(SLOT_KEYS),
                with_payload=True,
                with_vectors=False,
            )
        except Exception:  # noqa: BLE001 - server errors are open-ended
            return []

        records = [
            record
            for record in (
                MemoryRecord.from_payload(getattr(point, "payload", None))
                for point in points
            )
            if record is not None and record.slot_key is not None
        ]
        order = {key: index for index, key in enumerate(SLOT_KEYS)}
        records.sort(key=lambda record: order.get(record.slot_key or "", len(order)))
        return records

    # -- helpers ------------------------------------------------------------

    def _insert(self, record: MemoryRecord) -> None:
        """Embed and upsert one record.

        Raises:
            MemoryUnavailableError: If the collection or the embedder is
                unreachable. Unlike the read paths this does raise: a write
                that silently did nothing would have the model believe it
                had remembered something it had not, and act on that belief
                for the rest of the session.
        """
        client = self._connect()
        if client is None:
            reason = self._failure or "not connected"
            raise MemoryUnavailableError(
                f"long-term memory is unavailable ({reason}). Nothing was stored."
            )
        try:
            vector = self._embedder.embed(record.text)
        except EmbeddingError as exc:
            raise MemoryUnavailableError(
                f"could not embed the fact, so nothing was stored: {exc}"
            ) from exc

        from qdrant_client import models

        try:
            client.upsert(
                collection_name=self._collection,
                points=[
                    models.PointStruct(
                        id=_point_id(record),
                        vector=vector,
                        payload=record.as_payload(),
                    )
                ],
            )
        except Exception as exc:  # noqa: BLE001 - server errors are open-ended
            raise MemoryUnavailableError(f"memory write failed: {exc}") from exc

    def _find_slot(self, key: str) -> tuple[Any, str] | None:
        """Return ``(point_id, source)`` for the point holding ``key``.

        Returns ``None`` when the slot is empty or the lookup failed. A
        failed lookup deliberately reads as "empty": the insert that
        follows will fail on its own if the server is genuinely gone, and
        reporting the connection error from here would name the wrong
        operation.
        """
        client = self._connect()
        if client is None:
            return None

        from qdrant_client import models

        try:
            points, _ = client.scroll(
                collection_name=self._collection,
                scroll_filter=models.Filter(
                    must=[
                        models.FieldCondition(
                            key=FIELD_SLOT_KEY,
                            match=models.MatchValue(value=key),
                        )
                    ]
                ),
                limit=1,
                with_payload=True,
                with_vectors=False,
            )
        except Exception:  # noqa: BLE001 - server errors are open-ended
            return None
        if not points:
            return None
        payload = getattr(points[0], "payload", None) or {}
        return points[0].id, str(payload.get("source", "agent"))

    def _delete_point(self, point_id: Any) -> None:
        """Remove one point by id.

        The single narrow exception to ADR-008, argued in ADR-063: it is
        reachable only from :meth:`upsert_slot`, only for the four known
        slots, and only after the user-ownership check. There is no tool
        that reaches it with an arbitrary id, which is what keeps "the agent
        cannot delete anything" true of the tool surface.
        """
        client = self._connect()
        if client is None:
            return

        from qdrant_client import models

        try:
            client.delete(
                collection_name=self._collection,
                points_selector=models.PointIdsList(points=[point_id]),
            )
        except Exception as exc:  # noqa: BLE001 - server errors are open-ended
            raise MemoryUnavailableError(
                f"could not replace the old value, so nothing was changed: {exc}"
            ) from exc


def _not_superseded() -> Any:
    """Return the condition "no newer record has replaced this one".

    ``IsEmpty`` rather than a match on a sentinel value: the key is absent
    on every record that was never corrected, including every record
    written before ADR-107, so absence has to be what "active" means.
    """
    from qdrant_client import models

    return models.IsEmptyCondition(
        is_empty=models.PayloadField(key=FIELD_SUPERSEDED_BY)
    )


def _active_filter(category: Category | None) -> Any:
    """Return the filter every read path uses: active records, one tier or all.

    One function so that a new read path cannot forget the superseded
    check. Forgetting it would not fail loudly; it would quietly put a
    corrected fact back in front of the model next to its correction, which
    is the contradiction the mark exists to prevent (ADR-107).

    Args:
        category: Restrict to one tier, or ``None`` for all of them.

    Returns:
        A Qdrant ``Filter``.
    """
    from qdrant_client import models

    conditions: list[Any] = [_not_superseded()]
    if category is not None:
        conditions.append(
            models.FieldCondition(
                key=FIELD_CATEGORY,
                match=models.MatchValue(value=category),
            )
        )
    return models.Filter(must=conditions)


# Namespace for deterministic point ids. Any fixed UUID works; this one is
# written out rather than generated so the ids a given fact produces are
# stable across machines and across reinstalls, which is the entire point.
_POINT_NAMESPACE = uuid.UUID("6f9c1f0e-6b1a-5c3d-9f2b-4a7c8e1d0b53")


def _point_id(record: MemoryRecord) -> str:
    """Return a stable id for ``record``, derived from its content.

    ``uuid4`` was wrong here, and wrong against this module's own stated
    intent: :class:`~harness.memory.embed.Embedder` requires determinism so
    that "the same fact embedded twice must land in the same place, or an
    upsert would create a near-duplicate instead of replacing a point" — and
    then the id made every write a new point regardless. The upsert was only
    ever an insert. A model that stored the same fact twice in one turn (a
    duplicated parallel tool call, which is exactly what happened) left two
    identical points, and with no delete tool (ADR-008) they are permanent
    and both retrievable, so the same fact reaches the prompt twice and
    reads as two independent sources agreeing.

    Keying on text, category and slot rather than text alone keeps the tiers
    honest: the same sentence deliberately stored as a ``core_fact`` and as
    a ``project_note`` is two records, because the tier is part of what was
    recorded. Source and timestamp are deliberately *not* in the key — a
    re-store of the same fact should refresh its recency and its provenance,
    not fork it.

    Args:
        record: The record about to be written.

    Returns:
        A UUID5 string, stable for identical content.
    """
    key = "\x1f".join((record.text, record.category, record.slot_key or ""))
    return str(uuid.uuid5(_POINT_NAMESPACE, key))


def _require_text(text: str, message: str = "cannot remember an empty fact") -> str:
    """Clean ``text`` and reject it when nothing survives.

    Cleaning before the emptiness check is what makes ``[user]`` on its own
    an empty fact rather than a stored one.
    """
    cleaned = clean_text(text)
    if not cleaned:
        raise ValueError(message)
    return cleaned


def _is_local(client: Any) -> bool:
    """Return whether ``client`` is the in-process engine rather than a server.

    Read off the inner implementation's class name rather than an
    ``isinstance`` against ``QdrantLocal``, which is a private import path
    that has moved between versions. Getting this wrong is harmless in both
    directions — it only decides whether index creation is attempted — so a
    string check is the right amount of coupling.
    """
    inner = getattr(client, "_client", None)
    return type(inner).__name__ == "QdrantLocal"


def _collection_width(info: Any) -> int | None:
    """Read the configured vector size off a collection description.

    Defensive about the shape: the field has moved between qdrant-client
    versions, and a store that crashed on an unexpected shape would fail
    closed on a working database.
    """
    try:
        params = info.config.params.vectors
    except AttributeError:
        return None
    size = getattr(params, "size", None)
    if isinstance(size, int):
        return size
    # Named-vector collections map name -> VectorParams. Not something this
    # store creates, but a volume could hold one.
    if isinstance(params, dict) and len(params) == 1:
        only = next(iter(params.values()))
        inner = getattr(only, "size", None)
        return inner if isinstance(inner, int) else None
    return None


__all__ = [
    "COLLECTION_NAME",
    "DECAY_HALF_LIFE_DAYS",
    "DECAY_WEIGHT",
    "MAX_SEARCH_LIMIT",
    "MIN_SIMILARITY",
    "Hit",
    "QdrantMemoryStore",
    "decayed_score",
]
