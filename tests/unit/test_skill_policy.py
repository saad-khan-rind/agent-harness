"""``allowed-tools`` that actually restricts (ADR-082).

Week 2 parsed the field, printed it, and enforced nothing — deliberately,
because a half-working gate is worse than an honest note (ADR-038). This is
the other half: an opt-in policy wrapper that holds the model to what the
skill it loaded said it needed.

Three properties carry the whole design, and each has a test that fails for
a different reason:

* the restriction **only ever narrows** — a skill can take a tool away and
  can never grant one the base policy refused;
* it is **off unless asked**, so the shipped default is bit-for-bit Week 2;
* it **opens on read, and only on a successful read**, so a skill that could
  not be loaded cannot silently strand the agent;
* it **matches MCP tools by the name the skill author knows**, so a skill
  that declares ``web_search`` covers the ``web__web_search`` the adapter
  registered, and renaming the server in ``config.yaml`` cannot silently
  revoke it (ADR-042).
"""

from pathlib import Path

from harness.mcp import wrap_remote_tools
from harness.permissions import PermissionOutcome, RiskBasedPolicy
from harness.skills import ActiveSkills, SkillScopedPolicy, load_skills
from harness.skills.loader import SkillLibrary
from harness.tools.base import Tool
from harness.tools.registry import ToolRegistry
from harness.toolset import build_agent_registry
from tests.fakes import remote_tool_definition


def write_skill(
    root: Path,
    name: str,
    allowed: tuple[str, ...] = (),
    body: str = "Body.",
) -> None:
    """Create a skill folder, optionally declaring ``allowed-tools``.

    Args:
        root: The skills root.
        name: Skill name; also the folder name, which the loader checks.
        allowed: Tool names for the ``allowed-tools`` list. Empty writes no
            field at all, which is the "declares nothing" case.
        body: Instructions body.
    """
    directory = root / name
    directory.mkdir(parents=True)
    field = ""
    if allowed:
        field = "allowed-tools:\n" + "".join(f"  - {tool}\n" for tool in allowed)
    (directory / "SKILL.md").write_text(
        f"---\nname: {name}\ndescription: A skill.\n{field}---\n{body}\n",
        encoding="utf-8",
    )


def scoped(
    root: Path,
    inner: PermissionOutcome = "allow",
) -> tuple[ToolRegistry, SkillScopedPolicy, ActiveSkills, SkillLibrary]:
    """Build the enforced wiring exactly as the CLI does.

    Going through :func:`~harness.toolset.build_agent_registry` rather than
    constructing ``SkillRead`` by hand is the point: the thing under test is
    a wiring decision, and a test that wires it itself would pass even if
    the CLI wired it wrongly.

    Args:
        root: A skills root that already contains skill folders.
        inner: What the wrapped policy does with ``mutate`` tools.

    Returns:
        The registry, the policy, the active-skill set and the library.
    """
    library = load_skills(root)
    active = ActiveSkills(library)
    policy = SkillScopedPolicy(RiskBasedPolicy(inner), active)
    registry = build_agent_registry(
        root.parent / "workspace",
        skill_library=library,
        active_skills=active,
    )
    return registry, policy, active, library


def decide(policy: SkillScopedPolicy, registry: ToolRegistry, name: str) -> str:
    """Ask the policy about one registered tool by name."""
    tool: Tool = registry.get(name)
    return policy.decide(tool, {})


def add_remote(registry: ToolRegistry, server: str, *remote_names: str) -> None:
    """Register MCP tools exactly as the adapter would, prefixed by server.

    Through :func:`~harness.mcp.wrap_remote_tools` rather than a hand-made
    ``Tool`` with ``__`` in its name, so the name under test is the one the
    real adapter builds — if the separator ever changed, this would change
    with it.

    Args:
        registry: Where the wrapped tools go.
        server: The configured server label; becomes the prefix.
        remote_names: The tools the server advertises, unprefixed.
    """
    for tool in wrap_remote_tools(
        server, [remote_tool_definition(n) for n in remote_names], lambda *_: "ok"
    ):
        registry.register(tool)


# --- the default: nothing changes ------------------------------------------


def test_nothing_is_restricted_before_a_skill_is_read(tmp_path: Path) -> None:
    """An agent that has opened no skill is an ordinary agent.

    The restriction is scoped to "a skill said so"; with no skill read there
    is no such claim, and inventing one would break every task that does not
    use skills at all.
    """
    write_skill(tmp_path / "skills", "research", allowed=("read_file",))
    registry, policy, _, _ = scoped(tmp_path / "skills")

    assert decide(policy, registry, "write_file") == "allow"


# --- the restriction ---------------------------------------------------------


def test_a_tool_the_skill_did_not_declare_is_denied(tmp_path: Path) -> None:
    """The whole point: reading the skill closes the door on everything else."""
    write_skill(tmp_path / "skills", "research", allowed=("read_file",))
    registry, policy, _, _ = scoped(tmp_path / "skills")

    registry.execute("skill_read", {"skill": "research"})

    assert decide(policy, registry, "write_file") == "deny"


def test_a_tool_the_skill_declared_is_still_allowed(tmp_path: Path) -> None:
    """Narrowing must not become blocking; the declared tools keep working."""
    write_skill(tmp_path / "skills", "research", allowed=("read_file",))
    registry, policy, _, _ = scoped(tmp_path / "skills")

    registry.execute("skill_read", {"skill": "research"})

    assert decide(policy, registry, "read_file") == "allow"


def test_two_skills_give_the_union_not_the_intersection(tmp_path: Path) -> None:
    """An agent following two skills needs the tools of both.

    Intersecting would leave it able to do neither, which is the opposite of
    what following two sets of instructions means.
    """
    write_skill(tmp_path / "skills", "research", allowed=("read_file",))
    write_skill(tmp_path / "skills", "report", allowed=("write_file",))
    registry, policy, active, _ = scoped(tmp_path / "skills")

    registry.execute("skill_read", {"skill": "research"})
    registry.execute("skill_read", {"skill": "report"})

    assert active.allowed_tools is not None
    assert {"read_file", "write_file"} <= active.allowed_tools
    assert decide(policy, registry, "read_file") == "allow"
    assert decide(policy, registry, "write_file") == "allow"


# --- it narrows, it never widens --------------------------------------------


def test_a_skill_cannot_grant_a_tool_the_base_policy_refuses(
    tmp_path: Path,
) -> None:
    """The wrapper is a floor, not an override.

    If a skill's frontmatter could re-open a tool ``RiskBasedPolicy`` said
    no to, then editing a markdown file would be a privilege escalation —
    and skills are the least trusted input in the harness.
    """
    write_skill(tmp_path / "skills", "report", allowed=("write_file",))
    registry, policy, _, _ = scoped(tmp_path / "skills", inner="deny")

    registry.execute("skill_read", {"skill": "report"})

    assert decide(policy, registry, "write_file") == "deny"


def test_the_confirmation_tier_survives_the_wrapper(tmp_path: Path) -> None:
    """A declared mutating tool still asks, rather than being waved through.

    Narrowing must not accidentally *flatten* the three tiers into
    allow/deny; ``require_user_confirmation`` is the mode the demo runs in.
    """
    write_skill(tmp_path / "skills", "report", allowed=("write_file",))
    registry, policy, _, _ = scoped(
        tmp_path / "skills", inner="require_user_confirmation"
    )

    registry.execute("skill_read", {"skill": "report"})

    assert decide(policy, registry, "write_file") == "require_user_confirmation"


# --- MCP names ---------------------------------------------------------------


def test_a_declared_bare_name_covers_the_prefixed_mcp_tool(tmp_path: Path) -> None:
    """The shipped bug: ``web-research`` declared ``web_search``, the registry
    had ``web__web_search``, and the skill forbade the two tools it exists to
    use. A skill names a capability; the prefix is a deployment detail from
    ``config.yaml`` that the skill author cannot see and should not have to.
    """
    write_skill(tmp_path / "skills", "research", allowed=("web_search",))
    registry, policy, _, _ = scoped(tmp_path / "skills")
    add_remote(registry, "web", "web_search", "web_fetch")

    registry.execute("skill_read", {"skill": "research"})

    assert decide(policy, registry, "web__web_search") == "allow"
    assert decide(policy, registry, "web__web_fetch") == "deny"
    assert decide(policy, registry, "write_file") == "deny"


# --- lifecycle ---------------------------------------------------------------


def test_reset_reopens_the_tool_set(tmp_path: Path) -> None:
    """A skill scopes the conversation that read it, and ``/reset`` ends both.

    The skill body leaves the context on ``/reset`` and the model stops
    following it, so that is when the restriction goes too. It is the only
    way out on purpose: the first version reset per turn, and a user could
    lift the gate by asking again (ADR-082, amendment).
    """
    write_skill(tmp_path / "skills", "research", allowed=("read_file",))
    registry, policy, active, _ = scoped(tmp_path / "skills")
    registry.execute("skill_read", {"skill": "research"})
    assert decide(policy, registry, "write_file") == "deny"

    active.reset()

    assert active.names == ()
    assert decide(policy, registry, "write_file") == "allow"


# --- what the model is told ---------------------------------------------------


def test_the_footer_says_the_list_is_binding_when_it_is(tmp_path: Path) -> None:
    """The model has to know which world it is in.

    Being told "you may use only these" when nothing is enforced makes a
    model refuse work it was allowed to do; the reverse makes it plan work
    that will be refused. Both are avoidable by saying which is true.
    """
    write_skill(tmp_path / "skills", "research", allowed=("read_file",))
    registry, _, _, _ = scoped(tmp_path / "skills")

    result = registry.execute("skill_read", {"skill": "research"})

    assert "only these tools" in result
    assert "not enforced" not in result


# --- the tracker on its own ---------------------------------------------------
