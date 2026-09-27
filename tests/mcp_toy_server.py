"""Toy MCP server, launched as a real subprocess by the end-to-end tests.

The unit tests connect to an in-process server object, which is fast and
exercises the real protocol but not the real *transport*. This script is the
other half: it is what ``StdioMcpClient`` actually launches in production —
a separate process, spoken to over its stdin and stdout — so at least one
test proves that path works rather than assuming it (ADR-041).

It defines no tools of its own. Reusing :func:`tests.fakes.toy_mcp_server`
means the subprocess and the in-process tests offer exactly the same tools,
so a test cannot pass at one layer and fail at the other for reasons that
are really about the fixture.

Run it as ``python /abs/path/to/tests/mcp_toy_server.py``. The repository
root is put on ``sys.path`` here rather than relying on the working
directory, because the end-to-end tests run from a temporary project folder.
"""

import sys
from pathlib import Path

# The repository root, so ``tests.fakes`` imports regardless of where the
# parent process happened to be when it launched us.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tests.fakes import toy_mcp_server  # noqa: E402 - must follow the path fix

if __name__ == "__main__":
    # Blocks until the client closes the session, which is what the client's
    # own shutdown does when the REPL exits.
    toy_mcp_server("toy").run(transport="stdio")
