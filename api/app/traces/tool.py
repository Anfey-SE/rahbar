"""D2: Trace tool.

Usage:
    python -m api.app.traces.tool <trace_id> [--log traces/traces.jsonl]

Finds the trace record for the given trace_id and reconstructs the exact
context (retrieved passages) and prompt that produced that answer.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

DEFAULT_LOG = os.environ.get("TRACE_LOG_PATH", "/tmp/traces/traces.jsonl")


def find_trace(trace_id: str, log_path: Path) -> dict | None:
    if not log_path.exists():
        return None
    with log_path.open(encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                record = json.loads(line)
            except json.JSONDecodeError:
                continue
            if record.get("trace_id") == trace_id:
                return record
    return None


def render_trace(record: dict) -> str:
    lines: list[str] = []
    lines.append("=" * 72)
    lines.append(f"TRACE ID     : {record.get('trace_id')}")
    lines.append(f"REQUEST ID   : {record.get('request_id')}")
    lines.append(f"TIMESTAMP    : {record.get('timestamp')}")
    lines.append(f"CACHE HIT    : {record.get('cache_hit')}")
    lines.append("")

    lines.append("── QUERY ─────────────────────────────────────────────────────")
    lines.append(f"  User query   : {record.get('query')}")
    if record.get("rewritten_query"):
        lines.append(f"  Rewritten    : {record.get('rewritten_query')}")
    lines.append("")

    lines.append("── EMBEDDING & COLLECTION ────────────────────────────────────")
    lines.append(f"  Embed model  : {record.get('embed_model')}  (version {record.get('embed_version')})")
    lines.append(f"  Collection   : {record.get('collection')}  (version {record.get('collection_version')})")
    lines.append(f"  Filters      : {json.dumps(record.get('filters', {}))}")
    lines.append("")

    lines.append("── RETRIEVED CHUNKS (context sent to LLM) ────────────────────")
    for i, r in enumerate(record.get("retrieved", []), 1):
        rank = r.get("reranked_position") or i
        lines.append(
            f"  [{rank}] chunk_id={r.get('chunk_id')}  doc={r.get('doc_id')}  "
            f"ver={r.get('doc_version')}  score={r.get('score'):.4f}"
        )
    lines.append("")

    lines.append("── PROMPT ────────────────────────────────────────────────────")
    lines.append(f"  Prompt hash  : {record.get('prompt_hash')}")
    prompt = record.get("full_prompt", "")
    if prompt:
        lines.append("  Full prompt  :")
        for pline in prompt.splitlines():
            lines.append("    " + pline)
    lines.append("")

    lines.append("── GENERATION ────────────────────────────────────────────────")
    lines.append(f"  Model        : {record.get('model')}")
    lines.append(f"  Params       : {json.dumps(record.get('model_params', {}))}")
    lines.append(f"  Confidence   : {record.get('confidence')}")
    lines.append(f"  Answered     : {record.get('answered_from_context')}")
    if record.get("abstention_reason"):
        lines.append(f"  Abstention   : {record.get('abstention_reason')}")
    lines.append("")
    lines.append("  Response:")
    for rline in (record.get("response") or "").splitlines():
        lines.append("    " + rline)
    lines.append("")

    lines.append("── CITATIONS ─────────────────────────────────────────────────")
    for c in record.get("citations", []):
        lines.append(f"  {c}")
    lines.append("")

    lines.append("── GUARDRAIL ACTIONS ─────────────────────────────────────────")
    guards = record.get("guardrail_actions", [])
    if guards:
        for g in guards:
            lines.append(f"  action={g.get('action')}  triggered={g.get('triggered')}  {g.get('detail', '')}")
    else:
        lines.append("  (none)")
    lines.append("")

    lines.append("── STAGE LATENCIES ───────────────────────────────────────────")
    lat = record.get("stage_latencies", {})
    for k, v in lat.items():
        lines.append(f"  {k:<25}: {v} ms")
    lines.append("=" * 72)
    return "\n".join(lines)


def main() -> None:
    parser = argparse.ArgumentParser(description="Reconstruct a query trace from its trace_id.")
    parser.add_argument("trace_id", help="The trace_id to look up.")
    parser.add_argument(
        "--log",
        default=DEFAULT_LOG,
        help=f"Path to the traces.jsonl file (default: {DEFAULT_LOG}).",
    )
    args = parser.parse_args()
    record = find_trace(args.trace_id, Path(args.log))
    if record is None:
        print(f"trace_id '{args.trace_id}' not found in {args.log}", file=sys.stderr)
        sys.exit(1)
    print(render_trace(record))


if __name__ == "__main__":
    main()
