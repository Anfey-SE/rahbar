# RAHBAR Kubernetes Deployment Guide

> Assignment 03 — Part F | CS4032 RAGOps
> All commands run against **minikube**. Namespace: `rahbar`.

---

## Prerequisites

```bash
# Start minikube (CNI required for NetworkPolicy)
minikube start --driver=docker --cni=calico --memory=6144 --cpus=4

# Build images into minikube's registry
eval $(minikube docker-env)          # Linux/macOS
# minikube docker-env | Invoke-Expression   # PowerShell

docker build -t rahbar-api:dev -f api/Dockerfile .
docker build -t rahbar-indexer:dev -f indexer/Dockerfile .
docker build -t rahbar-ui:dev  -f ui/Dockerfile .
```

---

## 1. First-time Deployment (from scratch)

```bash
# Apply the full base stack (namespace, ConfigMap, PVC, Deployments, Services, StatefulSet)
kubectl apply -k k8s/base/

# Wait for the API to be ready
kubectl -n rahbar rollout status deployment/api

# Verify all pods are running
kubectl -n rahbar get pods
```

---

## 2. F1 — Shared Data Volume

A single PVC `rahbar-data` (2 Gi, `ReadWriteOnce`) is mounted by all workloads:

| Workload | Mount path | subPath | Purpose |
|----------|-----------|---------|---------|
| `api`, `api-candidate` | `/data/traces` | `traces` | Per-pod trace JSONL files |
| `ingest` Job / CronJob | `/data` | *(whole volume)* | Inbox, quarantine, archive, manifests |
| `eval-job` | `/data` | *(whole volume)* | Writes results to `/data/results` |

```bash
kubectl -n rahbar get pvc rahbar-data
```

---

## 3. F2 — Ingestion Job and CronJob

### One-shot ingestion (re-index now)
```bash
kubectl -n rahbar apply -f k8s/base/ingestion-job.yaml
kubectl -n rahbar wait --for=condition=complete job/ingest --timeout=300s
kubectl -n rahbar logs job/ingest
```

### Scheduled ingestion (every Sunday 02:00 UTC)
```bash
kubectl -n rahbar get cronjob ingest-cron
```

### Eval Job + Release Gate (F2 / C2)
```bash
kubectl -n rahbar apply -f k8s/base/eval-job.yaml
kubectl -n rahbar wait --for=condition=complete job/eval-job --timeout=600s
kubectl -n rahbar logs job/eval-job   # shows gate PASS/FAIL output
```

#### Change gate thresholds without rebuilding the image
```bash
kubectl -n rahbar edit configmap gate-thresholds
# Edit faithfulness_min, p95_latency_max_ms, recall5_max_drop, abstention_min
# Re-apply the eval job to re-run the gate:
kubectl -n rahbar delete job eval-job
kubectl -n rahbar apply -f k8s/base/eval-job.yaml
```

---

## 4. F3 — Candidate Deployment and Rolling Update

### Deploy the candidate
```bash
kubectl -n rahbar apply -f k8s/base/api-candidate-deployment.yaml
kubectl -n rahbar apply -f k8s/base/api-candidate-service.yaml
kubectl -n rahbar rollout status deployment/api-candidate
```

The candidate runs against its **own collection** (`rahbar-candidate`) and its **own Service** (`api-candidate:8000`).

### Gate PASS → rolling update live Deployment
```bash
# If eval-job exits 0 (gate pass), promote candidate image to live:
kubectl -n rahbar set image deployment/api api=rahbar-api:<candidate-sha>
kubectl -n rahbar rollout status deployment/api
```

### Gate FAIL → stop candidate, do NOT roll out
```bash
kubectl -n rahbar scale deployment/api-candidate --replicas=0
# Live deployment is untouched
```

### F3 Rollback demo
```bash
# Trigger a rollback of the live deployment to previous version
kubectl -n rahbar rollout undo deployment/api

# Verify rollback: check the ReplicaSet history
kubectl -n rahbar rollout history deployment/api

# Confirm the collection version changed in a trace
kubectl -n rahbar logs -l app.kubernetes.io/name=api --tail=5 | grep collection_version
```

---

## 5. F4 — Trace Observability on Cluster

Each API pod writes traces to its own file via the downward API:
`TRACE_LOG_PATH=/data/traces/trace-$(POD_NAME).jsonl`

```bash
# Traces written to stdout (picked up by kubectl logs)
kubectl -n rahbar logs -l app.kubernetes.io/name=api --tail=20 | python -m json.tool

# List trace files on the PVC
kubectl -n rahbar exec deployment/api -- ls /data/traces/

# Use the trace tool to reconstruct a specific query
kubectl -n rahbar exec deployment/api -- \
    python -m api.app.traces.tool <trace_id>

# Metrics summary (last 1 hour)
kubectl -n rahbar exec deployment/api -- \
    python -m api.app.traces.summary --window 1h
```

---

## 6. F5 — Resource Tuning

| Workload | CPU request | CPU limit | Memory request | Memory limit |
|----------|-------------|-----------|----------------|--------------|
| `api` | 100m | 500m | 256Mi | 512Mi |
| `api-candidate` | 100m | 500m | 256Mi | 512Mi |
| `chroma` | 200m | 1000m | 512Mi | 1Gi |
| `redis` | 50m | 200m | 128Mi | 256Mi |
| `ingest` Job | 200m | 1000m | 512Mi | 1Gi |
| `eval-job` | 200m | 1000m | 512Mi | 1Gi |

```bash
# Live resource usage
kubectl -n rahbar top pods
kubectl -n rahbar top nodes
```

---

## Validation

```bash
# Dry-run kustomize to verify all manifests render correctly
kubectl kustomize k8s/overlays/dev
```

---

## Teardown

```bash
kubectl delete namespace rahbar
minikube stop
```
