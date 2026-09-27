"""The memory record schema, and the rules a record must satisfy.

Every point in the ``agent_memory`` collection carries the same payload, and
the shape is fixed here rather than being whatever the calling site happened
to put in a dict (ADR-066). A vector database will accept any JSON as a
payload, which is precisely why the schema has to be enforced in code: an
unvalidated payload field is discovered when a filter silently matches
nothing, months later.

Five fields:

``text``
    The fact itself. What was embedded, and what is shown to the model.
``source``
    ``user``, ``agent`` or ``harness``. Provenance is what lets the injected
    block separate a fact the user stated from one the model inferred while
    reading a possibly hostile file (ADR-034), and it is the first question
    asked when memory turns out to be wrong.
``timestamp``
    Unix seconds. Feeds the recency term of the retrieval score (ADR-067)
    and nothing else — it is not a version, and a later timestamp does not
    supersede an earlier one on its own.
``category``
    ``core_fact`` or ``project_note``. What used to be the difference
    between ``MEMORY.md`` and ``notes/*.md``. Filtering on it is how the
    two-tier read path survives the move: core facts are injected up front,
    project notes are reachable by search. There is no third value: the run
    log never moved into the collection (ADR-068), so a ``session_log``
    category was a filter that could only ever match nothing (ADR-107).
``slot_key``
    Optional. Present only on slotted facts (``Name``, ``Likes``,
    ``Locale``, ``Language``), where a second write replaces the first
    rather than stacking a ghost (ADR-063, preserved).

One more payload key exists that is *not* a record field:
``superseded_by``, written by
:meth:`~harness.memory.qdrant_store.QdrantMemoryStore.supersede` when a
newer fact corrects this one. Every read path filters on its absence, so a
record that carries it is invisible to retrieval and still present on disk
(ADR-107). It is not on :class:`MemoryRecord` because nothing the read path
returns can have it.

The forged-tag rule lives here too, next to the schema it protects, because
it is a property of what may be *stored*, not of any one tool.
"""

import re
import time
from dataclasses import dataclass, field
from typing import Any, Literal, get_args

# Provenance. Kept as a Literal so mypy rejects a fourth value at the call
# site instead of it reaching Qdrant and being discovered by a filter that
# quietly returns nothing.
Source = Literal["user", "agent", "harness"]

# What kind of memory this is. The file store had these as two different
# files; as a payload field they become one collection the read path can
# slice. The run log was a third file and stayed one (ADR-068, ADR-107).
Category = Literal["core_fact", "project_note"]

SOURCES: tuple[str, ...] = get_args(Source)
CATEGORIES: tuple[str, ...] = get_args(Category)

# Payload field names, referenced by the store's filters and by the tests.
# Named constants because a typo in a Qdrant filter key is not an error —
# it is a filter that matches nothing.
FIELD_TEXT = "text"
FIELD_SOURCE = "source"
FIELD_TIMESTAMP = "timestamp"
FIELD_CATEGORY = "category"
FIELD_SLOT_KEY = "slot_key"
# Set on a record a newer one corrected. Present means hidden (ADR-107).
FIELD_SUPERSEDED_BY = "superseded_by"

# Slotted facts, carried over unchanged from the file store (ADR-063). The
# set is closed on purpose: an open slot namespace lets the model invent a
# key per session and every fact becomes its own slot, which is the same as
# having no slots at all.
SLOT_KEYS: tuple[str, ...] = ("Name", "Likes", "Locale", "Language")

# Ceiling on one record's text. The file store capped a whole file at
# 100 kB; a record is one fact, so the cap is far lower and is about
# retrieval quality as much as storage — a 10 kB "fact" embeds to a point
# that is near nothing in particular.
MAX_TEXT_CHARS = 4_000

# A leading source tag the caller already wrote. The store stamps
# provenance itself, so a model-supplied ``[user]`` would either nest tags
# or, worse, look like a user-authored line in the injected block. The agent
# does not get to promote itself to human authority (ADR-062, preserved).
_PROVENANCE_PREFIX = re.compile(
    r"^(?:[-*]\s*)?[\[(](?:user|agent|harness|human|system)[\])]\s*[:\-]?\s*",
    re.IGNORECASE,
)

_SLOT_ALT = "|".join(SLOT_KEYS)
_SLOT_BODY = re.compile(rf"^({_SLOT_ALT})\s*:\s*(.*)$", re.IGNORECASE)


@dataclass(frozen=True)
class MemoryRecord:
    """One validated memory row, ready to become a Qdrant point."""

    text: str
    source: Source
    category: Category
    timestamp: int = field(default_factory=lambda: int(time.time()))
    slot_key: str | None = None

    def __post_init__(self) -> None:
        """Validate every field, because Qdrant will not.

        Raises:
            ValueError: On empty text, over-long text, an unknown source or
                category, an unknown slot key, or a negative timestamp.
        """
        if not self.text.strip():
            raise ValueError("cannot remember an empty fact")
        if len(self.text) > MAX_TEXT_CHARS:
            raise ValueError(
                f"refusing to store {len(self.text)} characters in one "
                f"memory record: the limit is {MAX_TEXT_CHARS}. Split it "
                f"into separate facts."
            )
        if self.source not in SOURCES:
            raise ValueError(f"source must be one of {SOURCES}, got {self.source!r}")
        if self.category not in CATEGORIES:
            raise ValueError(
                f"category must be one of {CATEGORIES}, got {self.category!r}"
            )
        if self.slot_key is not None and self.slot_key not in SLOT_KEYS:
            raise ValueError(
                f"slot_key must be one of {SLOT_KEYS}, got {self.slot_key!r}"
            )
        if self.timestamp < 0:
            raise ValueError("timestamp must not be negative")

    def as_payload(self) -> dict[str, Any]:
        """Return the JSON payload for this record.

        ``slot_key`` is omitted rather than written as ``None`` when there
        is no slot. Qdrant's ``IsNull`` matching is a different construct
        from ``MatchValue``, and an absent key is the simpler thing for
        every filter in the store to reason about.
        """
        payload: dict[str, Any] = {
            FIELD_TEXT: self.text,
            FIELD_SOURCE: self.source,
            FIELD_TIMESTAMP: self.timestamp,
            FIELD_CATEGORY: self.category,
        }
        if self.slot_key is not None:
            payload[FIELD_SLOT_KEY] = self.slot_key
        return payload

    @classmethod
    def from_payload(cls, payload: dict[str, Any] | None) -> "MemoryRecord | None":
        """Rebuild a record from a stored payload, or ``None`` if unusable.

        Total by design. The collection sits on a volume that outlives every
        container and may hold points written by an older version of this
        code, or by a human with the Qdrant dashboard open. A malformed row
        is skipped, not raised on: one bad point must not make memory
        unreadable for every later session.

        Args:
            payload: What Qdrant returned, possibly ``None``.

        Returns:
            The record, or ``None`` when a required field is missing or has
            the wrong type.
        """
        if not payload:
            return None
        text = payload.get(FIELD_TEXT)
        source = payload.get(FIELD_SOURCE)
        category = payload.get(FIELD_CATEGORY)
        if not isinstance(text, str) or not text.strip():
            return None
        if source not in SOURCES or category not in CATEGORIES:
            return None
        raw_stamp = payload.get(FIELD_TIMESTAMP, 0)
        stamp = raw_stamp if isinstance(raw_stamp, int) and raw_stamp >= 0 else 0
        slot = payload.get(FIELD_SLOT_KEY)
        return cls(
            text=text,
            source=source,
            category=category,
            timestamp=stamp,
            slot_key=slot if slot in SLOT_KEYS else None,
        )


def clean_text(text: str) -> str:
    """Flatten a fact to one line and strip any forged provenance tag.

    Two jobs, done together because they are always wanted together:

    1. Whitespace is collapsed, so one fact stays one line. The injected
       block is a list, and a multi-line entry makes the boundary between
       two facts a matter of guesswork for the model reading it.
    2. A leading ``[user]``, ``[agent]``, ``[harness]``, ``[human]`` or
       ``[system]`` tag is removed, repeatedly, until none is left. Stacked
       tags (``[agent] [user] ...``) are how a single pass would be evaded.

    This is the safeguard that survives the move to vectors intact, and it
    now runs *before* the text is embedded as well as before it is stored,
    so a forged tag cannot influence where the point lands either.

    Args:
        text: Raw text from a tool argument or a REPL command.

    Returns:
        The cleaned single-line text, possibly empty.
    """
    stripped = " ".join(text.split())
    while True:
        updated = _PROVENANCE_PREFIX.sub("", stripped, count=1).strip()
        if updated == stripped:
            return stripped
        stripped = updated


def parse_slot(text: str) -> tuple[str, str] | None:
    """Return ``(Key, value)`` when ``text`` is a slotted fact, else ``None``.

    Args:
        text: One fact. Cleaned here, so a caller may pass raw text.

    Returns:
        The canonical title-cased key and its value, or ``None`` for
        unkeyed prose. The value may be empty; the store rejects that.
    """
    match = _SLOT_BODY.fullmatch(clean_text(text))
    if match is None:
        return None
    return match.group(1).title(), match.group(2).strip()


def canonical_slot(key: str) -> str | None:
    """Return the canonical spelling of ``key``, or ``None`` if unknown.

    Args:
        key: A slot name in any case, as a tool argument supplies it.

    Returns:
        The matching entry of :data:`SLOT_KEYS`, or ``None``.
    """
    for known in SLOT_KEYS:
        if known.lower() == key.strip().lower():
            return known
    return None
