"""Tests for eval/run_eval.py — no network, no model, no Chroma."""

from __future__ import annotations

import sys
from pathlib import Path

# Add eval/ to path so we can import run_eval
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "eval"))
import run_eval  # noqa: E402, I001


# ---------------------------------------------------------------------------
# _section_matches
# ---------------------------------------------------------------------------


class TestSectionMatches:
    """Test the clause-range and heading matching logic."""

    def test_exact_match(self) -> None:
        assert run_eval._section_matches("Faculty Summary", "Faculty Summary")

    def test_exact_match_case_insensitive(self) -> None:
        assert run_eval._section_matches("faculty summary", "Faculty Summary")

    def test_clause_exact(self) -> None:
        # "2.3" matches chunk section "2.3 Grade Points"
        assert run_eval._section_matches("2.3", "2.3 Grade Points")

    def test_clause_in_range(self) -> None:
        # "5.4" matches chunk section "5.4-5.7 Academic Warning/Probation"
        assert run_eval._section_matches("5.4", "5.4-5.7 Academic Warning/Probation")

    def test_clause_end_of_range(self) -> None:
        # "5.7" matches chunk section "5.4-5.7 Academic Warning/Probation"
        assert run_eval._section_matches("5.7", "5.4-5.7 Academic Warning/Probation")

    def test_clause_outside_range(self) -> None:
        # "5.8" does NOT match "5.4-5.7 ..."
        assert not run_eval._section_matches("5.8", "5.4-5.7 Academic Warning/Probation")

    def test_clause_different_major(self) -> None:
        # "4.3" does NOT match "5.4-5.7 ..."
        assert not run_eval._section_matches("4.3", "5.4-5.7 Academic Warning/Probation")

    def test_clause_single_in_section(self) -> None:
        # "1.21" matches "1.21-1.22 Attendance Requirements"
        assert run_eval._section_matches("1.21", "1.21-1.22 Attendance Requirements")

    def test_clause_no_match_40_vs_4(self) -> None:
        # "5.40" should NOT match "5.4-5.7" (5.40 > 5.7)
        assert not run_eval._section_matches("5.40", "5.4-5.7 Academic Warning")

    def test_heading_substring(self) -> None:
        # "Acceptable Uses of Generative AI Tools" is in chunk section
        assert run_eval._section_matches(
            "Acceptable Uses of Generative AI Tools",
            "Acceptable Uses of Generative AI Tools for Students"
        )

    def test_heading_no_match(self) -> None:
        assert not run_eval._section_matches("Hostel Rules", "Academic Rules")

    def test_fixture_section_match(self) -> None:
        # Fixture golden uses "2. Academic warning" and chunk has "2. Academic warning"
        assert run_eval._section_matches("2. Academic warning", "2. Academic warning")

    def test_fixture_heading_in_chunk(self) -> None:
        # "1. Attendance" matches "1. Attendance"
        assert run_eval._section_matches("1. Attendance", "1. Attendance")


# ---------------------------------------------------------------------------
# recall_at_k
# ---------------------------------------------------------------------------


class TestRecallAtK:
    def test_none_for_no_relevant(self) -> None:
        assert run_eval.recall_at_k([], [{"source": "a.pdf", "section": "1"}], 5) is None

    def test_perfect_recall(self) -> None:
        relevant = [{"source": "a.pdf", "section": "5.4"}]
        retrieved = [
            {"source": "a.pdf", "section": "5.4-5.7 Academic Warning"},
            {"source": "a.pdf", "section": "2.3 Grade Points"},
        ]
        assert run_eval.recall_at_k(relevant, retrieved, 5) == 1.0

    def test_zero_recall(self) -> None:
        relevant = [{"source": "a.pdf", "section": "5.4"}]
        retrieved = [
            {"source": "a.pdf", "section": "2.3 Grade Points"},
            {"source": "b.pdf", "section": "5.4-5.7 Academic Warning"},
        ]
        # source mismatch for the second one
        assert run_eval.recall_at_k(relevant, retrieved, 5) == 0.0

    def test_partial_recall(self) -> None:
        relevant = [
            {"source": "a.pdf", "section": "5.4"},
            {"source": "a.pdf", "section": "1.21"},
        ]
        retrieved = [
            {"source": "a.pdf", "section": "5.4-5.7 Academic Warning"},
            {"source": "a.pdf", "section": "3.1 Admission"},
        ]
        assert run_eval.recall_at_k(relevant, retrieved, 5) == 0.5

    def test_k_limits_retrieved(self) -> None:
        relevant = [{"source": "a.pdf", "section": "1.21"}]
        retrieved = [
            {"source": "a.pdf", "section": "3.1 Admission"},
            {"source": "a.pdf", "section": "2.3 Grade Points"},
            {"source": "a.pdf", "section": "1.21-1.22 Attendance"},  # at index 2
        ]
        # k=2 means only first 2 are considered
        assert run_eval.recall_at_k(relevant, retrieved, 2) == 0.0
        # k=3 includes the matching one
        assert run_eval.recall_at_k(relevant, retrieved, 3) == 1.0

    def test_heading_match(self) -> None:
        relevant = [{"source": "annual-report.pdf", "section": "Faculty Summary"}]
        retrieved = [{"source": "annual-report.pdf", "section": "Faculty Summary"}]
        assert run_eval.recall_at_k(relevant, retrieved, 5) == 1.0


# ---------------------------------------------------------------------------
# citation_validity
# ---------------------------------------------------------------------------


class TestCitationValidity:
    def test_none_for_no_citations(self) -> None:
        assert run_eval.citation_validity({"citations": [], "retrieved": []}) is None

    def test_all_valid(self) -> None:
        resp = {
            "citations": [{"source": "a.pdf", "section": "5.4"}],
            "retrieved": [{"source": "a.pdf", "section": "5.4-5.7 Academic Warning"}],
        }
        assert run_eval.citation_validity(resp) == 1.0

    def test_none_valid(self) -> None:
        resp = {
            "citations": [{"source": "a.pdf", "section": "9.9"}],
            "retrieved": [{"source": "a.pdf", "section": "5.4-5.7 Academic Warning"}],
        }
        assert run_eval.citation_validity(resp) == 0.0

    def test_partial_valid(self) -> None:
        resp = {
            "citations": [
                {"source": "a.pdf", "section": "5.4"},
                {"source": "a.pdf", "section": "9.9"},
            ],
            "retrieved": [{"source": "a.pdf", "section": "5.4-5.7 Academic Warning"}],
        }
        assert run_eval.citation_validity(resp) == 0.5


# ---------------------------------------------------------------------------
# summarise
# ---------------------------------------------------------------------------


class TestSummarise:
    def test_basic_summary(self) -> None:
        rows = [
            {
                "id": "q01", "type": "ordinary", "must_abstain": False,
                "relevant": [{"source": "a.pdf", "section": "5.4"}],
                "response": {
                    "retrieved": [{"source": "a.pdf", "section": "5.4-5.7 Warning"}],
                    "citations": [{"source": "a.pdf", "section": "5.4"}],
                    "answered_from_context": True,
                },
            },
        ]
        result = run_eval.summarise(rows)
        assert result["recall_at_3"] == 1.0
        assert result["recall_at_5"] == 1.0
        assert result["citation_validity"] == 1.0
        assert result["n_questions"] == 1

    def test_abstention_tracking(self) -> None:
        rows = [
            {
                "id": "q13", "type": "abstain", "must_abstain": True,
                "relevant": [],
                "response": {
                    "retrieved": [],
                    "citations": [],
                    "answered_from_context": False,
                },
            },
        ]
        result = run_eval.summarise(rows)
        assert result["abstention_correctness"] == 1.0


# ---------------------------------------------------------------------------
# gate
# ---------------------------------------------------------------------------


class TestGate:
    def test_pass_no_baseline(self) -> None:
        results = {
            "recall_at_5": 0.8,
            "citation_validity": 1.0,
            "abstention_correctness": 1.0,
        }
        assert run_eval.gate(results, None) == []

    def test_fail_recall_drop(self) -> None:
        results = {"recall_at_5": 0.7, "citation_validity": 1.0, "abstention_correctness": 1.0}
        baseline = {"recall_at_5": 0.8}
        failures = run_eval.gate(results, baseline)
        assert len(failures) == 1
        assert "recall@5" in failures[0]

    def test_pass_within_tolerance(self) -> None:
        results = {"recall_at_5": 0.76, "citation_validity": 1.0, "abstention_correctness": 1.0}
        baseline = {"recall_at_5": 0.8}
        assert run_eval.gate(results, baseline) == []

    def test_fail_citation_validity(self) -> None:
        results = {"recall_at_5": 0.8, "citation_validity": 0.90, "abstention_correctness": 1.0}
        failures = run_eval.gate(results, None)
        assert len(failures) == 1
        assert "citation_validity" in failures[0]

    def test_fail_abstention(self) -> None:
        results = {"recall_at_5": 0.8, "citation_validity": 1.0, "abstention_correctness": 0.5}
        failures = run_eval.gate(results, None)
        assert len(failures) == 1
        assert "abstention" in failures[0]

