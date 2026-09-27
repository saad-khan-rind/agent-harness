"""Where a child's capabilities and budget are actually enforced.

The requirement this module answers is the one that separates a sub-agent
system from a prompt trick: *the runtime, not the model, enforces the
limits*. Nothing in a child's prompt is load-bearing here. A researcher that
decides to write a file, or a critic that decides it needs twelve more tool
calls, is refused by code that never asks the model's opinion.

**It is a policy wrapper, and that is the whole design.** This harness has
exactly one permission gate —
:meth:`harness.loop.AgentLoop._execute` consults a
:class:`~harness.permissions.PermissionPolicy` before every tool call
(ADR-057) — and every tool a child can reach goes through it: file tools,
memory tools, skill tools, tools discovered on an MCP server. So a wrapper
here polices all of them, with no second enforcement point to keep correct
and no branch added to the loop. It is the same move
:class:`~harness.skills.policy.SkillScopedPolicy` makes, for the same reason
(ADR-086).

**It only ever narrows.** The wrapped policy still decides everything it
decided before — risk tiers, the delete-word list, the confirmation prompt —
and this can turn an ``allow`` into a ``deny`` and never the reverse. A
wrapper that could widen a decision would be a way to escalate past
:class:`~harness.permissions.RiskBasedPolicy`, which is precisely what a
sub-agent must not be.

**Three refusals, one mechanism.** Capability, call count and deadline are
different limits with the same enforcement point, and the reason is
practical: a budget that is checked somewhere else is a budget that gets
checked at three call sites, two of which will eventually be wrong. The
trade is stated honestly in :class:`ExecutionBudget` — the deadline bounds
what a child may *start*, not an in-flight model call, which only the LLM
client's own request timeout can bound.
"""

import time
from collections.abc import Callable

from harness.permissions import PermissionOutcome, PermissionPolicy
from harness.subagents.contracts import ExecutionBudget
from harness.tools.base import Tool


class ChildPolicy:
    """Narrow a policy to one child's capabilities and allowance.

    One instance per child run, never shared: it counts calls, and a counter
    shared between runs would retire a healthy child because an earlier one
    was busy.
    """

    def __init__(
        self,
        inner: PermissionPolicy,
        allowed_tools: frozenset[str],
        budget: ExecutionBudget,
        deadline: float | None = None,
        clock: Callable[[], float] | None = None,
    ) -> None:
        """Args:
        inner: The policy that decides when this wrapper permits the call.
            Already narrowed to the child's effective permission mode by
            the factory.
        allowed_tools: Names the child's capabilities resolved to. A tool
            outside this set is refused even if it somehow reached the
            child's registry — the registry filter and this check are
            deliberately redundant, because a capability system with one
            enforcement point is one refactor away from having none.
        budget: The child's effective allowance.
        deadline: Monotonic instant the run must stop acting at, or
            ``None`` when the timeout is disabled.
        clock: Monotonic clock, injectable so the deadline can be tested
            without sleeping.
        """
        self._inner = inner
        self._allowed = allowed_tools
        self._budget = budget
        self._deadline = deadline
        self._clock = clock if clock is not None else time.monotonic
        self._calls = 0
        self._attempts = 0
        self._refused_for_capability = 0
        self._budget_exhausted = False
        self._expired = False

    @property
    def calls(self) -> int:
        """Tool calls this child was permitted to make."""
        return self._calls

    @property
    def attempts(self) -> int:
        """Tool calls this child asked to make, refusals included."""
        return self._attempts

    @property
    def budget_exhausted(self) -> bool:
        """Whether the call allowance ran out during this run."""
        return self._budget_exhausted

    @property
    def expired(self) -> bool:
        """Whether the deadline passed during this run."""
        return self._expired

    @property
    def refused_for_capability(self) -> int:
        """Calls refused because the child does not hold the capability.

        Counted rather than merely refused because it is the number that
        proves the limit is real. A test asserting "the researcher could not
        write" is much stronger when it can also assert that the researcher
        *tried* and was stopped (ADR-086).
        """
        return self._refused_for_capability

    def decide(self, tool: Tool, arguments: dict[str, object]) -> PermissionOutcome:
        """Return the outcome for one of the child's tool calls.

        Order is the design, and it is cheapest-and-most-absolute first:

        1. **Capability.** A tool the specification never granted is denied
           regardless of budget or clock. This is a statement about what
           this agent *is*, and it cannot be bought with remaining budget.
        2. **Deadline.** Checked before the call count so a child that is
           out of time is told that, rather than being told it is out of
           calls with seconds still on the clock.
        3. **Call count.** The last resort, and the one that degrades
           gracefully: further calls are refused, the child keeps its
           context, and it can still write a result from what it has.
        4. **The wrapped policy**, which decides everything it always did.

        Args:
            tool: The tool the child asked for.
            arguments: Its arguments, passed through untouched.

        Returns:
            ``deny`` when this wrapper refuses, otherwise the wrapped
            policy's decision.
        """
        self._attempts += 1
        if tool.name not in self._allowed:
            self._refused_for_capability += 1
            return "deny"
        if self._deadline is not None and self._clock() >= self._deadline:
            self._expired = True
            return "deny"
        if self._budget.max_tool_calls and self._calls >= self._budget.max_tool_calls:
            self._budget_exhausted = True
            return "deny"

        decision = self._inner.decide(tool, arguments)
        # Counted only when something is actually going to run. A call the
        # wrapped policy refuses has cost the child a turn but has not spent
        # any of its tool allowance, and charging it for both would retire a
        # child twice over for one mistake.
        if decision != "deny":
            self._calls += 1
        return decision


def budget_notice(policy: ChildPolicy) -> str | None:
    """Return the line to append to a child's result when a limit stopped it.

    A child whose budget ran out still answers — it is asked to summarise
    what it has — and without this the parent would receive a thin result
    with no indication that it is thin *because the runtime cut it off*.
    That distinction changes what the parent should do next: a thin answer is
    a reason to ask differently, a truncated one is a reason to ask again
    with more room.

    Args:
        policy: The child's policy, after its run.

    Returns:
        A one-line notice, or ``None`` when no limit was hit.
    """
    if policy.expired:
        return (
            f"Note: the sub-agent ran out of time and was stopped "
            f"after {policy.calls} tool calls; the result below is what it "
            f"had reached."
        )
    if policy.budget_exhausted:
        return (
            f"Note: the sub-agent used its full allowance of "
            f"{policy.calls} tool calls; the result below is what it had "
            f"reached."
        )
    return None
