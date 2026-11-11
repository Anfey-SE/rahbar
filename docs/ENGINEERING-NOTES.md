# Engineering Notes

## 5. recall@5 by question type: where is it worst, and why?

From our real evaluation on the 15-question golden set (`eval/results.json`):
- **Version-sensitive:** 1.0000 (100%)
- **Prompt injection:** 1.0000 (100%)
- **Ordinary:** 0.8750 (87.5%)
- **Exact-term:** 0.5000 (50.0%) — **Worst**

**Why embeddings struggle:**  
Dense embedding models like `nomic-embed-text` project text into a continuous semantic latent space optimized for general topical similarity rather than exact alphanumeric token overlap. When a student query specifies an exact numeric clause (`1.21` in q02) or specific fee calculation rule (`q11`), dense vector dot-products match broadly related passages (e.g. general attendance rules or closure of admission) that lack the exact clause number.

**RAG lecture technique:**  
Hybrid retrieval (sparse lexical BM25 / keyword search combined with dense vector embeddings via Reciprocal Rank Fusion) or metadata pre-filtering by clause number addresses this by ensuring exact token matches are boosted to the top ranks.

## Platform answers (Member 1)

### Q1. Indexing in API startup, with 3 replicas and one Chroma volume

Three API pods would each try to write the same Chroma collection at once, halfway through different documents, leaving an index that no document produced. Every restart would also pay the full extraction and embedding cost again. What prevents it is that the API never writes: indexing is the Job in k8s/base/indexer-job.yaml (restartPolicy at line 15), and the single Chroma replica (k8s/base/chroma-statefulset.yaml line 10) is the only store. See docs/adr/0001-indexer-as-job.md.

### Q2. Readiness depends on Chroma, liveness does not: what if we swap them and Chroma is slow for 90 seconds?

In k8s/base/api-deployment.yaml the liveness probe is at line 73 (path /healthz, period 20 s, 3 failures) and the readiness probe at line 79 (path /readyz). If liveness pointed at /readyz, 3 failures at 20 s intervals means about 60 s, so the kubelet would kill and restart all three pods while Chroma was merely slow. The restarted pods would fail the same check and we would turn a slow dependency into a full outage and a restart storm. If readiness pointed at /healthz, the pods would stay in the Service and answer users with errors instead of being withheld. The startup probe (line 67) gives booting pods time before either probe runs.

### Q3. What does the CPU-based HPA measure while the model generates?

It measures CPU usage divided by the CPU request. While the API waits on the LLM it is I/O-bound, so CPU stays low even when users are queueing: docs/evidence/cluster-state.txt shows cpu: 8%/70% with 3 replicas. CPU is therefore a poor signal for this service. Better signals are in-flight requests per pod or request latency. To use one we would expose an in-flight gauge on /metrics (the API already serves request count and latency there) and feed it to the HPA through a custom-metrics adapter or KEDA.

### Q4. Indexer memory limit

The indexer Job limit in k8s/base/indexer-job.yaml line 43 is: memory: 512Mi. So far only the dummy indexer (3 chunks, a few seconds) has run on the cluster, so kubectl top has no meaningful peak for OCR on the real corpus yet; this must be re-measured with the real pipeline before the limit is justified. If the limit is too low the container is OOMKilled with exit code 137. We saw exactly that on chroma-0 (limit 1Gi, Reason OOMKilled, exit code 137), which is why its limit is now 2Gi.

### Q6. EMBED\_MODEL changed in the ConfigMap and only the API restarted

The API compares its EMBED\_MODEL with the model recorded in the collection metadata (api/app/routes/health.py around line 86). On a mismatch it returns 503 from /readyz with the expected and found model names and logs "embedding model mismatch". We ran this on the cluster (docs/evidence/pin-mismatch.txt): the new pod sat at 0/1 READY, and because maxUnavailable is 0 (line 14 of k8s/base/api-deployment.yaml) the old pods kept serving. Without the guardrail the API would have embedded questions with one model against vectors from another, and retrieval would have returned plausible-looking nonsense with no error anywhere.

### Q7. The failure that cost more than an hour (Member 1)

Symptom: after adding the NetworkPolicies, the test "ui must not reach chroma" timed out, which looked like success. What I wrongly believed: that the timeout proved the policy worked. The "api must still reach chroma" test also timed out, which showed the policies were blocking everything. The command that told the truth was kubectl get networkpolicy -n rahbar: its POD-SELECTOR column said app=api, while kubectl get pods --show-labels showed the pods are labelled app.kubernetes.io/name=api. The policies selected no pods. After changing the selectors, the api reached chroma (heartbeat reply) and the ui still timed out. Saved in docs/evidence/networkpolicy.txt. A second problem in the same step was an invalid CIDR ("echo 192.168.49.1/32") caused by shell quoting in a heredoc.

### Q7. The failure that cost me more than an hour (Member 2)

**Symptoms.** My indexer and API run in WSL Ubuntu, but Ollama runs on Windows. From Ubuntu, `curl http://<windows-address>:11434/api/tags` printed nothing at all: no JSON and no error message. I lost about 2 hours on this.

**What I wrongly believed first.** I first believed the address was the problem. I had copied `172.28.80.1` from an example and tested it, but it was not my address. My own `ip route show default` printed `default via 172.17.0.1`. Then I believed the setting `OLLAMA_HOST=0.0.0.0` (set with `setx`) was enough to make Ollama reachable from Ubuntu.

**What the truth was.** There were two causes. (1) The address was wrong: the real Windows host address seen from WSL was `172.17.0.1`. (2) I had quit Ollama while changing the setting and had not started it again, so nothing was listening on port 11434. The command that told me the truth was `netstat -ano | findstr 11434`, which printed nothing. After I started `ollama serve`, its log line `Listening on [::]:11434` and `netstat` showing `0.0.0.0:11434 LISTENING` confirmed it. Then `curl http://172.17.0.1:11434/api/tags` finally printed `{"models":[...` with both models.

**Where it lives in my code.** The address is the `OLLAMA_URL` setting in `api/app/config.py` (line 41); I set it to `http://172.17.0.1:11434` for my local runs. The same class of problem exists on the cluster, where the host address is `host.minikube.internal`.

**What I learned.** When a connection prints nothing, first check that something is listening (`netstat`) before changing addresses. The host address can also change after a reboot, so it must come from configuration and not be written into the code.

