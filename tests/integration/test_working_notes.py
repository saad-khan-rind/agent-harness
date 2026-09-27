"""Integration: the scratchpad as the agent actually gets it.

The unit suite proves the file behaves and the hook injects. What only the
wired harness can show is that the notes are *there* — registered as tools on
the real registry, injected into the real conversation by ``cli.main()``,
carried from one turn to the next, kept out of the workspace, and dropped by
``/reset`` along with the task they belong to (ADR-090).
"""

from pathlib import Path

import pytest
from harness import cli, session
from harness.config import load_settings
from harness.memory.scratchpad import NOTES_SECTION_HEADER, SCRATCHPAD_FILENAME
from tests.fakes import ScriptedLLM, call, text

_CONFIG = """
llm_base_url: https://example.invalid
llm_model: test-model
temperature: 0.0
max_iterations: 0
workspace_path: workspace
memory_path: memory
memory_enabled: false
skills_enabled: false
mcp_enabled: false
metrics_enabled: false
traces_enabled: false
subagents_enabled: false
budget_enabled: false
notes_enabled: {enabled}
notes_max_chars: {max_chars}
"""


@pytest.fixture
def project(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A runnable project directory."""
    (tmp_path / "workspace").mkdir()
    (tmp_path / "memory").mkdir()
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("LLM_API_KEY", "test-key")
    return tmp_path


def write_config(
    project: Path, *, enabled: bool = True, max_chars: int = 4_000
) -> None:
    """Write ``config.yaml`` for this run."""
    (project / "config.yaml").write_text(
        _CONFIG.format(enabled=str(enabled).lower(), max_chars=max_chars),
        encoding="utf-8",
    )


def run(monkeypatch: pytest.MonkeyPatch, script: ScriptedLLM, *argv: str) -> int:
    """Run ``cli.main(argv)`` with only the model replaced."""
    monkeypatch.setattr(session, "OpenAICompatibleClient", lambda settings, **_: script)
    return cli.main(list(argv))


def feed(monkeypatch: pytest.MonkeyPatch, lines: list[str]) -> None:
    """Script the REPL's stdin, raising EOF when the lines run out."""
    pending = list(lines)

    def _input(_: str = "") -> str:
        if not pending:
            raise EOFError
        return pending.pop(0)

    monkeypatch.setattr("builtins.input", _input)


def system_prompt(script: ScriptedLLM) -> str:
    """Return the system message from the last request the model got."""
    messages, _ = script.calls[-1]
    return str(messages[0]["content"])


def tool_names(script: ScriptedLLM) -> set[str]:
    """Return the tool names offered on the last request."""
    _, schemas = script.calls[-1]
    return {schema["function"]["name"] for schema in schemas}


def test_a_note_written_in_one_turn_is_in_front_of_the_model_in_the_next(
    project: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The behaviour the whole feature is for.

    Two turns on one ``Repl``, because the claim is about what survives
    between them — a single run would prove only that the tool wrote a file.
    """
    write_config(project)
    settings = load_settings(Path("config.yaml"), env_file=None)
    script = ScriptedLLM(
        [
            call("note_write", content="- wrote f1.md\n- f2.md still to do"),
            text("noted"),
            text("carrying on"),
        ]
    )
    monkeypatch.setattr(session, "OpenAICompatibleClient", lambda settings, **_: script)
    repl = cli.Repl(settings, trace=False)
    try:
        repl.ask("start the job and keep notes")
        repl.ask("carry on")
    finally:
        repl.close()

    system = system_prompt(script)
    assert NOTES_SECTION_HEADER in system
    assert "f2.md still to do" in system


def test_the_notes_live_outside_the_workspace(
    project: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Under the memory root, where the file tools cannot reach them (ADR-035).

    A task that rewrites the workspace must not be able to corrupt the
    agent's own record of what it has done.
    """
    write_config(project)
    script = ScriptedLLM([call("note_write", content="- noted"), text("ok")])

    run(monkeypatch, script, "keep a note")

    assert (project / "memory" / SCRATCHPAD_FILENAME).exists()
    assert not (project / "workspace" / SCRATCHPAD_FILENAME).exists()


def test_reset_drops_the_notes_with_the_task(
    project: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """The notes belong to the task, so the gesture that ends one ends both.

    Otherwise a user who cleared the conversation would be left with the
    previous task's plan injected into the next one — state with no way to
    reach it.
    """
    write_config(project)
    settings = load_settings(Path("config.yaml"), env_file=None)
    script = ScriptedLLM(
        [
            call("note_write", content="- from the old task"),
            text("noted"),
            text("fresh"),
        ]
    )
    monkeypatch.setattr(session, "OpenAICompatibleClient", lambda settings, **_: script)
    repl = cli.Repl(settings, trace=False)
    try:
        repl.ask("keep a note")
        repl._command("/reset")
        repl.ask("something else")
    finally:
        repl.close()

    assert "from the old task" not in system_prompt(script)


# --- what the Thursday audit changed (ADR-105) ------------------------------


def test_a_note_written_mid_run_reaches_the_very_next_request(
    project: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The gap that made the feature weakest on the runs it exists for.

    Injecting only at ``on_run_start`` meant a model that wrote its plan at
    iteration 3 spent the rest of the run looking at the notes from before
    it. The new state was on disk and in the tool confirmation — and
    compaction shrinks a note body out of that confirmation as readily as
    any other oversized argument, so on a long run the freshest state was
    the one the model could not see.
    """
    write_config(project)
    script = ScriptedLLM(
        [
            call("note_write", content="- f1.md written\n- f2.md still to do"),
            call("list_files", path="."),
            text("done"),
        ]
    )

    run(monkeypatch, script, "start the job and keep notes")

    # The request *after* the note — still inside the same run — carries it.
    messages, _ = script.calls[1]
    assert "f2.md still to do" in str(messages[0]["content"])


def test_a_one_shot_run_does_not_inherit_the_last_ones_notes(
    project: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Two unrelated invocations are not one thread.

    `docker run … "summarise the ledger"` has nothing to do with whatever
    the previous container was asked, so inheriting its notes injects
    another task's plan under a heading that says "for this task". The REPL
    is the opposite case and is covered above: its turns *are* one thread.
    """
    write_config(project)
    first = ScriptedLLM([call("note_write", content="- from the old task"), text("ok")])
    run(monkeypatch, first, "the old task")

    second = ScriptedLLM([text("fresh")])
    run(monkeypatch, second, "an entirely different task")

    assert "from the old task" not in system_prompt(second)


def test_the_repl_still_carries_notes_between_turns(
    project: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The one-shot reset must not leak into the REPL.

    Same scratchpad, opposite requirement: a REPL's turns are one
    conversation, and clearing between them would throw away the state the
    next turn is about to need.
    """
    write_config(project)
    settings = load_settings(Path("config.yaml"), env_file=None)
    script = ScriptedLLM(
        [
            call("note_write", content="- halfway through"),
            text("noted"),
            text("carrying on"),
        ]
    )
    monkeypatch.setattr(session, "OpenAICompatibleClient", lambda settings, **_: script)
    repl = cli.Repl(settings, trace=False)
    try:
        repl.ask("start")
        repl.ask("continue")
    finally:
        repl.close()

    assert "halfway through" in system_prompt(script)
