# Failure Analysis: Systematic Pipeline Breakages

To validate the observability and robustness of RAHBAR, we deliberately broke four of the six stages described in Section 5.5 of the assignment specification, observed the symptoms across cluster logs and evaluation metrics, diagnosed each as if encountering a production incident, and applied the appropriate remedy.

---

## Stage A · Extraction

- **How it was broken:** Disabled the OCR fallback in `indexer/pipeline/extract.py` by replacing the `pdf2image` + `tesseract` execution path with a simple fallback to empty string when text layer characters were < 50.
- **Symptom:**
  - Indexing completed without raising an exception, but total chunks indexed from `faculty-entrepreneurship-and-technical-start-ups-policy.pdf` dropped from 9 chunks to **1 empty/stub chunk**.
  - During evaluation, question `q08` (*"Does the start-up policy apply to visiting or adjunct faculty members?"*) failed completely: top-1 similarity score dropped to **0.412** (well below `RELEVANCE_FLOOR = 0.60`), triggering an automatic abstention (`answered_from_context: false`).
  - Overall Recall@5 dropped by **7.7 percentage points** (from 0.8462 to 0.7692).
- **Diagnosis (as if not caused on purpose):**
  1. Inspect the eval run: `eval/results.json` shows `q08` failing with `recall_at_5: 0.0` and abstaining.
  2. Query Chroma directly for chunks from `faculty-entrepreneurship-and-technical-start-ups-policy.pdf`:
     ```python
     res = col.get(where={"source": "faculty-entrepreneurship-and-technical-start-ups-policy.pdf"})
     print(len(res["ids"]), [len(t) for t in res["documents"]])
     ```
     Result: Only 1 document returned with text length 0.
  3. Inspect the PDF using `pdffonts corpus/raw/faculty-entrepreneurship-and-technical-start-ups-policy.pdf`:
     The font list is completely empty, proving the PDF is an image-only scan.
  4. Metric / Log Line: Indexer JSON log showed `{"msg":"page text below threshold","page":1,"len":0,"action":"ocr_skipped"}`.
- **Stage Identified:** Stage A (Extraction).
- **Remedy:** Re-enabled the Tesseract OCR fallback at 300 DPI for pages with < 50 characters. Chunks for the startup policy restored to 9; `q08` top-1 score returned to `0.8317`; Recall@5 restored to `0.8462`.

---

## Stage B · Chunking

- **How it was broken:** Replaced structure-aware clause chunking with a naive fixed-size chunker of **100 words with 0 overlap** in `indexer/pipeline/chunk.py`.
- **Symptom:**
  - Total chunks ballooned from 722 to **2,180 chunks**.
  - Numbered clause regulations (e.g., academic warning conditions spanning clauses 5.1 to 5.7) were sliced mid-sentence across multiple chunks.
  - Golden set Recall@5 plummeted from **0.8462 down to 0.5385** (a drop of over 30 percentage points).
  - Exact-term recall dropped to **0.00**, as clause numbers became separated from their respective rule definitions.
- **Diagnosis (as if not caused on purpose):**
  1. Running the CI eval gate (`python eval/run_eval.py`) immediately failed with exit code 1:
     `AssertionError: recall@5 (0.5385) dropped more than 5 percentage points below baseline (0.8462)`.
  2. Inspecting the failed queries (`q01`, `q10`, `q11`) revealed that the retrieved chunks contained fragments like `"shall be placed on probation if..."` without the preceding context defining who the rule applied to or what CGPA threshold was required.
  3. Metric / Log Line: Prometheus metric `retrieval_chunks_total` jumped 3×, while `eval/results.json` showed `exact_term_recall` at `0.0`.
- **Stage Identified:** Stage B (Chunking).
- **Remedy:** Restored structure-aware chunking with `CHUNK_SIZE=600` and `CHUNK_OVERLAP=100`, preserving clause boundaries (`5.1-5.7`) and table structures intact.

---

## Stage C · Retrieval & Embedding Pin

- **How it was broken:** Modified the API's ConfigMap to set `EMBED_MODEL="all-minilm-l6-v2"` while keeping the Chroma collection indexed with `nomic-embed-text` (simulating an accidental configuration drift during deployment).
- **Symptom:**
  - The API pod refused to enter `READY` status: `kubectl get pods -n rahbar` showed `0/1 READY` with `STATUS: Running`.
  - The Kubernetes readiness probe failed repeatedly: `GET /readyz` returned HTTP **503 Service Unavailable**.
  - All user queries through the Service were withheld, preventing silent garbage responses.
- **Diagnosis (as if not caused on purpose):**
  1. Inspect pod events:
     `kubectl describe pod -l app.kubernetes.io/name=api -n rahbar`
     Showed: `Warning Unhealthy Readiness probe failed: HTTP probe failed with statuscode: 503`.
  2. Inspect API logs:
     `kubectl logs -l app.kubernetes.io/name=api -n rahbar`
     Contained the explicit guardrail error line:
     `{"level":"ERROR","msg":"embedding model mismatch","configured":"all-minilm-l6-v2","collection":"nomic-embed-text","action":"refusing_traffic"}`.
  3. Metric / Log Line: `/readyz` response body:
     `{"status":"not_ready","checks":{"chroma":"ok","collection":"ok","count":722,"embed_model":"mismatch: expected all-minilm-l6-v2, found nomic-embed-text"}}`.
- **Stage Identified:** Stage C (Retrieval / Guardrail Configuration).
- **Remedy:** Corrected the ConfigMap to restore `EMBED_MODEL: "nomic-embed-text"` and restarted the API pod. The pod passed `/readyz` within 2 seconds and reached `1/1 READY`.

---

## Stage E · Source & Policy Versioning

- **How it was broken:** Removed the `superseded: false` metadata filter in `api/app/retrieval/chroma.py`, allowing the retriever to return repealed policies from `undergraduate-academic-rules-aug-2023.pdf` alongside current 2026 rules.
- **Symptom:**
  - On version-sensitive question `q12` (*"Under 2026 rules, can a student improve a 'D' grade after graduation?"*), the retriever fetched chunk `undergraduate-academic-rules-aug-2023.pdf:0031` at rank 1 with similarity score `0.841`.
  - The LLM generated an answer quoting the superseded 2023 policy instead of the active 2026 rule, citing the wrong document year.
  - The CI eval gate failed on version-sensitive accuracy.
- **Diagnosis (as if not caused on purpose):**
  1. Inspecting the citations on `POST /api/ask` for `q12`:
     The returned citation listed `source: "undergraduate-academic-rules-aug-2023.pdf"`.
  2. Inspecting the API request log:
     `{"level":"INFO","msg":"query executed","source_filter":null,"retrieved_sources":["undergraduate-academic-rules-aug-2023.pdf"]}`.
     The presence of the 2023 document in response to a 2026 query immediately pointed to a missing or bypassed `superseded` filter.
  3. Metric / Log Line: Evaluation metric `recall_at_5_by_type["version"]` dropped from **1.00 down to 0.50**.
- **Stage Identified:** Stage E (Source / Metadata Filtering).
- **Remedy:** Reinstated the default Chroma query filter:
  ```python
  where_filter = {"superseded": False} if not request.include_superseded else None
  ```
  The 2023 chunks were excluded, chunk `academic-rules-handbook-jun-2026.pdf:0052` was retrieved at rank 1, and version recall returned to **1.00**.
