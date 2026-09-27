"""Finding somewhere else to put a meeting. Pure functions, no MCP imports.

This is the capability the forked server does not have, and the one the
brief asks for: *clearly described* and *bounded* (ADR-072).

Upstream `ics-calendar-mcp` can list and search events and expand their
recurrences. What it cannot do is answer the question those queries are
usually asked in service of — *when am I free?* — because it has no notion
of busy time at all. That is the gap this fills, and it is the same shape of
gap `find_meeting_slot` fills in the forked time server.

**The problem.** An event conflicts with something. Given everything else on
the calendar for the next few days, where could it go instead? A human does
this by looking at a week view and picking a gap that is not stupid — not
7am, not next Friday when the thing is tomorrow, and preferably around the
time it was already at, because a 9am standup moved to 4pm is a different
meeting.

**Why it is not `find_meeting_slot` again.** ``mcp_server_time_plus`` already
answers "when in a day are these people all at work", and it does that with
no idea what is on anyone's calendar — it intersects working hours across
timezones. This answers "when is this person actually free", which needs the
events. The two compose: the time server narrows to hours that exist for
everybody, this one removes the hours already spoken for. Neither subsumes
the other, which is what keeps them separate servers rather than one
drawer of scheduling utilities.

**Scoring, stated plainly.** A tool that returns three options has to say why
those three, or the ranking is a black box the user cannot argue with::

    soonness = 0.5 ** (days_from_now / 3)
    affinity = 1 - |candidate_time_of_day - original_time_of_day| / 6h
    score    = 0.6 * soonness + 0.4 * affinity

Both terms land in ``[0, 1]`` and the weights read directly: sooner matters
somewhat more than keeping the hour. The half-life of three days is short on
purpose — a reschedule that drifts a fortnight out has not solved the
problem it was asked to solve.

**One suggestion per day, best first.** Three options inside the same
morning are not three options; they are one option with rounding error. The
search takes each day's best candidate first and only then backfills from
whatever is left, so three results usually mean three genuinely different
days to choose between.

Time is never read from the system clock in this module: the caller passes
``now``, so a search over a fixed calendar returns the same answer on any
machine on any day, which is what makes the tests deterministic.
"""

from dataclasses import dataclass
from datetime import date as date_type
from datetime import datetime, timedelta
from datetime import time as time_type
from typing import Any
from zoneinfo import ZoneInfo

from mcp_server_calendar.formatting import InvalidArgument

# Candidate starts sit on a quarter-hour grid, the same grid
# ``mcp_server_time_plus`` searches on. Anything finer suggests 10:07 as a
# meeting time, which no one accepts.
GRID_MINUTES = 15

# Bounds, mirrored into the JSON Schema so the model is told them rather
# than discovering them through a failed call.
MIN_DAYS_AHEAD = 1
MAX_DAYS_AHEAD = 14
MIN_DURATION_MINUTES = 15
MAX_DURATION_MINUTES = 480
MIN_RESULTS = 1
MAX_RESULTS = 10

# The outer limits of a working-hours window. Not a preference — a bound.
# Without it, a caller could ask for 00:00-23:59 and get a 3am suggestion
# presented with the same confidence as a 10am one.
EARLIEST_HOUR = 6
LATEST_HOUR = 22

DEFAULT_WORKING_HOURS = "09:00-17:00"
DEFAULT_DAYS_AHEAD = 7
DEFAULT_RESULTS = 3

# How the two halves of the score are weighted. Sooner matters more than
# keeping the original hour, but not overwhelmingly: at equal soonness the
# affinity term is what separates a sensible suggestion from a technically
# free one.
SOONNESS_WEIGHT = 0.6
AFFINITY_WEIGHT = 0.4

# Half-life of the soonness term, in days.
SOONNESS_HALF_LIFE_DAYS = 3.0

# How far from the original time of day still counts as "around then".
# Six hours, so a 09:00 meeting scores zero affinity at 15:00 and after —
# by then it is a different slot in the day whatever the clock says.
AFFINITY_SPAN_HOURS = 6.0

_MINUTES_PER_DAY = 24 * 60


@dataclass(frozen=True)
class Suggestion:
    """One proposed slot, with the numbers that put it where it is."""

    start: datetime
    end: datetime
    score: float
    soonness: float
    affinity: float

    def as_dict(self, view: ZoneInfo) -> dict[str, Any]:
        """Render the suggestion in ``view``, for the model to read.

        Both component scores are kept, not just the total. With one number
        "why is that first" has no answer short of re-deriving it, and this
        is a tool whose whole output is a ranking.
        """
        local_start = self.start.astimezone(view)
        local_end = self.end.astimezone(view)
        return {
            "start": local_start.isoformat(timespec="minutes"),
            "end": local_end.isoformat(timespec="minutes"),
            "day": local_start.strftime("%A %Y-%m-%d"),
            "local_time": (
                f"{local_start.strftime('%H:%M')}-{local_end.strftime('%H:%M')}"
            ),
            "score": round(self.score, 3),
            "why": {
                "soonness": round(self.soonness, 3),
                "time_of_day_match": round(self.affinity, 3),
            },
        }


@dataclass(frozen=True)
class WorkingHours:
    """The daily window a suggestion must fall inside."""

    start: time_type
    end: time_type

    @property
    def label(self) -> str:
        """Return the window as it was written, for echoing back."""
        return f"{self.start.strftime('%H:%M')}-{self.end.strftime('%H:%M')}"


def parse_working_hours(text: str) -> WorkingHours:
    """Parse ``"09:00-17:00"`` into a validated window.

    Raises:
        InvalidArgument: On a malformed string, an inverted window, or one
            reaching outside :data:`EARLIEST_HOUR`-:data:`LATEST_HOUR`.
    """
    raw = text.strip()
    if raw.count("-") != 1:
        raise InvalidArgument(
            f"working_hours must look like '09:00-17:00' (got {text!r})"
        )
    first, second = (part.strip() for part in raw.split("-"))
    start, end = _parse_clock(first), _parse_clock(second)
    if start >= end:
        raise InvalidArgument(f"working_hours must start before it ends (got {text!r})")
    if start.hour < EARLIEST_HOUR or (end.hour, end.minute) > (LATEST_HOUR, 0):
        raise InvalidArgument(
            f"working_hours must sit inside "
            f"{EARLIEST_HOUR:02d}:00-{LATEST_HOUR:02d}:00 (got {text!r})"
        )
    return WorkingHours(start=start, end=end)


def suggest_slots(
    duration_minutes: int,
    busy: list[tuple[datetime, datetime]],
    now: datetime,
    view: ZoneInfo,
    working_hours: WorkingHours,
    days_ahead: int = DEFAULT_DAYS_AHEAD,
    max_results: int = DEFAULT_RESULTS,
    include_weekends: bool = False,
    original_start: datetime | None = None,
    original_end: datetime | None = None,
) -> list[Suggestion]:
    """Return the best free slots for a meeting of ``duration_minutes``.

    Args:
        duration_minutes: Length of the meeting to place.
        busy: Occupied ``(start, end)`` intervals, recurrences already
            expanded by the client. The event being moved must already have
            been removed by the caller — leaving it in would make its own
            slot look occupied, so the search would be reasoning about a
            conflict the event has with itself.
        now: The instant the search starts from. Passed, never read from
            the clock, so the result is reproducible.
        view: Zone the working hours are expressed in and the results are
            reported in.
        working_hours: The daily window.
        days_ahead: How many days from ``now`` to search.
        max_results: How many suggestions to return.
        include_weekends: Whether Saturday and Sunday are candidates.
        original_start: Where the event is now, used for the time-of-day
            term. ``None`` drops that term and ranks on soonness alone,
            which is the right answer when there is no "original" — the
            caller is placing a new meeting, not moving one.
        original_end: End of the event's current slot. Given both ends, no
            candidate overlapping that slot is proposed: the tool was asked
            for somewhere *else*, and "leave it where it is" is not an
            alternative. Without this the first suggestion is frequently
            the event's own time, because the event was removed from
            ``busy`` and nothing else was blocking it.

    Returns:
        Up to ``max_results`` suggestions, best first, at most one per day
        until the days run out.

    Raises:
        InvalidArgument: If any bound is exceeded.
    """
    _require_range(
        "duration_minutes", duration_minutes, MIN_DURATION_MINUTES, MAX_DURATION_MINUTES
    )
    _require_range("days_ahead", days_ahead, MIN_DAYS_AHEAD, MAX_DAYS_AHEAD)
    _require_range("max_results", max_results, MIN_RESULTS, MAX_RESULTS)

    length = timedelta(minutes=duration_minutes)
    if length > _window_length(working_hours):
        raise InvalidArgument(
            f"a {duration_minutes}-minute meeting does not fit inside "
            f"working hours of {working_hours.label}"
        )

    target_minute = (
        _minute_of_day(original_start.astimezone(view)) if original_start else None
    )

    keep_clear = (
        (original_start, original_end)
        if original_start is not None and original_end is not None
        else None
    )

    by_day: dict[date_type, list[Suggestion]] = {}
    for offset in range(days_ahead + 1):
        day = (now.astimezone(view) + timedelta(days=offset)).date()
        if not include_weekends and day.weekday() >= 5:
            continue
        candidates = _candidates_on(
            day=day,
            length=length,
            busy=busy,
            now=now,
            view=view,
            working_hours=working_hours,
            target_minute=target_minute,
            keep_clear=keep_clear,
        )
        if candidates:
            by_day[day] = candidates

    return _spread_across_days(by_day, max_results)


def _candidates_on(
    day: date_type,
    length: timedelta,
    busy: list[tuple[datetime, datetime]],
    now: datetime,
    view: ZoneInfo,
    working_hours: WorkingHours,
    target_minute: int | None,
    keep_clear: tuple[datetime, datetime] | None = None,
) -> list[Suggestion]:
    """Return every free slot on one day, best first.

    The whole search happens on absolute instants. Local clock readings are
    produced by converting, never by arithmetic on offsets — the same rule
    ``mcp_server_time_plus`` follows, and for the same reason: a day that
    contains a DST transition is 23 or 25 hours long, and "09:00 plus eight
    hours" is not 17:00 on those two days a year.
    """
    window_start = datetime.combine(day, working_hours.start, tzinfo=view)
    window_end = datetime.combine(day, working_hours.end, tzinfo=view)
    # Never suggest a time that has already passed. Rounded up to the grid
    # so the first suggestion of today is a real clock time, not "in four
    # minutes".
    floor = max(window_start, _ceil_to_grid(now.astimezone(view)))

    found: list[Suggestion] = []
    cursor = floor
    while cursor + length <= window_end:
        finish = cursor + length
        blocked = any(
            begins < finish and cursor < finishes for begins, finishes in busy
        )
        if keep_clear is not None and cursor < keep_clear[1] and keep_clear[0] < finish:
            # The event's own slot. Proposing it back would answer a
            # different question than the one asked.
            blocked = True
        if not blocked:
            soonness, affinity = _score_parts(
                start=cursor, now=now, view=view, target_minute=target_minute
            )
            found.append(
                Suggestion(
                    start=cursor,
                    end=finish,
                    score=SOONNESS_WEIGHT * soonness + AFFINITY_WEIGHT * affinity,
                    soonness=soonness,
                    affinity=affinity,
                )
            )
        cursor += timedelta(minutes=GRID_MINUTES)

    found.sort(key=lambda slot: (-slot.score, slot.start))
    return found


def _score_parts(
    start: datetime,
    now: datetime,
    view: ZoneInfo,
    target_minute: int | None,
) -> tuple[float, float]:
    """Return ``(soonness, affinity)`` for one candidate start.

    Affinity is 1.0 when there is no original time to match, rather than
    0.0. Zero would mean "as bad as possible on a term that does not
    apply", which would drag every candidate down equally and change the
    absolute scores for no reason; 1.0 leaves the ranking to soonness
    alone, which is what "no preferred hour" should mean.
    """
    days_out = max(0.0, (start - now).total_seconds() / 86_400.0)
    soonness = 0.5 ** (days_out / SOONNESS_HALF_LIFE_DAYS)
    if target_minute is None:
        return soonness, 1.0
    drift_hours = abs(_minute_of_day(start.astimezone(view)) - target_minute) / 60.0
    affinity = max(0.0, 1.0 - drift_hours / AFFINITY_SPAN_HOURS)
    return soonness, affinity


def _spread_across_days(
    by_day: dict[date_type, list[Suggestion]], max_results: int
) -> list[Suggestion]:
    """Take each day's best first, then backfill from what is left.

    Two passes rather than one global sort, and the difference is the whole
    point: a global sort returns 09:00, 09:15 and 09:30 tomorrow, which is
    one option presented three times. This returns tomorrow, then the day
    after, then the day after that — three choices a person can actually
    weigh — and only falls back to second-best-on-a-day when there are not
    enough days with any free time at all.
    """
    picked: list[Suggestion] = []
    for slots in by_day.values():
        picked.append(slots[0])
    picked.sort(key=lambda slot: (-slot.score, slot.start))

    if len(picked) >= max_results:
        return picked[:max_results]

    chosen = list(picked)
    leftovers = [slot for slots in by_day.values() for slot in slots[1:]]
    leftovers.sort(key=lambda slot: (-slot.score, slot.start))
    for slot in leftovers:
        if len(chosen) >= max_results:
            break
        # Skip anything butting up against a slot already chosen: two
        # suggestions fifteen minutes apart are not two suggestions.
        if any(_too_close(slot, other) for other in chosen):
            continue
        chosen.append(slot)
    chosen.sort(key=lambda slot: (-slot.score, slot.start))
    return chosen[:max_results]


def _too_close(left: Suggestion, right: Suggestion) -> bool:
    """Return whether two suggestions overlap or nearly touch."""
    gap = timedelta(minutes=GRID_MINUTES)
    return left.start < right.end + gap and right.start < left.end + gap


def _window_length(working_hours: WorkingHours) -> timedelta:
    """Return how long the daily working window is."""
    return timedelta(
        minutes=_minute_of_time(working_hours.end)
        - _minute_of_time(working_hours.start)
    )


def _minute_of_day(moment: datetime) -> int:
    """Return minutes since local midnight."""
    return moment.hour * 60 + moment.minute


def _minute_of_time(value: time_type) -> int:
    """Return minutes since midnight for a wall-clock time."""
    return value.hour * 60 + value.minute


def _ceil_to_grid(moment: datetime) -> datetime:
    """Round ``moment`` up to the next quarter hour."""
    trimmed = moment.replace(second=0, microsecond=0)
    remainder = trimmed.minute % GRID_MINUTES
    if remainder == 0 and trimmed == moment:
        return trimmed
    return trimmed + timedelta(minutes=GRID_MINUTES - remainder)


def _parse_clock(text: str) -> time_type:
    """Parse ``HH:MM`` into a time.

    Raises:
        InvalidArgument: If it is not a 24-hour clock reading.
    """
    try:
        parsed = datetime.strptime(text, "%H:%M")
    except ValueError as exc:
        raise InvalidArgument(
            f"{text!r} is not a time of day in HH:MM form (try '09:00')"
        ) from exc
    return parsed.time()


def _require_range(field: str, value: int, low: int, high: int) -> None:
    """Reject a value outside its published bounds."""
    if not isinstance(value, int) or isinstance(value, bool):
        raise InvalidArgument(f"{field} must be a whole number (got {value!r})")
    if value < low or value > high:
        raise InvalidArgument(f"{field} must be between {low} and {high} (got {value})")
