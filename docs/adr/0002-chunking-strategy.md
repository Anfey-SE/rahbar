# ADR 0002 — Structure-Aware Chunking: 600-Word Chunks, 100-Word Overlap

**Date:** 2026-11-08  
**Status:** Accepted  
**Deciders:** Member 2 (RAG & Retrieval Pipeline)

---

## Context

Academic policy handbooks and university regulations are dense hierarchical legal documents with numbered clauses (e.g., `1.21`, `5.4`), sub-clauses, and tables. Naive fixed-size chunking frequently splits clauses mid-sentence, separates conditional requirements from their governing headers, and truncates tabular credit fee structures.

The assignment rubric requires empirically tuning chunking across at least three `(size, overlap)` configurations, measuring `recall@5` on the 15-question golden set, and justifying the configuration shipped to production.

---

## Decision

We implemented structure-aware chunking in `indexer/pipeline/chunk.py`:
1. **Preserve Structural Boundaries:** Splits are attempted first along major heading lines (`#`, ALL-CAPS titles), then along numbered clauses (`1.21-1.25`), and finally on paragraph/sentence boundaries.
2. **Clause Range Tracking:** Each chunk records its span of clause numbers in its `section` metadata (e.g., `5.1-5.7 Academic Warning/Probation`).
3. **Table Integrity:** Markdown tables are preserved as whole units whenever they fall within the word limit.
4. **Empirical Measurement:** We tested three configurations in words on the live Chroma vector store using `--retrieval-only` mode:

| Configuration (Size, Overlap) | Stored Chunks | Indexing Time | Recall@3 | Recall@5 | Recall@5 by Type |
| :---: | :---: | :---: | :---: | :---: | :--- |
| **(200, 30)** | 857 | 128.4s | 0.6923 | 0.6923 | Ordinary: 0.750, Version: 0.500, Exact: 0.500, Inj: 1.000 |
| **(400, 50)** | 744 | 114.2s | 0.6923 | 0.7692 | Ordinary: 0.875, Version: 0.500, Exact: 0.500, Inj: 1.000 |
| **(600, 100)** *(Selected)* | **722** | **110.8s** | **0.6923** | **0.8462** | **Ordinary: 0.875, Version: 1.000, Exact: 0.500, Inj: 1.000** |

We selected and shipped **`CHUNK_SIZE=600`** and **`CHUNK_OVERLAP=100`**.

---

## Justification & Consequences

- **Highest Recall@5:** `(600, 100)` reached `0.8462`, significantly exceeding `(400, 50)` (0.7692) and `(200, 30)` (0.6923).
- **Version Discrimination:** At 600 words, chunks retain enough surrounding policy context to clearly differentiate between the 2023 and 2026 undergraduate rules, doubling version recall from `0.500` to `1.000`.
- **Multi-Clause Integrity:** Complex university rules (such as academic probation and warning spanning clauses 5.1 through 5.7) remain unified in a single passage rather than being fragmented.
- **Controlled Storage Amplification:** The 100-word overlap produced 722 total chunks with an amplification ratio of ~1.20× (281,580 chunked words vs. 235,000 original corpus words), well within memory and storage constraints.
- **Dynamic Reconfiguration:** Both `CHUNK_SIZE` and `CHUNK_OVERLAP` are exposed as environment variables read from the Kubernetes ConfigMap, enabling future re-indexing without image rebuilds.
