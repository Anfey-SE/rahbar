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
- The ENGINEERING-NOTES.md answers — based on what we observed running the system.
- The FAILURE-ANALYSIS.md entries — from actually breaking and diagnosing each stage.

## Policy acknowledgement

We understand that AI-generated code that we cannot explain is treated as academic misconduct. Every line in this repository was read and understood by at least one team member. The disclosures above are specific and accurate.
