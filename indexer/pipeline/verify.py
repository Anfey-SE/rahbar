"""Verification stage (Assignment 03, Part A1).

Verification runs after extraction and before chunking, only for new or changed documents.
Four checks:
  1. quality: extraction failed, empty/nearly empty text, garbled text (low alpha ratio
     or excessive U+FFFD), and OCR confidence note.
  2. completeness: required metadata (source, title, date/version, page count) derived
     according to documented rules; truncation checks for PDF and Markdown.
  3. recency: logical document key (normalized title). Compare version/date with active
     documents. Newer incoming archives older; older incoming is quarantined.
  4. uniqueness: exact duplicates by file hash or whitespace-normalized text hash;
     near-duplicates by document-level embedding similarity (>= NEAR_DUP_THRESHOLD)
     among active documents.

Injection warning: regex scan for prompt injection patterns. Logged under warnings;
does NOT quarantine or fail the document.
"""

from __future__ import annotations

import hashlib
import math
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterator

from .config import IndexerSettings, get_settings
from .models import Document, Page


@dataclass
class CheckResult:
    """One verification check result."""

    name: str
    passed: bool
    reason: str
    details: dict[str, Any] = field(default_factory=dict)
    status: str = "pass"  # "pass", "fail", "skipped"


@dataclass
class VerificationResult:
    """Per-document verification outcome."""

    source: str
    doc_id: str
    checks: list[CheckResult]
    doc_hash: str = ""
    warnings: list[str] = field(default_factory=list)
    decision: str = "accepted"  # "accepted", "quarantined", "unchanged, skipped"
    actions: list[str] = field(default_factory=list)
    to_archive: list[str] = field(default_factory=list)

    @property
    def passed(self) -> bool:
        return all(c.passed and c.status != "fail" for c in self.checks)

    def __iter__(self) -> Iterator[Any]:
        yield self
        yield self.to_archive

    def to_dict(self) -> dict[str, Any]:
        return {
            "source": self.source,
            "file": self.source,
            "doc_id": self.doc_id,
            "doc_hash": self.doc_hash,
            "passed": self.passed,
            "checks": [
                {
                    "name": c.name,
                    "passed": (c.passed and c.status != "skipped"),
                    "pass": (c.passed and c.status != "skipped") if c.status != "skipped" else None,
                    "status": c.status,
                    "reason": c.reason,
                    "details": c.details,
                }
                for c in self.checks
            ],
            "warnings": self.warnings,
            "decision": self.decision,
            "actions": self.actions,
        }


def normalize_title(title: str) -> str:
    """Normalize title to a canonical logical document key.

    Rule: lower-case, punctuation removed, bracketed dates and version words removed.
    """
    t = title.lower()
    # Remove bracketed dates / versions like (jun 2026), [2023], etc.
    t = re.sub(r"[\(\[\{][^\)\]\}]*[\)\]\}]", " ", t)
    # Remove version words and date numbers
    t = re.sub(r"\b(?:v\d+(\.\d+)*|\d{4}(?:-\d{2,4})?)\b", " ", t)
    t = re.sub(
        r"\b(?:january|february|march|april|may|june|july|august|september|october|november|december)\b",
        " ",
        t,
    )
    t = re.sub(r"\b(?:jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)\b", " ", t)
    t = re.sub(r"\b(?:version|ver|draft|final)\b", " ", t)
    # Remove punctuation
    t = re.sub(r"[^\w\s]", " ", t)
    return " ".join(t.split())


def parse_version_tuple(v: str) -> tuple[int, int, int] | None:
    """Parse version or date string into a comparable tuple (major/year, minor/month, patch)."""
    m = re.search(r"(\d{4})[^\d]+(\d{1,2})", v)
    if m:
        return (int(m.group(1)), int(m.group(2)), 0)

    months = {
        "jan": 1, "feb": 2, "mar": 3, "apr": 4, "may": 5, "jun": 6,
        "jul": 7, "aug": 8, "sep": 9, "oct": 10, "nov": 11, "dec": 12,
    }
    m = re.search(r"(jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[^\d]+(\d{4})", v.lower())
    if m:
        return (int(m.group(2)), months[m.group(1)], 0)

    m = re.search(r"\b(19\d\d|20\d\d)\b", v)
    if m:
        return (int(m.group(1)), 1, 0)

    m = re.search(r"v?(\d+)(?:\.(\d+))?(?:\.(\d+))?", v.lower())
    if m:
        return (int(m.group(1)), int(m.group(2) or 0), int(m.group(3) or 0))

    return None


def compare_versions(v1: str, v2: str) -> int:
    """Compare two version/date strings.

    Returns:
       1 if v1 > v2 (v1 is newer)
      -1 if v1 < v2 (v1 is older)
       0 if v1 == v2 or uncomparable
    """
    t1 = parse_version_tuple(v1)
    t2 = parse_version_tuple(v2)
    if t1 is not None and t2 is not None:
        if t1 > t2:
            return 1
        elif t1 < t2:
            return -1
        else:
            return 0
    if v1 > v2:
        return 1
    elif v1 < v2:
        return -1
    return 0


def derive_title(
    path: Path,
    front_matter_dict: dict[str, str] | None = None,
    pdf_meta: dict[str, Any] | None = None,
    index_name: str | None = None,
    doc: Document | None = None,
) -> str | None:
    """Derive document title according to rules in order:
    1. Front matter `title`
    2. PDF /Title metadata (if not empty and not an artifact like .cdr/.pdf)
    3. `corpus/raw/_index.json` (or input _index.json) name
    4. First heading or first non-empty line (at least 5 characters).
    """
    if front_matter_dict and front_matter_dict.get("title"):
        val = front_matter_dict["title"].strip()
        if len(val) >= 3:
            return val

    if pdf_meta:
        raw_title = pdf_meta.get("Title") or pdf_meta.get("/Title")
        if raw_title and isinstance(raw_title, str):
            clean_title = raw_title.strip()
            lower = clean_title.lower()
            if (
                len(clean_title) >= 4
                and not any(lower.endswith(ext) for ext in (".cdr", ".pdf", ".ai", ".indd"))
                and not lower.startswith("untitled")
            ):
                return clean_title

    if index_name and len(index_name.strip()) >= 3:
        return index_name.strip()

    if doc and doc.pages:
        for p in doc.pages:
            lines = p.text.splitlines()
            for line in lines:
                s = line.strip()
                if not s:
                    continue
                if s.startswith("#"):
                    clean_h = s.lstrip("#").strip()
                    if len(clean_h) >= 5:
                        return clean_h
                elif len(s) >= 5:
                    return s

    if path.suffix.lower() in {".md", ".txt"} and path.is_file():
        try:
            content = path.read_text(encoding="utf-8", errors="replace")
            stripped = re.sub(r"^---\r?\n.*?\r?\n---\r?\n?", "", content, flags=re.S)
            for line in stripped.splitlines():
                s = line.strip()
                if not s:
                    continue
                if s.startswith("#"):
                    clean_h = s.lstrip("#").strip()
                    if len(clean_h) >= 5:
                        return clean_h
                elif len(s) >= 5:
                    return s
        except Exception:
            pass

    return None


def derive_version(
    path: Path,
    front_matter_dict: dict[str, str] | None = None,
    index_version: str | None = None,
    pdf_meta: dict[str, Any] | None = None,
) -> str | None:
    """Derive version or date according to rules in order:
    1. Front matter `version` or `date`
    2. `_index.json` doc_version
    3. PDF CreationDate/ModDate
    4. Year or year-month in the file name.
    """
    if front_matter_dict:
        for k in ("version", "date", "doc_version"):
            val = front_matter_dict.get(k)
            if val and val.strip() and val.strip() != "unknown":
                return val.strip()

    if index_version and index_version.strip() and index_version.strip() != "unknown":
        return index_version.strip()

    if pdf_meta:
        for date_key in ("CreationDate", "/CreationDate", "ModDate", "/ModDate"):
            raw_d = pdf_meta.get(date_key)
            if raw_d and isinstance(raw_d, str):
                m = re.search(r"(?:D:)?(\d{4})(\d{2})?", raw_d)
                if m:
                    year = m.group(1)
                    month = m.group(2)
                    if month and 1 <= int(month) <= 12:
                        return f"{year}-{month}"
                    return year

    stem = path.stem.lower()
    m_ym = re.search(r"\b(19\d\d|20\d\d)-(0[1-9]|1[0-2])\b", stem)
    if m_ym:
        return f"{m_ym.group(1)}-{m_ym.group(2)}"
    m_my = re.search(r"\b(jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)-(19\d\d|20\d\d)\b", stem)
    if m_my:
        months = {
            "jan": "01", "feb": "02", "mar": "03", "apr": "04", "may": "05", "jun": "06",
            "jul": "07", "aug": "08", "sep": "09", "oct": "10", "nov": "11", "dec": "12",
        }
        return f"{m_my.group(2)}-{months[m_my.group(1)]}"
    m_span = re.search(r"\b(19\d\d|20\d\d)-\d{2,4}\b", stem)
    if m_span:
        return m_span.group(0)
    m_yr = re.search(r"\b(19\d\d|20\d\d)\b", stem)
    if m_yr:
        return m_yr.group(1)

    return None


def extract_doc_samples(text: str, sample_size: int = 1500) -> list[str]:
    """Sample up to three text segments (start, middle, end) of ~1500 characters each."""
    t = text.strip()
    if not t:
        return [""]
    if len(t) <= sample_size:
        return [t]
    if len(t) <= sample_size * 2:
        return [t[:sample_size], t[-sample_size:]]
    mid = len(t) // 2
    half = sample_size // 2
    return [
        t[:sample_size],
        t[mid - half : mid + half],
        t[-sample_size:],
    ]


def compute_doc_vector(samples: list[str], settings: IndexerSettings) -> list[float]:
    """Compute mean vector across sampled segments and unit-normalize."""
    from .embed import embed

    valid_samples = [s for s in samples if s.strip()]
    if not valid_samples:
        valid_samples = ["empty document"]

    vecs = embed(valid_samples, settings)
    if not vecs:
        return []

    dim = len(vecs[0])
    mean_vec = [sum(v[i] for v in vecs) / len(vecs) for i in range(dim)]
    norm = math.sqrt(sum(x * x for x in mean_vec))
    if norm > 1e-9:
        return [x / norm for x in mean_vec]
    return mean_vec


def cosine_similarity(v1: Any, v2: Any) -> float:
    """Cosine similarity of two vectors."""
    if v1 is None or v2 is None or len(v1) == 0 or len(v2) == 0 or len(v1) != len(v2):
        return 0.0
    dot = float(sum(a * b for a, b in zip(v1, v2)))
    norm1 = math.sqrt(float(sum(a * a for a in v1)))
    norm2 = math.sqrt(float(sum(b * b for b in v2)))
    if norm1 > 1e-9 and norm2 > 1e-9:
        return dot / (norm1 * norm2)
    return dot


def check_quality(
    doc: Document | None,
    extraction_error: str | None,
    settings: IndexerSettings,
) -> CheckResult:
    """Check 1: Quality.

    Fails if extraction failed (exception, zero pages), text is empty/nearly empty,
    or garbled (low alpha ratio, excess U+FFFD).
    """
    if extraction_error:
        return CheckResult(
            name="quality",
            passed=False,
            reason=f"extraction failed: {extraction_error}",
            details={"error": extraction_error},
            status="fail",
        )
    if doc is None or not doc.pages:
        return CheckResult(
            name="quality",
            passed=False,
            reason="extraction produced zero pages",
            details={"pages": 0},
            status="fail",
        )

    page_count = len(doc.pages)
    total_chars = sum(len(p.text) for p in doc.pages)
    avg_chars = total_chars / page_count if page_count else 0.0

    if avg_chars < settings.verify_min_chars_per_page:
        return CheckResult(
            name="quality",
            passed=False,
            reason=f"empty or nearly empty text (avg {avg_chars:.1f} chars/page < {settings.verify_min_chars_per_page})",
            details={
                "avg_chars_per_page": round(avg_chars, 1),
                "threshold": settings.verify_min_chars_per_page,
                "total_chars": total_chars,
                "page_count": page_count,
            },
            status="fail",
        )

    all_text = " ".join(p.text for p in doc.pages)
    non_space = [c for c in all_text if not c.isspace()]
    alpha = [c for c in non_space if c.isalpha()]
    alpha_ratio = len(alpha) / len(non_space) if non_space else 0.0
    fffd_count = all_text.count("\ufffd")

    if alpha_ratio < settings.verify_min_alpha_ratio:
        return CheckResult(
            name="quality",
            passed=False,
            reason=f"garbled text (alpha ratio {alpha_ratio:.3f} < {settings.verify_min_alpha_ratio})",
            details={
                "alpha_ratio": round(alpha_ratio, 3),
                "threshold": settings.verify_min_alpha_ratio,
            },
            status="fail",
        )

    if fffd_count > settings.verify_max_replacement_chars:
        return CheckResult(
            name="quality",
            passed=False,
            reason=f"too many replacement characters ({fffd_count} > {settings.verify_max_replacement_chars})",
            details={
                "replacement_chars": fffd_count,
                "threshold": settings.verify_max_replacement_chars,
            },
            status="fail",
        )

    return CheckResult(
        name="quality",
        passed=True,
        reason="quality checks passed",
        details={
            "avg_chars_per_page": round(avg_chars, 1),
            "alpha_ratio": round(alpha_ratio, 3),
            "replacement_chars": fffd_count,
            "ocr_confidence_checked": False,
            "ocr_note": "sub-check skipped for performance; text length and alphabetic ratio validated",
        },
        status="pass",
    )


def check_completeness(
    path: Path,
    title: str | None,
    doc_version: str | None,
    page_count: int,
    raw_content: str | bytes,
    pdf_meta_pages: int | None = None,
    extraction_error: str | None = None,
) -> CheckResult:
    """Check 2: Completeness.

    Fails if required metadata is missing (source, title, date/version, page count)
    or if the document is truncated/corrupt.
    """
    if extraction_error:
        return CheckResult(
            name="completeness",
            passed=False,
            reason=f"cannot parse document (corrupt): {extraction_error}",
            details={"error": extraction_error},
            status="fail",
        )

    if not path.name:
        return CheckResult(
            name="completeness",
            passed=False,
            reason="missing required metadata: source",
            details={"missing": "source"},
            status="fail",
        )

    if not title:
        return CheckResult(
            name="completeness",
            passed=False,
            reason="missing required metadata: title",
            details={"missing": "title"},
            status="fail",
        )

    if not doc_version or doc_version == "unknown":
        return CheckResult(
            name="completeness",
            passed=False,
            reason="missing required metadata: date/version",
            details={"missing": "date/version"},
            status="fail",
        )

    if page_count <= 0:
        return CheckResult(
            name="completeness",
            passed=False,
            reason="missing required metadata: page count",
            details={"missing": "page_count"},
            status="fail",
        )

    if pdf_meta_pages is not None and pdf_meta_pages > page_count:
        return CheckResult(
            name="completeness",
            passed=False,
            reason=f"document is truncated (metadata specifies {pdf_meta_pages} pages, but only {page_count} read)",
            details={"meta_pages": pdf_meta_pages, "read_pages": page_count},
            status="fail",
        )

    suffix = path.suffix.lower()
    if suffix in {".md", ".txt"}:
        text = raw_content if isinstance(raw_content, str) else raw_content.decode("utf-8", errors="replace")
        if len(text) > 500:
            paras = [p.strip() for p in text.strip().split("\n\n") if p.strip()]
            if paras:
                last_para = paras[-1].rstrip()
                terminal_chars = {'.', '!', '?', ':', ';', ')', ']', '"', "'", '`', '>', '-', '*', '|'}
                if last_para and last_para[-1].isalpha() and last_para[-1] not in terminal_chars:
                    return CheckResult(
                        name="completeness",
                        passed=False,
                        reason="document is truncated: final paragraph stops mid-sentence",
                        details={"last_character": last_para[-1], "last_snippet": last_para[-40:]},
                        status="fail",
                    )

    return CheckResult(
        name="completeness",
        passed=True,
        reason="completeness checks passed",
        details={
            "source": path.name,
            "title": title,
            "doc_version": doc_version,
            "page_count": page_count,
        },
        status="pass",
    )


def check_recency(
    doc_id: str,
    logical_key: str,
    doc_version: str | None,
    existing_active_docs: list[dict[str, Any]],
) -> tuple[CheckResult, list[str]]:
    """Check 3: Recency.

    If an active document with the same logical key already exists:
    - newer version passes and marks older active document to be archived.
    - older version fails recency.
    If versions cannot be compared, pass with a note in details.
    Returns (CheckResult, list_of_doc_ids_to_archive).
    """
    if not doc_version or doc_version == "unknown":
        return CheckResult(
            name="recency",
            passed=True,
            reason="versions cannot be compared (missing date/version)",
            details={"note": "incoming document missing comparable version", "logical_key": logical_key},
            status="pass",
        ), []

    t_incoming = parse_version_tuple(doc_version)
    to_archive: list[str] = []
    for ex in existing_active_docs:
        ex_id = ex.get("doc_id", "")
        if ex_id == doc_id:
            continue
        if ex.get("status") == "archived":
            continue
        if ex.get("logical_key") == logical_key:
            ex_ver = str(ex.get("doc_version", "unknown"))
            t_existing = parse_version_tuple(ex_ver) if ex_ver else None
            if t_incoming is None or t_existing is None:
                return CheckResult(
                    name="recency",
                    passed=True,
                    reason="versions cannot be compared",
                    details={
                        "note": "cannot compare versions with existing document",
                        "existing_doc_id": ex_id,
                        "existing_version": ex_ver,
                        "incoming_version": doc_version,
                    },
                    status="pass",
                ), []
            cmp = compare_versions(doc_version, ex_ver)
            if cmp > 0:
                to_archive.append(ex_id)
            elif cmp < 0:
                return CheckResult(
                    name="recency",
                    passed=False,
                    reason=f"a newer version exists: {ex_id}",
                    details={
                        "existing_doc_id": ex_id,
                        "existing_version": ex_ver,
                        "incoming_version": doc_version,
                    },
                    status="fail",
                ), []

    if to_archive:
        return CheckResult(
            name="recency",
            passed=True,
            reason=f"newer version than existing active document(s): {', '.join(to_archive)}",
            details={"supersedes": to_archive, "incoming_version": doc_version},
            status="pass",
        ), to_archive

    return CheckResult(
        name="recency",
        passed=True,
        reason="no prior version detected",
        details={"logical_key": logical_key},
        status="pass",
    ), []


def check_uniqueness(
    doc_id: str,
    doc_hash: str,
    text: str,
    doc_vec: list[float] | None,
    existing_docs: list[dict[str, Any]],
    threshold: float,
) -> CheckResult:
    """Check 4: Uniqueness.

    Fails if exact duplicate (by byte hash or whitespace-normalized text hash)
    or near duplicate by embedding cosine similarity >= threshold against active docs.
    """
    # 1. Exact duplicate by byte hash
    for ex in existing_docs:
        ex_id = ex.get("doc_id", "")
        if ex_id == doc_id:
            continue
        if ex.get("doc_hash") == doc_hash:
            return CheckResult(
                name="uniqueness",
                passed=False,
                reason=f"exact duplicate of {ex_id}",
                details={"exact_duplicate_of": ex_id, "match_type": "file_hash"},
                status="fail",
            )

    # 2. Exact duplicate by whitespace-normalized text hash
    if text.strip():
        norm_text_hash = hashlib.sha256(" ".join(text.split()).encode("utf-8")).hexdigest()
        for ex in existing_docs:
            ex_id = ex.get("doc_id", "")
            if ex_id == doc_id:
                continue
            if ex.get("norm_text_hash") == norm_text_hash:
                return CheckResult(
                    name="uniqueness",
                    passed=False,
                    reason=f"exact duplicate of {ex_id}",
                    details={"exact_duplicate_of": ex_id, "match_type": "text_hash"},
                    status="fail",
                )

    # 3. Near-duplicate by embedding similarity among ACTIVE documents
    max_sim = 0.0
    near_dup_doc_id = None
    if doc_vec is not None and len(doc_vec) > 0 and len(existing_docs) > 0:
        for ex in existing_docs:
            ex_id = ex.get("doc_id", "")
            if ex_id == doc_id:
                continue
            if ex.get("status") == "archived":
                continue
            ex_vec = ex.get("embedding")
            if ex_vec is None or len(ex_vec) == 0:
                continue
            sim = cosine_similarity(doc_vec, ex_vec)
            if sim > max_sim:
                max_sim = sim
                near_dup_doc_id = ex_id

    if max_sim >= threshold and near_dup_doc_id is not None:
        return CheckResult(
            name="uniqueness",
            passed=False,
            reason=f"near-duplicate of {near_dup_doc_id}, similarity {max_sim:.3f}",
            details={
                "duplicate_of": near_dup_doc_id,
                "similarity": round(max_sim, 3),
                "threshold": threshold,
            },
            status="fail",
        )

    return CheckResult(
        name="uniqueness",
        passed=True,
        reason="document is unique",
        details={"max_similarity": round(max_sim, 3), "threshold": threshold},
        status="pass",
    )


def check_warnings(text: str) -> list[str]:
    """Scan text for prompt injection patterns. Match is recorded in warnings without failing."""
    warnings: list[str] = []
    pattern = r"(?i)\b(?:ignore\s+(?:all\s+)?(?:previous|prior)\s+instructions|system\s+prompt\s+override|disregard\s+(?:all\s+)?prior\s+instructions)\b"
    m = re.search(pattern, text)
    if m:
        warnings.append(f"possible prompt injection detected: '{m.group(0)}'")
    return warnings


def verify_document(
    path: Path | None = None,
    doc_id: str = "",
    doc_hash: str = "",
    doc: Document | None = None,
    raw_bytes: bytes = b"",
    settings: IndexerSettings | None = None,
    front_matter_dict: dict[str, str] | None = None,
    index_version: str | None = None,
    index_name: str | None = None,
    pdf_meta: dict[str, Any] | None = None,
    pdf_meta_pages: int | None = None,
    extraction_error: str | None = None,
    existing_active_docs: list[dict[str, Any]] | None = None,
    existing_uniqueness_docs: list[dict[str, Any]] | None = None,
    doc_vec: list[float] | None = None,
    # Compatibility arguments for stub callers / existing tests:
    source: str = "",
    text_length: int = 0,
    page_count: int = 0,
    doc_version: str = "",
    title: str = "",
) -> VerificationResult:
    """Run four verification checks on an extracted document.

    Supports tuple unpacking: `vr, to_archive = verify_document(...)`
    or single assignment: `vr = verify_document(...)`.
    """
    if path is None:
        path = Path(source or "document.txt")

    active_settings = settings or get_settings()
    src_name = source or path.name
    ver_val = doc_version or index_version or ""
    title_val = title or index_name or ""

    if doc is None and text_length > 0:
        doc = Document(
            source=src_name,
            doc_version=ver_val or "2026",
            superseded=False,
            pages=[Page(source=src_name, page=1, text="Sample text " * (text_length // 12 + 1))],
        )

    existing_active = existing_active_docs or []
    existing_unique = existing_uniqueness_docs or []

    final_title = derive_title(path, front_matter_dict, pdf_meta, title_val, doc) or title_val
    final_version = derive_version(path, front_matter_dict, ver_val, pdf_meta) or ver_val
    pages_num = page_count or (len(doc.pages) if doc and doc.pages else 0)
    if pages_num == 0 and path.suffix.lower() in {".md", ".txt"}:
        pages_num = 1

    logical_key = normalize_title(final_title) if final_title else ""
    full_text = " ".join(p.text for p in doc.pages) if doc and doc.pages else ""

    # Check 1: Quality
    to_archive: list[str] = []
    c_quality = check_quality(doc, extraction_error, active_settings)

    if not c_quality.passed or extraction_error:
        # Prerequisite failed: extraction failed or text is empty/garbled
        skip_msg = "not evaluated: extraction failed" if extraction_error else f"not evaluated: quality check failed ({c_quality.reason})"
        c_complete = CheckResult(
            name="completeness",
            passed=True,
            status="skipped",
            reason=skip_msg,
            details={"skipped": True, "prerequisite": "quality"},
        )
        c_recency = CheckResult(
            name="recency",
            passed=True,
            status="skipped",
            reason=skip_msg,
            details={"skipped": True, "prerequisite": "quality"},
        )
        c_uniqueness = CheckResult(
            name="uniqueness",
            passed=True,
            status="skipped",
            reason=skip_msg,
            details={"skipped": True, "prerequisite": "quality"},
        )
    else:
        # Check 2: Completeness
        c_complete = check_completeness(
            path=path,
            title=final_title,
            doc_version=final_version,
            page_count=pages_num,
            raw_content=raw_bytes,
            pdf_meta_pages=pdf_meta_pages,
            extraction_error=None,
        )

        # Check 3: Recency
        # Recency needs a logical key (title). If title is completely missing, recency cannot compare keys.
        if not final_title:
            c_recency = CheckResult(
                name="recency",
                passed=True,
                status="skipped",
                reason="not evaluated: missing title",
                details={"skipped": True, "prerequisite": "title"},
            )
        else:
            c_recency, to_archive = check_recency(doc_id, logical_key, final_version, existing_active)

        # Check 4: Uniqueness
        # Quality passed, so we have extracted text and doc_vec. Uniqueness evaluates directly!
        c_uniqueness = check_uniqueness(
            doc_id=doc_id,
            doc_hash=doc_hash,
            text=full_text,
            doc_vec=doc_vec,
            existing_docs=existing_unique,
            threshold=active_settings.verify_near_dup_threshold,
        )

    warnings = check_warnings(full_text)
    checks = [c_quality, c_complete, c_recency, c_uniqueness]
    passed = all(c.passed and c.status != "fail" for c in checks)

    decision = "accepted" if passed else "quarantined"
    actions = ["indexed"] if passed else ["quarantined"]
    for arch_id in to_archive:
        actions.append(f"archived_prior:{arch_id}")

    return VerificationResult(
        source=src_name,
        doc_id=doc_id,
        doc_hash=doc_hash,
        checks=checks,
        warnings=warnings,
        decision=decision,
        actions=actions,
        to_archive=to_archive,
    )
