"""Assemble the agent's complete tool set in one place.

``harness.tools.build_registry`` deliberately knows only about file tools:
letting it import memory and skills would point the dependency the wrong
way and make ``harness.tools`` reference every other subsystem. So the
knowledge of *which* subsystems contribute tools lives here instead, in one
function that the CLI, the tests, and Week 3's sub-agent factory all call.

Having exactly one assembler matters for a reason beyond tidiness: if the
tests hand-assembled their own registry, the tool set under test could drift
from the tool set that ships, and the contract tests would be validating a
fiction. ``AGENTS.md`` requires the tests to exercise production wiring, and
this is that wiring.
"""

from collections.abc import Sequence
from pathlib import Path

from harness.memory.consolidate import MemoryWriter
from harness.memory.notes import note_tools
from harness.memory.protocol import MemoryStore
from harness.memory.scratchpad import Scratchpad
from harness.memory.tools import memory_tools
from harness.skills.loader import SkillLibrary
from harness.skills.policy import ActiveSkills
from harness.skills.tools import skill_tools
from harness.tools import ToolRegistry, build_registry
from harness.tools.base import DEFAULT_MAX_OUTPUT_CHARS, Tool
from harness.tools.observe import DEFAULT_MAX_SEARCH_RESULTS


def build_agent_registry(
    workspace_root: Path,
    max_output_chars: int = DEFAULT_MAX_OUTPUT_CHARS,
    max_search_results: int = DEFAULT_MAX_SEARCH_RESULTS,
    memory_store: MemoryStore | None = None,
    skill_library: SkillLibrary | None = None,
    mcp_tools: Sequence[Tool] | None = None,
    active_skills: ActiveSkills | None = None,
    scratchpad: Scratchpad | None = None,
    memory_writer: MemoryWriter | None = None,
) -> ToolRegistry:
    """Build the registry the agent actually runs with.

    Registration order is the order the model sees the schemas in, and it is
    chosen rather than incidental: file tools first because most tasks are
    file tasks, then memory, then skills, then anything discovered on an
    MCP server. Observe tools come before mutate tools within each group,
    which is a cheap nudge toward looking before changing. Remote tools
    come last because they are the ones this harness did not write and
    cannot vouch for.

    Args:
        workspace_root: Sandbox root for the file tools.
        max_output_chars: Per-observation character cap, applied to every
            tool including memory and skills.
        max_search_results: Hit limit for ``search_text`` and ``find_files``.
        memory_store: Long-term memory, or ``None`` to register no memory
            tools at all. ``None`` is what ``memory_enabled: false``
            produces, and it must leave the model with no memory tools
            rather than tools that fail.
        skill_library: Discovered skills, or ``None`` for no skill tools.
            An empty library also registers nothing.
        mcp_tools: Tools discovered on MCP servers, already wrapped and
            already filtered by the deny-list. They register exactly like
            a built-in, which is the whole point of the registry seam: a
            duplicate name still raises, so a remote server cannot shadow
            a local tool (ADR-039).
        scratchpad: Short-term working notes, or ``None`` to register no
            note tools. ``None`` is what ``notes_enabled: false`` produces.
        memory_writer: The consolidating write path ``memory_store`` uses,
            or ``None`` for one with no judge (ADR-107). Ignored when
            ``memory_store`` is ``None``.
        active_skills: Passed to ``skill_read`` so it can record which
            skills were loaded, when ``skills_enforce_allowed_tools`` is
            on. ``None`` leaves the declared list advertised but not
            enforced (ADR-082). Nothing else in the registry sees it: the
            enforcement is a policy wrapper, not a tool behaviour.

    Returns:
        A registry holding every tool this harness offers.
    """
    extra: list[Tool] = []
    if scratchpad is not None:
        # Before long-term memory in the schema order: on a long task the
        # notes are the tool the model reaches for most, and the order the
        # schemas are listed in is the order it reads them.
        extra.extend(note_tools(scratchpad, max_output_chars))
    if memory_store is not None:
        extra.extend(memory_tools(memory_store, max_output_chars, memory_writer))
    if skill_library is not None:
        extra.extend(skill_tools(skill_library, max_output_chars, active_skills))
    if mcp_tools:
        extra.extend(mcp_tools)

    return build_registry(
        workspace_root,
        max_output_chars,
        max_search_results,
        extra_tools=extra,
    )
