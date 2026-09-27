"""The reschedule search: gaps, ranking, bounds, and the DST cases.

The capability, tested without a protocol conversation — ``suggest_slots``
takes events and returns slots and imports nothing from MCP, which is what
makes this file able to be exhaustive (ADR-072).

Every test passes ``now`` explicitly. Nothing here reads the system clock,
so a search over a fixed calendar gives the same answer on any machine on
any day; a test that depended on the real date would start failing on a
Saturday, which is the kind of failure nobody debugs and everybody reruns.

The dates are chosen, not arbitrary. **2026-09-14 is a Monday**, so a
seven-day search from it covers a clean working week, and 2026-10-25 and
2026-03-29 are the two Sundays Europe changes its clocks — the days on which
"09:00 plus eight hours" is not 17:00.
"""

from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pytest
from mcp_server_calendar.formatting import InvalidArgument
from mcp_server_calendar.reschedule import (
    MAX_DAYS_AHEAD,
    parse_working_hours,
    suggest_slots,
)

BERLIN = ZoneInfo("Europe/Berlin")
KARACHI = ZoneInfo("Asia/Karachi")

# A Monday, 08:00 local. Early enough that the whole working day is still
# ahead, which keeps "today" a candidate in most of these tests.
MONDAY_0800 = datetime(2026, 9, 14, 8, 0, tzinfo=BERLIN)

NINE_TO_FIVE = parse_working_hours("09:00-17:00")


def _busy(
    hour: int,
    minute: int = 0,
    minutes: int = 60,
    day: int = 14,
    zone: ZoneInfo = BERLIN,
) -> tuple[datetime, datetime]:
    """Build one occupied interval on 2026-09-<day> at a local wall time.

    A plain ``(start, end)`` pair rather than an event object: since the
    fork, busy time comes out of the upstream client with recurrences
    already expanded, so the search never sees an event — only the hours
    it occupies (ADR-071).
    """
    start = datetime(2026, 9, day, hour, minute, tzinfo=zone)
    return start, start + timedelta(minutes=minutes)


def _local_times(slots: list, zone: ZoneInfo = BERLIN) -> list[str]:
    """Return each suggestion as ``'Mon 10:00'`` for readable assertions."""
    return [slot.start.astimezone(zone).strftime("%a %H:%M") for slot in slots]


# --- finding the gaps ------------------------------------------------------


def test_a_free_day_suggests_the_start_of_the_working_day() -> None:
    """Nothing booked, so the best slot is as early as the rules allow.

    09:00 rather than 08:00: ``now`` is 08:00 and the day is wide open, so
    only the working-hours window stops it suggesting right now.
    """
    slots = suggest_slots(
        duration_minutes=60,
        busy=[],
        now=MONDAY_0800,
        view=BERLIN,
        working_hours=NINE_TO_FIVE,
        max_results=1,
    )

    assert _local_times(slots) == ["Mon 09:00"]


def test_a_booked_morning_is_skipped() -> None:
    """The whole job in one assertion: booked hours are not offered."""
    slots = suggest_slots(
        duration_minutes=60,
        busy=[_busy(9, minutes=120)],  # 09:00-11:00
        now=MONDAY_0800,
        view=BERLIN,
        working_hours=NINE_TO_FIVE,
        max_results=1,
    )

    assert _local_times(slots) == ["Mon 11:00"]


def test_a_fully_booked_day_falls_through_to_the_next() -> None:
    """Monday is solid, so the answer is Tuesday rather than nothing."""
    slots = suggest_slots(
        duration_minutes=60,
        busy=[_busy(9, minutes=480)],  # 09:00-17:00
        now=MONDAY_0800,
        view=BERLIN,
        working_hours=NINE_TO_FIVE,
        max_results=1,
    )

    assert _local_times(slots) == ["Tue 09:00"]


def test_a_time_that_has_already_passed_is_never_suggested() -> None:
    """Half the working day is gone by 13:20, and so are those slots."""
    slots = suggest_slots(
        duration_minutes=60,
        busy=[],
        now=datetime(2026, 9, 14, 13, 20, tzinfo=BERLIN),
        view=BERLIN,
        working_hours=NINE_TO_FIVE,
        max_results=1,
    )

    assert _local_times(slots) == ["Mon 13:30"]


def test_an_impossible_search_returns_nothing_rather_than_a_bad_slot() -> None:
    """Empty is a real answer, and better than a suggestion outside hours."""
    solid = [_busy(9, minutes=480, day=day) for day in range(14, 22)]

    slots = suggest_slots(
        duration_minutes=60,
        busy=solid,
        now=MONDAY_0800,
        view=BERLIN,
        working_hours=NINE_TO_FIVE,
        days_ahead=7,
    )

    assert slots == []


# --- what makes it a *re*schedule -----------------------------------------


# --- the ranking -----------------------------------------------------------


# --- weekends --------------------------------------------------------------


def test_weekends_are_skipped_by_default() -> None:
    """Nobody wants a Saturday suggestion for a work meeting."""
    busy = [_busy(9, minutes=480, day=day) for day in (14, 15, 16, 17, 18)]

    slots = suggest_slots(
        duration_minutes=60,
        busy=busy,
        now=MONDAY_0800,
        view=BERLIN,
        working_hours=NINE_TO_FIVE,
        days_ahead=7,
        max_results=3,
    )
    weekdays = {slot.start.astimezone(BERLIN).weekday() for slot in slots}

    assert weekdays <= {0, 1, 2, 3, 4}


# --- daylight saving -------------------------------------------------------


def test_events_in_a_foreign_zone_still_block_the_right_instants() -> None:
    """Busy time is an instant, not a wall-clock reading.

    A meeting booked 12:00-14:00 Karachi is 09:00-11:00 Berlin, so a Berlin
    working day must show it as the morning being gone.
    """
    slots = suggest_slots(
        duration_minutes=60,
        busy=[_busy(12, minutes=120, zone=KARACHI)],
        now=MONDAY_0800,
        view=BERLIN,
        working_hours=NINE_TO_FIVE,
        max_results=1,
    )

    assert _local_times(slots) == ["Mon 11:00"]


# --- bounds ----------------------------------------------------------------


@pytest.mark.parametrize(
    ("kwargs", "message"),
    [
        ({"duration_minutes": 5}, "duration_minutes must be between"),
        ({"duration_minutes": 1000}, "duration_minutes must be between"),
        ({"days_ahead": 0}, "days_ahead must be between"),
        ({"days_ahead": MAX_DAYS_AHEAD + 1}, "days_ahead must be between"),
        ({"max_results": 0}, "max_results must be between"),
        ({"max_results": 99}, "max_results must be between"),
    ],
)
def test_an_out_of_range_argument_is_refused_by_name(
    kwargs: dict[str, int], message: str
) -> None:
    """Bounds are what make the capability *bounded* (the brief's word).

    Each message names the argument and its range, so a model that guessed
    can correct itself in one turn instead of retrying identically until
    the no-progress guard stops it (ADR-049).
    """
    call = {
        "duration_minutes": 60,
        "busy": [],
        "now": MONDAY_0800,
        "view": BERLIN,
        "working_hours": NINE_TO_FIVE,
    }
    call.update(kwargs)
    with pytest.raises(InvalidArgument, match=message):
        suggest_slots(**call)  # type: ignore[arg-type]


# --- working hours ---------------------------------------------------------


@pytest.mark.parametrize(
    ("text", "message"),
    [
        ("09:00", "must look like"),
        ("09:00-12:00-15:00", "must look like"),
        ("9-5", "not a time of day"),
        ("25:00-26:00", "not a time of day"),
        ("17:00-09:00", "must start before it ends"),
        ("09:00-09:00", "must start before it ends"),
        ("03:00-11:00", "must sit inside"),
        ("09:00-23:30", "must sit inside"),
    ],
)
def test_a_bad_working_hours_string_is_refused_with_the_reason(
    text: str, message: str
) -> None:
    """Eight ways to write it wrong, each answered with what was expected.

    The outer 06:00-22:00 bound is the interesting pair: without it a
    caller could ask for 00:00-23:59 and get a 03:00 suggestion presented
    with exactly the same confidence as a 10:00 one.
    """
    with pytest.raises(InvalidArgument, match=message):
        parse_working_hours(text)
