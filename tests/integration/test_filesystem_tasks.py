"""Integration tests for the file-system access capabilities.

These reproduce the two scenarios given in the Week 1 brief with the model
scripted. Everything else is real: ``main()`` parses arguments, loads and
validates ``config.yaml`` plus the environment, builds the registry, seeds
the real system prompt, drives the real ``AgentLoop``, executes the real
file tools against a real directory, and prints a real answer.

The single seam replaced is the model, which is what makes these
*integration* tests rather than end-to-end ones (ADR-073). The value of
scripting it is that the assertions are exact and the suite needs no token,
so it runs in CI and on a TA's machine; the cost is that it proves the
harness works, not that an agent can do the task.

The brief's two cases are therefore run **twice**, in two suites.
``tests/e2e/test_agent_brief_cases.py`` is the same two prompts against the real
InnKube model in a container, where the tool calls are the model's own
choices and the assertions are on what actually landed on disk. Neither
version replaces the other: this one fails when the wiring breaks, that one
fails when the agent cannot do the job.
"""

from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest
from harness import cli, session
from harness.prompts import DATE_SECTION_HEADER
from tests.fakes import ScriptedLLM, call, text

# Minimal config the harness will actually load. Written per test into a
# temporary working directory so no test depends on the repository's own
# config.yaml or leaves anything behind.
_CONFIG = """
llm_base_url: https://example.invalid
llm_model: test-model
temperature: 0.0
max_iterations: 20
workspace_path: workspace
max_tool_output_chars: 4000
max_search_results: 50
"""


@pytest.fixture
def project(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Set up a runnable project directory and chdir into it.

    Mirrors what the container provides: a ``config.yaml`` next to the
    process, a workspace folder, and the API key in the environment.

    Returns:
        The workspace path the agent will operate on.
    """
    (tmp_path / "config.yaml").write_text(_CONFIG, encoding="utf-8")
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("LLM_API_KEY", "test-key")
    return workspace


def _run(monkeypatch: pytest.MonkeyPatch, script: ScriptedLLM, task: str) -> int:
    """Run one task through ``cli.main`` with the model replaced.

    Args:
        monkeypatch: pytest fixture.
        script: The scripted model to drive the run.
        task: The user's prompt.

    Returns:
        The process exit code from ``main``.
    """
    # Patch the name as the CLI module resolved it, so the real constructor
    # (and its live network client) is never reached.
    monkeypatch.setattr(session, "OpenAICompatibleClient", lambda settings, **_: script)
    return cli.main([task])


def test_numbers_in_words(
    project: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Brief E2E case ``numbers_in_words``.

    Tools exercised: create_folder, write_file, search_text.
    Prompt: create a folder ``output`` containing ``numbers.md`` listing the
    numbers 1 to 20 in words.

    Asserts, verbatim from the brief:
      * folder ``output/`` exists;
      * ``output/numbers.md`` exists and contains the byte-exact texts
        'one' and 'twenty';
      * the final agent turn acknowledges.
    """
    words = [
        "one",
        "two",
        "three",
        "four",
        "five",
        "six",
        "seven",
        "eight",
        "nine",
        "ten",
        "eleven",
        "twelve",
        "thirteen",
        "fourteen",
        "fifteen",
        "sixteen",
        "seventeen",
        "eighteen",
        "nineteen",
        "twenty",
    ]
    body = "\n".join(f"{index}. {word}" for index, word in enumerate(words, start=1))

    script = ScriptedLLM(
        [
            call("create_folder", call_id="1", path="output"),
            call("write_file", call_id="2", path="output/numbers.md", content=body),
            # The agent verifies its own work before answering, which is
            # what the third listed tool is for.
            call("search_text", call_id="3", query="twenty", path="output"),
            text("I created output/numbers.md with the numbers 1 to 20 in words."),
        ]
    )

    exit_code = _run(
        monkeypatch,
        script,
        "Create a folder called output and inside it a file called numbers.md "
        "that contains a list with the numbers from 1 to 20 in words.",
    )

    assert exit_code == 0

    # Assert: folder output/ exists.
    assert (project / "output").is_dir()

    # Assert: file exists and contains the byte-exact texts.
    numbers = project / "output" / "numbers.md"
    assert numbers.is_file()
    raw = numbers.read_bytes()
    assert b"one" in raw
    assert b"twenty" in raw

    # Assert: the final agent turn acknowledges.
    printed = capsys.readouterr().out
    assert "Agent: I created output/numbers.md" in printed


def test_find_and_inspect(
    project: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Brief E2E case ``find_and_inspect``.

    Tools exercised: list_files, read_file.
    Setup: a pre-populated ``path_ABC/`` tree.
    Prompt: describe what is inside ``path_ABC/``; if ``notes.txt`` exists,
    read it and report its line count.

    Asserts, verbatim from the brief:
      * the assistant mentions both notes.txt and readme.md in the listing;
      * the assistant reports 5 lines inside notes.txt.
    """
    data = project / "path_ABC" / "data"
    data.mkdir(parents=True)
    (project / "path_ABC" / "empty").mkdir()
    # Byte-exact fixture from the brief: five lines, no trailing newline.
    (data / "notes.txt").write_text("This\nfile\nhas\none\nline", encoding="utf-8")
    (data / "readme.md").write_text("Kartoffelsalat", encoding="utf-8")

    script = ScriptedLLM(
        [
            call("list_files", call_id="1", path="path_ABC"),
            call("list_files", call_id="2", path="path_ABC/data"),
            call("read_file", call_id="3", path="path_ABC/data/notes.txt"),
            text(
                "path_ABC/ contains data/ and empty/. Inside data/ there are "
                "notes.txt and readme.md. notes.txt has 5 lines."
            ),
        ]
    )

    exit_code = _run(
        monkeypatch,
        script,
        "Tell me what is inside path_ABC/. If notes.txt exists, read it and "
        "tell me how many lines it has.",
    )

    assert exit_code == 0
    printed = capsys.readouterr().out

    # Assert: the assistant mentions both files.
    assert "notes.txt" in printed
    assert "readme.md" in printed

    # Assert: the assistant reports 5 lines.
    assert "5 lines" in printed

    # The line count must come from the tool, not from the model inventing
    # it — otherwise this test would pass with a broken read_file.
    observations = [
        message
        for messages, _ in script.calls
        for message in messages
        if message["role"] == "tool"
    ]
    assert any("notes.txt (5 lines)" in message["content"] for message in observations)


def test_agent_cannot_delete_and_reports_why(
    project: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """The brief's hard constraint, through the whole harness: no deletion is possible.

    The model asks for a delete tool, gets told it does not exist, and the
    file is still on disk afterwards.
    """
    victim = project / "keep.txt"
    victim.write_text("important", encoding="utf-8")

    script = ScriptedLLM(
        [
            call("delete_file", call_id="1", path="keep.txt"),
            text("I cannot delete files; no such tool is available."),
        ]
    )
    _run(monkeypatch, script, "Delete keep.txt")

    assert victim.read_text(encoding="utf-8") == "important"
    observation = [
        message
        for messages, _ in script.calls
        for message in messages
        if message["role"] == "tool"
    ][0]
    assert "unknown tool" in observation["content"]
    assert "Agent: I cannot delete files" in capsys.readouterr().out


def test_agent_cannot_escape_the_workspace(
    project: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The sandbox holds: a write above the root touches nothing."""
    outside = project.parent / "outside.txt"

    script = ScriptedLLM(
        [
            call("write_file", call_id="1", path="../outside.txt", content="leaked"),
            text("I cannot write outside the workspace."),
        ]
    )
    _run(monkeypatch, script, "Write leaked to ../outside.txt")

    assert not outside.exists()


def test_the_prompt_tells_the_model_what_day_it_is(
    project: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The deterministic half of ADR-079: the dates reach the model at all.

    The end-to-end tier asserts the agent books "next Monday" on a Monday,
    which is the property that matters and needs a real model. This asserts
    the mechanism underneath it, with no token and no network: that the
    system prompt a run actually sends carries today's date, its weekday,
    and the seven days after it.

    Asserted on what was *sent* rather than on the constant, because the
    section is set per task through ``set_system_section`` — a wiring
    mistake would leave the constant perfect and the prompt empty.
    """
    script = ScriptedLLM([text("Noted.")])
    _run(monkeypatch, script, "what day is it?")

    messages, _tools = script.calls[-1]
    system = messages[0]["content"]
    # The fixture's config.yaml does not set `timezone`, so the harness
    # default applies. Asserting against that default rather than against
    # Europe/Berlin keeps this test about the wiring rather than about which
    # zone the shipped config happens to choose.
    today = datetime.now(ZoneInfo("UTC"))

    assert DATE_SECTION_HEADER in system
    assert today.strftime("%A %Y-%m-%d") in system, (
        "the prompt does not say what day it is"
    )
    # Tomorrow too: the list is what turns "next Monday" into a lookup.
    tomorrow = today + timedelta(days=1)
    assert tomorrow.strftime("%a %Y-%m-%d") in system
