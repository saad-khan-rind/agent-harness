"""Extended time MCP server: the reference server plus meeting-slot search.

Launched by the harness as a stdio subprocess, described in ``config.yaml``
like any other server (ADR-039). Nothing in ``harness/`` imports this
package and nothing here imports ``harness``: the two meet over the
protocol, which is the whole point of the exercise — if the harness could
import the server, the integration would prove nothing.

Run it directly for a manual check::

    python -m mcp_server_time_plus            # speaks MCP on stdin/stdout
    python -m mcp_server_time_plus --local-timezone Europe/Berlin
"""

import argparse
import asyncio

from mcp_server_time_plus.server import SERVER_NAME, build_server, serve

__all__ = ["SERVER_NAME", "build_server", "main", "serve"]


def main() -> None:
    """Parse arguments and run the server on stdio.

    The one argument mirrors upstream's ``--local-timezone``. Upstream reads
    the machine's zone through ``tzlocal`` when it is not given; this fork
    defaults to UTC instead and does not take the dependency, because the
    server runs inside a container whose clock is UTC and a dependency that
    exists to guess something the caller can state is not worth shipping.
    The value is only ever quoted in tool descriptions as a hint for the
    model (ADR-044).
    """
    parser = argparse.ArgumentParser(
        prog="mcp_server_time_plus",
        description=(
            "MCP server: current time, timezone conversion, and meeting-slot "
            "search across timezones."
        ),
    )
    parser.add_argument(
        "--local-timezone",
        default="UTC",
        help="IANA zone quoted in tool descriptions as the user's own (default: UTC).",
    )
    args = parser.parse_args()
    asyncio.run(serve(args.local_timezone))
