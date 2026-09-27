"""Read-only file tools: ``list_files``, ``read_file``, ``search_text``,
``find_files``.

These cover the "navigate folders and search in files" and "read files"
capabilities of the Week 1 brief (Section 8.2), plus finding files by name
glob so the model does not have to walk the tree with ``list_files``. Every
tool here is ``risk="observe"``: it may not change a single byte of the
workspace.

**No delete tool belongs in this module or any other.** The brief states the
agent CANNOT delete folders or files, and the strongest way to guarantee
that is to never write the class — a tool the registry does not know about
cannot be called, prompted into existence, or accidentally re-enabled by a
permission policy.
"""

import fnmatch
from pathlib import Path
from typing import Any

from harness.tools.base import Tool, ToolResult
from harness.tools.paths import display_path

# Shared JSON-Schema fragment. Defined once so all tools describe paths
# identically to the model — inconsistent wording makes tool choice harder.
_PATH_PROP = {
    "type": "string",
    "description": "Path relative to the workspace root. Use '.' for the root itself.",
}

# Files above this size are not read or searched. Two reasons: a multi-
# megabyte file would blow the context budget even after truncation, and
# reading one is almost always a mistake the model should hear about.
MAX_READABLE_BYTES = 1_000_000

# Cap on search hits so one broad query cannot flood the context.
DEFAULT_MAX_SEARCH_RESULTS = 50


class ListDirectory(Tool):
    """List files and folders under a workspace path.

    Named ``list_files`` for the model (matching the brief's E2E example)
    while the Python class keeps the broader name — it lists directories
    too, and the class name is what the team reads in reviews.
    """

    name = "list_files"
    description = (
        "List one directory level. Folders end with '/' and their contents "
        "are not shown. The only way to see empty folders."
    )
    parameters: dict[str, Any] = {
        "type": "object",
        "properties": {"path": _PATH_PROP},
        "required": ["path"],
    }
    risk = "observe"

    def _run(self, arguments: dict[str, Any]) -> ToolResult:
        """List one directory, one entry per line, directories suffixed ``/``.

        The trailing slash is how the model tells a folder from a file
        without a second tool call. Entries are sorted so repeated runs
        produce identical observations, which makes agent behaviour
        reproducible and tests stable.
        """
        target = self._resolve(arguments)
        if not target.exists():
            raise FileNotFoundError(target)
        if not target.is_dir():
            # Raised rather than returned so the shared handler in
            # Tool.execute writes the message, keeping wording consistent.
            raise NotADirectoryError(target)

        entries = sorted(
            target.iterdir(), key=lambda item: (not item.is_dir(), item.name)
        )
        shown = display_path(self.workspace_root, target)
        if not entries:
            # An explicit "empty" beats an empty string: a blank observation
            # reads to the model like a failed call.
            return ToolResult(f"{shown}/ is empty.")

        lines = [
            f"{entry.name}/" if entry.is_dir() else entry.name for entry in entries
        ]
        header = f"{shown}/ contains {len(entries)} entries:"
        return ToolResult("\n".join([header, *lines]))


class ReadFile(Tool):
    """Read a UTF-8 text file inside the workspace."""

    name = "read_file"
    description = (
        "Read a text file. The first line is 'name (N lines):' — report that "
        "count; do not recount newlines."
    )
    parameters: dict[str, Any] = {
        "type": "object",
        "properties": {"path": _PATH_PROP},
        "required": ["path"],
    }
    risk = "observe"

    def _run(self, arguments: dict[str, Any]) -> ToolResult:
        """Return a one-line header plus the file's exact contents.

        The header carries the line count because "how many lines does this
        file have?" is a question the model otherwise answers by counting
        newlines in its own head — unreliable, and one of the assertions in
        the brief's ``find_and_inspect`` E2E case depends on it.

        Contents follow verbatim (no line numbering) so that text read here
        can be passed straight back to ``edit_file`` as ``old_str``.
        """
        target = self._resolve(arguments)
        if not target.exists():
            raise FileNotFoundError(target)
        if target.is_dir():
            raise IsADirectoryError(target)

        size = target.stat().st_size
        if size > MAX_READABLE_BYTES:
            return ToolResult(
                f"File is {size} bytes, larger than the {MAX_READABLE_BYTES}-byte "
                f"read limit. Use search_text to find the relevant part instead.",
                ok=False,
            )

        # ``strict`` decoding: a decode failure raises UnicodeDecodeError,
        # which Tool.execute turns into a clear "not text" observation.
        # Silently replacing bad bytes would hand the model corrupted text.
        content = target.read_text(encoding="utf-8")
        shown = display_path(self.workspace_root, target)
        if not content:
            return ToolResult(f"{shown} is empty (0 lines).")

        # A trailing newline terminates the last line rather than starting a
        # new empty one, so splitlines() gives the count a human would give.
        line_count = len(content.splitlines())
        return ToolResult(f"{shown} ({line_count} lines):\n{content}")


class SearchText(Tool):
    """Search for an exact substring in files under a workspace path."""

    name = "search_text"
    description = (
        "Search file CONTENTS for an exact substring, not filenames. "
        "Returns matching lines with file and line number."
    )
    parameters: dict[str, Any] = {
        "type": "object",
        "properties": {
            "query": {
                "type": "string",
                "description": "Exact substring to find. Not a regular expression.",
            },
            "path": {
                "type": "string",
                "description": (
                    "File or directory to search, relative to the workspace root. "
                    "Use '.' to search everything."
                ),
            },
        },
        "required": ["query", "path"],
    }
    risk = "observe"

    def __init__(
        self,
        workspace_root: Path,
        max_output_chars: int = 4000,
        max_results: int = DEFAULT_MAX_SEARCH_RESULTS,
    ) -> None:
        """Extend the base tool with a hit limit.

        Args:
            workspace_root: Sandbox root.
            max_output_chars: Per-observation character cap.
            max_results: Stop after this many matching lines.
        """
        super().__init__(workspace_root, max_output_chars)
        self.max_results = max_results

    def _run(self, arguments: dict[str, Any]) -> ToolResult:
        """Walk the target and collect ``path:line: text`` hits.

        Plain substring matching, not regex: the brief asks for "search in
        files", and a regex tool invites the model to send patterns whose
        failure modes (catastrophic backtracking, subtly wrong escapes) are
        hard for it to diagnose from an error string.
        """
        query = self._string_argument(arguments, "query")
        if not query:
            raise ValueError("query must not be empty")
        target = self._resolve(arguments)
        if not target.exists():
            raise FileNotFoundError(target)

        # Sorted walk keeps results deterministic across runs and platforms.
        candidates = (
            [target]
            if target.is_file()
            else sorted(p for p in target.rglob("*") if p.is_file())
        )

        hits: list[str] = []
        skipped = 0
        truncated = False
        for candidate in candidates:
            if len(hits) >= self.max_results:
                truncated = True
                break
            try:
                text = candidate.read_text(encoding="utf-8")
            except (UnicodeDecodeError, OSError):
                # Binary blobs and unreadable files are skipped rather than
                # aborting the whole search: one bad file in a folder should
                # not deny the model results from the other twenty.
                skipped += 1
                continue
            relative = display_path(self.workspace_root, candidate)
            for number, line in enumerate(text.splitlines(), start=1):
                if query in line:
                    hits.append(f"{relative}:{number}: {line.strip()}")
                    if len(hits) >= self.max_results:
                        truncated = True
                        break

        if not hits:
            # Reported as a success: "no matches" is a valid, useful answer,
            # not a failure the model should try to work around.
            note = f" ({skipped} unreadable files skipped)" if skipped else ""
            return ToolResult(f"No matches for {query!r}.{note}")

        header = f"{len(hits)} match(es) for {query!r}:"
        footer = (
            [f"... stopped at the {self.max_results}-result limit."]
            if truncated
            else []
        )
        return ToolResult("\n".join([header, *hits, *footer]))


def _matches_name_glob(relative: str, filename: str, pattern: str) -> bool:
    """Return True if ``pattern`` matches the file's relative path or name.

    ``find_files`` is a *name* search, so two matches both count:

    * The basename (``haiku.md``, ``*.md``) so a nested file is found
      without the model knowing the folder.
    * The workspace-relative path (``notes/*.md``, ``notes/haiku.md``) so a
      narrower glob still works.

    ``fnmatch`` is used instead of regex for the same reason ``search_text``
    is a substring: a slightly wrong pattern from the model should miss,
    not explode. ``*`` matches across slashes here, which is what makes
    ``*.md`` recursive — unlike ``list_files``, which only sees one folder.

    Args:
        relative: Workspace-relative path, as shown to the model.
        filename: Final path component.
        pattern: Glob supplied by the model.

    Returns:
        Whether this file should appear in the observation.
    """
    posix = relative.replace("\\", "/")
    pat = pattern.replace("\\", "/")
    if fnmatch.fnmatchcase(filename, pat) or fnmatch.fnmatchcase(posix, pat):
        return True
    # ``**/haiku.md`` is what models copy from other agents; strip the
    # prefix and match again so it behaves like ``haiku.md`` anywhere.
    if pat.startswith("**/"):
        rest = pat[3:]
        return bool(rest) and (
            fnmatch.fnmatchcase(filename, rest) or fnmatch.fnmatchcase(posix, rest)
        )
    return False


class FindFiles(Tool):
    """Find files by glob under a workspace path.

    Named ``find_files`` so it sits next to ``list_files`` and ``search_text``
    without overlapping them: this tool matches *names*, not contents, and
    it recurses, which ``list_files`` does not.
    """

    name = "find_files"
    description = (
        "Find files by NAME glob, recursively. Pattern '*' lists every file "
        "under a folder. Returns files only, never folders. Use search_text "
        "for text inside files."
    )
    parameters: dict[str, Any] = {
        "type": "object",
        "properties": {
            "path": {
                "type": "string",
                "description": (
                    "Directory or file to search from, relative to the workspace "
                    "root. Use '.' to search the whole workspace."
                ),
            },
            "pattern": {
                "type": "string",
                "description": (
                    "Filename glob, not a regular expression. "
                    "Examples: '*.md', 'haiku.md', 'notes/*.txt'."
                ),
            },
        },
        "required": ["path", "pattern"],
    }
    risk = "observe"

    def __init__(
        self,
        workspace_root: Path,
        max_output_chars: int = 4000,
        max_results: int = DEFAULT_MAX_SEARCH_RESULTS,
    ) -> None:
        """Extend the base tool with a hit limit.

        Args:
            workspace_root: Sandbox root.
            max_output_chars: Per-observation character cap.
            max_results: Stop after this many matching paths.
        """
        super().__init__(workspace_root, max_output_chars)
        self.max_results = max_results

    def _run(self, arguments: dict[str, Any]) -> ToolResult:
        """Walk the target and collect matching file paths.

        The glob is matched against names already inside the workspace: the
        pattern is never joined onto the root as a path, so a pattern like
        ``../secret`` cannot escape. Only files are returned; folders stay
        on ``list_files``.
        """
        pattern = self._string_argument(arguments, "pattern")
        if not pattern:
            raise ValueError("pattern must not be empty")
        target = self._resolve(arguments)
        if not target.exists():
            raise FileNotFoundError(target)

        candidates = (
            [target]
            if target.is_file()
            else sorted(p for p in target.rglob("*") if p.is_file())
        )

        hits: list[str] = []
        truncated = False
        for candidate in candidates:
            if len(hits) >= self.max_results:
                truncated = True
                break
            if not candidate.is_file():
                continue
            relative = display_path(self.workspace_root, candidate)
            if _matches_name_glob(relative, candidate.name, pattern):
                hits.append(relative.replace("\\", "/"))
                if len(hits) >= self.max_results:
                    truncated = True
                    break

        if not hits:
            return ToolResult(f"No files matching {pattern!r}.")

        header = f"{len(hits)} file(s) matching {pattern!r}:"
        footer = (
            [f"... stopped at the {self.max_results}-result limit."]
            if truncated
            else []
        )
        return ToolResult("\n".join([header, *hits, *footer]))
