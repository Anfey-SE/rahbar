"""Stage 3: Chunk. Structure-aware, clause-preserving with overlap."""

from __future__ import annotations

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
    "student handbook",
]


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


def _is_real_heading(line: str) -> bool:
    """Identify genuine section and chapter headings."""
    s = line.strip()
    if not s or len(s) > 75 or s.endswith("."):
        return False
    if any(b in s.lower() for b in _BOILERPLATE_PHRASES):
        return False
    if s.count(",") > 1:
        return False
    # Markdown headings
    if s.startswith("#"):
        return True
    # Numbered chapter headings like "1. Introduction", "4. Registration"
    if re.match(r"^\d+\.\s+[A-Za-z]", s):
        return True
    # Meaningful title case subheadings
    if re.match(r"^[A-Z][A-Za-z0-9\s/&-]{3,65}$", s):
        title_keywords = [
            "academic", "course", "degree", "warning", "attendance", "probation",
            "repeat", "withdraw", "fee", "generative ai", "conduct", "faculty",
            "scope", "purpose", "admission", "registration", "assessment",
            "graduation", "summary", "honesty", "policy", "regulations",
        ]
        if any(w in s.lower() for w in title_keywords):
            return True
    return False


def _build_section_string(clauses: list[str], heading: str) -> str:
    """Build section string matching:
    - If clauses exist: "5.4-5.7 Academic Warning/Probation" or "4.25 Withdrawal from a Course"
    - If no clauses: "Acceptable Uses of Generative AI Tools" or heading
    """
    clean_heading = heading.strip().lstrip("#").strip()
    if any(b in clean_heading.lower() for b in _BOILERPLATE_PHRASES) or not clean_heading:
        clean_heading = ""

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
    Section strings include clause numbers and nearest heading.
    IDs are stable: {source}:{cid:04d}.
    """
    paragraphs: list[_Paragraph] = []
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
    current_heading = "Introduction"
    current_words: list[str] = []
    current_page = 1
    is_overlap_only = True
    cid = 0

    for para in paragraphs:
        if para.is_heading:
            # Emit current chunk before starting a new heading section
            if current_words:
                chunk_text = " ".join(current_words)
                chunk_clauses = _extract_clauses(chunk_text)
                sec_str = _build_section_string(chunk_clauses, current_heading)
                cid += 1
                chunks.append(
                    Chunk(
                        id=f"{doc.source}:{cid:04d}",
                        text=chunk_text,
                        source=doc.source,
                        section=sec_str,
                        page=current_page,
                        doc_version=doc.doc_version,
                        superseded=doc.superseded,
                    )
                )
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

        # If adding this paragraph fits under size
        if len(current_words) + len(words) <= size:
            current_words.extend(words)
        else:
            # Paragraph exceeds current chunk: emit current chunk
            if current_words:
                chunk_text = " ".join(current_words)
                chunk_clauses = _extract_clauses(chunk_text)
                sec_str = _build_section_string(chunk_clauses, current_heading)
                cid += 1
                chunks.append(
                    Chunk(
                        id=f"{doc.source}:{cid:04d}",
                        text=chunk_text,
                        source=doc.source,
                        section=sec_str,
                        page=current_page,
                        doc_version=doc.doc_version,
                        superseded=doc.superseded,
                    )
                )

                # Apply overlap
                if overlap > 0 and len(current_words) > overlap:
                    current_words = current_words[-overlap:]
                else:
                    current_words = []
                is_overlap_only = True
                current_page = para.page

            # If the paragraph itself exceeds size, split by sentences
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
                            chunk_text = " ".join(current_words)
                            chunk_clauses = _extract_clauses(chunk_text)
                            sec_str = _build_section_string(chunk_clauses, current_heading)
                            cid += 1
                            chunks.append(
                                Chunk(
                                    id=f"{doc.source}:{cid:04d}",
                                    text=chunk_text,
                                    source=doc.source,
                                    section=sec_str,
                                    page=current_page,
                                    doc_version=doc.doc_version,
                                    superseded=doc.superseded,
                                )
                            )
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

    # Emit final remaining words
    if current_words:
        chunk_text = " ".join(current_words)
        chunk_clauses = _extract_clauses(chunk_text)
        sec_str = _build_section_string(chunk_clauses, current_heading)
        cid += 1
        chunks.append(
            Chunk(
                id=f"{doc.source}:{cid:04d}",
                text=chunk_text,
                source=doc.source,
                section=sec_str,
                page=current_page,
                doc_version=doc.doc_version,
                superseded=doc.superseded,
            )
        )

    return chunks
