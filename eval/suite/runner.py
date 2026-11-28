"""Suite runner for component-level evaluation (Assignment 03, Part B)."""

from __future__ import annotations

import hashlib
import json
import math
import os
import random
import re
import time
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Protocol

import httpx

from .metrics import aggregate_component_metrics, score_question

# Fixed 6-question subset for --quick (3 answerable, 1 hard, 2 unanswerable).
QUICK_IDS: tuple[str, ...] = ("g01", "g04", "h01", "h06", "g26", "h07")

_OLDER_PATTERN = re.compile(r"\b(2023|old|previous|earlier)\b", re.IGNORECASE)
_TOKEN_RE = re.compile(r"\b\w+\b")
_BATCH_B_SOURCES = {
    "b10_academic_rules_v2.md",
    "b11_prompt_injection.md",
    "b12_uncovered_topic.md",
}


class ComponentRetriever(Protocol):
    """Protocol for component-level retrieval (real Chroma or offline test fake)."""

    def retrieve_metas(self, question: str, k: int = 5) -> list[dict[str, Any]]: ...

    def has_batch_b(self) -> bool: ...

    def collection_metadata(self) -> dict[str, Any]: ...


def _hash_embed(text: str, dim: int = 768) -> list[float]:
    """Deterministic feature-hashing embedder matching api/app/retrieval/hash_embed.py."""
    tokens = _TOKEN_RE.findall(text.lower())
    if not tokens:
        vec = [0.0] * dim
        vec[0] = 1.0
        return vec
    vec = [0.0] * dim
    for tok in tokens:
        h = hashlib.sha256(tok.encode("utf-8")).digest()
        idx = int.from_bytes(h[:4], "big") % dim
        sign = 1.0 if h[4] % 2 == 0 else -1.0
        vec[idx] += sign
    norm = math.sqrt(sum(x * x for x in vec))
    if norm == 0.0:
        vec[0] = 1.0
        return vec
    return [x / norm for x in vec]


class ChromaComponentRetriever:
    """Retriever using the same embedding + Chroma query + active filter as the API."""

    def __init__(
        self,
        collection_name: str,
        embed_model: str,
        embed_provider: str = "ollama",
        chroma_host: str = "localhost",
        chroma_port: int = 8000,
        ollama_url: str = "http://172.17.0.1:11434",
    ) -> None:
        import chromadb  # type: ignore[import-untyped]
        from chromadb.config import Settings as ChromaSettings

        self.collection_name = collection_name
        self.embed_model = embed_model
        self.embed_provider = embed_provider
        self.ollama_url = ollama_url
        self.client = chromadb.HttpClient(
            host=chroma_host,
            port=chroma_port,
            settings=ChromaSettings(anonymized_telemetry=False),
        )
        self.collection = self.client.get_collection(collection_name)

    def _embed(self, text: str) -> list[float]:
        if self.embed_provider == "hash" or self.embed_model == "hash-768":
            return _hash_embed(text)
        url = f"{self.ollama_url.rstrip('/')}/api/embed"
        payload = json.dumps({"model": self.embed_model, "input": text}).encode("utf-8")
        req = urllib.request.Request(
            url, data=payload, headers={"Content-Type": "application/json"}
        )
        with urllib.request.urlopen(req, timeout=30) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        embeddings = data.get("embeddings", [])
        if not embeddings or not embeddings[0]:
            raise ValueError("empty embedding returned from Ollama")
        return list(embeddings[0])

    def retrieve_metas(self, question: str, k: int = 5) -> list[dict[str, Any]]:
        vec = self._embed(question)
        where_filter: dict[str, Any] | None = (
            None
            if _OLDER_PATTERN.search(question)
            else {"$and": [{"status": "active"}, {"superseded": False}]}
        )
        res = self.collection.query(
            query_embeddings=[vec],  # type: ignore[arg-type]
            n_results=k,
            where=where_filter,  # type: ignore[arg-type]
            include=["metadatas"],
        )
        raw_metas = res.get("metadatas") or [[]]
        return [dict(m) for m in raw_metas[0] if m]

    def has_batch_b(self) -> bool:
        try:
            res = self.collection.get(
                where={"source": "b10_academic_rules_v2.md"},
                limit=1,
                include=[],
            )
            if res.get("ids"):
                return True
            res2 = self.collection.get(include=["metadatas"])
            sources = {str(m.get("source", "")) for m in (res2.get("metadatas") or []) if m}
            return bool(_BATCH_B_SOURCES & sources)
        except Exception:
            return False

    def collection_metadata(self) -> dict[str, Any]:
        return dict(self.collection.metadata or {})


def load_golden_jsonl(path: Path) -> list[dict[str, Any]]:
    """Load golden questions from JSONL."""
    questions: list[dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line:
            questions.append(json.loads(line))
    return questions


def verify_api_collection_and_config(
    api_url: str,
    expected_collection: str,
    http_client: httpx.Client | None = None,
) -> dict[str, Any]:
    """Verify /readyz matches expected_collection and fetch /info config."""
    base = api_url.rstrip("/")
    own_client = http_client is None
    client = http_client or httpx.Client(timeout=15.0)
    try:
        try:
            ready_resp = client.get(f"{base}/readyz")
        except Exception as exc:
            raise RuntimeError(f"API /readyz unreachable at {base}/readyz: {exc}") from exc

        if ready_resp.status_code != 200:
            raise RuntimeError(
                f"API /readyz returned HTTP {ready_resp.status_code}: {ready_resp.text}"
            )

        ready_data = ready_resp.json()
        reported = ready_data.get("collection")
        if not reported:
            checks_col = (ready_data.get("checks") or {}).get("collection")
            if checks_col and checks_col != "ok":
                reported = checks_col

        if reported != expected_collection:
            raise ValueError(
                f"Collection mismatch: API reports '{reported}', "
                f"but --collection is '{expected_collection}'"
            )

        config: dict[str, Any] = {
            "collection": expected_collection,
            "embed_model": (ready_data.get("checks") or {}).get("embed_model"),
        }

        try:
            info_resp = client.get(f"{base}/info")
            if info_resp.status_code == 200:
                info_data = info_resp.json()
                for k in (
                    "embed_model",
                    "collection",
                    "relevance_floor",
                    "top_k",
                    "temperature",
                    "llm_model",
                    "chunk_size",
                    "chunk_overlap",
                ):
                    if k in info_data and info_data[k] is not None:
                        config[k] = info_data[k]
        except Exception:
            pass

        return config
    finally:
        if own_client:
            client.close()


def _format_markdown_summary(result: dict[str, Any]) -> str:
    """Render a concise Markdown summary table for the suite result."""
    comp = result["levels"]["component"]
    cfg = result.get("config", {})
    lines = [
        f"# Evaluation Suite Summary ({result['timestamp']})",
        "",
        f"- **Collection:** `{result['collection']}`",
        f"- **Embed Model:** `{result['embed_model']}`",
        f"- **Seed:** `{result['seed']}` | **Quick:** `{result['quick']}` | **Questions:** `{result['n_questions']}`",
        f"- **Elapsed:** `{result.get('elapsed_seconds', 0.0)}s`",
        "",
        "## Configuration",
        "",
        "| Setting | Value |",
        "| --- | --- |",
    ]
    for k in (
        "embed_model",
        "collection",
        "relevance_floor",
        "top_k",
        "temperature",
        "llm_model",
        "chunk_size",
        "chunk_overlap",
    ):
        lines.append(f"| {k} | `{cfg.get(k)}` |")

    lines.extend([
        "",
        "## Component Level (Retrieval)",
        "",
        "| Metric | Value |",
        "| --- | --- |",
        f"| recall@3 | {comp['recall_at_3']:.4f} |",
        f"| recall@5 | {comp['recall_at_5']:.4f} |",
        f"| MRR | {comp['mrr']:.4f} |",
        f"| nDCG@5 | {comp['ndcg_at_5']:.4f} |",
        f"| Evaluated | {comp['n_evaluated']} |",
        f"| Skipped | {len(result['skipped'])} |",
        "",
    ])
    return "\n".join(lines)


def run_suite(
    golden_path: Path,
    api_url: str,
    collection_name: str,
    out_dir: Path,
    quick: bool = False,
    seed: int = 42,
    retriever: ComponentRetriever | None = None,
    http_client: httpx.Client | None = None,
) -> tuple[dict[str, Any], Path, Path]:
    """Execute the evaluation suite and write JSON + Markdown outputs."""
    random.seed(seed)
    started = time.perf_counter()

    api_config = verify_api_collection_and_config(
        api_url=api_url,
        expected_collection=collection_name,
        http_client=http_client,
    )

    all_questions = load_golden_jsonl(golden_path)
    if quick:
        quick_set = set(QUICK_IDS)
        selected = [q for q in all_questions if q["id"] in quick_set]
    else:
        selected = all_questions

    if retriever is None:
        embed_model = str(
            api_config.get("embed_model") or os.environ.get("EMBED_MODEL", "nomic-embed-text")
        )
        embed_provider = os.environ.get(
            "EMBED_PROVIDER", "hash" if embed_model == "hash-768" else "ollama"
        )
        retriever = ChromaComponentRetriever(
            collection_name=collection_name,
            embed_model=embed_model,
            embed_provider=embed_provider,
            chroma_host=os.environ.get("CHROMA_HOST", "localhost"),
            chroma_port=int(os.environ.get("CHROMA_PORT", 8000)),
            ollama_url=os.environ.get("OLLAMA_URL", "http://172.17.0.1:11434"),
        )

    col_meta = retriever.collection_metadata()
    embed_model_final = str(
        col_meta.get("embed_model")
        or api_config.get("embed_model")
        or os.environ.get("EMBED_MODEL", "nomic-embed-text")
    )

    config_section = {
        "embed_model": embed_model_final,
        "collection": collection_name,
        "relevance_floor": api_config.get(
            "relevance_floor", float(os.environ.get("RELEVANCE_FLOOR", 0.60))
        ),
        "top_k": api_config.get("top_k", int(os.environ.get("TOP_K", 5))),
        "temperature": api_config.get(
            "temperature", float(os.environ.get("TEMPERATURE", 0.2))
        ),
        "llm_model": api_config.get("llm_model", os.environ.get("LLM_MODEL", "llama3.2:3b")),
        "chunk_size": api_config.get(
            "chunk_size", col_meta.get("chunk_size", int(os.environ.get("CHUNK_SIZE", 600)))
        ),
        "chunk_overlap": api_config.get(
            "chunk_overlap",
            col_meta.get("chunk_overlap", int(os.environ.get("CHUNK_OVERLAP", 100))),
        ),
    }

    batch_b_present = retriever.has_batch_b()
    skipped: list[dict[str, str]] = []
    per_question: list[dict[str, Any]] = []
    top_k = max(5, int(config_section["top_k"]))

    for q in selected:
        qid = q["id"]
        if q.get("must_abstain"):
            skipped.append({"id": qid, "reason": "must_abstain"})
            continue
        req_batch = q.get("requires_batch")
        if req_batch == "B" and not batch_b_present:
            skipped.append({"id": qid, "reason": "requires_batch: B not in collection"})
            continue

        retrieved_metas = retriever.retrieve_metas(q["question"], k=top_k)
        scores = score_question(q.get("relevant") or [], retrieved_metas)
        per_question.append(
            {
                "id": qid,
                "type": q.get("type", ""),
                "first_hit_rank": scores["first_hit_rank"],
                "recall_at_3": scores["recall_at_3"],
                "recall_at_5": scores["recall_at_5"],
                "mrr": scores["mrr"],
                "ndcg_at_5": scores["ndcg_at_5"],
                "retrieved": [
                    {
                        "rank": idx,
                        "source": m.get("source"),
                        "page": m.get("page"),
                        "section": m.get("section"),
                    }
                    for idx, m in enumerate(retrieved_metas[:5], start=1)
                ],
            }
        )

    agg = aggregate_component_metrics(per_question)
    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    elapsed = round(time.perf_counter() - started, 2)

    result: dict[str, Any] = {
        "timestamp": ts,
        "collection": collection_name,
        "embed_model": embed_model_final,
        "seed": seed,
        "quick": quick,
        "n_questions": len(selected),
        "elapsed_seconds": elapsed,
        "config": config_section,
        "skipped": skipped,
        "levels": {
            "module": {
                "paraphrase_gap": None,
                "embed_ms_per_chunk": None,
                "max_determinism_diff": None,
            },
            "component": {
                "recall_at_3": agg["recall_at_3"],
                "recall_at_5": agg["recall_at_5"],
                "mrr": agg["mrr"],
                "ndcg_at_5": agg["ndcg_at_5"],
                "n_evaluated": agg["n_evaluated"],
                "per_question": per_question,
            },
            "e2e": {
                "faithfulness": None,
                "abstention_correctness": None,
                "citation_validity": None,
                "latency_p50_ms": None,
                "latency_p95_ms": None,
            },
        },
    }

    out_dir.mkdir(parents=True, exist_ok=True)
    json_path = out_dir / f"suite-{ts}.json"
    md_path = out_dir / f"suite-{ts}.md"
    latest_path = out_dir / "latest.json"

    json_text = json.dumps(result, indent=2)
    json_path.write_text(json_text, encoding="utf-8")
    latest_path.write_text(json_text, encoding="utf-8")
    md_path.write_text(_format_markdown_summary(result), encoding="utf-8")

    return result, json_path, md_path
