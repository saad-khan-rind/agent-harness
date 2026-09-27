"""Terminal application: argument parsing, REPL commands, error reporting.

The CLI is the only part of the harness a user touches directly, so its
failure modes matter as much as the loop's. These tests drive the real
``Repl`` and ``main`` with the model replaced.
"""

import io
from contextlib import redirect_stdout
from pathlib import Path
from typing import Any

import pytest
from harness import cli, console, session
from harness.config import load_settings
from harness.context import RunContext
from harness.llm.protocol import LLMError
from tests.fakes import FailingLLM, ScriptedLLM, text

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
    """A runnable project directory, chdir'd into, with a key in the env."""
    (tmp_path / "config.yaml").write_text(_CONFIG, encoding="utf-8")
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("LLM_API_KEY", "test-key")
    return workspace


def _install(monkeypatch: pytest.MonkeyPatch, llm: Any) -> None:
    """Replace the real client so nothing reaches the network.

    ``**_`` rather than a fixed signature: the CLI now passes the client an
    ``on_retry`` observer so the telemetry layer can see retries that happen
    below the loop (ADR-094). A fake that named every argument would have to
    be edited every time the client gains one, which is a test coupled to a
    constructor rather than to a behaviour.
    """
    monkeypatch.setattr(session, "OpenAICompatibleClient", lambda settings, **_: llm)


def _feed(monkeypatch: pytest.MonkeyPatch, lines: list[str]) -> None:
    """Script the REPL's stdin, raising EOF when the lines run out."""
    pending = list(lines)

    def _input(_: str = "") -> str:
        if not pending:
            raise EOFError
        return pending.pop(0)

    monkeypatch.setattr("builtins.input", _input)


# --- argument parsing -----------------------------------------------------


def test_one_shot_runs_and_exits(
    project: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A positional task runs once, prints, and returns 0 without a banner."""
    _install(monkeypatch, ScriptedLLM([text("done")]))

    assert cli.main(["say", "hello"]) == 0
    out = capsys.readouterr().out
    assert "Agent: done" in out
    assert "Agent Harness" not in out  # no banner in one-shot mode
    assert "tokens:" not in out


def test_quiet_hides_the_tool_trace(
    project: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """--quiet hides tool arrows and the usage line; the answer stays."""
    from tests.fakes import call

    _install(
        monkeypatch,
        ScriptedLLM(
            [
                call("list_files", path=".", prompt_tokens=10, completion_tokens=2),
                text("done", prompt_tokens=12, completion_tokens=4),
            ]
        ),
    )
    cli.main(["--quiet", "look"])

    out = capsys.readouterr().out
    assert "→ list_files" not in out
    assert "tokens:" not in out
    assert "Agent: done" in out


# --- REPL commands --------------------------------------------------------


def test_tools_command_lists_tools_and_the_delete_limit(
    project: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """/tools shows what the agent can do, and what it cannot."""
    _install(monkeypatch, ScriptedLLM([]))
    _feed(monkeypatch, ["/tools", "/exit"])
    cli.main([])

    out = capsys.readouterr().out
    assert "write_file" in out
    assert "deletion is not available" in out


def test_reset_clears_the_conversation(
    project: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """/reset drops the history, so the next turn starts from the system prompt."""
    llm = ScriptedLLM([text("first"), text("second")])
    _install(monkeypatch, llm)
    _feed(monkeypatch, ["one", "/reset", "two", "/exit"])
    cli.main([])

    assert "Conversation cleared" in capsys.readouterr().out
    # After the reset the second request carries only system + the new user
    # turn, with no trace of the first exchange.
    assert [m["role"] for m in llm.calls[1][0]] == ["system", "user"]


def test_unknown_command_is_reported(
    project: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A typo must not be sent to the model as a task."""
    _install(monkeypatch, ScriptedLLM([]))
    _feed(monkeypatch, ["/nonsense", "/exit"])
    cli.main([])

    assert "Unknown command" in capsys.readouterr().out


def test_multi_turn_history_is_kept(
    project: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The REPL is a conversation, not a series of one-shots (ADR-011)."""
    llm = ScriptedLLM([text("first"), text("second")])
    _install(monkeypatch, llm)
    _feed(monkeypatch, ["one", "two", "/exit"])
    cli.main([])

    assert [m["role"] for m in llm.calls[1][0]] == [
        "system",
        "user",
        "assistant",
        "user",
    ]


# --- error reporting ------------------------------------------------------


def test_llm_error_is_reported_and_the_repl_survives(
    project: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A dead endpoint must not throw away the user's conversation."""
    _install(monkeypatch, FailingLLM(LLMError("innkube is down")))
    _feed(monkeypatch, ["do something", "/exit"])

    assert cli.main([]) == 0
    assert "LLM error: innkube is down" in capsys.readouterr().out


def test_missing_api_key_exits_with_guidance(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The first thing a TA hits on a fresh checkout gets an actionable message."""
    (tmp_path / "config.yaml").write_text(_CONFIG, encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("LLM_API_KEY", raising=False)

    with pytest.raises(SystemExit, match="LLM_API_KEY is not set"):
        cli.main(["hi"])


# --- trace formatting helpers --------------------------------------------


def test_token_usage_is_shown_after_the_answer(
    project: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """One summary line after the answer; not a per-iteration ticker."""
    _install(
        monkeypatch,
        ScriptedLLM([text("done", prompt_tokens=120, completion_tokens=18)]),
    )
    cli.main(["hi"])

    out = capsys.readouterr().out
    agent_at = out.index("Agent: done")
    tokens_at = out.index("tokens:")
    assert tokens_at > agent_at
    assert "120 prompt" in out
    assert "18 completion" in out
    assert "session 138" in out


# --- CLI usability pins ---------------------------------------------------
# The mode-switching tests that accompanied these are gone with chat mode
# (ADR-024); the remaining cases pin the wait line, error marker, and
# interrupt behaviour.


def test_tool_error_is_marked_in_the_trace(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Failures get a ``!`` so they are not lost in a long arrow list."""
    hook = console.ConsoleTraceHook(enabled=True)

    hook.after_tool("read_file", "Error: No such file or directory: gone.txt")

    assert "← ! " in capsys.readouterr().out


def test_ctrl_c_during_a_turn_keeps_the_repl(
    project: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Ctrl-C while waiting on the model cancels the turn, not the process.

    The conversation survives, which is the point — a mistyped task should
    not cost the user their whole session.
    """

    class _InterruptLLM:
        def chat(self, messages: object, tools: object) -> object:
            raise KeyboardInterrupt

    _install(monkeypatch, _InterruptLLM())
    repl = cli.Repl(load_settings(), trace=False)

    repl.ask("hello")

    assert "Interrupted." in capsys.readouterr().out


def test_thinking_spinner_rewrites_one_line_then_freezes() -> None:
    """A TTY wait is one line, rewritten, then a finished line with a newline."""
    buf = io.StringIO()
    now = [100.0]
    spinner = console._ThinkingSpinner(
        "thinking (iteration 1/20)",
        stream=buf,
        glyphs=("|", "/", "-", "\\"),
        clock=lambda: now[0],
        interval=60.0,
    )
    spinner.tick()
    now[0] = 103.0
    spinner.tick()
    spinner.stop()

    text = buf.getvalue()
    assert "\r" in text
    assert text.endswith("\n")
    assert "thinking (iteration 1/20)" in text
    assert "3s" in text
    # The finished line uses the static ellipsis, not a leftover glyph.
    assert "… thinking (iteration 1/20)  3s" in text


class _EncodedOut:
    """A stdout stand-in whose encoding we control."""

    def __init__(self, encoding: str | None) -> None:
        self.encoding = encoding


class _FakeSpinner:
    """Records start/stop so the hook can be tested without a thread."""

    instances: list["_FakeSpinner"] = []

    def __init__(self, label: str, **_: object) -> None:
        self.label = label
        self.started = False
        self.stopped = False
        type(self).instances.append(self)

    def start(self) -> None:
        """Record that the hook asked the spinner to run."""
        self.started = True

    def stop(self) -> None:
        """Record that the hook asked the spinner to freeze."""
        self.stopped = True


def test_tty_thinking_line_starts_a_spinner(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """On a TTY the hook animates rather than printing the one-shot line."""
    _FakeSpinner.instances.clear()
    monkeypatch.setattr(console, "_spinner_enabled", lambda: True)
    monkeypatch.setattr(console, "_ThinkingSpinner", _FakeSpinner)

    hook = console.ConsoleTraceHook(enabled=True, max_iterations=20)
    context = RunContext(task="hi")
    context.iterations_used = 2
    hook.before_llm(context)

    assert capsys.readouterr().out == ""
    assert len(_FakeSpinner.instances) == 1
    assert _FakeSpinner.instances[0].started is True
    assert "2/20" in _FakeSpinner.instances[0].label
    hook.after_llm(context, None)
    assert _FakeSpinner.instances[0].stopped is True


# --- permission prompt (ADR-057) -------------------------------------------


class _Tty:
    """A stand-in stdin that claims to be a terminal."""

    def isatty(self) -> bool:
        """Report an interactive stream."""
        return True


class _Pipe:
    """A stand-in stdin that is not a terminal, like a piped or scripted run."""

    def isatty(self) -> bool:
        """Report a non-interactive stream."""
        return False


def _write_tool(workspace: Path) -> Any:
    """Return the real ``write_file`` tool, so the prompt shows a real name."""
    from harness.tools import build_registry

    return build_registry(workspace).get("write_file")


@pytest.mark.parametrize(
    ("answer", "expected"),
    [
        ("y", True),
        ("Y", True),
        ("yes", True),
        ("YES", True),
        ("  yes  ", True),
        ("ok", True),
        ("n", False),
        ("N", False),
        ("no", False),
        ("", False),
    ],
)
def test_the_prompt_reads_the_answer(
    monkeypatch: pytest.MonkeyPatch,
    workspace: Path,
    capsys: pytest.CaptureFixture[str],
    answer: str,
    expected: bool,
) -> None:
    """Every spelling of yes and no a person actually types.

    Case and surrounding space are not an answer, and neither is the exact
    word: someone approving a write for the ninth time types what comes to
    hand. A bare Enter is a refusal, because the safe answer has to be the
    one a distracted user gives by reflex.
    """
    monkeypatch.setattr("builtins.input", lambda *_: answer)
    confirmer = console.ConsoleConfirmer(stream=_Tty())

    assert confirmer.confirm(_write_tool(workspace), {"path": "a.txt"}) is expected
    assert "write_file" in capsys.readouterr().out


@pytest.mark.parametrize(
    ("answer", "allowed"),
    [
        ("a", True),
        ("allow", True),
        ("always", True),
        ("allow for session", True),
        ("d", False),
        ("deny", False),
        ("never", False),
        ("deny for session", False),
    ],
)
def test_the_session_answers_take_their_spellings_too(
    monkeypatch: pytest.MonkeyPatch,
    workspace: Path,
    capsys: pytest.CaptureFixture[str],
    answer: str,
    allowed: bool,
) -> None:
    """The two session answers are offered in words, so they answer in words.

    The prompt says "allow for session" and "deny for session"; a prompt
    that then only accepted a single letter would be telling the user
    something untrue about their own options (ADR-103).
    """
    monkeypatch.setattr("builtins.input", lambda *_: answer)
    confirmer = console.ConsoleConfirmer(stream=_Tty())
    tool = _write_tool(workspace)

    assert confirmer.confirm(tool, {}) is allowed
    # The decision stuck: the second call is not asked at all.
    monkeypatch.setattr("builtins.input", _never_asked)
    assert confirmer.confirm(tool, {}) is allowed


def _never_asked(prompt: str = "") -> str:
    """Stand-in for ``input`` that fails if the prompt comes back."""
    raise AssertionError(f"asked again after a session answer: {prompt!r}")


def test_a_typo_is_asked_again_rather_than_read_as_no(
    monkeypatch: pytest.MonkeyPatch,
    workspace: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The bug this replaced: everything but ``y`` was a refusal.

    Someone who means to approve a write and types ``ys`` had the call
    denied and saw the agent give up for no visible reason. A typo is not
    an answer, so the question is put again (ADR-103).
    """
    answers = iter(["ys", "yse", "yes"])
    monkeypatch.setattr("builtins.input", lambda *_: next(answers))
    confirmer = console.ConsoleConfirmer(stream=_Tty())

    assert confirmer.confirm(_write_tool(workspace), {}) is True
    out = capsys.readouterr().out
    assert "not one of the four answers" in out


def test_junk_runs_out_of_tries_and_refuses(
    monkeypatch: pytest.MonkeyPatch,
    workspace: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Bounded, and fails closed when it runs out.

    Asking again forever would hand a wedged front end an infinite loop,
    and an answer nobody managed to give is not a yes — the same rule the
    no-terminal case follows.
    """
    asked: list[str] = []

    def _answer(prompt: str = "") -> str:
        asked.append(prompt)
        return "maybe"

    monkeypatch.setattr("builtins.input", _answer)
    confirmer = console.ConsoleConfirmer(stream=_Tty())

    assert confirmer.confirm(_write_tool(workspace), {}) is False
    assert len(asked) == console.CONFIRM_ATTEMPTS
    assert "no usable answer" in capsys.readouterr().out


def test_the_prompt_offers_exactly_the_answers_it_accepts() -> None:
    """The prompt text and the parser are one table, or they drift.

    A documented answer the parser does not know reads, to the person
    typing it, as the agent ignoring them.
    """
    for letter in ("y", "n", "a", "d"):
        assert f"[{letter}]" in console.CONFIRM_PROMPT
    for choice in ("yes", "no", "allow_session", "deny_session"):
        assert console.CONFIRM_ANSWERS[choice]
    # No spelling may mean two things at once.
    seen: set[str] = set()
    for spellings in console.CONFIRM_ANSWERS.values():
        assert not (seen & spellings), seen & spellings
        seen |= spellings
    # The bare Enter is a refusal, and is the only empty answer.
    assert "" in console.CONFIRM_ANSWERS["no"]


def test_always_and_deny_for_session_stick_to_that_tool_only(
    monkeypatch: pytest.MonkeyPatch,
    workspace: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """``a`` approves and ``d`` refuses one tool for the session, per tool.

    A prompt that fires twelve times in one task gets answered without
    being read, which is worse than not asking — but the exemption has to
    be per tool, or one approval quietly becomes ``allow_all``. ``d`` is
    the mirror image for a person who has said no three times. The prompt
    also prints the policy's reason for asking ("changes state" for a
    write), so the question can be read (ADR-102).
    """
    from harness.permissions import build_policy
    from harness.tools import build_registry

    registry = build_registry(workspace)
    answers = iter(["a", "d", "n"])
    asked: list[str] = []

    def _answer(prompt: str) -> str:
        asked.append(prompt)
        return next(answers)

    monkeypatch.setattr("builtins.input", _answer)
    policy = build_policy("confirm_mutations")
    confirmer = console.ConsoleConfirmer(stream=_Tty(), explain=policy.tier_of)

    # `a`: approved now, and again without a question.
    assert confirmer.confirm(registry.get("write_file"), {}) is True
    assert "changes state" in capsys.readouterr().out
    assert confirmer.confirm(registry.get("write_file"), {}) is True
    assert len(asked) == 1
    # A different tool is a different decision: `d` refuses it, and again.
    assert confirmer.confirm(registry.get("edit_file"), {}) is False
    assert confirmer.confirm(registry.get("edit_file"), {}) is False
    assert len(asked) == 2
    # Neither answer leaked to a third tool.
    assert confirmer.confirm(registry.get("append_file"), {}) is False
    assert len(asked) == 3


def test_no_terminal_means_no(
    workspace: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """A run with nobody watching refuses rather than approving silently.

    This is the case that decides whether the whole feature is real. If a
    non-interactive run treated "cannot ask" as "yes", ``confirm_mutations``
    would be indistinguishable from ``allow_all`` in a container, a pipeline,
    or a test — every context where the prompt was the only protection.
    """
    confirmer = console.ConsoleConfirmer(stream=_Pipe())

    assert confirmer.confirm(_write_tool(workspace), {"path": "a.txt"}) is False
    assert "no terminal" in capsys.readouterr().out


def test_the_permissions_flag_overrides_the_config_file(
    project: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """``--permissions`` beats ``config.yaml``, as every other override does.

    Driven through ``main`` with a real write attempt: the flag is only worth
    anything if it reaches the policy the loop consults. The workspace stays
    empty because the call was refused before the tool ran, and stdin in a
    test is not a terminal, so ``confirm_mutations`` refuses without hanging.
    """
    from tests.fakes import call

    _install(
        monkeypatch,
        ScriptedLLM([call("write_file", path="a.txt", content="hi"), text("refused")]),
    )

    cli.main(["--permissions", "confirm_mutations", "write a file"])

    assert not (project / "a.txt").exists()
    assert "refused" in capsys.readouterr().out


def test_the_permissions_flag_accepts_auto(
    project: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``--permissions auto`` is accepted, as config.yaml and run.sh accept it.

    Resolved the same way the setting is: stdin in a test is not a terminal,
    so ``auto`` means ``allow_all`` and the write goes through (ADR-102).
    """
    from tests.fakes import call

    _install(
        monkeypatch,
        ScriptedLLM([call("write_file", path="a.txt", content="hi"), text("done")]),
    )

    assert cli.main(["--permissions", "auto", "write a file"]) == 0

    assert (project / "a.txt").read_text(encoding="utf-8") == "hi"


# --- traces ---------------------------------------------------------------


def test_a_one_shot_run_leaves_a_trace_file(
    project: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """With the defaults, every run is recorded under the memory root.

    The wiring — settings to tracer to exporter to file — is what this
    checks; the contents are ``tests/unit/test_span_trace.py``'s job
    (ADR-083, ADR-093).

    **One file per run, still.** Worth asserting rather than assuming: the
    memory hook now opens its own span, and if it did so before the run span
    existed it would start a second trace and write a second file. That is a
    bug this test caught once already, and the hook ordering that fixes it
    is invisible from anywhere else (ADR-093).
    """
    _install(monkeypatch, ScriptedLLM([text("done")]))

    assert cli.main(["hi"]) == 0

    traces = list((project.parent / "memory" / "traces").glob("*.jsonl"))
    assert len(traces) == 1
    lines = traces[0].read_text(encoding="utf-8").splitlines()
    assert lines[0].startswith('{"ts":')
    # Spans are written when they close, so the run's own span is last: it
    # is the one that finishes after everything inside it.
    assert '"span": "agent.run"' in lines[-1]
    assert '"event": "run_end"' in lines[-1]
    assert '"schema_version": 2' in lines[-1]


def test_traces_can_be_switched_off(
    project: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Off means no hook and no directory — not a hook that writes nothing."""
    (project.parent / "config.yaml").write_text(
        _CONFIG + "traces_enabled: false\n", encoding="utf-8"
    )
    _install(monkeypatch, ScriptedLLM([text("done")]))

    assert cli.main(["hi"]) == 0

    assert not (project.parent / "memory" / "traces").exists()


# --- /trace ---------------------------------------------------------------


def test_trace_command_replays_the_run_that_just_finished(
    project: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """``/trace`` with no argument is the question a person actually asks.

    The timeline is read back from the file the hook wrote, so what is
    shown is the record, not this process's memory of the run (ADR-087).
    """
    _install(monkeypatch, ScriptedLLM([text("done")]))
    _feed(monkeypatch, ["hi", "/trace", "/exit"])

    cli.main([])

    out = capsys.readouterr().out
    assert "run " in out and "  main  " in out and "answered" in out
    assert "1 model call" in out
    assert "0 tool calls" in out


def test_the_console_prints_the_top_level_answer_and_not_a_childs(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The answer is shown at run end; a delegated child's is not (ADR-110).

    Children share this hook, and their answer is a result for the parent
    rather than a reply to the user.
    """
    hook = console.ConsoleTraceHook(enabled=True)
    top = RunContext(task="")
    top.start_turn("hi")
    top.final_answer = "hello there"
    child = RunContext(task="", depth=1)
    child.start_turn("research")
    child.final_answer = "a child result"
    failed = RunContext(task="")
    failed.start_turn("hi")

    for context in (top, child, failed):
        hook.on_run_end(context)

    out = capsys.readouterr().out
    assert "Agent: hello there" in out
    assert "a child result" not in out
    assert out.count("Agent:") == 1


# --- what a permission decision looks like on the terminal (ADR-112) --------


def _decided(
    outcome: str,
    result: str,
    error_type: str | None = None,
    mode: str | None = "confirm_mutations",
    asked: bool = False,
) -> str:
    """Drive one call through the hook the way the loop does, return the output.

    ``asked`` stands in for the console prompt having put the question on
    screen between ``before_tool`` and ``after_permission``.
    """
    from harness.tools.base import ToolOutcome

    buffer = io.StringIO()
    terminal = console.Console()
    hook = console.ConsoleTraceHook(
        enabled=True, permission_mode=mode, console=terminal
    )
    with redirect_stdout(buffer):
        hook.before_tool("write_file", {"path": "a.txt"})
        if asked:
            terminal.mark_asked()
        hook.after_permission("write_file", outcome)
        hook.after_tool(
            "write_file",
            result,
            ToolOutcome(result, ok=error_type is None, error_type=error_type),
        )
    return buffer.getvalue()


def test_an_allowed_call_is_shown_once_as_an_arrow() -> None:
    """The ordinary case is unchanged: arrow, then result."""
    out = _decided("allow", "Created a.txt")

    assert out.count("write_file(path='a.txt')") == 1
    assert "→ write_file(path='a.txt')" in out
    assert "← Created a.txt" in out


def test_a_confirmed_call_is_not_shown_again_after_the_question() -> None:
    """The prompt showed the call; an arrow before it read as "already running"."""
    out = _decided("confirmed", "Created a.txt", asked=True)

    assert "→" not in out
    assert "← Created a.txt" in out


def test_a_call_allowed_for_the_session_still_shows_its_call_line() -> None:
    """Confirmed without a question: the result must not appear call-less.

    After "allow for session" the confirmer approves silently. The trace
    used to drop the call line anyway, so results showed up under nothing,
    which read as a log that was not telling the truth (ADR-113).
    """
    out = _decided("confirmed", "Created a.txt", asked=False)

    assert "→ write_file(path='a.txt')" in out
    assert "← Created a.txt" in out


def test_a_declined_call_prints_nothing_meant_for_the_model() -> None:
    """The prompt said "declined"; the model's instruction stays off the screen."""
    out = _decided(
        "declined",
        "Error: the user was asked to approve this call and declined. Do not retry.",
        error_type="permission.declined",
        asked=True,
    )

    assert out == ""


def test_a_denied_call_says_it_did_not_run_and_which_mode_refused_it() -> None:
    """One red line with the call and the mode, not an arrow and an error."""
    out = _decided(
        "deny",
        "Error: write_file is not permitted by the active permission policy.",
        error_type="permission.denied",
        mode="deny_mutations",
    )

    assert "→" not in out
    assert (
        "✗ write_file(path='a.txt') — not run: not allowed in deny_mutations mode"
        in out
    )
    assert "Do not" not in out and "Error:" not in out


def test_a_call_that_never_reached_the_policy_still_shows_what_was_tried() -> None:
    """A retired tool gets no permission decision; its call line must not vanish."""
    from harness.tools.base import ToolOutcome

    buffer = io.StringIO()
    hook = console.ConsoleTraceHook(enabled=True)
    with redirect_stdout(buffer):
        hook.before_tool("write_file", {"path": "a.txt"})
        hook.after_tool(
            "write_file",
            "Error: write_file is unavailable",
            ToolOutcome("x", ok=False, error_type="guard.tool_retired"),
        )

    out = buffer.getvalue()
    assert "→ write_file(path='a.txt')" in out
    assert "← ! Error: write_file is unavailable" in out


def test_answering_no_says_so_at_the_prompt(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """The reason is known at the prompt, so it is printed there."""
    from harness.tools.mutate import WriteFile

    class _Terminal(io.StringIO):
        def isatty(self) -> bool:
            return True

    monkeypatch.setattr("builtins.input", lambda _="": "n")
    confirmer = console.ConsoleConfirmer(stream=_Terminal())

    assert confirmer.confirm(WriteFile(Path(".")), {"path": "a.txt"}) is False
    assert "✗ declined — not run" in capsys.readouterr().out


def test_concurrent_children_each_print_their_own_call_line() -> None:
    """Held back per thread: B's call cannot be printed as A's (ADR-106, ADR-112).

    Two threads interleave exactly as concurrent sub-agents do — both
    announce a call before either is decided.
    """
    import threading

    buffer = io.StringIO()
    hook = console.ConsoleTraceHook(enabled=True)
    a_announced = threading.Event()
    b_announced = threading.Event()

    def child(name: str, mine: threading.Event, other: threading.Event) -> None:
        hook.before_tool("read_file", {"path": f"{name}.txt"})
        mine.set()
        other.wait(timeout=5)
        hook.after_permission("read_file", "allow")

    with redirect_stdout(buffer):
        first = threading.Thread(target=child, args=("a", a_announced, b_announced))
        second = threading.Thread(target=child, args=("b", b_announced, a_announced))
        first.start()
        second.start()
        first.join()
        second.join()

    out = buffer.getvalue()
    assert out.count("read_file(path='a.txt')") == 1
    assert out.count("read_file(path='b.txt')") == 1


def test_a_sub_agent_never_animates_and_says_whose_turn_it_is(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Concurrent children used to fight over one spinner line (ADR-113).

    Even on a terminal where the parent's line animates, a child prints one
    plain line naming its role, and cannot stop the parent's spinner.
    """
    monkeypatch.setattr(console, "_spinner_enabled", lambda: True)
    started: list[str] = []

    class _Spinner:
        def __init__(self, label: str) -> None:
            started.append(label)

        def start(self) -> None:
            """Pretend to animate."""

        def stop(self) -> None:
            started.append("stopped")

    monkeypatch.setattr(console, "_ThinkingSpinner", _Spinner)
    hook = console.ConsoleTraceHook(enabled=True)
    parent = RunContext(task="")
    parent.start_turn("research this")
    child = RunContext(task="", role="researcher", depth=1)
    child.start_turn("look it up")

    buffer = io.StringIO()
    with redirect_stdout(buffer):
        hook.before_llm(parent)
        hook.before_llm(child)
        hook.after_llm(child, object())

    assert len([label for label in started if label != "stopped"]) == 1
    assert "stopped" not in started
    assert "researcher thinking" in buffer.getvalue()


def test_other_output_waits_while_a_question_is_on_screen(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The question stays the last line until it is answered (ADR-113).

    A second thread — another sub-agent — tries to print while the prompt
    waits for input. Its line must come after the answer, not between the
    question and it, which is how a pending question used to scroll out of
    sight and leave the run waiting on something nobody could see.
    """
    import threading

    from harness.tools.mutate import WriteFile

    class _Terminal(io.StringIO):
        def isatty(self) -> bool:
            return True

    buffer = io.StringIO()
    question_shown = threading.Event()
    other_done = threading.Event()

    def _slow_answer(_: str = "") -> str:
        question_shown.set()
        # Give the other thread every chance to print in the middle.
        other_done.wait(timeout=0.5)
        buffer.write("<answered>\n")
        return "y"

    monkeypatch.setattr("builtins.input", _slow_answer)
    # One terminal, shared, exactly as the session wires it.
    terminal = console.Console()
    confirmer = console.ConsoleConfirmer(stream=_Terminal(), console=terminal)

    def _other_child() -> None:
        question_shown.wait(timeout=5)
        terminal.emit("  → web_search(query='x')")
        other_done.set()

    with redirect_stdout(buffer):
        other = threading.Thread(target=_other_child)
        other.start()
        assert confirmer.confirm(WriteFile(Path(".")), {"path": "a.txt"}) is True
        other.join(timeout=5)

    out = buffer.getvalue()
    assert out.index("<answered>") < out.index("web_search")


def test_a_sub_agents_lines_say_which_sub_agent_made_them(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Concurrent children interleave; each line names its child (ADR-120).

    An unlabelled ``→ write_file`` printed under "critic thinking" read as
    the critic writing, when the writer had. The parent's own lines stay
    unlabelled, including after a child on the same thread has finished.
    """
    from harness.tools.base import ToolOutcome

    hook = console.ConsoleTraceHook(enabled=True)
    child = RunContext(task="", role="writer", depth=1)
    parent = RunContext(task="")

    hook.on_run_start(child)
    hook.before_tool("write_file", {"path": "summary.md"})
    hook.after_permission("write_file", "allow")
    hook.after_tool("write_file", "Created summary.md", ToolOutcome("ok", ok=True))
    hook.on_run_end(child)
    hook.before_tool("read_file", {"path": "summary.md"})
    hook.after_permission("read_file", "allow")
    hook.after_tool("read_file", "summary.md (3 lines):", ToolOutcome("x", ok=True))
    hook.on_run_end(parent)

    out = capsys.readouterr().out
    assert "→ [writer] write_file(" in out
    assert "← [writer] Created summary.md" in out
    assert "→ read_file(" in out and "[writer] read_file" not in out
