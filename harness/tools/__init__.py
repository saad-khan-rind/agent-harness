"""Built-in file tools and the registry the loop talks to.

This package is the boundary the brief calls the *tool registry*
(Section 8.2). The loop imports :class:`ToolRegistry` and nothing else from
here — it never names a concrete tool class.
"""

from collections.abc import Sequence
from pathlib import Path

from harness.tools.base import (
    DEFAULT_MAX_OUTPUT_CHARS,
    Tool,
    ToolOutcome,
    ToolResult,
)
from harness.tools.mutate import AppendFile, CreateDirectory, EditFile, WriteFile
from harness.tools.observe import (
    DEFAULT_MAX_SEARCH_RESULTS,
    FindFiles,
    ListDirectory,
    ReadFile,
    SearchText,
)
from harness.tools.paths import PathEscapeError
from harness.tools.registry import ToolRegistry

__all__ = [
    "PathEscapeError",
    "Tool",
    "ToolOutcome",
    "ToolRegistry",
    "ToolResult",
    "build_registry",
    "builtin_tools",
]


def builtin_tools(
    workspace_root: Path,
    max_output_chars: int = DEFAULT_MAX_OUTPUT_CHARS,
    max_search_results: int = DEFAULT_MAX_SEARCH_RESULTS,
) -> list[Tool]:
    """Return the built-in file tools, observe tools first.

    There is deliberately **no delete tool** in this list, and no class to
    add to it (see :mod:`harness.tools.mutate`).

    Observe tools come first so they appear first in the schema list the
    model sees. It is a small nudge toward looking before mutating, which
    costs nothing and reduces blind overwrites.

    Args:
        workspace_root: Sandbox directory for all relative paths.
        max_output_chars: Per-observation character cap.
        max_search_results: Hit limit for ``search_text`` and ``find_files``.

    Returns:
        Freshly constructed tools, ready to ``register``.
    """
    return [
        ListDirectory(workspace_root, max_output_chars),
        ReadFile(workspace_root, max_output_chars),
        SearchText(workspace_root, max_output_chars, max_search_results),
        FindFiles(workspace_root, max_output_chars, max_search_results),
        CreateDirectory(workspace_root, max_output_chars),
        WriteFile(workspace_root, max_output_chars),
        AppendFile(workspace_root, max_output_chars),
        EditFile(workspace_root, max_output_chars),
    ]


def build_registry(
    workspace_root: Path,
    max_output_chars: int = DEFAULT_MAX_OUTPUT_CHARS,
    max_search_results: int = DEFAULT_MAX_SEARCH_RESULTS,
    extra_tools: Sequence[Tool] | None = None,
) -> ToolRegistry:
    """Build a registry pre-loaded with the built-in file tools.

    Convenience wrapper so the CLI, the tests, and any future sub-agent
    factory all assemble the default tool set the same way.

    ``extra_tools`` is how capabilities that are *not* file tools join the
    same registry without this package importing them. Memory tools use it
    today and MCP-discovered tools will use it next; keeping the direction
    of the dependency this way round is what stops ``harness.tools`` from
    growing a reference to every other subsystem.

    Args:
        workspace_root: Sandbox directory for all relative paths.
        max_output_chars: Per-observation character cap.
        max_search_results: Hit limit for ``search_text`` and ``find_files``.
        extra_tools: Already-constructed tools to register after the
            built-ins. A name collision raises, as it does for any
            duplicate registration.

    Returns:
        A registry containing the built-in file tools and any extras.
    """
    registry = ToolRegistry()
    for tool in builtin_tools(workspace_root, max_output_chars, max_search_results):
        registry.register(tool)
    for tool in extra_tools or ():
        registry.register(tool)
    return registry
