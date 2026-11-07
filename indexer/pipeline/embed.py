"""Stage 4: Embed. One vector per chunk, via the pinned model only."""

from __future__ import annotations

import json
import logging
import time
import urllib.error
import urllib.request

from .config import IndexerSettings

log = logging.getLogger("rahbar.indexer.embed")

_BATCH_SIZE = 32  # Ollama handles up to ~64 comfortably
_MAX_RETRIES = 3
_RETRY_BACKOFF = 1.5  # seconds base backoff


def embed(texts: list[str], settings: IndexerSettings) -> list[list[float]]:
    """Embed all texts via Ollama /api/embed, in batches of _BATCH_SIZE.

    Retries transient failures with exponential backoff and validates that
    all returned vectors are non-empty and uniform in dimension.
    """
    if not texts:
        return []

    url = f"{settings.ollama_url.rstrip('/')}/api/embed"
    all_vecs: list[list[float]] = []
    expected_dim: int | None = None

    for i in range(0, len(texts), _BATCH_SIZE):
        batch = texts[i : i + _BATCH_SIZE]
        payload = json.dumps({
            "model": settings.embed_model,
            "input": batch,
        }).encode("utf-8")

        response_data: dict[str, list[list[float]]] | None = None
        last_error: Exception | None = None

        for attempt in range(1, _MAX_RETRIES + 1):
            try:
                req = urllib.request.Request(
                    url,
                    data=payload,
                    headers={"Content-Type": "application/json"},
                )
                with urllib.request.urlopen(req, timeout=120) as resp:
                    response_data = json.loads(resp.read().decode("utf-8"))
                break
            except (urllib.error.URLError, TimeoutError, OSError) as exc:
                last_error = exc
                if attempt < _MAX_RETRIES:
                    wait_time = _RETRY_BACKOFF * (2 ** (attempt - 1))
                    log.warning(
                        "embed request failed, retrying",
                        extra={
                            "attempt": attempt,
                            "max_retries": _MAX_RETRIES,
                            "error": str(exc),
                            "wait_seconds": wait_time,
                        },
                    )
                    time.sleep(wait_time)
                else:
                    log.error(
                        "embed request failed after retries",
                        extra={"attempts": _MAX_RETRIES, "error": str(exc)},
                    )

        if response_data is None:
            raise RuntimeError(
                f"Failed to embed batch at offset {i} after {_MAX_RETRIES} attempts: {last_error}"
            )

        batch_embeddings = response_data.get("embeddings") or []
        if len(batch_embeddings) != len(batch):
            raise ValueError(
                f"Embedding count mismatch: expected {len(batch)}, received {len(batch_embeddings)}"
            )

        for vec_idx, vec in enumerate(batch_embeddings):
            if not vec:
                raise ValueError(f"Empty embedding vector received at index {i + vec_idx}")
            if expected_dim is None:
                expected_dim = len(vec)
            elif len(vec) != expected_dim:
                raise ValueError(
                    f"Dimension mismatch at index {i + vec_idx}: expected {expected_dim}, got {len(vec)}"
                )

        all_vecs.extend(batch_embeddings)
        log.info(
            "embedded batch",
            extra={"batch": i // _BATCH_SIZE + 1, "count": len(batch), "dimension": expected_dim},
        )

    return all_vecs
