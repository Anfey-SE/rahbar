# AI Usage Disclosure

This project used AI assistance in the following specific ways. All AI-generated content was reviewed, understood, and adapted by the team before use. No AI tool had access to exam or assessment content.

| Tool | What we used it for | Sections affected |
|------|--------------------|--------------------|
| Google Antigravity (Claude/Gemini) | Generating boilerplate for Kubernetes manifests (HPA, NetworkPolicy, kustomization); reviewing YAML syntax | k8s/base/, k8s/overlays/ |
| Google Antigravity | Generating the initial structure of CI/CD workflow files; adapting to our registry and secrets model | .github/workflows/ |
| Google Antigravity | Drafting the defensive `_int()` helper in `config.py` to handle K8s service-link URL injection | api/app/config.py |
| Google Antigravity | Helping diagnose the `enableServiceLinks` crash (CrashLoopBackOff with `ValueError: invalid literal for int`) | api/app/config.py, k8s/base/*-deployment.yaml |

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

## Member 2 (RAG and Indexing Pipeline)

I used two AI tools. This section was drafted with Claude from the record of my own work and edited by me.

### Tools and what each one did

**Claude (Anthropic chat, claude.ai)**
- Proposed the list of six corpus documents (`corpus/sources.yaml`) and the first draft of `docs/CORPUS.md`. I downloaded the files, checked the SHA-256 hashes with `fetch.py --verify`, counted the pages, and confirmed which two files have no extractable text.
- Wrote the `--out DIR` / `CORPUS_DIR` change in `corpus/fetch.py`, which I tested and merged (PR #3).
- Drafted the question skeleton of `eval/golden.yaml`. I checked every clause number and expected answer against the 2026 handbook and the 2023 rules myself, and changed q05 and q12.
- Gave me step-by-step PowerShell, git, WSL and Ollama instructions, wrote my pull request descriptions, wrote the instruction prompt I gave to Antigravity, and helped me review Antigravity's reports.

**Antigravity (Google's agent IDE; models Claude Opus 4.6 (Thinking) and Gemini 3.8 Flash)**
- Wrote and tested the five indexer stages (`indexer/pipeline/extract.py`, `clean.py`, `chunk.py`, `embed.py`, `store.py`) and `indexer/tests/test_pipeline_stages.py`.
- Wrote the retriever (`api/app/retrieval/chroma.py`), the providers (`ollama.py`, `hosted.py`), the answer pipeline (`api/app/services/answer.py`), the semantic cache (`cache.py`) and their mocked tests.
- Wrote the evaluation functions in `eval/run_eval.py`, the retrieval-only mode and `eval/test_eval.py`, and ran the chunk tuning and the full eval on my laptop.

### Code that was on dev before my work
Commit `4bca046` on dev contained drafts of files in my area (the indexer stages, `cache.py`, `answer.py`, `chroma.py`, `ollama.py`, `run_eval.py`, `golden.yaml`, `PIPELINE.md`, `FAILURE-ANALYSIS.md`). I did not write them; they came from a tool in Member 1's working folder. I had Antigravity audit them against my real PDFs. The audit found real faults, for example 107 of 121 Student Handbook chunks were tagged with the university header as their section, and the cache needed Redis access from the indexer, which the network policy blocks. We then rewrote or fixed those files, and I removed all numbers that came from no real run.

### What I checked and changed myself
- Every question in the golden set against the real PDFs, and that the two version questions (q10, q11) really have different answers in the 2023 and 2026 versions.
- Antigravity's first verification table: I found wrong labels and a placeholder section ("Heading on page 6") that would have made q06 fail, and it fixed both. I also found that the Prospectus chunks had a list letter as a section name, and it was fixed.
- I asked why `ollama.py` and `hosted.py` did not appear in the coverage report. The reason was Member 1's `api/.coveragerc` exclusion, and the real coverage of both is 100% with my mocked tests.
- The thresholds (relevance floor 0.64, cache threshold 0.92) were measured by Antigravity on my index, following my instructions. I reviewed the numbers behind them.
- All merges and pull requests.

### Where I did not use AI
The reading of the PDFs to check clauses, the decisions about what to merge, and my answer to Q7.

Antigravity (Google DeepMind) helped draft indexer pipeline stages (OCR fallback, chunking, cleaning regex), ChromaRetriever, the semantic cache, and the offline feature-hashing embedder for CI.
What I changed after the AI output: Adjusted the relevance floor to 0.60 based on real score gap measurements (0.5887 vs 0.6584), fixed citation document title mapping in answer.py, and tuned chunking from measurements (600, 100) instead of guesses.
