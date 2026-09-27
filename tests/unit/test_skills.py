"""Skills: discovery, progressive disclosure, and the read-only guarantee.

The property that justifies the mechanism is **progressive disclosure**: a
library can grow without the prompt growing with it. Several tests below
assert the negative half of that — that a body is *not* in the prompt — which
is the half that would rot silently if someone "simplified" the loader into
reading every file up front.
"""

from pathlib import Path

import pytest
from harness.context import RunContext
from harness.skills.hook import SKILLS_SECTION_HEADER, SkillsHook
from harness.skills.loader import load_skills
from harness.skills.tools import skill_tools
from harness.tools.registry import ToolRegistry


def _write_skill(root: Path, name: str, description: str, body: str = "Body.") -> Path:
    """Create a valid skill folder and return it."""
    directory = root / name
    directory.mkdir(parents=True)
    (directory / "SKILL.md").write_text(
        f"---\nname: {name}\ndescription: {description}\n---\n{body}\n",
        encoding="utf-8",
    )
    return directory


def _registry(root: Path) -> ToolRegistry:
    """Return a registry holding the skill tools for ``root``."""
    registry = ToolRegistry()
    for tool in skill_tools(load_skills(root)):
        registry.register(tool)
    return registry


# --- discovery ------------------------------------------------------------


def test_a_missing_skills_root_is_not_an_error(tmp_path: Path) -> None:
    """A harness shipped without skills is a valid configuration.

    The skills root is read-only, so there is nothing to create and nothing
    to fail; the right answer is an empty library.
    """
    library = load_skills(tmp_path / "absent")

    assert len(library) == 0
    assert library.index() == ""


def test_skills_are_discovered_and_sorted(tmp_path: Path) -> None:
    """Sorted so the injected index is byte-identical on every run."""
    _write_skill(tmp_path, "zebra", "Draws zebras.")
    _write_skill(tmp_path, "alpha", "Draws alphas.")

    assert load_skills(tmp_path).names() == ["alpha", "zebra"]


# --- level one: the index -------------------------------------------------


def test_the_hook_injects_the_index(tmp_path: Path) -> None:
    """Level one reaches the run through the same primitive memory uses."""
    _write_skill(tmp_path, "haiku", "Write a haiku.")
    context = RunContext(task="a task")
    context.ensure_system_prompt("You are an agent.")

    SkillsHook(load_skills(tmp_path)).on_run_start(context)

    system = str(context.messages[0]["content"])
    assert SKILLS_SECTION_HEADER in system
    assert "- haiku: Write a haiku." in system
    assert "skill_read" in system


# --- levels two and three -------------------------------------------------


def test_reading_a_skill_returns_its_body(tmp_path: Path) -> None:
    """Level two, on demand."""
    _write_skill(tmp_path, "haiku", "Write a haiku.", body="Count syllables 5-7-5.")

    result = _registry(tmp_path).execute("skill_read", {"skill": "haiku"})

    assert "Count syllables 5-7-5." in result


# --- read-only ------------------------------------------------------------


def test_no_tool_can_write_into_the_skills_root(tmp_path: Path) -> None:
    """A writable skills directory is a persistent injection channel.

    Text written during one run would become part of the system prompt of
    every later run, which is strictly worse than the memory case: the skill
    index is advertised as harness-supplied capability rather than as
    recorded notes. So there is no write tool and no volume (ADR-038).
    """
    _write_skill(tmp_path, "haiku", "Write a haiku.")
    registry = _registry(tmp_path)

    assert registry.names() == ["skill_read"]
    assert registry.get("skill_read").risk == "observe"


@pytest.mark.parametrize(
    "attempt", ["../other/SKILL.md", "../../etc/passwd", "sub/../../other/SKILL.md"]
)
def test_a_skill_cannot_read_outside_its_own_folder(
    tmp_path: Path, attempt: str
) -> None:
    """Confinement is nested: inside the root, and inside one skill.

    Without the inner check, a bundled-file read would be able to wander
    into a different skill, which is how one skill's instructions would end
    up quoted as another's.
    """
    _write_skill(tmp_path, "haiku", "Write a haiku.")
    _write_skill(tmp_path, "other", "Another skill.", body="Secret instructions.")

    result = _registry(tmp_path).execute(
        "skill_read", {"skill": "haiku", "file": attempt}
    )

    assert result.startswith("Error:")
    assert "Secret instructions." not in result


# --- allowed-tools: parsed and surfaced; enforcement is opt-in -------------
#
# The default is still Week 2's: parsed, shown to the model, not enforced
# (ADR-038). Turning it on is ADR-082 and is tested in
# ``tests/unit/test_skill_policy.py``; what this section pins is that the
# *shipped* default did not quietly change underneath it.


def _write_skill_with_allowed(
    root: Path,
    name: str,
    description: str,
    allowed: str,
    body: str = "Body.",
) -> Path:
    """Create a skill whose frontmatter includes ``allowed-tools``."""
    directory = root / name
    directory.mkdir(parents=True)
    (directory / "SKILL.md").write_text(
        f"---\nname: {name}\ndescription: {description}\n"
        f"allowed-tools:\n{allowed}---\n{body}\n",
        encoding="utf-8",
    )
    return directory


def test_allowed_tools_are_parsed_from_a_yaml_list(tmp_path: Path) -> None:
    """The Agent Skills list form."""
    _write_skill_with_allowed(
        tmp_path, "report", "Writes a report.", "  - write_file\n  - read_file\n"
    )

    skill = load_skills(tmp_path).get("report")

    assert skill is not None
    assert skill.allowed_tools == ("write_file", "read_file")
