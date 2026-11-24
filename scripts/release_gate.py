#!/usr/bin/env python3
"""C2: Release-gate script.

Reads gate thresholds from a YAML ConfigMap-mounted file and eval results
from a JSON file.  Exits 0 (PASS) or 1 (FAIL).

Usage:
    python scripts/release_gate.py [--config /etc/gate/thresholds.yaml] \
                                   [--results eval/results/latest.json]

Changing the ConfigMap thresholds.yaml (without rebuilding the image) changes
the gate outcome — that is the entire point.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

try:
    import yaml  # type: ignore[import-untyped]
except ImportError:
    yaml = None  # type: ignore[assignment]


def _load_yaml(path: Path) -> dict:
    text = path.read_text(encoding="utf-8")
    if yaml is not None:
        return yaml.safe_load(text)
    # minimal fallback parser for flat key: value YAML
    result: dict[str, str] = {}
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if ":" in line:
            k, _, v = line.partition(":")
            result[k.strip()] = v.strip()
    return result


def _cast(val):
    """Cast string values from minimal YAML parser to float/int."""
    if isinstance(val, (int, float)):
        return val
    try:
        return float(val)
    except (ValueError, TypeError):
        return val


def main() -> int:
    parser = argparse.ArgumentParser(description="RAGOps release gate")
    default_config = os.environ.get("GATE_CONFIG_PATH", "/etc/gate/thresholds.yaml")
    parser.add_argument("--config", default=default_config, help="Path to thresholds YAML")
    parser.add_argument(
        "--results", default="eval/results/latest.json", help="Path to eval results JSON"
    )
    parser.add_argument(
        "--baseline",
        default="eval/results/baseline.json",
        help="Path to baseline results JSON (optional)",
    )
    args = parser.parse_args()

    config_path = Path(args.config)
    results_path = Path(args.results)
    baseline_path = Path(args.baseline)

    # ---- Load thresholds ----
    if not config_path.exists():
        print(f"[GATE] ERROR: thresholds file not found: {config_path}", flush=True)
        print("[GATE] FAIL: cannot run gate without thresholds", flush=True)
        return 1

    raw = _load_yaml(config_path)
    thresholds = {k: _cast(v) for k, v in raw.items()}
    print(f"[GATE] Thresholds loaded from {config_path}:", flush=True)
    for k, v in thresholds.items():
        print(f"  {k}: {v}", flush=True)

    # ---- Load candidate results ----
    if not results_path.exists():
        print(f"[GATE] ERROR: results file not found: {results_path}", flush=True)
        print("[GATE] FAIL: no eval results to check", flush=True)
        return 1

    results = json.loads(results_path.read_text(encoding="utf-8"))
    print(f"\n[GATE] Candidate results from {results_path}:", flush=True)

    # ---- Load baseline (optional) ----
    baseline: dict = {}
    if baseline_path.exists():
        baseline = json.loads(baseline_path.read_text(encoding="utf-8"))
        print(f"[GATE] Baseline loaded from {baseline_path}", flush=True)
    else:
        print(
            f"[GATE] No baseline found at {baseline_path} — skipping relative checks",
            flush=True,
        )

    failures: list[str] = []

    # --- Check 1: faithfulness >= faithfulness_min ---
    if "faithfulness_min" in thresholds:
        required = float(thresholds["faithfulness_min"])
        actual = float(results.get("faithfulness", results.get("faithfulness_score", -1)))
        status = "PASS" if actual >= required else "FAIL"
        print(f"  faithfulness: {actual:.4f} >= {required} -> {status}", flush=True)
        if actual < required:
            failures.append(f"faithfulness {actual:.4f} < required {required}")

    # --- Check 2: p95 latency <= p95_latency_max_ms ---
    if "p95_latency_max_ms" in thresholds:
        required = float(thresholds["p95_latency_max_ms"])
        actual = float(results.get("p95_latency_ms", results.get("latency_p95_ms", -1)))
        status = "PASS" if actual <= required else "FAIL"
        print(f"  p95_latency_ms: {actual:.1f} <= {required} -> {status}", flush=True)
        if actual > required:
            failures.append(f"p95_latency_ms {actual:.1f} > max {required}")

    # --- Check 3: recall@5 drop <= recall5_max_drop (requires baseline) ---
    if "recall5_max_drop" in thresholds and baseline:
        max_drop = float(thresholds["recall5_max_drop"])
        candidate_recall = float(results.get("recall_at_5", results.get("recall5", -1)))
        baseline_recall = float(baseline.get("recall_at_5", baseline.get("recall5", -1)))
        if candidate_recall >= 0 and baseline_recall >= 0:
            drop = baseline_recall - candidate_recall
            status = "PASS" if drop <= max_drop else "FAIL"
            print(f"  recall@5 drop: {drop:.4f} <= {max_drop} -> {status}", flush=True)
            if drop > max_drop:
                failures.append(f"recall@5 dropped by {drop:.4f} > max allowed {max_drop}")
        else:
            print("  recall@5: missing from results or baseline -- skipping", flush=True)

    # --- Check 4: abstention correctness >= abstention_min ---
    if "abstention_min" in thresholds:
        required = float(thresholds["abstention_min"])
        actual = float(
            results.get("abstention_correctness", results.get("abstention_accuracy", required))
        )
        status = "PASS" if actual >= required else "FAIL"
        print(f"  abstention_correctness: {actual:.4f} >= {required} -> {status}", flush=True)
        if actual < required:
            failures.append(f"abstention_correctness {actual:.4f} < required {required}")

    print("", flush=True)
    if failures:
        print("[GATE] *** FAIL -- candidate did NOT pass the release gate ***", flush=True)
        for f in failures:
            print(f"  x {f}", flush=True)
        return 1

    print("[GATE] *** PASS -- candidate cleared all thresholds ***", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
