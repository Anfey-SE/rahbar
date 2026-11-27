"""Unit tests for eval.golden (Assignment 03 Part B).

Tests run offline with no external network or model dependencies.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from eval.golden import (
    BATCH_B_QUESTIONS,
    build_golden,
    check_absence,
    check_answers,
    parse_human_questions,
    validate_golden,
)


@pytest.fixture
def sample_human_markdown(tmp_path: Path) -> Path:
    md_content = """
Q: What is the minimum CGPA required for graduation?
TYPE: answerable
DOC: academic-rules-handbook-jun-2026.pdf
PAGE: 10
CLAUSE: 1.25
ANSWER: 2.00
WRITTEN-BY: test-user

Q: What are the library timings?
TYPE: unanswerable
DOC: none
PAGE: none
CLAUSE: none
ANSWER: none, must abstain
WRITTEN-BY: test-user

Q: Multi-document clause question?
TYPE: hard
DOC: academic-rules-handbook-jun-2026.pdf
PAGE: 28
CLAUSE: 6.7
DOC: academic-rules-handbook-jun-2026.pdf
PAGE: 10
CLAUSE: 1.22
ANSWER: Summer courses use 80% attendance; failing regular attendance gives F/A grade.
WRITTEN-BY: test-user
"""
    p = tmp_path / "HUMAN-TEST.md"
    p.write_text(md_content.strip(), encoding="utf-8")
    return p


@pytest.fixture
def sample_synthetic_jsonl(tmp_path: Path) -> Path:
    records = [
        {
            "id": f"g{i:02d}",
            "question": f"Question {i}?",
            "type": "answerable" if i <= 15 else ("unanswerable" if i <= 20 else "hard"),
            "label": "synthetic",
            "reviewed": i <= 10,
            "must_abstain": 16 <= i <= 20,
            "relevant": [{"source": "academic-rules-handbook-jun-2026.pdf", "page": 10, "section": "1.21"}]
            if not (16 <= i <= 20)
            else [],
            "answer": "Sample answer 100% attendance required" if not (16 <= i <= 20) else "none",
            "tags": [],
            "origin": "synthetic",
            "page_check": "text-layer",
            "notes": "",
        }
        for i in range(1, 26)
    ]
    p = tmp_path / "synthetic_test.jsonl"
    with open(p, "w", encoding="utf-8") as f:
        for r in records:
            f.write(json.dumps(r) + "\n")
    return p


def test_parse_human_questions(sample_human_markdown: Path) -> None:
    questions = parse_human_questions(sample_human_markdown)
    assert len(questions) == 3

    # Answerable question
    q1 = questions[0]
    assert q1["id"] == "h01"
    assert q1["type"] == "answerable"
    assert q1["label"] == "human"
    assert q1["reviewed"] is True
    assert q1["must_abstain"] is False
    assert len(q1["relevant"]) == 1
    assert q1["relevant"][0]["source"] == "academic-rules-handbook-jun-2026.pdf"
    assert q1["relevant"][0]["page"] == 10
    assert q1["relevant"][0]["section"] == "1.25"

    # Unanswerable question
    q2 = questions[1]
    assert q2["id"] == "h02"
    assert q2["type"] == "unanswerable"
    assert q2["must_abstain"] is True
    assert q2["relevant"] == []

    # Multi-clause question
    q3 = questions[2]
    assert q3["id"] == "h03"
    assert q3["type"] == "hard"
    assert len(q3["relevant"]) == 2
    assert q3["relevant"][0]["page"] == 28
    assert q3["relevant"][1]["page"] == 10


def test_build_golden_merges_and_outputs_unique_ids(
    tmp_path: Path,
    sample_synthetic_jsonl: Path,
    sample_human_markdown: Path,
) -> None:
    out_file = tmp_path / "golden.jsonl"
    built = build_golden(
        synthetic_path=sample_synthetic_jsonl,
        human_path=sample_human_markdown,
        output_path=out_file,
    )
    assert len(built) == 25 + len(BATCH_B_QUESTIONS) + 3
    assert out_file.exists()

    # Read back and verify IDs are unique
    seen = set()
    with open(out_file, encoding="utf-8") as f:
        for line in f:
            obj = json.loads(line)
            assert obj["id"] not in seen
            seen.add(obj["id"])


def test_validate_golden_success_on_real_golden_set() -> None:
    passed, stats, errors = validate_golden()
    assert passed is True, f"Validation errors: {errors}"
    assert stats["total"] >= 25
    assert stats["answerable"] >= 15
    assert stats["unanswerable"] >= 5
    assert stats["hard"] >= 5
    assert stats["human"] >= 8
    assert stats["synthetic_reviewed"] >= 8
    assert stats["requires_batch_b"] == 3


def test_validate_golden_catches_invalid_types_and_missing_files(tmp_path: Path) -> None:
    bad_data = [
        {
            "id": "bad01",
            "question": "Bad source question?",
            "type": "answerable",
            "label": "human",
            "reviewed": True,
            "must_abstain": False,
            "relevant": [{"source": "non-existent-doc.pdf", "page": 1, "section": "1.1"}],
            "answer": "Test",
        },
        {
            "id": "bad02",
            "question": "Out of bounds page?",
            "type": "answerable",
            "label": "synthetic",
            "reviewed": True,
            "must_abstain": False,
            "relevant": [{"source": "academic-rules-handbook-jun-2026.pdf", "page": 999, "section": "1.1"}],
            "answer": "Test",
        },
        {
            "id": "bad03",
            "question": "Unanswerable with relevant sources?",
            "type": "unanswerable",
            "label": "synthetic",
            "reviewed": False,
            "must_abstain": False,
            "relevant": [{"source": "academic-rules-handbook-jun-2026.pdf", "page": 1, "section": ""}],
            "answer": "none",
        },
    ]

    p = tmp_path / "bad_golden.jsonl"
    with open(p, "w", encoding="utf-8") as f:
        for item in bad_data:
            f.write(json.dumps(item) + "\n")

    passed, _, errors = validate_golden(golden_path=p)
    assert passed is False
    assert any("does not exist in corpus" in e for e in errors)
    assert any("out of bounds" in e for e in errors)
    assert any("must have must_abstain=True" in e for e in errors)


def test_check_answers_finds_terms(tmp_path: Path) -> None:
    sample_golden = tmp_path / "sample.jsonl"
    sample_golden.write_text(
        json.dumps({
            "id": "t01",
            "type": "answerable",
            "relevant": [{"source": "mock.pdf", "page": 1}],
            "answer": "Minimum CGPA 2.00 required",
        }) + "\n",
        encoding="utf-8",
    )
    mock_pages = {"mock.pdf": {1: "The minimum cgpa is 2.00 required for graduation."}}
    mismatches = check_answers(golden_path=sample_golden, pages_text=mock_pages)
    assert mismatches == []


def test_check_absence_runs(tmp_path: Path) -> None:
    sample_golden = tmp_path / "sample.jsonl"
    sample_golden.write_text(
        json.dumps({
            "id": "h08",
            "type": "unanswerable",
            "relevant": [],
            "answer": "none",
        }) + "\n",
        encoding="utf-8",
    )
    mock_pages = {
        "student-handbook-jun-2026.pdf": {
            12: "20. Dress Code: The students of the University are expected to dress appropriately."
        }
    }
    hits = check_absence(golden_path=sample_golden, pages_text=mock_pages)
    assert len(hits) == 1
    assert hits[0][0] == "h08"
    assert hits[0][1] == "dress code"
    assert hits[0][2] == "student-handbook-jun-2026.pdf"
