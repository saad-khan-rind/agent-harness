"""Entry point for ``python -m mcp_server_time_plus``.

Kept to one call, like ``harness/__main__.py``: the module is excluded from
coverage because the guarded line cannot run in-process, so there must be
nothing here worth testing.
"""

from mcp_server_time_plus import main

if __name__ == "__main__":  # pragma: no cover - exercised by the stdio e2e
    main()
