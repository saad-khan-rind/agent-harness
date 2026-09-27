"""The gate: an ``LLMClient`` that meters every call against two allowances.

This is the whole of the enforcement, and it is deliberately the only place.
Every model call the harness makes — the top-level agent's, and each
sub-agent's, because the factory hands children the same client instance —
passes through :meth:`BudgetedLLMClient.chat`. Checking here therefore covers
all of them without the loop, the factory or any hook knowing a budget exists
(ADR-088).

**Two allowances, two very different meanings.** ADR-089 split them because
one number cannot do both jobs:

* the **window** allowance is about money — a rolling 24 hours of spend,
  shared by every session on the volume, and the thing that actually has to
  be capped;
* the **prompt** allowance is about runaways — the most one instruction may
  cost before a person is asked whether it is still worth it. A task that
  genuinely needs more is not a fault, so this one is a *checkpoint*, not a
  ceiling: the user types ``continue`` and the same task carries on.

That distinction is why the two raise different exceptions. Both derive from
:class:`~harness.llm.protocol.LLMError`, so the loop's ``finally`` still
fires ``on_run_end`` exactly once and the CLI's existing error path reports
them — but the CLI can tell "pause, ask, resume" from "stop, the day is
spent" without parsing a message.

Two honest limits of the check, unchanged:

* **It cannot know a call's cost before making it.** The check is "is any
  allowance left", so the last call may overshoot by its own size. It is
  documented rather than solved, because the only "solution" is an estimate
  and ADR-031 rules those out.
* **It cannot stop a run more gently than a dead endpoint would.** The
  workspace may already have been changed by earlier tool calls, which is
  why both messages say where the run stopped, not merely that it did.
"""

import threading
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from harness.budget.ledger import BudgetLedger
from harness.llm.protocol import LLMClient, LLMError, LLMResponse


class BudgetExceeded(LLMError):
    """Raised when the rolling 24-hour allowance is spent.

    The hard one. Nothing continues until the window rolls over — unless the
    user explicitly takes the run onto credit, which is a decision the
    harness will not make on anybody's behalf.
    """


class PromptBudgetExceeded(LLMError):
    """Raised when one instruction has cost its whole per-prompt allowance.

    The soft one, and the distinction matters: the money is not gone, the
    task simply turned out to be bigger than one allowance. The CLI catches
    this, says so, and resumes the *same* task when the user types
    ``continue`` (ADR-089).
    """


@dataclass(frozen=True)
class BudgetStatus:
    """A snapshot of both allowances, for the banner and the per-answer line.

    Attributes:
        used_tokens: Prompt plus completion spent in the current window,
            every session on this memory volume included.
        limit_tokens: The configured window allowance.
        credit_tokens: Spend that went past the limit with consent.
        resets_at: When the window ends, or ``None`` if it has not started.
        warn_fraction: The share of the allowance past which the line is a
            warning rather than a report.
        prompt_used_tokens: Spend charged to the current instruction.
        prompt_limit_tokens: The per-prompt allowance, or ``0`` when off.
        on_credit: Whether this session is currently spending on credit.
    """

    used_tokens: int
    limit_tokens: int
    resets_at: datetime | None
    warn_fraction: float
    credit_tokens: int = 0
    prompt_used_tokens: int = 0
    prompt_limit_tokens: int = 0
    on_credit: bool = False

    @property
    def remaining_tokens(self) -> int:
        """Return what is left, never negative even after an overshoot."""
        return max(0, self.limit_tokens - self.used_tokens)

    @property
    def fraction_used(self) -> float:
        """Return the share of the allowance spent, ``1.0`` or above when over."""
        return self.used_tokens / self.limit_tokens

    @property
    def warning(self) -> bool:
        """Whether the window's spend has crossed the warning threshold."""
        return self.fraction_used >= self.warn_fraction

    @property
    def exhausted(self) -> bool:
        """Whether the window allowance is gone.

        Says nothing about whether the next call will be refused — on credit
        it will not be. That decision belongs to :meth:`BudgetedLLMClient.chat`.
        """
        return self.used_tokens >= self.limit_tokens

    @property
    def prompt_exhausted(self) -> bool:
        """Whether this instruction has used its whole allowance."""
        limit = self.prompt_limit_tokens
        return bool(limit) and self.prompt_used_tokens >= limit


class BudgetedLLMClient:
    """Wrap an ``LLMClient`` so every call is metered against both allowances.

    Implements :class:`~harness.llm.protocol.LLMClient` structurally, and is
    stateless in the sense the protocol requires: conversation state still
    lives in :class:`~harness.context.RunContext`. What is kept here is the
    ledger reference (shared on purpose), the current instruction's running
    total, and whether the user has taken this session onto credit.
    """

    def __init__(
        self,
        inner: LLMClient,
        ledger: BudgetLedger,
        daily_limit: int,
        warn_fraction: float = 0.8,
        prompt_limit: int = 0,
    ) -> None:
        """Args:
        inner: The real client (InnKube, or a scripted fake in tests).
        ledger: Where the window's total lives.
        daily_limit: Prompt plus completion tokens allowed per rolling
            24 hours. Validated positive by ``Settings``.
        warn_fraction: Share of the allowance past which :meth:`status`
            reports a warning. Display only; enforcement is the limit.
        prompt_limit: Tokens one instruction may spend before the user is
            asked whether to continue. ``0`` switches the checkpoint off,
            which is the pre-ADR-089 behaviour.
        """
        self._inner = inner
        self._ledger = ledger
        self._limit = daily_limit
        self._warn_fraction = warn_fraction
        self._prompt_limit = prompt_limit
        self._prompt_used = 0
        self._on_credit = False
        # The per-prompt total is the one piece of mutable state here, and
        # since a batched delegation runs children through this same wrapper
        # concurrently, `+=` on it is a read-modify-write that can lose a
        # child's spend. A child's tokens count against the instruction that
        # delegated it (ADR-089), so losing one would let a fan-out slip past
        # the checkpoint that pauses a runaway task.
        self._prompt_lock = threading.Lock()

    @property
    def ledger(self) -> BudgetLedger:
        """Return the ledger, for the banner and ``/budget``."""
        return self._ledger

    @property
    def limit(self) -> int:
        """Return the window allowance in tokens."""
        return self._limit

    @property
    def on_credit(self) -> bool:
        """Whether this session is spending past the limit with consent."""
        return self._on_credit

    # -- what the CLI drives -------------------------------------------------

    def start_prompt(self) -> None:
        """Begin a fresh per-prompt allowance.

        Called for a new instruction, and again for each ``continue`` —
        which is the entire mechanism. Nothing else resets it, so a task
        that keeps going keeps being checked.
        """
        self._prompt_used = 0

    def allow_credit(self) -> None:
        """Let this session spend past the window allowance.

        Session-scoped and deliberately not persisted: consent to overspend
        is given for the work in front of you, and a flag that outlived the
        container would quietly disarm the budget for every run after it.
        The *spend* is persisted; the permission is not (ADR-089).
        """
        self._on_credit = True

    def status(self) -> BudgetStatus:
        """Return both allowances, re-read from the ledger."""
        usage = self._ledger.usage()
        return BudgetStatus(
            used_tokens=usage.total_tokens,
            limit_tokens=self._limit,
            resets_at=self._ledger.resets_at(),
            warn_fraction=self._warn_fraction,
            credit_tokens=usage.credit_tokens,
            prompt_used_tokens=self._prompt_used,
            prompt_limit_tokens=self._prompt_limit,
            on_credit=self._on_credit,
        )

    # -- the gate ------------------------------------------------------------

    def chat(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]],
    ) -> LLMResponse:
        """Check both allowances, then call and charge.

        The per-prompt check comes first because it is the recoverable one:
        when both are spent, the more useful thing to tell someone is the one
        they can do something about in the next five seconds.

        Args:
            messages: Passed through untouched.
            tools: Passed through untouched.

        Returns:
            The inner client's response, after its usage has been recorded.

        Raises:
            PromptBudgetExceeded: This instruction has spent its allowance.
            BudgetExceeded: The window is spent and credit was not granted.
            LLMError: Whatever the inner client raises, unchanged.
        """
        status = self.status()
        if status.prompt_exhausted:
            raise PromptBudgetExceeded(self._prompt_message(status))
        if status.exhausted and not self._on_credit:
            raise BudgetExceeded(self._exhausted_message(status))

        response = self._inner.chat(messages, tools)
        # Charged after the reply because that is when the cost is known. A
        # call that raised cost nothing we can measure, so it is not charged
        # — the same rule ``RunContext.record_usage`` follows.
        spent = max(0, response.prompt_tokens) + max(0, response.completion_tokens)
        with self._prompt_lock:
            self._prompt_used += spent
        self._ledger.charge(
            response.prompt_tokens,
            response.completion_tokens,
            # Credit is what this call *was*, not what the session may do:
            # a call made while the window still had room is not borrowing,
            # even in a session that has consented to borrow later.
            on_credit=self._on_credit and status.exhausted,
        )
        return response

    # -- what the user is told -----------------------------------------------

    def _prompt_message(self, status: BudgetStatus) -> str:
        """Return the pause, which is an offer rather than a refusal."""
        return (
            f"per-prompt budget reached: this instruction has used "
            f"{status.prompt_used_tokens:,} of {status.prompt_limit_tokens:,} "
            f"tokens. The work so far is kept. Type 'continue' to carry on "
            f"with the same task on a fresh allowance, or give a new "
            f"instruction to move on."
        )

    def _exhausted_message(self, status: BudgetStatus) -> str:
        """Return the refusal, with the three ways out of it."""
        resets = (
            status.resets_at.strftime("%H:%M %Z on %Y-%m-%d")
            if status.resets_at is not None
            else "the next prompt"
        )
        return (
            f"token budget exhausted: {status.used_tokens:,} of "
            f"{status.limit_tokens:,} tokens used in the current 24-hour "
            f"window. No further model calls will be made until it resets at "
            f"{resets}. To continue now, say 'use credit on the go' and the "
            f"run will carry on with the overspend recorded, or raise "
            f"budget_daily_tokens in config.yaml."
        )
