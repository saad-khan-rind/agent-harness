"""The tool that turns a skill name into instructions.

One tool, because levels two and three of progressive disclosure are the
same operation: read a file belonging to a skill. ``skill_read('x')`` gets
the body, ``skill_read('x', 'reference.md')`` gets a bundled file.

The tool is ``observe``: skills are read-only reference material, and there
is deliberately no tool that writes into the skills root (ADR-038).
"""

from typing import Any

from harness.skills.loader import SKILL_FILENAME, SkillLibrary
from harness.skills.policy import ActiveSkills
from harness.tools.base import DEFAULT_MAX_OUTPUT_CHARS, Tool, ToolResult


class SkillRead(Tool):
    """Load one skill's instructions, or one of its bundled files."""

    name = "skill_read"
    description = (
        "Load the full instructions for a skill listed in the Skills "
        "section of your instructions. Do this before following it."
    )
    parameters: dict[str, Any] = {
        "type": "object",
        # Called ``skill`` rather than ``name``: the model is choosing a
        # skill, and a bare ``name`` in a tool that also takes a ``file``
        # reads like the file's name.
        "properties": {
            "skill": {
                "type": "string",
                "description": "Skill name, exactly as listed in the Skills section.",
            },
            "file": {
                "type": "string",
                "description": (
                    f"Optional file inside the skill folder. Defaults to "
                    f"'{SKILL_FILENAME}', the skill's instructions. Use it "
                    f"only for a file the instructions tell you to read."
                ),
            },
        },
        "required": ["skill"],
    }
    risk = "observe"

    def __init__(
        self,
        library: SkillLibrary,
        max_output_chars: int = DEFAULT_MAX_OUTPUT_CHARS,
        active: ActiveSkills | None = None,
    ) -> None:
        """Args:
        library: The discovered skills. Its root doubles as this tool's
            confinement root, so ``resolve_in_workspace`` keeps reads
            inside the skills directory exactly as it keeps the file
            tools inside the workspace (ADR-035).
        max_output_chars: Per-observation character cap.
        active: The run's active-skill set, when ``allowed-tools``
            enforcement is switched on. ``None`` — the default — means the
            declared list is shown but not enforced, which is Week 2's
            behaviour and still the shipped default (ADR-038, ADR-082).
            The tool only ever *records* what it read; the decision
            belongs to
            :class:`~harness.skills.policy.SkillScopedPolicy`.
        """
        super().__init__(library.root, max_output_chars)
        self._library = library
        self._active = active

    def _run(self, arguments: dict[str, Any]) -> ToolResult:
        """Return the requested skill file.

        An unknown skill name comes back as a failed observation listing the
        real names rather than an exception, because a model that misreads
        the index can fix that on its next turn.
        """
        skill = self._string_argument(arguments, "skill")
        filename = (
            self._string_argument(arguments, "file")
            if "file" in arguments
            else SKILL_FILENAME
        )
        try:
            content = self._library.read(skill, filename)
        except KeyError as exc:
            return ToolResult(str(exc.args[0]), ok=False)

        bundled = self._library.files(skill)
        footers: list[str] = []
        # Level three is advertised only once the model is already reading
        # level two, which is the whole point: the existence of these files
        # costs nothing until the skill is in play.
        if bundled and filename == SKILL_FILENAME:
            footers.append(
                f"Files bundled with this skill, readable with "
                f"skill_read(skill={skill!r}, file=...): {', '.join(bundled)}"
            )
        # The declared tool list travels with the body so a reader of the
        # transcript can see what the skill claimed it needed — and, since
        # ADR-082, so the permission policy can hold the model to it. The
        # footer says which of the two is happening, because "these are the
        # tools" and "these are the only tools" are very different sentences
        # to a model that is about to plan.
        if filename == SKILL_FILENAME:
            declared = self._library.get(skill)
            if declared is not None and declared.allowed_tools:
                listed = ", ".join(declared.allowed_tools)
                if self._active is not None:
                    footers.append(
                        f"While this skill is loaded you may use only these "
                        f"tools: {listed}. Anything else will be refused."
                    )
                else:
                    footers.append(
                        f"This skill declares it needs these tools "
                        f"(not enforced): {listed}"
                    )
        # Recorded after the body is read, never before: a skill that could
        # not be loaded must not narrow what the model may do (ADR-082).
        if self._active is not None and filename == SKILL_FILENAME:
            self._active.note_read(skill)
        footer = ("\n\n" + "\n".join(footers)) if footers else ""
        return ToolResult(f"Skill {skill} ({filename}):\n{content}{footer}")


def skill_tools(
    library: SkillLibrary,
    max_output_chars: int = DEFAULT_MAX_OUTPUT_CHARS,
    active: ActiveSkills | None = None,
) -> list[Tool]:
    """Return the skill tools, or nothing when the library is empty.

    An empty library registers no tool at all. Advertising ``skill_read``
    with nothing to read would spend schema tokens on every request and
    invite the model to call it and fail.

    Args:
        library: The discovered skills.
        max_output_chars: Per-observation character cap.
        active: The run's active-skill set when ``allowed-tools``
            enforcement is on, otherwise ``None``.

    Returns:
        Freshly constructed tools, ready to ``register``.
    """
    if not len(library):
        return []
    return [SkillRead(library, max_output_chars, active)]
