"""The skills this repository actually ships.

Every other skill test builds its input in ``tmp_path``, which proves the
loader works but says nothing about the two skills the agent is given. This
file loads the real ``skills/`` directory, and it exists mainly to catch one
specific recurring mistake.

``.dockerignore`` used to exclude ``*.md``. Skills *are* markdown, so an
over-broad ignore rule would silently ship an image whose skill library is
empty — no error, no warning, just an agent that has quietly lost a
capability. That rule is gone (ADR-104), but the class of mistake is not:
any pattern wide enough to catch a ``SKILL.md`` fails here instead of in a
demo. Both container images copy ``skills/`` and this test fails if the
library ever arrives empty.
"""

from pathlib import Path

import pytest
from harness.skills.loader import SKILL_FILENAME, load_skills, split_frontmatter
from harness.skills.tools import skill_tools
from harness.tools.base import DEFAULT_MAX_OUTPUT_CHARS

# Two directories up from tests/unit/, i.e. the repository root. Resolved
# from this file rather than from the working directory so the test gives
# the same answer under pytest, the container, and an IDE runner.
_REPO_ROOT = Path(__file__).resolve().parents[2]
_SKILLS_ROOT = _REPO_ROOT / "skills"


def test_the_shipped_library_is_not_empty() -> None:
    """The .dockerignore trap, and the reason this file exists.

    If this fails inside the container, ``skills/`` did not reach the image:
    check the ignore files for a pattern that catches ``SKILL.md``.
    """
    library = load_skills(_SKILLS_ROOT)

    assert len(library) >= 2, f"no skills found under {_SKILLS_ROOT}"


def test_every_shipped_skill_loads() -> None:
    """A malformed skill is skipped silently at run time, by design.

    That is right for robustness and wrong for development: a typo in our
    own frontmatter would cost the agent a capability with no visible
    symptom. So discovery is compared against the folders on disk, and a
    skipped skill fails the gate.
    """
    on_disk = sorted(
        path.name
        for path in _SKILLS_ROOT.iterdir()
        if path.is_dir() and (path / SKILL_FILENAME).is_file()
    )

    assert load_skills(_SKILLS_ROOT).names() == on_disk


@pytest.mark.parametrize(
    "directory",
    sorted(
        path
        for path in _SKILLS_ROOT.iterdir()
        if path.is_dir() and (path / SKILL_FILENAME).is_file()
    )
    if _SKILLS_ROOT.is_dir()
    else [],
)
def test_shipped_frontmatter_is_valid(directory: Path) -> None:
    """Parametrised per skill so a failure names the offending one."""
    text = (directory / SKILL_FILENAME).read_text(encoding="utf-8")

    metadata, body = split_frontmatter(text)

    assert metadata.get("name") == directory.name
    assert str(metadata.get("description", "")).strip()
    assert body.strip(), "a skill with no body teaches the model nothing"
    # Optional, so a missing field is fine. A present one must be a list
    # or a string — anything else is a typo the loader would silently drop.
    allowed = metadata.get("allowed-tools")
    if allowed is not None:
        assert isinstance(allowed, (list, str)), directory.name


def test_descriptions_are_short_enough_to_inject() -> None:
    """Descriptions are spent on every request, so they are bounded.

    A description long enough to be instructions is a sign the content
    belongs in the body, which is loaded on demand — the whole point of
    progressive disclosure.
    """
    for skill in load_skills(_SKILLS_ROOT).names():
        library = load_skills(_SKILLS_ROOT)
        entry = library.get(skill)
        assert entry is not None
        assert len(entry.description) <= 200, skill


def test_every_shipped_skill_file_fits_in_one_observation() -> None:
    """A skill longer than the observation budget loses its own ending.

    Found the hard way. ``Tool.execute`` truncates every observation to
    ``max_output_chars`` (4000 by default), and a skill body is delivered
    *as* an observation — so a SKILL.md that grows past the budget has its
    last rules cut off, silently, with no error anywhere. The rules at the
    end of a skill file are usually the ones about not doing something,
    which makes this the worst possible half to lose.

    The fix when this fails is not a bigger budget: it is to move the
    example or the long-form material into a bundled file, which is what
    level three of progressive disclosure is for (ADR-038). Each file is
    measured on its own, because each arrives in its own observation.

    A 25% margin is left so the failure arrives while a skill is being
    edited, rather than on the day someone adds one more rule.
    """
    library = load_skills(_SKILLS_ROOT)
    tool = skill_tools(library)[0]
    budget = int(DEFAULT_MAX_OUTPUT_CHARS * 0.75)

    for name in library.names():
        for filename in (SKILL_FILENAME, *library.files(name)):
            observation = tool.execute({"skill": name, "file": filename})
            assert "truncated" not in observation, f"{name}/{filename}"
            assert len(observation) <= budget, (
                f"{name}/{filename} is {len(observation)} chars of a "
                f"{DEFAULT_MAX_OUTPUT_CHARS}-char budget; move the long part "
                f"into a bundled file the model reads only when it needs it"
            )


def test_bundled_files_are_reachable_from_their_own_skill() -> None:
    """Level three is only real if the files are actually readable.

    Also pins the nesting: a bundled file is read through the skill that
    owns it, not through the library root.
    """
    library = load_skills(_SKILLS_ROOT)

    for name in library.names():
        for filename in library.files(name):
            assert library.read(name, filename)
