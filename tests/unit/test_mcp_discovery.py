"""Connecting several servers, and surviving the ones that will not start.

The policy under test is the degradation policy: a server that fails costs
its own tools and nothing else, and the user is told which. These tests
matter more than they look, because the failure they guard against is
silent — a harness that starts fine, has fewer tools than the user expects,
and gives no reason.

Only the *far end* is swapped. ``connect_mcp_servers`` runs for real; what
``_stdio_parameters`` hands back is an in-process server object instead of a
subprocess, which the SDK's client accepts natively. The real stdio path is
covered through the whole harness in
``tests/integration/test_mcp_tools.py``.
"""

import pytest
from harness.config import McpServerSettings
from harness.mcp import discovery as discovery_module

# Bound before the autouse fixture replaces the module attribute, so these
# tests can still reach the real function.
from harness.mcp.discovery import _stdio_parameters as build_launch_parameters
from harness.mcp.discovery import connect_mcp_servers
from tests.fakes import DENIED_TOOL_NAME, toy_mcp_server


@pytest.fixture(autouse=True)
def _in_process_servers(monkeypatch: pytest.MonkeyPatch) -> None:
    """Connect to an in-process toy server instead of launching a subprocess.

    Keyed on the configured name, so a test with two servers really does get
    two distinct servers and can assert on the prefixes that separate them.
    """
    monkeypatch.setattr(
        discovery_module,
        "_stdio_parameters",
        lambda server: toy_mcp_server(server.name),
    )


def _server(name: str = "toy", **overrides: object) -> McpServerSettings:
    """Build one server entry with sensible defaults."""
    return McpServerSettings(
        name=name,
        command="unused",
        timeout=5.0,
        **overrides,  # type: ignore[arg-type]
    )


def test_no_servers_configured_is_a_working_configuration() -> None:
    """The shipped default. A bare run must behave as it did before."""
    result = connect_mcp_servers([])
    assert result.tools == []
    assert result.clients == []
    assert result.warnings == []


def test_tools_from_one_server_are_discovered() -> None:
    """The happy path, through the function the CLI actually calls."""
    result = connect_mcp_servers([_server("toy")])
    try:
        assert "toy__add" in [tool.name for tool in result.tools]
        assert len(result.clients) == 1
    finally:
        result.close()


def test_two_servers_both_contribute_without_colliding() -> None:
    """Same toy server twice: identical tool names, different prefixes.

    This is the scenario that motivated namespacing — two servers offering
    the same name with potentially different behaviour. Without the prefix
    the second registration would raise and startup would die.
    """
    result = connect_mcp_servers([_server("alpha"), _server("beta")])
    try:
        names = [tool.name for tool in result.tools]
        assert "alpha__add" in names
        assert "beta__add" in names
        assert len(names) == len(set(names))
    finally:
        result.close()


def test_a_refused_tool_is_reported_by_name() -> None:
    """The user must be able to find out why a tool is not offered."""
    result = connect_mcp_servers([_server("toy")])
    try:
        assert any(DENIED_TOOL_NAME in warning for warning in result.warnings)
        assert any("refuses to register" in warning for warning in result.warnings)
    finally:
        result.close()


def test_a_server_that_will_not_start_degrades_the_run(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The stated policy: lose that server's tools, keep the session."""
    from mcp import StdioServerParameters

    def _broken(server: McpServerSettings) -> object:
        # One server is real, the other points at a command that cannot run.
        if server.name == "ghost":
            return StdioServerParameters(command="not-a-real-binary-at-all", args=[])
        return toy_mcp_server(server.name)

    monkeypatch.setattr(discovery_module, "_stdio_parameters", _broken)

    result = connect_mcp_servers(
        [_server("ghost"), _server("toy")], connect_timeout=10.0
    )
    try:
        # The healthy server still contributed everything it had.
        assert "toy__add" in [tool.name for tool in result.tools]
        assert [client.server_name for client in result.clients] == ["toy"]
        # And the broken one is named, with a reason.
        assert any(
            "ghost" in warning and "unavailable" in warning
            for warning in result.warnings
        )
    finally:
        result.close()


def test_close_shuts_every_client_down() -> None:
    """A stdio server is a child process; one leak per session adds up."""
    result = connect_mcp_servers([_server("alpha"), _server("beta")])
    result.close()
    for client in result.clients:
        assert client._thread is None


def test_the_configured_command_becomes_the_launch_parameters() -> None:
    """The one line that turns a config entry into a subprocess spec."""
    parameters = build_launch_parameters(
        McpServerSettings(name="toy", command="python", args=["-m", "toy_server"])
    )
    assert parameters.command == "python"
    assert parameters.args == ["-m", "toy_server"]
