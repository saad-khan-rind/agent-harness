"""Integration: the calendar server, as a real subprocess, driven by the agent.

The full stack, with only the model replaced (ADR-014): ``cli.main()`` reads
``config.yaml``, launches ``mcp_server_calendar`` as a **child process**,
discovers its five tools over stdio, offers them to the model beside the
file tools, runs whichever the model picks and feeds the result back into
the loop until an answer comes out.

Two things are only checkable at this level, and they are why this file
exists rather than only the in-process contract tests:

* **the process boundary works** — the server starts from `config.yaml`
  with a `--calendar-dir` it has never seen, creates it, and writes files a
  separate process can read back;
* **the tools compose in one run** — the model searches for an event, feeds
  the UID it got back into a reschedule call, and writes the answer to a
  file. Each call's arguments come out of the previous call's observation,
  which no per-tool test covers.
"""

import json
import sys
from datetime import date, timedelta
from pathlib import Path

import pytest
from harness import cli, session
from tests.fakes import ScriptedLLM, call, text

# The repository root, so the child process can import the package. The
# tests chdir into a temporary project, so this is passed as PYTHONPATH
# rather than relied on through the working directory.
REPO_ROOT = Path(__file__).resolve().parents[2]


def _next_monday() -> date:
    """Return a Monday at least a week away, as ``YYYY-MM-DD`` material.

    The events these tests create are dated relative to today rather than
    on a fixed day, because the server runs as a subprocess on the *real*
    clock — there is no way to freeze it from ``config.yaml`` — and two of
    its tools default their window to today: ``search_events`` and
    ``get_events_today``. A fixed date works until the calendar reaches it
    and then every search comes back empty. A reschedule test also needs
    its event genuinely in the future, or "never offers the current slot"
    is true for the wrong reason.

    A Monday, so the working-week assumptions in the scenarios hold, and
    at least seven days out, so the whole reschedule window is ahead of
    ``now`` whatever weekday the suite runs on.
    """
    today = date.today()
    ahead = (7 - today.weekday()) % 7 or 7
    return today + timedelta(days=ahead + 7)


MONDAY = _next_monday().isoformat()

_CONFIG = f"""
llm_base_url: https://example.invalid
llm_model: test-model
temperature: 0.0
max_iterations: 12
workspace_path: workspace
memory_enabled: false
skills_enabled: false
mcp_enabled: true
mcp_connect_timeout: 30.0
mcp_servers:
  - name: calendar
    command: {sys.executable}
    args:
      - "-m"
      - "mcp_server_calendar"
      - "--calendar-file"
      - "calendar/calendar.ics"
      - "--local-timezone"
      - "Europe/Berlin"
    transport: stdio
    timeout: 20.0
    env:
      PYTHONPATH: "{REPO_ROOT}"
"""

PREFIX = "calendar__"


@pytest.fixture
def project(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A runnable project whose only MCP server is the calendar.

    The calendar directory is deliberately *not* created: the server making
    it on first use is what lets a bare `docker run` with no volumes work,
    and that is worth proving rather than assuming.
    """
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
    """Return the observations the model was shown, oldest first."""
    messages, _ = script.calls[-1]
    return [str(m.get("content")) for m in messages if m.get("role") == "tool"]


def _create(summary: str, start: str, end: str, call_id: str) -> object:
    """Script one ``create_event`` call."""
    return call(
        f"{PREFIX}create_event",
        call_id=call_id,
        summary=summary,
        start=start,
        end=end,
    )


# --- the tools arrive ------------------------------------------------------


# --- the tools work through the agent --------------------------------------


def test_an_event_created_in_one_run_is_visible_in_the_next(
    project: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Two processes, one calendar volume: the persistence claim.

    Each ``cli.main()`` starts a fresh server subprocess, so what carries
    the event across is the directory, exactly as the volume does in the
    container.
    """
    first = ScriptedLLM(
        [
            _create("Design review", f"{MONDAY}T11:00", f"{MONDAY}T12:00", "1"),
            text("Added."),
        ]
    )
    assert _run(monkeypatch, first, "add the design review") == 0

    second = ScriptedLLM(
        [
            call(
                f"{PREFIX}get_events_range",
                call_id="1",
                start_date=MONDAY,
                end_date=MONDAY,
            ),
            text("You have the design review at 11:00."),
        ]
    )
    assert _run(monkeypatch, second, "what is on Monday?") == 0

    assert "Design review" in _observations(second)[-1]


def test_the_agent_searches_then_reschedules_using_the_uid_it_found(
    project: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The chain the demo shows, and the case a per-tool test cannot cover.

    The reschedule call's ``event_uid`` is not in the script's imagination:
    it is read out of the search observation by this test the same way the
    model would read it, so what is proven is that the two tools compose.
    """
    setup = ScriptedLLM(
        [
            _create("Standup", f"{MONDAY}T09:00", f"{MONDAY}T09:30", "1"),
            _create("Design review", f"{MONDAY}T11:00", f"{MONDAY}T12:00", "2"),
            _create("Client call", f"{MONDAY}T11:30", f"{MONDAY}T12:30", "3"),
            text("Added three."),
        ]
    )
    assert _run(monkeypatch, setup, "set up Monday") == 0

    finder = ScriptedLLM(
        [
            call(f"{PREFIX}search_events", call_id="1", query="design review"),
            text("Found it."),
        ]
    )
    assert _run(monkeypatch, finder, "find the design review") == 0
    uid = json.loads(_observations(finder)[-1])["events"][0]["uid"]

    script = ScriptedLLM(
        [
            call(f"{PREFIX}suggest_reschedule_slots", call_id="1", event_uid=uid),
            call(
                "write_file",
                call_id="2",
                path="options.md",
                content="# Reschedule options\n\nSee the tool output.\n",
            ),
            text("Three options, written to options.md."),
        ]
    )
    assert _run(monkeypatch, script, "the design review clashes - when else?") == 0

    suggestion, written = _observations(script)
    payload = json.loads(suggestion)

    assert [c["summary"] for c in payload["conflicts_with"]] == ["Client call"]
    assert payload["slots_found"] == 3
    # Three different days, not three quarter-hours in one morning.
    assert len({slot["day"] for slot in payload["slots"]}) == 3
    assert not written.startswith("Error:")
    assert (project / "workspace" / "options.md").is_file()


# --- failures are recoverable inside one run -------------------------------


def test_a_made_up_uid_is_recoverable_without_ending_the_run(
    project: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The recovery contract, at the boundary that matters most here.

    A model that invents a UID gets a message naming the two tools that
    produce real ones, and finds its way inside the same run — rather than
    the run dying on an exception from a subprocess.
    """
    setup = ScriptedLLM(
        [
            _create("Standup", f"{MONDAY}T09:00", f"{MONDAY}T09:30", "1"),
            text("Added."),
        ]
    )
    _run(monkeypatch, setup, "add a standup")

    script = ScriptedLLM(
        [
            call(
                f"{PREFIX}suggest_reschedule_slots",
                call_id="1",
                event_uid="the-standup",
            ),
            call(f"{PREFIX}search_events", call_id="2", query="standup"),
            text("Found the real one."),
        ]
    )
    assert _run(monkeypatch, script, "move the standup") == 0

    refused, found = _observations(script)
    assert refused.startswith("Error:")
    assert "search_events" in refused
    assert not found.startswith("Error:")


def test_a_corrupt_calendar_file_is_refused_rather_than_replaced(
    project: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The fork adds the only write this server has, and it appends.

    A file that will not parse means either losing it or refusing.
    Refusing is the only safe answer when there is no delete tool to undo a
    mistake with, and the check runs in the *server* because the harness
    cannot police what a separate process does to its own file.
    """
    corrupt = project / "calendar" / "calendar.ics"
    corrupt.parent.mkdir(parents=True, exist_ok=True)
    corrupt.write_text("this is not iCalendar at all", encoding="utf-8")

    script = ScriptedLLM(
        [
            call(
                f"{PREFIX}create_event",
                call_id="1",
                summary="Standup",
                start=f"{MONDAY}T09:00",
                end=f"{MONDAY}T10:00",
            ),
            text("I could not add that."),
        ]
    )
    assert _run(monkeypatch, script, "add a standup") == 0

    assert _observations(script)[-1].startswith("Error:")
    assert corrupt.read_text(encoding="utf-8") == "this is not iCalendar at all"


def test_a_recurring_event_reaches_the_agent_expanded(
    project: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The capability the fork bought, through the whole stack.

    A weekly standup written once, read a week later through a real
    subprocess. Upstream's ``recurring_ical_events`` expansion is what puts
    it on the 21st; without it the agent would be told that Monday is free.
    """
    calendar = project / "calendar" / "calendar.ics"
    calendar.parent.mkdir(parents=True, exist_ok=True)
    calendar.write_text(
        "BEGIN:VCALENDAR\r\nVERSION:2.0\r\nPRODID:-//test//EN\r\n"
        "BEGIN:VEVENT\r\nUID:standup-weekly\r\nSUMMARY:Weekly standup\r\n"
        "DTSTART;TZID=Europe/Berlin:20260907T090000\r\n"
        "DTEND;TZID=Europe/Berlin:20260907T093000\r\n"
        "RRULE:FREQ=WEEKLY;BYDAY=MO\r\nEND:VEVENT\r\nEND:VCALENDAR\r\n",
        encoding="utf-8",
    )

    script = ScriptedLLM(
        [
            call(
                f"{PREFIX}get_events_range",
                call_id="1",
                start_date="2026-09-21",
                end_date="2026-09-21",
            ),
            text("You have the standup at 09:00."),
        ]
    )
    assert _run(monkeypatch, script, "what is on Monday the 21st?") == 0

    observation = json.loads(_observations(script)[-1])
    assert observation["count"] == 1
    assert observation["events"][0]["start"].startswith("2026-09-21T09:00")
