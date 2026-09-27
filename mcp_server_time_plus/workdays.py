"""Working-day arithmetic over a date range. Pure functions, no MCP imports.

The second capability this server adds on top of the reference
``mcp-server-time`` fork, and the same split as
:mod:`mcp_server_time_plus.meeting`: this module is the capability,
:mod:`mcp_server_time_plus.server` is the interface. Nothing here knows what
MCP is, so all of it is tested by calling straight into it.

Where :func:`~mcp_server_time_plus.meeting.find_slots` answers "when in the
*day* can we meet", this answers "how many *days* of work are there between
two dates" — deadlines, notice periods, delivery estimates, "is a fortnight
enough". It is the question anybody planning anything asks, and it is one
people reliably get wrong by hand, because the answer depends on three
things at once: which days count as the weekend, which holidays fall inside
the range, and whether the end date is included.

Two deliberate absences.

**No timezone.** A calendar day is not an instant, and a working day even
less so: "three business days from Monday" means the same thing in Lahore
and in Lisbon. Introducing a zone here would let the same question give two
answers depending on the hour it was asked, which is worse than useless in
a planning tool. :func:`~mcp_server_time_plus.meeting.find_slots` handles
the case where zones genuinely matter.

**No built-in holiday calendar.** Public holidays are per country, per
region, and per year, and any list shipped inside a container is wrong the
moment it goes stale — silently, and in the direction that makes a deadline
look further away than it is. Holidays are a caller-supplied argument, so
the answer is only ever as good as an input the caller can see and correct.

Nothing here reads the system clock, so a question about a given range
returns the same answer on any machine on any day.
"""

from datetime import date as date_type
from datetime import timedelta
from typing import Any

from mcp_server_time_plus.meeting import InvalidArgument, parse_date

# Bounds, mirrored into the JSON Schema so the model is told them rather
# than discovering them through a failed call — the same policy as
# ``meeting.py``.
MAX_RANGE_DAYS = 3660  # about ten years
MAX_HOLIDAYS = 100
MAX_WEEKEND_DAYS = 6  # a seven-day weekend is an empty question, not a week

# Monday-first, matching ``date.weekday()``. Lower-cased on lookup, so
# "Saturday", "saturday" and "SATURDAY" all work; models are not consistent
# about case and rejecting one spelling teaches nothing.
_WEEKDAY_NAMES = (
    "monday",
    "tuesday",
    "wednesday",
    "thursday",
    "friday",
    "saturday",
    "sunday",
)

# The Saturday/Sunday default is a default, not an assumption baked into the
# arithmetic: several countries run Friday/Saturday, and a four-day week is
# an ordinary arrangement now. Both are expressed by passing ``weekend``.
DEFAULT_WEEKEND = ("saturday", "sunday")


def _weekday_index(name: str) -> int:
    """Resolve a weekday name to ``date.weekday()``'s 0-6, Monday first.

    Args:
        name: An English weekday name, in any case.

    Raises:
        InvalidArgument: If it is not a weekday name. The message lists what
            is accepted, because the model's next move is to correct it.
    """
    try:
        return _WEEKDAY_NAMES.index(name.strip().lower())
    except ValueError as exc:
        raise InvalidArgument(
            f"{name!r} is not a weekday name (expected one of "
            f"{', '.join(n.capitalize() for n in _WEEKDAY_NAMES)})."
        ) from exc


def _validate_weekend(names: list[str] | None) -> frozenset[int]:
    """Turn the weekend argument into the set of weekday numbers to skip.

    An explicitly empty list is honoured and means "every day is a working
    day", which is a real answer for a shift rota or a countdown to an
    event. It is distinct from the argument being omitted, which takes the
    Saturday/Sunday default.
    """
    if names is None:
        names = list(DEFAULT_WEEKEND)
    if len(names) > MAX_WEEKEND_DAYS:
        raise InvalidArgument(
            f"weekend can name at most {MAX_WEEKEND_DAYS} days; naming all "
            f"seven would leave no working days at all."
        )
    return frozenset(_weekday_index(name) for name in names)


def _validate_holidays(values: list[str] | None) -> list[date_type]:
    """Parse the holiday list, rejecting duplicates.

    Duplicates are refused rather than collapsed, on the same reasoning as
    the duplicate timezones in ``meeting.py``: a repeated date is a mistake
    in the caller's list, and quietly answering a different question than
    the one asked is worse than saying no. Order is not required — the list
    is sorted on the way out — because a caller assembling holidays from
    two sources should not have to merge them by hand.
    """
    if values is None:
        return []
    if len(values) > MAX_HOLIDAYS:
        raise InvalidArgument(
            f"holidays can hold at most {MAX_HOLIDAYS} dates (got {len(values)})."
        )
    parsed: list[date_type] = []
    seen: set[date_type] = set()
    for value in values:
        day = parse_date(value)
        if day in seen:
            raise InvalidArgument(f"holidays lists {value!r} more than once.")
        seen.add(day)
        parsed.append(day)
    return sorted(parsed)


def count_working_days(
    start_date: str,
    end_date: str,
    weekend: list[str] | None = None,
    holidays: list[str] | None = None,
    include_end_date: bool = True,
) -> dict[str, Any]:
    """Count the working days between two calendar dates.

    Args:
        start_date: First day of the range, ``YYYY-MM-DD``.
        end_date: Last day of the range, ``YYYY-MM-DD``. Included unless
            ``include_end_date`` is false.
        weekend: Weekday names that do not count as working days. Defaults
            to Saturday and Sunday. An empty list means a seven-day week.
        holidays: Dates that do not count as working days, ``YYYY-MM-DD``.
            Dates outside the range, or already falling on a weekend, are
            reported separately rather than silently ignored.
        include_end_date: Whether ``end_date`` itself counts. True suits
            "how long is this project"; false suits "how long until the
            deadline", where the deadline day is the target, not part of the
            wait.

    Returns:
        A JSON-safe dict with the counts, the effective settings, the
        holidays that actually reduced the total, and the first and last
        working day in the range.

    Raises:
        InvalidArgument: On any unparseable or out-of-bounds argument,
            including an end date before the start date.
    """
    start = parse_date(start_date)
    end = parse_date(end_date)
    weekend_indices = _validate_weekend(weekend)
    holiday_dates = _validate_holidays(holidays)

    if end < start:
        raise InvalidArgument(
            f"end_date {end_date!r} is before start_date {start_date!r}. "
            f"Swap them, or check which way round the range runs."
        )

    last = end if include_end_date else end - timedelta(days=1)
    total_days = (last - start).days + 1
    if total_days > MAX_RANGE_DAYS:
        raise InvalidArgument(
            f"the range covers {total_days} days and the limit is "
            f"{MAX_RANGE_DAYS}. Ask about a shorter period."
        )

    # An excluded end date can empty the range: start == end with
    # include_end_date false is a legitimate "no days at all", not an error.
    if total_days < 1:
        return {
            "start_date": start.isoformat(),
            "end_date": end.isoformat(),
            "include_end_date": include_end_date,
            "weekend": sorted(_WEEKDAY_NAMES[i] for i in weekend_indices),
            "total_days": 0,
            "working_days": 0,
            "weekend_days": 0,
            "holidays_excluded": [],
            "holidays_ignored": [day.isoformat() for day in holiday_dates],
            "first_working_day": None,
            "last_working_day": None,
        }

    in_range = {day for day in holiday_dates if start <= day <= last}

    working: list[date_type] = []
    weekend_days = 0
    holidays_taken: list[date_type] = []
    for offset in range(total_days):
        day = start + timedelta(days=offset)
        if day.weekday() in weekend_indices:
            weekend_days += 1
            continue
        if day in in_range:
            # Counted here rather than by set size: a holiday that lands on
            # a Sunday costs nobody a working day, and reporting it as one
            # is exactly the error people make doing this by hand.
            holidays_taken.append(day)
            continue
        working.append(day)

    return {
        "start_date": start.isoformat(),
        "end_date": end.isoformat(),
        "include_end_date": include_end_date,
        "weekend": sorted(_WEEKDAY_NAMES[i] for i in weekend_indices),
        "total_days": total_days,
        "working_days": len(working),
        "weekend_days": weekend_days,
        "holidays_excluded": [day.isoformat() for day in holidays_taken],
        "holidays_ignored": [
            day.isoformat() for day in holiday_dates if day not in set(holidays_taken)
        ],
        "first_working_day": working[0].isoformat() if working else None,
        "last_working_day": working[-1].isoformat() if working else None,
    }


__all__ = [
    "DEFAULT_WEEKEND",
    "MAX_HOLIDAYS",
    "MAX_RANGE_DAYS",
    "MAX_WEEKEND_DAYS",
    "count_working_days",
]
