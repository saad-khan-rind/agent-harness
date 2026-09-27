"""The two tools and the hook that put the scratchpad in front of the model.

Kept apart from ``harness/memory/tools.py`` because these do not touch the
vector store at all — they are the short-term half of memory (ADR-090), and
sharing a module with the long-term tools is how the two would start sharing
assumptions.

The injection is the important half. The notes are pushed into the system
prompt at the start of every run through the same
:meth:`~harness.context.RunContext.set_system_section` seam memory and skills
use, so the model never has to *ask* for its own state — which matters,
because a model that has to remember to check its notes has the problem the
notes were supposed to solve.
"""

from pathlib import Path
from typing import Any

from harness.context import RunContext
from harness.memory.scratchpad import (
    NOT_PERSISTED,
    NOTES_SECTION_HEADER,
    Scratchpad,
)
from harness.tools.base import DEFAULT_MAX_OUTPUT_CHARS, Tool, ToolOutcome, ToolResult


class NotesHook:
    """Put the current notes into the system prompt before every run.

    Implements :class:`~harness.hooks.Hook` structurally. Injection happens
    in ``on_run_start`` rather than once per session because the notes change
    *during* a run — a resumed task has notes its first turn did not — and a
    section written once would go stale exactly when it is being relied on.
    """

    def __init__(self, scratchpad: Scratchpad) -> None:
        """Args:
        scratchpad: The notes to inject.
        """
        self._scratchpad = scratchpad
        # What is currently in the system prompt, so an unchanged file is
        # not rewritten on every request.
        self._injected: str | None = None

    def on_run_start(self, context: RunContext) -> None:
        """Replace the working-notes section with what is on disk now."""
        self._inject(context)

    def _inject(self, context: RunContext) -> None:
        """Put the current notes in the system prompt, if they changed.

        Compared before writing so an unchanged file does not churn the
        system message on every iteration of a long run.
        """
        current = self._scratchpad.read()
        if current == self._injected:
            return
        context.set_system_section(NOTES_SECTION_HEADER, current)
        self._injected = current

    def before_llm(self, context: RunContext) -> None:
        """Refresh the section if the notes changed during this run.

        Injecting only at ``on_run_start`` was the gap this closes. A model
        that wrote its plan at iteration 3 spent iterations 4..N looking at
        the *pre-write* notes: the new ones existed on disk and in the tool
        confirmation, and compaction shrinks a note body out of that
        confirmation as readily as any other oversized argument — so on
        exactly the long runs the scratchpad exists for, the freshest state
        was the one the model could not see (ADR-105).
        """
        self._inject(context)

    def after_llm(self, context: RunContext, response: Any) -> None:
        """Ignore."""

    def on_llm_error(self, context: RunContext, error: BaseException) -> None:
        """Ignore: a failed model call does not change the notes."""

    def before_tool(self, name: str, arguments: dict[str, Any]) -> None:
        """Ignore."""

    def after_permission(self, name: str, outcome: str) -> None:
        """Ignore."""

    def after_tool(
        self, name: str, result: str, outcome: ToolOutcome | None = None
    ) -> None:
        """Ignore."""

    def on_run_end(self, context: RunContext) -> None:
        """Ignore: the tools persist as they go, so there is nothing to flush."""


def _result(outcome: str, refusal: str) -> ToolResult:
    """Turn the scratchpad's sentence into an observation with a failure bit.

    Two things count as a failure and both used to read as a success. An
    oversized write is refused with ``refusal`` in the sentence, and a write
    the volume would not take says :data:`NOT_PERSISTED` — the second was
    not checked at all, so a read-only mount produced a green observation
    and a model that went on believing its plan was safe (ADR-058, ADR-105).

    Args:
        outcome: What the scratchpad said.
        refusal: The wording that means this particular call was refused.

    Returns:
        The observation, marked failed when either applies.
    """
    failed = refusal in outcome or NOT_PERSISTED in outcome
    return ToolResult(outcome, ok=not failed)


class NotesToolBase(Tool):
    """Shared plumbing for the two note tools.

    Takes the scratchpad rather than a path, and passes the memory root up
    to :class:`~harness.tools.base.Tool` only because every tool has a
    confinement root the permission policy reads. Neither tool joins a path
    onto it — they write one fixed file — so they cannot reach the
    filesystem in the way the file tools can.
    """

    risk = "mutate"

    def __init__(
        self,
        scratchpad: Scratchpad,
        max_output_chars: int = DEFAULT_MAX_OUTPUT_CHARS,
    ) -> None:
        """Args:
        scratchpad: The notes these tools edit.
        max_output_chars: Per-observation character cap.
        """
        root = scratchpad.path.parent if scratchpad.path is not None else Path(".")
        super().__init__(root, max_output_chars)
        self._scratchpad = scratchpad


class NoteWrite(NotesToolBase):
    """Replace the working notes."""

    name = "note_write"
    description = (
        "Replace your working notes for this task. Notes survive when older "
        "tool results are compacted away, so keep what you would otherwise "
        "have to redo work to recover: what is done, what is left, and any "
        "decision you made."
    )
    parameters: dict[str, Any] = {
        "type": "object",
        "properties": {
            "content": {
                "type": "string",
                "description": (
                    "The full replacement notes, in markdown. A short list beats prose."
                ),
            }
        },
        "required": ["content"],
    }

    def _run(self, arguments: dict[str, Any]) -> ToolResult:
        """Write the notes, or report why they were refused."""
        content = self._string_argument(arguments, "content")
        return _result(self._scratchpad.write(content), "not saved")


class NoteAppend(NotesToolBase):
    """Add one line to the working notes."""

    name = "note_append"
    description = (
        "Add a line to your working notes without resending the rest. Use "
        "this to tick off a step of a long task as you finish it."
    )
    parameters: dict[str, Any] = {
        "type": "object",
        "properties": {
            "content": {
                "type": "string",
                "description": "The line to add.",
            }
        },
        "required": ["content"],
    }

    def _run(self, arguments: dict[str, Any]) -> ToolResult:
        """Append the line, or report why it was refused."""
        content = self._string_argument(arguments, "content")
        return _result(self._scratchpad.append(content), "not added")


def note_tools(
    scratchpad: Scratchpad,
    max_output_chars: int = DEFAULT_MAX_OUTPUT_CHARS,
) -> list[Tool]:
    """Return the note tools.

    Args:
        scratchpad: The notes they edit.
        max_output_chars: Per-observation character cap.

    Returns:
        Freshly constructed tools, ready to ``register``.
    """
    return [
        NoteWrite(scratchpad, max_output_chars),
        NoteAppend(scratchpad, max_output_chars),
    ]
