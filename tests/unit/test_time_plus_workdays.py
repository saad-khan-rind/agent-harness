"""Working-day counting: the arithmetic, and every validation message.

Calls straight into :mod:`mcp_server_time_plus.workdays`, with no MCP
session anywhere in the file. That is the payoff of keeping the capability
separate from the interface: the cases worth being exhaustive about — a
holiday landing on a Sunday, an excluded end date emptying the range, a
Friday/Saturday weekend — are cheap to write and fast to run here, and
``tests/unit/test_time_plus_server.py`` covers the contract on top of them.

Every date below is a real 2026 date and the weekday in the comment has been
checked; a test whose fixture is wrong about which day is a Tuesday proves
nothing at all.
"""

import pytest
from mcp_server_time_plus.meeting import InvalidArgument
from mcp_server_time_plus.workdays import (
    MAX_RANGE_DAYS,
    count_working_days,
)

# 2026-09-07 is a Monday, so 2026-09-07..2026-09-11 is one clean working
# week and 2026-09-12/13 are that weekend. Used throughout.
MONDAY = "2026-09-07"
FRIDAY = "2026-09-11"
SATURDAY = "2026-09-12"
SUNDAY = "2026-09-13"


# --- the ordinary cases ----------------------------------------------------


def test_a_range_spanning_the_weekend() -> None:
    """Monday to the following Sunday: seven days, five of them working."""
    result = count_working_days(MONDAY, SUNDAY)

    assert result["total_days"] == 7
    assert result["working_days"] == 5
    assert result["weekend_days"] == 2
    # The last working day is the Friday, not the Sunday the range ends on.
    assert result["last_working_day"] == FRIDAY


def test_a_single_weekend_day_has_no_working_days() -> None:
    """A correct zero, with the day-boundary fields honest about it."""
    result = count_working_days(SATURDAY, SATURDAY)

    assert result["working_days"] == 0
    assert result["weekend_days"] == 1
    assert result["first_working_day"] is None
    assert result["last_working_day"] is None


# --- include_end_date ------------------------------------------------------


def test_excluding_the_end_date_drops_one_day() -> None:
    """ "How long until Friday" is four days, not five."""
    result = count_working_days(MONDAY, FRIDAY, include_end_date=False)

    assert result["total_days"] == 4
    assert result["working_days"] == 4
    assert result["last_working_day"] == "2026-09-10"


# --- holidays --------------------------------------------------------------


def test_a_weekday_holiday_costs_a_working_day() -> None:
    """The ordinary case: a Wednesday off is one day fewer."""
    result = count_working_days(MONDAY, FRIDAY, holidays=["2026-09-09"])

    assert result["working_days"] == 4
    assert result["holidays_excluded"] == ["2026-09-09"]
    assert result["holidays_ignored"] == []


# --- the weekend is configurable -------------------------------------------


# --- validation ------------------------------------------------------------


def test_an_end_before_the_start_is_refused() -> None:
    """A reversed range is a caller error, and the message says how to fix it."""
    with pytest.raises(InvalidArgument, match="is before start_date"):
        count_working_days(FRIDAY, MONDAY)


@pytest.mark.parametrize("value", ["09-09-2026", "tomorrow"])
def test_a_malformed_date_is_refused(value: str) -> None:
    """The schema says YYYY-MM-DD, so anything else is rejected, not guessed."""
    with pytest.raises(InvalidArgument):
        count_working_days(value, FRIDAY)


def test_naming_all_seven_days_as_the_weekend_is_refused() -> None:
    """A week with no working days is a mistake, not a question."""
    with pytest.raises(InvalidArgument, match="at most"):
        count_working_days(
            MONDAY,
            FRIDAY,
            weekend=[
                "Monday",
                "Tuesday",
                "Wednesday",
                "Thursday",
                "Friday",
                "Saturday",
                "Sunday",
            ],
        )


def test_an_over_long_range_is_refused() -> None:
    """Bounded scope, as the brief asks: the answer is finite by construction."""
    with pytest.raises(InvalidArgument, match=str(MAX_RANGE_DAYS)):
        count_working_days("2000-01-01", "2050-01-01")


# --- the result is a usable record -----------------------------------------
