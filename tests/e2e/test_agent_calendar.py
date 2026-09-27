"""The forked calendar server, driven by a real agent over a real volume.

``mcp_server_calendar`` is a fork of ``adamchrabaszcz/ics-calendar-mcp``
(ADR-071) with two tools added, and the two halves of that sentence are
tested differently. The inherited surface is pinned by assertion in
``tests/unit/test_calendar_upstream.py`` — names, arguments, result keys —
because those are a contract, not a behaviour. What cannot be pinned that
way is whether the added capability is *usable*: whether a mid-sized model,
given a clash in plain English, finds its way from a summary to a UID to a
list of free slots.

That is this file. The calendar is a real ``.ics`` on a throwaway volume,
written by the agent itself in the first step of each test rather than
seeded, so what is under test includes the write path (``create_event``,
the one tool upstream does not have on that side).

Every test ends with the calendar still containing what it started with.
That is not caution, it is ADR-008: ``suggest_reschedule_slots`` suggests
and returns, because actually moving an event would mean deleting one.
"""

import warnings
from datetime import date, datetime

import pytest
from tests.e2e.conftest import CALENDAR, Stack, no_destructive_tool_was_called

pytestmark = pytest.mark.e2e

# One Monday, three events, two of which overlap. The clash is deliberate
# and it is the design review against the client call, 11:00 against 11:30.
BUILD_MONDAY = (
    "Put three things on my calendar for Monday 14 September 2026: "
    "standup from 09:00 to 09:30, design review from 11:00 to 12:00, "
    "and a client call from 11:30 to 12:30."
)


def _ics(stack: Stack) -> str:
    """The calendar file's contents, or the volume listing if it is absent."""
    path = f"{CALENDAR}/calendar.ics"
    if not stack.exists(path):
        pytest.fail(f"no calendar file was written; volume:\n{stack.tree(CALENDAR)}")
    return stack.read(path)


def test_the_agent_writes_real_icalendar_to_the_volume(stack: Stack) -> None:
    """``create_event`` end to end, asserted on the bytes it produced.

    The file has to be iCalendar, not a summary of one, because the whole
    argument for building on ``.ics`` is that the result is portable — it
    can be handed to Radicale or imported into Thunderbird. A file that only
    this project can read would make that claim false, so the assertions are
    on the RFC 5545 structure rather than on the tool having returned "ok".
    """
    run = stack.run(BUILD_MONDAY)
    run.assert_completed()

    assert run.tools_called.count("calendar__create_event") >= 3, (
        f"three events were asked for; the trace shows: {run.tools_called}"
    )
    body = _ics(stack)
    assert body.startswith("BEGIN:VCALENDAR"), f"not an iCalendar file:\n{body[:200]}"
    assert body.count("BEGIN:VEVENT") >= 3, f"fewer than three events:\n{body}"
    assert "Design review" in body or "design review" in body.lower()
    # Every event needs a UID, because that is how the agent refers back to
    # one in the next turn. A file full of events with no UIDs would look
    # fine and make the reschedule flow impossible.
    assert body.count("UID:") >= 3, f"events were written without UIDs:\n{body}"


def test_the_agent_finds_the_clash_and_is_offered_alternatives(
    stack: Stack,
) -> None:
    """The new capability, reached the way a user would reach it.

    The prompt gives a *name*, not a UID — "the design review" — which means
    the agent has to search the calendar to find the identifier before it
    can ask for alternatives. That two-step route is the realistic one and
    it is the one upstream's tools were already good at; what is new is
    having somewhere to go with the answer.

    Two container runs against one volume, which is also the point: the
    second agent knows nothing except what is on the disk the first left
    behind.
    """
    stack.run(BUILD_MONDAY).assert_completed()

    run = stack.run(
        "The design review on 14 September clashes with something. "
        "When else could it go that week?"
    )
    run.assert_completed()

    assert "calendar__suggest_reschedule_slots" in run.tools_called, (
        f"the reschedule tool was never reached: {run.tools_called}"
    )
    # Some route to the UID had to come first — by search, by listing the
    # day, or by asking for the range. Which one is the model's choice.
    lookups = {
        "calendar__search_events",
        "calendar__get_events_range",
        "calendar__get_events_today",
        "calendar__list_recurring_events",
    }
    assert lookups & set(run.tools_called), (
        f"the agent never looked the event up first: {run.tools_called}"
    )


MOVE_IT = (
    "Move the design review on 14 September to the best free slot you "
    "can find that week."
)


def test_nothing_is_removed_when_the_agent_is_told_to_move_an_event(
    stack: Stack,
) -> None:
    """ADR-008 at the far end of the chain, with a model trying to be helpful.

    "Move" is the one thing this agent cannot do, because moving means
    deleting the old event. The contract is therefore narrow and absolute:
    whatever the agent decides to do instead, the original event is still
    there afterwards and nothing was removed.

    Note what is *not* asserted: that the event count is unchanged. The agent
    is free to add — that is a judgement call, covered by the test below —
    and conflating "did not delete" with "did not add" is what made the first
    version of this test fail for the wrong reason (ADR-078).
    """
    stack.run(BUILD_MONDAY).assert_completed()
    before = _ics(stack)

    run = stack.run(MOVE_IT)
    run.assert_completed()

    after = _ics(stack)
    assert after.count("BEGIN:VEVENT") >= before.count("BEGIN:VEVENT"), (
        f"an event was removed, which ADR-008 says is impossible:\n{after}"
    )
    assert "T110000" in after or "11:00" in after, (
        f"the original 11:00 slot is gone from the calendar:\n{after}"
    )
    assert no_destructive_tool_was_called(run.tools_called), (
        f"a delete-shaped tool was called: {run.tools_called}"
    )


def test_the_permission_gate_is_what_makes_a_move_impossible(stack: Stack) -> None:
    """The robust version of "it must not duplicate the meeting".

    There is a real quality concern here, and it was found by this tier: asked
    to move a clashing design review, the agent sometimes calls
    ``create_event`` at the suggested slot, leaving the original at 11:00 and
    the calendar holding two design reviews. A move has silently become a
    copy, which is worse for the user than doing nothing.

    It was **not** the model's fault. ``_note`` in the calendar server ended
    every reschedule result with "create_event writes a new event if the user
    picks one", and a real agent read that clause as the next step. The note
    now says the part that matters — a second event is a duplicate, not a
    move — and names no tool to call next (ADR-078).

    That fix makes duplication *less likely*. It cannot make it impossible,
    because whether to write is the model's judgement and it varies run to
    run: the same unchanged test failed once and passed the next time. So it
    is not asserted here. A test that fails at random gets ignored, and an
    ignored test costs more than it catches.

    What *is* asserted is the thing the harness guarantees rather than the
    model: under ``deny_mutations`` no remote ``mutate`` tool runs at all, so
    the calendar cannot change whatever the agent decides to do. That makes
    this deterministic, and it is the mode to demo in when the point being
    made is "the agent cannot touch your data".

    ``calendar__create_event`` is the only ``mutate`` tool of the thirteen
    remote ones, which makes this the one case where the permission gate is
    tested across the MCP boundary rather than on a built-in tool.
    """
    stack.run(BUILD_MONDAY).assert_completed()
    before = _ics(stack)

    run = stack.run(MOVE_IT, permissions="deny_mutations")
    run.assert_completed()

    assert _ics(stack) == before, (
        "the calendar changed under deny_mutations, so a remote mutate tool "
        f"ran when it should have been refused: {run.tools_called}"
    )


def test_a_duplicate_meeting_is_reported_but_does_not_fail_the_run(
    stack: Stack,
) -> None:
    """The quality signal, kept visible without making the build flaky.

    This warning fired on a real run, and what it caught was worth having:
    a note that said "create_event writes a new event if the user picks
    one" read to a real agent as the next step. ADR-078 rewrote the note,
    which made duplication less likely and could not make it impossible —
    the same unchanged test then duplicated on one run and behaved on the
    next, because whether to write is the model's judgement.

    So the judgement was taken away. ``create_event`` now refuses a
    same-titled event nearby, and the refusal arrives as an ``Error:`` the
    next turn can act on (ADR-091). What the refusal cannot cover is the
    agent setting ``allow_duplicate`` — the flag exists because a user is
    still allowed to want two meetings with one name, and taking it away
    would trade this warning for a capability.

    That is why this is still a warning and not an assertion. But it now
    means something much narrower and much more worth reading: the agent
    overrode an explicit refusal. If it fires again the lever is the
    refusal's wording in ``_refuse_duplicate``, or the flag itself — not
    the model, and not this test.

    The hard assertion is ADR-008's, and it holds every time: the original
    event is still on the calendar. Only the *extra* one is a judgement call.
    """
    stack.run(BUILD_MONDAY).assert_completed()

    run = stack.run(MOVE_IT)
    run.assert_completed()

    reviews = [
        line
        for line in _ics(stack).splitlines()
        if line.upper().startswith("SUMMARY:") and "design review" in line.lower()
    ]

    assert reviews, f"the original design review is gone:\n{_ics(stack)}"
    if len(reviews) > 1:
        warnings.warn(
            f"the agent duplicated the meeting instead of reporting the "
            f"options: {reviews}. Since ADR-091 create_event refuses this, so "
            f"the agent must have set allow_duplicate=true after reading the "
            f"refusal. Not a failure — see "
            f"test_the_permission_gate_is_what_makes_a_move_impossible — but "
            f"the lever is _refuse_duplicate's wording or the flag itself in "
            f"mcp_server_calendar/server.py.",
            stacklevel=2,
        )


def test_a_recurring_rule_is_expanded_rather_than_read_literally(
    stack: Stack,
) -> None:
    """The reason the fork was worth taking, asked as a user would ask it.

    Upstream expands ``RRULE``s through ``recurring_ical_events``; a
    hand-written store would have had to implement ``BYDAY``, ``UNTIL`` and
    the rest of RFC 5545 to match it (ADR-071). The observable consequence
    is here: a standup defined once on 7 September must be reported on the
    21st, which is only possible if the rule was expanded rather than the
    ``DTSTART`` read.

    Seeded rather than written by the agent, because ``create_event`` does
    not write recurrence rules — this is upstream's half of the server, and
    the file is what a calendar exported from another application looks
    like.
    """
    stack.write(
        f"{CALENDAR}/calendar.ics",
        "BEGIN:VCALENDAR\r\n"
        "VERSION:2.0\r\n"
        "PRODID:-//agent-harness//e2e//EN\r\n"
        "BEGIN:VEVENT\r\n"
        "UID:standup-weekly\r\n"
        "SUMMARY:Weekly standup\r\n"
        "DTSTART;TZID=Europe/Berlin:20260907T090000\r\n"
        "DTEND;TZID=Europe/Berlin:20260907T093000\r\n"
        "RRULE:FREQ=WEEKLY;BYDAY=MO\r\n"
        "END:VEVENT\r\n"
        "END:VCALENDAR\r\n",
    )

    run = stack.run("What is on my calendar on Monday 21 September 2026?")
    run.assert_completed()

    assert "standup" in run.answer, (
        f"the weekly rule was not expanded onto the 21st: {run.answer!r}"
    )


def test_a_weekday_the_user_names_lands_on_that_weekday(stack: Stack) -> None:
    """The bug a real session found: "next Monday" booked on a Tuesday.

    Asked to put a client meeting on "next Monday", the agent called
    ``get_current_time``, counted forward in its head, wrote 2026-09-15 and
    then told the user "Monday, September 15". It was a Tuesday, and nothing
    in the run ever disagreed with it (ADR-079).

    Two fixes, both cheap. The system prompt now carries a ``## Today``
    section listing the next seven days with their weekday names, so the
    model reads the date off a list instead of counting; and
    ``create_event`` echoes the ``day_of_week`` it actually wrote, so a
    wrong date contradicts itself in the very next observation.

    The assertion needs no fixed date, which is what lets it keep working
    next month: whatever the agent booked, the day it booked it on has to be
    the weekday the user asked for. Parsed out of the ``.ics`` rather than
    read from the reply, because the reply is where the original bug was
    *invisible*.
    """
    run = stack.run(
        "I have a meeting with a client next Monday from 5 to 6pm about the "
        "app. Put it on my calendar."
    )
    run.assert_completed()

    starts = [
        line.split(":", 1)[1]
        for line in _ics(stack).splitlines()
        if line.upper().startswith("DTSTART")
    ]
    assert starts, f"no event was written:\n{_ics(stack)}"

    booked = datetime.strptime(starts[0][:8], "%Y%m%d").date()
    assert booked.strftime("%A") == "Monday", (
        f"the user said Monday and the agent booked {booked}, which is a "
        f"{booked.strftime('%A')}"
    )
    # ...and it has to be the *coming* Monday, not one three weeks out.
    assert 0 < (booked - date.today()).days <= 8, (
        f"{booked} is not the next Monday from today ({date.today()})"
    )
