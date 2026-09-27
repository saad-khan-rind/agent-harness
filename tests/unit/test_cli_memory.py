"""The user-facing half of memory: ``/remember``, ``/memory``, the banner.

``/remember`` is the third write trigger and the only one the user drives
directly. It exists because the most reliable source of a durable fact is
the person stating it — routing "always answer in German" through the model
first costs a turn and risks the model paraphrasing it into something else.
So the tests below assert it writes with **no agent turn at all**. The one
model call it can make is the consolidation judge, and only when
consolidation is on and a similar fact is already stored (ADR-107).

Everything runs through ``cli.main``: the real settings loader, the real
session, the real hook wiring. Only two things are replaced — the model
(``ScriptedLLM``) and the Qdrant *connection*, swapped for the in-process
engine. Note what is not replaced: the store, the embedder, the records and
the retrieval are all the shipped ones, so what these tests exercise is the
production path with the network taken out (ADR-069).
"""

import re
from pathlib import Path
from typing import Any

import pytest
from harness import cli, session
from harness.memory import HashingEmbedder, QdrantMemoryStore, SessionLog
from tests.fakes import ScriptedLLM, text

# Memory beside the workspace, as the container ships it.
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
    """A runnable project directory, chdir'd into, with a key in the env."""
    (tmp_path / "config.yaml").write_text(_CONFIG, encoding="utf-8")
    (tmp_path / "workspace").mkdir()
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("LLM_API_KEY", "test-key")
    return tmp_path


@pytest.fixture(autouse=True)
def _in_process_qdrant(monkeypatch: pytest.MonkeyPatch) -> None:
    """Point the CLI's store builder at an in-process Qdrant.

    ``autouse`` because a test that forgot it would silently exercise the
    degraded path — every write refused, every read empty — and still pass
    some of its assertions, which is the worst kind of green.

    The local embedder is used rather than the configured one for the same
    reason the suite has no InnKube key: the endpoint is not reachable, and
    a probe against it would spend the connect timeout in every test.
    """
    from qdrant_client import QdrantClient

    def _build(settings: Any, llm: Any) -> QdrantMemoryStore:
        return QdrantMemoryStore(
            embedder=HashingEmbedder(),
            session_log=SessionLog(settings.memory_path),
            client=QdrantClient(":memory:"),
        )

    monkeypatch.setattr(session, "_build_memory_store", _build)


def _install(monkeypatch: pytest.MonkeyPatch, llm: Any) -> None:
    """Replace the real client so nothing reaches the network."""
    monkeypatch.setattr(session, "OpenAICompatibleClient", lambda settings, **_: llm)


def _feed(monkeypatch: pytest.MonkeyPatch, lines: list[str]) -> None:
    """Script the REPL's stdin, raising EOF when the lines run out."""
    pending = list(lines)

    def _input(_: str = "") -> str:
        if not pending:
            raise EOFError
        return pending.pop(0)

    monkeypatch.setattr("builtins.input", _input)


# --- /remember ------------------------------------------------------------


def test_remember_writes_without_calling_the_model(
    project: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """The whole justification for the command, asserted on cost.

    An empty ``ScriptedLLM`` would raise if the loop ran, so this pins that
    ``/remember`` is a pure harness write.
    """
    llm = ScriptedLLM([])
    _install(monkeypatch, llm)
    _feed(monkeypatch, ["/remember I prefer answers in German", "/exit"])

    cli.main([])

    out = capsys.readouterr().out
    assert llm.calls == []
    # Says what was kept: the value of the feature is the user trusting it.
    assert "Recorded in long-term memory" in out
    assert "I prefer answers in German" in out


def test_a_remembered_fact_reaches_the_next_turn(
    project: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Within one session too, not only across restarts.

    ``on_run_start`` re-queries per turn, so a fact recorded at the prompt
    is in the system message of the very next request — provided the turn's
    task is about it, which is the behaviour change the migration
    introduced and the reason the task here shares its wording.
    """
    llm = ScriptedLLM([text("Verstanden.")])
    _install(monkeypatch, llm)
    _feed(
        monkeypatch,
        ["/remember answer in German", "answer in German please", "/exit"],
    )

    cli.main([])

    system = str(llm.calls[-1][0][0]["content"])
    assert "answer in German" in system


def test_remember_reports_a_failed_write_instead_of_crashing(
    project: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A memory backend that goes away must not take down the REPL.

    This is the degradation policy at the one place the *user* meets it: a
    command they typed, that failed, reported in a sentence rather than a
    traceback.
    """
    from harness.memory.protocol import MemoryUnavailableError

    _install(monkeypatch, ScriptedLLM([]))
    _feed(monkeypatch, ["/remember something", "/exit"])

    def _explode(self: object, *args: Any, **kwargs: Any) -> None:
        raise MemoryUnavailableError("memory write failed: connection refused")

    monkeypatch.setattr(QdrantMemoryStore, "store", _explode)

    assert cli.main([]) == 0
    assert "Could not record that" in capsys.readouterr().out


# --- /memory --------------------------------------------------------------


def test_memory_command_shows_what_is_stored(
    project: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Verifiable persistence without involving the model.

    Reads the store rather than the current prompt on purpose: the question
    this answers is "what will the next session know", and showing the
    injected copy would hide a write that silently failed.
    """
    _install(monkeypatch, ScriptedLLM([]))
    _feed(monkeypatch, ["/remember a stored fact", "/memory", "/exit"])

    cli.main([])

    out = capsys.readouterr().out
    assert "a stored fact" in out
    assert "1 record(s)" in out


# --- the banner and the disabled path ------------------------------------


def test_the_banner_names_the_memory_backend(
    project: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """The user is handing an LLM a store that outlives the container.

    It used to be a path on that line. A path no longer says the useful
    thing — whether memory will work at all — so the line reports the
    backend and the embedder instead.
    """
    _install(monkeypatch, ScriptedLLM([]))
    _feed(monkeypatch, ["/exit"])

    cli.main([])

    out = capsys.readouterr().out
    assert "Memory:" in out
    assert "embeddings:" in out


def test_disabling_memory_removes_the_tools_and_the_commands(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """ "Off" must mean no memory tools, not tools that fail when called.

    Offering ``memory_store`` with memory disabled would burn a turn and
    teach the model that its tools are unreliable.
    """
    (tmp_path / "config.yaml").write_text(
        _CONFIG + "memory_enabled: false\n", encoding="utf-8"
    )
    (tmp_path / "workspace").mkdir()
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("LLM_API_KEY", "test-key")

    llm = ScriptedLLM([text("done")])
    _install(monkeypatch, llm)
    _feed(monkeypatch, ["/remember something", "do a thing", "/exit"])
    cli.main([])

    out = capsys.readouterr().out
    assert "Memory is disabled" in out
    assert "Memory:    disabled" in out
    _, tools = llm.calls[-1]
    names = {schema["function"]["name"] for schema in tools}
    assert not any(name.startswith("memory") for name in names)


def test_remember_corrects_an_older_fact_and_says_what_it_replaced(
    project: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """``/remember`` is the user's way to fix a fact, whoever recorded it.

    With consolidation on, a similar stored fact means one judge call — the
    only model call ``/remember`` ever makes — and the reply names what was
    replaced so the user can see the correction happened (ADR-107).
    """
    (project / "config.yaml").write_text(
        _CONFIG + "memory_consolidation_enabled: true\n", encoding="utf-8"
    )
    judge = ScriptedLLM([text('{"decision": "update", "target": 1}')])
    _install(monkeypatch, judge)
    _feed(
        monkeypatch,
        [
            "/remember I use VS Code as my editor",
            "/remember I use Neovim as my editor",
            "/memory",
            "/exit",
        ],
    )

    cli.main([])

    out = capsys.readouterr().out
    assert len(judge.calls) == 1
    assert "(replaces: I use VS Code as my editor)" in out
    memory_listing = out.split("backend:")[-1]
    assert "I use Neovim as my editor" in memory_listing
    assert "VS Code" not in memory_listing


def test_a_rebuilt_collection_is_reported_in_one_shot_mode(
    project: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A rebuild re-embeds every record; it must not happen in silence (ADR-110).

    One-shot mode prints no banner, so the note is printed after the task.
    """
    from qdrant_client import QdrantClient

    def _build(settings: Any, llm: Any) -> QdrantMemoryStore:
        store = QdrantMemoryStore(
            embedder=HashingEmbedder(),
            session_log=SessionLog(settings.memory_path),
            client=QdrantClient(":memory:"),
        )
        store._migration_note = "rebuilt memory at 256 dimensions (was 512)"
        return store

    monkeypatch.setattr(session, "_build_memory_store", _build)
    _install(monkeypatch, ScriptedLLM([text("done")]))

    assert cli.main(["hello"]) == 0

    assert "Warning: memory rebuilt memory at 256 dimensions" in capsys.readouterr().out


def test_each_answer_links_to_its_trace_when_traces_are_exported(
    project: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """The run id is the trace id, so the link is findable without searching.

    The docs said to search Jaeger "by the run id the banner prints", and the
    banner prints none (ADR-111). The exporter is replaced by one that keeps
    nothing, so no collector is needed.
    """

    class _Discard:
        def on_start(self, span: Any) -> None:
            """Nothing to do."""

        def export(self, span: Any) -> None:
            """Drop it."""

        def shutdown(self) -> None:
            """Nothing to release."""

    monkeypatch.setattr(
        session, "build_otlp_exporter", lambda *a, **k: (_Discard(), None)
    )
    monkeypatch.setenv("OTLP_ENABLED", "true")
    _install(monkeypatch, ScriptedLLM([text("done")]))

    assert cli.main(["hello"]) == 0

    out = capsys.readouterr().out
    # 32 hex characters: the run id, which is already a valid trace id.
    assert re.search(r"trace: http://localhost:16686/trace/[0-9a-f]{32}\b", out)


def test_no_trace_link_when_nothing_is_exported(
    project: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A link to a viewer that never received the run would be a dead end."""
    _install(monkeypatch, ScriptedLLM([text("done")]))

    assert cli.main(["hello"]) == 0

    assert "trace:" not in capsys.readouterr().out
