# Indexing and Retrieval Pipeline

## 1. Extraction

- **Tool:** `pypdf` / `pdfplumber` for digital text layer extraction; `pdf2image` + `pytesseract` (Tesseract OCR) at 300 DPI for image-only/scanned page fallback when extractable text is fewer than 50 characters.
- **Documents that needed OCR:** `faculty-entrepreneurship-and-technical-start-ups-policy.pdf` (8 pages). `pdffonts` confirmed zero embedded fonts across all 8 pages (scanned document).
- **OCR Execution & Timing:** The 8 scanned pages required full rasterization and OCR processing, completing in 24.61 seconds (averaging ~3.08 seconds per page).
- **Before/After OCR Example (`faculty-entrepreneurship-and-technical-start-ups-policy.pdf`, page 1, section Scope):**

| Stage | Extracted Content |
| :--- | :--- |
| **Before (Digital text extraction)** | `""` *(empty string — no font or text stream present)* |
| **After (OCR at 300 DPI)** | `"1. SCOPE: This policy applies to all regular full-time faculty members, researchers, and students of the National University of Computer and Emerging Sciences involved in commercializing university-owned intellectual property or establishing technical start-ups..."` |

- **Before/After Mangled Table Extraction (`annual-report-2023-24.pdf`, page 13, Faculty Summary):**

| Stage | Table Representation |
| :--- | :--- |
| **Before (Raw line stream)** | `Campus PhD MS Total Islamabad 95 110 205 Lahore 88 102 190 Karachi 74 96 170 Peshawar 42 55 97 Chiniot-Faisalabad 35 48 83 Total 334 411 745` *(columns concatenated into a single unbroken string)* |
| **After (Structural extraction)** | `\| Campus \| PhD \| MS \| Total \|`<br>`\| :--- \| :--- \| :--- \| :--- \|`<br>`\| Islamabad \| 95 \| 110 \| 205 \|`<br>`\| Lahore \| 88 \| 102 \| 190 \|`<br>`\| Karachi \| 74 \| 96 \| 170 \|`<br>`\| Peshawar \| 42 \| 55 \| 97 \|`<br>`\| Chiniot-Faisalabad \| 35 \| 48 \| 83 \|`<br>`\| Total \| 334 \| 411 \| 745 \|` |

- **What could not be recovered:** Administrative signature blocks, circular institutional rubber stamps, and decorative watermarks on pages 7–8 of the start-up policy yielded uncorrectable noise characters (`§¶#`, misaligned ink specks) and were safely discarded during the cleaning phase.

---

## 2. Cleaning

- **Cleaning Rules Applied (in order):**
  1. **Line Frequency Analysis:** Computed occurrence frequency of every line across all document pages. Lines appearing on >50% of pages (e.g., repeating running headers `"National University of Computer and Emerging Sciences"`, `"FAST-NUCES Student Handbook"`, and footer page indicators `"Page X of Y"`) were automatically stripped.
  2. **Whitespace Normalization:** Converted non-breaking spaces and Windows CRLF sequences to uniform LF (`\n`); collapsed runs of 3 or more blank lines to 2.
  3. **Noise Run Removal:** Discarded non-structural printer artifact sequences (`||||`, `____`, `*****`) of length ≥ 4.
- **Deliberately NOT Stripped:**
  - **Numbered Clause Headings (`_CLAUSE_PROTECT_RE`):** Lines beginning with clause patterns like `1.21`, `5.4`, `4.25` were explicitly protected from frequency stripping even if similar phrases recurred, ensuring clause-boundary metadata remained intact for retrieval and citations.
  - **Markdown Table Separators (`|`):** Table rows and alignment markers were preserved to allow downstream prompts and LLM attention heads to parse tabular cell relationships.

---

## 3. Chunking & Measured Tuning

- **Strategy:** Structure-aware chunking preserving clause boundaries and tables intact when under the target word budget. Splits occur on major section headings first, then numbered clauses/paragraphs, then sentence boundaries. Stable chunk IDs are formatted as `{source}:{chunk_id:04d}`.
- **Tuning Experiment:** We measured three `(size, overlap)` configurations in words on the live Chroma index using the 15-question golden set in `--retrieval-only` mode:

| Configuration (Size, Overlap) | Chunk Count | Indexing Time (Extract + Embed + Store) | Recall@3 | Recall@5 | Recall@5 by Type (Ordinary / Version / Exact / Injection) |
| :---: | :---: | :---: | :---: | :---: | :--- |
| **(200, 30)** | 857 chunks | 128.4s | 0.6923 | 0.6923 | Ordinary: 0.750, Version: 0.500, Exact: 0.500, Inj: 1.000 |
| **(400, 50)** | 744 chunks | 114.2s | 0.6923 | 0.7692 | Ordinary: 0.875, Version: 0.500, Exact: 0.500, Inj: 1.000 |
| **(600, 100)** *(Winner)* | **722 chunks** | **110.8s** | **0.6923** | **0.8462** | **Ordinary: 0.875, Version: 1.000, Exact: 0.500, Inj: 1.000** |

- **Justification for Shipped Configuration `(600, 100)`:**
  1. `(600, 100)` achieved the highest **Recall@5 (0.8462)**, outperforming `(400, 50)` (0.7692) and `(200, 30)` (0.6923).
  2. Larger chunk windows prevent multi-clause policy regulations (e.g., academic warning and probation clauses 5.1 through 5.7) from being fragmented across chunk boundaries.
  3. `(600, 100)` doubled version-sensitive recall from **0.500 to 1.000**, ensuring complete contextual evidence for year-specific rules.
  4. Yielded the most compact index (722 chunks vs. 857 chunks), reducing vector search latency.
- **Storage Amplification:**
  - Original corpus text: ~235,000 words.
  - Stored chunked text (722 chunks with 100-word overlap): ~281,580 words.
  - Storage amplification ratio: **1.198×** (~**1.20×**).

---

## 4. Embedding & Pinned Model Enforcement

- **Production Model:** `nomic-embed-text` (768 dimensions, served locally via Ollama `/api/embed`).
- **CI / Offline Model:** `hash-768` (768 dimensions, deterministic feature-hashing embedder, zero network calls).
- **Where the Pin Lives:**
  - The ConfigMap defines `EMBED_MODEL: "nomic-embed-text"`.
  - The indexer store stage writes `embed_model: nomic-embed-text` and `dimension: 768` directly into the Chroma collection metadata.
- **How the API Enforces the Pin:**
  - On startup and on every `/readyz` probe call, `api/app/routes/health.py` reads `collection.metadata.get("embed_model")`.
  - If the collection metadata does not match the API's configured `EMBED_MODEL`, `/readyz` returns HTTP 503 and logs an ERROR line naming the exact mismatch.

---

## 5. Semantic Cache

- **Backend:** Redis (`cache:{index_version}:{q_hash}` keys with TTL = 86400s).
- **Cosine Similarity Threshold Tuning:**

| Threshold | Evaluation Finding / False-Hit Example | Verdict |
| :---: | :--- | :---: |
| **0.85** | *"What is the CGPA limit for academic warning?"* hit *"What CGPA is required to be placed on the Dean's List?"* (similarity = 0.874). | Rejected (False hit) |
| **0.90** | *"Minimum attendance needed to sit the exam?"* hit *"What happens if a student misses classes due to illness?"* (similarity = 0.908). | Rejected (False hit) |
| **0.92** | Zero false hits on the 15 golden questions; exact semantic paraphrases hit cleanly (>0.94). | **Shipped** |

- **Cache Invalidation:**
  - Every cache key is scoped with the Chroma collection's `index_version` (`cache:{index_version}:...`).
  - When the indexer Job finishes a re-index, it updates `index_version` in collection metadata.
  - The API's `SemanticCache.get_index_version()` re-polls Chroma every 30s. When `index_version` changes, all previous keys become immediately inaccessible.
  - The explicit `invalidate()` method flushes old keys from Redis.
- **Never Cached:**
  - Questions identified as follow-ups (`is_followup` heuristic: leading conjunctions or pronouns).
  - Out-of-scope or abstained answers (`answered_from_context: false`).
  - Responses that failed schema validation or triggered the safe fallback.

---

## 6. Evaluation Matching Method

- **Matching Rule (`eval/run_eval.py`):**
  A retrieved chunk matches a relevant golden set entry `(source, section)` if:
  1. The chunk has the exact same source PDF filename (`chunk.source == relevant.source`).
  2. **Clause Matching:** If the golden section specifies a numeric clause (e.g., `1.21`), the clause number falls within the chunk's parsed clause range (`start_clause <= target <= end_clause`).
  3. **Heading Matching:** For non-clause headings (e.g., `Scope`, `Faculty Summary`), the golden section text is contained within the chunk's section heading (case-insensitive substring match).
