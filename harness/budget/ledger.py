"""The persisted allowance: a rolling 24-hour window, plus credit past it.

One small JSON file. It answers two questions — how many tokens has this
harness spent in the current window, and how much has been borrowed past the
limit — and it has to answer them across restarts, because a budget that
resets whenever the container does is not a budget.

**The window rolls from first use, it does not sit on the calendar.** The
original version keyed the total on the local date, so the allowance reset at
local midnight (ADR-088). That is easy to reason about and wrong for the way
this harness is actually used: a demo that starts at 23:40 gets twenty
minutes of allowance and then a full fresh one, and a session that starts at
00:10 has to wait almost a full day for more. The window now starts when the
first prompt is charged and ends 24 hours later, so everyone gets the same
allowance however their day is arranged (ADR-089).

Nothing starts the clock but real spend. Opening the REPL, reading the
banner and quitting costs nothing and does not consume the window — the
first charged model call is what sets ``started_at``.

The rest of the design is unchanged from ADR-088 and each point is a test:

* **Re-read before every write.** Two sessions on the same memory volume
  each add their own calls to whatever is on disk at that moment, so the
  total stays approximately right rather than being whichever process wrote
  last. A read-modify-write without a lock, stated as such: this is a
  spending guard, not an accounting system.
* **Never raises.** It is consulted around every model call. A full disk or
  a read-only mount degrades to an in-memory count for the session and a
  banner note, exactly as the trace hook does (ADR-083).
* **Atomic writes.** The file is replaced, not rewritten in place, so a
  process killed mid-write leaves the previous total rather than a
  half-written JSON the next read has to throw away.
"""

import json
import os
import threading
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

BUDGET_FILENAME = "budget.json"

# How long an allowance lasts once it starts. A day, expressed as a duration
# rather than as a date, which is the whole point of ADR-089.
WINDOW = timedelta(hours=24)


@dataclass(frozen=True)
class WindowUsage:
    """What has been spent in the current 24-hour window.

    Attributes:
        started_at: When the window opened — the moment of the first charge
            after the last reset. ``None`` before anything has been spent,
            which is what "the clock has not started" looks like on disk.
        prompt_tokens: Prompt tokens billed in the window, across sessions.
        completion_tokens: Completion tokens billed in the window.
        credit_tokens: Tokens spent *past* the limit, after the user asked
            to keep going on credit. Counted separately so the line can say
            how deep the borrowing went, and so it survives a restart.
    """

    started_at: datetime | None = None
    prompt_tokens: int = 0
    completion_tokens: int = 0
    credit_tokens: int = 0

    @property
    def total_tokens(self) -> int:
        """Return prompt plus completion — the number the limit applies to."""
        return self.prompt_tokens + self.completion_tokens


class BudgetLedger:
    """The rolling allowance, persisted under the memory root.

    Construct it with ``root=None`` for a purely in-memory ledger — what a
    session with memory disabled gets, and what tests that only care about
    the arithmetic use.
    """

    def __init__(
        self,
        root: Path | None,
        timezone: str = "UTC",
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        """Args:
        root: Memory root, or ``None`` to keep the total in memory only.
            Not created here; ``load_settings`` creates and write-probes it
            at startup.
        timezone: IANA zone the window's timestamps are reported in. The
            window length is 24 hours either way; the zone only decides how
            "resets at" reads to a person.
        clock: Returns the current time. Injectable so the rollover can be
            tested without waiting a day.
        """
        self._path = root.resolve() / BUDGET_FILENAME if root is not None else None
        self._zone = ZoneInfo(timezone)
        self._clock = clock if clock is not None else self._now
        self._usage = WindowUsage()
        self._write_error: str | None = None
        self._read_error: str | None = None
        # Every charge is a read-modify-write across a file that a second
        # session may also be writing, and since delegation became concurrent
        # it is also one that several sub-agents of *this* session reach at
        # once. Without the lock two simultaneous charges both read the same
        # total and the second overwrites the first, so tokens that were
        # really spent stop being counted — which is the one thing a budget
        # must not do (ADR-106). Contention is irrelevant: the critical
        # section is a small file write next to a model call.
        self._lock = threading.RLock()
        self._reload()

    # -- what callers read ---------------------------------------------------

    @property
    def path(self) -> Path | None:
        """Return the ledger file, or ``None`` for an in-memory ledger."""
        return self._path

    @property
    def describe(self) -> str:
        """Return one banner phrase saying where the total lives.

        Says so when persistence is *not* working, because a budget that
        silently forgets itself on every restart is the failure a user would
        never notice from the numbers alone.
        """
        if self._path is None:
            return "in memory only (no directory configured)"
        if self._write_error is not None:
            return f"in memory only ({self._write_error})"
        return str(self._path)

    @property
    def warning(self) -> str | None:
        """Return why an existing ledger file was ignored, or ``None``.

        A corrupt or unreadable file is not fatal — the count starts from
        zero for the session — but it is worth one line at startup, because
        "the budget reset itself" is otherwise indistinguishable from a new
        window.
        """
        if self._read_error is None:
            return None
        return f"budget file ignored ({self._read_error}); counting from zero"

    def usage(self) -> WindowUsage:
        """Return the window's totals, re-read from disk and rolled if due."""
        with self._lock:
            self._reload()
            return self._usage

    def resets_at(self) -> datetime | None:
        """Return when the current window ends, or ``None`` if unstarted.

        ``None`` is not "never": it means no prompt has been charged yet, so
        the allowance is whole and its clock starts on the next one. The
        banner says that in words rather than printing a time that would be
        wrong the moment the user typed anything.
        """
        with self._lock:
            self._reload()
            if self._usage.started_at is None:
                return None
            return (self._usage.started_at + WINDOW).astimezone(self._zone)

    # -- what the client writes ----------------------------------------------

    def charge(
        self, prompt_tokens: int, completion_tokens: int, *, on_credit: bool = False
    ) -> WindowUsage:
        """Add one call's usage to the window and persist it.

        Negative counts are clamped to zero for the same reason
        :meth:`RunContext.record_usage` clamps them: a bad payload must not
        be able to shrink the running total. Zeros — the endpoint omitted
        ``usage`` — add nothing and are not estimated (ADR-031).

        **The first charge starts the window.** Not the first banner, not the
        process start: the first spend. A session opened and closed without
        asking anything leaves the allowance untouched (ADR-089).

        Args:
            prompt_tokens: Prompt tokens billed for the call.
            completion_tokens: Completion tokens billed for the call.
            on_credit: Whether this call was made past the limit, with the
                user's consent. Recorded in its own counter as well as the
                totals, so the report can say how far past.

        Returns:
            The window's usage after the charge.
        """
        # Re-read first so a second session's spend since our last look is
        # included in what we write back. Read, modify and write are one
        # critical section: splitting them is exactly how a concurrent charge
        # gets lost.
        with self._lock:
            self._reload()
            spent = max(0, prompt_tokens) + max(0, completion_tokens)
            self._usage = WindowUsage(
                started_at=self._usage.started_at or self._clock(),
                prompt_tokens=self._usage.prompt_tokens + max(0, prompt_tokens),
                completion_tokens=(
                    self._usage.completion_tokens + max(0, completion_tokens)
                ),
                credit_tokens=self._usage.credit_tokens + (spent if on_credit else 0),
            )
            self._persist()
            return self._usage

    # -- internals -----------------------------------------------------------

    def _now(self) -> datetime:
        """Return the current time, timezone-aware."""
        return datetime.now(self._zone)

    def _reload(self) -> None:
        """Bring the in-memory copy up to date with the file and the clock.

        Order matters: the file is read first, then the window's age is
        checked, so a file left by a window that has since expired is
        discarded rather than added to. When the file cannot be read or is
        not the shape we wrote, the in-memory copy stands — the alternative
        is a crash on every call.
        """
        # Once a write has failed the file is stale by definition: reading it
        # back would overwrite this session's own charges with whatever was
        # there before the mount went read-only.
        if self._path is not None and self._write_error is None:
            stored = self._read()
            if stored is not None:
                self._usage = stored
        started = self._usage.started_at
        if started is not None and self._clock() - started >= WINDOW:
            # Expired. A fresh window, unstarted: the next charge sets its
            # clock, so an idle day does not consume allowance.
            self._usage = WindowUsage()

    def _read(self) -> WindowUsage | None:
        """Return the stored usage, or ``None`` if there is none to trust."""
        assert self._path is not None  # only called for a persisted ledger
        try:
            raw = json.loads(self._path.read_text(encoding="utf-8"))
            if not isinstance(raw, dict):
                raise TypeError(f"expected an object, found {type(raw).__name__}")
            stamp = raw.get("started_at")
            spent = max(0, int(raw["prompt_tokens"])) + max(
                0, int(raw["completion_tokens"])
            )
            if stamp is None and spent:
                # Spend with no window clock: a ledger written before
                # ADR-089, when the file was keyed by calendar day. There is
                # no honest way to continue it — the start time it needs was
                # never recorded — so it is discarded with a note rather
                # than resumed under a start time we invented.
                raise ValueError("no window start; ledger predates ADR-089")
            return WindowUsage(
                started_at=datetime.fromisoformat(str(stamp)) if stamp else None,
                prompt_tokens=max(0, int(raw["prompt_tokens"])),
                completion_tokens=max(0, int(raw["completion_tokens"])),
                credit_tokens=max(0, int(raw.get("credit_tokens", 0))),
            )
        except FileNotFoundError:
            # A fresh volume. Not an error, and not worth a banner note.
            return None
        except (OSError, ValueError, TypeError, KeyError) as exc:
            # Unreadable, or a file from before ADR-089 that is keyed by
            # `day` and has no `started_at`. Either way it is not ours to
            # add to: noted once for the banner, and replaced by the next
            # successful write.
            self._read_error = f"{type(exc).__name__}: {exc}"
            return None

    def _persist(self) -> None:
        """Write the in-memory copy atomically. Never raises."""
        if self._path is None:
            return
        started = self._usage.started_at
        payload = json.dumps(
            {
                "started_at": started.isoformat() if started else None,
                "prompt_tokens": self._usage.prompt_tokens,
                "completion_tokens": self._usage.completion_tokens,
                "credit_tokens": self._usage.credit_tokens,
            }
        )
        # Sibling temp file plus ``os.replace``: on every platform we ship
        # to, the replace is atomic, so a reader sees the old file or the new
        # one and never a truncated one.
        temp = self._path.with_suffix(".json.tmp")
        try:
            # The memory root is created at startup when memory is on; when
            # it is off the budget still wants a home, so the directory is
            # made here, the way the trace hook makes its own.
            self._path.parent.mkdir(parents=True, exist_ok=True)
            temp.write_text(payload, encoding="utf-8")
            os.replace(temp, self._path)
            self._write_error = None
        except OSError as exc:
            self._write_error = f"cannot write {self._path.name}: {exc}"
