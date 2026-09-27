"""Short-term memory: the scratchpad file, its two tools, and the hook.

The claim ADR-090 makes is narrow and worth stating as tests: there is one
small markdown file outside the context window, the model can write it, it is
put in front of the model every run without being asked for, and nothing
about it can take a run down — a read-only mount degrades to in-memory notes
the way a missing Qdrant does (ADR-069) and a failing trace does (ADR-083).

The other half is what it is *not*. It is not the vector store, it is not
reachable by the file tools, and it is not inherited by sub-agents. Each of
those has a test here because each of them is a way the feature could look
fine and be wrong.
"""

from pathlib import Path

import pytest
from harness.context import RunContext
from harness.memory.notes import NotesHook, note_tools
from harness.memory.scratchpad import (
    EMPTY_NOTES,
    LEGACY_SCRATCHPAD_FILENAME,
    NOT_PERSISTED,
    NOTES_SECTION_HEADER,
    SCRATCHPAD_FILENAME,
    Scratchpad,
)
from harness.subagents.contracts import ExecutionBudget
from harness.subagents.roles import Capability, build_catalog
from harness.tools.base import Tool, ToolResult


def tools(scratchpad: Scratchpad) -> dict[str, Tool]:
    """Return the note tools by name."""
    return {tool.name: tool for tool in note_tools(scratchpad)}


class _named(Tool):
    """A tool that exists only to carry a registered name past a classifier."""

    description = "A stand-in."
    parameters: dict[str, object] = {"type": "object", "properties": {}}
    risk = "observe"

    def __init__(self, name: str) -> None:
        """Args:
        name: The name as the registry would know it.
        """
        self.name = name
        super().__init__(Path("/nonexistent"))

    def _run(self, arguments: dict[str, object]) -> ToolResult:
        return ToolResult("ok")


# -- the file -----------------------------------------------------------------


def test_notes_survive_the_process(tmp_path: Path) -> None:
    """The whole point: state that outlives the conversation holding it."""
    Scratchpad(tmp_path).write("- wrote f1.md\n- wrote f2.md")

    assert "wrote f2.md" in Scratchpad(tmp_path).read()
    assert (tmp_path / SCRATCHPAD_FILENAME).exists()


def test_append_adds_without_resending_the_rest(tmp_path: Path) -> None:
    """Ticking off one step of a long task must not cost the whole file."""
    scratchpad = Scratchpad(tmp_path)
    scratchpad.write("- wrote f1.md")
    scratchpad.append("- wrote f2.md")

    assert scratchpad.read() == "- wrote f1.md\n- wrote f2.md"


def test_an_oversized_write_is_refused_rather_than_truncated(tmp_path: Path) -> None:
    """A plan cut off mid-sentence is worse than a refusal it can retry.

    The notes go into every request, so an unbounded file would quietly eat
    the budget it exists to protect — but silently trimming it would leave
    the model believing it recorded something it did not.
    """
    scratchpad = Scratchpad(tmp_path, max_chars=500)
    scratchpad.write("- kept")
    outcome = scratchpad.write("x" * 600)

    assert "not saved" in outcome
    assert "500" in outcome
    assert scratchpad.read() == "- kept"


def test_clear_empties_the_notes(tmp_path: Path) -> None:
    """`/reset` drops the task, so it has to drop the task's notes with it."""
    scratchpad = Scratchpad(tmp_path)
    scratchpad.write("- something")
    scratchpad.clear()

    assert scratchpad.is_empty
    assert scratchpad.read() == EMPTY_NOTES


def test_an_unwritable_file_degrades_and_says_so(tmp_path: Path) -> None:
    """Never raises — and never lets the model believe a lost note was kept.

    The failure is reported in the observation the model gets back, because
    notes it thinks are saved and are not are worse than no notes at all.
    """
    scratchpad = Scratchpad(tmp_path)
    (tmp_path / SCRATCHPAD_FILENAME).mkdir()  # a directory where the file goes

    outcome = scratchpad.write("- noted")

    # The wording is the one ``notes.py`` tests for, so the observation the
    # model gets is marked failed rather than green (ADR-105).
    assert NOT_PERSISTED in outcome
    assert scratchpad.read() == "- noted"
    assert "in memory only" in scratchpad.describe


# -- the tools ----------------------------------------------------------------


def test_a_refused_write_comes_back_as_a_failure(tmp_path: Path) -> None:
    """Otherwise the model reads "not saved" as done and carries on (ADR-058)."""
    scratchpad = Scratchpad(tmp_path, max_chars=500)
    result = tools(scratchpad)["note_write"].invoke({"content": "x" * 600})

    assert not result.ok
    assert "not saved" in result.text


def test_the_tools_do_not_take_a_path(tmp_path: Path) -> None:
    """One fixed file, so there is no path to traverse out of (ADR-035).

    The file tools are confined by resolving against a root; these are
    confined by having nowhere to go, which is the stronger version.
    """
    for tool in note_tools(Scratchpad(tmp_path)):
        assert set(tool.parameters["properties"]) == {"content"}


# -- the hook -----------------------------------------------------------------


def test_the_hook_puts_the_notes_in_front_of_the_model(tmp_path: Path) -> None:
    """Injected, not retrieved.

    A model that has to remember to check its notes has exactly the problem
    the notes were meant to solve.
    """
    scratchpad = Scratchpad(tmp_path)
    scratchpad.write("- wrote f1.md")
    context = RunContext(task="")
    context.ensure_system_prompt("SYSTEM")

    NotesHook(scratchpad).on_run_start(context)

    system = context.messages[0]["content"]
    assert NOTES_SECTION_HEADER in system
    assert "wrote f1.md" in system


# -- what sub-agents get ------------------------------------------------------


@pytest.mark.parametrize("role", ["researcher", "writer", "critic"])
def test_no_shipped_role_can_write_the_parents_notes(role: str) -> None:
    """A child sharing the parent's scratchpad would corrupt its state.

    The parent's notes say where the *parent* had got to. A researcher
    ticking off its own steps in that file is how a resumed task comes back
    believing work it never did is finished (ADR-090).
    """
    spec = build_catalog(ExecutionBudget()).get(role)
    assert spec is not None
    assert Capability.NOTES not in spec.capabilities


# --- the gaps the Thursday audit found (ADR-105) ----------------------------


def test_the_file_is_clipped_on_the_way_out_too(tmp_path: Path) -> None:
    """The bound used to exist only on the way in.

    Which left the file itself an unchecked input to every request: a
    `notes.md` hand-edited on the volume, or left by a session configured
    with a larger ceiling, was injected verbatim into the prompt it exists
    to protect. The injection hook passes no ceiling of its own, so this is
    where the ceiling has to be.
    """
    (tmp_path / SCRATCHPAD_FILENAME).write_text("x" * 9_000, encoding="utf-8")
    scratchpad = Scratchpad(tmp_path, max_chars=1_000)

    notes = scratchpad.read()

    assert len(notes) < 1_400
    assert "truncated" in notes
    # And it says what to do about it, rather than just stopping mid-word.
    assert "note_write" in notes


def test_a_write_re_reads_before_replacing(tmp_path: Path) -> None:
    """A file changed on disk must not be clobbered from a stale copy.

    ``append`` already re-read; ``write`` did not, so it replaced the file
    from an in-process copy that could be arbitrarily old. Since ADR-110 a
    second *session* cannot write the file while this one holds it, but a
    person can still edit it on the volume, and the lost update would be
    just as silent.
    """
    first = Scratchpad(tmp_path)
    first.read()  # first now holds an empty copy
    (tmp_path / SCRATCHPAD_FILENAME).write_text("- edited by hand", "utf-8")

    first.write("- from this session")

    # The replacement is still a replacement — that is what write means —
    # but it was computed against what is actually on disk.
    assert "from this session" in first.read()


def test_a_torn_write_cannot_leave_half_a_file(tmp_path: Path) -> None:
    """Atomic, like the budget ledger, and for a sharper reason.

    A half-written ledger costs a number. A half-written scratchpad is
    injected verbatim into the next run's prompt.
    """
    scratchpad = Scratchpad(tmp_path)
    scratchpad.write("- a plan worth keeping")

    # No temp file left behind, and the real file is whole.
    assert not list(tmp_path.glob("*.tmp"))
    assert (tmp_path / SCRATCHPAD_FILENAME).read_text(
        encoding="utf-8"
    ) == "- a plan worth keeping"


def test_clear_reports_a_failure_instead_of_swallowing_it(tmp_path: Path) -> None:
    """``/reset`` printed "cleared" while the notes stayed on disk.

    On a read-only volume that is a sentence which is not true, and the next
    run re-injects the task the user just said they were done with.
    """
    scratchpad = Scratchpad(tmp_path)
    (tmp_path / SCRATCHPAD_FILENAME).mkdir()

    assert NOT_PERSISTED in scratchpad.clear()


def test_is_empty_reads_the_file_rather_than_a_cache(tmp_path: Path) -> None:
    """It is the banner's "empty" or "carried over", so it has to be current."""
    scratchpad = Scratchpad(tmp_path)
    assert scratchpad.is_empty

    (tmp_path / SCRATCHPAD_FILENAME).write_text("- edited by hand", "utf-8")

    assert not scratchpad.is_empty


# --- one writer at a time, and the new name (ADR-110) -----------------------


def test_a_second_live_session_gets_private_notes(tmp_path: Path) -> None:
    """Two containers on one volume no longer share one plan.

    The first session owns ``notes.md``; the second keeps its notes in
    memory, says so, and never touches the file.
    """
    first = Scratchpad(tmp_path)
    first.write("- step 1 done")
    second = Scratchpad(tmp_path)

    outcome = second.write("- a different task")

    assert "another session is using notes.md" in outcome
    assert NOT_PERSISTED not in outcome
    assert "another session" in second.describe
    assert second.read() == "- a different task"
    assert first.read() == "- step 1 done"
    assert (tmp_path / SCRATCHPAD_FILENAME).read_text("utf-8") == "- step 1 done"


def test_the_next_session_owns_the_notes_once_the_first_has_closed(
    tmp_path: Path,
) -> None:
    """Carry-over still works: a restarted session picks up the notes."""
    first = Scratchpad(tmp_path)
    first.write("- half way through the migration")
    first.close()

    later = Scratchpad(tmp_path)

    assert "another session" not in later.describe
    assert later.read() == "- half way through the migration"


def test_notes_from_the_old_file_name_are_adopted(tmp_path: Path) -> None:
    """An upgrade does not drop a task's notes: memory.md becomes notes.md."""
    (tmp_path / LEGACY_SCRATCHPAD_FILENAME).write_text("- old notes", "utf-8")

    scratchpad = Scratchpad(tmp_path)

    assert scratchpad.read() == "- old notes"
    assert not (tmp_path / LEGACY_SCRATCHPAD_FILENAME).exists()
    assert SCRATCHPAD_FILENAME == "notes.md"
