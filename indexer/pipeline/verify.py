"""Verification stub (Part A1).

Real checks (quality, completeness, recency, uniqueness) come later.
For now every check passes unconditionally. The CLI already does the
plumbing: a failing document is quarantined and a report is written.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal


@dataclass
class CheckResult:
    """One verification check result."""

    name: str
    passed: bool
    reason: str


@dataclass
class VerificationResult:
    """Per-document verification outcome."""

    source: str
    doc_id: str
    checks: list[CheckResult]

    @property
    def passed(self) -> bool:
        return all(c.passed for c in self.checks)

    def to_dict(self) -> dict[str, object]:
        return {
            "source": self.source,
            "doc_id": self.doc_id,
            "passed": self.passed,
            "checks": [
                {"name": c.name, "passed": c.passed, "reason": c.reason}
                for c in self.checks
            ],
        }


CheckName = Literal["quality", "completeness", "recency", "uniqueness"]


def verify_document(
    source: str,
    doc_id: str,
    doc_hash: str,
    text_length: int,
    page_count: int,
    doc_version: str,
    title: str,
) -> VerificationResult:
    """Run four verification checks on a document.

    Stub implementation: every check passes. Real checks added in a later task.
    """
    checks = [
        CheckResult(
            name="quality",
            passed=True,
            reason="stub: quality check not yet implemented",
        ),
        CheckResult(
            name="completeness",
            passed=True,
            reason="stub: completeness check not yet implemented",
        ),
        CheckResult(
            name="recency",
            passed=True,
            reason="stub: recency check not yet implemented",
        ),
        CheckResult(
            name="uniqueness",
            passed=True,
            reason="stub: uniqueness check not yet implemented",
        ),
    ]
    return VerificationResult(source=source, doc_id=doc_id, checks=checks)
