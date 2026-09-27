"""Confine file-tool paths to the configured workspace root.

This module is the **in-process half** of the sandbox required by the lab
brief (Section 8.2: "enforcing basic security measures"). The other half is
the container (Section 7.3). Both are needed:

* The container stops the *process* from touching the host file system.
* This module stops the *LLM* from talking the harness into reading
  ``../../etc/passwd`` or writing outside the folder the user opened.

Every built-in file tool routes its ``path`` argument through
:func:`resolve_in_workspace`. A tool that skips it is a security bug, so the
helper lives in one module rather than being copy-pasted per tool.
"""

from pathlib import Path


class PathEscapeError(ValueError):
    """Raised when a resolved path is outside the workspace.

    Subclasses ``ValueError`` so callers that only care about "bad argument"
    can catch it generically, while the tool base class can still catch it
    specifically and turn it into a clear observation for the LLM.
    """


def resolve_in_workspace(
    workspace_root: Path,
    relative: str,
    root_label: str = "workspace",
) -> Path:
    """Resolve ``relative`` against a sandbox root and reject escapes.

    The check is deliberately *not* a string prefix comparison. Two separate
    attacks defeat naive prefix matching:

    1. ``..`` traversal — ``workspace/../../secret`` textually starts with
       ``workspace`` but resolves elsewhere.
    2. Sibling-name confusion — ``/app/workspace-backup`` starts with the
       string ``/app/workspace`` but is a different directory.

    Calling :meth:`Path.resolve` first normalises ``..`` segments *and*
    follows symlinks (so a symlink planted inside the workspace that points
    at ``/etc`` is caught too), and :meth:`Path.relative_to` then does a
    component-wise containment check rather than a substring one.

    The root is a parameter, and that is what makes this the harness's *one*
    containment primitive rather than a workspace-specific helper. Week 2
    added two more sandboxes — long-term memory and the read-only skills
    directory — and both are confined by calling this function with a
    different root, so there is exactly one place to audit and exactly one
    place a bug could hide (ADR-035).

    Args:
        workspace_root: Sandbox root. Resolved here so callers may pass a
            relative or symlinked root without weakening the check.
        relative: User- or model-supplied path. May be relative or absolute;
            an absolute path is still joined onto the root, so
            ``/etc/passwd`` becomes ``<root>/etc/passwd`` rather than the
            real system file.
        root_label: What to call the root in the refusal message. The model
            reads that message and retries against it, so telling it "the
            workspace" when it was reaching outside the *memory* directory
            would send it looking in the wrong place.

    Returns:
        Absolute, symlink-free path guaranteed to live inside
        ``workspace_root``.

    Raises:
        PathEscapeError: If the path would leave the root.
    """
    root = workspace_root.resolve()

    # ``lstrip`` on the separators makes an absolute model-supplied path
    # behave as workspace-relative instead of overriding the root: with
    # pathlib, ``root / "/etc/passwd"`` would otherwise discard ``root``
    # entirely and yield ``/etc/passwd``.
    cleaned = relative.strip()
    if cleaned.startswith(("/", "\\")):
        cleaned = cleaned.lstrip("/\\")

    # An empty or "." path means "the workspace itself", which is legal.
    resolved = (root / cleaned).resolve() if cleaned else root

    try:
        resolved.relative_to(root)
    except ValueError as exc:
        # Report the path the caller supplied, not the resolved one: echoing
        # the absolute host path back to the model leaks layout information
        # the sandbox is meant to hide.
        raise PathEscapeError(
            f"Path escapes the {root_label} and was refused: {relative!r}"
        ) from exc
    return resolved


def display_path(workspace_root: Path, absolute: Path) -> str:
    """Render ``absolute`` the way the model should see it.

    Tool observations feed straight back into the LLM context, so they must
    speak in workspace-relative terms. Leaking ``/app/workspace/...`` would
    both waste tokens and teach the model to start guessing absolute host
    paths, which the resolver would then have to reject.

    Args:
        workspace_root: Sandbox root.
        absolute: A path already validated by :func:`resolve_in_workspace`.

    Returns:
        Workspace-relative path string, or ``"."`` for the root itself.
    """
    relative = absolute.relative_to(workspace_root.resolve())
    # ``Path.relative_to`` returns ``.`` as an empty-looking path; make that
    # explicit so observations never contain a bare empty string.
    return str(relative) if str(relative) != "." else "."
