"""The synchronous client, driven against a real MCP server.

What is replaced in this file is the *server*, never the client. Each test
hands a real ``MCPServer`` to the real :class:`StdioMcpClient`, so the
handshake, the tool listing, the argument round trip, the error mapping and
the async-to-sync bridge all genuinely run. Only the thing on the far end is
a toy (ADR-041).

The bridge is the part worth testing hardest. It owns a thread and an event
loop, and its failure modes are the ones that would be worst in a REPL: a
call that never returns, a session that dies quietly, a thread that outlives
the process it was serving.
"""

import sys
from collections.abc import Iterator
from pathlib import Path

import anyio
import pytest
from harness.mcp.client import McpConnectionError, McpToolError, StdioMcpClient
from mcp.server.mcpserver import MCPServer
from tests.fakes import DENIED_TOOL_NAME, DESTRUCTIVE_TOOL_NAME, toy_mcp_server

from mcp import StdioServerParameters


@pytest.fixture
def client() -> Iterator[StdioMcpClient]:
    """Return a connected client over the in-process toy server.

    Closed in teardown whatever the test did, so a failing assertion cannot
    leak a thread into the next test.
    """
    connected = StdioMcpClient(server_name="toy", target=toy_mcp_server(), timeout=5.0)
    connected.connect()
    yield connected
    connected.close()


# --- discovery ------------------------------------------------------------


def test_discovery_returns_wrapped_prefixed_tools(client: StdioMcpClient) -> None:
    """The whole point: remote capabilities arrive as ordinary harness tools."""
    names = [tool.name for tool in client.discover_tools()]
    assert "toy__add" in names
    assert "toy__ping" in names
    assert all(name.startswith("toy__") for name in names)


def test_a_denied_tool_never_reaches_the_tool_list(client: StdioMcpClient) -> None:
    """ADR-008 has to survive a server that offers deletion at runtime."""
    names = [tool.name for tool in client.discover_tools()]
    assert not any(DENIED_TOOL_NAME in name for name in names)


def test_a_destructive_annotation_is_refused_whatever_the_name(
    client: StdioMcpClient,
) -> None:
    """The rule that a word list cannot express (ADR-059).

    ``sweep_stale_entries`` contains no denied word and would have sailed
    into the registry on spelling alone. It declares ``destructive_hint``,
    so it is refused on its own word instead.
    """
    names = [tool.name for tool in client.discover_tools()]

    assert not any(DESTRUCTIVE_TOOL_NAME in name for name in names)


# --- calling --------------------------------------------------------------


def test_a_tool_call_round_trips(client: StdioMcpClient) -> None:
    """Arguments reach the server and its answer comes back as text."""
    assert client.call_tool("add", {"a": 2, "b": 3}) == "5"


def test_a_tool_that_raises_on_the_server_becomes_an_error(
    client: StdioMcpClient,
) -> None:
    """A remote failure is an observation, exactly like a local one.

    Note what does *not* appear: the server's internal exception text. The
    SDK sanitises it to "Error executing tool explode" rather than shipping
    a traceback across the wire, so the observation names the tool and the
    server and nothing about the far side's internals. That is the right
    behaviour to assert on — a test expecting the original message would be
    asserting a leak.
    """
    tools = {tool.name: tool for tool in client.discover_tools()}
    observation = tools["toy__explode"].execute({})
    assert observation.startswith("Error: ")
    assert "explode" in observation
    assert "toy" in observation


# --- lifecycle ------------------------------------------------------------


def test_calling_before_connect_is_refused() -> None:
    """A clear message beats an AttributeError from a None session."""
    client = StdioMcpClient(server_name="toy", target=toy_mcp_server())
    with pytest.raises(McpConnectionError, match="not connected"):
        client.call_tool("add", {"a": 1, "b": 2})


def test_a_server_that_never_finishes_starting_is_abandoned() -> None:
    """The REPL must not hang behind a server that will not answer.

    The child is not reaped — see the module docstring — but the caller is
    released, which is the property the session depends on.
    """
    client = StdioMcpClient(
        server_name="sleepy",
        target=StdioServerParameters(
            # sys.executable, not "python": the interpreter running the tests
            # is the only one guaranteed to exist on a contributor's machine
            # and inside the test image.
            command=sys.executable,
            args=["-c", "import time; time.sleep(5)"],
        ),
        connect_timeout=0.2,
    )
    with pytest.raises(McpConnectionError, match="did not start"):
        client.connect()
    client.close()


def test_a_slow_tool_call_times_out_instead_of_hanging() -> None:
    """A wedged call must return control, not park the whole REPL."""
    server = MCPServer(name="slow")

    @server.tool()
    async def wait() -> str:
        """Sleep far longer than the client is willing to wait."""
        await anyio.sleep(30)
        return "done"

    client = StdioMcpClient(server_name="slow", target=server, timeout=0.3)
    client.connect()
    try:
        with pytest.raises(McpToolError, match="slow"):
            client.call_tool("wait", {})
    finally:
        client.close()


# --- a launched server's stderr goes to its log, not the terminal (ADR-113) ---

_TOY_SERVER_SCRIPT = str(Path(__file__).resolve().parents[1] / "mcp_toy_server.py")


def test_a_failing_tool_does_not_print_the_servers_log_to_the_terminal(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capfd: pytest.CaptureFixture[str],
) -> None:
    """One failure, one line: the harness's. The server's own is in its log.

    ``capfd`` captures at the file-descriptor level, which is what a child
    process inherits — so this sees exactly what would reach the terminal.
    """
    from harness.mcp import client as client_module

    monkeypatch.setattr(client_module.tempfile, "gettempdir", lambda: str(tmp_path))
    client = StdioMcpClient(
        "toy",
        StdioServerParameters(command=sys.executable, args=[_TOY_SERVER_SCRIPT]),
    )
    client.connect()
    try:
        with pytest.raises(McpToolError):
            client.call_tool("explode", {})
    finally:
        client.close()

    captured = capfd.readouterr()
    assert "exploded on purpose" not in captured.err
    assert "exploded on purpose" in client.log_path.read_text(encoding="utf-8")


def test_a_server_that_dies_at_launch_is_explained_from_its_log(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The reason moved off the terminal, so the warning carries it instead."""
    from harness.mcp import client as client_module

    monkeypatch.setattr(client_module.tempfile, "gettempdir", lambda: str(tmp_path))
    crashing = StdioServerParameters(
        command=sys.executable,
        args=["-c", "import sys; sys.stderr.write('boom: no module named x\\n')"],
    )
    client = StdioMcpClient("broken", crashing, connect_timeout=15)

    with pytest.raises(McpConnectionError, match="boom: no module named x"):
        client.connect()
    client.close()
