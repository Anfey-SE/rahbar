"""Stage 5: Store. The only code in the whole system that writes to Chroma."""

from __future__ import annotations

from datetime import datetime, timezone
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
    """Store chunks in Chroma without deleting the collection (zero-downtime).

    1. Gets or creates the collection.
    2. Upserts all new chunks in batches.
    3. Prunes stale IDs no longer in the current index.
    4. Updates collection metadata last with embed_model, dimension, and
       a fresh index_version timestamp.
    """
    if not chunks:
        return 0

    client = get_client(settings)
    dim = len(vectors[0]) if vectors else 0

    # Get or create collection without deleting to prevent API downtime
    collection = client.get_or_create_collection(
        name=settings.collection,
        metadata={"hnsw:space": "cosine"},
    )

    # Fetch existing IDs so we can prune stale documents
    existing_ids: set[str] = set()
    try:
        existing_data = collection.get(include=[])
        if existing_data and "ids" in existing_data:
            existing_ids = set(existing_data["ids"])
    except Exception as exc:
        log.warning("could not retrieve existing collection IDs", extra={"error": str(exc)})

    # Upsert new chunks in batches
    new_ids: set[str] = set()
    for i in range(0, len(chunks), _BATCH_SIZE):
        batch_chunks = chunks[i : i + _BATCH_SIZE]
        batch_vecs = vectors[i : i + _BATCH_SIZE]
        batch_ids = [c.id for c in batch_chunks]
        new_ids.update(batch_ids)

        collection.upsert(
            ids=batch_ids,
            documents=[c.text for c in batch_chunks],
            embeddings=batch_vecs,
            metadatas=[c.metadata() for c in batch_chunks],
        )
        log.info("upserted batch", extra={"offset": i, "count": len(batch_chunks)})

    # Prune stale IDs
    stale_ids = list(existing_ids - new_ids)
    if stale_ids:
        log.info("pruning stale chunks", extra={"count": len(stale_ids)})
        for i in range(0, len(stale_ids), _BATCH_SIZE):
            collection.delete(ids=stale_ids[i : i + _BATCH_SIZE])

    # Generate fresh index_version timestamp
    index_version = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")

    # Update collection metadata last (pins model & invalidates old cache entries)
    # Note: hnsw:space cannot be modified after creation, only mutable metadata is passed
    collection.modify(
        metadata={
            "embed_model": settings.embed_model,
            "dimension": dim,
            "index_version": index_version,
        }
    )

    count = int(collection.count())
    log.info(
        "index updated",
        extra={
            "collection": settings.collection,
            "chunks": count,
            "embed_model": settings.embed_model,
            "dimension": dim,
            "index_version": index_version,
        },
    )
    return count
