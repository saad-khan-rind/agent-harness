"""The typed extension seams stay present and correctly shaped.

``harness/subagents`` still carries no behaviour — it is the contract Week 3
plugs into. Memory, hooks, MCP and now permissions have implementations, but
the *protocol shapes* are still pinned here so a reshape cannot pass
silently. Nothing imports the protocols at runtime, so without a test they
could be renamed, gutted, or accidentally deleted and every other test would
still pass.

The permission seam's *behaviour* is tested in ``test_permissions.py``; what
stays here is its shape, plus the one assertion that shape alone cannot make
— that the loop actually consults it (ADR-057).

These tests assert the seams exist with the method names the architecture
document promises. They are cheap insurance on the claim that the loop is
extensible, which is the claim the whole design rests on.
"""

from typing import Any, Protocol, get_type_hints

from harness.context import RunContext
from harness.hooks import Hook, NoOpHook
from harness.mcp.protocol import McpClient
from harness.memory.protocol import MemoryStore
from harness.permissions import AllowAllPolicy, PermissionPolicy
from harness.subagents.protocol import SubAgentFactory
from harness.tools.base import Tool


def _methods(protocol: type) -> set[str]:
    """Return the public method names a protocol declares."""
    return {name for name in vars(protocol) if not name.startswith("_")}


# --- the seams exist with the promised shape ------------------------------


def test_memory_seam():  # type: ignore[no-untyped-def]
    """Week 2 long-term memory: vector records that outlive the process.

    This seam has now been reshaped twice, and both reshapes are the point
    of the test. The Week 1 draft was a key-value store
    (``store``/``retrieve``). Week 2 made it file-shaped — ``read_core``,
    ``list_notes``, ``preview`` — because a two-tier *file* design has no
    query to score (ADR-033). Moving the backing store to Qdrant brought
    the query back, so the file verbs are gone and ``search`` returned
    (ADR-065).

    Pinned here for the same reason it was pinned before: nothing imports
    the protocol at runtime, so without this the seam could be gutted and
    every other test would still pass.
    """
    assert issubclass(MemoryStore, Protocol)  # type: ignore[arg-type]
    assert _methods(MemoryStore) >= {
        "root",
        "available",
        "describe",
        "store",
        "upsert_slot",
        "search",
        "count",
        "recent",
        "append_session",
    }


def test_the_memory_seam_kept_no_file_verbs():  # type: ignore[no-untyped-def]
    """The migration replaced the seam rather than growing it.

    A store that still offered ``read(path)`` beside ``search(query)``
    would let half the harness address memory as a filesystem and the other
    half as a collection, which is how two retrieval strategies end up
    disagreeing about what is stored (ADR-065).
    """
    assert not (
        _methods(MemoryStore)
        & {"read", "write", "edit", "read_core", "append_core", "list_notes", "preview"}
    )


def test_memory_seam_offers_no_way_to_delete():  # type: ignore[no-untyped-def]
    """ADR-008 reaches the memory seam too.

    Anthropic's memory tool ships ``delete`` and ``rename``; ours omits
    both, because a rename is a delete of the old path. Asserted on the
    protocol rather than on one implementation, so a future store cannot
    add them without this failing.
    """
    forbidden = {"delete", "remove", "unlink", "rename", "move"}
    assert not (_methods(MemoryStore) & forbidden)


def test_mcp_seam():  # type: ignore[no-untyped-def]
    """Week 2 MCP: connect, discover tools, invoke them.

    ``discover_tools`` must return our own ``Tool`` type, because that is
    what makes remote tools register on the same registry the loop already
    uses instead of needing a second dispatcher.
    """
    assert _methods(McpClient) >= {"connect", "discover_tools", "call_tool"}
    hints = get_type_hints(McpClient.discover_tools)
    assert hints["return"] == list[Tool]


def test_subagent_seam():  # type: ignore[no-untyped-def]
    """Week 3 sub-agents: a factory returning another loop, not a new one."""
    from harness.loop import AgentLoop

    assert _methods(SubAgentFactory) >= {"create"}
    assert get_type_hints(SubAgentFactory.create)["return"] is AgentLoop


def test_permission_seam_supports_the_three_outcomes():  # type: ignore[no-untyped-def]
    """The brief (§8.4) requires allow, deny and require-user-confirmation."""
    from typing import get_args

    from harness.permissions import PermissionOutcome

    assert set(get_args(PermissionOutcome)) == {
        "allow",
        "deny",
        "require_user_confirmation",
    }
    assert _methods(PermissionPolicy) >= {"decide"}


def test_the_permission_seam_has_a_call_site(workspace: Any) -> None:
    """The seam is wired, not merely declared.

    This is the test the seam lacked for two weeks. ``PermissionPolicy``
    existed, ``AllowAllPolicy`` existed, and ``decide`` was never called from
    anywhere in ``harness/`` — so §8.4 was unmet and nothing failed. Asserted
    by running a real loop over a real registry with a policy that records
    what it was asked about, because the only thing that makes a seam real is
    that something calls it.
    """
    from harness.context import RunContext
    from harness.loop import AgentLoop
    from harness.tools import build_registry
    from tests.fakes import ScriptedLLM, call, text

    asked: list[str] = []

    class RecordingPolicy:
        """Allow everything, and remember every tool it was asked about."""

        def decide(self, tool: Tool, arguments: dict[str, object]) -> Any:
            """Record the tool name and allow the call."""
            asked.append(tool.name)
            return "allow"

    registry = build_registry(workspace)
    loop = AgentLoop(
        llm=ScriptedLLM([call("list_files", path="."), text("done")]),
        registry=registry,
        policy=RecordingPolicy(),
    )

    assert loop.run(RunContext(task="look")) == "done"
    assert asked == ["list_files"]


def test_the_confirmer_seam_exists() -> None:
    """``require_user_confirmation`` needs a way to reach a user.

    Kept separate from ``PermissionPolicy`` because deciding is pure and
    asking is not: ``AGENTS.md`` bans ``print`` outside ``cli.py``, so the
    loop is handed something that knows how to ask and never learns how it
    does (ADR-057).
    """
    from harness.permissions import Confirmer, DenyingConfirmer

    assert _methods(Confirmer) >= {"confirm"}
    # Fails closed: no way to ask means no.
    assert DenyingConfirmer().confirm(None, {}) is False  # type: ignore[arg-type]


def test_hook_seam_covers_the_whole_lifecycle():  # type: ignore[no-untyped-def]
    """Week 3 observability attaches here; the loop already calls all six."""
    assert _methods(Hook) >= {
        "on_run_start",
        "before_llm",
        "after_llm",
        "before_tool",
        "after_tool",
        "on_run_end",
    }


# --- the default implementations behave ------------------------------------


def test_allow_all_policy_allows(workspace: Any) -> None:
    """The shipped default for ``allow_all`` mode: it must never block (ADR-057)."""
    from harness.tools import build_registry

    policy = AllowAllPolicy()
    registry = build_registry(workspace)
    for name in registry.names():
        assert policy.decide(registry.get(name), {}) == "allow"


def test_noop_hook_is_silent_and_total() -> None:
    """Every callback exists and none raises — the loop calls them unguarded."""
    hook = NoOpHook()
    context = RunContext(task="t")

    hook.on_run_start(context)
    hook.before_llm(context)
    hook.after_llm(context, None)
    hook.before_tool("read_file", {"path": "a"})
    hook.after_tool("read_file", "contents")
    hook.on_run_end(context)


def test_noop_hook_satisfies_the_protocol_structurally() -> None:
    """No inheritance needed — that is the point of using Protocol."""
    hook: Hook = NoOpHook()
    assert hook is not None


def test_print_and_input_stay_in_the_terminal_front_end() -> None:
    """Library code never talks to the terminal (AGENTS.md).

    The rule used to name one file, ``cli.py``. That file was split into
    ``cli.py``, ``console.py`` and ``session.py``, so the rule names the
    three — and is now checked rather than only written down. The trace
    replay module has its own small command-line entry point
    (``python -m harness.observability.replay``) and prints there.
    """
    import ast
    from pathlib import Path

    import harness

    allowed = {"cli.py", "console.py", "session.py", "replay.py"}
    root = Path(harness.__file__).parent
    offenders = []
    for path in sorted(root.rglob("*.py")):
        if path.name in allowed:
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and getattr(node.func, "id", None) in {
                "print",
                "input",
            }:
                offenders.append(f"{path.relative_to(root)}:{node.lineno}")

    assert offenders == [], f"print/input outside the terminal front end: {offenders}"
