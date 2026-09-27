"""Web research MCP server: ``web_search`` and ``web_fetch``.

Launched by the harness as a stdio subprocess and described in
``config.yaml`` like any other server (ADR-039). Nothing in ``harness/``
imports this package and nothing here imports ``harness``: the two meet over
the protocol, which is the point — if the harness could import the server,
the integration would prove nothing.

Run it directly for a manual check::

    python -m mcp_server_web_search           # speaks MCP on stdin/stdout
    python -m mcp_server_web_search --timeout 30
"""

import argparse
import asyncio

from mcp_server_web_search.server import SERVER_NAME, build_server, serve

__all__ = ["SERVER_NAME", "build_server", "main", "serve"]


def main() -> None:
    """Parse arguments and run the server on stdio.

    One argument. The per-call deadline the harness enforces lives in
    ``config.yaml``; this is the server's own budget for a single search or
    retrieval, which must be the shorter of the two if a slow page is to
    produce a readable error rather than a client-side timeout.
    """
    parser = argparse.ArgumentParser(
        prog="mcp_server_web_search",
        description="MCP server: web search and single-page retrieval.",
    )
    parser.add_argument(
        "--timeout",
        type=float,
        default=20.0,
        help="Seconds allowed for one search or page fetch (default: 20).",
    )
    parser.add_argument(
        "--contact",
        default="",
        help=(
            "URL or email named in the User-Agent so a site operator can "
            "reach you. Wikimedia sites refuse requests without one."
        ),
    )
    args = parser.parse_args()
    asyncio.run(serve(args.timeout, args.contact))
