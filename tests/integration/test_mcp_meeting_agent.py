"""Integration: the shipped server's tools, used by a running agent.

``tests/integration/test_mcp_time_plus.py`` proves the protocol works — discovery,
invocation and errors through a real subprocess. This file is the layer
above: ``cli.main()`` loads ``config.yaml``, launches
``mcp_server_time_plus`` as a child process, discovers its three tools,
offers them to the model beside ``read_file``, runs the tool the model picks
and feeds the result back into the loop until an answer comes out.

The unit suite covers each remote tool on its own. This file is the
agent-level path: a three-timezone slot, a no-overlap observation the
model can explain, and a reversed date range recovered inside one run.

The model is scripted, as everywhere else in ``tests/integration``: what is under
test is the harness and the server, not the model's competence, and CI has
no token (ADR-014).
"""

import json
import sys
from pathlib import Path

import pytest
from harness import cli, session
from tests.fakes import ScriptedLLM, call, text

# The repository root, so the child process can import the package. The
# tests chdir into a temporary project, so this is passed to the server as
# PYTHONPATH rather than relied on through the working directory.
REPO_ROOT = Path(__file__).resolve().parents[2]

_CONFIG = f"""
llm_base_url: https://example.invalid
llm_model: test-model
temperature: 0.0
max_iterations: 10
workspace_path: workspace
memory_enabled: false
skills_enabled: false
mcp_enabled: true
mcp_connect_timeout: 30.0
mcp_servers:
  - name: time_plus
    command: {sys.executable}
    args: ["-m", "mcp_server_time_plus", "--local-timezone", "Europe/Berlin"]
    transport: stdio
    timeout: 20.0
    env:
      PYTHONPATH: "{REPO_ROOT}"
"""


@pytest.fixture
def project(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A runnable project whose only MCP server is the one we ship."""
    (tmp_path / "config.yaml").write_text(_CONFIG, encoding="utf-8")
    (tmp_path / "workspace").mkdir()
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("LLM_API_KEY", "test-key")
    return tmp_path


def _run(monkeypatch: pytest.MonkeyPatch, script: ScriptedLLM, task: str) -> int:
    """Run one task through ``cli.main`` with the model replaced."""
    monkeypatch.setattr(session, "OpenAICompatibleClient", lambda settings, **_: script)
    return cli.main([task])


def _observations(script: ScriptedLLM) -> list[str]:
    """Return the tool results the model was shown, oldest first."""
    messages, _ = script.calls[-1]
    return [m["content"] for m in messages if m.get("role") == "tool"]


# --- find_meeting_slot -----------------------------------------------------


def test_the_agent_can_schedule_a_meeting_across_three_timezones(
    project: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """The whole point of the new tool, exercised as a user would ask for it.

    Berlin, Karachi and Sydney do have a common hour; the assertion is on the
    observation the loop fed back, because that JSON is what the model would
    have to write its answer from.
    """
    script = ScriptedLLM(
        [
            call(
                "time_plus__find_meeting_slot",
                timezones=["Europe/Berlin", "Asia/Karachi", "Australia/Sydney"],
                date="2026-09-09",
                duration_minutes=60,
                max_results=3,
            ),
            text("09:00 Berlin works: 12:00 in Karachi, 17:00 in Sydney."),
        ]
    )
    assert _run(monkeypatch, script, "when can Berlin, Karachi and Sydney meet?") == 0

    payload = json.loads(_observations(script)[-1])
    assert payload["organiser_timezone"] == "Europe/Berlin"
    assert payload["slots_found"] >= 1
    assert [view["timezone"] for view in payload["slots"][0]["local"]] == [
        "Europe/Berlin",
        "Asia/Karachi",
        "Australia/Sydney",
    ]
    assert "09:00 Berlin works" in capsys.readouterr().out


def test_no_common_time_comes_back_as_an_answer_the_agent_can_explain(
    project: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """ "They cannot meet" must not look like a tool failure.

    Karachi's working day ends at the moment New York's begins, so there is
    no slot. The observation is a normal result carrying each working day in
    UTC — enough for the model to say *why*, which an error string would not
    be.
    """
    script = ScriptedLLM(
        [
            call(
                "time_plus__find_meeting_slot",
                timezones=["Europe/Berlin", "Asia/Karachi", "America/New_York"],
                date="2026-09-09",
            ),
            text("No hour works: Karachi finishes as New York starts."),
        ]
    )
    assert _run(monkeypatch, script, "can Berlin, Karachi and New York meet?") == 0

    observation = _observations(script)[-1]
    assert not observation.startswith("Error:")
    payload = json.loads(observation)
    assert payload["slots_found"] == 0
    assert payload["no_overlap"]["working_days_utc"]["Asia/Karachi"]["end_utc"] == (
        "2026-09-09T13:00+00:00"
    )


# --- count_working_days ----------------------------------------------------


def test_a_reversed_date_range_is_recoverable_inside_one_run(
    project: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The failure contract for the new tool, through the whole harness.

    The model swaps the two dates, reads the message, and calls again the
    right way round. Validation messages are written as instructions
    rather than as complaints. The unit suite covers the same refusal on
    the server; this is the loop feeding that text back and retrying.
    """
    script = ScriptedLLM(
        [
            call(
                "time_plus__count_working_days",
                call_id="1",
                start_date="2026-09-11",
                end_date="2026-09-07",
            ),
            call(
                "time_plus__count_working_days",
                call_id="2",
                start_date="2026-09-07",
                end_date="2026-09-11",
            ),
            text("That is five working days."),
        ]
    )
    assert _run(monkeypatch, script, "how long is that week?") == 0

    first, second = _observations(script)
    assert first.startswith("Error:")
    assert "Swap them" in first
    assert json.loads(second)["working_days"] == 5
