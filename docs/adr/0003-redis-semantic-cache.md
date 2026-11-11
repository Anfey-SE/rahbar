# ADR 0003 — Redis as the semantic cache store, threshold 0.92

**Date:** 2026-11-02
**Status:** Accepted
**Deciders:** Member 1 (Platform), Member 2 (RAG)

## Context

Identical or semantically near-identical questions should not incur an LLM call on every request. We need a cache that can compare question embeddings (not just exact strings) and that fits in the existing cluster without adding a new stateful component.

## Decision

Use the **Redis** Deployment already in the cluster (required by the spec). Cache entries are keyed by a generation counter (`cache:gen`) plus a hash of the question embedding vector. Cosine similarity threshold: **0.92**. TTL: 3600 s.

**Threshold selection:**

| Threshold | False-hit example | Verdict |
|-----------|-------------------|---------|
| 0.85 | "What is the CGPA limit?" → hit "What is the GPA requirement?" | Rejected |
| 0.90 | "Minimum attendance?" → hit "Max absences allowed?" | Rejected |
| **0.92** | No false hits on 15-question golden set | **Shipped** |

**Never cached:** Abstentions (`answered_from_context=false`) and follow-up questions (question starts with "and", "also", "what about", "follow").

**Cache invalidation:** After a successful re-index, the indexer CronJob increments `cache:gen` in Redis. All old cache keys (which include the previous generation number) are effectively orphaned and expire naturally via TTL.

## Consequences

- **Good:** Sub-millisecond cache hits; no new infrastructure; invalidation is atomic (one INCR).
- **Bad:** Redis is not a vector database; linear scan over all cached vectors is O(n). With a small cache (< 1000 entries) this is fine. At scale, switch to a vector-indexed cache (e.g. Redis with RediSearch).
- **PII note:** Question text is embedded and stored in Redis in-cluster. Questions never leave the cluster unless `LLM_PROVIDER=hosted`.
