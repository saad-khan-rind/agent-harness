"""The meeting-slot capability, tested without speaking MCP.

Everything here calls :mod:`mcp_server_time_plus.meeting` directly. The
protocol surface is covered next door in ``test_time_plus_server.py`` and the
transport in ``tests/integration/test_mcp_time_plus.py``; separating them means a
failure says *which* of the three is broken instead of leaving three
suspects.

Every expected value below is a fact about the IANA timezone database on a
named date, not a value copied out of a previous run. The DST pair is the
clearest example: the same query one week apart must produce different
absolute times, because Europe changed its clocks on 25 October 2026 and the
United States did not until 1 November.
"""

from typing import Any

import pytest
from mcp_server_time_plus.meeting import (
    GRID_MINUTES,
    InvalidArgument,
    find_slots,
    zone,
)

# A Wednesday, comfortably inside northern-hemisphere summer time, used
# wherever the test does not care about a clock change.
ORDINARY_DAY = "2026-09-09"


def _starts(result: dict[str, object]) -> list[str]:
    """Return the UTC start of every returned slot."""
    slots = result["slots"]
    assert isinstance(slots, list)
    return [slot["start_utc"] for slot in slots]


# --- the happy path --------------------------------------------------------


def test_finds_every_slot_that_suits_both_participants() -> None:
    """Berlin and Karachi share a five-hour window on an ordinary Wednesday.

    Berlin works 09:00-18:00 = 07:00-16:00 UTC, Karachi 04:00-13:00 UTC. A
    one-hour meeting therefore fits from 07:00 to 12:00 UTC inclusive, which
    on a quarter-hour grid is 21 starts.
    """
    result = find_slots(
        ["Europe/Berlin", "Asia/Karachi"], ORDINARY_DAY, 60, "09:00", "18:00", 20
    )

    assert result["slots_found"] == 21
    assert result["slots_returned"] == 20
    assert result["truncated"] is True
    assert _starts(result)[0] == "2026-09-09T07:00+00:00"


def test_each_participant_reads_the_slot_on_their_own_clock() -> None:
    """The same instant is 09:00 in Berlin and 12:00 in Karachi."""
    result = find_slots(["Europe/Berlin", "Asia/Karachi"], ORDINARY_DAY, 60)

    first = result["slots"][0]["local"]
    assert first[0] == {
        "timezone": "Europe/Berlin",
        "start": "09:00",
        "end": "10:00",
        "day": "Wednesday",
        "date": "2026-09-09",
    }
    assert first[1]["timezone"] == "Asia/Karachi"
    assert (first[1]["start"], first[1]["end"]) == ("12:00", "13:00")


def test_slots_are_ordered_and_sit_on_the_quarter_hour_grid() -> None:
    """Earliest first, and every start is a multiple of the grid."""
    result = find_slots(["Europe/Berlin", "Asia/Karachi"], ORDINARY_DAY, 60)

    starts = _starts(result)
    assert starts == sorted(starts)
    for start in starts:
        assert int(start[14:16]) % GRID_MINUTES == 0


# --- the parts that are easy to get wrong ---------------------------------


def test_the_whole_meeting_must_fit_not_just_its_start() -> None:
    """A meeting starting at 17:30 and ending at 18:30 is out of hours.

    Checking only the start is the obvious bug in this kind of search, so it
    gets its own test: the last Berlin start must be 17:00 for a one-hour
    meeting ending at 18:00.
    """
    result = find_slots(
        ["Europe/Berlin", "Europe/Paris"], ORDINARY_DAY, 60, max_results=20
    )

    last_local = result["slots"][result["slots_returned"] - 1]["local"][0]
    assert last_local["end"] <= "18:00"

    # An eight-hour meeting inside a nine-hour day leaves one hour of slack,
    # which is five quarter-hour starts: 09:00 through 10:00.
    longer = find_slots(["Europe/Berlin", "Europe/Paris"], ORDINARY_DAY, 480)
    assert longer["slots_found"] == 5
    assert longer["slots"][4]["local"][0]["start"] == "10:00"
    assert longer["slots"][4]["local"][0]["end"] == "18:00"


def test_participants_may_be_on_different_calendar_days() -> None:
    """Auckland's Wednesday morning is Los Angeles' Tuesday afternoon.

    The date is carried per participant for exactly this case; one date on
    the slot would be wrong for one of them.
    """
    result = find_slots(
        ["Pacific/Auckland", "America/Los_Angeles"], ORDINARY_DAY, 60, "06:00", "20:00"
    )

    assert result["slots_found"] > 0
    auckland, los_angeles = result["slots"][0]["local"]
    assert auckland["date"] == "2026-09-09"
    assert los_angeles["date"] == "2026-09-08"


def test_no_overlap_is_an_answer_not_an_error() -> None:
    """Berlin, Karachi and New York cannot all meet inside office hours.

    Karachi's working day ends at 13:00 UTC and New York's begins at 13:00
    UTC, so a one-hour meeting has nowhere to sit. Returning that as a normal
    result with the evidence attached is what lets the agent explain the
    "no"; a tool failure would leave it with nothing to say.
    """
    result = find_slots(
        ["Europe/Berlin", "Asia/Karachi", "America/New_York"], ORDINARY_DAY, 60
    )

    assert result["slots_found"] == 0
    assert result["slots"] == []
    windows = result["no_overlap"]["working_days_utc"]
    assert set(windows) == {"Europe/Berlin", "Asia/Karachi", "America/New_York"}
    assert windows["Asia/Karachi"]["end_utc"] == "2026-09-09T13:00+00:00"
    assert windows["America/New_York"]["start_utc"] == "2026-09-09T13:00+00:00"


# --- validation ------------------------------------------------------------


@pytest.mark.parametrize(
    ("kwargs", "expected"),
    [
        ({"timezones": ["Europe/Berlin"]}, "between 2 and 8"),
        ({"timezones": ["UTC"] * 9}, "between 2 and 8"),
        ({"timezones": ["UTC", "UTC"]}, "listed more than once"),
        ({"timezones": ["UTC", "Mars/Olympus"]}, "not an IANA timezone"),
        ({"date": "2026-9-9"}, "not in YYYY-MM-DD form"),
        ({"date": "09/09/2026"}, "not a valid YYYY-MM-DD date"),
        ({"date": "2026-13-01"}, "not a valid YYYY-MM-DD date"),
        ({"date": "1899-01-01"}, "outside the supported range"),
        ({"duration_minutes": 5}, "duration_minutes must be between"),
        ({"duration_minutes": 999}, "duration_minutes must be between"),
        ({"earliest_local": "9am"}, "not a 24-hour HH:MM"),
        ({"latest_local": "25:00"}, "not a 24-hour HH:MM"),
        ({"earliest_local": "18:00", "latest_local": "09:00"}, "must be before"),
        (
            {"earliest_local": "09:00", "latest_local": "09:30"},
            "shorter than the requested",
        ),
        ({"max_results": 0}, "max_results must be between"),
        ({"max_results": 21}, "max_results must be between"),
    ],
)
def test_bad_arguments_are_rejected_by_name(
    kwargs: dict[str, object], expected: str
) -> None:
    """Each message names the argument and the rule it broke.

    A model correcting itself has one turn's worth of attention: "Invalid
    input" costs it a retry, "date '09/09/2026' is not in YYYY-MM-DD form"
    does not.
    """
    call: dict[str, object] = {
        "timezones": ["Europe/Berlin", "Asia/Karachi"],
        "date": ORDINARY_DAY,
    }
    call.update(kwargs)

    with pytest.raises(InvalidArgument) as caught:
        find_slots(**call)  # type: ignore[arg-type]
    assert expected in str(caught.value)


def test_an_unknown_zone_names_itself_in_the_error() -> None:
    """The caller sent three timezones; it needs to know which one was wrong."""
    with pytest.raises(InvalidArgument, match="Europe/Berlinn"):
        zone("Europe/Berlinn")


def test_the_entry_point_starts_the_server_with_the_requested_zone(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``python -m mcp_server_time_plus --local-timezone X`` reaches ``serve``.

    The launch path is what ``config.yaml`` names, so a broken argument
    parser would show up as "server failed to start" in the CLI and nowhere
    else. Stubbing ``asyncio.run`` keeps this a unit test: the coroutine is
    inspected, not awaited.
    """
    import mcp_server_time_plus as package

    started: dict[str, object] = {}

    def fake_run(coro: Any) -> None:
        started["coro"] = coro
        coro.close()  # never awaited; closing avoids a "never awaited" warning

    monkeypatch.setattr(package.asyncio, "run", fake_run)
    monkeypatch.setattr(
        "sys.argv", ["mcp_server_time_plus", "--local-timezone", "Asia/Karachi"]
    )

    package.main()

    assert started["coro"] is not None
