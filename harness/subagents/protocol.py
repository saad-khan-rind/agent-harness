"""The two seams delegation is reached through.

A sub-agent is another ``AgentLoop`` with its own instructions, context,
limits and permissions — not a second loop implementation. That claim has
been the sub-agent seam's contract since Week 1, and
:class:`SubAgentFactory` is the half of it that pins the claim: a factory
returns an ``AgentLoop``, so anything that satisfies this protocol has, by
its own type signature, not written a second agent runtime.

:class:`Delegator` is the half Week 3 added. Building a child and *running
one under a contract* are different operations with different failure modes,
and keeping them apart is what lets a caller construct a specialist to
inspect it — a test, a future orchestrator — without starting a run.
``ConfigurableFactory`` implements both, because the two need the same
collaborators; the protocols stay separate because the operations are.

Nothing imports these at runtime. They are pinned by
``tests/unit/test_seams.py`` for exactly that reason: a seam nobody imports
is a seam that can be silently reshaped.
"""

from typing import Protocol

from harness.loop import AgentLoop
from harness.subagents.contracts import DelegationResult, DelegationTask


class SubAgentFactory(Protocol):
    """Build specialized agents that reuse the parent harness."""

    def create(self, role: str) -> AgentLoop:
        """Return a loop configured for ``role``.

        Args:
            role: Named specialist (e.g. ``researcher``, ``writer``).

        Returns:
            An ``AgentLoop`` the parent can ``run`` and whose final text
            is folded back into the parent context.
        """


class Delegator(Protocol):
    """Run one task on a specialist and return a validated result.

    The runtime operation, as distinct from the construction above. Its
    contract is that it does not raise for anything a model can cause: an
    unknown role, an exhausted budget, a dead endpoint and an unparseable
    answer all come back as a :class:`DelegationResult` carrying a status,
    because an exception crossing back into the parent's tool dispatch turns
    one child's bad turn into the end of the parent's run (ADR-085).
    """

    def delegate(self, task: DelegationTask) -> DelegationResult:
        """Execute ``task`` on the specialist it names.

        Args:
            task: The parent's envelope — the task, the context the parent
                chose to hand over, and its constraints.

        Returns:
            The structured outcome, whatever happened.
        """
