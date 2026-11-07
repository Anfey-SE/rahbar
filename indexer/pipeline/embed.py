"""Stage 4: Embed. One vector per chunk, via the pinned model only."""

from __future__ import annotations

import json
import logging
import urllib.request

from .config import IndexerSettings

log = logging.getLogger("rahbar.indexer.embed")

_BATCH_SIZE = 32  # Ollama handles up to ~64 comfortably


def embed(texts: list[str], settings: IndexerSettings) -> list[list[float]]:
    """Embed all texts via Ollama /api/embed, in batches of _BATCH_SIZE."""
    url = f"{settings.ollama_url.rstrip('/')}/api/embed"
    all_vecs: list[list[float]] = []

    for i in range(0, len(texts), _BATCH_SIZE):
        batch = texts[i: i + _BATCH_SIZE]
        payload = json.dumps({
            "model": settings.embed_model,
            "input": batch,
        }).encode()
        req = urllib.request.Request(
            url, data=payload,
            headers={"Content-Type": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=120) as resp:
            data = json.loads(resp.read())
        all_vecs.extend(data["embeddings"])
        log.info("embedded batch",
                 extra={"batch": i // _BATCH_SIZE + 1, "count": len(batch)})

    return all_vecs
