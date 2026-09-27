"""Daily token budget: one number, persisted, enforced at the model call.

The harness already measures what every model call costs — InnKube reports
``usage`` and :class:`~harness.llm.protocol.LLMResponse` carries it
(ADR-031). What it did not do was *remember* the total past the process or
*stop* when the total got large. This package adds both:

* :mod:`harness.budget.ledger` — the persisted total for a **rolling
  24-hour window** that starts on the first charged prompt (ADR-089), kept
  under the memory root so it survives ``docker run --rm`` and stays out of
  the file tools' reach.
* :mod:`harness.budget.client` — an :class:`~harness.llm.protocol.LLMClient`
  wrapper that charges the ledger after every call and stops before one when
  either allowance is gone: the window's, or the **per-prompt** allowance
  that asks whether a long task should keep going.

Why a client wrapper and not a hook or a loop guard: spend happens at the
model call, every model call — the parent's and each sub-agent's — goes
through the one client instance the CLI builds, and a hook may not raise
into a run. Wrapping the client is therefore the single gate that sees
everything, which is the same argument ADR-057 makes for enforcing
permissions in one place. ``loop.py`` does not change (ADR-088).
"""

from harness.budget.client import (
    BudgetedLLMClient,
    BudgetExceeded,
    BudgetStatus,
    PromptBudgetExceeded,
)
from harness.budget.ledger import BUDGET_FILENAME, WINDOW, BudgetLedger, WindowUsage

__all__ = [
    "BUDGET_FILENAME",
    "WINDOW",
    "BudgetExceeded",
    "BudgetLedger",
    "BudgetStatus",
    "BudgetedLLMClient",
    "PromptBudgetExceeded",
    "WindowUsage",
]
