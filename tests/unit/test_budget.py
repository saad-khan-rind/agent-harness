"""The token budget: the ledger's arithmetic and the client's two gates.

The ledger is tested with an injected clock, because the behaviour that
matters most — a 24-hour window that starts on first use and then rolls
over — is the one that cannot be observed by waiting (ADR-089). The client
is tested with ``ScriptedLLM`` so the assertions are about what was
*charged* and what was *refused*, never about a network.
"""

import json
import os
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest
from harness.budget import (
    BUDGET_FILENAME,
    BudgetedLLMClient,
    BudgetExceeded,
    BudgetLedger,
)
from harness.llm.protocol import LLMError
from tests.fakes import ScriptedLLM, text

_BERLIN = ZoneInfo("Europe/Berlin")


class _Clock:
    """A settable clock so a test can move to tomorrow."""

    def __init__(self, at: datetime) -> None:
        self.at = at

    def __call__(self) -> datetime:
        return self.at


def _noon(day: int) -> datetime:
    return datetime(2026, 9, day, 12, 0, tzinfo=_BERLIN)


# --- ledger -----------------------------------------------------------------


def test_ledger_starts_at_zero_and_charges_add_up(tmp_path: Path) -> None:
    """Prompt and completion are summed separately and the total is both."""
    ledger = BudgetLedger(tmp_path, "Europe/Berlin", clock=_Clock(_noon(16)))

    assert ledger.usage().total_tokens == 0
    ledger.charge(6_000, 30)
    usage = ledger.charge(6_100, 50)

    assert usage.prompt_tokens == 12_100
    assert usage.completion_tokens == 80
    assert usage.total_tokens == 12_180


def test_ledger_persists_across_instances(tmp_path: Path) -> None:
    """A restart on the same day continues the same total.

    This is the property that makes it a budget rather than a session
    counter: a container restarted with ``--rm`` must not start over.
    """
    clock = _Clock(_noon(16))
    BudgetLedger(tmp_path, "Europe/Berlin", clock=clock).charge(500, 20)

    again = BudgetLedger(tmp_path, "Europe/Berlin", clock=clock)

    assert again.usage().total_tokens == 520
    assert again.path == tmp_path / BUDGET_FILENAME
    stored = json.loads((tmp_path / BUDGET_FILENAME).read_text(encoding="utf-8"))
    assert stored["prompt_tokens"] == 500
    assert stored["completion_tokens"] == 20
    assert stored["credit_tokens"] == 0
    # The window's clock is on disk, not the calendar date: that is what
    # lets a restart continue the same 24 hours (ADR-089).
    assert stored["started_at"].startswith("2026-09-16T12:00")


def test_the_window_rolls_over_24_hours_after_it_started() -> None:
    """A full day after the first charge, the allowance is whole again."""
    clock = _Clock(_noon(16))
    ledger = BudgetLedger(None, "Europe/Berlin", clock=clock)
    ledger.charge(900, 100)

    # One minute short: still the same window.
    clock.at = datetime(2026, 9, 17, 11, 59, tzinfo=_BERLIN)
    assert ledger.usage().total_tokens == 1_000

    clock.at = datetime(2026, 9, 17, 12, 1, tzinfo=_BERLIN)

    assert ledger.usage().total_tokens == 0
    # And unstarted again, so an idle day does not consume the next window.
    assert ledger.usage().started_at is None
    assert ledger.charge(10, 1).total_tokens == 11


@pytest.mark.parametrize(
    "content",
    [
        "not json",
        "{}",
        '{"started_at": "nope", "prompt_tokens": 1, "completion_tokens": 1}',
        "[]",
    ],
)
def test_ledger_survives_a_corrupt_file_and_says_so(
    tmp_path: Path, content: str
) -> None:
    """Garbage on disk means counting from zero with a warning, not a crash."""
    (tmp_path / BUDGET_FILENAME).write_text(content, encoding="utf-8")

    ledger = BudgetLedger(tmp_path, "Europe/Berlin", clock=_Clock(_noon(16)))

    assert ledger.usage().total_tokens == 0
    assert ledger.warning is not None
    assert "ignored" in ledger.warning
    # The next write replaces the garbage with a file the next start reads.
    ledger.charge(5, 5)
    again = BudgetLedger(tmp_path, "Europe/Berlin", clock=_Clock(_noon(16)))
    assert again.usage().total_tokens == 10
    assert again.warning is None


@pytest.mark.skipif(
    getattr(os, "geteuid", lambda: 0)() == 0, reason="root ignores permission bits"
)
def test_unwritable_root_degrades_to_in_memory_and_keeps_counting(
    tmp_path: Path,
) -> None:
    """A read-only mount costs persistence, never the run and never the count.

    After the first failed write the session's own charges must not be
    thrown away by re-reading the stale file on the next call.
    """
    root = tmp_path / "ro"
    root.mkdir()
    # A live window, not a pre-ADR-089 file: this test is about losing the
    # *write*, so the read has to succeed or it would be proving the wrong
    # thing.
    (root / BUDGET_FILENAME).write_text(
        json.dumps(
            {
                "started_at": _noon(16).isoformat(),
                "prompt_tokens": 40,
                "completion_tokens": 2,
            }
        ),
        encoding="utf-8",
    )
    root.chmod(0o500)
    try:
        ledger = BudgetLedger(root, "Europe/Berlin", clock=_Clock(_noon(16)))
        assert ledger.usage().total_tokens == 42
        ledger.charge(10, 0)
        ledger.charge(10, 0)

        assert ledger.usage().total_tokens == 62
        assert "in memory only" in ledger.describe
    finally:
        root.chmod(0o700)


# --- client -----------------------------------------------------------------


def test_client_passes_through_and_charges_the_ledger() -> None:
    """Every call's usage lands in the ledger; the reply is untouched."""
    inner = ScriptedLLM([text("hi", prompt_tokens=600, completion_tokens=40)])
    ledger = BudgetLedger(None, "UTC")
    client = BudgetedLLMClient(inner, ledger, daily_limit=10_000)

    response = client.chat([{"role": "user", "content": "x"}], [])

    assert response.content == "hi"
    assert ledger.usage().total_tokens == 640
    status = client.status()
    assert status.used_tokens == 640
    assert status.remaining_tokens == 9_360
    assert not status.warning and not status.exhausted


def test_client_refuses_once_the_limit_is_reached() -> None:
    """The refusal is an ``LLMError`` and the inner client is not called."""
    inner = ScriptedLLM([text("one", prompt_tokens=90, completion_tokens=10)])
    client = BudgetedLLMClient(inner, BudgetLedger(None, "UTC"), daily_limit=100)

    client.chat([], [])
    assert client.status().exhausted

    with pytest.raises(BudgetExceeded, match="100 of 100 tokens") as excinfo:
        client.chat([], [])
    assert isinstance(excinfo.value, LLMError)
    assert "budget_daily_tokens" in str(excinfo.value)
    assert len(inner.calls) == 1


def test_client_warns_past_the_threshold() -> None:
    """Warning is a display flag that trips before the limit does."""
    inner = ScriptedLLM([text("a", prompt_tokens=85, completion_tokens=0)])
    client = BudgetedLLMClient(
        inner, BudgetLedger(None, "UTC"), daily_limit=100, warn_fraction=0.8
    )

    client.chat([], [])

    status = client.status()
    assert status.warning and not status.exhausted
    assert status.fraction_used == pytest.approx(0.85)


# --- settings ---------------------------------------------------------------
