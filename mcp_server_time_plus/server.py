"""The MCP interface: four tools, two inherited and two new.

**What this is.** A fork of the reference ``mcp-server-time`` server from
``modelcontextprotocol/servers`` (MIT; see ``NOTICE.md``), carrying its two
tools — ``get_current_time`` and ``convert_time`` — unchanged in name,
arguments and result shape, plus two capabilities the upstream server does
not have: ``find_meeting_slot`` and ``count_working_days``. That is the
brief's Week 2 requirement, which asks for an *existing* server extended
with at least one new tool rather than a new server written from nothing.

The two additions are the same question at two scales — "when inside a day
can these people meet" and "how many days of work are there between two
dates" — which is what keeps this one coherent server rather than a drawer
of unrelated utilities (ADR-045, ADR-051).

**Why a fork rather than a dependency.** ``mcp-server-time`` requires
``mcp<2``; this repository pins ``mcp==2.2.0`` because the harness client
uses the 2.x ``Client`` API. Installing both is not possible in one
environment, and downgrading the harness to suit a server would be the tail
wagging the dog. The upstream code also uses the 1.x low-level decorators
(``@server.list_tools()``), which 2.x replaced. So the two inherited tools
are ported to ``MCPServer`` here — same names, same arguments, same JSON —
and the port is small enough to read in one sitting. ADR-044 records this.

**Where the line is.** This module owns the *interface*: tool names,
descriptions, argument schemas, argument validation, the transformation of a
result into JSON, and the mapping from a failure to an MCP response. The
scheduling logic lives in :mod:`mcp_server_time_plus.meeting` and knows
nothing about MCP. That split is what lets the logic be tested exhaustively
without a protocol conversation, and lets the interface be read as the
contract it is.

**Schema style.** Argument schemas are generated from these signatures by
the SDK, and every argument carries an explicit ``Field(description=...)``
plus its bounds. Two consequences are deliberate. Bounds in the schema are
told to the model rather than discovered through a failed call. And the
schemas stay *flat* — scalars and arrays of scalars, no nested models — so
they survive the harness adapter, which rebuilds a remote schema from its
``properties`` and would drop the ``$defs`` block a nested model generates
(ADR-043, ADR-045).
"""

import json
from datetime import datetime, timedelta
from typing import Annotated, Any, NoReturn

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import ToolAnnotations
from pydantic import Field

from mcp_server_time_plus.meeting import (
    MAX_DURATION_MINUTES,
    MAX_PARTICIPANTS,
    MAX_RESULTS,
    MIN_DURATION_MINUTES,
    MIN_PARTICIPANTS,
    MIN_RESULTS,
    InvalidArgument,
    find_slots,
    parse_clock,
    zone,
)
from mcp_server_time_plus.workdays import (
    MAX_HOLIDAYS,
    MAX_WEEKEND_DAYS,
    count_working_days,
)

SERVER_NAME = "time-plus"

# Every tool here reads a clock or does arithmetic: nothing is written, no
# network is touched, and the same arguments give the same answer (modulo
# "now"). Saying so in the annotations is not decoration — the harness
# adapter reads ``read_only_hint`` to decide whether a discovered tool
# registers as observe or mutate, and an unannotated tool is treated as a
# mutation, which is the safe default but the wrong one here.
READ_ONLY = ToolAnnotations(
    read_only_hint=True,
    destructive_hint=False,
    idempotent_hint=True,
    open_world_hint=False,
)


def _json(payload: dict[str, Any]) -> str:
    """Render a result as the JSON text the model will read.

    Indented on purpose: this text is pasted into the model's context, and
    two spaces of structure cost a handful of tokens while making a nested
    result legible to a mid-sized model. Matches upstream's formatting.
    """
    return json.dumps(payload, indent=2)


def _reject(exc: InvalidArgument) -> NoReturn:
    """Turn a validation failure into an MCP error the caller can act on.

    This mapping is the difference between a usable tool and a frustrating
    one. The SDK treats an unrecognised exception as a crash: the client is
    told only "Error executing tool find_meeting_slot", the message is
    withheld, and a model that mistyped one timezone has nothing to correct.
    ``ToolError`` is the SDK's channel for a failure the author anticipated,
    and its message reaches the caller intact.

    The distinction is kept deliberately narrow. Only
    :class:`~mcp_server_time_plus.meeting.InvalidArgument` is mapped, so a
    genuine bug in our code still surfaces as a crash with a traceback in the
    server log and a generic message to the client — an internal error should
    not be dressed up as advice to the model.

    It is also the same policy the harness applies at its own boundary: a
    tool failure becomes an observation the agent can recover from, never a
    dead end (ADR-006, ADR-016).
    """
    raise ToolError(str(exc)) from exc


def _time_result(moment: datetime, timezone_name: str) -> dict[str, Any]:
    """Shape one instant exactly as upstream's ``TimeResult`` does.

    Kept byte-compatible with the reference server so a client that already
    parses ``mcp-server-time`` output keeps working against this fork.
    """
    return {
        "timezone": timezone_name,
        "datetime": moment.isoformat(timespec="seconds"),
        "day_of_week": moment.strftime("%A"),
        "is_dst": bool(moment.dst()),
    }


def _offset_label(source: datetime, target: datetime) -> str:
    """Describe the offset between two readings of the same instant.

    Whole hours render as ``+2.0h``; the quarter- and half-hour zones
    (Kathmandu at +5:45, Adelaide at +9:30) render without trailing zeros.
    Upstream's formatting, preserved.
    """
    difference = (
        (target.utcoffset() or timedelta()) - (source.utcoffset() or timedelta())
    ).total_seconds() / 3600
    if difference.is_integer():
        return f"{difference:+.1f}h"
    return f"{difference:+.2f}".rstrip("0").rstrip(".") + "h"


def build_server(local_timezone: str = "UTC") -> MCPServer:
    """Assemble the server and register its four tools.

    A function rather than module-level state so tests can build a server,
    connect a real client to it in-process and exercise the real protocol
    without a subprocess — the same shape ``tests/fakes.py`` uses for the toy
    server (ADR-041).

    Args:
        local_timezone: Zone named in the tool descriptions as the default
            for a user who does not say where they are. Only ever a hint to
            the model; nothing here reads the machine's clock settings.

    Returns:
        A server ready for ``run_stdio_async()`` or for an in-process client.

    Raises:
        InvalidArgument: If ``local_timezone`` is not an IANA name. Failing
            at startup is right for a launch argument: a server that starts
            with a broken default would hand that default to the model.
    """
    zone(local_timezone)
    server = MCPServer(
        name=SERVER_NAME,
        instructions=(
            "Time and timezone tools, plus meeting-slot search across "
            "several timezones. All answers are computed from the IANA "
            "timezone database; nothing is written and no network is used."
        ),
    )

    @server.tool(
        name="get_current_time",
        description="Get the current time in a specific IANA timezone.",
        annotations=READ_ONLY,
    )
    def get_current_time(
        timezone: Annotated[
            str,
            Field(
                description=(
                    "IANA timezone name, e.g. 'America/New_York' or "
                    f"'Europe/London'. Use '{local_timezone}' if the user "
                    "gave no timezone."
                )
            ),
        ],
    ) -> str:
        """Inherited from the reference server, unchanged in behaviour."""
        try:
            tz = zone(timezone)
        except InvalidArgument as exc:
            _reject(exc)
        return _json(_time_result(datetime.now(tz), timezone))

    @server.tool(
        name="convert_time",
        description="Convert a wall-clock time from one IANA timezone to another.",
        annotations=READ_ONLY,
    )
    def convert_time(
        source_timezone: Annotated[
            str,
            Field(
                description=(
                    "IANA timezone the time is given in, e.g. "
                    f"'Europe/Berlin'. Use '{local_timezone}' if unstated."
                )
            ),
        ],
        time: Annotated[
            str, Field(description="Time to convert, 24-hour HH:MM, e.g. '14:30'.")
        ],
        target_timezone: Annotated[
            str,
            Field(
                description=(
                    "IANA timezone to convert into, e.g. 'Asia/Tokyo'. Use "
                    f"'{local_timezone}' if unstated."
                )
            ),
        ],
    ) -> str:
        """Inherited from the reference server, unchanged in behaviour."""
        try:
            source_zone = zone(source_timezone)
            target_zone = zone(target_timezone)
            parsed = parse_clock(time, "time")
        except InvalidArgument as exc:
            _reject(exc)
        # Today's date in the source zone, as upstream does: an offset is
        # only meaningful on a date, because it is the date that decides
        # whether either side is on summer time.
        now = datetime.now(source_zone)
        source_time = datetime(
            now.year, now.month, now.day, parsed.hour, parsed.minute, tzinfo=source_zone
        )
        target_time = source_time.astimezone(target_zone)
        return _json(
            {
                "source": _time_result(source_time, source_timezone),
                "target": _time_result(target_time, target_timezone),
                "time_difference": _offset_label(source_time, target_time),
            }
        )

    @server.tool(
        name="find_meeting_slot",
        description=(
            "Find times on a given day when a meeting of a given length "
            "falls inside everyone's working hours, across up to eight "
            "timezones. Returns each slot in every participant's local "
            "clock. The date is read in the first timezone listed."
        ),
        annotations=READ_ONLY,
    )
    def find_meeting_slot(
        timezones: Annotated[
            list[str],
            Field(
                description=(
                    "IANA timezone names, one per participant, organiser "
                    "first — the date is that person's calendar day. "
                    "No duplicates."
                ),
                min_length=MIN_PARTICIPANTS,
                max_length=MAX_PARTICIPANTS,
            ),
        ],
        date: Annotated[
            str,
            Field(
                description=(
                    "The organiser's local day to search, YYYY-MM-DD, "
                    "e.g. '2026-09-09'."
                )
            ),
        ],
        duration_minutes: Annotated[
            int,
            Field(
                description="Meeting length in minutes.",
                ge=MIN_DURATION_MINUTES,
                le=MAX_DURATION_MINUTES,
            ),
        ] = 60,
        earliest_local: Annotated[
            str,
            Field(
                description=(
                    "Start of the working day on each participant's own "
                    "clock, 24-hour HH:MM."
                )
            ),
        ] = "09:00",
        latest_local: Annotated[
            str,
            Field(
                description=(
                    "End of the working day on each participant's own "
                    "clock, 24-hour HH:MM. The meeting must end by then."
                )
            ),
        ] = "18:00",
        max_results: Annotated[
            int,
            Field(
                description="How many slots to return, earliest first.",
                ge=MIN_RESULTS,
                le=MAX_RESULTS,
            ),
        ] = 5,
    ) -> str:
        """The new capability. See :mod:`mcp_server_time_plus.meeting`."""
        try:
            found = find_slots(
                timezones=timezones,
                date=date,
                duration_minutes=duration_minutes,
                earliest_local=earliest_local,
                latest_local=latest_local,
                max_results=max_results,
            )
        except InvalidArgument as exc:
            _reject(exc)
        return _json(found)

    @server.tool(
        name="count_working_days",
        description=(
            "Count the working days from start_date to end_date, skipping "
            "weekends and any holidays you name. Both dates count unless "
            "include_end_date is false. For deadlines, notice periods and "
            "delivery estimates. Dates only, no timezone: a working day "
            "means the same thing everywhere."
        ),
        annotations=READ_ONLY,
    )
    def count_working_days_tool(
        start_date: Annotated[
            str,
            Field(description="First day of the range, YYYY-MM-DD, e.g. '2026-09-09'."),
        ],
        end_date: Annotated[
            str,
            Field(
                description=(
                    "Last day of the range, YYYY-MM-DD. Must not be before start_date."
                )
            ),
        ],
        weekend: Annotated[
            list[str],
            Field(
                description=(
                    "Weekday names that are not working days, e.g. "
                    "['Saturday', 'Sunday'] or ['Friday', 'Saturday']. "
                    "Pass an empty list for a seven-day week."
                ),
                max_length=MAX_WEEKEND_DAYS,
            ),
        ] = ["Saturday", "Sunday"],  # noqa: B006 - read only, never mutated
        holidays: Annotated[
            list[str],
            Field(
                description=(
                    "Dates that are not working days, YYYY-MM-DD. There is "
                    "no built-in holiday calendar; list the ones that apply."
                ),
                max_length=MAX_HOLIDAYS,
            ),
        ] = [],  # noqa: B006 - read only, never mutated
        include_end_date: Annotated[
            bool,
            Field(
                description=(
                    "Whether end_date itself counts. Default true: 'how many "
                    "working days from Monday to Friday' is 5, not 4. Set it "
                    "false only for 'how long until <date>', where the date "
                    "is the target rather than the last day worked. The word "
                    "'between' does not imply false — leave the default "
                    "unless the request is clearly a countdown."
                )
            ),
        ] = True,
    ) -> str:
        """The second new capability. See :mod:`mcp_server_time_plus.workdays`."""
        try:
            counted = count_working_days(
                start_date=start_date,
                end_date=end_date,
                weekend=list(weekend),
                holidays=list(holidays),
                include_end_date=include_end_date,
            )
        except InvalidArgument as exc:
            _reject(exc)
        return _json(counted)

    # Referenced so linters see the registrations as used; the decorator has
    # already handed each function to the server.
    _ = (get_current_time, convert_time, find_meeting_slot, count_working_days_tool)
    return server


async def serve(local_timezone: str = "UTC") -> None:
    """Run the server on stdin/stdout until the client disconnects.

    stdio is the transport the harness launches (ADR-039): the agent image
    starts this module as a child process and speaks MCP over its pipes, so
    a single ``docker run`` still has every tool available with no port, no
    second container and no network listener.
    """
    await build_server(local_timezone).run_stdio_async()


__all__ = ["InvalidArgument", "SERVER_NAME", "build_server", "serve"]
