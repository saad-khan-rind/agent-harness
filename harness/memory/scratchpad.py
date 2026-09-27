"""Short-term memory: one markdown file the agent keeps its own state in.

The harness has had two kinds of memory and a hole between them. Long-term
memory is a vector collection queried by similarity (ADR-065) — the right
shape for "what did the user tell me weeks ago", the wrong shape for "which
of these hundred files have I already written". The conversation is the
other kind, and compaction is exactly the thing that eats it: the older an
observation, the more likely it has been stubbed by the time it matters
(ADR-032).

So a long task loses its own working state, silently, and the model's only
recovery is to redo work to find out whether it already did it.

**The fix is the one every current harness converged on: a file.** Claude
Code keeps `CLAUDE.md`; other harnesses call it a scratchpad, a notepad or a
task file. The shape is the same and the reason is the same — a file is
outside the context window, so compaction cannot reach it; it is written in
the model's own words, so nothing has to be inferred back out of it; and it
is one small blob that can be injected whole at the start of every turn
rather than retrieved by a similarity score that might miss.

This is deliberately **not** the vector store, and the split is worth
stating because the two are easy to confuse:

* long-term memory answers *"what do I know about this user"*, is queried,
  ranked, and may return nothing;
* the scratchpad answers *"where had I got to"*, is never queried, and is
  always present in full.

Three rules keep it honest:

* **Bounded.** It is injected into every request, so an unbounded file would
  quietly eat the context budget it exists to protect. Writes past
  ``max_chars`` are refused with a message saying so, rather than silently
  truncated — a plan cut off mid-sentence is worse than a refused write the
  model can retry shorter.
* **Never raises.** A read-only mount degrades to in-memory notes for the
  session plus a banner note, the same call ADR-069 makes for a missing
  Qdrant and ADR-083 for traces.
* **Outside the workspace.** It lives under the memory root, so the file
  tools cannot reach it (ADR-035) and a task that rewrites the workspace
  cannot corrupt the agent's own notes.
"""

import os
from pathlib import Path
from typing import IO

# ``notes.md``, not ``memory.md``: the old name put the agent's short-term
# working notes next to long-term *memory* under a name that said memory,
# and the two are different subsystems (ADR-110).
SCRATCHPAD_FILENAME = "notes.md"

# The previous name. A volume that still has it is migrated on first use,
# so an upgrade does not silently drop a task's notes.
LEGACY_SCRATCHPAD_FILENAME = "memory.md"

# Held with an exclusive OS lock for as long as a session owns the notes.
# A separate file because writes replace ``notes.md`` with ``os.replace``,
# and a lock on a file that is replaced is a lock on a file nobody reads.
LOCK_FILENAME = "notes.lock"

# The section the notes are injected under, and the header the hook replaces
# on every turn. Spelled here so the hook and its tests cannot disagree.
NOTES_SECTION_HEADER = "## Working notes"

# What a fresh scratchpad says. Not empty: an empty section reads as "there
# is nothing to say", while this says "this is yours, here is what it is
# for", which is the difference between a model using it and ignoring it.
EMPTY_NOTES = (
    "(empty — nothing recorded yet for this task)\n"
    "\n"
    "Use note_write to keep the state of a long task here: what you have "
    "finished, what is left, and anything you would otherwise have to redo "
    "a tool call to find out. These notes survive when older tool results "
    "are compacted out of the conversation."
)

DEFAULT_MAX_CHARS = 4_000

# What a failed write says, spelled once. ``notes.py`` decides whether a tool
# call succeeded by looking for it, and the two used to disagree: the tool
# tested for "not saved" while a failed persist said "only in memory", so a
# read-only volume produced a green observation on every note write and the
# model went on believing its plan was safe (ADR-105).
NOT_PERSISTED = "notes NOT saved to disk"


class Scratchpad:
    """The agent's own notes, kept in one markdown file under the memory root.

    Construct with ``root=None`` for notes that live only in memory — what a
    session with memory disabled gets, and what unit tests use.
    """

    def __init__(self, root: Path | None, max_chars: int = DEFAULT_MAX_CHARS) -> None:
        """Args:
        root: Memory root, or ``None`` to keep notes in memory only.
        max_chars: Ceiling on the file. Injected verbatim into every
            request, so this is a share of the context budget, not a disk
            limit.

        One session writes the notes file at a time, because two containers
        sharing it would each inject the other's plan under a heading that
        says "your notes for this task". The first session takes an
        exclusive lock on ``notes.lock``; a second one that
        starts while it is held keeps private notes in memory and says so in
        the banner. The lock is the operating system's, so it is released
        when the process ends — a crash cannot leave the notes locked, and a
        restarted session still picks up where the last one stopped
        (ADR-110).
        """
        self._path = root.resolve() / SCRATCHPAD_FILENAME if root is not None else None
        self._max_chars = max_chars
        self._notes = ""
        self._error: str | None = None
        self._lock: IO[str] | None = None
        # True when another live session holds the lock: this one's notes
        # are its own and never touch the file.
        self._private = False
        if self._path is not None:
            if self._take_lock(self._path.parent):
                _adopt_legacy_notes(self._path)
            else:
                self._private = True
        self._load()

    def close(self) -> None:
        """Release the lock so another session can own the notes.

        The process ending releases it too; this is for a caller that keeps
        running, such as a test that opens two sessions in turn.
        """
        lock = self._lock
        self._lock = None
        if lock is not None:
            try:
                lock.close()
            except OSError:
                return

    def _take_lock(self, root: Path) -> bool:
        """Try to become the one session that writes the notes file.

        Returns:
            ``False`` only when another session holds the lock. Every other
            outcome — no ``fcntl`` on this platform, a volume where the lock
            file cannot be created — returns ``True`` and falls back to the
            shared-file behaviour, whose failures ``_persist`` already
            reports. A lock that cannot be taken must not cost the notes.
        """
        try:
            import fcntl
        except ImportError:  # pragma: no cover - Windows has no fcntl
            return True
        try:
            root.mkdir(parents=True, exist_ok=True)
            handle = (root / LOCK_FILENAME).open("a", encoding="utf-8")
        except OSError:
            return True
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            handle.close()
            return False
        self._lock = handle
        return True

    # -- what the hook and banner read ---------------------------------------

    @property
    def path(self) -> Path | None:
        """Return the notes file, or ``None`` when kept in memory only."""
        return self._path

    @property
    def max_chars(self) -> int:
        """Return the ceiling a write is refused above."""
        return self._max_chars

    @property
    def describe(self) -> str:
        """Return one banner phrase saying where the notes live."""
        if self._path is None:
            return "in memory only (no directory configured)"
        if self._private:
            return f"in memory only (another session is using {SCRATCHPAD_FILENAME})"
        if self._error is not None:
            return f"in memory only ({self._error})"
        return str(self._path)

    def read(self) -> str:
        """Return the notes, or the empty-state text when there are none.

        Re-read from disk each time so a second session's notes on the same
        volume are seen, which is the same read-modify-write the budget
        ledger uses and carries the same caveat: approximately right, not
        transactional.

        **Clipped to the same ceiling the writes are.** The bound used to be
        enforced only on the way in, which left the file itself as an
        unchecked input to every request: a `notes.md` hand-edited on the
        volume, or left by a session configured with a larger
        ``notes_max_chars``, was injected verbatim into the prompt it was
        supposed to be protecting. The injection hook passes no ceiling of
        its own, so this is where the ceiling has to be (ADR-105).
        """
        self._load()
        if not self._notes:
            return EMPTY_NOTES
        if len(self._notes) <= self._max_chars:
            return self._notes
        kept = self._notes[: self._max_chars]
        return (
            f"{kept}\n\n[notes truncated: the file on disk is "
            f"{len(self._notes):,} characters, over the {self._max_chars:,} "
            f"limit. Use note_write to replace it with a shorter version.]"
        )

    @property
    def is_empty(self) -> bool:
        """Whether anything has actually been written.

        Re-reads, because the banner prints "empty" or "carried over" from
        this and a cached answer would describe the file as it was when the
        session started rather than as it is.
        """
        self._load()
        return not self._notes.strip()

    # -- what the tools write ------------------------------------------------

    def write(self, content: str) -> str:
        """Replace the notes wholesale.

        Args:
            content: The new notes, in the model's own words.

        Returns:
            A confirmation, or the reason the write was refused.
        """
        text = content.strip()
        # Re-read first, for the reason ``append`` does: the in-process copy
        # may predate another session's write, and replacing the file from a
        # stale copy is a silent lost update (ADR-105).
        self._load()
        if len(text) > self._max_chars:
            return (
                f"notes not saved: {len(text):,} characters is over the "
                f"{self._max_chars:,} limit. These notes are sent with every "
                f"request, so keep them to the state you would otherwise have "
                f"to redo work to recover. Write a shorter version."
            )
        self._notes = text
        return self._persist(f"notes saved ({len(text):,} characters)")

    def append(self, content: str) -> str:
        """Add a line to the notes without resending the rest.

        Args:
            content: The line to add.

        Returns:
            A confirmation, or the reason the write was refused.
        """
        addition = content.strip()
        if not addition:
            return "notes unchanged: nothing to add"
        self._load()
        merged = f"{self._notes}\n{addition}".strip() if self._notes else addition
        if len(merged) > self._max_chars:
            return (
                f"note not added: the notes would reach {len(merged):,} "
                f"characters, over the {self._max_chars:,} limit. Use "
                f"note_write to replace them with a shorter summary first."
            )
        self._notes = merged
        return self._persist(f"note added ({len(merged):,} characters total)")

    def clear(self) -> str:
        """Empty the notes. Used by ``/reset``, which drops the task too.

        Returns:
            A confirmation, or the reason the file could not be emptied.
            Returned rather than discarded: on a read-only mount ``/reset``
            used to print "cleared" while the previous task's notes stayed
            on disk, to be injected again on the next run (ADR-105).
        """
        self._notes = ""
        return self._persist("cleared")

    # -- internals -----------------------------------------------------------

    def _load(self) -> None:
        """Read the file into memory. Never raises."""
        if self._path is None or self._error is not None or self._private:
            return
        try:
            self._notes = self._path.read_text(encoding="utf-8").strip()
        except FileNotFoundError:
            # A fresh volume, which is not an error and not worth a note.
            self._notes = ""
        except OSError as exc:
            self._error = f"cannot read {self._path.name}: {exc}"

    def _persist(self, confirmation: str) -> str:
        """Write the notes out. Never raises; says so when it could not.

        Args:
            confirmation: What to report when the write lands.

        Returns:
            The confirmation, or it plus the reason persistence failed —
            the model is told, because notes it believes are saved and are
            not are worse than no notes.
        """
        if self._path is None:
            return f"{confirmation}, in memory only"
        if self._private:
            # Saved as far as this session is concerned, which is all a
            # private copy promises; not a failure, so not NOT_PERSISTED.
            return (
                f"{confirmation}, in memory only: another session is using "
                f"{SCRATCHPAD_FILENAME}"
            )
        # Sibling temp file plus ``os.replace``, the same shape the budget
        # ledger uses and for the same reason: a plain ``write_text``
        # interrupted by a crash or a full disk leaves a half-written file,
        # and this one is injected verbatim into the next run's prompt
        # (ADR-088, ADR-105).
        temp = self._path.with_suffix(".md.tmp")
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            temp.write_text(self._notes, encoding="utf-8")
            os.replace(temp, self._path)
            self._error = None
            return confirmation
        except OSError as exc:
            self._error = f"cannot write {self._path.name}: {exc}"
            return f"{NOT_PERSISTED} ({exc}); {confirmation} in memory only"


def _adopt_legacy_notes(path: Path) -> None:
    """Rename a ``memory.md`` left by an older version to ``notes.md``.

    Only when there is no ``notes.md`` yet, and only by the session holding
    the lock, so two sessions cannot both try. Never raises: a volume that
    will not allow the rename keeps the old file, and the notes simply start
    empty under the new name.

    Args:
        path: Where the notes file now lives.
    """
    legacy = path.with_name(LEGACY_SCRATCHPAD_FILENAME)
    if path.exists() or not legacy.is_file():
        return
    try:
        os.replace(legacy, path)
    except OSError:
        return
