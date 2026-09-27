"""The inherited surface, pinned. This is the file that proves the fork.

``mcp_server_calendar`` is a fork of `adamchrabaszcz/ics-calendar-mcp`
(MIT), and the claim that makes it a fork rather than a rewrite is narrow
and checkable: **a client written against upstream gets the same five tools,
with the same names, the same arguments and the same result keys.**

So this file asserts upstream's contract rather than ours. Every assertion
here is a thing we are not free to change, and if one of them has to change
the honest move is to update ``NOTICE.md`` and stop calling it a fork —
which is the whole reason the file is separate from
``test_calendar_server.py``, where our own additions are tested.

The counterpart for the other forked server is
``test_time_plus_server.py``, which pins upstream's ``TimeResult`` fields
and its ``+3.75h`` offset formatting for exactly the same reason (ADR-044).
"""

import json
from datetime import datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import pytest
from mcp_server_calendar.client import ICSCalendarClient
from mcp_server_calendar.formatting import (
    format_event,
    format_recurring_event,
)
from mcp_server_calendar.server import build_server

from mcp import Client

BERLIN = ZoneInfo("Europe/Berlin")
MONDAY_0800 = datetime(2026, 9, 14, 8, 0, tzinfo=BERLIN)

pytestmark = pytest.mark.anyio

# One weekly standup and one ordinary meeting. The standup starts a week
# *before* the window every test asks about, so anything that finds it on
# the 14th found it by expanding the rule rather than by reading a date.
_FEED = """BEGIN:VCALENDAR\r
VERSION:2.0\r
PRODID:-//test//EN\r
BEGIN:VEVENT\r
UID:standup-weekly\r
SUMMARY:Weekly standup\r
DTSTART;TZID=Europe/Berlin:20260907T090000\r
DTEND;TZID=Europe/Berlin:20260907T093000\r
LOCATION:Room 2\r
DESCRIPTION:Daily sync\r
RRULE:FREQ=WEEKLY;BYDAY=MO\r
END:VEVENT\r
BEGIN:VEVENT\r
UID:design-review\r
SUMMARY:Design review\r
DTSTART;TZID=Europe/Berlin:20260914T110000\r
DTEND;TZID=Europe/Berlin:20260914T120000\r
ORGANIZER:mailto:alice@example.com\r
STATUS:CONFIRMED\r
END:VEVENT\r
END:VCALENDAR\r
"""


@pytest.fixture
def anyio_backend() -> str:
    """Run the async tests on asyncio only, matching the rest of the suite."""
    return "asyncio"


@pytest.fixture
def feed(tmp_path: Path) -> Path:
    """A calendar file holding the fixture feed above."""
    path = tmp_path / "calendar.ics"
    path.write_text(_FEED, encoding="utf-8")
    return path


def _client(feed: Path) -> ICSCalendarClient:
    """Build the forked client over the fixture feed."""
    return ICSCalendarClient(feed, timezone="Europe/Berlin")


def _payload(result: Any) -> dict[str, Any]:
    """Decode the JSON a tool returned."""
    return json.loads(result.content[0].text)


# --- the five tool names ---------------------------------------------------


async def test_the_five_upstream_tools_are_all_present(feed: Path) -> None:
    """Names are the contract. Renaming one breaks every existing client.

    The two extra names are ours and are asserted separately in
    ``test_calendar_server.py``; here the point is that nothing upstream
    offered went missing in the port.
    """
    async with Client(build_server(feed, "Europe/Berlin")) as client:
        listed = await client.list_tools()
    names = {tool.name for tool in listed.tools}

    assert {
        "get_events_today",
        "get_events_range",
        "get_event_details",
        "list_recurring_events",
        "search_events",
    } <= names


async def test_the_upstream_argument_names_are_unchanged(feed: Path) -> None:
    """``start_date`` / ``end_date`` / ``event_uid`` / ``query``.

    Upstream's spellings, not ours. A client that already builds these
    calls keeps working, which is the difference between extending a server
    and replacing one.
    """
    async with Client(build_server(feed, "Europe/Berlin")) as client:
        listed = await client.list_tools()
    by_name = {tool.name: tool for tool in listed.tools}

    assert set(by_name["get_events_range"].input_schema["properties"]) == {
        "start_date",
        "end_date",
    }
    assert set(by_name["get_event_details"].input_schema["properties"]) == {"event_uid"}
    assert set(by_name["search_events"].input_schema["properties"]) == {
        "query",
        "start_date",
        "end_date",
    }


# --- upstream's result shapes ----------------------------------------------


async def test_a_range_result_keeps_upstreams_envelope(feed: Path) -> None:
    """``start_date`` / ``end_date`` / ``events`` / ``count``, as upstream."""
    async with Client(build_server(feed, "Europe/Berlin")) as client:
        result = await client.call_tool(
            "get_events_range",
            {"start_date": "2026-09-14", "end_date": "2026-09-14"},
        )
    payload = _payload(result)

    assert set(payload) == {"start_date", "end_date", "events", "count"}
    assert payload["count"] == len(payload["events"])


async def test_a_today_result_keeps_upstreams_envelope(feed: Path) -> None:
    """``date`` / ``events`` / ``count``, as upstream."""
    async with Client(
        build_server(feed, "Europe/Berlin", now=lambda: MONDAY_0800)
    ) as client:
        result = await client.call_tool("get_events_today", {})

    assert set(_payload(result)) == {"date", "events", "count"}


async def test_a_search_result_keeps_upstreams_envelope(feed: Path) -> None:
    """``query`` / ``events`` / ``count``, as upstream."""
    async with Client(
        build_server(feed, "Europe/Berlin", now=lambda: MONDAY_0800)
    ) as client:
        result = await client.call_tool("search_events", {"query": "design"})

    assert set(_payload(result)) == {"query", "events", "count"}


def test_one_event_keeps_upstreams_field_names(feed: Path) -> None:
    """The five always-present keys, and the four conditional ones.

    This is the single most load-bearing assertion in the file: every tool
    that returns events returns *these* keys, and a client parsing
    upstream's output parses ours.
    """
    from icalendar import Calendar

    calendar = Calendar.from_ical(feed.read_text())
    review = next(
        component
        for component in calendar.walk("VEVENT")
        if str(component.get("UID")) == "design-review"
    )

    formatted = format_event(review, "Europe/Berlin")

    assert {"uid", "summary", "start", "end", "all_day"} <= set(formatted)
    assert formatted["uid"] == "design-review"
    assert formatted["summary"] == "Design review"
    assert formatted["all_day"] is False
    # Upstream strips the mailto: prefix; keeping that is not decoration,
    # it is what a client that displays the organiser expects.
    assert formatted["organizer"] == "alice@example.com"
    assert formatted["status"] == "CONFIRMED"


def test_the_recurring_shape_keeps_upstreams_rule_summary(feed: Path) -> None:
    """The readable rule line is upstream's idea and worth keeping.

    An ``RRULE`` is not something a mid-sized model reads reliably, so
    "weekly on Monday" is the field that makes ``list_recurring_events``
    useful rather than raw.
    """
    from icalendar import Calendar

    calendar = Calendar.from_ical(feed.read_text())
    standup = next(
        component
        for component in calendar.walk("VEVENT")
        if str(component.get("UID")) == "standup-weekly"
    )

    formatted = format_recurring_event(standup, "Europe/Berlin")

    assert formatted["recurrence"]["frequency"] == "WEEKLY"
    assert formatted["recurrence"]["days"] == ["MO"]
    assert formatted["recurrence"]["description"] == "weekly on Monday"


# --- the capability that made the fork worth taking ------------------------


def test_a_weekly_rule_is_expanded_onto_a_later_week(feed: Path) -> None:
    """Upstream's real value, and the thing a hand-written store lacked.

    The standup is defined once, starting 7 September. Finding it on the
    14th means the ``RRULE`` was expanded — which is
    ``recurring_ical_events`` doing the work, and doing it by hand means
    implementing RFC 5545's recurrence rules (BYDAY, BYSETPOS, UNTIL,
    COUNT, EXDATE), which is a project rather than a function (ADR-071).
    """
    from datetime import date

    events = _client(feed).get_events_between(date(2026, 9, 14), date(2026, 9, 15))

    assert "Weekly standup" in [event["summary"] for event in events]


def test_a_recurring_event_blocks_every_occurrence(feed: Path) -> None:
    """The reason recurrence matters to *us* rather than to a viewer.

    ``suggest_reschedule_slots`` asks the client what is busy. If the rule
    were not expanded, the 09:00 standup would block one Monday and leave
    every later Monday looking free — and the tool would confidently
    suggest a slot straight on top of a standing meeting.
    """
    busy = _client(feed).busy_between(
        datetime(2026, 9, 21, 0, 0, tzinfo=BERLIN),
        datetime(2026, 9, 22, 0, 0, tzinfo=BERLIN),
    )

    assert any(start.hour == 9 for start, _ in busy)


# --- the inherited defect the fork fixes -----------------------------------


async def test_a_failure_is_an_mcp_error_not_a_successful_result(
    feed: Path,
) -> None:
    """Upstream returns ``{"error": ...}`` from a *successful* tool call.

    Every upstream tool ends in ``except Exception as e: return {"error":
    str(e)}``. The client then sees ``is_error`` false, the harness counts
    the call as a success, and ADR-048's per-tool failure guard never fires
    on a tool that is failing every single time. This is the one inherited
    behaviour the fork deliberately does *not* keep (ADR-071).
    """
    async with Client(build_server(feed, "Europe/Berlin")) as client:
        result = await client.call_tool(
            "get_events_range",
            {"start_date": "not-a-date", "end_date": "2026-09-15"},
        )

    assert result.is_error
    assert "YYYY-MM-DD" in result.content[0].text


async def test_a_missing_event_is_an_error_that_says_what_to_call(
    feed: Path,
) -> None:
    """Upstream answers ``{"error": "Event not found: X"}`` and calls it a win."""
    async with Client(build_server(feed, "Europe/Berlin")) as client:
        result = await client.call_tool("get_event_details", {"event_uid": "nope"})

    assert result.is_error
    assert "search_events" in result.content[0].text
