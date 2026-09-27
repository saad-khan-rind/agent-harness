"""Workspace-mutating file tools: create, write, append and edit.

These cover "the agent can create folders and files" and "read and modify
files" from the Week 1 brief (Section 8.2). Every tool here is
``risk="mutate"``.

**There is no delete tool, not even as a stub.** The brief is explicit that
the agent CANNOT delete folders or files. Three properties enforce it:

1. No class implements deletion, so the registry has nothing to expose.
2. ``write_file`` refuses to overwrite a directory, which is the closest
   thing to an accidental recursive removal available here.
3. ``edit_file`` replaces text in place; it cannot unlink a file.
4. ``append_file`` only ever adds to the end of one, and is the only tool
   here that cannot destroy existing content at all.

Truncation to zero bytes via ``write_file`` with empty content is still
possible and is *not* deletion — the path survives. It is called out in
``decisions.md`` (ADR-009) so the trade-off is on the record rather than
discovered during the oral exam.
"""

from typing import Any

from harness.tools.base import Tool, ToolResult
from harness.tools.paths import display_path

_PATH_PROP = {
    "type": "string",
    "description": "Path relative to the workspace root.",
}

# Guard against a runaway model writing a gigabyte into the sandbox. Large
# enough for any realistic lab task, small enough to bound disk use.
MAX_WRITE_BYTES = 1_000_000


class CreateDirectory(Tool):
    """Create a directory inside the workspace.

    Exposed to the model as ``create_folder`` to match the wording of the
    brief's ``numbers_in_words`` E2E case.
    """

    name = "create_folder"
    description = (
        "Create a folder and any missing parents. Safe to call twice. "
        "write_file already creates parents, so this is rarely needed first."
    )
    parameters: dict[str, Any] = {
        "type": "object",
        "properties": {"path": _PATH_PROP},
        "required": ["path"],
    }
    risk = "mutate"

    def _run(self, arguments: dict[str, Any]) -> ToolResult:
        """Create the folder, reporting whether it was already there.

        ``mkdir`` is idempotent here (``exist_ok=True``) because a model that
        re-creates a folder it already made has not done anything wrong, and
        a hard error would push it into a pointless recovery detour. The
        observation still distinguishes the two cases so the model does not
        conclude it just created something that predates the run.
        """
        target = self._resolve(arguments)
        shown = display_path(self.workspace_root, target)

        if target.is_file():
            # Refuse rather than clobber: mkdir would fail anyway, but this
            # message tells the model *why* in its own terms.
            return ToolResult(
                f"Cannot create folder {shown}: a file with that name already exists.",
                ok=False,
            )
        if target.is_dir():
            return ToolResult(f"Folder {shown}/ already exists.")

        target.mkdir(parents=True, exist_ok=True)
        return ToolResult(f"Created folder {shown}/.")


class WriteFile(Tool):
    """Create or overwrite a text file inside the workspace."""

    name = "write_file"
    description = (
        "Write the WHOLE file, creating parents. Replaces existing content. "
        "Use append_file to add to the end, or edit_file for a small change."
    )
    parameters: dict[str, Any] = {
        "type": "object",
        "properties": {
            "path": _PATH_PROP,
            "content": {
                "type": "string",
                "description": "Full file contents to write.",
            },
        },
        "required": ["path", "content"],
    }
    risk = "mutate"

    def _run(self, arguments: dict[str, Any]) -> ToolResult:
        """Write the file whole, creating parent folders on the way.

        Auto-creating parents saves a turn: without it the model must call
        ``create_folder`` first and will sometimes forget, burning an
        iteration on an error it could have avoided. The parents still land
        inside the workspace because the path was resolved first.

        The description tells the model this replaces the whole file. That
        matters — a model that treats ``write_file`` as "append" silently
        destroys work, and the harness cannot tell the difference.
        """
        target = self._resolve(arguments)
        content = self._string_argument(arguments, "content")
        shown = display_path(self.workspace_root, target)

        if target.is_dir():
            raise IsADirectoryError(target)

        encoded = content.encode("utf-8")
        if len(encoded) > MAX_WRITE_BYTES:
            return ToolResult(
                f"Refusing to write {len(encoded)} bytes to {shown}: the limit is "
                f"{MAX_WRITE_BYTES} bytes.",
                ok=False,
            )

        existed = target.is_file()
        target.parent.mkdir(parents=True, exist_ok=True)
        # newline="" keeps the content byte-exact: without it Python would
        # translate "\n" to the platform line ending, and the brief's E2E
        # cases assert on byte-exact text.
        target.write_text(content, encoding="utf-8", newline="")

        verb = "Overwrote" if existed else "Created"
        line_count = len(content.splitlines())
        return ToolResult(f"{verb} {shown} ({len(encoded)} bytes, {line_count} lines).")


class AppendFile(Tool):
    """Add text to the end of a workspace file, creating it if absent.

    The one tool here that cannot destroy anything. It never reads the
    existing content into the model's context and never rewrites it, so the
    bytes already on disk are untouchable by construction.

    Why it exists, when ``write_file`` and ``edit_file`` were already there.
    Adding a line to a file that grows — a journal, a log, a running set of
    meeting notes, a list being collected across several turns — had only
    one route: ``read_file``, then ``write_file`` with the old content plus
    the new. That route is wrong in three separate ways, and the third is
    the serious one.

    * It costs the whole file twice per addition, once as an observation and
      once as tool-call arguments, which is the largest avoidable use of the
      context window in a long task.
    * It puts the file through the model, so a note comes back subtly
      reworded — an "append" that quietly edits history.
    * **It can silently lose data.** ``read_file`` refuses files over
      ``MAX_READABLE_BYTES``, and every observation is truncated to
      ``max_output_chars`` (4000 by default) before the model sees it. A
      model that then "rewrites the file with the new line added" writes
      back what it *saw*, and the rest is gone. Nothing in the harness can
      tell that apart from an intentional rewrite. A file that grows past
      the observation budget is not exotic; it is what a journal does after
      a few dozen entries.

    So this is a safety tool wearing a convenience tool's clothes
    (ADR-050).
    """

    name = "append_file"
    description = (
        "Add text to the END of a file, creating it if missing. Keeps "
        "existing content untouched and does not read it back, so use this "
        "for logs, journals and any file you are adding to over time. "
        "A newline is inserted first if the file does not end with one."
    )
    parameters: dict[str, Any] = {
        "type": "object",
        "properties": {
            "path": _PATH_PROP,
            "content": {
                "type": "string",
                "description": "Text to add at the end of the file.",
            },
        },
        "required": ["path", "content"],
    }
    risk = "mutate"

    def _run(self, arguments: dict[str, Any]) -> ToolResult:
        """Append the text, separating it from what is already there.

        Two decisions are worth stating.

        **The separator is added for you.** If the file does not end in a
        newline, one is inserted before the new text. Without it the
        commonest use — adding a line to a list — silently joins the new
        line onto the end of the old one, and the model has no way to know
        it happened because it never sees the file. Making the model
        remember a leading ``\\n`` every time is a rule it will get right
        four times out of five, which is the worst of both worlds.

        **The size cap is checked against the result, not the addition.**
        Otherwise a bounded write becomes an unbounded one: a thousand
        appends of a kilobyte each are individually fine and collectively a
        runaway. Refusing names the resulting size, so the model is told
        what to do about it rather than left to guess.
        """
        target = self._resolve(arguments)
        content = self._string_argument(arguments, "content")
        shown = display_path(self.workspace_root, target)

        if target.is_dir():
            raise IsADirectoryError(target)
        if not content:
            # Not an error, but worth saying: a model appending nothing has
            # usually put its text in the wrong argument, and silence here
            # would look like success.
            return ToolResult(f"Nothing to append to {shown}: content was empty.")

        existing = target.read_bytes() if target.is_file() else b""
        separator = b"\n" if existing and not existing.endswith(b"\n") else b""
        addition = separator + content.encode("utf-8")

        total = len(existing) + len(addition)
        if total > MAX_WRITE_BYTES:
            return ToolResult(
                f"Refusing to append to {shown}: the file would reach {total} bytes "
                f"and the limit is {MAX_WRITE_BYTES}. Start a new file instead.",
                ok=False,
            )

        created = not target.is_file()
        target.parent.mkdir(parents=True, exist_ok=True)
        with target.open("ab") as handle:
            handle.write(addition)

        verb = "Created" if created else "Appended to"
        added_lines = len(content.splitlines())
        return ToolResult(
            f"{verb} {shown} (+{len(addition)} bytes, +{added_lines} lines, "
            f"{total} bytes total)."
        )


class EditFile(Tool):
    """Replace a unique substring in an existing workspace file."""

    name = "edit_file"
    description = (
        "Replace one exact substring. old_str must appear once; read_file "
        "first and copy it byte for byte. If several matches, include more "
        "surrounding lines. Use write_file for a full rewrite."
    )
    parameters: dict[str, Any] = {
        "type": "object",
        "properties": {
            "path": _PATH_PROP,
            "old_str": {
                "type": "string",
                "description": (
                    "Exact text to find. Must appear exactly once in the file."
                ),
            },
            "new_str": {
                "type": "string",
                "description": (
                    "Replacement text. Use an empty string to remove old_str."
                ),
            },
        },
        "required": ["path", "old_str", "new_str"],
    }
    risk = "mutate"

    def _run(self, arguments: dict[str, Any]) -> ToolResult:
        """Replace ``old_str`` with ``new_str``, but only on a unique match.

        The uniqueness rule is the whole safety story of this tool. If a
        model asks to replace ``"count"`` in a file with twelve occurrences,
        *any* choice the harness makes is a guess: replacing all twelve is
        usually wrong, replacing the first is arbitrary. Failing with the
        match count instead turns an ambiguous edit into a specific,
        recoverable instruction — the model widens ``old_str`` and retries.

        Ambiguity is reported through the error path rather than resolved,
        because a wrong edit is invisible to the user until much later.
        """
        target = self._resolve(arguments)
        old_str = self._string_argument(arguments, "old_str")
        new_str = self._string_argument(arguments, "new_str")
        shown = display_path(self.workspace_root, target)

        if not target.exists():
            raise FileNotFoundError(target)
        if target.is_dir():
            raise IsADirectoryError(target)
        if not old_str:
            # An empty needle matches everywhere; ``str.replace`` would
            # interleave new_str between every character.
            raise ValueError("old_str must not be empty")

        content = target.read_text(encoding="utf-8")
        occurrences = content.count(old_str)
        if occurrences == 0:
            raise ValueError(
                f"old_str was not found in {shown}. Read the file first and copy the "
                f"exact text, including whitespace."
            )
        if occurrences > 1:
            raise ValueError(
                f"old_str appears {occurrences} times in {shown} and must appear once. "
                f"Include more surrounding text to make it unique."
            )
        if old_str == new_str:
            # Not an error, but worth saying: an identical replacement means
            # the model has misdiagnosed the problem and is about to loop.
            return ToolResult(f"No change made to {shown}: old_str equals new_str.")

        target.write_text(
            content.replace(old_str, new_str, 1), encoding="utf-8", newline=""
        )
        return ToolResult(f"Edited {shown}: replaced 1 occurrence.")
