"""Integration: both token allowances, through the real CLI.

The unit suite proves the ledger rolls and the client refuses. What only the
wired harness can show is where the enforcement sits and what a person sees
when it fires: that the one client ``cli.main()`` builds is the metered one,
that a sub-agent's calls land in the same window, that the line prints after
every answer whether or not ``--quiet`` is set, that a spent *window* stops
the run without killing the REPL, and — the new half (ADR-089) — that a spent
*prompt* allowance pauses instead of stopping, and that typing ``continue``
or ``use credit on the go`` gets the work moving again.

So these tests drive ``cli.main()`` with only the model replaced (ADR-014)
and read the ledger the run leaves on the memory volume.
"""

import json
from pathlib import Path

import pytest
from harness import cli, session
from harness.budget import BUDGET_FILENAME
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
notes_enabled: false
timezone: Europe/Berlin
subagents_enabled: {subagents}
budget_enabled: {enabled}
budget_daily_tokens: {limit}
budget_prompt_tokens: {prompt_limit}
budget_warn_fraction: 0.8
"""


@pytest.fixture
def project(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A runnable project directory with an empty memory root."""
    (tmp_path / "workspace").mkdir()
    (tmp_path / "memory").mkdir()
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("LLM_API_KEY", "test-key")
    return tmp_path


def write_config(
    project: Path,
    *,
    limit: int = 1_000_000,
    prompt_limit: int = 0,
    enabled: bool = True,
    subagents: bool = False,
) -> None:
    """Write ``config.yaml`` for this run.

    ``prompt_limit`` defaults to ``0`` — the checkpoint off — so that every
    test that is not about the checkpoint reads the same as it did before
    ADR-089 and cannot be paused by it accidentally.
    """
    (project / "config.yaml").write_text(
        _CONFIG.format(
            limit=limit,
            prompt_limit=prompt_limit,
            enabled=str(enabled).lower(),
            subagents=str(subagents).lower(),
        ),
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


def ledger(project: Path) -> dict[str, object]:
    """Return what the run left in ``memory/budget.json``."""
    path = project / "memory" / BUDGET_FILENAME
    return dict(json.loads(path.read_text(encoding="utf-8")))


# -- the line after every answer ----------------------------------------------


def test_the_total_is_persisted_and_continued_by_the_next_session(
    project: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Two one-shot runs on one memory root add to one window total."""
    write_config(project, limit=100_000)

    run(monkeypatch, ScriptedLLM([text("a", prompt_tokens=1_000)]), "first")
    run(monkeypatch, ScriptedLLM([text("b", prompt_tokens=1_000)]), "second")

    out = capsys.readouterr().out
    assert "budget: 1,000 / 100,000" in out
    assert "budget: 2,000 / 100,000" in out
    stored = ledger(project)
    assert stored["prompt_tokens"] == 2_000
    # The second session continued the first one's window rather than
    # starting its own — the start time is what makes that observable.
    assert stored["started_at"] is not None


# -- the hard stop: the window ------------------------------------------------


def test_an_exhausted_window_stops_the_run_and_keeps_the_repl(
    project: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Once the window is spent, the next prompt is refused before any call.

    The refusal names the numbers and the ways out, the standing is printed
    underneath it, and the session goes on — ``/exit`` still works.
    """
    write_config(project, limit=1_000)
    feed(monkeypatch, ["first", "second", "/exit"])
    script = ScriptedLLM([text("done", prompt_tokens=990, completion_tokens=10)])

    assert run(monkeypatch, script) == 0

    out = capsys.readouterr().out
    assert "Agent: done" in out
    assert "budget: 1,000 / 1,000 tokens in 24h (100%)" in out
    assert "EXHAUSTED" in out
    assert "Budget stop: token budget exhausted: 1,000 of 1,000 tokens" in out
    assert "use credit on the go" in out
    assert "budget_daily_tokens" in out
    # Exactly one model call was made: the second prompt never reached it.
    assert len(script.calls) == 1


def test_the_stop_lands_mid_run_when_the_limit_is_crossed(
    project: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A run that crosses the limit is cut at its next model call, not later.

    The tool call before the cut has already happened — the same is true of
    an endpoint failure — and the message says so rather than pretending
    otherwise.
    """
    write_config(project, limit=1_000)
    script = ScriptedLLM(
        [
            call("list_files", path=".", prompt_tokens=1_200, completion_tokens=10),
            text("never reached"),
        ]
    )

    run(monkeypatch, script, "look")

    out = capsys.readouterr().out
    assert "→ list_files" in out
    assert "never reached" not in out
    assert "Budget stop:" in out
    assert "1,210 of 1,000 tokens" in out
    assert len(script.calls) == 1


# -- the soft stop: one instruction -------------------------------------------


def test_a_spent_prompt_allowance_pauses_rather_than_stops(
    project: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """The per-prompt checkpoint is a question, and it is asked in those words.

    The window still has almost all of its allowance here — only the
    instruction has run long — so the wording has to be a pause the user can
    answer, not a refusal (ADR-089).
    """
    write_config(project, limit=100_000, prompt_limit=1_000)
    feed(monkeypatch, ["do something long", "/exit"])
    script = ScriptedLLM(
        [
            call("list_files", path=".", prompt_tokens=1_100, completion_tokens=10),
            text("never reached"),
        ]
    )

    assert run(monkeypatch, script) == 0

    out = capsys.readouterr().out
    assert "Paused: per-prompt budget reached" in out
    assert "1,110 of 1,000" in out
    assert "Type 'continue'" in out
    assert "never reached" not in out
    # Not a window stop: the day is barely touched, and saying otherwise
    # would send someone editing the wrong setting.
    assert "Budget stop:" not in out
    assert "EXHAUSTED" not in out


def test_continue_resumes_the_same_task_on_a_fresh_allowance(
    project: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """``continue`` is the whole recovery: one word, same conversation.

    The resumed turn must see the work already done — that is what makes it
    a continuation rather than a restart — so the assertion is on the
    messages the model was sent, not only on the output.
    """
    write_config(project, limit=100_000, prompt_limit=1_000)
    feed(monkeypatch, ["write the files", "continue", "/exit"])
    script = ScriptedLLM(
        [
            call(
                "write_file",
                path="one.txt",
                content="a",
                prompt_tokens=1_100,
                completion_tokens=10,
            ),
            text("both written", prompt_tokens=900, completion_tokens=10),
        ]
    )

    assert run(monkeypatch, script) == 0

    out = capsys.readouterr().out
    assert "Paused:" in out
    assert "Agent: both written" in out
    messages, _ = script.calls[-1]
    # The first turn's tool result is still there, so nothing was redone.
    assert any(message.get("role") == "tool" for message in messages)
    assert any("write the files" in str(message.get("content")) for message in messages)
    # And the allowance really was reset: the second turn spent 910 of its
    # own 1,000 without being paused again.
    assert out.count("Paused:") == 1


# -- credit -------------------------------------------------------------------


def test_credit_lets_a_stopped_run_carry_on_and_records_the_overspend(
    project: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """The way past a spent window, and the paper trail it leaves.

    Three things have to hold together: the refusal happens first, the
    consent is taken in words rather than through a flag, and what is spent
    afterwards is counted separately so nobody has to guess later how far
    past the limit the session went.
    """
    write_config(project, limit=1_000)
    feed(monkeypatch, ["first", "second", "use credit on the go", "continue", "/exit"])
    script = ScriptedLLM(
        [
            text("done", prompt_tokens=990, completion_tokens=10),
            text("done again", prompt_tokens=400, completion_tokens=20),
        ]
    )

    assert run(monkeypatch, script) == 0

    out = capsys.readouterr().out
    assert "Budget stop:" in out
    assert "Credit enabled for this session" in out
    assert "Agent: done again" in out
    assert "ON CREDIT" in out
    assert "420 on credit" in out
    stored = ledger(project)
    assert stored["credit_tokens"] == 420
    assert stored["prompt_tokens"] == 1_390


# -- sub-agents ---------------------------------------------------------------


def test_a_childs_calls_are_charged_to_the_same_window(
    project: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Delegation cannot spend outside the budget: the child shares the client.

    The parent's own session total does not include the child's tokens — a
    child has its own ``RunContext`` — but the ledger does, and the line
    printed to the user is the ledger's number.
    """
    write_config(project, limit=100_000, subagents=True)
    research = json.dumps(
        {
            "answer": "Nothing of note.",
            "evidence": [],
            "uncertainties": [],
            "confidence": "low",
        }
    )
    script = ScriptedLLM(
        [
            # Parent delegates.
            call(
                "delegate",
                call_id="d1",
                role="researcher",
                task="Look around.",
                prompt_tokens=5_000,
                completion_tokens=100,
            ),
            # Researcher child: one call, reported straight back.
            text(
                f"```json\n{research}\n```", prompt_tokens=3_000, completion_tokens=50
            ),
            # Parent answers.
            text("Nothing to report.", prompt_tokens=5_500, completion_tokens=20),
        ]
    )

    run(monkeypatch, script, "delegate a look around")

    out = capsys.readouterr().out
    assert "Agent: Nothing to report." in out
    # 5,000+100 + 3,000+50 + 5,500+20 — the child's 3,050 included.
    assert "budget: 13,670 / 100,000 tokens in 24h" in out
    # The session line, by contrast, is the parent's own context only.
    assert "(session 10,620)" in out
    assert ledger(project)["prompt_tokens"] == 13_500
