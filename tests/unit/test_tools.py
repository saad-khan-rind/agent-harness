"""Behaviour of the built-in file tools.

Tools are exercised through ``Tool.execute`` — the public entry point the
registry calls — rather than through ``_run``, so the shared error handling
in the base class is under test too.
"""

from pathlib import Path

from harness.tools import ToolRegistry, build_registry
from harness.tools.mutate import MAX_WRITE_BYTES

# --- create_folder --------------------------------------------------------


def test_create_folder_refuses_to_clobber_a_file(
    registry: ToolRegistry, workspace: Path
) -> None:
    """A folder cannot replace an existing file of the same name."""
    (workspace / "notes.txt").write_text("hello", encoding="utf-8")
    result = registry.execute("create_folder", {"path": "notes.txt"})
    assert result.startswith("Error:")
    assert (workspace / "notes.txt").read_text(encoding="utf-8") == "hello"


def test_create_folder_outside_workspace_is_refused(
    registry: ToolRegistry, tmp_path: Path
) -> None:
    """Path confinement is enforced through the tool, not just the helper."""
    result = registry.execute("create_folder", {"path": "../escaped"})
    assert result.startswith("Error:")
    assert not (tmp_path / "escaped").exists()


# --- write_file -----------------------------------------------------------


def test_write_file_creates_file_with_exact_content(
    registry: ToolRegistry, workspace: Path
) -> None:
    """Content is written byte-exact, which the brief's E2E asserts rely on."""
    registry.execute("write_file", {"path": "a.md", "content": "one\ntwenty\n"})
    assert (workspace / "a.md").read_bytes() == b"one\ntwenty\n"


def test_write_file_rejects_oversized_content(registry: ToolRegistry) -> None:
    """The write cap bounds how much disk a runaway model can consume."""
    result = registry.execute(
        "write_file", {"path": "big.txt", "content": "x" * (MAX_WRITE_BYTES + 1)}
    )
    assert result.startswith("Error:")


# --- read_file ------------------------------------------------------------


def test_read_file_returns_content_and_line_count(
    registry: ToolRegistry, workspace: Path
) -> None:
    """The header line count is what lets the model answer 'how many lines?'.

    Uses the exact fixture from the brief's ``find_and_inspect`` case.
    """
    (workspace / "notes.txt").write_text("This\nfile\nhas\none\nline", encoding="utf-8")
    result = registry.execute("read_file", {"path": "notes.txt"})
    assert "notes.txt (5 lines)" in result
    assert "This\nfile\nhas\none\nline" in result


def test_read_file_missing_is_reported(registry: ToolRegistry) -> None:
    """A missing file is a recoverable observation."""
    result = registry.execute("read_file", {"path": "nope.txt"})
    assert result.startswith("Error:")
    assert "No such file" in result


def test_read_file_rejects_binary(registry: ToolRegistry, workspace: Path) -> None:
    """Invalid UTF-8 is reported rather than silently mangled."""
    (workspace / "blob.bin").write_bytes(b"\xff\xfe\x00\x01")
    result = registry.execute("read_file", {"path": "blob.bin"})
    assert result.startswith("Error:")
    assert "UTF-8" in result


# --- list_files -----------------------------------------------------------


def test_list_files_marks_directories(registry: ToolRegistry, workspace: Path) -> None:
    """Trailing slashes let the model tell folders from files in one call."""
    (workspace / "data").mkdir()
    (workspace / "readme.md").write_text("x", encoding="utf-8")
    result = registry.execute("list_files", {"path": "."})
    assert "data/" in result
    assert "readme.md" in result


# --- search_text ----------------------------------------------------------


def test_search_text_finds_matches_with_line_numbers(
    registry: ToolRegistry, workspace: Path
) -> None:
    """Hits carry file and line so the model can go straight to read_file."""
    (workspace / "a.txt").write_text("alpha\nbeta\ngamma", encoding="utf-8")
    result = registry.execute("search_text", {"query": "beta", "path": "."})
    assert "a.txt:2" in result


# --- find_files -----------------------------------------------------------


def test_find_files_matches_nested_glob(
    registry: ToolRegistry, workspace: Path
) -> None:
    """``*.md`` recurses; ``list_files`` would only see the starting folder."""
    (workspace / "notes").mkdir()
    (workspace / "notes" / "haiku.md").write_text("x", encoding="utf-8")
    (workspace / "skip.txt").write_text("x", encoding="utf-8")
    result = registry.execute("find_files", {"path": ".", "pattern": "*.md"})
    assert "notes/haiku.md" in result
    assert "skip.txt" not in result


def test_find_files_outside_workspace_is_refused(
    registry: ToolRegistry, tmp_path: Path
) -> None:
    """The glob cannot be used to walk out of the sandbox."""
    (tmp_path / "secret.txt").write_text("x", encoding="utf-8")
    result = registry.execute("find_files", {"path": "..", "pattern": "*.txt"})
    assert result.startswith("Error:")
    assert "escapes the workspace" in result
    assert "secret.txt" not in result


def test_find_files_pattern_does_not_escape(
    registry: ToolRegistry, tmp_path: Path, workspace: Path
) -> None:
    """The pattern is not resolved as a path, so ``../`` in it cannot leak."""
    (tmp_path / "outside.md").write_text("x", encoding="utf-8")
    (workspace / "inside.md").write_text("x", encoding="utf-8")
    result = registry.execute("find_files", {"path": ".", "pattern": "../outside.md"})
    assert result.startswith("No files matching")
    assert "file(s) matching" not in result


# --- append_file ----------------------------------------------------------


def test_append_file_keeps_the_existing_content(
    registry: ToolRegistry, workspace: Path
) -> None:
    """The whole point: what was there is still there, byte for byte."""
    (workspace / "log.md").write_text("first\n", encoding="utf-8")

    result = registry.execute("append_file", {"path": "log.md", "content": "second\n"})

    assert (workspace / "log.md").read_text(encoding="utf-8") == "first\nsecond\n"
    assert "Appended to log.md" in result


def test_append_file_caps_the_resulting_size(
    registry: ToolRegistry, workspace: Path
) -> None:
    """The limit is on the file, not the addition.

    Checking only the addition would turn a bounded write into an unbounded
    one: a thousand small appends are individually legal and collectively a
    runaway.
    """
    (workspace / "big.txt").write_text("x" * (MAX_WRITE_BYTES - 1), encoding="utf-8")

    result = registry.execute("append_file", {"path": "big.txt", "content": "yy"})

    assert result.startswith("Error:")
    assert "would reach" in result
    # Nothing was written: the file is exactly as it was.
    assert len((workspace / "big.txt").read_bytes()) == MAX_WRITE_BYTES - 1


def test_append_file_cannot_escape_the_workspace(registry: ToolRegistry) -> None:
    """Containment is the base class's job, and it applies here too."""
    result = registry.execute("append_file", {"path": "../escape.md", "content": "x"})

    assert result.startswith("Error:")
    assert "escapes the workspace" in result


# --- edit_file ------------------------------------------------------------


def test_edit_file_replaces_a_unique_match(
    registry: ToolRegistry, workspace: Path
) -> None:
    """The happy path: one occurrence, one replacement."""
    (workspace / "a.txt").write_text("hello world", encoding="utf-8")
    registry.execute(
        "edit_file", {"path": "a.txt", "old_str": "world", "new_str": "there"}
    )
    assert (workspace / "a.txt").read_text(encoding="utf-8") == "hello there"


def test_edit_file_refuses_ambiguous_match(
    registry: ToolRegistry, workspace: Path
) -> None:
    """Several matches means any choice is a guess, so the tool refuses.

    The count is in the message so the model can widen old_str and retry.
    """
    (workspace / "a.txt").write_text("x\nx\nx", encoding="utf-8")
    result = registry.execute(
        "edit_file", {"path": "a.txt", "old_str": "x", "new_str": "y"}
    )
    assert result.startswith("Error:")
    assert "3 times" in result
    assert (workspace / "a.txt").read_text(encoding="utf-8") == "x\nx\nx"


# --- no delete ------------------------------------------------------------


def test_no_delete_tool_is_registered(registry: ToolRegistry) -> None:
    """The brief's hard constraint: the agent CANNOT delete.

    Asserted on the registry rather than on a class list, because the
    registry is what the model actually sees.
    """
    names = registry.names()
    assert names == [
        "list_files",
        "read_file",
        "search_text",
        "find_files",
        "create_folder",
        "write_file",
        "append_file",
        "edit_file",
    ]
    forbidden = ("delete", "remove", "rm", "unlink", "rmdir")
    assert not [n for n in names if any(word in n for word in forbidden)]


def test_tools_are_split_into_observe_and_mutate(registry: ToolRegistry) -> None:
    """The risk split exists and is correct; ``RiskBasedPolicy`` keys off it."""
    risks = {name: registry.get(name).risk for name in registry.names()}
    assert risks == {
        "list_files": "observe",
        "read_file": "observe",
        "search_text": "observe",
        "find_files": "observe",
        "create_folder": "mutate",
        "write_file": "mutate",
        "append_file": "mutate",
        "edit_file": "mutate",
    }


# --- shared base behaviour ------------------------------------------------


def test_tool_output_is_truncated_to_the_budget(workspace: Path) -> None:
    """The per-observation cap is enforced through the real tool path."""
    (workspace / "a.txt").write_text("y" * 500, encoding="utf-8")
    tight = build_registry(workspace, max_output_chars=50)
    assert "truncated" in tight.execute("read_file", {"path": "a.txt"})
