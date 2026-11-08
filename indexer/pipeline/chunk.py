"""Stage 3: Chunk. Structure-aware, clause-preserving with overlap."""

from __future__ import annotations

from pathlib import Path
import re
from typing import NamedTuple

from .models import Chunk, Document

# Size unit: words as a proxy for tokens (1 word ≈ 1.3 tokens).

# Non-clause GPA or numerical values to ignore from clause numbering
_NON_CLAUSE_VALUES = {
    "2.00", "4.00", "3.00", "1.00", "0.00", "3.67", "3.33", "2.67",
    "2.33", "1.67", "1.33", "3.50", "2.50", "1.50", "0.50",
}

# Regex to detect clause numbers (e.g. 1.21, 5.4, 4.25, 5.8)
_CLAUSE_RE = re.compile(r"(?:^|\s)(?P<num>[1-9]\d*\.\d+(?:\.\d+)*)(?:\s|[.:\-]|\Z)")

_BOILERPLATE_PHRASES = [
    "national university of computer and emerging sciences",
    "chiniot-faisalabad",
    "fast-nuces",
    "fast nuces",
    "table of contents",
    "undergraduate academic rules and regulations",
    "academic rules and regulations",
    "student handbook",
]

_TITLE_KEYWORDS = [
    "academic", "course", "degree", "warning", "attendance", "probation",
    "repeat", "withdraw", "fee", "generative ai", "conduct", "faculty",
    "scope", "purpose", "admission", "registration", "assessment",
    "graduation", "summary", "honesty", "policy", "regulations",
    "eligibility", "evaluation", "procedure", "annex", "declaration",
]


def is_clause_doc(source: str) -> bool:
    """True for documents with numbered clauses (e.g. 2026 handbook, 2023 rules, Prospectus).
    Non-clause documents are Student Handbook, Annual Report, and Start-ups Policy.
    """
    src = source.lower()
    if any(k in src for k in ["student-handbook", "annual-report", "faculty-entrepreneurship", "start-up"]):
        return False
    return True


def _extract_clauses(text: str) -> list[str]:
    """Find all valid clause numbers in the text in order of appearance."""
    clauses: list[str] = []
    seen: set[str] = set()
    for m in _CLAUSE_RE.finditer(text):
        num = m.group("num")
        if num in _NON_CLAUSE_VALUES:
            continue
        if num not in seen:
            seen.add(num)
            clauses.append(num)
    return clauses


def _clause_sort_key(c: str) -> list[int]:
    try:
        return [int(x) for x in c.split(".")]
    except ValueError:
        return [999]


def _is_title_case_heading(s: str) -> bool:
    """Validate true title case or all caps heading, rejecting wrapped sentence fragments."""
    words = s.split()
    if not words or len(words) > 10:
        return False
    if s.isupper():
        return True
    stop_words = {
        "and", "or", "the", "to", "for", "in", "of", "on", "at", "by", "with",
        "a", "an", "as", "&", "/",
    }
    if not words[0][0].isupper():
        return False
    # In a title, every non-stopword must begin with an uppercase letter
    for w in words[1:]:
        clean_w = re.sub(r"[^\w]", "", w)
        if clean_w.lower() in stop_words:
            continue
        if clean_w and clean_w[0].islower():
            return False
    return True


def _is_real_heading(line: str) -> bool:
    """Identify genuine section and chapter headings."""
    s = line.strip()
    if not s or len(s) > 75 or s.endswith("."):
        return False
    if any(b in s.lower() for b in _BOILERPLATE_PHRASES):
        return False
    if s.count(",") > 1:
        return False
    # Markdown headings from extract stage
    if s.startswith("#"):
        return True
    # Numbered chapter headings like "1. Introduction", "4. Registration", "XII. Policy..."
    if re.match(r"^(?:[IVXLCDM]+\.|\d+\.)\s+[A-Za-z]", s):
        return True
    # Meaningful title case subheadings with domain keywords
    if _is_title_case_heading(s):
        s_lower = s.lower()
        if any(w in s_lower for w in _TITLE_KEYWORDS):
            return True
    return False


def _build_section_string(doc_source: str, clauses: list[str], heading: str) -> str:
    """Build section string matching:
    - For non-clause documents: nearest real heading, stripped of leading numeric prefix.
      Never "Heading on page N". Fallback to document title.
    - For clause documents: "5.4-5.7 Academic Warning/Probation" or "4.25 Withdrawal from a Course".
    """
    clean_heading = heading.strip().lstrip("#").strip()
    if any(b in clean_heading.lower() for b in _BOILERPLATE_PHRASES) or not clean_heading:
        clean_heading = ""

    # Non-clause documents (Student Handbook, Annual Report, Start-ups Policy)
    if not is_clause_doc(doc_source):
        # Strip leading numbers: "4. Acceptable Uses..." -> "Acceptable Uses of Generative AI Tools"
        h = re.sub(r"^(?:[IVXLCDM]+\.|\d+\.)\s+", "", clean_heading).strip()
        if not h:
            return Path(doc_source).stem.replace("-", " ").title()
        return h

    # Clause-numbered documents
    if not clauses:
        return clean_heading or "Introduction"

    sorted_clauses = sorted(clauses, key=_clause_sort_key)
    c_start = sorted_clauses[0]
    c_end = sorted_clauses[-1]

    if c_start == c_end:
        clause_prefix = c_start
    else:
        clause_prefix = f"{c_start}-{c_end}"

    if clean_heading:
        if clean_heading.startswith(c_start):
            clean_heading = clean_heading[len(c_start):].strip(" .-:")
        return f"{clause_prefix} {clean_heading}".strip()
    return clause_prefix


class _Paragraph(NamedTuple):
    text: str
    page: int
    is_heading: bool
    heading_text: str


def chunk(doc: Document, size: int, overlap: int) -> list[Chunk]:
    """Structure-aware chunker with clause preservation and overlap.

    Splits text while keeping clause definitions and tables intact when they fit
    under `size` words. Page numbers are tracked exactly per paragraph.
    Section strings include clause numbers and nearest heading for clause docs,
    or the nearest clean heading for non-clause docs.
    IDs are stable: {source}:{cid:04d}.
    """
    paragraphs: list[_Paragraph] = []
    default_title = Path(doc.source).stem.replace("-", " ").title()

    for p in doc.pages:
        raw_blocks = re.split(r"\n\s*\n", p.text)
        for block in raw_blocks:
            lines = block.splitlines()
            current_body: list[str] = []
            for line in lines:
                s = line.strip()
                if _is_real_heading(s):
                    if current_body:
                        b_text = "\n".join(current_body).strip()
                        if b_text:
                            paragraphs.append(
                                _Paragraph(
                                    text=b_text,
                                    page=p.page,
                                    is_heading=False,
                                    heading_text="",
                                )
                            )
                        current_body = []
                    clean_h = s.lstrip("#").strip()
                    paragraphs.append(
                        _Paragraph(
                            text=s,
                            page=p.page,
                            is_heading=True,
                            heading_text=clean_h,
                        )
                    )
                else:
                    current_body.append(line)
            if current_body:
                b_text = "\n".join(current_body).strip()
                if b_text:
                    paragraphs.append(
                        _Paragraph(
                            text=b_text,
                            page=p.page,
                            is_heading=False,
                            heading_text="",
                        )
                    )

    chunks: list[Chunk] = []
    current_heading = default_title if not is_clause_doc(doc.source) else "Introduction"
    current_words: list[str] = []
    current_page = 1
    is_overlap_only = True
    cid = 0

    def emit_current(words: list[str], heading: str, page: int) -> None:
        nonlocal cid
        if not words:
            return
        chunk_text = " ".join(words)
        chunk_clauses = _extract_clauses(chunk_text) if is_clause_doc(doc.source) else []
        sec_str = _build_section_string(doc.source, chunk_clauses, heading)
        cid += 1
        chunks.append(
            Chunk(
                id=f"{doc.source}:{cid:04d}",
                text=chunk_text,
                source=doc.source,
                section=sec_str,
                page=page,
                doc_version=doc.doc_version,
                superseded=doc.superseded,
            )
        )

    for para in paragraphs:
        if para.is_heading:
            # Check for chapter heading transition like "4. Registration"
            chap_match = re.match(r"^(\d+)\.\s+[A-Za-z]", para.heading_text)
            if chap_match and is_clause_doc(doc.source):
                chap_num = chap_match.group(1)
                # If current_words contains clauses starting with this chapter (e.g. 4.1 4.2 4.3),
                # split them so previous chapter text is emitted under previous heading,
                # and new chapter clauses remain under the new heading.
                split_idx = len(current_words)
                for i, w in enumerate(current_words):
                    if re.match(rf"^{chap_num}\.\d+", w):
                        split_idx = i
                        break

                prev_w = current_words[:split_idx]
                new_w = current_words[split_idx:]

                if prev_w:
                    emit_current(prev_w, current_heading, current_page)

                current_heading = para.heading_text
                current_page = para.page
                current_words = new_w
                is_overlap_only = False if new_w else True
                continue
            else:
                if current_words:
                    emit_current(current_words, current_heading, current_page)
                    current_words = []
                    is_overlap_only = True
                current_heading = para.heading_text
                current_page = para.page
                continue

        words = para.text.split()
        if not words:
            continue

        if is_overlap_only:
            current_page = para.page
            is_overlap_only = False

        if len(current_words) + len(words) <= size:
            current_words.extend(words)
        else:
            if current_words:
                emit_current(current_words, current_heading, current_page)
                if overlap > 0 and len(current_words) > overlap:
                    current_words = current_words[-overlap:]
                else:
                    current_words = []
                is_overlap_only = True
                current_page = para.page

            if len(words) > size:
                sentences = re.split(r"(?<=[.!?])\s+", para.text)
                for sent in sentences:
                    swords = sent.split()
                    if not swords:
                        continue
                    if len(current_words) + len(swords) <= size:
                        if is_overlap_only:
                            current_page = para.page
                            is_overlap_only = False
                        current_words.extend(swords)
                    else:
                        if current_words:
                            emit_current(current_words, current_heading, current_page)
                            if overlap > 0 and len(current_words) > overlap:
                                current_words = current_words[-overlap:]
                            else:
                                current_words = []
                            is_overlap_only = True
                            current_page = para.page
                        current_words.extend(swords)
                        is_overlap_only = False
            else:
                current_words.extend(words)
                is_overlap_only = False

    if current_words:
        emit_current(current_words, current_heading, current_page)

    return chunks
