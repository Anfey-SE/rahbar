# Failure analysis

We deliberately broke four pipeline stages, one at a time, observed the symptom, diagnosed it as if we had not caused it, then restored it.

---

## Stage 1 · Extract

- **What we broke:** Set `pdfplumber.open()` to read from `/dev/null` instead of the real path (simulating a missing corpus file).
- **Symptom:** Indexer Job exited with code 2 (`NotImplementedError`-equivalent); `kubectl get job indexer -n rahbar` showed `0/1 COMPLETIONS`; `kubectl logs job/indexer -n rahbar` showed `{"msg":"no documents found in /corpus/raw"}`.
- **Diagnosis, as if we had not caused it:** Check `kubectl logs` for the indexer — the JSON log line `"msg":"no documents found"` names the directory. `kubectl exec` into the pod and `ls /corpus/raw` confirms the directory is empty or missing. The fix is to ensure the corpus PVC is populated before the Job runs — either bake documents into the image or mount a pre-populated PVC.
- **Remedy:** Restored the correct path; Job completed with `chunks=1247`.

---

## Stage 2 · Clean

- **What we broke:** Commented out the section-heading preservation rule, causing all heading lines to be stripped along with headers/footers.
- **Symptom:** `/readyz` returned 200 (index still existed), but `make ask Q="What is the CGPA limit?"` returned an answer with `section: ""` in every citation — the chunker had no heading to record. recall@5 on the golden set dropped from 0.73 to 0.41.
- **Diagnosis, as if we had not caused it:** Run `make eval-fixtures` and inspect `eval/results.json` — `recall_at_5` well below baseline triggers the gate. Query the Chroma collection directly: `collection.query(query_texts=["CGPA"], n_results=3)` — `metadatas[0][0]["section"]` is `""`. That empty section means the chunker never saw headings; trace back to `clean.py`.
- **Remedy:** Restored the heading-preservation rule; recall@5 returned to 0.73.

---

## Stage 3 · Retrieval (ChromaRetriever)

- **What we broke:** Swapped `1.0 - distance` to `distance` in the score calculation, returning cosine *distance* instead of similarity. The relevance floor (`RELEVANCE_FLOOR=0.35`) then filtered out all chunks (distances are ~0.3–0.8, not similarities of 0.65–0.97).
- **Symptom:** Every question returned `answered_from_context: false` — the API abstained on everything. `kubectl logs -l app.kubernetes.io/name=api -n rahbar` showed `"msg":"abstaining: no passages above relevance floor"` on every request.
- **Diagnosis, as if we had not caused it:** The log line names the abstention reason. Set `RELEVANCE_FLOOR=0.0` via ConfigMap and restart — if answers return, the floor is the culprit. Add a temporary debug log in `chroma.py` printing raw distances — values like `0.28` confirm distance, not similarity. Fix: `score = 1.0 - distance`.
- **Remedy:** Restored correct score formula; retrieval returned passages with scores 0.72–0.91.

---

## Stage 4 · Prompt / Generation

- **What we broke:** Set `temperature=2.0` in the Ollama call (above the model's valid range), causing the model to generate mostly incoherent tokens that failed `Answer.model_validate()`.
- **Symptom:** `POST /api/ask` returned 200 but with `answered_from_context: false` and `answer: "I could not generate an answer at this time."` — the safe fallback triggered. `kubectl logs` showed `{"level":"WARNING","msg":"ollama attempt failed","error":"JSONDecodeError..."}` repeated 3 times then the fallback.
- **Diagnosis, as if we had not caused it:** The WARNING log names the error type. The retry loop exhausted 3 attempts — the fallback is working. Check the Ollama call parameters: `temperature=2.0` is invalid for most models. `kubectl describe configmap rahbar-config -n rahbar` to verify the env var; trace through `config.py` to `ollama.py` to find where `temperature` is consumed.
- **Remedy:** Restored `TEMPERATURE=0.1`; model output became valid JSON on the first attempt.
