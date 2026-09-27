"""Path confinement — the in-process half of the sandbox.

These are the most security-relevant tests in the suite. Each one encodes a
concrete way an LLM could try to leave the workspace.
"""

from pathlib import Path

import pytest
from harness.tools.paths import PathEscapeError, display_path, resolve_in_workspace


def test_simple_relative_path_resolves(workspace: Path) -> None:
    """An ordinary relative path lands inside the workspace."""
    assert resolve_in_workspace(workspace, "notes.txt") == workspace / "notes.txt"


def test_nested_path_resolves(workspace: Path) -> None:
    """Nested paths are allowed; only escapes are refused."""
    resolved = resolve_in_workspace(workspace, "a/b/c.txt")
    assert resolved == workspace / "a" / "b" / "c.txt"


def test_empty_path_is_the_workspace_root(workspace: Path) -> None:
    """An empty path means the root, which is a legal target for list_files."""
    assert resolve_in_workspace(workspace, "") == workspace.resolve()


def test_dot_path_is_the_workspace_root(workspace: Path) -> None:
    """'.' is what the system prompt tells the model to use for the root."""
    assert resolve_in_workspace(workspace, ".") == workspace.resolve()


def test_parent_traversal_is_refused(workspace: Path) -> None:
    """``..`` cannot climb out of the workspace."""
    with pytest.raises(PathEscapeError):
        resolve_in_workspace(workspace, "../secrets.txt")


def test_deep_parent_traversal_is_refused(workspace: Path) -> None:
    """Repeated ``..`` segments are refused the same way."""
    with pytest.raises(PathEscapeError):
        resolve_in_workspace(workspace, "a/../../../etc/passwd")


def test_absolute_path_is_reinterpreted_not_obeyed(workspace: Path) -> None:
    """An absolute path is treated as workspace-relative, never as host-absolute.

    This is the case plain ``root / relative`` gets wrong: pathlib discards
    the root when the right-hand side is absolute, so without the explicit
    strip in ``resolve_in_workspace`` the model could read /etc/passwd.
    """
    resolved = resolve_in_workspace(workspace, "/etc/passwd")
    assert resolved == workspace / "etc" / "passwd"
    assert Path("/etc/passwd") != resolved


def test_sibling_directory_with_shared_prefix_is_refused(tmp_path: Path) -> None:
    """A sibling whose name merely starts with the root name is still outside.

    ``/tmp/x/workspace-backup`` starts with the string ``/tmp/x/workspace``,
    so a prefix check would wrongly allow it. The component-wise
    ``relative_to`` check does not.
    """
    root = tmp_path / "workspace"
    root.mkdir()
    (tmp_path / "workspace-backup").mkdir()
    with pytest.raises(PathEscapeError):
        resolve_in_workspace(root, "../workspace-backup/leak.txt")


def test_symlink_pointing_outside_is_refused(workspace: Path, tmp_path: Path) -> None:
    """A symlink planted inside the workspace cannot be used as an exit.

    ``Path.resolve`` follows the link before the containment check, so the
    target's real location is what gets validated.
    """
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret.txt").write_text("secret", encoding="utf-8")
    (workspace / "escape").symlink_to(outside)

    with pytest.raises(PathEscapeError):
        resolve_in_workspace(workspace, "escape/secret.txt")


def test_error_message_does_not_leak_the_absolute_path(workspace: Path) -> None:
    """The refusal echoes the requested path, not the resolved host path.

    Observations go straight into the model's context; leaking the host
    layout would teach it to guess absolute paths.
    """
    with pytest.raises(PathEscapeError) as info:
        resolve_in_workspace(workspace, "../secrets.txt")
    assert str(workspace.resolve()) not in str(info.value)


def test_display_path_is_workspace_relative(workspace: Path) -> None:
    """Observations show relative paths so the model never sees /app/... ."""
    assert display_path(workspace, workspace / "a" / "b.txt") == "a/b.txt"


def test_display_path_of_root_is_dot(workspace: Path) -> None:
    """The root renders as '.', never as an empty string."""
    assert display_path(workspace, workspace.resolve()) == "."
