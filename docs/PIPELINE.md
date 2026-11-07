# Indexing and retrieval pipeline

## Extraction

**Tool:** `pdfplumber` for text layer extraction; `pytesseract` + `Pillow` for OCR fallback.

**Documents that needed OCR:** `examination-regulations-2024.pdf` pages 4–9 had no embedded fonts (`pdffonts` confirmed zero fonts). Pytesseract at 200 DPI recovered the text.

**Before/after OCR (page 5, section 4.1):**

| Stage | Content |
|-------|---------|
| Before (pdfplumber) | *(empty string — no text layer)* |
| After (pytesseract) | "4.1 A student who is absent from more than 20% of the scheduled classes shall not be allowed to appear in the final examination." |

**What could not be recovered:** Figures and signature blocks in the scanned pages returned noise characters and were stripped by the cleaner.

## Cleaning

Rules applied (in order):

1. Strip repeating headers/footers matching `Page N of M`, bare page numbers, and the domain `www.nu.edu.pk` on every page.
2. Collapse 3+ consecutive blank lines to 2.
3. Strip OCR noise runs (`|||`, `---`, `___` of length ≥ 4) that are not horizontal rules.

**Deliberately NOT stripped:** Section headings (lines starting with `##` or ALL-CAPS of ≥ 5 chars) — the chunker and citation system depend on them. Table separators are also preserved so `pdfplumber` table extraction can still parse them in a second pass.

## Chunking

**Strategy:** Split on section headings first, then paragraphs, then sentences. Slide a window of `size` tokens (words) with `overlap` token step-back between neighbours. Each chunk records the section heading it falls under.

**Shipped configuration:** size=300 tokens, overlap=50 tokens.

**Three measured configurations:**

| Size (tokens) | Overlap (tokens) | recall@5 | Chunks stored |
| --- | --- | --- | --- |
| 200 | 30 | 0.61 | 1 842 |
| **300** | **50** | **0.73** | **1 247** |
| 400 | 80 | 0.70 | 968 |

300/50 gave the best recall@5 without excessive chunk count. The 400/80 setting showed lower recall because long chunks diluted section-specific content.

**Storage amplification:** 1 247 chunks × 300 tokens = 374 100 stored tokens; original corpus ≈ 314 000 tokens → amplification ratio **1.19×**.

## Embedding

**Model:** `nomic-embed-text` (768-dimensional, served by Ollama on `host.minikube.internal:11434`).

**Where the pin lives:** `collection.metadata["embed_model"]` written by `store.py` at index-creation time.

**How the API checks it:** On startup (`lifespan`) and on every `/readyz` call, `api/app/routes/health.py` reads `collection.metadata["embed_model"]` and compares it to `EMBED_MODEL` from the ConfigMap. A mismatch returns HTTP 503 and logs one JSON ERROR line. See `docs/evidence/pin-mismatch.txt` for a live demonstration.

## Semantic cache

**Backend:** Redis (`cache:vec:gen{N}:*` keys, packed float32 vectors; `cache:ans:gen{N}:*` keys, JSON responses). TTL 3600 s.

**Thresholds tried:**

| Threshold | False-hit example | Decision |
|-----------|-------------------|----------|
| 0.85 | "What is the CGPA limit?" hit "What is the GPA requirement?" | Rejected |
| 0.90 | "Minimum attendance?" hit "Max absences allowed?" | Rejected |
| **0.92** | No false hits on 15-question golden set | **Shipped** |

**Invalidation:** Incrementing `cache:gen` in Redis effectively orphans all old keys. The indexer CronJob calls `cache.invalidate()` after a successful re-index.

**Never cached:** Abstentions (`answered_from_context=false`) and follow-up questions (detected by `is_followup` heuristic — question starts with "and", "also", "what about", "follow").

## Evaluation method

`recall_at_k` matches by `(source, section)` pairs. A retrieved chunk counts as a hit if its `source` filename and `section` heading exactly match a `relevant` entry in the golden set. This is strict enough to catch wrong-section retrieval but tolerant of chunk-ID changes after re-indexing.

Questions with `must_abstain: true` have empty `relevant` lists — `recall_at_k` returns `None` for them and they are excluded from the recall average.
