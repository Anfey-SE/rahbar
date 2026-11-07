# Engineering Notes

## 1. Indexing in API startup: what breaks with 3 replicas and one Chroma volume?

If the API indexed on startup, all 3 replicas would call `collection.add()` simultaneously against the same Chroma StatefulSet on every pod restart. Chroma's HNSW index is not designed for concurrent writers; the result is data corruption, duplicate chunk IDs, or a deadlock that prevents any replica from becoming Ready. Beyond correctness, a re-index on every restart means a cold-start penalty on every rolling update and every pod eviction — unacceptable at 3 replicas. We moved all writes to the Kubernetes Job (`k8s/base/indexer-job.yaml`), making the API strictly read-only. See ADR 0001.

## 2. Readiness and liveness swapped, Chroma slow for 90 seconds: what happens?

If `/readyz` is wired to liveness and `/healthz` to readiness (the swap), Kubernetes kills and restarts a pod the moment Chroma is slow — even if the pod itself is healthy. With 3 replicas and a 90-second Chroma hiccup:

1. All 3 liveness probes (now on `/readyz`) fail within `failureThreshold × period = 3 × 20 = 60 s`.
2. Kubernetes kills all 3 pods and schedules replacements.
3. The replacements also fail their liveness probes (Chroma is still slow).
4. This becomes a restart storm: 100% downtime for the duration of the Chroma hiccup.

With probes correctly wired: liveness → `/healthz` (always 200, no deps), readiness → `/readyz` (checks Chroma). A slow Chroma makes pods unready (removed from the Service endpoints) but does **not** restart them. Traffic is held at the load balancer; the moment Chroma recovers, pods report ready again. Zero restarts, zero dropped connections.

## 3. What does a CPU-based HPA measure on a generation service, and what should scale it?

A CPU-based HPA measures CPU utilisation averaged across pods. For a RAG service, most latency is **I/O-bound** (Chroma query, Ollama HTTP call, Redis lookup) — not CPU-bound. During a busy period the pods are mostly blocking on network I/O, so CPU stays low even as request queue depth grows. The HPA sees `12% / 70%` and does nothing, while users queue.

A better signal is **request throughput or queue depth** (via KEDA with a Prometheus trigger on `rahbar_requests_total`) or **p95 latency** (also from Prometheus). Either metric rises before CPU does and gives the scaler time to provision new pods before users notice degradation. We kept the CPU-based HPA as the baseline (required by the spec) and noted this limitation. See `docs/evidence/get-hpa.txt` for real output.

## 4. The indexer's peak memory, its limit, the margin, and the exit code when it is wrong

From `kubectl top pod` during a full corpus run: peak ~310 MiB. Limit set to 512 MiB (margin: 202 MiB, ~65%). If the indexer exceeds its memory limit, the kernel OOM-kills the container; Kubernetes reports `OOMKilled` with exit code 137. The Job's `restartPolicy: OnFailure` with `backoffLimit: 3` retries up to 3 times. After 3 OOMKills the Job status becomes `Failed` and the alert fires. The old collection (from the previous successful run) continues serving the API unchanged — because the API reads from Chroma independently and the new collection was never atomically committed.

## 5. recall@5 by question type: where is it worst, and why?

From `eval/results.json` with the stub retriever on the fixture corpus:

| Type | recall@5 |
|------|---------|
| ordinary | 0.75 |
| exact | 0.80 |
| version | 0.65 |
| injection | 0.70 |
| abstain | n/a |

**Version questions** are worst. When both the 2023 and 2026 handbooks are indexed, a version question like "What changed about grade capping?" retrieves chunks from both — but the golden set only marks one version's section as relevant. The retriever has no concept of which version the user wants; it returns the highest-similarity chunk regardless of `doc_version`. Fixing this requires either: (a) query-time version filtering based on a user-supplied year, or (b) only indexing the non-superseded version and answering version-change questions from the diff.

## 6. EMBED_MODEL changed on the API only: what does your system do?

The `/readyz` probe compares `EMBED_MODEL` (from the ConfigMap env var) against `collection.metadata["embed_model"]` (written by the indexer at index time). On mismatch it returns HTTP 503 with:

```json
{"status":"not_ready","failing_dependency":"embed_model",
 "detail":"Collection embed_model 'dummy-768' does not match expected 'nomic-embed-text'",
 "expected":"nomic-embed-text","found":"dummy-768"}
```

Because `readinessProbe` targets `/readyz` and the rolling update has `maxUnavailable: 0`, the new pods never enter the Service endpoints — old pods keep serving. No traffic sees a mismatched query. The startup `lifespan` check also fails fast, causing an immediate process exit rather than silent nonsense retrieval. See `docs/evidence/pin-mismatch.txt` for the live demonstration (steps 1–3).

## 7. The failure that cost us more than an hour

**Kubernetes service-link variable injection.** When `enableServiceLinks` is not set to `false`, Kubernetes injects environment variables for every Service in the namespace — including `REDIS_PORT=tcp://10.98.203.230:6379`. Our `config.py` `_int()` helper called `int(os.environ.get("REDIS_PORT", 6379))` and crashed with `ValueError: invalid literal for int() with base 10: 'tcp://...'`. The API pods entered `CrashLoopBackOff` immediately after the rollout. 

Diagnosis took time because the error only appeared in `kubectl logs --previous` (the container exited before the startup probe fired). The fix was `enableServiceLinks: false` on every pod spec **and** a defensive `_int()` that strips the `tcp://host:` prefix before parsing. Chroma had the same bug and was fixed earlier; we missed that the same injection affected every other workload.
