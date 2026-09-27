"""Discover skills on disk and expose them at three levels of detail.

A skill is a folder holding a ``SKILL.md`` whose YAML frontmatter carries a
name and a description, followed by a markdown body:

.. code-block:: text

    skills/
      daily-report/
        SKILL.md          frontmatter + body
        reference.md      optional bundled file

The point of the mechanism is **progressive disclosure**, and the three
levels are what implement it (ADR-038):

1. name and description only, injected into every run — tens of tokens per
   skill, enough for the model to decide whether a skill is relevant;
2. the ``SKILL.md`` body, read with ``skill_read`` once it decides one is;
3. bundled files beside it, read individually and only if the body says to.

Loading every body up front would defeat the purpose: a skill library is
meant to be able to grow without the cost of the prompt growing with it.

The skills root is **read-only**. There is no tool that writes into it and
no volume mounted over it. A writable skill directory would be a persistent
instruction-injection channel: text written during one run would become
part of the system prompt of every later run.
"""

from dataclasses import dataclass
from pathlib import Path

import yaml

from harness.tools.paths import resolve_in_workspace

# The file that makes a directory a skill. Fixed name so discovery needs no
# configuration and a skill folder is recognisable at a glance.
SKILL_FILENAME = "SKILL.md"

# Frontmatter fence, as used by the format this follows.
_FENCE = "---"


@dataclass(frozen=True)
class Skill:
    """One discovered skill.

    Frozen because a skill is read-only reference material: nothing in a run
    should be able to edit the library it is reading from.

    Attributes:
        name: Identifier the model passes to ``skill_read``. Taken from the
            frontmatter, not the folder name, so the two cannot disagree
            silently — a mismatch is a load error instead.
        description: One line telling the model when this skill applies.
            This is the *only* part that costs tokens on every run.
        directory: Folder holding ``SKILL.md`` and any bundled files.
        allowed_tools: Tool names the skill says it needs. Parsed and
            surfaced, **not enforced**. Enforcement is the Week 3
            permission policy (ADR-038); shipping a half-working gate
            here would look like a boundary that is not one.
    """

    name: str
    description: str
    directory: Path
    allowed_tools: tuple[str, ...] = ()


class SkillLibrary:
    """The skills found under one read-only root."""

    def __init__(self, root: Path, skills: tuple[Skill, ...]) -> None:
        """Args:
        root: Skills root, used to confine ``read``.
        skills: Discovered skills, in index order.
        """
        self._root = root
        self._skills = skills

    @property
    def root(self) -> Path:
        """Return the read-only skills root."""
        return self._root

    def __len__(self) -> int:
        """Return how many skills were discovered."""
        return len(self._skills)

    def names(self) -> list[str]:
        """Return the discovered skill names, in index order."""
        return [skill.name for skill in self._skills]

    def index(self) -> str:
        """Return level one: names and descriptions only.

        Returns:
            One line per skill, or an empty string when there are none. An
            empty string rather than a "no skills" note, so a harness with
            no library spends no tokens saying so.
        """
        if not self._skills:
            return ""
        return "\n".join(
            f"- {skill.name}: {skill.description}" for skill in self._skills
        )

    def get(self, name: str) -> Skill | None:
        """Return the named skill, or ``None`` if it was not discovered."""
        for skill in self._skills:
            if skill.name == name:
                return skill
        return None

    def read(self, name: str, filename: str = SKILL_FILENAME) -> str:
        """Return level two or three: a file belonging to one skill.

        Both levels are the same operation, which is why they are one
        method: ``SKILL.md`` is just the file you read first.

        Args:
            name: Skill name from the index.
            filename: File inside the skill folder. Defaults to the skill
                body.

        Returns:
            The file's text.

        Raises:
            KeyError: If no such skill was discovered.
            PathEscapeError: If ``filename`` points outside the skill
                folder. Confinement is nested here: inside the skills root,
                and inside this one skill's folder, so a bundled-file read
                cannot wander into a different skill.
            FileNotFoundError: If the file does not exist.
            IsADirectoryError: If the path is a directory.
        """
        skill = self.get(name)
        if skill is None:
            available = ", ".join(self.names()) or "none"
            raise KeyError(f"unknown skill {name!r}. Available skills: {available}")

        target = resolve_in_workspace(
            skill.directory, filename, root_label=f"{name} skill folder"
        )
        if not target.exists():
            raise FileNotFoundError(target)
        if target.is_dir():
            raise IsADirectoryError(target)
        return target.read_text(encoding="utf-8")

    def files(self, name: str) -> list[str]:
        """Return the bundled filenames of one skill, for level three.

        Args:
            name: Skill name from the index.

        Returns:
            Sorted filenames relative to the skill folder, excluding the
            body itself. Empty when the skill has no bundled files or is
            unknown.
        """
        skill = self.get(name)
        if skill is None:
            return []
        try:
            found = sorted(p for p in skill.directory.rglob("*") if p.is_file())
        except OSError:
            return []
        return [
            path.relative_to(skill.directory).as_posix()
            for path in found
            if path.name != SKILL_FILENAME
        ]


class SkillLoadError(ValueError):
    """Raised when a ``SKILL.md`` exists but cannot be used.

    Separate from "no skills found", which is not an error. A malformed
    skill is a defect in the library that someone must fix, so it is worth
    a distinct type the CLI can report rather than a silent skip.
    """


def load_skills(root: Path) -> SkillLibrary:
    """Discover every skill under ``root``.

    A missing root yields an empty library rather than an error: a harness
    shipped without skills is a valid configuration, and the skills root is
    read-only so there is nothing to create.

    Malformed skills are **skipped, not fatal**. One badly edited
    ``SKILL.md`` must not stop the agent from starting, for the same reason
    an unreadable memory file does not: these are conveniences layered on
    the agent, never preconditions.

    Names are unique without a check here, and that is the reason
    :func:`_parse_skill` insists the declared name match the folder name:
    two folders in one directory cannot share a name, so the file system
    supplies the uniqueness guarantee. A separate duplicate check would be
    unreachable code pretending to be a safeguard.

    Args:
        root: Skills root, typically ``/app/skills`` in the container.

    Returns:
        The library, possibly empty.
    """
    if not root.is_dir():
        return SkillLibrary(root, ())

    discovered: list[Skill] = []
    # Sorted so the index order, and therefore the prompt, is identical on
    # every run and on every machine.
    for candidate in sorted(root.iterdir()):
        if not candidate.is_dir():
            continue
        body = candidate / SKILL_FILENAME
        if not body.is_file():
            continue
        try:
            discovered.append(_parse_skill(body, candidate))
        except (SkillLoadError, OSError, UnicodeDecodeError, yaml.YAMLError):
            continue

    return SkillLibrary(root, tuple(discovered))


def _parse_skill(body: Path, directory: Path) -> Skill:
    """Parse one ``SKILL.md`` into a :class:`Skill`.

    Args:
        body: Path to the ``SKILL.md`` file.
        directory: Folder containing it.

    Returns:
        The parsed skill.

    Raises:
        SkillLoadError: If the frontmatter is missing, unparseable, not a
            mapping, or lacks a usable name or description.
    """
    text = body.read_text(encoding="utf-8")
    metadata, _ = split_frontmatter(text)

    name = str(metadata.get("name", "")).strip()
    description = str(metadata.get("description", "")).strip()
    if not name:
        raise SkillLoadError(f"{body} frontmatter has no 'name'")
    if not description:
        raise SkillLoadError(f"{body} frontmatter has no 'description'")
    if name != directory.name:
        # Two sources of truth for one identifier is a bug waiting to
        # happen: the index would advertise one name while the folder,
        # and therefore the bundled files, live under another.
        raise SkillLoadError(
            f"{body} declares name {name!r} but sits in folder "
            f"{directory.name!r}; they must match"
        )
    return Skill(
        name=name,
        description=description,
        directory=directory,
        allowed_tools=_parse_allowed_tools(metadata.get("allowed-tools")),
    )


def _parse_allowed_tools(raw: object) -> tuple[str, ...]:
    """Normalise the optional ``allowed-tools`` frontmatter field.

    The Agent Skills format accepts a YAML list or a space-separated
    string. Either becomes a tuple of distinct names, in the order they
    were written. An absent or empty field is an empty tuple, which
    means "no restriction declared" — and, this week, declared
    restrictions are not applied either.

    Args:
        raw: The YAML value, or ``None`` when the key is missing.

    Returns:
        Tool names, unique, in declaration order.
    """
    if raw is None:
        return ()
    if isinstance(raw, str):
        names = raw.split()
    elif isinstance(raw, list):
        names = [str(item).strip() for item in raw if str(item).strip()]
    else:
        # A number or a mapping is not a tool list. Treat it as undeclared
        # rather than aborting the skill: the field is optional and unused
        # this week, so a typo in it must not hide the skill.
        return ()

    seen: set[str] = set()
    ordered: list[str] = []
    for name in names:
        if name and name not in seen:
            seen.add(name)
            ordered.append(name)
    return tuple(ordered)


def split_frontmatter(text: str) -> tuple[dict[str, object], str]:
    """Split YAML frontmatter from a markdown body.

    Args:
        text: Full file text, expected to open with a ``---`` fence.

    Returns:
        The parsed mapping and the remaining body.

    Raises:
        SkillLoadError: If the fence is missing or unterminated, or the
            frontmatter is not a mapping.
        yaml.YAMLError: If the frontmatter is not valid YAML.
    """
    lines = text.splitlines(keepends=True)
    if not lines or lines[0].strip() != _FENCE:
        raise SkillLoadError("file does not start with a '---' frontmatter fence")

    for index in range(1, len(lines)):
        if lines[index].strip() != _FENCE:
            continue
        raw = "".join(lines[1:index])
        loaded = yaml.safe_load(raw) or {}
        if not isinstance(loaded, dict):
            raise SkillLoadError("frontmatter must be a YAML mapping")
        return loaded, "".join(lines[index + 1 :])

    raise SkillLoadError("frontmatter fence is never closed")


__all__ = [
    "SKILL_FILENAME",
    "Skill",
    "SkillLibrary",
    "SkillLoadError",
    "load_skills",
    "split_frontmatter",
]
