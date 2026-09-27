"""Long-term memory: vector records that outlive the process.

The loop never imports this package. Memory reaches a run through two seams
that already existed: the :class:`~harness.hooks.Hook` lifecycle (retrieval
at run start, the mechanical record at run end) and the
:class:`~harness.tools.registry.ToolRegistry` (the memory tools). See
``docs/architecture.md`` and ADR-065 to ADR-070 for the move from markdown
files to Qdrant, and ADR-033 to ADR-037 for what that move preserved.
"""

from harness.memory.embed import (
    Embedder,
    EmbeddingError,
    FastEmbedEmbedder,
    HashingEmbedder,
    SentenceTransformerEmbedder,
    build_embedder,
    load_fastembed,
    load_sentence_transformer,
)
from harness.memory.hook import MEMORY_SECTION_HEADER, MemoryHook
from harness.memory.protocol import MemoryStore, MemoryUnavailableError
from harness.memory.qdrant_store import COLLECTION_NAME, Hit, QdrantMemoryStore
from harness.memory.records import CATEGORIES, SLOT_KEYS, MemoryRecord
from harness.memory.sessions import SESSIONS_FILENAME, SessionLog
from harness.memory.tools import memory_tools

__all__ = [
    "CATEGORIES",
    "COLLECTION_NAME",
    "MEMORY_SECTION_HEADER",
    "SESSIONS_FILENAME",
    "SLOT_KEYS",
    "Embedder",
    "EmbeddingError",
    "FastEmbedEmbedder",
    "HashingEmbedder",
    "SentenceTransformerEmbedder",
    "Hit",
    "MemoryHook",
    "MemoryRecord",
    "MemoryStore",
    "MemoryUnavailableError",
    "QdrantMemoryStore",
    "SessionLog",
    "build_embedder",
    "load_fastembed",
    "load_sentence_transformer",
    "memory_tools",
]
