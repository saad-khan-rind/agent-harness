"""Permission policy seam, and the two implementations the CLI can select.

This is the brief's §8.4 requirement: permission management applied
consistently across the system rather than as isolated special cases. It is
enforced in exactly one place — :meth:`harness.loop.AgentLoop._execute`, on
every tool call the model makes — so a new tool, a memory tool, a skill tool
and a tool discovered on an MCP server are all policed the same way, without
any of them knowing a policy exists.

Two seams live here, and they are separate on purpose:

* :class:`PermissionPolicy` *decides*. It is pure: it looks at the tool and
  the arguments and returns one of three outcomes. It never prints, never
  blocks, and can be unit-tested without a terminal.
* :class:`Confirmer` *asks*. ``require_user_confirmation`` has to reach a
  human, but ``AGENTS.md`` bans ``print`` outside ``cli.py``, so the loop is
  handed something that knows how to ask and does not care how. The CLI
  injects a terminal implementation; tests inject a scripted one; a future
  non-terminal front end injects its own (ADR-057).

The default when the loop is constructed with neither is deliberate:
:class:`AllowAllPolicy` (nothing changes for a caller that has not opted in)
paired with :class:`DenyingConfirmer` (if a policy *does* ask for
confirmation and nobody wired a way to ask, the answer is no, not yes).

From ADR-102 the policy decides in two steps. First every tool gets a
**tier** — ``allow``, ``ask`` or ``deny`` — from the deny-list, then the
operator's :class:`PermissionRules`, then its server's default, then its
``reach`` and ``risk``. Then the **mode** says what ``ask`` means in this
session. Keeping those apart is what lets ``/tools`` show a reviewer "what
is this tool worth" without also having to say "and what will happen".
"""

from dataclasses import dataclass
from fnmatch import fnmatchcase
from typing import Literal, Protocol

from harness.tools.base import DENIED_TOOL_WORDS, Tool

PermissionOutcome = Literal["allow", "deny", "require_user_confirmation"]

# Selected in ``config.yaml`` or with ``--permissions``. Named modes rather
# than a free-form policy path: the set of sensible policies is small, and a
# mode is something a user can be shown in the banner and reason about.
PermissionMode = Literal["allow_all", "confirm_mutations", "deny_mutations"]

# What ``config.yaml`` may say. ``auto`` is not a mode the policy knows: the
# CLI resolves it to ``confirm_mutations`` on a terminal and ``allow_all``
# anywhere else, before anything downstream sees it (:func:`resolve_mode`).
ConfiguredPermissionMode = Literal[PermissionMode, "auto"]

# What a tool call is worth before the mode is applied. A *tier* is a
# property of the tool (from a rule, its server, or its risk and reach); an
# *outcome* is what this session does about it. ``ask`` becomes a prompt in
# ``confirm_mutations`` and a plain ``allow`` in ``allow_all``; ``deny`` is
# ``deny`` in every mode (ADR-102).
PermissionTier = Literal["allow", "ask", "deny"]

# Where a tier came from, for the ``/tools`` listing and the prompt. A
# person being asked "run it?" should be able to see whether that is
# because of a line they wrote in ``config.yaml`` or because the tool
# reaches the open internet, and a reviewer onboarding a new server needs
# the same column to know which rows still rest on the default.
TierSource = Literal["denied", "rule", "server", "reach", "risk", "default"]


@dataclass(frozen=True)
class PermissionRules:
    """Name patterns that set a tool's tier ahead of its metadata.

    Evaluated ``deny``, then ``ask``, then ``allow``; the first list with a
    matching pattern wins, and a pattern's specificity does not change that
    order. So ``ask: ["web__*"]`` prompts for ``web__web_search`` even when
    ``allow: ["web__web_search"]`` is also present. This is the precedence
    Claude Code documents for its own rule lists, and it is chosen because
    the alternative — most-specific-wins — makes a broad ``deny`` something
    a narrower ``allow`` can quietly carve a hole in.

    Patterns are shell-style globs (``fnmatch``) over the full tool name,
    matched case-insensitively: ``web__*`` is every tool on the ``web``
    server, ``calendar__add_*`` is that server's writes, ``*`` is everything.

    Attributes:
        deny: Patterns refused outright, whatever the mode.
        ask: Patterns that need a person's yes in ``confirm_mutations``.
        allow: Patterns that run without asking.
    """

    deny: tuple[str, ...] = ()
    ask: tuple[str, ...] = ()
    allow: tuple[str, ...] = ()

    def match(self, name: str) -> PermissionTier | None:
        """Return the tier the first matching list names, or ``None``.

        Args:
            name: Full tool name as registered (prefixed, for MCP tools).

        Returns:
            ``deny``, ``ask`` or ``allow`` when a pattern matched, else
            ``None`` so the caller falls through to the next source.
        """
        lowered = name.lower()
        tier: PermissionTier
        for tier, patterns in (
            ("deny", self.deny),
            ("ask", self.ask),
            ("allow", self.allow),
        ):
            if any(fnmatchcase(lowered, pattern.lower()) for pattern in patterns):
                return tier
        return None


@dataclass(frozen=True)
class TierDecision:
    """A tool's tier, where it came from, and a sentence saying why.

    The ``reason`` is written for a person, because it is printed next to
    the confirmation prompt and in ``/tools``. It is also why the decision
    is a value rather than a tuple: three positional strings are easy to
    get in the wrong order and hard to notice when they are.
    """

    tier: PermissionTier
    source: TierSource
    reason: str


def is_denied_name(name: str, denied_tools: frozenset[str] = frozenset()) -> bool:
    """Whether a tool name is refused by the deny-list, whatever else says.

    The call-time half of ADR-008. ``denied_tools`` is compared exactly and
    :data:`~harness.tools.base.DENIED_TOOL_WORDS` as substrings, both
    case-insensitively, exactly as the MCP adapter does at registration
    (ADR-042): two enforcement points, one definition.

    Args:
        name: Full tool name.
        denied_tools: Extra exact names, already lower-cased or not.

    Returns:
        ``True`` when the name must never run.
    """
    lowered = name.lower()
    if lowered in {denied.lower() for denied in denied_tools}:
        return True
    return any(word in lowered for word in DENIED_TOOL_WORDS)


def resolve_mode(
    configured: ConfiguredPermissionMode, interactive: bool
) -> PermissionMode:
    """Turn the configured mode into the one the policy will enforce.

    ``auto`` exists because the right default is different for the two ways
    the harness is run, and the difference is not a preference. On a
    terminal a person is present, so asking is cheap and the brief's
    human-in-the-loop requirement is met by asking. In a one-shot run, a
    pipe, a CI job or a container without a TTY there is nobody to answer,
    and a prompt there resolves to "no" — which would turn every unattended
    write into a refusal the moment the default changed. Neither fixed
    default is right for both, so the file says ``auto`` and this function
    says what that means here (ADR-102).

    A mode named explicitly — in ``config.yaml`` or with ``--permissions``
    — is returned unchanged. Pinning ``confirm_mutations`` on a pipe is a
    legitimate way to make an unattended run read-only-plus-deny-list, and
    the fail-closed prompt then does exactly that.

    Args:
        configured: Value of ``permission_mode`` after any CLI override.
        interactive: Whether stdin is a terminal a question can be put to.

    Returns:
        One of the three modes :func:`build_policy` accepts.
    """
    if configured == "auto":
        return "confirm_mutations" if interactive else "allow_all"
    return configured


def open_world_outcome_for(mode: PermissionMode) -> PermissionOutcome:
    """What an ``ask``-tier *read* gets in this mode.

    Separate from what a mutation gets because ``deny_mutations`` does not
    mean "deny reaching the internet": a read-only session is one for
    showing someone the agent without letting it change anything, and the
    web is the main thing worth showing. So the read-only mode leaves
    open-world reads *allowed*, ``confirm_mutations`` asks about them, and
    ``allow_all`` does not ask about anything.

    The sub-agent factory uses this with the *parent's* mode rather than
    the child's. A researcher runs as ``deny_mutations`` so it cannot write,
    but whether its web searches prompt is the parent session's choice, not
    the role's: a person who chose ``confirm_mutations`` said "ask me before
    reaching out", and delegating does not un-say it.

    Args:
        mode: A resolved permission mode.

    Returns:
        ``require_user_confirmation`` under ``confirm_mutations``, else
        ``allow``.
    """
    return "require_user_confirmation" if mode == "confirm_mutations" else "allow"


class PermissionPolicy(Protocol):
    """Decide whether a tool call may proceed."""

    def decide(self, tool: Tool, arguments: dict[str, object]) -> PermissionOutcome:
        """Return ``allow``, ``deny``, or ``require_user_confirmation``."""


class Confirmer(Protocol):
    """Ask a human to approve one tool call.

    Separate from the policy because deciding and asking fail differently.
    A policy that cannot decide is a bug; a confirmer that cannot ask — no
    terminal, a piped stdin, a closed front end — is an ordinary situation
    that has to resolve to a safe answer rather than an exception.
    """

    def confirm(self, tool: Tool, arguments: dict[str, object]) -> bool:
        """Return whether the user approved this call."""


class DenyingConfirmer:
    """Default confirmer: there is no user to ask, so the answer is no.

    This is the fail-closed default. The alternative — treating "nobody to
    ask" as approval — would mean a policy asking for confirmation silently
    behaved like :class:`AllowAllPolicy` in exactly the contexts where no
    human is watching, which is the opposite of what a confirmation prompt
    is for.
    """

    def confirm(self, tool: Tool, arguments: dict[str, object]) -> bool:
        """Refuse.

        Args:
            tool: Tool the loop is about to execute (unused).
            arguments: Parsed tool arguments (unused).

        Returns:
            ``False``, always.
        """
        return False


class AllowAllPolicy:
    """Every registered tool is allowed.

    The shipped default, and still a real decision rather than an absence of
    one: the tools that reach a registry have already passed the guarantees
    that actually contain this agent — path confinement (ADR-007), no delete
    tool by construction (ADR-008), and the MCP deny-list at registration
    (ADR-042). This policy says those are sufficient for an unattended run,
    which is what makes one-shot and container runs work unchanged.
    """

    def decide(self, tool: Tool, arguments: dict[str, object]) -> PermissionOutcome:
        """Always allow.

        Args:
            tool: Tool the loop is about to execute.
            arguments: Parsed tool arguments (unused).

        Returns:
            ``allow``.
        """
        return "allow"


class RiskBasedPolicy:
    """Set every tool's tier, then apply the mode to it.

    The tier comes from the first of these that has an opinion, and the
    order is the design (ADR-102):

    1. The deny-list. A name containing ``delete``, ``rename`` and friends,
       or one listed in ``denied_tools``, is ``deny`` and nothing below can
       change it. ADR-008 says this agent cannot delete, and until Week 2
       that was true because no delete class was ever written. The MCP
       adapter re-established it at registration time (ADR-042); this
       re-establishes it at call time, so the guarantee holds even if some
       future path registers a tool the adapter never saw — and even if a
       rule says ``allow: ["*"]``.
    2. A rule in ``config.yaml`` (:class:`PermissionRules`): the operator's
       explicit word about a name or a pattern.
    3. The server's default tier, for a tool discovered on an MCP server
       whose entry sets ``permission:``. One line to onboard a server
       cautiously, before anyone has reviewed what it offers.
    4. Reach. A tool that talks to the open world is ``ask`` even when it
       only reads: the query itself is data leaving the machine.
    5. Risk. A ``mutate`` tool is ``ask``; an ``observe`` tool is ``allow``.

    Every tool the harness can hold has a risk and a reach, so the policy
    is total: there is no tool it has no opinion about, including one
    discovered at runtime on a server nobody has configured a rule for.

    The mode then says what ``ask`` means. ``allow_all`` runs it,
    ``confirm_mutations`` asks, and ``deny_mutations`` refuses a mutation
    while letting an open-world read through — see
    :func:`open_world_outcome_for` for why those two halves differ.
    """

    def __init__(
        self,
        mutate_outcome: PermissionOutcome = "require_user_confirmation",
        denied_tools: frozenset[str] = frozenset(),
        rules: PermissionRules | None = None,
        server_tiers: dict[str, PermissionTier] | None = None,
        open_world_outcome: PermissionOutcome | None = None,
    ) -> None:
        """Store the rules this policy applies.

        Args:
            mutate_outcome: What an ``ask``-tier mutation gets. ``allow``
                makes this policy equivalent to :class:`AllowAllPolicy`
                apart from the deny-list; ``require_user_confirmation``
                prompts; ``deny`` makes the run read-only.
            denied_tools: Extra exact names refused outright, on top of
                :data:`~harness.tools.base.DENIED_TOOL_WORDS`. Compared
                case-insensitively.
            rules: Name patterns that set a tier ahead of the metadata.
                ``None`` means no rules, which is the pre-ADR-102 policy.
            server_tiers: Default tier per MCP server label, consulted for
                a tool carrying that ``server_name`` when no rule matched.
            open_world_outcome: What an ``ask``-tier *read* gets. ``None``
                means the same as ``mutate_outcome``, which is right for a
                policy built by hand; :func:`build_policy` derives it from
                the mode instead.
        """
        self._mutate_outcome = mutate_outcome
        self._open_world_outcome = (
            open_world_outcome if open_world_outcome is not None else mutate_outcome
        )
        self._denied_tools = frozenset(name.lower() for name in denied_tools)
        self._rules = rules if rules is not None else PermissionRules()
        self._server_tiers = dict(server_tiers or {})

    def tier_of(self, tool: Tool) -> TierDecision:
        """Return the tool's tier, where it came from, and why.

        The mode is not applied here: this is the half a reviewer wants to
        see in ``/tools`` — "what is this tool worth" — separately from
        "what will this session do about it", which is :meth:`decide`.

        Args:
            tool: Any registered tool.

        Returns:
            The decision, with a reason written for a person.
        """
        if is_denied_name(tool.name, self._denied_tools):
            return TierDecision("deny", "denied", "on the deny-list")
        from_rule = self._rules.match(tool.name)
        if from_rule is not None:
            return TierDecision(from_rule, "rule", "matched a permission rule")
        server = getattr(tool, "server_name", None)
        if server is not None and server in self._server_tiers:
            return TierDecision(
                self._server_tiers[server],
                "server",
                f"the {server} server's configured default",
            )
        if tool.reach == "open_world":
            return TierDecision("ask", "reach", "reaches outside the sandbox")
        if tool.risk == "mutate":
            return TierDecision("ask", "risk", "changes state")
        return TierDecision("allow", "default", "reads inside the sandbox")

    def decide(self, tool: Tool, arguments: dict[str, object]) -> PermissionOutcome:
        """Return the outcome for this call.

        Args:
            tool: Tool the loop is about to execute.
            arguments: Parsed tool arguments. Read only through
                :meth:`~harness.tools.base.Tool.risk_for`, which lets a call
                that can change nothing leave the ask tier — ``delegate`` to
                read-only specialists (ADR-119).

        Returns:
            ``deny`` for a ``deny`` tier, ``allow`` for an ``allow`` tier,
            and for ``ask`` the outcome the mode configured — the mutation
            one when the tool mutates, the open-world one when it only
            reads.
        """
        decision = self.tier_of(tool)
        if decision.tier == "deny":
            return "deny"
        if decision.tier == "allow":
            return "allow"
        # Asked only when the tool's own risk put it in the ask tier: a
        # rule, a server default or the reach of the tool is a choice made
        # about the tool, and one call's arguments cannot undo it (ADR-119).
        if decision.source == "risk" and tool.risk_for(dict(arguments)) == "observe":
            return "allow"
        if tool.risk == "mutate":
            return self._mutate_outcome
        return self._open_world_outcome


def build_policy(
    mode: PermissionMode,
    denied_tools: frozenset[str] = frozenset(),
    rules: PermissionRules | None = None,
    server_tiers: dict[str, PermissionTier] | None = None,
    open_world_outcome: PermissionOutcome | None = None,
) -> RiskBasedPolicy:
    """Return the policy a configured mode names.

    One factory so the CLI, the tests, and the sub-agent factory turn a mode
    into a policy the same way, and so adding a mode is one edit rather
    than a search for every ``if mode ==`` in the codebase.

    Note that ``allow_all`` returns a :class:`RiskBasedPolicy` configured to
    allow mutations, **not** :class:`AllowAllPolicy`. The difference is the
    deny-list: ADR-008 says this agent cannot delete, and that is a guarantee
    rather than a mode, so "allow everything" must still mean "everything
    that was ever allowed". :class:`AllowAllPolicy` stays as the loop's
    zero-configuration default, where the caller has opted out of policy
    entirely.

    Args:
        mode: Value of ``permission_mode``, already resolved.
        denied_tools: Extra exact names to refuse, passed through to
            :class:`RiskBasedPolicy`.
        rules: The ``permission_rules`` block, if any.
        server_tiers: Per-server default tiers from ``mcp_servers[].permission``.
        open_world_outcome: Override for what open-world reads get. The CLI
            leaves it ``None`` so it follows the mode; the sub-agent
            factory passes the *parent's* answer so a child cannot reach
            out more quietly than the session it runs inside.

    Returns:
        The matching policy. Typed as :class:`RiskBasedPolicy` rather than
        the protocol so callers that want :meth:`RiskBasedPolicy.tier_of`
        for a listing do not have to cast.
    """
    if open_world_outcome is None:
        open_world_outcome = open_world_outcome_for(mode)
    if mode == "confirm_mutations":
        mutate_outcome: PermissionOutcome = "require_user_confirmation"
    elif mode == "deny_mutations":
        mutate_outcome = "deny"
    else:
        mutate_outcome = "allow"
    return RiskBasedPolicy(
        mutate_outcome,
        denied_tools,
        rules=rules,
        server_tiers=server_tiers,
        open_world_outcome=open_world_outcome,
    )
