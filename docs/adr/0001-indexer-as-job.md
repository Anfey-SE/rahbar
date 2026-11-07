# ADR 0001 — Indexer runs as a Kubernetes Job, not in API startup

**Date:** 2026-11-01
**Status:** Accepted
**Deciders:** Member 1 (Platform), Member 2 (RAG)

## Context

The API has 3 replicas sharing one Chroma StatefulSet backed by a single PVC. If the indexer ran at API startup, 3 pods would call `collection.add()` simultaneously on every restart — pod eviction, rolling update, or crash. Chroma's HNSW index is not safe for concurrent writers; the result is data corruption, duplicate IDs, or a deadlock. Additionally, a re-index on every restart imposes an unacceptable cold-start penalty.

## Decision

The indexer is a separate Kubernetes **Job** (`k8s/base/indexer-job.yaml`) with `restartPolicy: OnFailure` and `backoffLimit: 3`. The API image has no indexing code at all. A weekly **CronJob** (`k8s/base/indexer-cronjob.yaml`) handles scheduled re-indexing. The store stage atomically replaces the collection (delete-then-create) so the API continues reading the old collection until the new one is fully written.

## Consequences

- **Good:** No write races; the Job's exit code gates success; CronJob handles scheduled re-index; the API is provably read-only.
- **Bad:** After a fresh `kubectl apply`, the first `/readyz` returns 503 until the Job completes (~2 min with the dummy index). Operators must `kubectl wait --for=condition=complete job/indexer -n rahbar`.
- **Implementation:** `k8s/base/indexer-job.yaml` with args `["python", "-m", "pipeline.run", "--dummy"]` for week-1 CI; real pipeline uses the same image without `--dummy`.
