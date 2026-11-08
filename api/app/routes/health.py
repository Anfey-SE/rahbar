"""Liveness and readiness (section 4.3). They answer different questions."""

from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, Request, Response

log = logging.getLogger("rahbar.health")

router = APIRouter()


@router.get("/healthz")
def healthz() -> dict[str, str]:
    """Is the process alive? Must never touch Chroma, Redis or the LLM."""
    return {"status": "alive"}


@router.get("/readyz")
def readyz(request: Request, response: Response) -> dict[str, Any]:
    """Is the process ready to serve traffic?

    Returns 200 only if Chroma is reachable, the collection exists and is non-empty,
    the embedding pin matches EMBED_MODEL, and Redis is reachable.
    Otherwise returns 503 with a JSON body naming the failing dependency.
    """
    settings = getattr(request.app.state, "settings", None)
    if settings is None:
        from ..config import get_settings

        settings = get_settings()

    # 1. Chroma reachability
    try:
        import chromadb
        from chromadb.config import Settings as ChromaSettings

        chroma_client = chromadb.HttpClient(
            host=settings.chroma_host,
            port=settings.chroma_port,
            settings=ChromaSettings(anonymized_telemetry=False),
        )
        chroma_client.heartbeat()
    except Exception as exc:
        response.status_code = 503
        return {
            "status": "not_ready",
            "failing_dependency": "chroma",
            "detail": f"Chroma unreachable: {exc}",
        }

    # 2. Collection exists
    try:
        collection = chroma_client.get_collection(settings.collection)
    except Exception as exc:
        response.status_code = 503
        return {
            "status": "not_ready",
            "failing_dependency": "collection",
            "detail": f"Collection '{settings.collection}' does not exist: {exc}",
        }

    # 3. Collection non-empty
    try:
        count = collection.count()
        if count == 0:
            response.status_code = 503
            return {
                "status": "not_ready",
                "failing_dependency": "collection",
                "detail": f"Collection '{settings.collection}' is empty",
                "count": 0,
            }
    except Exception as exc:
        response.status_code = 503
        return {
            "status": "not_ready",
            "failing_dependency": "collection",
            "detail": f"Failed counting collection '{settings.collection}': {exc}",
        }

    # 4. Embedding pin check
    metadata = collection.metadata or {}
    collection_embed_model = metadata.get("embed_model")
    if collection_embed_model != settings.embed_model:
        log.error(
            "embedding model mismatch",
            extra={"expected": settings.embed_model, "found": collection_embed_model},
        )
        response.status_code = 503
        detail_msg = (
            f"Collection embed_model '{collection_embed_model}' does not "
            f"match expected '{settings.embed_model}'"
        )
        return {
            "status": "not_ready",
            "failing_dependency": "embed_model",
            "detail": detail_msg,
            "expected": settings.embed_model,
            "found": collection_embed_model,
        }

    # 5. Redis reachability
    try:
        import redis

        redis_client = redis.Redis(
            host=settings.redis_host,
            port=settings.redis_port,
            socket_connect_timeout=2.0,
            socket_timeout=2.0,
        )
        redis_client.ping()
    except Exception as exc:
        response.status_code = 503
        return {
            "status": "not_ready",
            "failing_dependency": "redis",
            "detail": f"Redis unreachable: {exc}",
        }

    return {
        "status": "ready",
        "checks": {
            "chroma": "ok",
            "collection": "ok",
            "count": count,
            "embed_model": collection_embed_model,
            "redis": "ok",
        },
    }
