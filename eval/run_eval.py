#!/usr/bin/env python3
"""Score a running RAHBAR against a golden set, and gate CI on the result (section 5).

    python eval/run_eval.py --api http://<ingress>                 # your corpus
    python eval/run_eval.py --api ... --golden eval/fixtures/golden.fixture.yaml
    python eval/run_eval.py --api ... --update-baseline            # after a deliberate improvement

    # Retrieval-only mode (no LLM, fast for chunk tuning):
    python eval/run_eval.py --retrieval-only --golden eval/golden.yaml \
        --ollama-url http://172.17.0.1:11434 \
        --chroma-host localhost --chroma-port 8000 --collection rahbar

Exit code 0 when every gate passes, 1 when any fails. CI relies on that exit code.

Given: talking to the API, reading the golden set, writing results.json, the exit
code. Yours: the four evaluation functions implemented below.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import time
import urllib.request
from pathlib import Path

import httpx
import yaml

HERE = Path(__file__).resolve().parent

# The gate from section 5.4. Change these only with a reason written in docs/PIPELINE.md.
MAX_RECALL5_DROP = 0.05        # recall@5 may fall at most 5 points below the baseline
MIN_CITATION_VALIDITY = 0.95   # at least 95% of citations must match retrieved passages
REQUIRED_ABSTENTION = 1.0      # every must-abstain question must be declined


def load_golden(path: Path) -> list[dict]:
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    questions = data.get("questions") or []
    if not questions:
        sys.exit(f"no questions in {path}")
    return questions


def ask(client: httpx.Client, api: str, question: str) -> dict:
    """POST one question and return the AskResponse as a dict (see api/app/schemas.py)."""
    response = client.post(f"{api.rstrip('/')}/api/ask", json={"question": question})
    response.raise_for_status()
    return response.json()


# ---------------------------------------------------------------------------
# Section matching helpers for recall_at_k
# ---------------------------------------------------------------------------
# Chunks store sections like "5.4-5.7 Academic Warning/Probation" or
# "Faculty Summary". The golden set uses bare clause numbers like "5.4"
# or heading text like "Faculty Summary".
#
# Matching rule (documented in PIPELINE.md):
#   Same source AND one of:
#     (a) The relevant section is a clause number (e.g. "5.4"). The chunk's
#         section starts with a clause range like "5.4-5.7" or a single
#         clause "5.4". The relevant clause must fall within the range
#         [start, end] using tuple comparison. Sub-clauses count:
#         relevant "5.4" matches chunk "5.4-5.7" because 5.4 is in range.
#     (b) The relevant section is heading text. It appears as a substring
#         of the chunk's section (case-insensitive), or vice versa.

_CLAUSE_RE = re.compile(r"^(\d+(?:\.\d+)*)$")
_RANGE_RE = re.compile(r"^(\d+(?:\.\d+)*)(?:-(\d+(?:\.\d+)*))?")


def _parse_clause(s: str) -> tuple[int, ...] | None:
    """Parse '5.4' into (5, 4). Returns None if not a clause number."""
    try:
        return tuple(int(x) for x in s.split("."))
    except ValueError:
        return None


def _section_matches(relevant_section: str, chunk_section: str) -> bool:
    """Check if a golden relevant section matches a chunk's section string.

    Returns True when the relevant section is found in the chunk section,
    using clause-range logic for numbered sections and substring matching
    for heading text.
    """
    rel = relevant_section.strip()
    chk = chunk_section.strip()

    # Exact match (case-insensitive)
    if rel.lower() == chk.lower():
        return True

    # (a) Clause number matching: relevant is a bare clause like "5.4"
    rel_clause_m = _CLAUSE_RE.match(rel)
    if rel_clause_m:
        rel_tuple = _parse_clause(rel_clause_m.group(1))
        # Try to parse chunk section as a clause range like "5.4-5.7 ..."
        chk_range_m = _RANGE_RE.match(chk)
        if rel_tuple and chk_range_m:
            start_tuple = _parse_clause(chk_range_m.group(1))
            end_str = chk_range_m.group(2)
            end_tuple = _parse_clause(end_str) if end_str else start_tuple
            if start_tuple and end_tuple:
                # Check major clause matches and relevant is within range
                if start_tuple[0] == rel_tuple[0] and start_tuple <= rel_tuple <= end_tuple:
                    return True

        # Also check if the clause number appears standalone in the chunk section
        # e.g. "2.3 Grade Points" contains clause "2.3"
        if re.search(r"(?:^|\s)" + re.escape(rel) + r"(?:\s|$|[^.\d])", chk):
            return True

        return False

    # (b) Heading text matching: substring, case-insensitive
    if len(rel) >= 3 and (rel.lower() in chk.lower() or chk.lower() in rel.lower()):
        return True

    return False


def recall_at_k(relevant: list[dict], retrieved: list[dict], k: int) -> float | None:
    """Fraction of the relevant (source, section) pairs found in the top k retrieved.
    Return None when the question has no relevant passages (must-abstain questions).

    Match by source (exact) and section (clause-range or substring).
    See docs/PIPELINE.md §Evaluation method.
    """
    if not relevant:
        return None
    top_k = retrieved[:k]
    hits = 0
    for rel in relevant:
        rel_src = rel["source"]
        rel_sec = rel["section"]
        for ret in top_k:
            if ret["source"] == rel_src and _section_matches(rel_sec, ret["section"]):
                hits += 1
                break  # count each relevant pair at most once
    return hits / len(relevant)


def citation_validity(response: dict) -> float | None:
    """Fraction of the response's citations that match a passage in response['retrieved'].
    Return None when there are no citations.

    Uses the same section matching rule as recall_at_k so that a citation
    citing section "5.4" is valid if a retrieved chunk has section "5.4-5.7 ...".
    """
    citations = response.get("citations", [])
    if not citations:
        return None
    retrieved = response.get("retrieved", [])
    valid = 0
    for c in citations:
        for r in retrieved:
            if c["source"] == r["source"] and _section_matches(c["section"], r["section"]):
                valid += 1
                break
    return valid / len(citations)


def summarise(rows: list[dict]) -> dict:
    """Overall recall@3, recall@5, citation validity and abstention correctness, plus
    recall@5 broken down by question type. This dict becomes results.json."""
    recall3_scores: list[float] = []
    recall5_scores: list[float] = []
    citation_scores: list[float] = []
    abstention_correct: list[float] = []
    by_type: dict[str, list[float]] = {}
    per_question: list[dict] = []

    for row in rows:
        relevant = row.get("relevant", [])
        response = row.get("response", {})
        retrieved = response.get("retrieved", [])
        qtype = row.get("type", "ordinary")
        must_abstain = row.get("must_abstain", False)

        r3 = recall_at_k(relevant, retrieved, 3)
        r5 = recall_at_k(relevant, retrieved, 5)
        cv = citation_validity(response)

        if r3 is not None:
            recall3_scores.append(r3)
        if r5 is not None:
            recall5_scores.append(r5)
            by_type.setdefault(qtype, []).append(r5)
        if cv is not None:
            citation_scores.append(cv)

        abstain_ok: bool | None = None
        if must_abstain:
            abstained = not response.get("answered_from_context", True)
            abstention_correct.append(1.0 if abstained else 0.0)
            abstain_ok = abstained

        per_question.append({
            "id": row.get("id", "?"),
            "type": qtype,
            "recall_at_3": r3,
            "recall_at_5": r5,
            "citation_validity": cv,
            "abstention_correct": abstain_ok,
        })

    def _avg(lst: list[float]) -> float | None:
        return round(sum(lst) / len(lst), 4) if lst else None

    return {
        "recall_at_3": _avg(recall3_scores),
        "recall_at_5": _avg(recall5_scores),
        "citation_validity": _avg(citation_scores),
        "abstention_correctness": _avg(abstention_correct),
        "recall_at_5_by_type": {
            t: round(sum(v) / len(v), 4) for t, v in by_type.items()
        },
        "n_questions": len(rows),
        "per_question": per_question,
    }


def gate(results: dict, baseline: dict | None) -> list[str]:
    """Return one message per failed gate (section 5.4); an empty list means pass."""
    failures: list[str] = []

    r5 = results.get("recall_at_5")
    cv = results.get("citation_validity")
    abstention = results.get("abstention_correctness")

    if baseline is not None:
        b_r5 = baseline.get("recall_at_5")
        if r5 is not None and b_r5 is not None:
            if b_r5 - r5 > MAX_RECALL5_DROP:
                failures.append(
                    f"recall@5 dropped from {b_r5:.4f} to {r5:.4f} "
                    f"(max allowed drop: {MAX_RECALL5_DROP})"
                )

    if cv is not None and cv < MIN_CITATION_VALIDITY:
        failures.append(
            f"citation_validity {cv:.4f} < required {MIN_CITATION_VALIDITY}"
        )

    if abstention is not None and abstention < REQUIRED_ABSTENTION:
        failures.append(
            f"abstention_correctness {abstention:.4f} < required {REQUIRED_ABSTENTION}"
        )

    return failures


# ---------------------------------------------------------------------------
# Retrieval-only helpers (no API server needed, queries Chroma directly)
# ---------------------------------------------------------------------------

def _embed_via_ollama(text: str, model: str, ollama_url: str) -> list[float]:
    """Embed a single text via Ollama /api/embed."""
    url = f"{ollama_url.rstrip('/')}/api/embed"
    payload = json.dumps({"model": model, "input": text}).encode("utf-8")
    req = urllib.request.Request(url, data=payload, headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=30) as resp:
        data = json.loads(resp.read().decode("utf-8"))
    return data["embeddings"][0]


def _retrieve_from_chroma(
    question: str,
    k: int,
    chroma_host: str,
    chroma_port: int,
    collection_name: str,
    embed_model: str,
    ollama_url: str,
) -> list[dict]:
    """Retrieve top-k chunks directly from Chroma (no API server needed)."""
    import chromadb  # type: ignore[import-untyped]
    from chromadb.config import Settings as ChromaSettings

    client = chromadb.HttpClient(
        host=chroma_host, port=chroma_port,
        settings=ChromaSettings(anonymized_telemetry=False),
    )
    col = client.get_collection(collection_name)
    vec = _embed_via_ollama(question, embed_model, ollama_url)

    # Filter out superseded docs unless question asks about older version
    older_pattern = re.compile(r"\b(2023|old|previous|earlier)\b", re.IGNORECASE)
    where_filter = None if older_pattern.search(question) else {"superseded": False}

    results = col.query(
        query_embeddings=[vec],  # type: ignore[arg-type]
        n_results=k,
        where=where_filter,  # type: ignore[arg-type]
        include=["documents", "metadatas", "distances"],
    )

    passages: list[dict] = []
    ids = results.get("ids", [[]])[0]
    docs = results.get("documents", [[]])[0]
    metas = results.get("metadatas", [[]])[0]
    dists = results.get("distances", [[]])[0]

    for doc_id, _text, meta, dist in zip(ids, docs, metas, dists, strict=False):
        m = dict(meta) if meta else {}
        page_val = m.get("page")
        parsed_page: int | None = None
        if page_val is not None:
            try:
                parsed_page = int(page_val)
            except (ValueError, TypeError):
                parsed_page = None
        score = round(1.0 - float(dist), 4)
        passages.append({
            "chunk_id": str(doc_id),
            "source": str(m.get("source", "")),
            "section": str(m.get("section", "")),
            "page": parsed_page,
            "score": score,
        })
    return passages


def main() -> int:
    parser = argparse.ArgumentParser(description="Evaluate RAHBAR against a golden set.")
    parser.add_argument("--api", help="base URL, e.g. http://192.168.49.2")
    parser.add_argument("--host", help="Host header, when your Ingress rule names a host")
    parser.add_argument("--golden", type=Path, default=HERE / "golden.yaml")
    parser.add_argument("--baseline", type=Path, default=HERE / "baseline.json")
    parser.add_argument("--out", type=Path, default=HERE / "results.json")
    parser.add_argument("--update-baseline", action="store_true",
                        help="write these results as the new baseline")
    # Retrieval-only mode: fast recall scoring without LLM
    parser.add_argument("--retrieval-only", action="store_true",
                        help="query Chroma directly, skip the LLM (fast for tuning)")
    parser.add_argument("--ollama-url", default="http://172.17.0.1:11434")
    parser.add_argument("--embed-model", default="nomic-embed-text")
    parser.add_argument("--chroma-host", default="localhost")
    parser.add_argument("--chroma-port", type=int, default=8000)
    parser.add_argument("--collection", default="rahbar")
    parser.add_argument("--top-k", type=int, default=5)
    args = parser.parse_args()

    if not args.retrieval_only and not args.api:
        parser.error("--api is required unless --retrieval-only is set")

    questions = load_golden(args.golden)
    rows: list[dict] = []

    if args.retrieval_only:
        # Retrieval-only: embed and query Chroma directly, no API/LLM needed
        print(f"Retrieval-only mode: collection={args.collection}, k={args.top_k}")
        for q in questions:
            started = time.perf_counter()
            must_abstain = bool(q.get("must_abstain", False))
            # For must-abstain questions, still retrieve to check nothing bad comes back
            retrieved = _retrieve_from_chroma(
                q["question"], args.top_k,
                args.chroma_host, args.chroma_port, args.collection,
                args.embed_model, args.ollama_url,
            )
            elapsed = round(time.perf_counter() - started, 2)
            # Build a minimal response dict for summarise()
            response = {
                "retrieved": retrieved,
                "citations": [],
                "answered_from_context": False,  # no LLM was called
            }
            rows.append({
                "id": q["id"], "type": q.get("type", "ordinary"),
                "must_abstain": must_abstain,
                "relevant": q.get("relevant") or [],
                "expect": q.get("expect"),
                "response": response,
                "seconds": elapsed,
            })
            # Show per-question recall immediately
            rel = q.get("relevant") or []
            r5 = recall_at_k(rel, retrieved, min(args.top_k, 5))
            r3 = recall_at_k(rel, retrieved, min(args.top_k, 3))
            top_score = retrieved[0]["score"] if retrieved else 0.0
            print(f"  {q['id']:<5} {elapsed:>6.2f}s  r@3={r3}  r@5={r5}  "
                  f"top={top_score:.4f}  {q['question'][:50]}")
    else:
        # Full mode: call the API (includes LLM generation)
        headers = {"Host": args.host} if args.host else {}
        with httpx.Client(timeout=600.0, headers=headers) as client:
            for q in questions:
                started = time.perf_counter()
                response = ask(client, args.api, q["question"])
                rows.append({
                    "id": q["id"], "type": q.get("type", "ordinary"),
                    "must_abstain": bool(q.get("must_abstain", False)),
                    "relevant": q.get("relevant") or [],
                    "expect": q.get("expect"),
                    "response": response,
                    "seconds": round(time.perf_counter() - started, 2),
                })
                print(f"  {q['id']:<5} {rows[-1]['seconds']:>6.2f}s  {q['question'][:60]}")

    try:
        results = summarise(rows)
        baseline = json.loads(args.baseline.read_text()) if args.baseline.exists() else None
        failures = gate(results, baseline)
    except NotImplementedError as exc:
        print(f"\nnot implemented yet: {exc}")
        return 2

    results["golden"] = str(args.golden)
    results["questions"] = len(rows)
    if args.retrieval_only:
        results["mode"] = "retrieval-only"
        results["collection"] = args.collection
    args.out.write_text(json.dumps(results, indent=2), encoding="utf-8")
    print(f"\nresults written to {args.out}")
    if args.update_baseline:
        args.baseline.write_text(json.dumps(results, indent=2), encoding="utf-8")
        print(f"baseline updated: {args.baseline}")

    # Print summary table
    print(f"\n{'='*60}")
    print(f"  recall@3:                {results['recall_at_3']}")
    print(f"  recall@5:                {results['recall_at_5']}")
    print(f"  citation_validity:       {results['citation_validity']}")
    print(f"  abstention_correctness:  {results['abstention_correctness']}")
    print("  recall@5 by type:")
    for t, v in results.get("recall_at_5_by_type", {}).items():
        print(f"    {t:20s} {v}")
    print(f"{'='*60}")

    if failures:
        print("\nGATE FAILED")
        for message in failures:
            print(f"  - {message}")
        return 1
    print("\ngate passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
