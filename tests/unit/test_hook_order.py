"""The order hooks are composed in is load bearing, so it is pinned here.

Until this test the order was guaranteed by comments in ``session.py``, and a
hook appended in the wrong place would have broken something without any test
noticing:

* **Telemetry first.** It opens the run's span in ``on_run_start``; a hook
  running before it would record its work outside the run, as a second trace.
* **The conversation fold second.** It rewrites the context, so it must run
  before anything measures the context or injects into it (ADR-108).
* **The console last.** ``CompositeHook`` runs ``on_run_end`` in reverse, so
  the last hook is the first to see the end of a run — which is what lets the
  answer be printed before slow run-end work such as memory extraction
  (ADR-110).
"""

from pathlib import Path
from typing import Any

import pytest
from harness import session
from harness.config import load_settings
from harness.console import ConsoleTraceHook
from harness.memory import HashingEmbedder, MemoryHook, QdrantMemoryStore, SessionLog
from harness.memory.conversation import ConversationSummaryHook
from harness.memory.extract import MemoryExtractionHook
from harness.memory.notes import NotesHook
from harness.observability import TelemetryHook
from harness.skills import SkillsHook
from tests.fakes import ScriptedLLM

_CONFIG = """
llm_base_url: https://example.invalid
llm_model: test-model
workspace_path: workspace
memory_path: memory
skills_path: {skills}
memory_extraction_enabled: true
mcp_enabled: false
metrics_enabled: false
traces_enabled: false
"""


@pytest.fixture
def every_hook_session(
    tmp_path: Path, skills_root: Path, monkeypatch: pytest.MonkeyPatch
) -> Any:
    """A session with every optional hook switched on, and no network."""
    (tmp_path / "config.yaml").write_text(
        _CONFIG.format(skills=skills_root), encoding="utf-8"
    )
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("LLM_API_KEY", "test-key")
    monkeypatch.setattr(
        session, "OpenAICompatibleClient", lambda settings, **_: ScriptedLLM([])
    )

    def _store(settings: Any, llm: Any) -> QdrantMemoryStore:
        from qdrant_client import QdrantClient

        return QdrantMemoryStore(
            embedder=HashingEmbedder(),
            session_log=SessionLog(settings.memory_path),
            client=QdrantClient(":memory:"),
        )

    monkeypatch.setattr(session, "_build_memory_store", _store)
    settings = load_settings(tmp_path / "config.yaml", env_file=None)
    built = session.HarnessSession(settings, trace=True)
    yield built
    built.close()


def test_hooks_are_composed_in_the_order_the_run_depends_on(
    every_hook_session: Any,
) -> None:
    """The whole order, stated once, so a change to it is a visible decision."""
    order = [type(hook) for hook in every_hook_session.hooks]

    assert order == [
        TelemetryHook,
        ConversationSummaryHook,
        MemoryHook,
        MemoryExtractionHook,
        SkillsHook,
        NotesHook,
        ConsoleTraceHook,
    ]
