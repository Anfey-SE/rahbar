"""Stage 2: Clean. Remove repeating artefacts without destroying structure."""

from __future__ import annotations

import re
from collections import Counter

from .models import Document, Page

_PAGE_NUM_RE = re.compile(r"^Page\s+\d+(\s+of\s+\d+)?$", re.IGNORECASE)
_BARE_NUM_RE = re.compile(r"^\d+$")
_HANDBOOK_PAGE_RE = re.compile(r"^(?:Student Handbook \d{4}\s+\d+|\d+\s+Student Handbook \d{4})$", re.IGNORECASE)
_URL_RE = re.compile(r"^(https?://)?www\.\S+\.pk/?$", re.IGNORECASE)
_MULTI_BLANK_RE = re.compile(r"\n{3,}")
_CLAUSE_PROTECT_RE = re.compile(r"^\d+(\.\d+)*\s+[A-Za-z]")


def _is_protected(line: str) -> bool:
    """Deliberately preserved elements:
    1. Clause numbers with titles (e.g. 1.21 Attendance, 5.4 Academic Warning).
    2. Table rows (lines containing table separators '|' or multiple tab/space columns).
    3. Markdown headings ('#' prefixes).
    """
    s = line.strip()
    if not s:
        return False
    stripped = s.lstrip("# ").strip()
    if _PAGE_NUM_RE.match(stripped) or _BARE_NUM_RE.match(stripped) or _HANDBOOK_PAGE_RE.match(stripped):
        return False
    if s.startswith("#"):
        return True
    if "|" in s:
        return True
    if _CLAUSE_PROTECT_RE.match(s):
        return True
    return False


def clean(doc: Document) -> Document:
    """Strip repeating headers/footers and noise across pages.

    Identifies lines that repeat on more than half the pages of a multi-page
    document (e.g. university name running header, website URL, confidential notice),
    plus explicit page numbers and footers.

    Deliberately preserves:
    - Clause numbers and section headings (vital for retrieval matching).
    - Table rows and data formatting.
    """
    total_pages = len(doc.pages)
    repeating_lines: set[str] = set()

    if total_pages > 2:
        line_page_counts: Counter[str] = Counter()
        for p in doc.pages:
            unique_lines = {line.strip() for line in p.text.splitlines() if line.strip()}
            for line in unique_lines:
                line_page_counts[line] += 1

        half_pages = total_pages / 2.0
        for line, count in line_page_counts.items():
            if count > half_pages and not _is_protected(line):
                repeating_lines.add(line)

    cleaned_pages: list[Page] = []
    for p in doc.pages:
        kept_lines: list[str] = []
        for line in p.text.splitlines():
            s = line.strip()
            if not s:
                kept_lines.append("")
                continue

            # Check if line is a detected repeating header/footer
            if s in repeating_lines:
                continue

            # Strip bare page numbers, handbook page headers, or URLs
            s_clean = s.lstrip("# ").strip()
            if _PAGE_NUM_RE.match(s_clean) or _BARE_NUM_RE.match(s_clean) or _HANDBOOK_PAGE_RE.match(s_clean) or _URL_RE.match(s_clean):
                continue

            kept_lines.append(line)

        cleaned_text = "\n".join(kept_lines)
        cleaned_text = _MULTI_BLANK_RE.sub("\n\n", cleaned_text).strip()
        cleaned_pages.append(
            Page(source=p.source, page=p.page, text=cleaned_text, ocr=p.ocr)
        )

    return Document(
        source=doc.source,
        doc_version=doc.doc_version,
        superseded=doc.superseded,
        pages=cleaned_pages,
    )
