"""Offline unit tests for eval.suite (Assignment 03, Part B)."""

from __future__ import annotations

import json
import math
import sys
from pathlib import Path
from typing import Any

import httpx
import pytest

from eval.suite.__main__ import main
from eval.suite.metrics import (
    aggregate_component_metrics,
    is_hit,
    score_question,
)
from eval.suite.runner import QUICK_IDS, run_suite


class FakeRetriever:
    """Offline fake retriever returning preconfigured metadata lists per question."""

    def __init__(
        self,
        responses: dict[str, list[dict[str, Any]]],
        batch_b: bool = False,
        metadata: dict[str, Any] | None = None,
    ) -> None:
        self._responses = responses
        self._batch_b = batch_b
        self._meta = metadata or {"embed_model": "nomic-embed-text", "dimension": 768}

    def retrieve_metas(self, question: str, k: int = 5) -> list[dict[str, Any]]:
        return self._responses.get(question, [])[:k]

    def has_batch_b(self) -> bool:
        return self._batch_b

    def collection_metadata(self) -> dict[str, Any]:
        return dict(self._meta)


def _mock_http_client(
    reported_collection: str = "ragops_demo",
    ready_status: int = 200,
) -> httpx.Client:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/readyz":
            return httpx.Response(
                ready_status,
                json={
                    "status": "ready" if ready_status == 200 else "not_ready",
                    "collection": reported_collection,
                    "checks": {
                        "chroma": "ok",
                        "collection": "ok",
                        "count": 100,
                        "embed_model": "nomic-embed-text",
                        "redis": "ok",
                    },
                },
            )
        if request.url.path == "/info":
            return httpx.Response(
                200,
                json={
                    "embed_model": "nomic-embed-text",
                    "collection": reported_collection,
                    "relevance_floor": 0.60,
                    "top_k": 5,
                    "temperature": 0.2,
                    "llm_model": "llama3.2:3b",
                    "chunk_size": 600,
                    "chunk_overlap": 100,
                },
            )
        return httpx.Response(404)

    return httpx.Client(transport=httpx.MockTransport(handler))


def test_hit_rule_source_page_and_clause_range() -> None:
    rel = {"source": "handbook.pdf", "page": 24, "section": "5.4"}

    # Same source + same page -> hit
    assert is_hit(rel, {"source": "handbook.pdf", "page": 24, "section": "Other"})
    # Same source + different page + clause inside range -> hit
    assert is_hit(rel, {"source": "handbook.pdf", "page": 25, "section": "5.1-5.7 Warning"})
    # Different source even with same page -> miss
    assert not is_hit(rel, {"source": "other.pdf", "page": 24, "section": "5.4"})
    # Same source, wrong page, out-of-range clause -> miss
    assert not is_hit(rel, {"source": "handbook.pdf", "page": 10, "section": "1.21-1.25 Attendance"})


def test_hand_computed_metrics() -> None:
    rel1 = [{"source": "a.pdf", "page": 10, "section": "1.21"}]

    # Hit at rank 2
    s_rank2 = score_question(
        rel1,
        [
            {"source": "a.pdf", "page": 1, "section": "Intro"},
            {"source": "a.pdf", "page": 10, "section": "1.21"},
        ],
    )
    assert s_rank2["first_hit_rank"] == 2
    assert s_rank2["recall_at_3"] == 1.0
    assert s_rank2["recall_at_5"] == 1.0
    assert s_rank2["mrr"] == 0.5
    assert s_rank2["ndcg_at_5"] == pytest.approx(1.0 / math.log2(3), rel=1e-5)

    # Hit at rank 4
    s_rank4 = score_question(
        rel1,
        [
            {"source": "b.pdf", "page": 1},
            {"source": "b.pdf", "page": 2},
            {"source": "b.pdf", "page": 3},
            {"source": "a.pdf", "page": 10},
        ],
    )
    assert s_rank4["first_hit_rank"] == 4
    assert s_rank4["recall_at_3"] == 0.0
    assert s_rank4["recall_at_5"] == 1.0
    assert s_rank4["mrr"] == 0.25
    assert s_rank4["ndcg_at_5"] == pytest.approx(1.0 / math.log2(5), rel=1e-5)

    # Two relevant items hit at rank 1 and rank 3
    rel2 = [
        {"source": "a.pdf", "page": 10, "section": "1.21"},
        {"source": "a.pdf", "page": 28, "section": "6.7"},
    ]
    s_two = score_question(
        rel2,
        [
            {"source": "a.pdf", "page": 10},
            {"source": "b.pdf", "page": 2},
            {"source": "a.pdf", "page": 28},
        ],
    )
    expected_idcg = 1.0 / math.log2(2) + 1.0 / math.log2(3)
    expected_dcg = 1.0 / math.log2(2) + 1.0 / math.log2(4)
    assert s_two["recall_at_3"] == 1.0
    assert s_two["recall_at_5"] == 1.0
    assert s_two["mrr"] == 1.0
    assert s_two["ndcg_at_5"] == pytest.approx(expected_dcg / expected_idcg, rel=1e-5)

    agg = aggregate_component_metrics([s_rank2, s_rank4])
    assert agg["recall_at_3"] == 0.5
    assert agg["recall_at_5"] == 1.0
    assert agg["mrr"] == 0.375
    assert agg["n_evaluated"] == 2


def test_skip_logic_and_result_keys(tmp_path: Path) -> None:
    golden = tmp_path / "golden.jsonl"
    questions = [
        {
            "id": "g01",
            "question": "Q1?",
            "type": "answerable",
            "must_abstain": False,
            "relevant": [{"source": "a.pdf", "page": 10, "section": "1.21"}],
        },
        {
            "id": "g26",
            "question": "Q_unans?",
            "type": "unanswerable",
            "must_abstain": True,
            "relevant": [],
        },
        {
            "id": "g31",
            "question": "Q_batch_b?",
            "type": "hard",
            "must_abstain": False,
            "requires_batch": "B",
            "relevant": [{"source": "b10_academic_rules_v2.md", "page": 1, "section": "4.25"}],
        },
    ]
    golden.write_text("\n".join(json.dumps(q) for q in questions) + "\n", encoding="utf-8")

    fake_ret = FakeRetriever(
        {"Q1?": [{"source": "a.pdf", "page": 10, "section": "1.21"}]},
        batch_b=False,
    )
    out_dir = tmp_path / "out"
    result, json_path, md_path = run_suite(
        golden_path=golden,
        api_url="http://mock-api",
        collection_name="ragops_demo",
        out_dir=out_dir,
        quick=False,
        seed=7,
        retriever=fake_ret,
        http_client=_mock_http_client("ragops_demo"),
    )

    assert json_path.is_file()
    assert md_path.is_file()
    assert (out_dir / "latest.json").is_file()

    # Required keys
    for key in (
        "timestamp",
        "collection",
        "embed_model",
        "seed",
        "quick",
        "n_questions",
        "config",
        "skipped",
        "levels",
    ):
        assert key in result

    for cfg_key in (
        "embed_model",
        "collection",
        "relevance_floor",
        "top_k",
        "temperature",
        "llm_model",
        "chunk_size",
        "chunk_overlap",
    ):
        assert cfg_key in result["config"]

    assert result["quick"] is False
    assert result["seed"] == 7
    assert result["n_questions"] == 3
    assert len(result["skipped"]) == 2
    reasons = {s["id"]: s["reason"] for s in result["skipped"]}
    assert reasons["g26"] == "must_abstain"
    assert "requires_batch" in reasons["g31"]
    assert result["levels"]["component"]["recall_at_5"] == 1.0
    assert result["levels"]["module"]["paraphrase_gap"] is None
    assert result["levels"]["e2e"]["faithfulness"] is None


def test_quick_mode_subset(tmp_path: Path) -> None:
    golden = Path("eval/golden.jsonl")
    fake_ret = FakeRetriever({}, batch_b=True)
    result, _, _ = run_suite(
        golden_path=golden,
        api_url="http://mock-api",
        collection_name="ragops_demo",
        out_dir=tmp_path / "quick_out",
        quick=True,
        seed=42,
        retriever=fake_ret,
        http_client=_mock_http_client("ragops_demo"),
    )
    assert result["quick"] is True
    assert result["n_questions"] == len(QUICK_IDS) == 6
    assert result["levels"]["component"]["n_evaluated"] == 4
    assert len(result["skipped"]) == 2


def test_collection_mismatch_exits_nonzero(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    golden = tmp_path / "g.jsonl"
    golden.write_text('{"id": "g01", "question": "Q?", "type": "answerable", "must_abstain": false, "relevant": []}\n')

    mock_client = _mock_http_client("other_collection")
    monkeypatch.setattr(
        "eval.suite.runner.httpx.Client",
        lambda **kwargs: mock_client,
    )
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "eval.suite",
            "--golden",
            str(golden),
            "--api",
            "http://mock-api",
            "--collection",
            "ragops_demo",
            "--out",
            str(tmp_path / "out"),
        ],
    )

    rc = main()
    err = capsys.readouterr().err
    assert rc == 1
    assert "Collection mismatch" in err
