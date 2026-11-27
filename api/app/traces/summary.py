"""D3: Metrics summary script.

Usage:
    python -m api.app.traces.summary --window 1h [--log traces/traces.jsonl]
    python -m api.app.traces.summary --window 24h

Outputs a JSON summary containing:
  - top-1 retrieval similarity distribution (mean, p50, p95, p99)
  - abstention rate
  - cache hit rate
  - p50 and p95 latency per stage
  - number of injection attempts flagged
  - total queries in window
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path
from statistics import mean

DEFAULT_LOG = os.environ.get("TRACE_LOG_PATH", "/tmp/traces/traces.jsonl")

WINDOW_SECONDS = {
    "1h": 3600,
    "6h": 6 * 3600,
    "24h": 24 * 3600,
    "7d": 7 * 24 * 3600,
    "all": None,
}


def _percentile(data: list[float], pct: float) -> float:
    if not data:
        return 0.0
    s = sorted(data)
    idx = int(len(s) * pct / 100)
    return s[min(idx, len(s) - 1)]


def _parse_window(window: str) -> int | None:
    if window in WINDOW_SECONDS:
        return WINDOW_SECONDS[window]
    raise ValueError(f"Unknown window: {window}. Use one of {list(WINDOW_SECONDS)}")


def summarise(log_path: Path, window_seconds: int | None) -> dict:
    if not log_path.exists():
        return {"error": f"{log_path} not found"}

    cutoff = None
    if window_seconds is not None:
        cutoff = time.time() - window_seconds

    records: list[dict] = []
    with log_path.open(encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue
            # filter by window
            if cutoff is not None:
                try:
                    ts_str = rec.get("timestamp", "")
                    import datetime
                    ts = datetime.datetime.strptime(ts_str, "%Y-%m-%dT%H:%M:%SZ").timestamp()
                    if ts < cutoff:
                        continue
                except Exception:
                    pass  # include if timestamp unparseable
            records.append(rec)

    if not records:
        return {"total_queries": 0, "window": str(window_seconds)}

    total = len(records)
    cache_hits = sum(1 for r in records if r.get("cache_hit"))
    abstentions = sum(1 for r in records if not r.get("answered_from_context"))
    injection_flags = sum(
        sum(1 for g in r.get("guardrail_actions", []) if g.get("triggered") and "injection" in g.get("action", "").lower())
        for r in records
    )

    # Top-1 similarity scores
    top1_scores: list[float] = []
    for r in records:
        retrieved = r.get("retrieved", [])
        if retrieved:
            top1_scores.append(retrieved[0].get("score", 0.0))

    # Stage latencies
    stage_keys = ["cache_lookup_ms", "retrieval_ms", "rerank_ms", "generation_ms", "total_ms"]
    latencies: dict[str, list[float]] = {k: [] for k in stage_keys}
    for r in records:
        lat = r.get("stage_latencies", {})
        for k in stage_keys:
            v = lat.get(k)
            if v is not None:
                latencies[k].append(float(v))

    def lat_stats(vals: list[float]) -> dict:
        if not vals:
            return {}
        return {
            "mean_ms": round(mean(vals), 2),
            "p50_ms": round(_percentile(vals, 50), 2),
            "p95_ms": round(_percentile(vals, 95), 2),
        }

    return {
        "window_seconds": window_seconds,
        "total_queries": total,
        "cache_hit_rate": round(cache_hits / total, 4) if total else 0,
        "abstention_rate": round(abstentions / total, 4) if total else 0,
        "injection_attempts_flagged": injection_flags,
        "top1_similarity": {
            "mean": round(mean(top1_scores), 4) if top1_scores else None,
            "p50": round(_percentile(top1_scores, 50), 4) if top1_scores else None,
            "p95": round(_percentile(top1_scores, 95), 4) if top1_scores else None,
        },
        "stage_latencies": {k: lat_stats(latencies[k]) for k in stage_keys},
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Summarise trace log over a time window.")
    parser.add_argument("--window", default="1h", help="Time window: 1h, 6h, 24h, 7d, all")
    parser.add_argument("--log", default=DEFAULT_LOG)
    parser.add_argument("--json", action="store_true", help="Output raw JSON only")
    args = parser.parse_args()

    try:
        window_secs = _parse_window(args.window)
    except ValueError as e:
        print(str(e), file=sys.stderr)
        sys.exit(1)

    result = summarise(Path(args.log), window_secs)

    if args.json:
        print(json.dumps(result, indent=2))
        return

    # Human-readable
    print(f"\n{'=' * 60}")
    print(f"  RAHBAR Trace Summary  |  window: {args.window}")
    print(f"{'=' * 60}")
    print(f"  Total queries          : {result.get('total_queries')}")
    print(f"  Cache hit rate         : {result.get('cache_hit_rate', 0):.1%}")
    print(f"  Abstention rate        : {result.get('abstention_rate', 0):.1%}")
    print(f"  Injection flags        : {result.get('injection_attempts_flagged')}")
    sim = result.get("top1_similarity", {})
    print(f"  Top-1 similarity mean  : {sim.get('mean')}")
    print(f"  Top-1 similarity p50   : {sim.get('p50')}")
    print(f"  Top-1 similarity p95   : {sim.get('p95')}")
    print()
    print("  Stage Latencies (p95):")
    for stage, stats in result.get("stage_latencies", {}).items():
        if stats:
            print(f"    {stage:<22}: p95={stats.get('p95_ms')} ms  p50={stats.get('p50_ms')} ms")
    print(f"{'=' * 60}\n")


if __name__ == "__main__":
    main()
