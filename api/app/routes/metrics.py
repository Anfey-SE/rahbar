"""Prometheus metrics at /metrics (section 4.8).

Request count and latency are wired up already. Add your own the same way:
retrieval latency, generation latency, cache hits, abstentions, fallbacks, tokens.
"""

from __future__ import annotations

from fastapi import APIRouter, Response
from prometheus_client import CONTENT_TYPE_LATEST, Counter, Histogram, generate_latest

router = APIRouter()

REQUESTS = Counter("rahbar_requests_total", "HTTP requests", ["path", "status"])
LATENCY = Histogram(
    "rahbar_request_seconds",
    "HTTP request latency",
    ["path"],
    buckets=(0.05, 0.1, 0.25, 0.5, 1, 2, 5, 10, 30, 60),
)

ABSTENTIONS = Counter(
    "rahbar_abstentions_total",
    "Total count of abstained requests",
    ["reason"],
)

FALLBACKS = Counter(
    "rahbar_fallbacks_total",
    "Total count of fallback responses",
    ["reason"],
)

CACHE_HITS = Counter(
    "rahbar_cache_hits_total",
    "Total count of semantic cache hits",
)

CACHE_MISSES = Counter(
    "rahbar_cache_misses_total",
    "Total count of semantic cache misses",
)

RETRIEVAL_LATENCY = Histogram(
    "rahbar_retrieval_seconds",
    "Retrieval duration in seconds",
    buckets=(0.01, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0),
)

GENERATION_LATENCY = Histogram(
    "rahbar_generation_seconds",
    "LLM generation duration in seconds",
    buckets=(0.1, 0.5, 1.0, 2.5, 5.0, 10.0, 20.0, 60.0),
)

LLM_PROMPT_TOKENS = Counter(
    "rahbar_llm_prompt_tokens_total",
    "Total LLM prompt tokens evaluated",
    ["provider"],
)

LLM_EVAL_TOKENS = Counter(
    "rahbar_llm_eval_tokens_total",
    "Total LLM completion tokens evaluated",
    ["provider"],
)


@router.get("/metrics")
def metrics() -> Response:
    return Response(generate_latest(), media_type=CONTENT_TYPE_LATEST)
