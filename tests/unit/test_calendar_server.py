"""What the fork *adds*, over a real MCP session.

``test_calendar_upstream.py`` pins the five inherited tools — that file is
the fork's evidence and everything in it is a thing we may not change. This
one covers the two capabilities upstream does not have,
``suggest_reschedule_slots`` and ``create_event``, plus the whole server's
annotations and schema bounds.

The split matters for a reason beyond tidiness: when one of these tests
fails it is our bug, and when one of the upstream tests fails the fork claim
itself is in question. Two different conversations, two different files.
"""

import json
import os
from datetime import datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import pytest
from mcp_server_calendar.reschedule import MAX_DAYS_AHEAD, MIN_DAYS_AHEAD
from mcp_server_calendar.server import SERVER_NAME, build_server

from mcp import Client

BERLIN = ZoneInfo("Europe/Berlin")
MONDAY_0800 = datetime(2026, 9, 14, 8, 0, tzinfo=BERLIN)

pytestmark = pytest.mark.anyio


@pytest.fixture
def anyio_backend() -> str:
    """Run the async tests on asyncio only, matching the rest of the suite."""
    return "asyncio"


@pytest.fixture
def calendar_file(tmp_path: Path) -> Path:
    """An absent calendar file for one test.

    Absent rather than empty: a calendar that does not exist yet is the
    normal state of a fresh install, and the server has to answer that with
    "no events" rather than an error.
    """
    return tmp_path / "calendar.ics"


def _server(calendar_file: Path) -> Any:
    """Build the shipped server with a frozen clock."""
    return build_server(calendar_file, "Europe/Berlin", now=lambda: MONDAY_0800)


async def _seed(client: Any, plan: list[tuple[str, str, str]]) -> None:
    """Create the given ``(summary, start, end)`` events."""
    for summary, start, end in plan:
        await client.call_tool(
            "create_event", {"summary": summary, "start": start, "end": end}
        )


def _payload(result: Any) -> dict[str, Any]:
    """Decode the JSON a tool returned."""
    return json.loads(result.content[0].text)


async def _uid_of(client: Any, query: str) -> str:
    """Return the UID of the first event matching ``query``.

    The search window is given explicitly. ``search_events`` defaults its
    window to *today* on the machine's clock — upstream's behaviour, and not
    the frozen ``now`` the server was built with — so a bare search stops
    finding the seeded September 2026 events the day the real date passes
    them, and every reschedule test here would fail on a Tuesday for no
    reason of its own.
    """
    found = _payload(
        await client.call_tool(
            "search_events",
            {"query": query, "start_date": "2026-09-01", "end_date": "2026-12-31"},
        )
    )
    return str(found["events"][0]["uid"])


# --- the surface as a whole ------------------------------------------------


async def test_the_server_publishes_seven_tools(calendar_file: Path) -> None:
    """Five inherited plus two added, asserted in full.

    Exact rather than a subset: a tool appearing here without being
    documented and permission-reviewed is what this catches.
    """
    async with Client(_server(calendar_file)) as client:
        listed = await client.list_tools()

    assert sorted(tool.name for tool in listed.tools) == [
        "create_event",
        "get_event_details",
        "get_events_range",
        "get_events_today",
        "list_recurring_events",
        "search_events",
        "suggest_reschedule_slots",
    ]


async def test_no_tool_offers_deletion(calendar_file: Path) -> None:
    """ADR-008 reaches the servers we write, not only the harness.

    Upstream is read-only, so it had nothing to remove; the fork adds a
    write and still adds no delete. One would be refused at registration by
    the adapter on its name alone (ADR-042), so shipping it would mean
    shipping a tool that can never be called.
    """
    async with Client(_server(calendar_file)) as client:
        listed = await client.list_tools()

    forbidden = ("delete", "remove", "cancel", "purge", "drop")
    assert not [
        tool.name
        for tool in listed.tools
        if any(word in tool.name for word in forbidden)
    ]


async def test_reads_are_read_only_and_only_the_write_is_not(
    calendar_file: Path,
) -> None:
    """The annotation the harness adapter branches on.

    ``read_only_hint`` decides whether a discovered tool registers as
    observe or mutate, which decides whether ``confirm_mutations`` prompts
    for it (ADR-057). Six of the seven are queries; getting this wrong
    means prompting for a calendar read or writing an event unasked.
    """
    async with Client(_server(calendar_file)) as client:
        listed = await client.list_tools()
    by_name = {tool.name: tool for tool in listed.tools}

    for name in (
        "get_events_today",
        "get_events_range",
        "get_event_details",
        "list_recurring_events",
        "search_events",
        "suggest_reschedule_slots",
    ):
        assert by_name[name].annotations.read_only_hint is True, name
    assert by_name["create_event"].annotations.read_only_hint is False
    # Writing is not destroying, and the difference is what stops the
    # adapter refusing our own tool at registration (ADR-059).
    assert by_name["create_event"].annotations.destructive_hint is False


async def test_every_tool_describes_itself(calendar_file: Path) -> None:
    """A tool the model cannot tell apart from another is one it misuses."""
    async with Client(_server(calendar_file)) as client:
        listed = await client.list_tools()

    for tool in listed.tools:
        assert tool.description and len(tool.description) > 40, tool.name


async def test_the_reschedule_bounds_are_in_the_published_schema(
    calendar_file: Path,
) -> None:
    """The brief's word is *bounded*, and this is where that is checkable.

    Upstream publishes no bounds at all — its range query has no ceiling
    and its search defaults to a year. A limit enforced in code but absent
    from the schema is one the model finds by failing, at a turn a time.
    """
    async with Client(_server(calendar_file)) as client:
        listed = await client.list_tools()
    schema = next(
        tool.input_schema
        for tool in listed.tools
        if tool.name == "suggest_reschedule_slots"
    )
    properties = schema["properties"]

    assert properties["days_ahead"]["minimum"] == MIN_DAYS_AHEAD
    assert properties["days_ahead"]["maximum"] == MAX_DAYS_AHEAD
    assert properties["max_results"]["maximum"] == 10
    assert properties["duration_minutes"]["maximum"] == 480
    assert schema["required"] == ["event_uid"]


async def test_the_schemas_stay_flat(calendar_file: Path) -> None:
    """Nested models generate ``$defs``, which the harness adapter drops.

    The adapter rebuilds a remote schema from its ``properties``, so a
    nested argument reaches the model with its shape missing (ADR-043).
    """
    async with Client(_server(calendar_file)) as client:
        listed = await client.list_tools()

    for tool in listed.tools:
        assert "$defs" not in tool.input_schema
        for spec in tool.input_schema["properties"].values():
            assert "$ref" not in spec


# --- create_event, the first addition --------------------------------------


async def test_creating_an_event_returns_it_with_a_uid(
    calendar_file: Path,
) -> None:
    """The UID is what every other tool refers to the event by."""
    async with Client(_server(calendar_file)) as client:
        result = await client.call_tool(
            "create_event",
            {
                "summary": "Standup",
                "start": "2026-09-14T09:00",
                "end": "2026-09-14T09:30",
            },
        )
    created = _payload(result)["created"]

    assert created["summary"] == "Standup"
    assert created["uid"]
    assert created["start"].endswith("+02:00")


async def test_a_created_event_is_readable_by_the_inherited_tools(
    calendar_file: Path,
) -> None:
    """The join between the addition and the fork.

    ``create_event`` writes iCalendar that upstream's own reader parses —
    if it did not, the two halves of this server would be talking past
    each other and only one of them would be usable.
    """
    async with Client(_server(calendar_file)) as client:
        await _seed(client, [("Standup", "2026-09-14T09:00", "2026-09-14T09:30")])
        result = await client.call_tool(
            "get_events_range",
            {"start_date": "2026-09-14", "end_date": "2026-09-14"},
        )
    payload = _payload(result)

    assert payload["count"] == 1
    assert payload["events"][0]["summary"] == "Standup"


async def test_creating_two_events_appends_rather_than_replaces(
    calendar_file: Path,
) -> None:
    """There is no delete, so a write that overwrote would be one by stealth."""
    async with Client(_server(calendar_file)) as client:
        await _seed(
            client,
            [
                ("Standup", "2026-09-14T09:00", "2026-09-14T09:30"),
                ("Review", "2026-09-14T11:00", "2026-09-14T12:00"),
            ],
        )
        result = await client.call_tool(
            "get_events_range",
            {"start_date": "2026-09-14", "end_date": "2026-09-14"},
        )

    assert _payload(result)["count"] == 2


async def test_an_event_longer_than_a_day_is_refused_as_a_typo(
    calendar_file: Path,
) -> None:
    """A mistyped year would black out the calendar to the horizon.

    Every reschedule search would then return nothing, with no clue why,
    and there is no delete tool to undo it with.
    """
    async with Client(_server(calendar_file)) as client:
        result = await client.call_tool(
            "create_event",
            {
                "summary": "Typo",
                "start": "2026-09-14T09:00",
                "end": "2027-09-14T10:00",
            },
        )

    assert result.is_error
    assert "24 hours" in result.content[0].text


# --- the duplicate guard (ADR-091) -----------------------------------------


async def test_a_second_event_with_the_same_title_is_refused(
    calendar_file: Path,
) -> None:
    """The enforcement half of ADR-078's fix.

    Told to *move* a clashing meeting, a real agent would call
    ``suggest_reschedule_slots`` and then write a new event at the best
    slot — leaving the original where it was and the calendar holding two
    design reviews. A move had silently become a copy, which is worse for
    the user than doing nothing.

    ADR-078 answered it in the tool's closing note, and the note made it
    less likely without making it impossible, because whether to write is
    the model's judgement. This does not ask the model to judge.
    """
    async with Client(_server(calendar_file)) as client:
        await _seed(client, [("Design review", "2026-09-14T11:00", "2026-09-14T12:00")])
        result = await client.call_tool(
            "create_event",
            {
                "summary": "Design review",
                "start": "2026-09-15T11:00",
                "end": "2026-09-15T12:00",
            },
        )

    assert result.is_error
    message = result.content[0].text
    # The refusal has to leave a next step that does not need guessing: what
    # is already there, and both ways forward (ADR-058).
    assert "already on this calendar" in message
    assert "2026-09-14T11:00" in message
    assert "allow_duplicate=true" in message
    assert "report the alternative times" in message


async def test_the_refused_duplicate_never_reaches_the_file(
    calendar_file: Path,
) -> None:
    """Enforced, not merely reported.

    A guard that logged the refusal and wrote anyway would pass the test
    above and fail this one, which is why both exist.
    """
    async with Client(_server(calendar_file)) as client:
        await _seed(client, [("Design review", "2026-09-14T11:00", "2026-09-14T12:00")])
        await client.call_tool(
            "create_event",
            {
                "summary": "Design review",
                "start": "2026-09-15T11:00",
                "end": "2026-09-15T12:00",
            },
        )
        result = await client.call_tool(
            "get_events_range",
            {"start_date": "2026-09-14", "end_date": "2026-09-16"},
        )

    assert _payload(result)["count"] == 1


async def test_case_and_spacing_do_not_get_a_duplicate_past_the_guard(
    calendar_file: Path,
) -> None:
    """A guard the shift key defeats is not a guard."""
    async with Client(_server(calendar_file)) as client:
        await _seed(client, [("Design review", "2026-09-14T11:00", "2026-09-14T12:00")])
        result = await client.call_tool(
            "create_event",
            {
                "summary": "  design   REVIEW ",
                "start": "2026-09-15T11:00",
                "end": "2026-09-15T12:00",
            },
        )

    assert result.is_error


async def test_allow_duplicate_writes_the_second_one(
    calendar_file: Path,
) -> None:
    """The user is still allowed to want two meetings with one name.

    The flag is what turns a judgement call into a deliberate second
    choice: the model cannot reach it by accident, and a user who asks for
    a second standup gets one.
    """
    async with Client(_server(calendar_file)) as client:
        await _seed(client, [("Standup", "2026-09-14T09:00", "2026-09-14T09:30")])
        result = await client.call_tool(
            "create_event",
            {
                "summary": "Standup",
                "start": "2026-09-15T09:00",
                "end": "2026-09-15T09:30",
                "allow_duplicate": True,
            },
        )
        listing = await client.call_tool(
            "get_events_range",
            {"start_date": "2026-09-14", "end_date": "2026-09-16"},
        )

    assert not result.is_error
    assert _payload(listing)["count"] == 2


async def test_a_different_title_is_not_a_duplicate(
    calendar_file: Path,
) -> None:
    """Narrowing must not become blocking.

    Exact on the normalised title and nothing else. Matching loosely would
    start refusing "Standup" because "Standup with Bob" exists, and a
    guard that blocks real work is a guard somebody switches off.
    """
    async with Client(_server(calendar_file)) as client:
        result = await client.call_tool(
            "create_event",
            {
                "summary": "Standup with Bob",
                "start": "2026-09-15T09:00",
                "end": "2026-09-15T09:30",
            },
        )
        await _seed(client, [("Standup", "2026-09-14T09:00", "2026-09-14T09:30")])

    assert not result.is_error


async def test_the_same_title_far_enough_away_is_allowed(
    calendar_file: Path,
) -> None:
    """A quarterly review is not a duplicate of the last one.

    The window is what keeps the guard about duplication rather than about
    ever reusing a name, so its far edge is worth pinning.
    """
    async with Client(_server(calendar_file)) as client:
        await _seed(
            client, [("Quarterly review", "2026-09-14T11:00", "2026-09-14T12:00")]
        )
        result = await client.call_tool(
            "create_event",
            {
                "summary": "Quarterly review",
                "start": "2026-12-14T11:00",
                "end": "2026-12-14T12:00",
            },
        )

    assert not result.is_error


# --- the reader's cache, which the guard made load-bearing -----------------


async def test_events_written_in_one_tick_are_all_visible_afterwards(
    calendar_file: Path,
) -> None:
    """The regression CI found and a Mac could not.

    The reader caches the parsed calendar and re-reads when the file looks
    different. Before ADR-091 that was harmless, because ``create_event``
    never read through the reader — the first read happened after every
    write. The duplicate guard reads *before* each write, which primes the
    cache mid-sequence, and on a filesystem whose ``st_mtime`` advances once
    a second three quick writes share one timestamp. The reader then serves
    the file as it stood after the first write, forever.

    ``os.utime`` reproduces that on any filesystem, which is the point: this
    failed on a GitLab runner and passed on Docker Desktop, and a test that
    only fails on the unlucky one is not a test.
    """
    stamp = 1_780_000_000.0
    async with Client(_server(calendar_file)) as client:
        for summary, start, end in [
            ("Standup", "2026-09-14T09:00", "2026-09-14T09:30"),
            ("Design review", "2026-09-14T11:00", "2026-09-14T12:00"),
            ("Client call", "2026-09-14T11:30", "2026-09-14T12:30"),
        ]:
            result = await client.call_tool(
                "create_event", {"summary": summary, "start": start, "end": end}
            )
            assert not result.is_error, result.content[0].text
            # Every write lands on the same timestamp, as a one-second
            # filesystem would leave them.
            os.utime(calendar_file, (stamp, stamp))
        listing = await client.call_tool(
            "get_events_range",
            {"start_date": "2026-09-01", "end_date": "2026-12-31"},
        )

    assert calendar_file.read_text(encoding="utf-8").count("BEGIN:VEVENT") == 3
    assert _payload(listing)["count"] == 3, (
        "the reader served a cached calendar from before the later writes"
    )


async def test_a_file_changed_underneath_at_the_same_mtime_is_re_read(
    calendar_file: Path,
) -> None:
    """The other half: a writer we do not control.

    The e2e tier seeds ``calendar.ics`` onto the volume directly, so
    ``invalidate`` cannot be the whole answer — the reader has to notice a
    change it was never told about. Size is what catches it when the
    timestamp cannot.
    """
    stamp = 1_780_000_000.0
    async with Client(_server(calendar_file)) as client:
        await _seed(client, [("Standup", "2026-09-14T09:00", "2026-09-14T09:30")])
        os.utime(calendar_file, (stamp, stamp))
        first = await client.call_tool(
            "get_events_range",
            {"start_date": "2026-09-01", "end_date": "2026-12-31"},
        )
        # Somebody else appends, and the clock does not move.
        body = calendar_file.read_text(encoding="utf-8")
        calendar_file.write_text(
            body.replace(
                "END:VCALENDAR",
                "BEGIN:VEVENT\r\nUID:outside-writer\r\nSUMMARY:Added elsewhere\r\n"
                "DTSTART;TZID=Europe/Berlin:20260914T140000\r\n"
                "DTEND;TZID=Europe/Berlin:20260914T150000\r\n"
                "END:VEVENT\r\nEND:VCALENDAR",
            ),
            encoding="utf-8",
        )
        os.utime(calendar_file, (stamp, stamp))
        second = await client.call_tool(
            "get_events_range",
            {"start_date": "2026-09-01", "end_date": "2026-12-31"},
        )

    assert _payload(first)["count"] == 1
    assert _payload(second)["count"] == 2, (
        "a file that changed under the same mtime was served from cache"
    )


# --- suggest_reschedule_slots, the headline addition -----------------------


async def test_reschedule_reports_the_clash_and_proposes_other_days(
    calendar_file: Path,
) -> None:
    """The headline case, as a demo would run it.

    Design review 11:00-12:00 overlaps a client call at 11:30. The result
    names the clash — a model told to reschedule will reschedule, and
    "this is what it collides with" is what lets it explain itself — and
    the suggestions land on different days.
    """
    async with Client(_server(calendar_file)) as client:
        await _seed(
            client,
            [
                ("Standup", "2026-09-14T09:00", "2026-09-14T09:30"),
                ("Design review", "2026-09-14T11:00", "2026-09-14T12:00"),
                ("Client call", "2026-09-14T11:30", "2026-09-14T12:30"),
            ],
        )
        uid = await _uid_of(client, "design")
        result = await client.call_tool("suggest_reschedule_slots", {"event_uid": uid})
    payload = _payload(result)

    assert len(payload["conflicts_with"]) == 1
    assert payload["slots_found"] == 3
    assert len({slot["day"] for slot in payload["slots"]}) == 3
    assert "Nothing has been moved" in payload["note"]


async def test_reschedule_never_proposes_the_events_current_slot(
    calendar_file: Path,
) -> None:
    """The bug this tool had before it had this test.

    The event is removed from the busy list so it does not block itself,
    which leaves its own hour free and — a perfect time-of-day match —
    sorted first. The tool was asked where *else* it could go.
    """
    async with Client(_server(calendar_file)) as client:
        await _seed(client, [("Design review", "2026-09-14T11:00", "2026-09-14T12:00")])
        uid = await _uid_of(client, "design")
        result = await client.call_tool("suggest_reschedule_slots", {"event_uid": uid})

    assert all(
        not slot["start"].startswith("2026-09-14T11:00")
        for slot in _payload(result)["slots"]
    )


async def test_an_event_with_no_clash_is_said_to_have_no_clash(
    calendar_file: Path,
) -> None:
    """The answer a model asked to reschedule will not otherwise give.

    Told to move something, it moves it. Being told the thing is not
    actually colliding is what lets it check with the user first.
    """
    async with Client(_server(calendar_file)) as client:
        await _seed(client, [("Standup", "2026-09-14T09:00", "2026-09-14T09:30")])
        uid = await _uid_of(client, "standup")
        result = await client.call_tool("suggest_reschedule_slots", {"event_uid": uid})
    payload = _payload(result)

    assert payload["conflicts_with"] == []
    assert "may not need moving" in payload["note"]


async def test_a_recurring_event_blocks_the_slots_it_occupies(
    calendar_file: Path,
) -> None:
    """The capability the fork bought, at the level it actually pays off.

    A weekly standup defined once must block *every* Monday, not only the
    first. Without upstream's expansion the tool would confidently propose
    09:00 next Monday, straight on top of a standing meeting — and the
    suggestion would look perfectly reasonable in the output.
    """
    calendar_file.parent.mkdir(parents=True, exist_ok=True)
    calendar_file.write_text(
        "BEGIN:VCALENDAR\r\nVERSION:2.0\r\nPRODID:-//test//EN\r\n"
        "BEGIN:VEVENT\r\nUID:standup-weekly\r\nSUMMARY:Weekly standup\r\n"
        "DTSTART;TZID=Europe/Berlin:20260907T090000\r\n"
        "DTEND;TZID=Europe/Berlin:20260907T100000\r\n"
        "RRULE:FREQ=WEEKLY;BYDAY=MO\r\n"
        "END:VEVENT\r\n"
        "BEGIN:VEVENT\r\nUID:one-off\r\nSUMMARY:One off\r\n"
        "DTSTART;TZID=Europe/Berlin:20260915T090000\r\n"
        "DTEND;TZID=Europe/Berlin:20260915T100000\r\n"
        "END:VEVENT\r\nEND:VCALENDAR\r\n",
        encoding="utf-8",
    )

    async with Client(_server(calendar_file)) as client:
        result = await client.call_tool(
            "suggest_reschedule_slots", {"event_uid": "one-off", "days_ahead": 14}
        )
    payload = _payload(result)

    # 21 September is the Monday after next. The rule puts the standup at
    # 09:00 there, so nothing may be proposed inside that hour.
    assert all(
        not slot["start"].startswith("2026-09-21T09:") for slot in payload["slots"]
    )


async def test_reschedule_reports_in_the_requested_timezone(
    calendar_file: Path,
) -> None:
    """Whoever asked reads their own clock, not the server's.

    This is where the calendar server meets ``time_plus``: one instant,
    two wall-clock readings, and a suggestion is only useful on the clock
    of the person deciding.
    """
    async with Client(_server(calendar_file)) as client:
        await _seed(client, [("Standup", "2026-09-14T09:00", "2026-09-14T09:30")])
        uid = await _uid_of(client, "standup")
        result = await client.call_tool(
            "suggest_reschedule_slots",
            {"event_uid": uid, "timezone": "Asia/Karachi"},
        )
    payload = _payload(result)

    assert payload["searched"]["timezone"] == "Asia/Karachi"
    assert payload["slots"][0]["start"].endswith("+05:00")


async def test_a_search_with_no_room_says_what_to_widen(
    calendar_file: Path,
) -> None:
    """An empty result has to be actionable or the model just retries it."""
    async with Client(_server(calendar_file)) as client:
        await _seed(
            client,
            [
                ("Solid Monday", "2026-09-14T09:00", "2026-09-14T17:00"),
                ("Solid Tuesday", "2026-09-15T09:00", "2026-09-15T17:00"),
                ("Standup", "2026-09-14T08:00", "2026-09-14T08:30"),
            ],
        )
        uid = await _uid_of(client, "standup")
        result = await client.call_tool(
            "suggest_reschedule_slots", {"event_uid": uid, "days_ahead": 1}
        )
    payload = _payload(result)

    assert payload["slots_found"] == 0
    assert "days_ahead" in payload["note"]
    assert "include_weekends" in payload["note"]


# --- failures reach the model as advice ------------------------------------


@pytest.mark.parametrize(
    ("tool", "arguments", "fragment"),
    [
        ("suggest_reschedule_slots", {"event_uid": "nope"}, "search_events"),
        (
            "create_event",
            {"summary": "", "start": "2026-09-14T09:00", "end": "2026-09-14T10:00"},
            "summary must not be empty",
        ),
        (
            "create_event",
            {
                "summary": "Backwards",
                "start": "2026-09-14T10:00",
                "end": "2026-09-14T09:00",
            },
            "end must be after start",
        ),
        (
            "create_event",
            {"summary": "Bad", "start": "not-a-date", "end": "2026-09-14T10:00"},
            "ISO-8601",
        ),
        ("search_events", {"query": "  "}, "must not be empty"),
    ],
)
async def test_a_bad_argument_comes_back_as_a_message_not_a_crash(
    calendar_file: Path, tool: str, arguments: dict[str, Any], fragment: str
) -> None:
    """The difference between a usable tool and a frustrating one.

    An unmapped exception reaches the client as "Error executing tool X"
    with the reason withheld, and a model that mistyped one argument has
    nothing to correct. ``ToolError`` carries the message through, and the
    harness turns it into an observation the agent can recover from
    (ADR-006, ADR-016).
    """
    async with Client(_server(calendar_file)) as client:
        result = await client.call_tool(tool, arguments)

    assert result.is_error
    assert fragment in result.content[0].text


async def test_a_working_hours_window_outside_the_bounds_is_refused(
    calendar_file: Path,
) -> None:
    """Without the outer bound a caller gets a 03:00 suggestion presented
    with the same confidence as a 10:00 one.
    """
    async with Client(_server(calendar_file)) as client:
        await _seed(client, [("Standup", "2026-09-14T09:00", "2026-09-14T09:30")])
        uid = await _uid_of(client, "standup")
        result = await client.call_tool(
            "suggest_reschedule_slots",
            {"event_uid": uid, "working_hours": "02:00-23:00"},
        )

    assert result.is_error
    assert "must sit inside" in result.content[0].text


async def test_a_corrupt_calendar_file_is_not_silently_overwritten(
    calendar_file: Path,
) -> None:
    """The one thing a server with no delete tool must not do by accident.

    Appending to a file that will not parse means either losing it or
    refusing. Refusing is the only safe answer, and the message says why.
    """
    calendar_file.parent.mkdir(parents=True, exist_ok=True)
    calendar_file.write_text("this is not iCalendar at all", encoding="utf-8")

    async with Client(_server(calendar_file)) as client:
        result = await client.call_tool(
            "create_event",
            {
                "summary": "Standup",
                "start": "2026-09-14T09:00",
                "end": "2026-09-14T09:30",
            },
        )

    assert result.is_error
    assert calendar_file.read_text(encoding="utf-8") == "this is not iCalendar at all"


async def test_a_bad_launch_timezone_fails_at_startup(calendar_file: Path) -> None:
    """A server that started with a broken default would hand it to the model."""
    from mcp_server_calendar.formatting import InvalidArgument

    with pytest.raises(InvalidArgument, match="not an IANA timezone"):
        build_server(calendar_file, "Europe/Berln")


def test_the_server_name_is_stable() -> None:
    """It prefixes every tool the harness registers from this server.

    Renaming it renames ``calendar__suggest_reschedule_slots`` and every
    reference to it in the docs and the scenarios.
    """
    assert SERVER_NAME == "calendar"


# --- the launch path -------------------------------------------------------


def test_the_entry_point_parses_its_arguments(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``python -m mcp_server_calendar`` is how ``config.yaml`` starts this.

    A mistake in the argument parser surfaces as "server failed to start"
    in the CLI and nowhere else — the child's stderr is not the harness's,
    so the one line that would explain it is lost. Stubbing ``asyncio.run``
    keeps this a unit test: the coroutine is inspected, never awaited.
    """
    import mcp_server_calendar as package

    started: dict[str, object] = {}

    def fake_run(coro: Any) -> None:
        started["coro"] = coro
        coro.close()  # never awaited; closing avoids a "never awaited" warning

    monkeypatch.setattr(package.asyncio, "run", fake_run)
    monkeypatch.setattr(
        "sys.argv",
        [
            "mcp_server_calendar",
            "--calendar-file",
            str(tmp_path / "calendar.ics"),
            "--local-timezone",
            "Asia/Karachi",
        ],
    )

    package.main()

    assert started["coro"] is not None


def test_the_calendar_file_argument_is_required(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Required rather than defaulted, and that is deliberate.

    Upstream requires ``ICS_CALENDAR_URL`` for the same reason. Guessing
    the path would mean a server that starts happily and writes a user's
    events somewhere that disappears on the next run.
    """
    import mcp_server_calendar as package

    monkeypatch.setattr("sys.argv", ["mcp_server_calendar"])

    with pytest.raises(SystemExit):
        package.main()
