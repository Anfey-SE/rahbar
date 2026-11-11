# ADR 0003 — Redis as the Semantic Cache Store (Threshold 0.92)

**Date:** 2026-11-08  
**Status:** Accepted  
**Deciders:** Member 1 (Platform & Infrastructure), Member 2 (RAG & Retrieval Pipeline)

---

## Context

User queries in academic advising frequently exhibit high semantic repetition (e.g., *"When is academic warning applied?"* vs. *"What CGPA causes an academic warning?"*). Repeating the entire RAG pipeline (embedding, vector retrieval, prompt compilation, and LLM autoregressive inference) for repeated queries incurs substantial computational latency (~5–10s on CPU) and unnecessary load.

The assignment specification (Section 3.3) permits using either **Redis** or a **second ChromaDB collection** as the semantic cache backend, requiring the team to defend the architectural choice in an ADR.

---

## Options Considered

### Option 1: A Second ChromaDB Collection
- **Pros:** Native approximate nearest neighbor (ANN) vector indexing.
- **Cons:**
  1. **No native TTL / expiry:** Chroma lacks time-to-live mechanisms. Evicting old queries requires manual tombstone sweeps or collection recreations.
  2. **Write Latency & Resource Contention:** Adding new cache entries after every query requires embedding upserts and HNSW index modifications, competing with the indexer on disk I/O and CPU.
  3. **Concurrency:** In a multi-replica API architecture (3 replicas), concurrent writes to a shared Chroma cache volume introduce write-locking hazards and race conditions.

### Option 2: Redis In-Cluster Deployment (Chosen)
- **Pros:**
  1. **Built-in Key Expiration (TTL):** Automatic cache eviction via `EXPIRE` without background garbage-collection jobs.
  2. **Sub-millisecond Read/Write Latency:** In-memory operations avoid disk serialization bottlenecks during high-throughput query bursts.
  3. **Clean Cache Invalidation:** Partitioning keys by Chroma's collection `index_version` (`cache:{index_version}:{q_hash}`) or calling `flushdb()` allows instant, zero-downtime invalidation upon re-indexing.
  4. **Simple Linear Scan:** For realistic cache sizes (< 1,000 entries), cosine similarity over stored 768-dim float arrays completes in under 2 milliseconds in Python, eliminating HNSW indexing overhead.
- **Cons:** Linear scan is $O(N)$ with respect to cached queries. (If cache grows beyond 50,000 queries, Redis with RediSearch vector indexing can be adopted).

---

## Decision

We chose **Redis** as the semantic cache backend.

1. **Threshold Selection (0.92):** Evaluated against golden questions and paraphrased queries:

| Threshold | Evaluation Finding / False-Hit Example | Verdict |
| :---: | :--- | :---: |
| **0.85** | *"What is the CGPA limit for academic warning?"* hit *"What CGPA is required to be placed on the Dean's List?"* (similarity = 0.874). | Rejected (False hit) |
| **0.90** | *"Minimum attendance needed to sit the exam?"* hit *"What happens if a student misses classes due to illness?"* (similarity = 0.908). | Rejected (False hit) |
| **0.92** | Zero false hits on the 15 golden questions; exact semantic paraphrases hit cleanly (>0.94). | **Shipped** |

2. **Cache Invalidation:** Scoped by Chroma collection `index_version`. Re-indexing creates a new version timestamp, rendering all past cache entries instantly obsolete. An explicit `invalidate()` method flushes old keys.
3. **Never-Cache Rules:** Follow-up questions (`is_followup`), unanswerable/abstained answers (`answered_from_context: false`), and validation fallback responses are strictly excluded from the cache.
4. **Data Governance / PII:** Question embeddings and cached answers reside exclusively within the Kubernetes cluster inside the `rahbar` namespace.

---

## Consequences

- **Sub-5ms Cache Responses:** Queries hitting the semantic cache bypass Chroma retrieval and LLM generation completely, reducing response latency from ~8s to <5ms.
- **Cluster Isolation:** The Redis Service is ClusterIP only and strictly restricted by Kubernetes NetworkPolicy so only the API deployment can access it.
