"""Inject the skill index into every run.

Level one of progressive disclosure. The hook writes one labelled section
holding names and descriptions; bodies arrive later through ``skill_read``.

It reuses :meth:`RunContext.set_system_section`, the same primitive memory
uses, which is why neither subsystem owns it and why ``loop.py`` mentions
neither (ADR-038).
"""

from typing import Any

from harness.context import RunContext
from harness.skills.loader import SkillLibrary
from harness.tools.base import ToolOutcome

SKILLS_SECTION_HEADER = "## Skills available"

# Spent on every run, so it earns its length by telling the model the one
# thing the index cannot: that these are names, and the instructions are
# somewhere else.
_PREAMBLE = (
    "Each line is a skill you can load. If one matches the task, call "
    "skill_read with its name to get the instructions, then follow them."
)


class SkillsHook:
    """Advertise the skill library at the start of each run."""

    def __init__(self, library: SkillLibrary, max_inject_chars: int = 2000) -> None:
        """Args:
        library: The discovered skills.
        max_inject_chars: Ceiling on the injected index. A library big
            enough to exceed it is a signal that descriptions have
            grown into instructions, which is what the body is for.
        """
        self._library = library
        self._max_inject_chars = max_inject_chars

    def on_run_start(self, context: RunContext) -> None:
        """Write the skill index into the system message.

        Idempotent, so the section is refreshed rather than duplicated on
        every REPL turn.
        """
        index = self._library.index()
        body = f"{_PREAMBLE}\n\n{index}" if index else ""
        context.set_system_section(SKILLS_SECTION_HEADER, body, self._max_inject_chars)

    def before_llm(self, context: RunContext) -> None:
        """Ignore: the library is read-only, so the index cannot change."""

    def after_llm(self, context: RunContext, response: Any) -> None:
        """Ignore."""

    def on_llm_error(self, context: RunContext, error: BaseException) -> None:
        """Ignore: a failed model call changes nothing about the skill index."""

    def before_tool(self, name: str, arguments: dict[str, Any]) -> None:
        """Ignore."""

    def after_permission(self, name: str, outcome: str) -> None:
        """Ignore the permission decision: skills inject, they do not police."""

    def after_tool(
        self, name: str, result: str, outcome: ToolOutcome | None = None
    ) -> None:
        """Ignore."""

    def on_run_end(self, context: RunContext) -> None:
        """Ignore: skills are reference material, so nothing is recorded."""
