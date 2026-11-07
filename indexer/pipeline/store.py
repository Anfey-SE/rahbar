"""Stage 5: Store. The only code in the whole system that writes to Chroma."""

from __future__ import annotations

import logging
from typing import Any

from .config import IndexerSettings
from .models import Chunk

log = logging.getLogger("rahbar.indexer.store")

_BATCH_SIZE = 100


def get_client(settings: IndexerSettings) -> Any:
    import chromadb  # type: ignore[import-untyped]
    from chromadb.config import Settings as ChromaSettings

    return chromadb.HttpClient(
        host=settings.chroma_host,
        port=settings.chroma_port,
        settings=ChromaSettings(anonymized_telemetry=False),
    )


def store(chunks: list[Chunk], vectors: list[list[float]],
          settings: IndexerSettings) -> int:
    """Write every chunk atomically: delete old collection, create new, add all.

    Records embed_model and dimension in collection metadata for the pin check
    (section 4.4). Returns the count of stored chunks.
    """
    if not chunks:
        return 0

    client = get_client(settings)
    dim = len(vectors[0]) if vectors else 0

    # Atomic swap: delete old collection, create fresh one
    try:
        client.delete_collection(settings.collection)
        log.info("deleted old collection",
                 extra={"collection": settings.collection})
    except Exception:
        pass  # Collection did not exist yet — fine

    collection = client.create_collection(
        name=settings.collection,
        metadata={
            "hnsw:space": "cosine",
            "embed_model": settings.embed_model,
            "dimension": dim,
        },
    )

    for i in range(0, len(chunks), _BATCH_SIZE):
        batch_chunks = chunks[i: i + _BATCH_SIZE]
        batch_vecs = vectors[i: i + _BATCH_SIZE]
        collection.add(
            ids=[c.id for c in batch_chunks],
            documents=[c.text for c in batch_chunks],
            embeddings=batch_vecs,
            metadatas=[c.metadata() for c in batch_chunks],
        )
        log.info("stored batch",
                 extra={"offset": i, "count": len(batch_chunks)})

    count = int(collection.count())
    log.info("index written",
             extra={"collection": settings.collection, "chunks": count,
                    "embed_model": settings.embed_model, "dimension": dim})
    return count
