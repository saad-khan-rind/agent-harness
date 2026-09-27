"""Integration: the permission modes, through the real CLI.

``tests/unit/test_permissions.py`` proves the policy decides correctly and
that the loop consults it. What no unit test can show is the thing the
brief's §8.4 actually asks for — that a mode chosen in ``config.yaml`` or on
the command line reaches the one enforcement point (ADR-057) and changes
what happens on disk. That is a property of the wiring in ``cli.main()``,
so it is asserted here in the same shape as every other file in this
directory: the real CLI, the real registry, real files, a scripted model
(ADR-014).

Each test scripts a model that *tries* the thing the mode forbids. The
assertions are on the workspace and on the observation the model was
shown, not on the model's good manners.
"""

from pathlib import Path

import pytest
from harness import cli, console, session
from tests.fakes import ScriptedLLM, call, text

_CONFIG = """
llm_base_url: https://example.invalid
llm_model: test-model
temperature: 0.0
max_iterations: 20
workspace_path: workspace
memory_enabled: false
skills_enabled: false
max_tool_output_chars: 4000
max_search_results: 50
"""


@pytest.fixture
def project(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A runnable project with one file worth reading.

    Returns:
        The workspace directory the agent operates on.
    """
    (tmp_path / "config.yaml").write_text(_CONFIG, encoding="utf-8")
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "notes.md").write_text("read me\n", encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("LLM_API_KEY", "test-key")
    return workspace


def _run(monkeypatch: pytest.MonkeyPatch, script: ScriptedLLM, *argv: str) -> int:
    """Run ``cli.main(argv)`` with the model replaced."""
    monkeypatch.setattr(session, "OpenAICompatibleClient", lambda settings, **_: script)
    return cli.main(list(argv))


def _observations(script: ScriptedLLM) -> list[str]:
    """Return the tool results the model was shown, oldest first."""
    messages, _ = script.calls[-1]
    return [str(m["content"]) for m in messages if m.get("role") == "tool"]


def _set_mode(project: Path, mode: str) -> None:
    """Rewrite ``config.yaml`` with a ``permission_mode``."""
    (project.parent / "config.yaml").write_text(
        _CONFIG + f"permission_mode: {mode}\n", encoding="utf-8"
    )


# --- deny_mutations ---------------------------------------------------------


def test_deny_mutations_makes_the_run_read_only(
    project: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Reading works, writing is refused, and the run still ends with an answer.

    The refusal reaches the model as an ``Error:`` observation it can act
    on, the same shape as every other recoverable failure — so the model
    reads the file it was allowed to read and reports what it could not do.
    """
    _set_mode(project, "deny_mutations")

    script = ScriptedLLM(
        [
            call("read_file", call_id="1", path="notes.md"),
            call("write_file", call_id="2", path="out.md", content="copy"),
            text("I read notes.md but was not permitted to write out.md."),
        ]
    )
    assert _run(monkeypatch, script, "copy notes.md to out.md") == 0

    read, refused = _observations(script)
    assert "read me" in read
    assert refused.startswith("Error:")
    assert "not permitted" in refused
    assert not (project / "out.md").exists()
    assert "not permitted to write" in capsys.readouterr().out


# --- confirm_mutations ------------------------------------------------------


def test_confirm_mutations_fails_closed_without_a_terminal(
    project: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """No way to ask means no, never a silent yes.

    stdin under pytest is not a TTY, which is exactly the situation of a
    one-shot run in a script or a container without ``-it``. The call is
    declined, the user is told why, and the file is not written.
    """
    _set_mode(project, "confirm_mutations")

    script = ScriptedLLM(
        [
            call("write_file", call_id="1", path="out.md", content="x"),
            text("The write was declined."),
        ]
    )
    assert _run(monkeypatch, script, "write out.md") == 0

    (declined,) = _observations(script)
    assert declined.startswith("Error:")
    assert "declined" in declined
    assert not (project / "out.md").exists()
    assert "no terminal to ask on" in capsys.readouterr().out


def test_confirm_mutations_runs_the_call_when_the_user_approves(
    project: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The approval path, with a terminal and a ``y`` from the person at it.

    The confirmer is the CLI's own, with only its notion of "is this a
    terminal" and the ``input`` builtin replaced — so what runs is the
    prompt the user would see, not a stand-in.
    """
    _set_mode(project, "confirm_mutations")
    monkeypatch.setattr(console, "is_interactive", lambda stream: True)
    monkeypatch.setattr("builtins.input", lambda *_: "y")

    script = ScriptedLLM(
        [
            call("write_file", call_id="1", path="out.md", content="approved"),
            text("Written."),
        ]
    )
    assert _run(monkeypatch, script, "write out.md") == 0

    assert (project / "out.md").read_text(encoding="utf-8") == "approved"


# --- the flag and the guarantee -------------------------------------------


def test_allow_all_still_refuses_a_configured_denied_tool(
    project: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``denied_tools`` is honoured in every mode, including the permissive one.

    ADR-008's deny-list is a guarantee rather than a mode; ``denied_tools``
    extends it for one deployment, and "allow everything" must still mean
    "everything that was ever allowed".
    """
    (project.parent / "config.yaml").write_text(
        _CONFIG + "permission_mode: allow_all\ndenied_tools: [write_file]\n",
        encoding="utf-8",
    )

    script = ScriptedLLM(
        [
            call("write_file", call_id="1", path="out.md", content="x"),
            call("append_file", call_id="2", path="out.md", content="x"),
            text("write_file was refused, so I appended instead."),
        ]
    )
    assert _run(monkeypatch, script, "create out.md") == 0

    refused, allowed = _observations(script)
    assert refused.startswith("Error:")
    assert not allowed.startswith("Error:")
    assert (project / "out.md").read_text(encoding="utf-8") == "x"


# --- rules and auto (ADR-102) ----------------------------------------------


def test_a_deny_rule_in_config_is_enforced_through_the_cli(
    project: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``permission_rules.deny`` reaches the loop, and a glob is a glob.

    The whole path — YAML, ``Settings``, ``build_policy``, the loop — with
    a pattern rather than an exact name, because that is what a rule adds
    over ``denied_tools``.
    """
    (project.parent / "config.yaml").write_text(
        _CONFIG
        + "permission_mode: allow_all\npermission_rules:\n  deny: ['write_*']\n",
        encoding="utf-8",
    )

    script = ScriptedLLM(
        [
            call("write_file", call_id="1", path="out.md", content="x"),
            call("append_file", call_id="2", path="out.md", content="x"),
            text("write_file was refused, so I appended instead."),
        ]
    )
    assert _run(monkeypatch, script, "create out.md") == 0

    refused, allowed = _observations(script)
    assert refused.startswith("Error:")
    assert not allowed.startswith("Error:")
    assert (project / "out.md").read_text(encoding="utf-8") == "x"


def test_auto_is_allow_all_when_stdin_is_not_a_terminal(
    project: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The shipped default, in the context every test and container runs in.

    A default of ``auto`` must not turn a one-shot run into a session that
    refuses every write: with no terminal to ask on, it resolves to
    ``allow_all`` and the write lands. (Under pytest stdin is never a TTY,
    so no monkeypatching is needed to be in this case.)
    """
    # No permission_mode line at all: the config-file default is under test.
    script = ScriptedLLM(
        [
            call("write_file", call_id="1", path="out.md", content="x"),
            text("Written."),
        ]
    )
    assert _run(monkeypatch, script, "create out.md") == 0

    assert not _observations(script)[-1].startswith("Error:")
    assert (project / "out.md").read_text(encoding="utf-8") == "x"


def test_auto_is_confirm_mutations_on_a_terminal(
    project: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The same default, with a person present: the write is asked about.

    The prompt is answered ``n`` here, and the refusal is the *declined*
    wording rather than the policy's, which is how the test tells "asked
    and refused" from "never asked".
    """
    monkeypatch.setattr(console, "is_interactive", lambda stream: True)
    monkeypatch.setattr("builtins.input", lambda *_: "n")

    script = ScriptedLLM(
        [
            call("write_file", call_id="1", path="out.md", content="x"),
            text("You declined, so nothing was written."),
        ]
    )
    assert _run(monkeypatch, script, "create out.md") == 0

    assert "declined" in _observations(script)[-1]
    assert not (project / "out.md").exists()
