"""Configuring MCP servers.

Servers are described in ``config.yaml`` and nowhere in code, so this file
is the guard on the only interface a user has to the feature. Every test
here is about failing at *startup*, with the offending key named, rather
than at the first tool call — a bad server entry that validates cleanly
would surface much later as a tool that mysteriously is not there.
"""

from pathlib import Path

import pytest
from harness.config import McpServerSettings, Settings, load_settings
from pydantic import ValidationError

_BASE_CONFIG = """
llm_base_url: https://example.invalid
llm_model: test-model
workspace_path: workspace
memory_path: memory
skills_path: skills
"""


def _settings(**overrides: object) -> Settings:
    """Build settings without touching the filesystem or a real key."""
    return Settings(llm_api_key="test-key", **overrides)  # type: ignore[arg-type]


# --- defaults -------------------------------------------------------------


def test_mcp_is_on_by_default_with_no_servers() -> None:
    """The shipped default: nothing to start, nothing to break (ADR-039)."""
    settings = _settings()
    assert settings.mcp_enabled is True
    assert settings.mcp_servers == []


def test_a_server_entry_has_working_defaults() -> None:
    """Only name and command should be mandatory to add a server."""
    server = McpServerSettings(name="time", command="uvx")
    assert server.transport == "stdio"
    assert server.enabled is True
    assert server.args == []
    assert server.env == {}
    assert server.denied_tools == []
    assert server.timeout > 0


# --- server names ---------------------------------------------------------


@pytest.mark.parametrize("name", ["Time", "time-server", ""])
def test_an_unusable_server_name_is_refused(name: str) -> None:
    """The name is prefixed onto every tool, so it must be a valid identifier.

    Caught here rather than downstream: a name that cannot form a function
    name would otherwise make every tool from that server silently vanish
    at wrap time, with nothing to point at the cause.
    """
    with pytest.raises(ValidationError, match="name"):
        McpServerSettings(name=name, command="uvx")


@pytest.mark.parametrize("name", ["time_server"])
def test_a_snake_case_server_name_is_accepted(name: str) -> None:
    """The names a user would reasonably write."""
    assert McpServerSettings(name=name, command="uvx").name == name


def test_two_servers_may_not_share_a_name() -> None:
    """Duplicates would collide in the registry, blaming tools for a config bug."""
    with pytest.raises(ValidationError, match="duplicate"):
        _settings(
            mcp_servers=[
                McpServerSettings(name="time", command="a"),
                McpServerSettings(name="time", command="b"),
            ]
        )


# --- the rest of the entry ------------------------------------------------


@pytest.mark.parametrize("command", ["", "   "])
def test_an_empty_command_is_refused(command: str) -> None:
    """Otherwise the failure arrives as a launch error, not a config error."""
    with pytest.raises(ValidationError, match="command"):
        McpServerSettings(name="time", command=command)


@pytest.mark.parametrize("timeout", [0, -1.0])
def test_a_non_positive_call_timeout_is_refused(timeout: float) -> None:
    """A zero timeout would fail every call on that server."""
    with pytest.raises(ValidationError, match="timeout"):
        McpServerSettings(name="time", command="uvx", timeout=timeout)


@pytest.mark.parametrize("timeout", [0, -0.5])
def test_a_non_positive_connect_timeout_is_refused(timeout: float) -> None:
    """Zero would abandon every server before it finished starting."""
    with pytest.raises(ValidationError, match="mcp_connect_timeout"):
        _settings(mcp_connect_timeout=timeout)


# --- reading it out of config.yaml ----------------------------------------


def test_servers_are_read_from_the_yaml_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The path a user actually takes: edit config.yaml, restart.

    Asserted through ``load_settings`` rather than by constructing the model,
    because the nested-list parsing is the part that could plausibly break.
    """
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("LLM_API_KEY", "test-key")
    config = tmp_path / "config.yaml"
    config.write_text(
        _BASE_CONFIG
        + """
mcp_enabled: true
mcp_connect_timeout: 12.5
mcp_servers:
  - name: time
    command: uvx
    args: ["mcp-server-time"]
    timeout: 7.5
    denied_tools: ["set_system_clock"]
  - name: notes
    command: python
    args: ["-m", "notes_server"]
    enabled: false
""",
        encoding="utf-8",
    )

    settings = load_settings(config, env_file=None)

    assert settings.mcp_connect_timeout == 12.5
    assert [server.name for server in settings.mcp_servers] == ["time", "notes"]
    first = settings.mcp_servers[0]
    assert first.command == "uvx"
    assert first.args == ["mcp-server-time"]
    assert first.timeout == 7.5
    assert first.denied_tools == ["set_system_clock"]
    assert settings.mcp_servers[1].enabled is False


def test_the_shipped_config_parses(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The committed config.yaml must load, MCP block included.

    Cheap, and it catches the specific mistake of documenting a field in
    YAML comments that the model does not actually accept.

    The list was empty when this test was written; it now carries the three
    servers this repository ships, so the assertion moved with it rather
    than being deleted. What it guards is unchanged: whatever is committed
    must parse, and a `docker run` with no arguments must start exactly the
    servers the file names (ADR-046).
    """
    monkeypatch.setenv("LLM_API_KEY", "test-key")
    monkeypatch.chdir(tmp_path)
    settings = load_settings(
        Path(__file__).resolve().parents[2] / "config.yaml", env_file=None
    )
    assert settings.mcp_enabled is True

    shipped = [server.name for server in settings.mcp_servers if server.enabled]
    assert shipped == ["time_plus", "calendar", "web"]

    time_plus = settings.mcp_servers[0]
    assert time_plus.transport == "stdio"
    # `python -m`, so the entry works in the image, in the test image and in
    # a developer's checkout without a path that only exists in one of them.
    assert time_plus.args[:2] == ["-m", "mcp_server_time_plus"]

    # Looked up by name rather than by index, so adding a fourth server
    # does not silently repoint these assertions at the wrong entry — which
    # is exactly what happened when the calendar was inserted.
    by_name = {server.name: server for server in settings.mcp_servers}

    calendar = by_name["calendar"]
    assert calendar.transport == "stdio"
    assert calendar.args[:2] == ["-m", "mcp_server_calendar"]
    # An absolute path, because the server inherits the harness's working
    # directory and a relative one would put a user's events somewhere that
    # vanishes with the container. A *file*, not a URL: upstream reads its
    # feed from ICS_CALENDAR_URL and a private feed URL is a secret, which
    # §8.2 forbids committing — moving it to a local file is the deviation
    # that made the fork usable here (ADR-071).
    assert (
        calendar.args[calendar.args.index("--calendar-file") + 1]
        == "/app/calendar/calendar.ics"
    )
    # No credentials of any kind. The four calendar servers rejected in the
    # survey each need a password, an API key or an OAuth file before they
    # will start, and the brief forbids committing secrets (8.2); the one we
    # forked needed only its feed URL moved to a local file (ADR-071).
    assert calendar.env == {}

    web = by_name["web"]
    assert web.transport == "stdio"
    assert web.args[:2] == ["-m", "mcp_server_web_search"]
    # The client's per-call deadline must exceed the server's own budget,
    # or a slow page times out on our side and the server's readable error
    # never arrives (ADR-055).
    assert web.timeout > float(web.args[web.args.index("--timeout") + 1])
