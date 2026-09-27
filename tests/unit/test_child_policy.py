"""The gate a child's capabilities and budget are actually enforced on.

:class:`~harness.subagents.policy.ChildPolicy` is where three of the four
things §8.4 asks a sub-agent to have — its own permissions, its own execution
limits, and by extension its own tool set — stop being configuration and
start being refusals. Until this file it had no direct test: the delegation
tests reached it through a whole child run, which proves it works in the one
arrangement they set up and says nothing about the order of its checks, its
counters, or what happens at each boundary.

The counters are not incidental. ``refused_for_capability`` exists so a test
can assert that a researcher *tried* to write and was stopped, rather than
only that it had no way to try (ADR-086), and a counter nothing asserts on is
a comment with a type annotation.

Everything here is the real policy over real tools. The only double is the
clock, which is injected so a deadline can pass without the suite sleeping.
"""

from pathlib import Path

import pytest
from harness.permissions import AllowAllPolicy, PermissionOutcome, build_policy
from harness.subagents import ChildPolicy, ExecutionBudget
from harness.subagents.policy import budget_notice
from harness.tools import build_registry
from harness.tools.base import Tool


@pytest.fixture
def tools(workspace: Path) -> dict[str, Tool]:
    """The eight built-in tools, by name, over a real workspace."""
    registry = build_registry(workspace)
    return {name: registry.get(name) for name in registry.names()}


class _Clock:
    """A monotonic clock a test can move by hand."""

    def __init__(self, now: float = 1_000.0) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now


def _policy(
    tools: dict[str, Tool],
    allowed: set[str],
    *,
    budget: ExecutionBudget | None = None,
    deadline: float | None = None,
    clock: _Clock | None = None,
) -> ChildPolicy:
    """Build a child policy over a permissive inner policy."""
    return ChildPolicy(
        inner=AllowAllPolicy(),
        allowed_tools=frozenset(allowed),
        budget=budget if budget is not None else ExecutionBudget(),
        deadline=deadline,
        clock=clock,
    )


# -- capability: what this agent *is* ---------------------------------------


def test_a_tool_outside_the_capabilities_is_denied(tools: dict[str, Tool]) -> None:
    """The refusal that makes a specialist a specialist."""
    policy = _policy(tools, {"read_file"})

    assert policy.decide(tools["write_file"], {}) == "deny"
    assert policy.decide(tools["read_file"], {}) == "allow"


def test_a_refused_capability_is_counted_as_an_attempt(
    tools: dict[str, Tool],
) -> None:
    """The evidence that the limit is real rather than merely unreachable.

    Asserting "the researcher did not write" is weak on its own — it is also
    true of an agent that never tried. This is the assertion that it tried.
    """
    policy = _policy(tools, {"read_file"})

    policy.decide(tools["write_file"], {})
    policy.decide(tools["write_file"], {})

    assert policy.refused_for_capability == 2
    assert policy.attempts == 2
    # A refused call has not spent any of the child's tool allowance.
    assert policy.calls == 0


def test_capability_outranks_a_budget_that_still_has_room(
    tools: dict[str, Tool],
) -> None:
    """A capability cannot be bought with remaining budget.

    The order of the checks is the design: what an agent *is* is not
    negotiable against what it has left to spend.
    """
    policy = _policy(tools, {"read_file"}, budget=ExecutionBudget(max_tool_calls=100))

    assert policy.decide(tools["write_file"], {}) == "deny"
    assert policy.budget_exhausted is False
    assert policy.refused_for_capability == 1


# -- the deadline -----------------------------------------------------------


def test_a_call_after_the_deadline_is_denied_and_recorded(
    tools: dict[str, Tool],
) -> None:
    """The clock half of the budget, and the flag the status is read from."""
    clock = _Clock()
    policy = _policy(tools, {"read_file"}, deadline=clock.now + 10, clock=clock)

    assert policy.decide(tools["read_file"], {}) == "allow"
    clock.now += 11
    assert policy.decide(tools["read_file"], {}) == "deny"
    assert policy.expired is True


def test_the_deadline_is_checked_before_the_call_count(
    tools: dict[str, Tool],
) -> None:
    """A child out of time is told that, not told it is out of calls.

    Both limits have been reached here. The one reported is the clock,
    because "you ran out of time" and "you ran out of calls" send the parent
    to different next moves.
    """
    clock = _Clock()
    policy = _policy(
        tools,
        {"read_file"},
        budget=ExecutionBudget(max_tool_calls=1),
        deadline=clock.now + 10,
        clock=clock,
    )
    policy.decide(tools["read_file"], {})  # spends the only call
    clock.now += 11

    assert policy.decide(tools["read_file"], {}) == "deny"
    assert policy.expired is True
    assert policy.budget_exhausted is False


def test_no_deadline_means_the_clock_is_never_consulted(
    tools: dict[str, Tool],
) -> None:
    """``timeout_seconds: 0`` disables the check rather than expiring at once."""
    clock = _Clock()
    policy = _policy(tools, {"read_file"}, deadline=None, clock=clock)
    clock.now += 10_000

    assert policy.decide(tools["read_file"], {}) == "allow"
    assert policy.expired is False


# -- the call allowance -----------------------------------------------------


def test_the_call_allowance_runs_out_and_says_so(tools: dict[str, Tool]) -> None:
    """The last-resort limit, and the one that degrades gracefully."""
    policy = _policy(tools, {"read_file"}, budget=ExecutionBudget(max_tool_calls=2))

    assert policy.decide(tools["read_file"], {}) == "allow"
    assert policy.decide(tools["read_file"], {}) == "allow"
    assert policy.decide(tools["read_file"], {}) == "deny"

    assert policy.budget_exhausted is True
    assert policy.calls == 2
    assert policy.attempts == 3


def test_a_zero_call_allowance_means_no_limit(tools: dict[str, Tool]) -> None:
    """``0`` reads as "no ceiling here" everywhere in ``ExecutionBudget``."""
    policy = _policy(tools, {"read_file"}, budget=ExecutionBudget(max_tool_calls=0))

    for _ in range(50):
        assert policy.decide(tools["read_file"], {}) == "allow"
    assert policy.budget_exhausted is False


def test_a_call_the_inner_policy_denies_costs_no_allowance(
    tools: dict[str, Tool],
) -> None:
    """One mistake must not be charged twice.

    A call the wrapped policy refuses never runs, so it has not spent any of
    the child's tool allowance. Charging it for both would retire a child
    twice over for one bad call.
    """

    class _Denying:
        """An inner policy that refuses everything."""

        def decide(self, tool: Tool, arguments: dict[str, object]) -> PermissionOutcome:
            """Refuse."""
            return "deny"

    policy = ChildPolicy(
        inner=_Denying(),
        allowed_tools=frozenset({"read_file"}),
        budget=ExecutionBudget(max_tool_calls=2),
    )

    assert policy.decide(tools["read_file"], {}) == "deny"
    assert policy.decide(tools["read_file"], {}) == "deny"

    assert policy.calls == 0
    assert policy.budget_exhausted is False


# -- it only ever narrows ---------------------------------------------------


def test_the_wrapper_never_widens_the_inner_decision(
    tools: dict[str, Tool],
) -> None:
    """The property that stops a sub-agent being a privilege escalation.

    Under ``deny_mutations`` the real policy refuses a write. The wrapper
    holds the capability and has budget to spare, and the answer is still no.
    """
    policy = ChildPolicy(
        inner=build_policy("deny_mutations", frozenset()),
        allowed_tools=frozenset({"write_file", "read_file"}),
        budget=ExecutionBudget(max_tool_calls=10),
    )

    assert policy.decide(tools["write_file"], {}) == "deny"


def test_a_confirmation_is_passed_through_not_swallowed(
    tools: dict[str, Tool],
) -> None:
    """``require_user_confirmation`` survives the wrapper.

    §8.4 requires three outcomes, and a wrapper that collapsed ``ask`` into
    ``allow`` or ``deny`` would quietly remove one of them for every child.
    """
    policy = ChildPolicy(
        inner=build_policy("confirm_mutations", frozenset()),
        allowed_tools=frozenset({"write_file"}),
        budget=ExecutionBudget(),
    )

    assert policy.decide(tools["write_file"], {}) == "require_user_confirmation"


# -- what the parent is told ------------------------------------------------


def test_a_child_stopped_by_the_clock_says_so(tools: dict[str, Tool]) -> None:
    """The notice exists so a thin result is not mistaken for a complete one."""
    clock = _Clock()
    policy = _policy(tools, {"read_file"}, deadline=clock.now + 1, clock=clock)
    policy.decide(tools["read_file"], {})
    clock.now += 2
    policy.decide(tools["read_file"], {})

    notice = budget_notice(policy)
    assert notice is not None
    assert "ran out of time" in notice
    assert "1 tool calls" in notice


def test_a_child_stopped_by_its_allowance_says_so(tools: dict[str, Tool]) -> None:
    """The other notice, naming the allowance rather than the clock."""
    policy = _policy(tools, {"read_file"}, budget=ExecutionBudget(max_tool_calls=1))
    policy.decide(tools["read_file"], {})
    policy.decide(tools["read_file"], {})

    notice = budget_notice(policy)
    assert notice is not None
    assert "full allowance" in notice


def test_a_child_that_hit_no_limit_gets_no_notice(tools: dict[str, Tool]) -> None:
    """Silence when there is nothing to report."""
    policy = _policy(tools, {"read_file"})
    policy.decide(tools["read_file"], {})

    assert budget_notice(policy) is None
