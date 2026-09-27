"""Memory tools: store a fact, replace a slot, search.

Three tools where there were four (ADR-070). The file surface was
``memory_read`` / ``memory_write`` / ``memory_append`` / ``memory_edit`` —
four verbs that all existed because memory was a filesystem and a
filesystem needs them. Records in a collection need a different three:

``memory_store``
    Write a new fact. Replaces both ``memory_append`` and ``memory_write``,
    which differed only in which file they targeted; the target is now a
    ``category`` argument. Goes through
    :class:`~harness.memory.consolidate.MemoryWriter`, so a repeat is not
    stored twice and a correction supersedes what it corrects (ADR-107).
``memory_upsert_slot``
    Replace the value in one of four known slots. The overwrite mechanic
    ADR-063 introduced, preserved exactly, including its refusal to
    overwrite anything the user recorded.
``memory_search``
    Pull facts by meaning. This is the tool with no file-era equivalent and
    the reason for the migration: ``memory_read`` required the model to
    already know the path, so a fact it could not name was a fact it could
    not reach.

Gone with the files: ``memory_edit``. There is no substring to replace in a
record — a corrected fact is a new fact, and for the four slotted keys it is
an upsert. ``memory_read`` is gone too: its job, "load the note you saw in
the index", is what ``memory_search`` does without needing an index.

Still absent, and still for ADR-008: ``delete`` and ``rename``. The store
deletes a point when a slot is replaced, but no tool reaches that path with
an arbitrary id, so the tool surface offers the agent no way to remove
anything.
"""

from typing import Any

from harness.memory.consolidate import MemoryWriter
from harness.memory.protocol import MemoryStore, MemoryUnavailableError
from harness.memory.records import CATEGORIES, SLOT_KEYS
from harness.tools.base import DEFAULT_MAX_OUTPUT_CHARS, Tool, ToolResult

# Default hits for one search. Small: each hit costs context, and a model
# that needs more can ask for more.
DEFAULT_SEARCH_LIMIT = 5

# Ceiling advertised in the schema. The store clamps independently, so a
# model that ignores the schema still cannot empty the window.
SCHEMA_SEARCH_LIMIT = 20

_TEXT_PROP = {
    "type": "string",
    "description": (
        "One durable fact or note, in a single sentence. Do not include a "
        "[user] or [agent] tag; the harness records who wrote it."
    ),
}

_CATEGORY_PROP = {
    "type": "string",
    "enum": ["core_fact", "project_note"],
    "description": (
        "core_fact for a short durable fact about the user or their setup — "
        "these are loaded into every later session. project_note for longer "
        "working notes, which are found by memory_search when relevant."
    ),
}

_SLOT_PROP = {
    "type": "string",
    "enum": list(SLOT_KEYS),
    "description": (
        "Which slot to replace. Each slot holds exactly one value: storing "
        "a new one discards the old."
    ),
}

_SLOT_TEXT_PROP = {
    "type": "string",
    "description": "The new value for the slot, e.g. 'Saad' for Name.",
}

_QUERY_PROP = {
    "type": "string",
    "description": (
        "What you are trying to remember, in natural language. Matched by "
        "meaning, not by exact words, so describe the topic rather than "
        "guessing the wording used when it was stored."
    ),
}

_FILTER_PROP = {
    "type": "string",
    "enum": list(CATEGORIES),
    "description": "Restrict the search to one kind of memory. Omit to search all.",
}

_LIMIT_PROP = {
    "type": "integer",
    "minimum": 1,
    "maximum": SCHEMA_SEARCH_LIMIT,
    "description": f"How many results to return (default {DEFAULT_SEARCH_LIMIT}).",
}


class MemoryToolBase(Tool):
    """Shared plumbing for the vector-backed memory tools.

    The base class still takes a confinement root, because every tool has
    one and the permission policy reads it. Memory is no longer a directory,
    so the root passed is the memory volume that holds the session log — the
    only path this subsystem still touches. Nothing in these tools joins a
    path onto it; they cannot reach the filesystem at all, which is a
    stronger containment guarantee than the file store had rather than a
    weaker one (ADR-069).
    """

    def __init__(
        self,
        store: MemoryStore,
        max_output_chars: int = DEFAULT_MAX_OUTPUT_CHARS,
    ) -> None:
        """Args:
        store: Vector-backed memory.
        max_output_chars: Per-observation character cap.
        """
        super().__init__(store.root, max_output_chars)
        self._store = store


class MemoryStoreTool(MemoryToolBase):
    """Record one durable fact or note, consolidated against what is stored."""

    name = "memory_store"
    description = (
        "Remember one fact for future sessions. Use core_fact for short "
        "durable facts about the user, which load automatically next time; "
        "project_note for longer notes, found later with memory_search. "
        "Never store secrets, tokens or passwords."
    )
    parameters: dict[str, Any] = {
        "type": "object",
        "properties": {"text": _TEXT_PROP, "category": _CATEGORY_PROP},
        "required": ["text", "category"],
    }
    risk = "mutate"

    def __init__(
        self,
        store: MemoryStore,
        max_output_chars: int = DEFAULT_MAX_OUTPUT_CHARS,
        writer: MemoryWriter | None = None,
    ) -> None:
        """Args:
        store: Vector-backed memory.
        max_output_chars: Per-observation character cap.
        writer: The consolidating write path. ``None`` builds one with no
            judge, which still refuses to store an exact repeat.
        """
        super().__init__(store, max_output_chars)
        self._writer = writer if writer is not None else MemoryWriter(store)

    def _run(self, arguments: dict[str, Any]) -> ToolResult:
        """Store the fact with agent provenance, and say what the write did.

        The confirmation is one line, content included, because the CLI
        trace shows a tool's *first* line and nothing else: a two-line
        observation reads as "Remembered as core_fact:" with the fact
        itself invisible to whoever is watching the run.

        A refusal is a failed observation that names the fact in the way
        and the one way to change it. The model is not told to retry: the
        refusal is a rule, not a transient error (ADR-107).
        """
        text = self._string_argument(arguments, "text")
        category = self._string_argument(arguments, "category")
        if category not in CATEGORIES:
            return ToolResult(
                f"category must be 'core_fact' or 'project_note', got {category!r}.",
                ok=False,
            )
        try:
            outcome = self._writer.write(
                text=text,
                category=category,  # type: ignore[arg-type]
                source="agent",
            )
        except MemoryUnavailableError as exc:
            return ToolResult(str(exc), ok=False)
        record = outcome.record
        if outcome.action == "unchanged":
            return ToolResult(f"Already remembered: {record.text}")
        if outcome.action == "refused":
            return ToolResult(
                f"Not stored: this would replace a fact the user recorded "
                f"themselves ({record.text!r}), and only the user can change "
                f"that. Tell them, and suggest /remember if it is out of date.",
                ok=False,
            )
        if outcome.action == "updated" and outcome.replaced is not None:
            return ToolResult(
                f"Updated {record.category}: {record.text} "
                f"(replaces: {outcome.replaced.text})"
            )
        return ToolResult(f"Remembered as {record.category}: {record.text}")


class MemoryUpsertSlot(MemoryToolBase):
    """Replace the single value held in one known slot."""

    name = "memory_upsert_slot"
    description = (
        "Set one of the fixed memory slots (" + ", ".join(SLOT_KEYS) + "), "
        "replacing whatever it held. Use this instead of memory_store when "
        "the fact can only have one value, so it does not accumulate stale "
        "copies. A slot the user set themselves cannot be overwritten."
    )
    parameters: dict[str, Any] = {
        "type": "object",
        "properties": {"slot_key": _SLOT_PROP, "text": _SLOT_TEXT_PROP},
        "required": ["slot_key", "text"],
    }
    risk = "mutate"

    def _run(self, arguments: dict[str, Any]) -> ToolResult:
        """Replace the slot, refusing a user-owned one.

        One line, like ``memory_store``: the CLI trace shows a tool's first
        line only. ``record.text`` already begins with the key, so the
        confirmation reads "Set Name: Saad" rather than repeating it.
        """
        try:
            record = self._store.upsert_slot(
                slot_key=self._string_argument(arguments, "slot_key"),
                text=self._string_argument(arguments, "text"),
                source="agent",
            )
        except MemoryUnavailableError as exc:
            return ToolResult(str(exc), ok=False)
        return ToolResult(f"Set {record.text}")


class MemorySearch(MemoryToolBase):
    """Find stored memory by meaning."""

    name = "memory_search"
    description = (
        "Search long-term memory by meaning. The Memory section of your "
        "instructions already holds the facts most relevant to the current "
        "request; use this when you need something it did not include, such "
        "as an older note or a detail from a past session."
    )
    parameters: dict[str, Any] = {
        "type": "object",
        "properties": {
            "query": _QUERY_PROP,
            "category_filter": _FILTER_PROP,
            "limit": _LIMIT_PROP,
        },
        "required": ["query"],
    }
    risk = "observe"

    def _run(self, arguments: dict[str, Any]) -> ToolResult:
        """Search and format the hits, best first."""
        query = self._string_argument(arguments, "query")
        category = arguments.get("category_filter")
        if category is not None and category not in CATEGORIES:
            return ToolResult(
                f"category_filter must be one of {', '.join(CATEGORIES)}, "
                f"got {category!r}.",
                ok=False,
            )
        limit = _positive_int(arguments.get("limit"), DEFAULT_SEARCH_LIMIT)
        hits = self._store.search(query, limit=limit, category=category)
        if not hits:
            scope = f" in {category}" if category else ""
            # "Nothing matched" and "the store is unreachable" are the same
            # empty list on the read path, which degrades rather than raises
            # (ADR-069). Saying the first when the second is true tells the
            # model that a fact it stored last week does not exist, and it
            # then confidently answers as though it does not — so the two
            # get different sentences, and the second says it is a fault
            # rather than an absence (ADR-105).
            if not self._store.available:
                return ToolResult(
                    f"Long-term memory is unavailable, so this search could "
                    f"not run: {self._store.describe}. This is not an answer "
                    f"about whether memory holds {query!r} — it may. Carry "
                    f"on without it, and say so if it mattered.",
                    ok=False,
                )
            return ToolResult(
                f"Nothing in memory{scope} matches {query!r}. "
                f"Memory may simply not hold this yet."
            )
        lines = [f"{len(hits)} result(s) for {query!r}, most relevant first:"]
        for hit in hits:
            record = hit.record
            lines.append(
                f"- [{record.source}] ({record.category}, score "
                f"{hit.score:.2f}) {record.text}"
            )
        return ToolResult("\n".join(lines))


def memory_tools(
    store: MemoryStore,
    max_output_chars: int = DEFAULT_MAX_OUTPUT_CHARS,
    writer: MemoryWriter | None = None,
) -> list[Tool]:
    """Return the memory tools, observe tools first.

    Args:
        store: Vector-backed memory.
        max_output_chars: Per-observation character cap.
        writer: The consolidating write path for ``memory_store``, or
            ``None`` for one with no judge.

    Returns:
        Freshly constructed tools, ready to ``register``.
    """
    return [
        MemorySearch(store, max_output_chars),
        MemoryStoreTool(store, max_output_chars, writer),
        MemoryUpsertSlot(store, max_output_chars),
    ]


def _positive_int(value: Any, default: int) -> int:
    """Coerce a model-supplied limit, falling back rather than failing.

    Models pass ``"5"`` as often as ``5``. Refusing the call over that would
    spend a whole turn on a difference the tool can absorb, and the store
    clamps the result anyway.
    """
    try:
        number = int(value)
    except (TypeError, ValueError):
        return default
    return number if number > 0 else default
