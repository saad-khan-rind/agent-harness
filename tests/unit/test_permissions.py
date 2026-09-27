"""The permission seam, from the policy up to the loop that enforces it.

Until ADR-057 this module was a declared seam with no call sites:
``PermissionPolicy`` existed, ``decide`` was never invoked, and the brief's
§8.4 requirement — permission management applied *consistently* rather than
as isolated special cases — was unmet. These tests pin the wiring, because
wiring is exactly the kind of thing that can be removed without any other
test noticing.

The loop tests below drive the real ``AgentLoop`` over a real
``ToolRegistry`` holding the real file tools, with only the model scripted
(ADR-014). That matters here more than usual: a permission check that only
works against a mock tool would prove nothing about the tools that ship.
"""

from pathlib import Path
from typing import Any

from harness.context import RunContext
from harness.loop import AgentLoop
from harness.permissions import (
    AllowAllPolicy,
    DenyingConfirmer,
    PermissionRules,
    RiskBasedPolicy,
    build_policy,
    open_world_outcome_for,
    resolve_mode,
)
from harness.tools import ToolRegistry, build_registry
from harness.tools.base import ERROR_PREFIX, Tool, ToolResult
from tests.fakes import ScriptedLLM, call, text


class RecordingConfirmer:
    """A ``Confirmer`` that answers from a script and remembers what it saw.

    The double the CLI's terminal prompt stands in for. Recording the calls
    is the point: a policy that never actually asks would otherwise pass a
    test that only checks the final observation.
    """

    def __init__(self, *answers: bool) -> None:
        """Args:
        *answers: Replies to give, in order. The last one repeats, so a
            test does not have to count how often the model retries.
        """
        self._answers = list(answers)
        self.asked: list[tuple[str, dict[str, object]]] = []

    def confirm(self, tool: Tool, arguments: dict[str, object]) -> bool:
        """Record the question and return the next scripted answer."""
        self.asked.append((tool.name, dict(arguments)))
        if len(self._answers) > 1:
            return self._answers.pop(0)
        return self._answers[0] if self._answers else False


class DeleteEverything(Tool):
    """A tool whose *name* the deny-list must refuse, however it got here.

    ADR-008 says this agent cannot delete. No such class ships — this one
    exists only so the call-time half of that guarantee has something to
    refuse, since by construction there is nothing in the real registry for
    it to act on.
    """

    name = "delete_everything"
    description = "Deletes things. Exists only to be refused by the policy."
    parameters: dict[str, Any] = {"type": "object", "properties": {}}
    risk = "observe"

    def __init__(self) -> None:
        """Build the tool with a sentinel root it never touches."""
        super().__init__(Path("/nonexistent"))
        self.calls = 0

    def _run(self, arguments: dict[str, Any]) -> ToolResult:
        """Record that it ran, which in a passing test never happens."""
        self.calls += 1
        return ToolResult("deleted everything")


class WebSearch(Tool):
    """A read-only tool that reaches the open internet.

    The shape that motivated the second axis: ``risk`` says it only reads,
    and that is true, but the query goes to a third party. Stands in for
    ``web__web_search`` without launching a server (ADR-102).
    """

    name = "web__web_search"
    description = "Searches the web. Exists only to be tiered by the policy."
    parameters: dict[str, Any] = {"type": "object", "properties": {}}
    risk = "observe"
    reach = "open_world"
    server_name = "web"

    def __init__(self) -> None:
        """Build the tool with a sentinel root it never touches."""
        super().__init__(Path("/nonexistent"))

    def _run(self, arguments: dict[str, Any]) -> ToolResult:
        """Never reached in these tests."""
        return ToolResult("results")


def _observations(context: RunContext) -> list[str]:
    """Return the tool observations in a finished context, in order.

    Not ``messages[-1]``: the last message of a completed run is the model's
    final answer, so a test that read the end of the list would be asserting
    on the wrong thing.
    """
    return [
        str(message["content"])
        for message in context.messages
        if message.get("role") == "tool"
    ]


def _loop(registry: ToolRegistry, responses: list[Any], **kwargs: Any) -> AgentLoop:
    """Build a loop with the other guards off, so these tests test one thing.

    ``max_repeated_calls`` in particular has to be disabled: several tests
    below deliberately make the same refused call over and over, which is
    the no-progress guard's job to stop and not what is under test here.
    """
    return AgentLoop(
        llm=ScriptedLLM(responses),
        registry=registry,
        max_iterations=0,
        max_repeated_calls=0,
        **kwargs,
    )


# --- the policies decide correctly ----------------------------------------


def test_allow_all_policy_allows_every_shipped_tool(workspace: Path) -> None:
    """The Week 1 default still allows everything, now that it is consulted."""
    policy = AllowAllPolicy()
    registry = build_registry(workspace)
    for name in registry.names():
        assert policy.decide(registry.get(name), {}) == "allow"


def test_risk_based_policy_splits_on_the_declared_risk(workspace: Path) -> None:
    """Observe tools run; mutate tools take the configured outcome.

    Asserted over the whole production registry rather than one tool, so a
    new tool cannot quietly land on the wrong side of the split: every tool
    declares a risk level, which is what makes the policy total.
    """
    policy = RiskBasedPolicy("require_user_confirmation")
    registry = build_registry(workspace)
    outcomes = {
        name: policy.decide(registry.get(name), {}) for name in registry.names()
    }
    for name, outcome in outcomes.items():
        expected = (
            "allow"
            if registry.get(name).risk == "observe"
            else "require_user_confirmation"
        )
        assert outcome == expected, name
    # Both halves are non-empty, or the assertion above is vacuous.
    assert "allow" in outcomes.values()
    assert "require_user_confirmation" in outcomes.values()


def test_denied_names_are_refused_whatever_their_risk_level() -> None:
    """ADR-008 is re-established at call time, not only at registration.

    The tool below declares itself ``observe``, which under the risk split
    alone would sail through. The deny-list runs first for exactly that
    reason: a mislabelled or hostile tool definition must not be able to
    talk its way past the one guarantee that has no exceptions.
    """
    assert RiskBasedPolicy("allow").decide(DeleteEverything(), {}) == "deny"


def test_build_policy_maps_every_mode(workspace: Path) -> None:
    """Each configured mode produces the policy it names."""
    registry = build_registry(workspace)
    write = registry.get("write_file")
    assert build_policy("allow_all").decide(write, {}) == "allow"
    assert build_policy("confirm_mutations").decide(write, {}) == (
        "require_user_confirmation"
    )
    assert build_policy("deny_mutations").decide(write, {}) == "deny"


def test_allow_all_mode_still_honours_a_deny_list(workspace: Path) -> None:
    """The deny-list is a guarantee, not a mode, so it survives ``allow_all``.

    This is why ``build_policy("allow_all")`` is not ``AllowAllPolicy``: the
    permissive *mode* still has to refuse the names ADR-008 refuses, and the
    permissive *class* is only the loop's default for a caller who wired no
    policy at all.
    """
    policy = build_policy("allow_all", frozenset({"write_file"}))
    registry = build_registry(workspace)
    assert policy.decide(registry.get("write_file"), {}) == "deny"
    assert build_policy("allow_all").decide(DeleteEverything(), {}) == "deny"


def test_default_confirmer_refuses() -> None:
    """No way to ask means no.

    The fail-closed default. If this ever returned ``True`` a policy that
    asks for confirmation would behave exactly like ``AllowAllPolicy``
    wherever nobody wired a confirmer — silently, and only in the unattended
    runs where the prompt mattered most.
    """
    assert DenyingConfirmer().confirm(DeleteEverything(), {}) is False


# --- tiers: reach, rules, servers (ADR-102) --------------------------------


def test_an_open_world_read_is_asked_about_only_where_the_mode_asks() -> None:
    """Reading is not free when the read leaves the machine.

    The TA's case: ``web_search`` ran silently under ``confirm_mutations``
    because the policy keyed off ``risk`` alone. Reach is the second axis:
    an open-world read takes the same prompt a write does in the confirming
    mode, and runs in the other two — ``deny_mutations`` is about writes,
    and the web is what a read-only demo shows.
    """
    policy = build_policy("confirm_mutations")
    decision = policy.tier_of(WebSearch())
    assert (decision.tier, decision.source) == ("ask", "reach")
    assert policy.decide(WebSearch(), {}) == "require_user_confirmation"
    assert build_policy("deny_mutations").decide(WebSearch(), {}) == "allow"
    assert build_policy("allow_all").decide(WebSearch(), {}) == "allow"
    # The helper the sub-agent factory uses agrees with the modes above.
    assert open_world_outcome_for("confirm_mutations") == "require_user_confirmation"
    assert open_world_outcome_for("deny_mutations") == "allow"
    assert open_world_outcome_for("allow_all") == "allow"


def test_a_child_inherits_the_parents_open_world_answer() -> None:
    """A researcher under a confirming session prompts before searching.

    Its own mode is ``deny_mutations`` (it may not write), which on its own
    would let open-world reads through. The parent's answer is passed in
    so delegating cannot make the web quieter than the session it runs in.
    """
    policy = build_policy(
        "deny_mutations",
        open_world_outcome=open_world_outcome_for("confirm_mutations"),
    )
    assert policy.decide(WebSearch(), {}) == "require_user_confirmation"


def test_rules_set_the_tier_and_are_evaluated_deny_then_ask_then_allow(
    workspace: Path,
) -> None:
    """A rule beats the metadata default; the first list with a match wins.

    ``allow`` names the exact tool and ``ask`` a pattern, and ``ask`` still
    wins — an allow rule cannot carve a hole in a broader rule above it.
    ``deny`` holds in every mode, and is matched case-insensitively because
    a config file is written by a human.
    """
    registry = build_registry(workspace)
    write = registry.get("write_file")

    # allow lifts the prompt for what it names, and nothing else.
    policy = build_policy(
        "confirm_mutations", rules=PermissionRules(allow=("web__*", "write_file"))
    )
    assert policy.tier_of(WebSearch()).source == "rule"
    assert policy.decide(WebSearch(), {}) == "allow"
    assert policy.decide(write, {}) == "allow"
    assert policy.decide(registry.get("edit_file"), {}) == "require_user_confirmation"

    # ask outranks a more specific allow.
    rules = PermissionRules(ask=("web__*",), allow=("web__web_search",))
    assert build_policy("confirm_mutations", rules=rules).decide(WebSearch(), {}) == (
        "require_user_confirmation"
    )

    # deny outranks ask, and the mode cannot soften it.
    rules = PermissionRules(deny=("Write_File",), ask=("write_file",))
    for mode in ("allow_all", "confirm_mutations", "deny_mutations"):
        assert build_policy(mode, rules=rules).decide(write, {}) == "deny", mode  # type: ignore[arg-type]


def test_the_deny_list_beats_an_allow_everything_rule() -> None:
    """ADR-008 survives ``allow: ["*"]``: the deny-list runs before rules."""
    policy = build_policy("allow_all", rules=PermissionRules(allow=("*",)))
    assert policy.decide(DeleteEverything(), {}) == "deny"
    assert policy.tier_of(DeleteEverything()).source == "denied"


def test_a_servers_default_tier_sits_between_rules_and_metadata() -> None:
    """``mcp_servers[].permission`` is the one-line way to onboard a server.

    It applies to that server's tools only, and a rule — more deliberate
    than a server-wide line — still wins over it.
    """
    policy = build_policy("confirm_mutations", server_tiers={"web": "allow"})
    assert policy.tier_of(WebSearch()).source == "server"
    assert policy.decide(WebSearch(), {}) == "allow"
    # A different server's default says nothing about this tool.
    policy = build_policy("confirm_mutations", server_tiers={"calendar": "allow"})
    assert policy.decide(WebSearch(), {}) == "require_user_confirmation"
    # A rule outranks the server line.
    policy = build_policy(
        "confirm_mutations",
        rules=PermissionRules(ask=("web__web_search",)),
        server_tiers={"web": "allow"},
    )
    assert policy.decide(WebSearch(), {}) == "require_user_confirmation"


def test_every_shipped_tool_has_a_tier_and_a_reason(workspace: Path) -> None:
    """The policy is total, and every decision can explain itself.

    Asserted over the production registry so a new tool cannot land
    without a tier, and so ``/tools`` never prints an empty reason.
    """
    policy = build_policy("confirm_mutations")
    registry = build_registry(workspace)
    for name in registry.names():
        decision = policy.tier_of(registry.get(name))
        assert decision.tier in {"allow", "ask", "deny"}, name
        assert decision.reason, name
        # Built-in tools are all local, so their tier follows risk alone.
        expected = "allow" if registry.get(name).risk == "observe" else "ask"
        assert decision.tier == expected, name


def test_auto_resolves_by_whether_anyone_can_answer() -> None:
    """A terminal gets asked, a pipe gets the unattended default, and a
    named mode is never resolved away."""
    assert resolve_mode("auto", interactive=True) == "confirm_mutations"
    assert resolve_mode("auto", interactive=False) == "allow_all"
    for mode in ("allow_all", "confirm_mutations", "deny_mutations"):
        assert resolve_mode(mode, interactive=True) == mode  # type: ignore[arg-type]
        assert resolve_mode(mode, interactive=False) == mode  # type: ignore[arg-type]


# --- the loop enforces them -----------------------------------------------


def test_a_denied_call_is_refused_before_the_tool_runs(workspace: Path) -> None:
    """Deny means the tool is never executed, not that its result is discarded.

    The file's absence is the real assertion. A policy that let the write
    happen and then reported an error upstream would satisfy every
    string-matching test and none of the point.
    """
    registry = build_registry(workspace)
    loop = _loop(
        registry,
        [call("write_file", path="a.txt", content="hi"), text("could not")],
        policy=build_policy("deny_mutations"),
    )
    context = RunContext(task="write")

    assert loop.run(context) == "could not"
    assert not (workspace / "a.txt").exists()
    observation = _observations(context)[-1]
    assert observation.startswith(ERROR_PREFIX)
    assert "not permitted" in observation


def test_reading_still_works_when_writing_is_denied(workspace: Path) -> None:
    """The split is enforced, not just declared: observe tools run unhindered."""
    (workspace / "a.txt").write_text("contents")
    registry = build_registry(workspace)
    loop = _loop(
        registry,
        [call("read_file", path="a.txt"), text("read it")],
        policy=build_policy("deny_mutations"),
    )
    context = RunContext(task="read")

    assert loop.run(context) == "read it"
    assert "contents" in _observations(context)[-1]


def test_confirmation_is_asked_for_and_honoured_when_granted(workspace: Path) -> None:
    """An approved call runs, and the confirmer was actually consulted."""
    registry = build_registry(workspace)
    confirmer = RecordingConfirmer(True)
    loop = _loop(
        registry,
        [call("write_file", path="a.txt", content="hi"), text("written")],
        policy=build_policy("confirm_mutations"),
        confirmer=confirmer,
    )

    assert loop.run(RunContext(task="write")) == "written"
    assert (workspace / "a.txt").read_text() == "hi"
    assert confirmer.asked == [("write_file", {"path": "a.txt", "content": "hi"})]


def test_a_declined_confirmation_stops_the_call(workspace: Path) -> None:
    """Said no means the tool does not run, and the model is told why.

    The wording is checked because it is load-bearing: the model's only
    route out of this is to understand that a *person* refused, rather than
    that the tool is broken and worth retrying.
    """
    registry = build_registry(workspace)
    loop = _loop(
        registry,
        [call("write_file", path="a.txt", content="hi"), text("asked and refused")],
        policy=build_policy("confirm_mutations"),
        confirmer=RecordingConfirmer(False),
    )
    context = RunContext(task="write")

    assert loop.run(context) == "asked and refused"
    assert not (workspace / "a.txt").exists()
    observation = _observations(context)[-1]
    assert observation.startswith(ERROR_PREFIX)
    assert "declined" in observation


def test_an_unknown_tool_name_is_not_turned_into_a_permission_error(
    workspace: Path,
) -> None:
    """A hallucinated name gets the registry's message, which is the useful one.

    The policy has no tool to judge, and "not permitted" would send the
    model looking for permission it cannot get, instead of at the list of
    names that actually exist.
    """
    registry = build_registry(workspace)
    loop = _loop(
        registry,
        [call("teleport", target="mars"), text("no such tool")],
        policy=build_policy("deny_mutations"),
    )
    context = RunContext(task="teleport")

    assert loop.run(context) == "no such tool"
    observation = _observations(context)[-1]
    assert "unknown tool" in observation
    assert "not permitted" not in observation


def test_repeated_refusals_retire_the_tool(workspace: Path) -> None:
    """Refusals count as failures, which is what keeps a denied path bounded.

    Without this a model could call a denied tool forever with slightly
    different arguments: never the same call twice, so the no-progress guard
    never fires, and never a real tool failure, so ADR-048 never fires
    either. ``AGENTS.md`` requires every path through the loop to terminate,
    so a refusal is counted like any other failure and the tool is retired
    on the same limit.
    """
    registry = build_registry(workspace)
    loop = _loop(
        registry,
        [
            call("write_file", call_id=f"c{index}", path=f"{index}.txt", content="x")
            for index in range(3)
        ]
        + [text("gave up")],
        policy=build_policy("deny_mutations"),
        max_tool_failures=3,
    )
    context = RunContext(task="write")

    assert loop.run(context) == "gave up"
    assert context.tool_failures["write_file"] == 3
    # The third refusal carries the retirement notice, so the model is told
    # to stop rather than left to discover it.
    assert "rest of this task" in _observations(context)[-1]


def test_permissions_reach_tools_the_file_registry_never_saw(workspace: Path) -> None:
    """§8.4's "consistently, not special cases", tested where it could break.

    A tool registered from somewhere other than ``build_registry`` — an MCP
    server, memory, a skill — is dispatched through the same method, so it
    is policed by the same policy without anything being added for it. This
    one is also named to trip the deny-list, which is the ADR-008 backstop
    for a server that advertises a delete tool at runtime.
    """
    registry = build_registry(workspace)
    remote = DeleteEverything()
    registry.register(remote)
    loop = _loop(
        registry,
        [call("delete_everything"), text("refused")],
        policy=build_policy("allow_all"),
    )
    context = RunContext(task="delete")

    assert loop.run(context) == "refused"
    assert remote.calls == 0
    assert "not permitted" in _observations(context)[-1]
