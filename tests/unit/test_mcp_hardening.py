"""The three MCP changes of Wednesday: annotations, reconnect, transport.

Each one closes a gap that the Tuesday code had no answer for, and each is
tested against the real client and the real adapter with only the server
replaced (ADR-041).

* **Refusal reads the protocol, not the spelling.** ``DENIED_TOOL_WORDS``
  catches ``delete_file`` and misses ``sweep``. A server that annotates
  ``destructive_hint: true`` has said what it does in MCP's own vocabulary,
  and that is now the first signal checked (ADR-059).
* **A dead server is restarted once.** Before this, the first crash made
  every later call to that server fail for the rest of the session
  (ADR-060).
* **Transport is a config choice.** stdio launches a subprocess, http
  connects to one that is already running; everything else is shared
  (ADR-061).
"""

from typing import Any

import pytest
from harness.config import McpServerSettings
from harness.mcp.adapter import (
    is_denied,
    is_destructive,
    refusal_reason,
    wrap_remote_tools,
)
from harness.mcp.client import McpConnectionError, McpToolError, SdkMcpClient
from harness.mcp.discovery import _target_for, connect_mcp_servers
from mcp.types import ToolAnnotations
from tests.fakes import remote_tool_definition, toy_mcp_server

from mcp import StdioServerParameters


def _annotated(
    name: str,
    read_only: bool | None = None,
    destructive: bool | None = None,
) -> Any:
    """Build a remote definition carrying the annotations under test."""
    tool = remote_tool_definition(name, description="Does a thing to some data.")
    if read_only is None and destructive is None:
        return tool
    tool.annotations = ToolAnnotations(
        read_only_hint=read_only, destructive_hint=destructive
    )
    return tool


# --- refusal by annotation (ADR-059) ---------------------------------------


def test_a_destructive_annotation_is_refused() -> None:
    """The case the word list cannot express, and the reason this exists."""
    tool = _annotated("sweep", read_only=False, destructive=True)

    assert is_destructive(tool) is True
    assert refusal_reason(tool) == "annotated destructive"
    # And the name itself is innocent, so nothing else would have caught it.
    assert is_denied("sweep") is False


def test_a_denied_name_still_wins_without_annotations() -> None:
    """The word list did not go away; it moved to being the second check."""
    assert refusal_reason(_annotated("delete_all")) == "denied name"


def test_a_destructive_tool_never_reaches_the_wrapped_list() -> None:
    """The refusal is enforced where registration happens, not only reported."""
    tools = wrap_remote_tools(
        server_name="toy",
        remote_tools=[
            _annotated("safe_read", read_only=True),
            _annotated("sweep", destructive=True),
        ],
        call=lambda name, arguments: "unused",
    )

    assert [tool.name for tool in tools] == ["toy__safe_read"]


# --- reconnect after a lost session (ADR-060) ------------------------------


def _connected() -> SdkMcpClient:
    """Return a client with a live in-process session."""
    client = SdkMcpClient(server_name="toy", target=toy_mcp_server(), timeout=5.0)
    client.connect()
    return client


def test_a_lost_session_is_restarted_and_the_call_retried() -> None:
    """The whole point: one crash must not disable a server for the session.

    The session is killed the way a dying subprocess kills it — the client
    handle goes away — and the very next call has to work anyway.
    """
    client = _connected()
    try:
        assert client.call_tool("add", {"a": 1, "b": 2}) == "3"

        client.close()  # stands in for the server dying under us
        client._ever_connected = True  # ...but not deliberately (see below)

        assert client.call_tool("add", {"a": 2, "b": 3}) == "5"
        assert client.reconnects == 1
    finally:
        client.close()


def test_a_tool_that_fails_is_not_retried() -> None:
    """A failing tool is not a broken transport, and a retry changes nothing.

    Retrying here would double every failed call's cost for no benefit, and
    would mean a tool with a side effect ran twice.
    """
    client = _connected()
    try:
        with pytest.raises(McpToolError):
            client.call_tool("explode", {})
        assert client.reconnects == 0
    finally:
        client.close()


def test_a_server_that_cannot_be_restarted_reports_the_failure() -> None:
    """One attempt, then the original error stands.

    A dead command is the case where reconnection cannot help, and the
    caller must still see a failure it can turn into an observation rather
    than a retry loop.
    """
    client = SdkMcpClient(
        server_name="gone",
        target=StdioServerParameters(command="definitely-not-a-real-binary"),
        timeout=2.0,
        connect_timeout=2.0,
    )
    client._ever_connected = True  # pretend it worked once, then died

    with pytest.raises((McpToolError, McpConnectionError)):
        client.call_tool("anything", {})

    assert client.reconnects == 0


# --- transport is configuration (ADR-061) ----------------------------------


def test_a_stdio_entry_produces_launch_parameters() -> None:
    """Unchanged behaviour for every server that exists today."""
    target = _target_for(
        McpServerSettings(name="local", command="python", args=["-m", "thing"])
    )

    assert isinstance(target, StdioServerParameters)
    assert target.command == "python"
    assert target.args == ["-m", "thing"]


def test_an_http_entry_produces_the_url() -> None:
    """The SDK reads the type: a string is a Streamable HTTP session."""
    target = _target_for(
        McpServerSettings(
            name="remote", transport="http", url="https://example.invalid/mcp"
        )
    )

    assert target == "https://example.invalid/mcp"


def test_an_unreachable_http_server_degrades_like_any_other() -> None:
    """The degradation policy does not change with the transport.

    Port 1 on localhost refuses immediately, so this exercises the failure
    path without a network round trip. The session must survive with the
    tools it does have, which here is none, plus a warning naming the server.
    """
    discovery = connect_mcp_servers(
        [
            McpServerSettings(
                name="remote",
                transport="http",
                url="http://127.0.0.1:1/mcp",
                timeout=2.0,
            )
        ],
        connect_timeout=5.0,
    )
    try:
        assert discovery.tools == []
        assert len(discovery.warnings) == 1
        assert "remote" in discovery.warnings[0]
        assert "unavailable" in discovery.warnings[0]
    finally:
        discovery.close()
