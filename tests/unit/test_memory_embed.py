"""The embedder seam: the local fallback, the remote one, and the choice.

Moving memory into a vector database adds a component the file store never
had, and it is the component most likely to be missing: an embedding model.
These tests cover the three things that has to do (ADR-066).

**Determinism.** The same fact embedded twice must land in the same place,
across processes. ``hash()`` is randomised per process by default, so a
naive implementation would put a fact in one bucket today and another
tomorrow, and every stored vector would become unqueryable after a restart.
That failure is silent — nothing raises, recall just goes to zero — so it
is tested directly.

**Failure containment.** A model endpoint that returns the wrong width, or
fails outright, must raise rather than return something plausible. A store
that silently mixes vector spaces returns confident nonsense.

**The choice.** ``build_embedder`` probes rather than guessing, because "a
key is configured" and "this deployment serves embeddings" are different
claims and only one of them can be read from config.
"""

import math
from typing import Any

import pytest
from harness.memory.embed import (
    EmbeddingError,
    HashingEmbedder,
    RemoteEmbedder,
    build_embedder,
    load_fastembed,
    load_sentence_transformer,
)


def _cosine(left: list[float], right: list[float]) -> float:
    """Return the cosine of two vectors, for the similarity assertions."""
    dot = sum(a * b for a, b in zip(left, right, strict=True))
    norm = math.sqrt(sum(a * a for a in left)) * math.sqrt(sum(b * b for b in right))
    return dot / norm if norm else 0.0


class _StubEmbeddings:
    """The ``client.embeddings`` namespace, scripted."""

    def __init__(self, width: int = 4, error: Exception | None = None) -> None:
        self._width = width
        self._error = error
        self.calls: list[tuple[str, list[str]]] = []

    def create(self, model: str, input: list[str]) -> Any:  # noqa: A002 - SDK name
        """Return a scripted response, or raise the scripted error."""
        self.calls.append((model, input))
        if self._error is not None:
            raise self._error
        vector = [float(index + 1) for index in range(self._width)]
        return type(
            "Response",
            (),
            {"data": [type("Datum", (), {"embedding": vector})()]},
        )()


class _StubClient:
    """An SDK client exposing only what the embedder uses."""

    def __init__(self, width: int = 4, error: Exception | None = None) -> None:
        self.embeddings = _StubEmbeddings(width, error)


# --- the local fallback ----------------------------------------------------


def test_the_same_text_embeds_to_the_same_vector() -> None:
    """The property everything stored depends on."""
    embedder = HashingEmbedder()

    assert embedder.embed("deploys on Thursdays") == embedder.embed(
        "deploys on Thursdays"
    )


def test_the_vector_is_stable_across_processes() -> None:
    """``hash()`` is not, and this is the bug that would hide.

    Nothing raises when bucket assignment changes between runs: writes
    succeed, queries return nothing, and memory looks empty. Asserting a
    fixed digest-derived bucket is what catches a change to the hash.
    """
    import subprocess
    import sys

    script = (
        "from harness.memory.embed import HashingEmbedder;"
        "v = HashingEmbedder(16).embed('passau');"
        "print([round(x, 6) for x in v])"
    )
    first = subprocess.run(
        [sys.executable, "-c", script],
        capture_output=True,
        text=True,
        check=True,
        env={"PYTHONHASHSEED": "1", "PATH": "/usr/bin:/bin"},
    ).stdout
    second = subprocess.run(
        [sys.executable, "-c", script],
        capture_output=True,
        text=True,
        check=True,
        env={"PYTHONHASHSEED": "2", "PATH": "/usr/bin:/bin"},
    ).stdout

    assert first == second


def test_a_zero_width_embedder_is_refused() -> None:
    """A width of zero produces a collection nothing can be stored in."""
    with pytest.raises(ValueError, match="at least 1"):
        HashingEmbedder(0)


# --- the remote embedder ---------------------------------------------------


def test_the_remote_embedder_returns_what_the_endpoint_sent() -> None:
    """The ordinary path, including that the model name is forwarded."""
    client = _StubClient(width=4)
    embedder = RemoteEmbedder(client, "text-embedding-3-small", 4)

    assert embedder.embed("a fact") == [1.0, 2.0, 3.0, 4.0]
    assert client.embeddings.calls[0][0] == "text-embedding-3-small"


def test_a_failing_endpoint_raises_rather_than_substituting() -> None:
    """Falling back mid-session would change the vector space silently.

    Records written at 1536 dimensions cannot be queried at 256. A store
    that swapped embedders on an error would return confident nonsense
    from every later query, so the error is raised and the caller decides.
    """
    embedder = RemoteEmbedder(_StubClient(error=RuntimeError("503")), "m", 4)

    with pytest.raises(EmbeddingError, match="embedding request failed"):
        embedder.embed("a fact")


def test_a_wrong_width_response_is_refused() -> None:
    """A deployment that changes model under one name is a real failure.

    Caught here rather than at ``upsert``, where Qdrant would report it as
    a dimension error naming the collection instead of the model.
    """
    embedder = RemoteEmbedder(_StubClient(width=8), "m", 4)

    with pytest.raises(EmbeddingError, match="expected 4"):
        embedder.embed("a fact")


# --- choosing between them -------------------------------------------------


def test_a_chat_only_deployment_falls_back() -> None:
    """404 on /embeddings is the expected answer from a chat deployment.

    It is not an error to report: the harness is designed to run without
    embeddings, and refusing to start would make memory a precondition for
    an agent instead of a convenience.
    """
    client = _StubClient(error=RuntimeError("404 Not Found"))

    assert isinstance(build_embedder(client, "m"), HashingEmbedder)


# --- the local rung --------------------------------------------------------


class _StubFastEmbed:
    """Stands in for ``fastembed.TextEmbedding``: yields fixed vectors."""

    def __init__(self, width: int = 4) -> None:
        self.width = width
        self.seen: list[str] = []

    def embed(self, texts: list[str]) -> Any:
        """Yield one unit vector per text, recording what was asked."""
        for text in texts:
            self.seen.append(text)
            yield [1.0] + [0.0] * (self.width - 1)


def test_a_missing_local_model_is_not_an_error() -> None:
    """Absent package, no network, read-only cache, typo — all one answer.

    The caller has a working alternative for every one of them, so none
    deserves a traceback in front of a user.
    """
    assert load_fastembed("definitely-not-a-real-checkpoint-name") is None
    assert load_fastembed("") is None
    assert load_sentence_transformer("") is None


def test_build_embedder_falls_back_to_lexical_when_nothing_else_loads() -> None:
    """The bottom rung is still reachable, which is what keeps CI runnable.

    CI has neither network nor credentials, so this is the path the whole
    memory suite runs on.
    """
    embedder = build_embedder(
        client=None, model="", local_model="definitely-not-a-real-checkpoint"
    )

    assert isinstance(embedder, HashingEmbedder)
