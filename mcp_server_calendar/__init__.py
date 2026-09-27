"""Calendar MCP server: a fork of ics-calendar-mcp, plus two new tools.

Forked from `adamchrabaszcz/ics-calendar-mcp` (MIT; see ``NOTICE.md``),
carrying its five query tools and adding ``suggest_reschedule_slots`` and
``create_event``. The same relationship ``mcp_server_time_plus`` has with
the reference time server, and for the same reason (ADR-044, ADR-071).

Launched by the harness as a stdio subprocess and described in
``config.yaml`` like any other server (ADR-039). Nothing in ``harness/``
imports this package and nothing here imports ``harness``: the two meet
over the protocol, which is the whole point of the exercise.

Run it directly for a manual check::

    python -m mcp_server_calendar --calendar-file ./calendar/calendar.ics
    python -m mcp_server_calendar --calendar-file ./calendar/calendar.ics \
        --local-timezone Europe/Berlin
"""

import argparse
import asyncio
from pathlib import Path

from mcp_server_calendar.server import SERVER_NAME, build_server, serve

__all__ = ["SERVER_NAME", "build_server", "main", "serve"]


def main() -> None:
    """Parse arguments and run the server on stdio.

    ``--calendar-file`` replaces upstream's ``ICS_CALENDAR_URL`` and is
    required rather than defaulted. Both parts are deliberate. A local file
    removes the last credential — a private ICS feed URL *is* a secret, and
    the brief forbids committing those (8.2) — and requiring it means the
    server cannot quietly write a user's events somewhere that vanishes
    with the container.

    ``--local-timezone`` mirrors ``mcp_server_time_plus``: the zone a time
    with no offset is read as, and the zone results come back in. Upstream
    reads ``ICS_TIMEZONE`` and defaults to Europe/Warsaw, its author's
    zone; ours defaults to UTC and never reads the machine's clock
    settings (ADR-044).
    """
    parser = argparse.ArgumentParser(
        prog="mcp_server_calendar",
        description=(
            "MCP server: iCalendar-backed calendar with recurring-event "
            "expansion and reschedule suggestions."
        ),
    )
    parser.add_argument(
        "--calendar-file",
        required=True,
        type=Path,
        help="The .ics file. Created on first write if it does not exist.",
    )
    parser.add_argument(
        "--local-timezone",
        default="UTC",
        help="IANA zone for input without an offset and for results (default: UTC).",
    )
    args = parser.parse_args()
    asyncio.run(serve(args.calendar_file, args.local_timezone))
