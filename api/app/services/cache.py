"""Semantic cache layer backed by Redis for RAHBAR API.

Key prefix = index_version read from Chroma collection metadata.
Re-read with a short TTL (~30 seconds) so re-indexing automatically invalidates the cache.
Hits require cosine similarity >= CACHE_THRESHOLD.
Never caches follow-ups, abstentions, fallbacks, or unvalidated answers.
"""

from __future__ import annotations

import hashlib
import json
import logging
import math
import time
import urllib.request
from typing import Any

from ..config import Settings
from ..routes.metrics import CACHE_HITS, CACHE_MISSES
from ..schemas import AskResponse

log = logging.getLogger("rahbar.cache")

_FOLLOWUP_PREFIXES = ("and ", "also ", "what about ", "follow", "additionally ")


def is_followup(question: str) -> bool:
    """Check if the question is a conversational follow-up."""
    q = question.strip().lower()
    return any(q.startswith(p) for p in _FOLLOWUP_PREFIXES)


def should_never_cache(question: str, response: AskResponse | None = None) -> bool:
    """Never-cache rules: follow-up questions, abstentions, and fallbacks."""
    if is_followup(question):
        return True
    if response is not None:
        if not response.answered_from_context:
            return True
        if not response.citations:
            return True
        if response.confidence == "low":
            return True
        ans_lower = response.answer.lower()
        if "could not generate" in ans_lower or "do not have enough information" in ans_lower:
            return True
    return False


def _cosine_similarity(vec_a: list[float], vec_b: list[float]) -> float:
    """Calculate cosine similarity between two vectors."""
    if not vec_a or not vec_b or len(vec_a) != len(vec_b):
        return 0.0
    dot = sum(a * b for a, b in zip(vec_a, vec_b, strict=True))
    norm_a = math.sqrt(sum(a * a for a in vec_a))
    norm_b = math.sqrt(sum(b * b for b in vec_b))
    if norm_a == 0.0 or norm_b == 0.0:
        return 0.0
    return dot / (norm_a * norm_b)


class SemanticCache:
    """Redis semantic cache keyed by Chroma index_version with cosine similarity check."""

    def __init__(self, settings: Settings, ttl: int = 3600) -> None:
        self.settings = settings
        self.host = settings.redis_host
        self.port = settings.redis_port
        self.threshold: float = getattr(settings, "cache_threshold", 0.92)
        self.ttl = ttl
        self._version_ttl: float = getattr(settings, "cache_index_version_ttl", 30.0)
        self._client: Any = None
        self._chroma_client: Any = None
        self.enabled: bool = (settings.llm_provider != "stub")
        self._cached_index_version: str | None = None
        self._last_version_check: float = 0.0

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

    def get_index_version(self) -> str:
        """Fetch index_version from Chroma collection metadata, caching for 30s."""
        now = time.monotonic()
        if self._cached_index_version and (now - self._last_version_check < self._version_ttl):
            return self._cached_index_version

        try:
            import chromadb
            from chromadb.config import Settings as ChromaSettings

            if self._chroma_client is None:
                self._chroma_client = chromadb.HttpClient(
                    host=self.settings.chroma_host,
                    port=self.settings.chroma_port,
                    settings=ChromaSettings(anonymized_telemetry=False),
                )
            col = self._chroma_client.get_collection(self.settings.collection)
            meta = col.metadata or {}
            self._cached_index_version = str(meta.get("index_version", "default"))
        except Exception as exc:
            log.warning("could not read index_version from chroma", extra={"error": str(exc)})
            if not self._cached_index_version:
                self._cached_index_version = "default"

        self._last_version_check = now
        return self._cached_index_version

    def _embed(self, text: str) -> list[float]:
        """Embed question for cache lookup/store via Ollama or hash."""
        if getattr(self.settings, "embed_provider", "ollama") == "hash":
            from ..retrieval.hash_embed import hash_embed

            return hash_embed(text)

        url = f"{self.settings.ollama_url.rstrip('/')}/api/embed"
        payload = json.dumps({
            "model": self.settings.embed_model,
            "input": text,
        }).encode("utf-8")
        req = urllib.request.Request(
            url,
            data=payload,
            headers={"Content-Type": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        return data["embeddings"][0]

    def lookup(
        self,
        question: str,
        question_embedding: list[float] | None = None,
    ) -> AskResponse | None:
        """Lookup cached AskResponse if cosine similarity >= threshold."""
        if should_never_cache(question):
            return None

        client = self._get_client()
        if client is None:
            return None

        try:
            vec = question_embedding if question_embedding is not None else self._embed(question)
        except Exception as exc:
            log.warning("cache embedding failed during lookup", extra={"error": str(exc)})
            return None

        try:
            index_ver = self.get_index_version()
            prefix = f"cache:{index_ver}:"
            keys = client.keys(f"{prefix}*")
            if not keys:
                CACHE_MISSES.inc()
                return None

            best_sim = -1.0
            best_entry: dict[str, Any] | None = None

            for key in keys:
                raw = client.get(key)
                if not raw:
                    continue
                try:
                    entry = json.loads(raw)
                    cached_vec = entry.get("embedding", [])
                    sim = _cosine_similarity(vec, cached_vec)
                    if sim > best_sim:
                        best_sim = sim
                        best_entry = entry
                except (json.JSONDecodeError, KeyError):
                    continue

            if best_entry is not None and best_sim >= self.threshold:
                CACHE_HITS.inc()
                log.info(
                    "semantic cache hit",
                    extra={"similarity": round(best_sim, 4), "threshold": self.threshold},
                )
                resp_dict = best_entry["response"]
                return AskResponse.model_validate(resp_dict)

            CACHE_MISSES.inc()
            return None
        except Exception as exc:
            log.warning("cache lookup error", extra={"error": str(exc)})
            return None

    def store(
        self,
        question: str,
        response: AskResponse,
        question_embedding: list[float] | None = None,
    ) -> bool:
        """Store response in cache if allowed by caching policy."""
        if should_never_cache(question, response):
            return False

        client = self._get_client()
        if client is None:
            return False

        try:
            vec = question_embedding if question_embedding is not None else self._embed(question)
        except Exception as exc:
            log.warning("cache embedding failed during store", extra={"error": str(exc)})
            return False

        try:
            index_ver = self.get_index_version()
            q_hash = hashlib.sha256(question.strip().lower().encode("utf-8")).hexdigest()[:16]
            key = f"cache:{index_ver}:{q_hash}"
            entry = {
                "question": question,
                "embedding": vec,
                "response": response.model_dump(),
            }
            client.set(key, json.dumps(entry), ex=self.ttl)
            log.info("stored response in semantic cache", extra={"key": key})
            return True
        except Exception as exc:
            log.warning("failed to store in semantic cache", extra={"error": str(exc)})
            return False
