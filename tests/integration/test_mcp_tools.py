"""Integration: does a tool on a real MCP server reach the model and back?

This file holds the deterministic half of the Week 2 MCP deliverable; the
end-to-end half, against a real model, is ``tests/e2e/test_agent_mcp.py``
(ADR-073). Here the whole harness runs — ``main()`` loads and validates
``config.yaml``, launches the
server as a **real subprocess**, completes the MCP handshake, discovers its
tools, wraps them, registers them beside the built-ins, drives the real
``AgentLoop``, and prints a real answer — with only the model replaced
(ADR-014).

The subprocess is the point of doing it here rather than in the unit tests.
Those connect to an in-process server, which exercises the protocol but not
the transport; this is the only place where the thing the harness ships —
launch a program, talk over its stdin and stdout, shut it down — actually
runs.
"""

import sys
from pathlib import Path

import pytest
from harness import cli, session
from tests.fakes import (
    DENIED_TOOL_NAME,
    DESTRUCTIVE_TOOL_NAME,
    ScriptedLLM,
    call,
    text,
)

# The toy server, addressed absolutely: the tests run from a temporary
# project directory, so a relative path would not resolve.
_TOY_SERVER = Path(__file__).resolve().parents[1] / "mcp_toy_server.py"

_CONFIG = f"""
llm_base_url: https://example.invalid
llm_model: test-model
temperature: 0.0
max_iterations: 10
workspace_path: workspace
memory_path: memory
skills_path: skills
memory_enabled: false
skills_enabled: false
mcp_enabled: true
mcp_connect_timeout: 30.0
mcp_servers:
  - name: toy
    command: {sys.executable}
    args: ["{_TOY_SERVER}"]
    transport: stdio
    timeout: 20.0
"""

# The same project, with the server pointed at a command that cannot run.
_BROKEN_CONFIG = _CONFIG.replace(
    f"command: {sys.executable}", "command: definitely-not-a-real-binary"
).replace(f'args: ["{_TOY_SERVER}"]', "args: []")


@pytest.fixture
def project(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Set up a runnable project directory with one MCP server configured."""
    (tmp_path / "config.yaml").write_text(_CONFIG, encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("LLM_API_KEY", "test-key")
    return tmp_path


@pytest.fixture
def broken_project(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """The same project, but the configured server cannot be launched."""
    (tmp_path / "config.yaml").write_text(_BROKEN_CONFIG, encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("LLM_API_KEY", "test-key")
    return tmp_path


def _run(monkeypatch: pytest.MonkeyPatch, script: ScriptedLLM, task: str) -> int:
    """Run one task through ``cli.main`` with the model replaced."""
    monkeypatch.setattr(session, "OpenAICompatibleClient", lambda settings, **_: script)
    return cli.main([task])


def _tool_names(script: ScriptedLLM) -> list[str]:
    """Return the tool names sent to the model on its last request."""
    _, tools = script.calls[-1]
    return [tool["function"]["name"] for tool in tools]


# --- the deliverable ------------------------------------------------------


def test_a_remote_tool_is_discovered_and_offered_to_the_model(
    project: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Discovery, through the whole harness and a real subprocess.

    The assertion is on what the model was *sent*: a tool the harness never
    implemented, discovered at runtime, sitting in the same schema list as
    ``read_file``.
    """
    script = ScriptedLLM([text("Nothing to do.")])
    assert _run(monkeypatch, script, "hello") == 0

    names = _tool_names(script)
    assert "toy__add" in names
    # Beside the built-ins, not instead of them.
    assert "read_file" in names
    assert "write_file" in names


def test_the_model_can_call_a_remote_tool_and_use_the_result(
    project: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Invocation and result, the full loop: reason, act, observe, answer."""
    script = ScriptedLLM(
        [
            call("toy__add", a=2, b=40),
            text("The answer is 42."),
        ]
    )
    assert _run(monkeypatch, script, "add 2 and 40") == 0

    # The observation the second request carried is the server's own answer.
    messages, _ = script.calls[-1]
    observations = [m for m in messages if m.get("role") == "tool"]
    assert observations[-1]["content"] == "42"
    assert "The answer is 42." in capsys.readouterr().out


def test_a_remote_failure_is_an_observation_not_a_crash(
    project: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The brief's "errors must be communicated upstream effectively"."""
    script = ScriptedLLM(
        [
            call("toy__explode"),
            text("That tool failed, so I stopped."),
        ]
    )
    assert _run(monkeypatch, script, "make it explode") == 0

    messages, _ = script.calls[-1]
    observations = [m for m in messages if m.get("role") == "tool"]
    assert observations[-1]["content"].startswith("Error: ")


def test_a_deletion_tool_offered_by_a_server_never_reaches_the_model(
    project: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """ADR-008 held by construction until tools started arriving at runtime.

    The toy server really does advertise ``delete_everything``. The registry
    the model is shown must not contain it, and the user must be told it was
    refused rather than left to wonder.
    """
    script = ScriptedLLM([text("Nothing to do.")])
    assert _run(monkeypatch, script, "hello") == 0

    assert not any(DENIED_TOOL_NAME in name for name in _tool_names(script))
    output = capsys.readouterr().out
    assert DENIED_TOOL_NAME in output
    assert "refuses to register" in output


def test_a_destructive_annotation_is_refused_and_named_at_startup(
    project: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """The other half of ADR-008 at runtime, through the real CLI.

    ``sweep_stale_entries`` contains no denied word — a word list would have
    let it through — and declares ``destructive_hint: true``. It must not
    reach the model, and the startup line must say *why* it was refused, so
    an operator can tell a server's own declaration from our name matching
    (ADR-059).
    """
    script = ScriptedLLM([text("Nothing to do.")])
    assert _run(monkeypatch, script, "hello") == 0

    assert not any(DESTRUCTIVE_TOOL_NAME in name for name in _tool_names(script))
    output = capsys.readouterr().out
    assert f"{DESTRUCTIVE_TOOL_NAME} (annotated destructive)" in output
    assert f"{DENIED_TOOL_NAME} (denied name)" in output


# --- degradation ----------------------------------------------------------


def test_an_unavailable_server_does_not_stop_the_session(
    broken_project: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The degradation policy, through the real startup path.

    A server that will not launch costs its own tools. The harness still
    starts, the built-in tools are still there, and the user is told which
    server failed — the alternative, a harness that refuses to run because a
    weather server is down, would be worse.
    """
    script = ScriptedLLM(
        [call("list_files", path="."), text("The workspace is empty.")]
    )
    assert _run(monkeypatch, script, "what is in the workspace?") == 0

    names = _tool_names(script)
    assert "read_file" in names
    assert not any(name.startswith("toy__") for name in names)

    output = capsys.readouterr().out
    assert "toy" in output
    assert "unavailable" in output


# --- shutdown -------------------------------------------------------------
