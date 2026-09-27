"""Entry point for ``python -m mcp_server_web_search``.

Kept to one call, like ``mcp_server_time_plus/__main__.py``: the module is
excluded from coverage because the guarded line cannot run in-process, so
there must be nothing here worth testing.
"""

from mcp_server_web_search import main

if __name__ == "__main__":  # pragma: no cover - exercised by the stdio e2e
    main()
