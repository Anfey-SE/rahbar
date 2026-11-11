# ADR 0004 — Ollama on the host as the LLM provider; Groq as hosted fallback

**Date:** 2026-11-03
**Status:** Accepted
**Deciders:** Member 1 (Platform), Member 2 (RAG)

## Context

Running the LLM inside the minikube cluster is impractical on a single-node lab machine (insufficient GPU VRAM). The lab machine has a discrete GPU that Ollama can use on the host OS. We also need a provider that works in CI without any GPU or real model.

## Decision

Three providers, selected by `LLM_PROVIDER` in the ConfigMap:

| Value | Implementation | Use case |
|-------|---------------|----------|
| `stub` | `StubProvider` (deterministic) | CI, unit tests — no model, no network |
| `ollama-host` | `OllamaProvider` → `host.minikube.internal:11434` | Development and grading on the lab machine |
| `hosted` | `HostedProvider` → Groq OpenAI-compatible API | Fallback when Ollama is unavailable |

**Default:** `stub` (safe for CI). Operators change to `ollama-host` by editing the ConfigMap and restarting the API Deployment.

**PII policy:** With `ollama-host`, question text stays on the local host. With `hosted`, question text is sent to Groq's API. Users are warned in `docs/AI-USAGE.md`. The key comes from a Kubernetes Secret (`make secret`) and never touches a file in the repository.

## Consequences

- **Good:** CI never calls a real model; swapping providers is one ConfigMap change; Groq free tier is sufficient for demos.
- **Bad:** `ollama-host` requires the host to run `OLLAMA_HOST=0.0.0.0 ollama serve`; latency is higher than in-cluster (~200–400 ms vs ~50 ms).
- **Ollama model pinning:** `LLM_MODEL=llama3.2:3b` in the ConfigMap. The model must be pulled on the host before the API can serve requests: `ollama pull llama3.2:3b`.
