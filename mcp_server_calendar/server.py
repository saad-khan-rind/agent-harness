"""The MCP interface: seven tools, five inherited and two new.

**What this is.** A fork of `adamchrabaszcz/ics-calendar-mcp` (MIT; see
``NOTICE.md``), carrying its five tools — ``get_events_today``,
``get_events_range``, ``get_event_details``, ``list_recurring_events`` and
``search_events`` — with their upstream names, arguments and result shapes,
plus two capabilities the upstream server does not have:
``suggest_reschedule_slots`` and ``create_event``.

That is the same relationship ``mcp_server_time_plus`` has with the
reference time server (ADR-044), and the same reason: the brief asks for an
*existing* server extended with at least one new tool, and the honest way to
show that is to keep the inherited surface working unchanged.

**Why a fork rather than a dependency.** Identical to ADR-044's answer, and
the SDK says it out loud — importing ``mcp.server.fastmcp`` under
``mcp==2.2.0`` fails with *"This is mcp 2.x, where FastMCP was renamed to
MCPServer … or pin 'mcp<2' to keep running v1 code."* Upstream requires
``mcp>=1.0.0`` and is written against ``FastMCP``; this repository pins
``mcp==2.2.0`` because the harness client uses the 2.x ``Client`` API
(ADR-039). Both cannot be installed in one environment, and downgrading the
harness to suit a server would be the tail wagging the dog. So the five
tools are ported to ``MCPServer`` — same names, same arguments, same JSON.

**One inherited defect, fixed.** Every upstream tool ends in
``except Exception as e: return {"error": str(e)}``. That returns a failure
as a *successful* tool call: the client sees ``is_error`` false, the harness
counts it as a success, and ADR-048's per-tool failure guard never fires on
a tool that is failing every time. Here a validation failure raises
``ToolError``, which the harness turns into an ``Error:`` observation the
agent can recover from (ADR-071).

**Where the line is.** This module owns the interface. Reading lives in
:mod:`mcp_server_calendar.client`, writing in
:mod:`mcp_server_calendar.writer`, and the scheduling search in
:mod:`mcp_server_calendar.reschedule` — none of which imports MCP.
"""

import json
from datetime import datetime, timedelta
from pathlib import Path
from typing import Annotated, Any, NoReturn

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import ToolAnnotations
from pydantic import Field

from mcp_server_calendar.client import (
    MAX_EVENTS_RETURNED,
    ICSCalendarClient,
    now_in,
)
from mcp_server_calendar.formatting import InvalidArgument, parse_date, zone
from mcp_server_calendar.reschedule import (
    DEFAULT_DAYS_AHEAD,
    DEFAULT_RESULTS,
    DEFAULT_WORKING_HOURS,
    MAX_DAYS_AHEAD,
    MAX_DURATION_MINUTES,
    MAX_RESULTS,
    MIN_DAYS_AHEAD,
    MIN_DURATION_MINUTES,
    MIN_RESULTS,
    parse_working_hours,
    suggest_slots,
)
from mcp_server_calendar.writer import create_event as write_event

SERVER_NAME = "calendar"

# Reading a calendar changes nothing and touches no network. Saying so is
# not decoration: the harness adapter reads ``read_only_hint`` to decide
# whether a discovered tool registers as observe or mutate, and an
# unannotated tool is treated as a mutation — the safe default, but the
# wrong one for a query.
READ_ONLY = ToolAnnotations(
    read_only_hint=True,
    destructive_hint=False,
    idempotent_hint=True,
    open_world_hint=False,
)

# Creating an event writes a file. Explicitly *not* destructive: it appends,
# and nothing here can replace or remove what is already stored. That
# distinction is what the permission policy acts on — ``confirm_mutations``
# asks before this tool and never before a query (ADR-057) — and it is also
# what stops the adapter refusing our own tool at registration (ADR-059).
WRITES = ToolAnnotations(
    read_only_hint=False,
    destructive_hint=False,
    idempotent_hint=False,
    open_world_hint=False,
)


def _json(payload: dict[str, Any]) -> str:
    """Render a result as the JSON text the model will read."""
    return json.dumps(payload, indent=2)


def _reject(exc: InvalidArgument) -> NoReturn:
    """Turn a validation failure into an MCP error the caller can act on.

    The SDK treats an unrecognised exception as a crash: the client is told
    only "Error executing tool X", the message is withheld, and a model
    that mistyped one argument has nothing to correct. ``ToolError`` is the
    SDK's channel for a failure the author anticipated, and its message
    reaches the caller intact.

    Deliberately narrow: only :class:`InvalidArgument` is mapped, so a
    genuine bug still surfaces as a crash with a traceback in the server
    log rather than being dressed up as advice to the model.
    """
    raise ToolError(str(exc)) from exc


def _refuse_duplicate(existing: list[dict[str, Any]], summary: str) -> None:
    """Raise when this title is already on the calendar nearby.

    The message has one job: leave the model a next step it can take
    without guessing. So it names the event that is already there and the
    two ways forward — the flag when a second meeting is genuinely wanted,
    and "report the options" when the user actually asked for a move
    (ADR-058, ADR-091).

    Args:
        existing: What :meth:`ICSCalendarClient.same_summary_near` found.
        summary: The title that was about to be written.

    Raises:
        InvalidArgument: When ``existing`` is not empty. Mapped to a
            ``ToolError`` by the caller, like every other refusal here.
    """
    if not existing:
        return
    first = existing[0]
    also = (
        f" (and {len(existing) - 1} more like it nearby)" if len(existing) > 1 else ""
    )
    raise InvalidArgument(
        f"an event called {summary!r} is already on this calendar at "
        f"{first.get('start') or 'an unknown time'} "
        f"(uid {first.get('uid') or 'unknown'}){also}. Nothing here can move "
        "or delete it, so writing this one would leave TWO copies of the "
        "meeting rather than moving it. If the user asked for a second, "
        "separate meeting, call again with allow_duplicate=true. If they "
        "asked for this one to be moved, say that it cannot be moved and "
        "report the alternative times instead."
    )


def _parse_moment(text: str, field: str, default_zone: str) -> datetime:
    """Parse an ISO timestamp, applying ``default_zone`` when it is naive.

    Accepting a naive value is a judgement call worth naming. Refusing
    anything without an offset is more correct and worse in practice:
    models write ``2026-09-14T09:00`` constantly, and a tool that fails on
    it burns a turn teaching a lesson the server could simply apply. The
    assumed zone is echoed in every result, so it is visible rather than
    silent.
    """
    raw = text.strip()
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError as exc:
        raise InvalidArgument(
            f"{field} must be an ISO-8601 date and time such as "
            f"'2026-09-14T09:00' (got {text!r})"
        ) from exc
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=zone(default_zone))
    return parsed


def build_server(
    calendar_file: Path,
    local_timezone: str = "UTC",
    now: Any = None,
) -> MCPServer:
    """Assemble the server and register its seven tools.

    A function rather than module-level state so tests can build a server,
    connect a real client to it in-process and exercise the real protocol
    without a subprocess (ADR-041). Upstream keeps a module-level singleton
    with ``get_client``/``reset_client``/``set_client`` around it; holding
    the client here removes the global and its three undo hatches.

    Args:
        calendar_file: The ``.ics`` file. Created on first write; an
            absent file reads as an empty calendar rather than an error,
            because that is the normal state of a fresh install.
        local_timezone: IANA zone used when a caller gives a time with no
            offset, and the zone events are reported in. Never read from
            the machine's clock settings.
        now: Callable returning the current aware ``datetime``, injected by
            the tests so a reschedule search over a fixed calendar gives
            the same answer on any machine on any day.

    Returns:
        A server ready for ``run_stdio_async()`` or for an in-process client.

    Raises:
        InvalidArgument: If ``local_timezone`` is not an IANA name. Failing
            at startup is right for a launch argument: a server that
            started with a broken default would hand it to the model.
    """
    zone(local_timezone)
    calendar = ICSCalendarClient(calendar_file, timezone=local_timezone)
    clock = now if now is not None else (lambda: now_in(local_timezone))

    server = MCPServer(
        name=SERVER_NAME,
        instructions=(
            "Calendar tools over an iCalendar (.ics) file: list and search "
            "events with recurring events expanded, add an event, and "
            "suggest alternative slots for one that clashes. Nothing is "
            "deleted and no network is used."
        ),
    )

    # --- inherited from upstream, unchanged in name and result shape -----

    @server.tool(
        name="get_events_today",
        description=(
            "Get all calendar events for today, with recurring events "
            "expanded. Returns their times, titles and locations."
        ),
        annotations=READ_ONLY,
    )
    def get_events_today() -> str:
        """Inherited from upstream, unchanged in behaviour."""
        try:
            events = calendar.get_events_today()
        except InvalidArgument as exc:
            _reject(exc)
        return _json(
            {
                "date": clock().date().isoformat(),
                "events": events,
                "count": len(events),
            }
        )

    @server.tool(
        name="get_events_range",
        description=(
            "Get calendar events between two dates, with recurring events "
            "expanded. Use it to see what a day or a week already holds."
        ),
        annotations=READ_ONLY,
    )
    def get_events_range(
        start_date: Annotated[
            str, Field(description="Start date, ISO format YYYY-MM-DD.")
        ],
        end_date: Annotated[
            str,
            Field(
                description=(
                    "End date, ISO format YYYY-MM-DD. On or after "
                    "start_date, and at most 366 days later."
                )
            ),
        ],
    ) -> str:
        """Inherited from upstream, unchanged in behaviour."""
        try:
            start = parse_date(start_date)
            end = parse_date(end_date)
            events = calendar.get_events_between(start, end + timedelta(days=1))
        except InvalidArgument as exc:
            _reject(exc)
        return _json(
            {
                "start_date": start_date,
                "end_date": end_date,
                "events": events,
                "count": len(events),
            }
        )

    @server.tool(
        name="get_event_details",
        description=(
            "Get the full details of one event by its UID. The UID comes "
            "from get_events_range or search_events."
        ),
        annotations=READ_ONLY,
    )
    def get_event_details(
        event_uid: Annotated[str, Field(description="The event's unique identifier.")],
    ) -> str:
        """Inherited from upstream, unchanged in behaviour.

        Upstream returns ``{"error": "Event not found: …"}`` from a
        successful call; this raises so the harness sees a failure and the
        model sees advice (ADR-071).
        """
        try:
            event = calendar.get_event_by_uid(event_uid)
        except InvalidArgument as exc:
            _reject(exc)
        if event is None:
            _reject(
                InvalidArgument(
                    f"no event with uid {event_uid!r}. Use search_events or "
                    f"get_events_range to find the uid of the event you mean."
                )
            )
        return _json(event)

    @server.tool(
        name="list_recurring_events",
        description=(
            "List the repeating events with their rules — how often, on "
            "which days, until when. Returns the base events, not every "
            "occurrence."
        ),
        annotations=READ_ONLY,
    )
    def list_recurring_events() -> str:
        """Inherited from upstream, unchanged in behaviour."""
        try:
            events = calendar.get_recurring_events()
        except InvalidArgument as exc:
            _reject(exc)
        return _json({"events": events, "count": len(events)})

    @server.tool(
        name="search_events",
        description=(
            "Find events whose title, description or location contains a "
            "piece of text. Use it when the user names a meeting but not "
            "its date."
        ),
        annotations=READ_ONLY,
    )
    def search_events(
        query: Annotated[str, Field(description="Text to look for, e.g. 'standup'.")],
        start_date: Annotated[
            str, Field(description="Optional start date YYYY-MM-DD; defaults to today.")
        ] = "",
        end_date: Annotated[
            str,
            Field(
                description=(
                    "Optional end date YYYY-MM-DD; defaults to a year after the start."
                )
            ),
        ] = "",
    ) -> str:
        """Inherited from upstream, unchanged in behaviour."""
        try:
            start = parse_date(start_date) if start_date else None
            end = parse_date(end_date) if end_date else None
            events = calendar.search_events(query, start, end)
        except InvalidArgument as exc:
            _reject(exc)
        return _json({"query": query, "events": events, "count": len(events)})

    # --- what the fork adds ----------------------------------------------

    @server.tool(
        name="create_event",
        description=(
            "Add one event to the calendar. This writes to the calendar "
            "file, so say what you are about to add before calling it. "
            "Events cannot be deleted afterwards, so an event with a title "
            "already on the calendar nearby is refused unless "
            "allow_duplicate is set — writing one is how a 'move' turns "
            "into two copies of the same meeting."
        ),
        annotations=WRITES,
    )
    def create_event(
        summary: Annotated[
            str, Field(description="One-line title, e.g. 'Standup with Bob'.")
        ],
        start: Annotated[
            str,
            Field(
                description=(
                    "Start, ISO-8601, e.g. '2026-09-14T09:00'. A time with "
                    f"no offset is read as {local_timezone}."
                )
            ),
        ],
        end: Annotated[str, Field(description="End, ISO-8601. Must be after start.")],
        location: Annotated[str, Field(description="Where it happens.")] = "",
        description: Annotated[str, Field(description="Longer notes.")] = "",
        allow_duplicate: Annotated[
            bool,
            Field(
                description=(
                    "Write this even though an event with the same title is "
                    "already on the calendar nearby. Only set it when the "
                    "user has asked for a second, separate meeting — never "
                    "to carry out a 'move', which this agent cannot do."
                )
            ),
        ] = False,
    ) -> str:
        """Append the event and return it, UID included.

        Refuses a same-titled event nearby unless ``allow_duplicate`` says
        otherwise, which is the enforcement half of ADR-078's fix. The
        wording half — ``_NOT_A_MOVE`` — made duplication less likely and
        could not make it impossible, because whether to write is the
        model's judgement and it varied run to run. This does not ask the
        model to judge: it hands back a refusal naming the event already
        there, which the harness turns into an ``Error:`` observation the
        next turn can act on (ADR-091).
        """
        try:
            begins = _parse_moment(start, "start", local_timezone)
            finishes = _parse_moment(end, "end", local_timezone)
            if not allow_duplicate:
                _refuse_duplicate(calendar.same_summary_near(summary, begins), summary)
            created = write_event(
                calendar.path,
                summary=summary,
                start=begins,
                end=finishes,
                location=location,
                description=description,
            )
            # The only write in this server, so the only place the reader's
            # cache can go stale. Told rather than left to infer: the cache
            # asks the filesystem whether the file looks different, and on a
            # filesystem with second-resolution timestamps two writes in one
            # tick do not (ADR-091).
            calendar.invalidate()
        except InvalidArgument as exc:
            _reject(exc)
        return _json({"created": created})

    @server.tool(
        name="suggest_reschedule_slots",
        description=(
            "Propose alternative times for an event that clashes. Reads "
            "everything else on the calendar for the next few days, "
            "recurring events included, removes the hours already taken, "
            "and returns the best free slots — one per day, soonest first, "
            "preferring times close to the original. Suggestions only: "
            "nothing is moved or written."
        ),
        annotations=READ_ONLY,
    )
    def suggest_reschedule_slots(
        event_uid: Annotated[
            str,
            Field(
                description=(
                    "UID of the event to move, from search_events or "
                    "get_events_range. Its own slot is excluded from the "
                    "busy times, and its length and hour are used as the "
                    "default duration and the time of day to stay near."
                )
            ),
        ],
        days_ahead: Annotated[
            int,
            Field(
                ge=MIN_DAYS_AHEAD,
                le=MAX_DAYS_AHEAD,
                description=(
                    f"How many days from now to search "
                    f"({MIN_DAYS_AHEAD}-{MAX_DAYS_AHEAD})."
                ),
            ),
        ] = DEFAULT_DAYS_AHEAD,
        duration_minutes: Annotated[
            int,
            Field(
                ge=0,
                le=MAX_DURATION_MINUTES,
                description=(
                    f"Length of the meeting "
                    f"({MIN_DURATION_MINUTES}-{MAX_DURATION_MINUTES}), or 0 "
                    f"to keep the event's own length."
                ),
            ),
        ] = 0,
        working_hours: Annotated[
            str,
            Field(
                description=(
                    f"Daily window suggestions must fall inside, as "
                    f"'HH:MM-HH:MM' (default '{DEFAULT_WORKING_HOURS}'). "
                    f"Must sit inside 06:00-22:00."
                )
            ),
        ] = DEFAULT_WORKING_HOURS,
        timezone: Annotated[
            str,
            Field(
                description=(
                    "IANA zone the working hours are in and the results are "
                    f"reported in, or '' for {local_timezone}."
                )
            ),
        ] = "",
        max_results: Annotated[
            int,
            Field(
                ge=MIN_RESULTS,
                le=MAX_RESULTS,
                description=(
                    f"How many slots to propose ({MIN_RESULTS}-{MAX_RESULTS})."
                ),
            ),
        ] = DEFAULT_RESULTS,
        include_weekends: Annotated[
            bool, Field(description="Whether Saturday and Sunday are candidates.")
        ] = False,
    ) -> str:
        """Return ranked alternative slots for the named event."""
        try:
            view = zone(timezone or local_timezone)
            hours = parse_working_hours(working_hours)
            reference = clock()

            # The *occurrence*, not the base event. For a weekly standup
            # ``get_event_by_uid`` returns the first Monday it ever
            # happened, possibly years ago; rescheduling that is
            # meaningless. This is the subtlety recurrence introduces and
            # the reason the fork was worth taking (ADR-071).
            moving = calendar.occurrence_of(event_uid, reference)
            if moving is None:
                moving = calendar.get_event_by_uid(event_uid)
            if moving is None:
                _reject(
                    InvalidArgument(
                        f"no event with uid {event_uid!r}. Use search_events "
                        f"or get_events_range to find the uid of the event "
                        f"you mean."
                    )
                )

            begins = _parse_moment(
                str(moving.get("start") or ""), "start", local_timezone
            )
            finishes = _parse_moment(
                str(moving.get("end") or ""), "end", local_timezone
            )
            length = duration_minutes or max(
                1, int((finishes - begins).total_seconds() // 60)
            )

            horizon = reference + timedelta(days=days_ahead + 1)
            busy = calendar.busy_between(
                min(reference, begins), horizon, skip_uid=event_uid
            )
            # Named, not just timed: a person asking why their meeting has
            # to move needs "Client call", not "something at 11:30".
            clashes = calendar.clashes_with(begins, finishes, skip_uid=event_uid)

            suggestions = suggest_slots(
                duration_minutes=length,
                busy=busy,
                now=reference,
                view=view,
                working_hours=hours,
                days_ahead=days_ahead,
                max_results=max_results,
                include_weekends=include_weekends,
                original_start=begins,
                original_end=finishes,
            )
        except InvalidArgument as exc:
            _reject(exc)

        return _json(
            {
                "event": {
                    "uid": event_uid,
                    "summary": moving.get("summary"),
                    "current_start": begins.astimezone(view).isoformat(
                        timespec="minutes"
                    ),
                    "duration_minutes": length,
                },
                "conflicts_with": [
                    {
                        "summary": clash.get("summary"),
                        "start": clash.get("start"),
                    }
                    for clash in clashes
                ],
                "searched": {
                    "from": reference.astimezone(view).isoformat(timespec="minutes"),
                    "days_ahead": days_ahead,
                    "working_hours": hours.label,
                    "timezone": timezone or local_timezone,
                    "weekends_included": include_weekends,
                },
                "slots_found": len(suggestions),
                "slots": [slot.as_dict(view) for slot in suggestions],
                "note": _note(bool(suggestions), bool(clashes)),
            }
        )

    return server


# The sentence every branch of _note ends with. It is worth naming as a
# constant because getting it wrong has a specific, ugly consequence, and we
# got it wrong once already.
#
# The first version ended "Nothing has been moved; create_event writes a new
# event if the user picks one." A real agent read that as an instruction: told
# to move a clashing design review, it called suggest_reschedule_slots, then
# called create_event at the best slot — leaving the original at 11:00 and the
# calendar holding *two* design reviews. It was doing what the note said.
#
# The tool cannot move an event, because moving means deleting the old one and
# this agent cannot delete (ADR-008). So the note has to say the part that
# matters — a new event is a duplicate, not a move — and hand the decision
# back to the user rather than naming a tool to call next (ADR-078).
#
# The note alone was never going to be enough, and the e2e tier kept saying
# so: the same unchanged test duplicated the meeting on one run and behaved
# on the next, because whether to write is the model's judgement. The
# enforcement now lives in ``create_event``, which refuses a same-titled
# event nearby (ADR-091). This sentence stays because telling the model the
# refusal is coming is cheaper than letting it spend a turn discovering it.
_NOT_A_MOVE = (
    "Nothing has been moved and nothing can be: this agent cannot delete, so "
    "the original event stays where it is. Adding an event at one of these "
    "times would leave TWO copies of the meeting, not move it — create_event "
    "will refuse it for that reason. Report these options and let the user "
    "decide; only create an event if they ask for one after seeing them."
)


def _note(has_slots: bool, has_conflicts: bool) -> str:
    """Return the closing line, which says what the caller should do next.

    The combination worth naming is "free slots but no conflict": the tool
    was asked to move something that is not actually clashing, and saying
    so lets the model check with the user instead of quietly relocating a
    meeting nobody asked to move.
    """
    if not has_slots:
        return (
            "No free slot of that length inside those working hours in the "
            "days searched. Try a longer days_ahead, a wider working_hours, "
            "or include_weekends."
        )
    if not has_conflicts:
        return (
            "Nothing on this calendar overlaps the event as it stands, so it "
            f"may not need moving. Check before using one of these. {_NOT_A_MOVE}"
        )
    return f"Suggestions only. {_NOT_A_MOVE}"


async def serve(calendar_file: Path, local_timezone: str = "UTC") -> None:
    """Run the server on stdio until the client disconnects."""
    await build_server(calendar_file, local_timezone).run_stdio_async()


__all__ = [
    "MAX_EVENTS_RETURNED",
    "SERVER_NAME",
    "build_server",
    "serve",
]
