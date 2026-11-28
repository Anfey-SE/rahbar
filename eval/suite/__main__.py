"""CLI entry point: python -m eval.suite."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from .runner import run_suite


def main() -> int:
    parser = argparse.ArgumentParser(description="RAHBAR RAGOps evaluation suite.")
    parser.add_argument("--golden", required=True, help="Path to eval/golden.jsonl")
    parser.add_argument("--api", required=True, help="Base URL of running RAHBAR API")
    parser.add_argument("--collection", required=True, help="Expected Chroma collection name")
    parser.add_argument("--out", required=True, help="Output directory for JSON/MD results")
    parser.add_argument("--quick", action="store_true", help="Run fixed 6-question quick subset")
    parser.add_argument("--seed", type=int, default=42, help="Random seed (default: 42)")
    args = parser.parse_args()

    golden_path = Path(args.golden)
    if not golden_path.is_file():
        print(f"ERROR: golden file not found: {golden_path}", file=sys.stderr)
        return 1

    try:
        result, json_path, md_path = run_suite(
            golden_path=golden_path,
            api_url=args.api,
            collection_name=args.collection,
            out_dir=Path(args.out),
            quick=args.quick,
            seed=args.seed,
        )
    except Exception as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1

    comp = result["levels"]["component"]
    print(f"collection={result['collection']}")
    print(f"quick={result['quick']} n_questions={result['n_questions']}")
    print(f"evaluated={comp['n_evaluated']} skipped={len(result['skipped'])}")
    print(
        f"recall@3={comp['recall_at_3']:.4f} "
        f"recall@5={comp['recall_at_5']:.4f} "
        f"mrr={comp['mrr']:.4f} "
        f"ndcg@5={comp['ndcg_at_5']:.4f}"
    )
    print(f"result_json={json_path}")
    print(f"result_md={md_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
