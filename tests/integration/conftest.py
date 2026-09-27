"""Shared wiring for the integration suite.

Every file here drives ``cli.main()``, which since ADR-065 builds a Qdrant
client. ``.github/workflows/ci.yml`` provides no Docker and no network, so without
something here each of those runs would dial a port nothing is listening on
and degrade — every memory assertion would fail, and the ones that do not
touch memory would still pay a connect timeout apiece.

So the connection, and only the connection, is replaced: an in-process
Qdrant over a directory under ``tmp_path``. The store, the embedder, the
records, the filters, the scoring and the whole CLI are the shipped ones
(ADR-069).

``autouse`` on purpose. A test that forgot to ask for this would silently
run against the degraded path — writes refused, reads empty — and would
still pass any assertion that did not involve memory. That is the worst
kind of green, and opting *out* is the rarer and more deliberate choice:
the handful of tests that want a genuinely unreachable backend patch
``_build_memory_store`` again themselves, which wins because it is applied
later.
"""

from pathlib import Path
from typing import Any

import pytest
from harness import session
from harness.memory import HashingEmbedder, QdrantMemoryStore, SessionLog


@pytest.fixture(autouse=True)
def _in_process_qdrant(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Point ``cli._build_memory_store`` at an on-disk in-process Qdrant.

    Storage is per test and shared across the ``cli.main()`` calls within
    one test; the client is built fresh per call. That mirrors production,
    where each session opens its own connection to one volume — and it
    matters here because ``HarnessSession.close()`` closes the client, so a
    single shared instance would be dead by the second run.

    The local embedder is used rather than the configured one for the same
    reason the suite has no InnKube key: the endpoint is unreachable, and
    probing it would spend the connect timeout in every test.
    """
    storage = tmp_path / "qdrant"

    def _build(settings: Any, llm: Any) -> QdrantMemoryStore:
        from qdrant_client import QdrantClient

        return QdrantMemoryStore(
            embedder=HashingEmbedder(),
            session_log=SessionLog(settings.memory_path),
            client=QdrantClient(path=str(storage)),
        )

    monkeypatch.setattr(session, "_build_memory_store", _build)


@pytest.fixture
def seed_memory(tmp_path: Path) -> Any:
    """Return a callable that pre-loads records before ``cli.main()`` runs.

    Some tests need memory to *already* contain something — most of all the
    prompt-injection ones, where the whole scenario is a hostile fact that
    was recorded by an earlier session. Before ADR-065 that was a file to
    write; now it is a record to store, and there is no way to seed one
    except through the store.

    The client is opened and closed inside the call, because the on-disk
    local engine holds an exclusive lock: leaving it open would make the
    ``cli.main()`` that follows unable to connect, and the test would then
    pass or fail for the wrong reason.
    """

    def _seed(*records: tuple[str, str]) -> None:
        """Store ``(text, source)`` pairs as core facts.

        The text is stored **exactly as given**, which is the point for the
        injection tests: the store's own cleaning still applies, so a fact
        that arrives with a forged tag is stripped just as it would be in
        production, and the test sees the real behaviour rather than a
        seeded shortcut around it.
        """
        from qdrant_client import QdrantClient

        store = QdrantMemoryStore(
            embedder=HashingEmbedder(),
            session_log=SessionLog(tmp_path),
            client=QdrantClient(path=str(tmp_path / "qdrant")),
        )
        try:
            for content, source in records:
                store.store(content, "core_fact", source)  # type: ignore[arg-type]
        finally:
            store.close()

    return _seed
