"""Skills: loadable bundles of instructions, disclosed just in time.

Like memory, skills reach a run through seams that already existed — a
lifecycle hook for the index and the tool registry for ``skill_read`` — so
``loop.py`` does not know they exist. See ADR-038.
"""

from harness.skills.hook import SKILLS_SECTION_HEADER, SkillsHook
from harness.skills.loader import (
    SKILL_FILENAME,
    Skill,
    SkillLibrary,
    SkillLoadError,
    load_skills,
)
from harness.skills.policy import ActiveSkills, SkillScopedPolicy
from harness.skills.tools import SkillRead, skill_tools

__all__ = [
    "SKILLS_SECTION_HEADER",
    "SKILL_FILENAME",
    "ActiveSkills",
    "Skill",
    "SkillLibrary",
    "SkillLoadError",
    "SkillRead",
    "SkillScopedPolicy",
    "SkillsHook",
    "load_skills",
    "skill_tools",
]
