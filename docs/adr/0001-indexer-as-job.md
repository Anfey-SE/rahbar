# ADR 0001: The indexer runs as a Kubernetes Job, not at API startup

Status: accepted

## Context
The API runs as 3 replicas against one Chroma volume. The corpus has to be extracted, OCR'd, chunked and embedded before any question can be answered.

## Decision
Indexing runs as a one-shot `Job` (`k8s/base/indexer-job.yaml`) and a weekly `CronJob` (`reindex-cronjob.yaml`) with the same image and arguments. The API only reads. The `chroma` StatefulSet is the only store, and the Job is the only writer.

## Why not index at API startup
1. Three replicas would write one volume at once and could leave an index no document produced.
2. Every restart, rolling update or OOM kill would pay the full embedding cost again.
3. Readiness would be meaningless: a pod is either ready before the index exists or takes minutes and gets killed by its probes.
4. Re-indexing would need a redeploy.

## Consequences
- `/readyz` returns 503 until the collection exists and is non-empty, so pods without an index stay out of the Service.
- A re-index is one command with no downtime: `kubectl create job --from=cronjob/reindex manual-reindex-1 -n rahbar`.
- The `reindex` CronJob has been seen firing on its own and completing in the cluster.
