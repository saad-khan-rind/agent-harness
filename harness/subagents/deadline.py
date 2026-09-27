"""The other half of a child's timeout: the one the permission gate cannot see.

:class:`~harness.subagents.policy.ChildPolicy` enforces
``ExecutionBudget.timeout_seconds`` on the way into a tool call, which is the
right place for it and was, until now, the *only* place. That leaves a gap
the ADR did not state: the check runs when a child asks for a tool, and a
child that stops asking is never checked again. :meth:`AgentLoop.run` reads
no clock — it counts iterations — so a child that spends its remaining
allowance reasoning runs past its deadline for as long as its iteration cap
permits, and ``TIMED_OUT`` is a status the runtime can never produce.

Sequentially that is a wrong number in a record. Under a batched
``delegate`` it is a liveness bug: a child that will not stop holds a worker
thread, and the batch cannot finish until it does.

**Enforced at the model call, by wrapping the client.** That is the shape
``harness/budget/`` already established for the token allowance (ADR-088):
the loop is not the place for a limit, a wrapper around
:class:`~harness.llm.protocol.LLMClient` is, because every iteration must
pass through one to continue. Wrapping also means the check costs nothing
when the feature is off — the factory passes the bare client through when
``timeout_seconds`` is zero — and that ``loop.py`` gains no line, which is
the constraint the whole sub-agent design is built around (ADR-084).

This is not a second *permission* gate. Tool calls are decided in exactly one
place, as ADR-057 requires; what is added here is a second **resource** meter,
on the resource the permission gate never sees (ADR-106).
"""

import time
from collections.abc import Callable
from typing import Any

from harness.llm.protocol import LLMClient, LLMResponse


class ChildDeadlineExceeded(RuntimeError):
    """Raised when a child's wall-clock allowance ran out mid-run.

    Deliberately *not* an :class:`~harness.llm.protocol.LLMError`. The
    endpoint is fine; it is this child that is out of time, and the two need
    different statuses on the way back to the parent — ``FAILED`` says retry,
    ``TIMED_OUT`` says narrow the task. Being a separate type is what lets
    :meth:`ConfigurableFactory.delegate` tell them apart without inspecting a
    message string.
    """


class DeadlineLLMClient:
    """Refuse the next model call once a child's deadline has passed.

    Implements :class:`~harness.llm.protocol.LLMClient` structurally. One
    instance per child run, because the deadline is a property of that run.

    What it bounds is what a child may *start*. A model call already in
    flight is bounded by the LLM client's own request timeout and by nothing
    here — the same honest limit :class:`ExecutionBudget` already states for
    the tool-call check. Checking before the call rather than after is
    deliberate: after would let a child spend a whole request to be told it
    should not have.
    """

    def __init__(
        self,
        inner: LLMClient,
        deadline: float,
        clock: Callable[[], float] | None = None,
    ) -> None:
        """Args:
        inner: The real client, shared with the parent and every sibling.
            Stateless, as the protocol requires, so sharing is safe.
        deadline: Monotonic instant past which no further call may start.
        clock: Monotonic clock, injectable so a test can expire a child
            without sleeping. Must be the same clock the child's
            :class:`~harness.subagents.policy.ChildPolicy` was given, or the
            two halves of one timeout would disagree.
        """
        self._inner = inner
        self._deadline = deadline
        self._clock = clock if clock is not None else time.monotonic

    @property
    def expired(self) -> bool:
        """Whether the deadline has passed."""
        return self._clock() >= self._deadline

    def chat(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]],
    ) -> LLMResponse:
        """Forward one call, unless this child is out of time.

        Args:
            messages: Chat messages, passed through untouched.
            tools: Tool schemas, passed through untouched.

        Returns:
            Whatever the wrapped client returns.

        Raises:
            ChildDeadlineExceeded: If the deadline passed before this call.
            LLMError: Whatever the wrapped client raises.
        """
        if self.expired:
            raise ChildDeadlineExceeded(
                "the sub-agent's time allowance ran out before this step"
            )
        return self._inner.chat(messages, tools)
