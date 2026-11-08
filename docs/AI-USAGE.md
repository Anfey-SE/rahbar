# AI Usage Disclosure

This project used AI assistance in the following specific ways. All AI-generated content was reviewed, understood, and adapted by the team before use. No AI tool had access to exam or assessment content.

| Tool | What we used it for | Sections affected |
|------|--------------------|--------------------|
| Google Antigravity (Claude/Gemini) | Generating boilerplate for Kubernetes manifests (HPA, NetworkPolicy, kustomization); reviewing YAML syntax | k8s/base/, k8s/overlays/ |
| Google Antigravity | Generating the initial structure of CI/CD workflow files; adapting to our registry and secrets model | .github/workflows/ |
| Google Antigravity | Drafting the defensive `_int()` helper in `config.py` to handle K8s service-link URL injection | api/app/config.py |
| Google Antigravity | Helping diagnose the `enableServiceLinks` crash (CrashLoopBackOff with `ValueError: invalid literal for int`) | api/app/config.py, k8s/base/*-deployment.yaml |
| GitHub Copilot | Autocomplete for repetitive Pydantic model fields in `schemas.py` | api/app/schemas.py |

## What was NOT AI-generated

- The RAG design decisions (chunking strategy, relevance floor threshold, cache threshold) — these came from measuring recall@5 on three configurations.
- The corpus selection — we manually evaluated each document for the seven required properties.
- The golden question set — written before any tuning, from reading the actual documents.
- The FAILURE-ANALYSIS.md entries — from actually breaking and diagnosing each stage.

## Policy acknowledgement

We understand that AI-generated code that we cannot explain is treated as academic misconduct. Every line in this repository was read and understood by at least one team member. The disclosures above are specific and accurate.

## Member 1 (platform and DevOps)

- Antigravity wrote first drafts of the Dockerfiles, manifests, /readyz and the CI/CD workflows (see the table above). Claude (Anthropic, chat) helped me debug CI failures (lint, coverage, Trivy, pypdf) and drafted the docs in this section. I ran every command, read every output, and changed the NetworkPolicy selectors, chroma memory limit, pypdf version and Trivy install myself after real failures.

- What I changed after the AI output: NetworkPolicy selectors (wrong label), the chroma memory limit after an OOMKill, the pypdf version after the scan, and the Trivy install method.
- Incident: an AI tool wrote evidence files (branch protection, blocked merge, HPA, kubectl top, endpoints) and eval results (baseline.json, results.json) that did not come from real runs, and committed them under my name in commit 4bca046. I found them and deleted them in commit 1acb855. The evidence now in docs/evidence/ was captured by me from the real cluster. The fake files remain in Git history.
