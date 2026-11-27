"""D1: Trace record writer.

Every query emits one JSON line to stdout (picked up by kubectl logs)
and to TRACE_LOG_PATH (a volume-mounted file for offline analysis).

Trace fields (RAGOps Table 2 / section 5.1):
  trace_id, timestamp, query, rewritten_query, embed_model, embed_version,
  collection, collection_version, retrieved (chunk_id, doc_id, doc_version,
  score), filters, reranked_order, prompt_hash, model, model_params,
  response, citations, guardrail_actions, stage_latencies, cache_hit.
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

log = logging.getLogger("rahbar.traces")

TRACE_LOG_PATH = Path(os.environ.get("TRACE_LOG_PATH", "/tmp/traces/traces.jsonl"))


def _ensure_trace_dir() -> None:
    try:
        TRACE_LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    except OSError:
        pass


@dataclass
class StageLatencies:
    cache_lookup_ms: float = 0.0
    retrieval_ms: float = 0.0
    rerank_ms: float = 0.0
    generation_ms: float = 0.0
    total_ms: float = 0.0

    def to_dict(self) -> dict:
        return {
            "cache_lookup_ms": round(self.cache_lookup_ms, 2),
            "retrieval_ms": round(self.retrieval_ms, 2),
            "rerank_ms": round(self.rerank_ms, 2),
            "generation_ms": round(self.generation_ms, 2),
            "total_ms": round(self.total_ms, 2),
        }


@dataclass
class RetrievedChunkTrace:
    chunk_id: str
    doc_id: str
    doc_version: str
    score: float
    reranked_position: int | None = None


@dataclass
class TraceRecord:
    # Identity
    trace_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    request_id: str = ""
    timestamp: str = field(default_factory=lambda: time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()))

    # Query
    query: str = ""
    rewritten_query: str = ""

    # Embedding
    embed_model: str = ""
    embed_version: str = "1"

    # Vector store
    collection: str = ""
    collection_version: str = ""

    # Retrieval
    retrieved: list[RetrievedChunkTrace] = field(default_factory=list)
    filters: dict[str, Any] = field(default_factory=dict)
    reranked_order: list[str] = field(default_factory=list)  # chunk_ids in reranked order

    # Generation
    prompt_hash: str = ""
    full_prompt: str = ""
    model: str = ""
    model_params: dict[str, Any] = field(default_factory=dict)
    response: str = ""
    citations: list[dict] = field(default_factory=list)

    # Guardrails
    guardrail_actions: list[dict] = field(default_factory=list)

    # Timing
    stage_latencies: StageLatencies = field(default_factory=StageLatencies)

    # Cache
    cache_hit: bool = False

    # Outcome
    answered_from_context: bool = True
    abstention_reason: str = ""
    confidence: str = ""

    def set_prompt(self, prompt: str) -> None:
        self.full_prompt = prompt
        self.prompt_hash = hashlib.sha256(prompt.encode()).hexdigest()[:16]

    def add_guardrail(self, action: str, detail: str = "", triggered: bool = True) -> None:
        self.guardrail_actions.append({
            "action": action,
            "detail": detail,
            "triggered": triggered,
        })

    def to_dict(self) -> dict:
        return {
            "trace_id": self.trace_id,
            "request_id": self.request_id,
            "timestamp": self.timestamp,
            "query": self.query,
            "rewritten_query": self.rewritten_query,
            "embed_model": self.embed_model,
            "embed_version": self.embed_version,
            "collection": self.collection,
            "collection_version": self.collection_version,
            "retrieved": [
                {
                    "chunk_id": r.chunk_id,
                    "doc_id": r.doc_id,
                    "doc_version": r.doc_version,
                    "score": round(r.score, 6),
                    "reranked_position": r.reranked_position,
                }
                for r in self.retrieved
            ],
            "filters": self.filters,
            "reranked_order": self.reranked_order,
            "prompt_hash": self.prompt_hash,
            "model": self.model,
            "model_params": self.model_params,
            "response": self.response,
            "citations": self.citations,
            "guardrail_actions": self.guardrail_actions,
            "stage_latencies": self.stage_latencies.to_dict(),
            "cache_hit": self.cache_hit,
            "answered_from_context": self.answered_from_context,
            "abstention_reason": self.abstention_reason,
            "confidence": self.confidence,
        }

    def emit(self) -> None:
        """Write one JSON line to stdout and to the trace log file."""
        line = json.dumps(self.to_dict(), ensure_ascii=False)
        # stdout → kubectl logs
        print(line, flush=True)
        # volume file → offline analysis
        _ensure_trace_dir()
        try:
            with TRACE_LOG_PATH.open("a", encoding="utf-8") as f:
                f.write(line + "\n")
        except OSError as exc:
            log.warning("trace write failed: %s", exc)
