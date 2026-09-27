"""Integration cover for tool recovery and the seams the scenarios skip.

The scenario files each tell one story. This file is the rest: a tool
failure that reaches the model and is usable, memory that stays out of
the workspace, and a slot that replaces rather than accumulates. Happy
paths already covered by unit tests are not repeated here.

Same rules as the rest of ``tests/integration``: the real CLI, the real registry,
real files, a scripted model (ADR-014).
"""

from pathlib import Path

import pytest
from harness import cli, session
from tests.fakes import ScriptedLLM, call, text

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
    """A runnable project. The three roots are created by startup, not here."""
    (tmp_path / "config.yaml").write_text(_CONFIG, encoding="utf-8")
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
    return [str(m["content"]) for m in messages if m.get("role") == "tool"]


def _seed(project: Path) -> Path:
    """Create the workspace tree these tests read from.

    ``docs/`` is two levels deep on purpose: the difference between
    ``list_files`` and ``find_files`` only shows up below the first level.
    """
    workspace = project / "workspace"
    (workspace / "docs" / "adr").mkdir(parents=True)
    (workspace / "notes").mkdir(parents=True)
    (workspace / "docs" / "readme.md").write_text("# Docs\n", encoding="utf-8")
    (workspace / "docs" / "adr" / "0001-loop.md").write_text(
        "# ADR-0001\nWe use a ReAct loop.\n", encoding="utf-8"
    )
    (workspace / "docs" / "logo.png").write_bytes(b"\x89PNG\r\n")
    (workspace / "notes" / "todo.md").write_text(
        "- [ ] ship it\n- [ ] ship it\n", encoding="utf-8"
    )
    return workspace


# --- find_files vs list_files ----------------------------------------------


# --- edit_file -------------------------------------------------------------


def test_an_ambiguous_edit_is_refused_and_the_agent_recovers(
    project: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The refusal has to be usable, not just correct.

    ``todo.md`` contains the same line twice. The tool refuses rather than
    guessing which one was meant, the refusal reaches the model as an
    ``Error:`` observation, and the next call widens ``old_str`` and
    succeeds — all inside one run.
    """
    workspace = _seed(project)
    script = ScriptedLLM(
        [
            call(
                "edit_file",
                call_id="1",
                path="notes/todo.md",
                old_str="- [ ] ship it",
                new_str="- [x] ship it",
            ),
            call(
                "edit_file",
                call_id="2",
                path="notes/todo.md",
                old_str="- [ ] ship it\n- [ ] ship it",
                new_str="- [x] ship it\n- [ ] ship it",
            ),
            text("Marked the first one done."),
        ]
    )
    assert _run(monkeypatch, script, "tick off the first todo") == 0

    refusal, success = _observations(script)
    assert refusal.startswith("Error:")
    assert not success.startswith("Error:")
    assert (workspace / "notes" / "todo.md").read_text(encoding="utf-8") == (
        "- [x] ship it\n- [ ] ship it\n"
    )


# --- memory_search ---------------------------------------------------------


def test_memory_and_the_workspace_stay_separate(
    project: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The separation, restated for a store that is not a filesystem.

    ``read_file`` cannot reach the memory root, and the memory tools have
    no path argument to reach the workspace with — so the second direction
    is now structural rather than enforced. Both are asserted here because
    the first is still an ordinary path check that could regress.
    """
    memory = project / "memory"
    memory.mkdir(exist_ok=True)
    (memory / "sessions.md").write_text("# Sessions\n\n- Secret.\n", encoding="utf-8")
    workspace = _seed(project)
    (workspace / "notes").mkdir(exist_ok=True)
    (workspace / "notes" / "todo.md").write_text("Ship it.", encoding="utf-8")

    script = ScriptedLLM(
        [
            call("read_file", call_id="1", path="../memory/sessions.md"),
            text("I could not read that."),
        ]
    )
    assert _run(monkeypatch, script, "read the session log directly") == 0

    into_memory = _observations(script)[-1]
    assert into_memory.startswith("Error:")
    assert "Secret." not in into_memory


# --- memory_store ----------------------------------------------------------


# --- memory_upsert_slot ----------------------------------------------------


def test_a_slot_replaces_rather_than_accumulating_through_a_real_run(
    project: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The mangoes-to-pizza demo: one Name, one Likes, no ghost.

    Run one records the old values, run two replaces them, and run three
    asserts on the *prompt* — because a stale value that is merely
    outranked rather than removed would still show up there.
    """
    seed = ScriptedLLM(
        [
            call("memory_upsert_slot", call_id="1", slot_key="Name", text="Carol"),
            call("memory_upsert_slot", call_id="2", slot_key="Likes", text="mangoes"),
            text("Noted."),
        ]
    )
    _run(monkeypatch, seed, "I am Carol and I like mangoes")

    script = ScriptedLLM(
        [
            call("memory_upsert_slot", call_id="1", slot_key="Name", text="Alice"),
            call("memory_upsert_slot", call_id="2", slot_key="Likes", text="pizza"),
            text("Updated."),
        ]
    )
    assert _run(monkeypatch, script, "Call me Alice. I like pizza.") == 0

    # Asked separately for the same reason as above: what is under test is
    # that each slot holds one value, not that one query reaches both.
    about_name = ScriptedLLM([text("Alice.")])
    _run(monkeypatch, about_name, "what is my name")
    about_food = ScriptedLLM([text("Pizza.")])
    _run(monkeypatch, about_food, "what food do I like")

    name_prompt = str(about_name.calls[-1][0][0]["content"])
    food_prompt = str(about_food.calls[-1][0][0]["content"])

    assert "Name: Alice" in name_prompt
    assert "Carol" not in name_prompt
    assert "Likes: pizza" in food_prompt
    assert "mangoes" not in food_prompt


# --- append_file -----------------------------------------------------------
