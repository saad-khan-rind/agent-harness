"""Long-term memory seam.

Memory is **records in a vector collection**, not files. The shape of this
protocol follows from that choice (ADR-065, superseding ADR-033):

* every durable fact is one point in one collection, carrying the payload
  defined in :mod:`harness.memory.records`;
* the always-injected tier is not a file but a *query* — the top core facts
  by relevance to what the user just asked, re-ranked by recency;
* the just-in-time tier is not a list of paths but :meth:`search`, which
  the agent calls when the injected block did not already contain what it
  needs.

That is still the two-tier retrieval the brief describes — "dropping small,
high-value files into the context up front, and keeping lightweight
identifiers that let the agent load information just-in-time" (Section 2,
*Long-term memory*) — with the identifiers replaced by a semantic query.
The trade is stated in ADR-065: the harness gains recall that does not
depend on the model guessing a filename, and takes on a scoring function it
now has to justify, plus a service that can be down.

Because it can be down, **every method on this protocol is total**. There
is no "memory unavailable" exception for callers to handle: a read returns
nothing, a write returns a refusal string the model can act on, and the run
continues. Memory is a convenience layered on the agent, never a
precondition for one — the same rule the file store followed when a
directory was unreadable, applied to a network dependency.

Two invariants implementations own:

1. **Provenance is stamped, never accepted.** The caller says who is
   writing; the text is cleaned of any tag it arrived with, before it is
   embedded and before it is stored (ADR-062, preserved).
2. **Budgets.** One record has a text ceiling and one query has a result
   ceiling, so a runaway write cannot make memory unusable for every later
   session and a broad query cannot empty the context window.

There is still no ``delete`` on this protocol and no delete tool built on
it. ADR-008 says the agent cannot remove anything. :meth:`upsert_slot`
replaces one point by id, which is the single narrow exception argued in
ADR-063: it is the mechanism that stops a slot accumulating contradictory
copies of itself, it is reachable only for four known keys, and it refuses
to overwrite anything the user recorded.

Every other fact is corrected with :meth:`supersede`, which removes nothing:
it marks the old record so the read paths skip it (ADR-107). Deciding *when*
one fact corrects another is not the store's job — that is
:class:`~harness.memory.consolidate.MemoryWriter`, which sits in front of
this seam on every write path.
"""

from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from harness.memory.records import Category, MemoryRecord, Source


@dataclass(frozen=True)
class Hit:
    """One retrieval result: the record, its cosine, and its final score.

    A concrete type on the seam rather than a ``Protocol``, and deliberately
    so. ``list`` is invariant, so a store returning ``list[SomeHitImpl]``
    would not satisfy a protocol declaring ``list[ScoredRecord]`` however
    compatible the elements were — the seam would type-check only if every
    caller and every implementation agreed on one class anyway. Naming that
    class here is the honest version of the same constraint.

    Both scores are kept, not just the final one. With only the combined
    number, "why did that rank first" has no answer short of re-deriving it
    from the timestamp, which is exactly the question asked when retrieval
    surprises someone.

    Attributes:
        record: The stored record.
        similarity: Raw cosine similarity, before any decay.
        score: Final rank score, cosine combined with recency (ADR-067).
    """

    record: MemoryRecord
    similarity: float
    score: float


class MemoryStore(Protocol):
    """Durable memory shared by the harness, the agent and the user."""

    @property
    def root(self) -> Path:
        """Return the memory volume, which now holds only the session log.

        Kept on the seam because every :class:`~harness.tools.base.Tool`
        carries a confinement root and the permission policy reads it. The
        memory tools no longer join anything onto it — they cannot reach
        the filesystem at all — so this is the path the harness owns, not a
        path the agent can address.
        """

    @property
    def available(self) -> bool:
        """Return whether the backing collection is currently usable.

        Checked by the REPL to tell the user why ``/memory`` is empty. Never
        checked as a precondition by the read or write paths — they degrade
        on their own.
        """

    @property
    def describe(self) -> str:
        """Return a one-line description of the backend and embedder.

        Shown in the startup banner and by ``/memory``. It exists because
        the two failure modes of this subsystem — no database, and no
        embedding model — are both silent otherwise, and a demo where
        retrieval is quietly lexical should say so on screen.
        """

    def store(
        self,
        text: str,
        category: Category,
        source: Source,
        slot_key: str | None = None,
    ) -> MemoryRecord:
        """Embed one fact and insert it.

        Args:
            text: The fact. Cleaned and flattened by the implementation; a
                leading provenance tag is stripped rather than trusted.
            category: Which tier this belongs to.
            source: Who is recording it. Stamped by the caller, never read
                out of ``text``.
            slot_key: Canonical slot name for a slotted fact, else ``None``.

        Returns:
            The record as stored, with the timestamp the implementation
            assigned.

        Raises:
            ValueError: If the text is empty or over the record ceiling, or
                the category, source or slot key is unknown.
            MemoryUnavailableError: If the collection cannot be written.
                Raised rather than swallowed *only here*: a write that
                silently did nothing would have the model believe it had
                remembered something it had not.
        """

    def upsert_slot(self, slot_key: str, text: str, source: Source) -> MemoryRecord:
        """Replace the fact held in one slot, or create it.

        The overwrite mechanic the file store implemented by rewriting a
        line (ADR-063). Here it is a filtered lookup, a delete by point id,
        and an insert.

        Args:
            slot_key: Slot name, any case. Must be a known slot.
            text: The new value.
            source: Who is recording it.

        Returns:
            The newly stored record.

        Raises:
            ValueError: If the slot is unknown, the value is empty, or the
                existing point was recorded by the user and ``source`` is
                not ``user``. The user wins.
            MemoryUnavailableError: If the collection cannot be written.
        """

    def search(
        self,
        query: str,
        limit: int,
        category: Category | None = None,
    ) -> list[Hit]:
        """Return records most relevant to ``query``, best first.

        Args:
            query: Natural-language query. Embedded with the same embedder
                that wrote the records.
            limit: Maximum hits to return. Clamped by the implementation.
            category: Restrict to one tier, or ``None`` for all of them.

        Returns:
            Ranked hits, or an empty list when the collection is empty,
            unreachable, or nothing scored above the floor. Never raises:
            this is the read path, and a failed read means "no memory this
            run", not a failed run.
        """

    def count(self, category: Category | None = None) -> int:
        """Return how many records are stored, for ``/memory`` and tests.

        Returns:
            The count, or ``0`` when the collection is unreachable.
        """

    def recent(
        self, limit: int, category: Category | None = None
    ) -> list[MemoryRecord]:
        """Return the newest records, newest first.

        A scan rather than a search, and the only read path that is not a
        query: ``/memory`` answers "what is stored", which has no query
        vector. Not offered to the model — a tool that dumps memory
        wholesale is how the context budget the retrieval exists to protect
        gets spent anyway.

        Args:
            limit: Most records to return.
            category: Restrict to one tier, or ``None`` for all.

        Returns:
            Records newest first, or an empty list when unreachable.
        """

    def slots(self) -> list[MemoryRecord]:
        """Return every slotted fact, in slot-key order.

        The second read path that is not a query, and the reason it is on
        the seam rather than private to the implementation: the four slots
        are the facts most certain to be worth injecting, and routing them
        through :meth:`search` made surfacing them contingent on the
        embedder scoring a paraphrase above the similarity floor. It does
        not reliably do that — ``"what do I like"`` misses a stored
        ``Likes:`` fact outright under the lexical fallback — so the hook
        reads them directly and spends the handful of characters every run.

        Bounded by construction: there are four known keys, one record
        each, so this cannot grow into the wholesale memory dump
        :meth:`recent` is kept away from the model to prevent.

        Returns:
            One record per set slot, or an empty list when none are set or
            the collection is unreachable. Never raises.
        """

    def supersede(self, old: MemoryRecord, new: MemoryRecord) -> None:
        """Hide ``old`` from every read path because ``new`` corrects it.

        The correction mechanic for facts that are not slots (ADR-107). It
        marks rather than deletes, so ADR-008 holds: the record stays in
        the collection, and re-storing its text clears the mark.

        Args:
            old: The record being corrected, as a read path returned it.
            new: The record that replaces it, already stored.

        Raises:
            MemoryUnavailableError: If the collection cannot be written.
        """

    def append_session(self, task: str, outcome: str, iterations: int) -> None:
        """Record one finished run.

        The harness-driven write trigger: it fires on every run end
        regardless of what the model decided, and costs no extra LLM call
        (ADR-036). Failures are swallowed — this runs from a ``finally``
        block, often while the run is already failing, and turning a bad run
        into a crashed one would replace a useful error with a confusing
        one.

        Args:
            task: The user request that was run.
            outcome: Short result summary.
            iterations: Loop iterations the run consumed.
        """


class MemoryUnavailableError(RuntimeError):
    """A memory write could not reach the backing collection."""
