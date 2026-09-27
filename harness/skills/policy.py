"""Skill-scoped tool permissions: `allowed-tools` that actually restricts.

A skill's frontmatter may declare ``allowed-tools``. Since Week 2 the harness
has parsed that list, shown it to the model in the skill body, and enforced
nothing — the footer literally said "not enforced this week" (ADR-038). This
module is the enforcement (ADR-082).

**Where the decision happens matters more than what it decides.** There is
exactly one permission gate in this harness: ``AgentLoop._execute`` consults a
:class:`~harness.permissions.PermissionPolicy` before every tool call
(ADR-057). So this is a policy *wrapper*, not a check inside ``skill_read``
and not a branch in the loop. A second enforcement point would be a second
thing to keep correct, and the first one to be forgotten.

**Scope opens on a read and lasts as long as the conversation does.** The
skill body is in the message history until ``/reset`` clears it, and the
model keeps following it for exactly that long — so the restriction has to
track the conversation, not the turn. The first version reset it at the top
of every REPL turn, and that let a user defeat the whole gate by pressing
Enter and asking again: on turn two the model was still following the skill
and the harness believed nothing was active (ADR-082, amendment). Reading a
second skill widens the allow-list to the union: an agent told to follow two
skills needs the tools of both, and intersecting them would leave it able to
do neither.

**A skill names a capability, not a deployment.** MCP tools reach the model
as ``<server>__<name>`` (ADR-042), where ``<server>`` is whatever
``config.yaml`` happened to call it. A skill that declares ``web_search``
therefore also permits ``web__web_search``: the frontmatter is matched
against the bare name as well as the full one. Renaming a server must not
silently revoke every skill that used it, and the shipped ``web-research``
skill did exactly that to itself before this rule existed. The match is
one-directional — it can only admit names the skill already wrote — so
skills still only ever narrow.

**Off by default**, and that is not timidity. One wrong entry in a skill's
frontmatter becomes a tool the model cannot call, and the model is told only
that permission was refused — it cannot see the frontmatter that refused it.
That is an acceptable trade when someone has deliberately turned it on to
demonstrate scoped permissions, and a bad one as a silent default.
"""

from harness.mcp import NAME_SEPARATOR
from harness.permissions import PermissionOutcome, PermissionPolicy
from harness.skills.loader import SkillLibrary
from harness.tools.base import Tool

# Always allowed, whatever a skill declares. `skill_read` is how a skill gets
# read in the first place, so a skill that forgot to list it would make every
# *other* skill unreachable — including the one the model needs next.
_ALWAYS_ALLOWED = frozenset({"skill_read"})


def _permits(allowed: frozenset[str], name: str) -> bool:
    """Whether a declared list covers a registered tool name.

    Exact match first. Failing that, an MCP tool registered as
    ``<server>__<remote>`` is covered when the skill declared ``<remote>``
    — the name the skill author sees in the server's own documentation,
    rather than the one the adapter built from ``config.yaml``.

    Args:
        allowed: The union of what the active skills declared.
        name: The tool name as the registry knows it.

    Returns:
        ``True`` when the skill permits the tool under either spelling.
    """
    if name in allowed:
        return True
    _, separator, remote = name.partition(NAME_SEPARATOR)
    return bool(separator) and remote in allowed


class ActiveSkills:
    """Which skills have been read in the current run.

    Mutable and shared: :class:`~harness.skills.tools.SkillRead` writes to it,
    :class:`SkillScopedPolicy` reads from it. A shared object rather than a
    callback because the alternative — the tool holding a reference to the
    policy — points a tool at the thing that polices it, which is backwards.
    """

    def __init__(self, library: SkillLibrary) -> None:
        """Args:
        library: Where declared tool lists are looked up.
        """
        self._library = library
        self._read: list[str] = []

    def note_read(self, skill: str) -> None:
        """Record that ``skill`` was loaded into the context.

        Args:
            skill: The skill name, as the library knows it.
        """
        if skill not in self._read:
            self._read.append(skill)

    def reset(self) -> None:
        """Forget what was read.

        Called when the conversation is cleared (``/reset``), because that
        is when the skill body leaves the context and the model stops
        following it. Not at the start of each turn: the body is still in
        the history then, and so is the model's intent to follow it.
        """
        self._read.clear()

    @property
    def names(self) -> tuple[str, ...]:
        """The skills read so far, in the order they were read."""
        return tuple(self._read)

    @property
    def allowed_tools(self) -> frozenset[str] | None:
        """The union of declared tool names, or ``None`` when unrestricted.

        ``None`` — rather than an empty set — for two distinct situations
        that must both mean "do not restrict": no skill has been read yet,
        and the skills that were read declare nothing. An empty set would
        mean "no tool is permitted", which would strand the agent the moment
        it opened a skill whose author left the field out.
        """
        declared: set[str] = set()
        found = False
        for name in self._read:
            skill = self._library.get(name)
            if skill is not None and skill.allowed_tools:
                declared.update(skill.allowed_tools)
                found = True
        if not found:
            return None
        return frozenset(declared | _ALWAYS_ALLOWED)


class SkillScopedPolicy:
    """Wrap a policy so a skill's ``allowed-tools`` bounds the tool set.

    Composition rather than a new policy class: the wrapped policy still
    decides everything it decided before — risk levels, deny-lists, the
    confirmation tier — and this only ever *narrows* the result. A wrapper
    that could widen one would be a way to escalate past
    :class:`~harness.permissions.RiskBasedPolicy`, which is the opposite of
    what a skill should be able to do.
    """

    def __init__(self, inner: PermissionPolicy, active: ActiveSkills) -> None:
        """Args:
        inner: The policy that decides when the skill permits the tool.
        active: The skills read so far in this run.
        """
        self._inner = inner
        self._active = active

    def decide(self, tool: Tool, arguments: dict[str, object]) -> PermissionOutcome:
        """Deny anything outside the active skill's list, else defer.

        Args:
            tool: The tool the model asked for.
            arguments: Its arguments, passed through untouched.

        Returns:
            ``deny`` when a skill is active and did not declare this tool;
            otherwise whatever the wrapped policy decided.
        """
        allowed = self._active.allowed_tools
        if allowed is not None and not _permits(allowed, tool.name):
            return "deny"
        return self._inner.decide(tool, arguments)
