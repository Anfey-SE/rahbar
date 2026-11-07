"""Semantic cache layer backed by Redis for RAHBAR API.

Caches answers based on question embedding cosine similarity against a threshold.
Invalidated by generation ID bump on re-indexing.
Follow-up questions and abstentions are never cached.
"""

from __future__ import annotations

import json
import logging
import math
from typing import Any

from ..config import Settings
from ..schemas import AskResponse, RetrievedRef

log = logging.getLogger("rahbar.cache")

_FOLLOWUP_PREFIXES = ("and ", "also ", "what about ", "follow", "additionally ")


def is_followup(question: str) -> bool:
    """Check if the question is a conversational follow-up."""
    q = question.strip().lower()
    return any(q.startswith(p) for p in _FOLLOWUP_PREFIXES)


def should_never_cache(question: str, response: AskResponse | None = None) -> bool:
    """Never-cache rules: follow-up questions and abstentions must never be cached."""
    if is_followup(question):
        return True
    if response is not None and not response.answered_from_context:
        return True
    return False


def _cosine_similarity(vec_a: list[float], vec_b: list[float]) -> float:
    """Calculate cosine similarity between two vectors."""
    if not vec_a or not vec_b or len(vec_a) != len(vec_b):
        return 0.0
    dot = sum(a * b for a, b in zip(vec_a, vec_b))
    norm_a = math.sqrt(sum(a * a for a in vec_a))
    norm_b = math.sqrt(sum(b * b for b in vec_b))
    if norm_a == 0.0 or norm_b == 0.0:
        return 0.0
    return dot / (norm_a * norm_b)


class SemanticCache:
    """Redis-backed semantic cache with cosine similarity threshold and generation invalidation."""

    def __init__(self, settings: Settings, ttl: int = 3600) -> None:
        self.host = settings.redis_host
        self.port = settings.redis_port
        self.threshold: float = getattr(settings, "cache_threshold", 0.92)
        self.ttl = ttl
        self._client: Any = None
        self.enabled: bool = (settings.llm_provider != "stub")

    def _get_client(self) -> Any:
        if not self.enabled:
            return None
        if self._client is None:
            try:
                import redis

                self._client = redis.Redis(
                    host=self.host,
                    port=self.port,
                    socket_connect_timeout=2.0,
                    decode_responses=True,
                )
            except Exception as exc:
                log.warning("failed to initialize redis client", extra={"error": str(exc)})
                return None
        return self._client

    def generation_id(self) -> int:
        """Get current cache generation ID. Incremented on re-index."""
        client = self._get_client()
        if client is None:
            return 0
        try:
            val = client.get("cache:generation_id")
            return int(val) if val else 0
        except Exception:
            return 0

    def bump_generation(self) -> int:
        """Bump generation ID to invalidate all current cache entries atomically."""
        client = self._get_client()
        if client is None:
            return 0
        try:
            new_gen = client.incr("cache:generation_id")
            log.info("bumped cache generation_id", extra={"generation_id": new_gen})
            return new_gen
        except Exception as exc:
            log.warning("failed to bump cache generation_id", extra={"error": str(exc)})
            return 0

    def invalidate(self) -> bool:
        """Invalidate cache entries via generation bump."""
        return self.bump_generation() > 0

    def clear_cache(self) -> bool:
        """Flush or clear all cached entries."""
        client = self._get_client()
        if client is None:
            return False
        try:
            client.flushdb()
            log.info("flushed redis cache")
            return True
        except Exception as exc:
            log.warning("failed to flush redis cache", extra={"error": str(exc)})
            return False

    def lookup(
        self,
        question: str,
        question_embedding: list[float] | None = None,
    ) -> AskResponse | None:
        """Look up semantically cached response using cosine similarity against threshold."""
        if should_never_cache(question):
            return None

        client = self._get_client()
        if client is None:
            return None

        try:
            gen = self.generation_id()
            prefix = f"cache:v{gen}:"
            keys = client.keys(f"{prefix}*")
            if not keys:
                return None

            best_sim = -1.0
            best_entry: dict[str, Any] | None = None

            for key in keys:
                raw = client.get(key)
                if not raw:
                    continue
                entry = json.loads(raw)
                # Exact match hit
                if entry.get("question", "").strip().lower() == question.strip().lower():
                    best_entry = entry
                    best_sim = 1.0
                    break

                # Vector similarity match if embeddings available
                if question_embedding and entry.get("embedding"):
                    sim = _cosine_similarity(question_embedding, entry["embedding"])
                    if sim > best_sim:
                        best_sim = sim
                        best_entry = entry

            if best_entry is not None and best_sim >= self.threshold:
                log.info(
                    "semantic cache hit",
                    extra={"similarity": round(best_sim, 4), "threshold": self.threshold},
                )
                resp_data = best_entry["response"]
                return AskResponse(
                    request_id=resp_data.get("request_id", ""),
                    answer=resp_data["answer"],
                    citations=resp_data.get("citations", []),
                    answered_from_context=resp_data.get("answered_from_context", True),
                    confidence=resp_data.get("confidence", "high"),
                    retrieved=[RetrievedRef(**r) for r in resp_data.get("retrieved", [])],
                    provider=resp_data.get("provider", "cache"),
                )

        except Exception as exc:
            log.warning("cache lookup error", extra={"error": str(exc)})

        return None

    def store(
        self,
        question: str,
        response: AskResponse,
        question_embedding: list[float] | None = None,
    ) -> bool:
        """Store response in cache unless never_cache condition is met."""
        if should_never_cache(question, response):
            log.debug("never_cache rule matched; skipping cache store")
            return False

        client = self._get_client()
        if client is None:
            return False

        try:
            gen = self.generation_id()
            import hashlib

            q_hash = hashlib.sha256(question.strip().lower().encode()).hexdigest()[:16]
            key = f"cache:v{gen}:{q_hash}"

            payload = {
                "question": question,
                "embedding": question_embedding,
                "response": {
                    "request_id": response.request_id,
                    "answer": response.answer,
                    "citations": [c.model_dump() for c in response.citations],
                    "answered_from_context": response.answered_from_context,
                    "confidence": response.confidence,
                    "retrieved": [r.model_dump() for r in response.retrieved],
                    "provider": response.provider,
                },
            }
            client.setex(key, self.ttl, json.dumps(payload))
            log.debug("stored answer in semantic cache", extra={"key": key, "ttl": self.ttl})
            return True
        except Exception as exc:
            log.warning("failed to store in cache", extra={"error": str(exc)})
            return False
