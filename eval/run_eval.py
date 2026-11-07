#!/usr/bin/env python3
"""Score a running RAHBAR against a golden set, and gate CI on the result (section 5).

    python eval/run_eval.py --api http://<ingress>                 # your corpus
    python eval/run_eval.py --api ... --golden eval/fixtures/golden.fixture.yaml
    python eval/run_eval.py --api ... --update-baseline            # after a deliberate improvement

Exit code 0 when every gate passes, 1 when any fails. CI relies on that exit code.

Given: talking to the API, reading the golden set, writing results.json, the exit
code. Yours: the four evaluation functions implemented below.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
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


def recall_at_k(relevant: list[dict], retrieved: list[dict], k: int) -> float | None:
    """Fraction of the relevant (source, section) pairs found in the top k retrieved.
    Return None when the question has no relevant passages (must-abstain questions).

    Match by (source, section) pair — see docs/PIPELINE.md §Evaluation method.
    """
    if not relevant:
        return None
    top_k = retrieved[:k]
    retrieved_pairs = {(r["source"], r["section"]) for r in top_k}
    hits = sum(1 for r in relevant if (r["source"], r["section"]) in retrieved_pairs)
    return hits / len(relevant)


def citation_validity(response: dict) -> float | None:
    """Fraction of the response's citations that match a passage in response['retrieved'].
    Return None when there are no citations."""
    citations = response.get("citations", [])
    if not citations:
        return None
    retrieved_pairs = {
        (r["source"], r["section"]) for r in response.get("retrieved", [])
    }
    valid = sum(1 for c in citations if (c["source"], c["section"]) in retrieved_pairs)
    return valid / len(citations)


def summarise(rows: list[dict]) -> dict:
    """Overall recall@3, recall@5, citation validity and abstention correctness, plus
    recall@5 broken down by question type. This dict becomes results.json."""
    recall3_scores: list[float] = []
    recall5_scores: list[float] = []
    citation_scores: list[float] = []
    abstention_correct: list[float] = []
    by_type: dict[str, list[float]] = {}

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

        if must_abstain:
            abstained = not response.get("answered_from_context", True)
            abstention_correct.append(1.0 if abstained else 0.0)

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


def main() -> int:
    parser = argparse.ArgumentParser(description="Evaluate RAHBAR against a golden set.")
    parser.add_argument("--api", required=True, help="base URL, e.g. http://192.168.49.2")
    parser.add_argument("--host", help="Host header, when your Ingress rule names a host")
    parser.add_argument("--golden", type=Path, default=HERE / "golden.yaml")
    parser.add_argument("--baseline", type=Path, default=HERE / "baseline.json")
    parser.add_argument("--out", type=Path, default=HERE / "results.json")
    parser.add_argument("--update-baseline", action="store_true",
                        help="write these results as the new baseline")
    args = parser.parse_args()

    questions = load_golden(args.golden)
    rows = []
    headers = {"Host": args.host} if args.host else {}
    with httpx.Client(timeout=120.0, headers=headers) as client:
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
    args.out.write_text(json.dumps(results, indent=2), encoding="utf-8")
    print(f"\nresults written to {args.out}")
    if args.update_baseline:
        args.baseline.write_text(json.dumps(results, indent=2), encoding="utf-8")
        print(f"baseline updated: {args.baseline}")

    if failures:
        print("\nGATE FAILED")
        for message in failures:
            print(f"  - {message}")
        return 1
    print("\ngate passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
