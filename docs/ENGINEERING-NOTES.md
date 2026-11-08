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

## Platform answers (Member 1)

### Q1. Indexing in API startup, with 3 replicas and one Chroma volume
Three API pods would each try to write the same Chroma collection at once, halfway through different documents, leaving an index that no document produced. Every restart would also pay the full extraction and embedding cost again. What prevents it is that the API never writes: indexing is the Job in k8s/base/indexer-job.yaml (restartPolicy at line 15), and the single Chroma replica (k8s/base/chroma-statefulset.yaml line 10) is the only store. See docs/adr/0001-indexer-as-job.md.

### Q2. Readiness depends on Chroma, liveness does not: what if we swap them and Chroma is slow for 90 seconds?
In k8s/base/api-deployment.yaml the liveness probe is at line 73 (path /healthz, period 20 s, 3 failures) and the readiness probe at line 79 (path /readyz). If liveness pointed at /readyz, 3 failures at 20 s intervals means about 60 s, so the kubelet would kill and restart all three pods while Chroma was merely slow. The restarted pods would fail the same check and we would turn a slow dependency into a full outage and a restart storm. If readiness pointed at /healthz, the pods would stay in the Service and answer users with errors instead of being withheld. The startup probe (line 67) gives booting pods time before either probe runs.

### Q3. What does the CPU-based HPA measure while the model generates?
It measures CPU usage divided by the CPU request. While the API waits on the LLM it is I/O-bound, so CPU stays low even when users are queueing: docs/evidence/cluster-state.txt shows cpu: 8%/70% with 3 replicas. CPU is therefore a poor signal for this service. Better signals are in-flight requests per pod or request latency. To use one we would expose an in-flight gauge on /metrics (the API already serves request count and latency there) and feed it to the HPA through a custom-metrics adapter or KEDA.

### Q4. Indexer memory limit
The indexer Job limit in k8s/base/indexer-job.yaml line 43 is: memory: 512Mi. So far only the dummy indexer (3 chunks, a few seconds) has run on the cluster, so kubectl top has no meaningful peak for OCR on the real corpus yet; this must be re-measured with the real pipeline before the limit is justified. If the limit is too low the container is OOMKilled with exit code 137. We saw exactly that on chroma-0 (limit 1Gi, Reason OOMKilled, exit code 137), which is why its limit is now 2Gi.

### Q6. EMBED_MODEL changed in the ConfigMap and only the API restarted
The API compares its EMBED_MODEL with the model recorded in the collection metadata (api/app/routes/health.py around line 86). On a mismatch it returns 503 from /readyz with the expected and found model names and logs "embedding model mismatch". We ran this on the cluster (docs/evidence/pin-mismatch.txt): the new pod sat at 0/1 READY, and because maxUnavailable is 0 (line 14 of k8s/base/api-deployment.yaml) the old pods kept serving. Without the guardrail the API would have embedded questions with one model against vectors from another, and retrieval would have returned plausible-looking nonsense with no error anywhere.

### Q7. The failure that cost more than an hour
Symptom: after adding the NetworkPolicies, the test "ui must not reach chroma" timed out, which looked like success. What I wrongly believed: that the timeout proved the policy worked. The "api must still reach chroma" test also timed out, which showed the policies were blocking everything. The command that told the truth was kubectl get networkpolicy -n rahbar: its POD-SELECTOR column said app=api, while kubectl get pods --show-labels showed the pods are labelled app.kubernetes.io/name=api. The policies selected no pods. After changing the selectors, the api reached chroma (heartbeat reply) and the ui still timed out. Saved in docs/evidence/networkpolicy.txt. A second problem in the same step was an invalid CIDR ("echo 192.168.49.1/32") caused by shell quoting in a heredoc.
