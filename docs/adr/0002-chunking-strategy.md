# ADR 0002 — Structure-aware chunking: 300-token chunks, 50-token overlap

**Date:** 2026-11-02
**Status:** Accepted
**Deciders:** Member 2 (RAG)

## Context

Fixed-size chunking breaks mid-sentence and discards section boundaries that citations depend on. The assignment requires measuring three (size, overlap) configurations and justifying the choice.

## Decision

Split on section headings first, then paragraphs, then sentences. Slide a window of **300 tokens** (words) with **50 tokens** of overlap between neighbours. Each chunk records the section heading it falls under.

Three measured configurations on the fixture corpus (see `docs/PIPELINE.md`):

| Size | Overlap | recall@5 |
|------|---------|---------|
| 200 | 30 | 0.61 |
| **300** | **50** | **0.73** |
| 400 | 80 | 0.70 |

300/50 gave best recall@5. The 400/80 setting showed lower recall because long chunks diluted section-specific content — a broader match, but less precise.

## Consequences

- **Good:** Citations include meaningful section headings; overlap preserves cross-boundary context; recall@5 is 0.73.
- **Bad:** Variable chunk count per document; storage amplification ~1.19× (374 100 stored vs 314 000 original tokens).
- ConfigMap keys `CHUNK_SIZE=300` and `CHUNK_OVERLAP=50` let operators retune without rebuilding the image.
