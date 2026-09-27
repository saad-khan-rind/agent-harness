"""Integration: does a fact recorded in one session reach the next one?

This file holds the deterministic half of the Week 2 memory deliverable, now
against the vector store; the end-to-end half, where a real model decides
what is worth remembering, is ``tests/e2e/test_agent_memory.py``
(ADR-073). Here the whole harness runs — ``main()`` loads and validates
``config.yaml``, creates the roots, builds the registry, composes the hooks,
drives the real ``AgentLoop`` against real files, and prints a real answer —
with only the model replaced (ADR-014) and the Qdrant *connection* swapped
for the in-process engine.

That second substitution needs justifying, because it is new. Only the
transport changes: the store, the embedder, the records, the filters and the
retrieval are all the shipped ones. What is lost is proof that HTTP works,
which is a property of ``qdrant-client`` rather than of this harness. What
is gained is that these tests run in a pipeline with no Docker, which is
what ``.github/workflows/ci.yml`` provides (ADR-069). The persistence claim is
preserved by sharing one client across two ``cli.main()`` calls — the same
role the volume plays in production.

The central test runs ``cli.main()`` **twice** over one collection and
asserts the fact appears in the messages the *second* run's model received.
That framing is the point. Asserting a record exists would only prove a
write happened; asserting on the second run's prompt proves the whole round
trip, and it is the only version of the test that fails if retrieval,
composition or the hook ordering breaks.
"""

from pathlib import Path

import pytest
from harness import cli, session
from harness.memory.sessions import SESSIONS_FILENAME
from tests.fakes import ScriptedLLM, call, text

# Memory sits beside the workspace, never inside it: that is the arrangement
# the harness enforces at startup and the one the container ships (ADR-035).
_CONFIG = """
llm_base_url: https://example.invalid
llm_model: test-model
temperature: 0.0
max_iterations: 20
workspace_path: workspace
memory_path: memory
skills_path: skills
max_tool_output_chars: 4000
max_search_results: 50
"""


@pytest.fixture
def project(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Set up a runnable project directory and chdir into it.

    Only ``config.yaml`` and the key are provided: the workspace and memory
    directories are left absent on purpose, so every test here also
    exercises the startup path that creates them.

    Returns:
        The project root; ``project / "memory"`` is the memory directory.
    """
    (tmp_path / "config.yaml").write_text(_CONFIG, encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("LLM_API_KEY", "test-key")
    return tmp_path


def _run(monkeypatch: pytest.MonkeyPatch, script: ScriptedLLM, task: str) -> int:
    """Run one task through ``cli.main`` with the model replaced."""
    monkeypatch.setattr(session, "OpenAICompatibleClient", lambda settings, **_: script)
    return cli.main([task])


def _system_prompt(script: ScriptedLLM) -> str:
    """Return the system message from the model's last request.

    The *last* request, because ``ScriptedLLM`` records the whole history
    every time; scanning all of them would double-count.
    """
    messages, _ = script.calls[-1]
    return str(messages[0]["content"])


def _observations(script: ScriptedLLM) -> list[str]:
    """Return every observation the model was shown on its last request."""
    messages, _ = script.calls[-1]
    return [str(m.get("content")) for m in messages if m.get("role") == "tool"]


# --- the deliverable ------------------------------------------------------


def test_a_fact_recorded_in_one_run_reaches_the_next_run(
    project: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Two processes, one collection: the persistence claim itself.

    Run one has the model record a preference. Run two is a completely
    separate ``cli.main()`` call — new settings, new store, new registry,
    new context — and the assertion is on what its model was *sent*.
    """
    first = ScriptedLLM(
        [
            call(
                "memory_store",
                text="Prefers answers in German",
                category="core_fact",
            ),
            text("Noted."),
        ]
    )
    assert _run(monkeypatch, first, "Remember that I prefer German") == 0

    second = ScriptedLLM([text("Verstanden.")])
    assert _run(monkeypatch, second, "Which language do you prefer answers in?") == 0

    assert "[agent] Prefers answers in German" in _system_prompt(second)


def test_the_next_run_can_search_for_something_not_injected(
    project: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Tier two, through the whole harness, and the capability files lacked.

    A ``project_note`` is not injected up front. The second session asks a
    question about it and reaches it with ``memory_search`` — using words
    that do not appear in the note and without knowing any identifier,
    which is precisely what ``memory_read(path)`` could not do (ADR-070).
    """
    first = ScriptedLLM(
        [
            call(
                "memory_store",
                text="Runs Fedora 41 with Podman, not Docker",
                category="project_note",
            ),
            text("Noted."),
        ]
    )
    _run(monkeypatch, first, "Remember my setup")

    second = ScriptedLLM(
        [
            call("memory_search", query="container runtime and operating system"),
            text("You use Podman."),
        ]
    )
    _run(monkeypatch, second, "What container runtime do I use?")

    # Not injected up front — that is what makes it tier two...
    assert "Podman" not in _system_prompt(second)
    # ...and reachable anyway, which is what makes tier two useful.
    assert any("Podman" in observation for observation in _observations(second))


def test_a_user_recorded_fact_outranks_an_agent_one_in_the_same_slot(
    project: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The ``/remember`` guarantee, through two whole runs.

    The user states their name; the agent then tries to change it in a
    later session and is refused with an observation it can act on. This is
    ADR-063's rule surviving the migration at the level the user sees it.
    """
    monkeypatch.setattr(
        session, "OpenAICompatibleClient", lambda settings, **_: ScriptedLLM([])
    )
    pending = ["/remember Name: Alice", "/exit"]
    monkeypatch.setattr(
        "builtins.input", lambda _="": pending.pop(0) if pending else _raise_eof()
    )
    cli.main([])

    second = ScriptedLLM(
        [
            call("memory_upsert_slot", slot_key="Name", text="Someone Else"),
            text("I could not change that."),
        ]
    )
    _run(monkeypatch, second, "My name is actually Someone Else")

    assert any("user wins" in observation for observation in _observations(second))


# --- confinement ----------------------------------------------------------


def test_the_file_tools_cannot_reach_the_memory_directory(
    project: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The root separation, through the whole harness rather than a unit.

    A unit test proves the resolver refuses. This proves the *wiring* does:
    that the tool the model was actually given is rooted at the workspace
    and that the refusal comes back as a recoverable observation.

    The stakes changed with the migration and are worth stating: the facts
    are no longer on this disk at all, so this now protects the run log
    rather than the memory the next session loads. The guarantee got
    stronger, not weaker — there is no file for ``write_file`` to reach
    even if it escaped.
    """
    memory = project / "memory"
    memory.mkdir(exist_ok=True)
    (memory / SESSIONS_FILENAME).write_text("# Sessions\n\n- a real run\n", "utf-8")

    script = ScriptedLLM(
        [
            call(
                "write_file",
                path="../memory/sessions.md",
                content="- a run that never happened\n",
            ),
            text("I could not do that."),
        ]
    )
    _run(monkeypatch, script, "Overwrite the session log")

    assert any("escapes the workspace" in obs for obs in _observations(script))
    assert "a run that never happened" not in (memory / SESSIONS_FILENAME).read_text(
        encoding="utf-8"
    )


def test_a_workspace_override_cannot_swallow_memory(
    project: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The same hole, reached through the command line instead of the config.

    ``--workspace ./memory`` bypasses the startup check unless the override
    is re-checked, which it is.
    """
    (project / "memory").mkdir(exist_ok=True)
    monkeypatch.setattr(
        session, "OpenAICompatibleClient", lambda settings, **_: ScriptedLLM([])
    )

    with pytest.raises(SystemExit, match="must not overlap"):
        cli.main(["--workspace", str(project / "memory"), "Do nothing"])


# --- skills ---------------------------------------------------------------


def _raise_eof() -> str:
    """Raise ``EOFError`` to end a scripted REPL session."""
    raise EOFError
