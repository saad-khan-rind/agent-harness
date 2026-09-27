"""Turning memory text into vectors.

Qdrant stores vectors and compares them. It does not make them, so moving
memory into a vector database adds a component the file store never needed:
something that maps a sentence to a point in space. That component is the
one part of this migration with a hard external dependency, and it is the
one most likely to be unavailable — which is why it is a seam rather than a
function call (ADR-065).

Three implementations ship, and :func:`build_embedder` tries them in this
order — best retrieval first, each one degrading to the next:

* :class:`RemoteEmbedder` calls the OpenAI-compatible ``/embeddings``
  endpoint the harness already talks to for chat. It reuses the configured
  base URL and key, so a working harness needs no second credential and no
  second network egress rule.
* :class:`FastEmbedEmbedder` and :class:`SentenceTransformerEmbedder` both
  run a small model in-process: no key, no endpoint, and no network after
  the first download. They are interchangeable — same checkpoints, same
  vectors — and differ only in what they drag in. fastembed reaches the
  model through onnxruntime in about 24 MB of wheels; sentence-transformers
  reaches it through torch in about 1.4 GB, which is ten times the base
  image this project ships on. fastembed is tried first for that reason,
  and is the Qdrant project's own library besides.
* :class:`HashingEmbedder` computes a vector locally from character n-grams
  and word tokens. No network, no model, no key, fully deterministic.

The middle rung was added after the bottom one was found doing a job it
cannot do. A hashing embedder measures *lexical* overlap, not meaning:
"car" and "automobile" are near-orthogonal to it, and so are "what do I
like" and "Likes: swimming" — which scored 0.115 against a floor of 0.20,
so a stored fact was never injected and the agent reported knowing nothing
about a user it had a record for. With only two rungs, any deployment that
served chat but not embeddings landed there, and nothing about the run
looked wrong.

So the lexical embedder stays, and its purpose is narrowed to what it is
actually good for: keeping the subsystem's *shape* identical when no model
of any kind is available. CI has neither network nor credentials
(`.github/workflows/ci.yml`), and without it every memory test would need a live
model and the suite would stop being runnable, which the brief (Section
8.2) does not allow. It is a test harness and a last resort, not a
retrieval strategy, and :attr:`Embedder.min_similarity` is where each
implementation states what its own scores are worth.

The first rung is chosen by a probe rather than a guess, because "the key
is set" and "the endpoint serves embeddings" are different claims: InnKube
is a chat deployment, and whether it exposes ``/embeddings`` at all is a
property of the deployment, not of the configuration.
"""

import hashlib
import math
import re
from typing import Any, Protocol

# Dimension of the local fallback. Large enough that unrelated n-grams
# rarely collide into the same bucket, small enough that a vector is a few
# kilobytes rather than tens. Not tunable from config: changing it changes
# what is stored, and a collection built at one dimension cannot be queried
# at another, so it is a code-level constant that appears in the collection
# name instead.
HASHING_DIMENSIONS = 256

# Text longer than this is clipped before hashing or before being sent to a
# model. A memory record is meant to be one fact or one short note; anything
# past this is either a runaway write or a pasted file, and neither embeds
# into a meaningful point.
MAX_EMBED_CHARS = 8_000

# Word-ish tokens for the lexical fallback. Unicode-aware so a German or
# Urdu fact tokenises rather than collapsing to one bucket.
_TOKEN = re.compile(r"\w+", re.UNICODE)

# Length of the character n-grams the fallback mixes in alongside whole
# tokens. Three is the usual choice: it survives a plural or a typo, where
# whole-token matching does not.
_NGRAM = 3


class Embedder(Protocol):
    """Maps text to a fixed-length vector.

    Implementations must be deterministic for a given text — the same fact
    embedded twice must land in the same place, or an upsert would create a
    near-duplicate instead of replacing a point.
    """

    @property
    def dimensions(self) -> int:
        """Return the length of every vector this embedder produces."""

    @property
    def name(self) -> str:
        """Return a short label for logs and the ``/memory`` command."""

    @property
    def min_similarity(self) -> float:
        """Return the cosine floor below which a hit is noise.

        On the embedder rather than in the store, because it is a property
        of the vector space and not of the retrieval policy. Every model
        puts unrelated text at a different baseline: the lexical fallback
        scores two unrelated sentences near 0.0 and needs a low floor, while
        a sentence-transformers model scores almost any two English
        sentences above 0.3 and would, at that same low floor, return the
        whole collection for every query.

        Keeping one constant for all of them meant the number was
        necessarily wrong for all but one — and wrong in the dangerous
        direction when the embedder improved, since better embeddings with a
        floor tuned for worse ones silently stop filtering anything.
        """

    def embed(self, text: str) -> list[float]:
        """Return the vector for one piece of text.

        Args:
            text: The text to embed. May be empty; implementations must
                return a valid vector rather than raising, because an empty
                query is a user typing nothing, not a bug.

        Returns:
            A list of floats of length :attr:`dimensions`.
        """


class HashingEmbedder:
    """A deterministic, offline, lexical embedder.

    Hashes whole word tokens and character trigrams into a fixed number of
    buckets, weights them by sublinear term frequency, and L2-normalises the
    result so cosine similarity is a dot product.

    This is the classic hashing trick, and it is chosen over a bag-of-words
    index for one reason that matters to the rest of the subsystem: it
    produces a **dense fixed-length vector**, so it goes into the same
    Qdrant collection, through the same query path, and under the same
    scoring code as a real embedding model. Swapping the embedder changes
    retrieval quality and nothing else. That is what makes the fallback safe
    to ship: the degraded mode exercises the production code path rather
    than a parallel one that could rot unnoticed.
    """

    def __init__(self, dimensions: int = HASHING_DIMENSIONS) -> None:
        """Args:
        dimensions: Vector length. Must be positive.

        Raises:
            ValueError: If ``dimensions`` is not positive.
        """
        if dimensions < 1:
            raise ValueError("dimensions must be at least 1")
        self._dimensions = dimensions

    @property
    def dimensions(self) -> int:
        """Return the configured vector length."""
        return self._dimensions

    @property
    def name(self) -> str:
        """Return the label shown wherever the active embedder is reported."""
        return f"hashing-{self._dimensions}d (offline, lexical)"

    @property
    def min_similarity(self) -> float:
        """Return the floor measured for lexical matching.

        The honest finding, unchanged from when this was a module constant:
        no value cleanly separates signal from noise here. Over the queries
        this repository's tests use, genuine matches land between 0.16 and
        0.56 and false ones between 0.00 and 0.22, and those ranges overlap.
        "how do we deploy" against "the harness deploys with docker compose"
        scores 0.235, barely above "the weather in Lisbon tomorrow" against
        the same note at 0.215 — because lexically they *are* about equally
        similar. Only a real embedding model tells them apart.

        So the floor stays low and the asymmetry is deliberate: a false
        positive costs one line in a prompt that already frames its contents
        as background facts, while a false negative costs the fact entirely
        and the model cannot know it was there (ADR-067).
        """
        return 0.20

    def embed(self, text: str) -> list[float]:
        """Return the L2-normalised hashed vector for ``text``.

        An empty or whitespace-only text yields the zero vector. Qdrant
        accepts it and it scores 0 against everything, which is the correct
        answer for "no query": nothing is more relevant than anything else.
        """
        clipped = text[:MAX_EMBED_CHARS].lower()
        counts: dict[int, float] = {}
        for token in _TOKEN.findall(clipped):
            _add(counts, token, self._dimensions)
            # Trigrams inside the token, not across the whole string, so
            # word boundaries still carry information.
            for start in range(len(token) - _NGRAM + 1):
                _add(counts, token[start : start + _NGRAM], self._dimensions)

        vector = [0.0] * self._dimensions
        for bucket, count in counts.items():
            # Sublinear scaling: a fact that repeats a word five times is
            # not five times as much about that word.
            vector[bucket] = 1.0 + math.log(count)

        norm = math.sqrt(sum(value * value for value in vector))
        if norm == 0.0:
            return vector
        return [value / norm for value in vector]


class RemoteEmbedder:
    """Embeddings from the OpenAI-compatible endpoint already configured.

    Holds no credentials of its own: it is handed a built SDK client, which
    keeps the key in exactly one place (``Settings``) and lets the tests
    drive this class with a stub that has no network at all.
    """

    def __init__(self, client: Any, model: str, dimensions: int) -> None:
        """Args:
        client: An object exposing ``embeddings.create(model=, input=)``,
            in practice the ``openai.OpenAI`` instance.
        model: Embedding model name to request.
        dimensions: Vector length this model returns. Discovered by
            :func:`build_embedder` rather than assumed, because the same
            model name can be served at different sizes.
        """
        self._client = client
        self._model = model
        self._dimensions = dimensions

    @property
    def dimensions(self) -> int:
        """Return the vector length discovered at startup."""
        return self._dimensions

    @property
    def name(self) -> str:
        """Return the label shown wherever the active embedder is reported."""
        return f"{self._model} ({self._dimensions}d, remote)"

    @property
    def min_similarity(self) -> float:
        """Return the floor for a hosted embedding model.

        Lower than the local model's because the OpenAI-family spaces are
        the better-spread of the two: unrelated English sentences sit near
        0.0-0.2 rather than being pushed up by a model trained to make
        everything retrievable against everything.

        Stated as an estimate rather than a measurement, because this class
        cannot be exercised without a live endpoint and this repository's
        CI has none. It is the one number here that has not been measured on
        the deployment it will run against, so it is the first thing to
        re-tune if hosted retrieval looks too permissive.
        """
        return 0.30

    def embed(self, text: str) -> list[float]:
        """Return the model's vector for ``text``.

        Raises:
            EmbeddingError: If the endpoint fails or returns a vector of
                the wrong length. The caller decides what to do about it;
                this class does not silently substitute a fallback, because
                a memory subsystem that quietly changes its vector space
                mid-session would return nonsense from every later query.
        """
        clipped = text[:MAX_EMBED_CHARS]
        try:
            response = self._client.embeddings.create(
                model=self._model,
                input=[clipped if clipped.strip() else " "],
            )
            vector = [float(value) for value in response.data[0].embedding]
        except Exception as exc:  # noqa: BLE001 - provider errors are open-ended
            raise EmbeddingError(f"embedding request failed: {exc}") from exc
        if len(vector) != self._dimensions:
            raise EmbeddingError(
                f"embedding model {self._model!r} returned "
                f"{len(vector)} dimensions, expected {self._dimensions}"
            )
        return vector


class SentenceTransformerEmbedder:
    """Embeddings from a sentence-transformers model running in-process.

    The middle rung of the ladder, and the one that makes memory work
    without a credential. It needs no key, no endpoint and no network after
    the first download, which is what a keyless demo on a laptop actually
    is — the case :class:`HashingEmbedder` was written for and serves badly.

    The cost is a heavyweight import and a model that has to be fetched
    once. Both are paid at startup by :func:`build_embedder`, never lazily
    inside a query, so a run that reaches the first ``memory_search`` has
    already proved the model loads.
    """

    def __init__(self, model: Any, name: str, dimensions: int) -> None:
        """Args:
        model: A loaded ``SentenceTransformer``, or any object with a
            compatible ``encode``. Injected rather than constructed here
            so the tests can drive this class without torch.
        name: Model identifier, for the banner.
        dimensions: Vector width, read from the loaded model rather than
            assumed — the same checkpoint name can be served at different
            widths, and a wrong guess here is a collection that cannot be
            queried.
        """
        self._model = model
        self._name = name
        self._dimensions = dimensions

    @property
    def dimensions(self) -> int:
        """Return the model's vector width."""
        return self._dimensions

    @property
    def name(self) -> str:
        """Return the label shown wherever the active embedder is reported."""
        return f"{self._name} ({self._dimensions}d, local)"

    @property
    def min_similarity(self) -> float:
        """Return the floor measured for this model family.

        Unlike the lexical floor above, this number is **not** measured in
        this repository: CI has no network and cannot download a model, so
        there is no honest way to derive it here. It is a starting point
        chosen to be conservative for the default checkpoint, and
        ``scripts/tune_memory_floor.py`` measures the real one on the
        machine that will run the demo, against that machine's own facts.
        ``memory_min_similarity`` in config.yaml overrides it once measured.

        The caveat is worth stating plainly because the alternative is the
        trap this whole release is about: a number that looks authoritative,
        was never checked against the thing it governs, and fails silently.

        Higher than either other embedder, and that is the point of having
        the number here instead of in the store: reusing the lexical floor
        of 0.20 with this model would admit essentially every pair, turning
        a better embedder into a *worse* retriever. Improving the embedder
        without moving the floor is the trap this property exists to close.
        """
        return 0.35

    def embed(self, text: str) -> list[float]:
        """Return the model's vector for ``text``.

        Normalised on the way out, because the collection is cosine and the
        rest of this subsystem assumes a dot product in ``[0, 1]``.
        :func:`~harness.memory.qdrant_store.decayed_score` combines the
        similarity with a recency term on that assumption, and unnormalised
        vectors would put the combined score off the end of its own scale.

        Raises:
            EmbeddingError: If encoding fails or returns the wrong width.
                Not swallowed into the lexical fallback: changing vector
                space mid-session would make every later query nonsense,
                which is the same argument :class:`RemoteEmbedder` makes.
        """
        clipped = text[:MAX_EMBED_CHARS]
        try:
            raw = self._model.encode(
                clipped if clipped.strip() else " ",
                normalize_embeddings=True,
            )
            vector = [float(value) for value in raw]
        except Exception as exc:  # noqa: BLE001 - model errors are open-ended
            raise EmbeddingError(f"local embedding failed: {exc}") from exc
        if len(vector) != self._dimensions:
            raise EmbeddingError(
                f"local model {self._name!r} returned {len(vector)} "
                f"dimensions, expected {self._dimensions}"
            )
        return vector


class EmbeddingError(RuntimeError):
    """One embedding call failed."""


class FastEmbedEmbedder:
    """Embeddings from a fastembed ONNX model running in-process.

    The same job as :class:`SentenceTransformerEmbedder` at a fraction of
    the weight, which is what makes it the right default for an image
    someone else has to pull. sentence-transformers reaches the model
    through torch: 1.4 GB of wheels once scipy, scikit-learn and
    transformers come with it, on a base image of about 150 MB. fastembed
    reaches the same checkpoint through onnxruntime in roughly 24 MB.

    It is also the Qdrant project's own library, so it is already the
    closest thing this repository has to a supported pairing with the
    ``qdrant-client`` pin next to it.
    """

    def __init__(self, model: Any, name: str, dimensions: int) -> None:
        """Args:
        model: A loaded ``fastembed.TextEmbedding``, or anything with a
            compatible ``embed``. Injected rather than constructed here
            so the tests can drive this class with no model and no
            network.
        name: Model identifier, for the banner.
        dimensions: Vector width, measured from a real embedding at load
            time rather than assumed from the name.
        """
        self._model = model
        self._name = name
        self._dimensions = dimensions

    @property
    def dimensions(self) -> int:
        """Return the model's vector width."""
        return self._dimensions

    @property
    def name(self) -> str:
        """Return the label shown wherever the active embedder is reported."""
        return f"{self._name} ({self._dimensions}d, local onnx)"

    @property
    def min_similarity(self) -> float:
        """Return the floor for this model family.

        The same caveat as :class:`SentenceTransformerEmbedder`: not
        measured in this repository, because CI cannot download a model.
        ``scripts/tune_memory_floor.py`` measures it where one exists.
        """
        return 0.35

    def embed(self, text: str) -> list[float]:
        """Return the model's vector for ``text``.

        fastembed normalises its output, which is what the cosine
        collection and :func:`~harness.memory.qdrant_store.decayed_score`
        both assume.

        Raises:
            EmbeddingError: If encoding fails or returns the wrong width.
        """
        clipped = text[:MAX_EMBED_CHARS]
        try:
            raw = next(iter(self._model.embed([clipped if clipped.strip() else " "])))
            vector = [float(value) for value in raw]
        except Exception as exc:  # noqa: BLE001 - model errors are open-ended
            raise EmbeddingError(f"local embedding failed: {exc}") from exc
        if len(vector) != self._dimensions:
            raise EmbeddingError(
                f"local model {self._name!r} returned {len(vector)} "
                f"dimensions, expected {self._dimensions}"
            )
        return vector


def load_fastembed(model_name: str, cache_dir: str = "") -> Embedder | None:
    """Load a fastembed model, or return ``None``.

    Every failure means the same thing to the caller — "not available
    here" — and they are all ordinary: the package is absent, the container
    has no network on first run, the cache is read-only, the name is not one
    fastembed serves. None deserves a traceback, because there is a working
    alternative for all of them.

    The width is taken from an actual embedding rather than a lookup table.
    A wrong width here is not a bad label; it is a collection sized wrongly
    and every later query rejected, so it is worth one extra inference at
    startup to know rather than assume.

    Args:
        model_name: Checkpoint to load, e.g.
            ``sentence-transformers/all-MiniLM-L6-v2``. Empty means "do not
            try".
        cache_dir: Where fastembed keeps downloaded models. Empty uses its
            default. Set this in a container to a path baked at build time,
            so a run needs no network at all.

    Returns:
        A ready :class:`FastEmbedEmbedder`, or ``None``.
    """
    if not model_name.strip():
        return None
    try:
        from fastembed import TextEmbedding

        # Two call shapes rather than a ``**kwargs`` dict, because
        # ``TextEmbedding`` types its keyword arguments individually and a
        # splatted ``dict[str, str]`` does not type-check against them.
        model = (
            TextEmbedding(model_name=model_name, cache_dir=cache_dir)
            if cache_dir.strip()
            else TextEmbedding(model_name=model_name)
        )
        width = len(next(iter(model.embed(["ping"]))))
    except Exception:  # noqa: BLE001 - absent, offline, or unreadable
        return None
    if width < 1:
        return None
    return FastEmbedEmbedder(model, model_name, width)


def load_sentence_transformer(model_name: str) -> Embedder | None:
    """Load a sentence-transformers model, or return ``None``.

    Every failure is one answer — "not available here" — and they are all
    ordinary: the package is not installed, the machine is offline on first
    run and cannot fetch the weights, the cache directory is read-only, the
    name is a typo. None of them is worth a traceback in front of a user,
    because the caller has a working alternative for all of them.

    The import is inside the function on purpose. ``sentence_transformers``
    pulls in torch, which is seconds of import time and hundreds of
    megabytes of RAM; a harness run with ``memory_enabled: false``, or one
    that reaches a serving endpoint, must not pay for it.

    Args:
        model_name: Checkpoint to load, e.g. ``BAAI/bge-small-en-v1.5``.
            An empty name means "do not try", and returns ``None``.

    Returns:
        A ready :class:`SentenceTransformerEmbedder`, or ``None``.
    """
    if not model_name.strip():
        return None
    try:
        from sentence_transformers import SentenceTransformer

        model = SentenceTransformer(model_name)
        # Documented as ``int | None``: a model whose width cannot be
        # determined is one this seam cannot size a collection for.
        reported = model.get_sentence_embedding_dimension()
        width = int(reported) if reported is not None else 0
    except Exception:  # noqa: BLE001 - absent, offline, or unreadable
        return None
    if width < 1:
        return None
    return SentenceTransformerEmbedder(model, model_name, width)


def build_embedder(
    client: Any | None,
    model: str,
    fallback_dimensions: int = HASHING_DIMENSIONS,
    local_model: str = "",
    local_cache_dir: str = "",
) -> Embedder:
    """Return the best embedder available, probing the endpoint once.

    The probe is a single call with a one-character input. It answers the
    only question that matters and cannot be answered from configuration:
    does this deployment serve embeddings, and at what width. A chat-only
    deployment answers 404, an unreachable one answers a connection error,
    and both mean the same thing here — use the local fallback.

    Failing over rather than failing hard is the deliberate choice. Memory
    is a convenience layered on the agent (ADR-033), so a missing embedding
    model degrades retrieval quality; it must not stop a run from starting,
    exactly as an unreadable memory file never did.

    There are three rungs, tried in order, and the order is by retrieval
    quality: the serving endpoint, a local model (fastembed for preference,
    sentence-transformers if that is what is installed), and the lexical
    fallback. The middle rung is the one that was missing, and
    its absence is what made the failure mode bite — with only two rungs, a
    chat-only deployment dropped the whole subsystem to word overlap, and
    since ADR-066 called that "degraded retrieval" rather than a defect,
    nothing about the run looked wrong.

    Args:
        client: Built SDK client, or ``None`` to skip the probe entirely
            (no key configured, or embeddings switched off).
        model: Embedding model name to probe.
        fallback_dimensions: Width of the lexical embedder.
        local_model: Local checkpoint to try when the endpoint serves no
            embeddings, loaded through fastembed if it is installed and
            sentence-transformers otherwise. Empty skips that rung.
        local_cache_dir: Where fastembed keeps its models. Set in a
            container to a path populated at build time, so the run
            itself needs no network.

    Returns:
        The best available embedder. Never ``None`` and never raises: a
        missing embedding model degrades retrieval, it does not stop a run
        from starting.
    """
    remote = _probe_remote(client, model)
    if remote is not None:
        return remote
    # fastembed before sentence-transformers: same checkpoint, same
    # vectors, ~24 MB of wheels against ~1.4 GB. Whichever is installed
    # wins, so a deployment that already has torch for other reasons keeps
    # working without adding a dependency it does not want.
    local = load_fastembed(local_model, local_cache_dir)
    if local is None:
        local = load_sentence_transformer(local_model)
    if local is not None:
        return local
    return HashingEmbedder(fallback_dimensions)


def _probe_remote(client: Any | None, model: str) -> Embedder | None:
    """Return an :class:`RemoteEmbedder` when the endpoint serves embeddings.

    The probe is a single call with a one-character input. It answers the
    only question that matters and cannot be answered from configuration:
    does this deployment serve embeddings, and at what width. A chat-only
    deployment answers 404, an unreachable one answers a connection error,
    and both mean the same thing here — try the next rung.

    Args:
        client: Built SDK client, or ``None`` to skip the probe.
        model: Embedding model name to probe.

    Returns:
        The embedder, or ``None`` when this deployment has no embeddings.
    """
    if client is None or not model.strip():
        return None
    try:
        probe = client.embeddings.create(model=model, input=["ping"])
        width = len(probe.data[0].embedding)
    except Exception:  # noqa: BLE001 - any failure means "no embeddings here"
        return None
    if width < 1:
        return None
    return RemoteEmbedder(client, model, width)


def _add(counts: dict[int, float], feature: str, dimensions: int) -> None:
    """Add ``feature`` to the bucket it hashes into, weighted by its length.

    blake2b with an eight-byte digest, read big-endian and taken modulo the
    dimension. A cryptographic hash is not needed for correctness, but it is
    *stable across processes and Python versions*, which ``hash()`` is not —
    PYTHONHASHSEED randomisation would put the same fact in a different
    bucket on every restart and make stored vectors unqueryable.

    The ``log(1 + len)`` weight is what stops function words deciding the
    ranking. Unweighted, "the weather in Lisbon tomorrow" scored 0.298
    against a note about deploying — the same as the *weakest genuine
    match* — because both contain "the", "in" and their trigrams, and short
    common tokens are numerous enough to dominate a short text. Weighting
    by length is a crude stand-in for inverse document frequency and needs
    no corpus, which matters because there is no corpus here and because a
    stopword list would have to be per-language: this harness stores facts
    in German and Urdu as readily as English.

    Measured on the same texts, the weight moves the weakest genuine match
    to 0.354 and the strongest false one to 0.254, which is the gap
    :data:`~harness.memory.qdrant_store.MIN_SIMILARITY` sits in.
    """
    digest = hashlib.blake2b(feature.encode("utf-8"), digest_size=8).digest()
    bucket = int.from_bytes(digest, "big") % dimensions
    counts[bucket] = counts.get(bucket, 0.0) + math.log(1 + len(feature))
