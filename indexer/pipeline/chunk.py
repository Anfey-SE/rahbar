"""Stage 3: Chunk. Structure-aware, with overlap."""

from __future__ import annotations

import re

from .models import Chunk, Document

_SECTION_RE = re.compile(
    r"^(#{1,4}\s+.+|[A-Z][A-Z\s\d\.]{4,}:?\s*)$", re.M
)


def _tokenise(text: str) -> list[str]:
    """Word-based tokenisation (1 token ≈ 1 word)."""
    return text.split()


def chunk(doc: Document, size: int, overlap: int) -> list[Chunk]:
    """Split on section headings, then slide a window of `size` tokens.

    Each chunk records the section heading it falls under, its page number,
    doc_version and superseded flag.
    """
    # Build a single text with page-boundary markers so we can track page numbers
    page_texts: list[tuple[int, str]] = [(p.page, p.text) for p in doc.pages]
    full_text = "\n".join(text for _, text in page_texts)

    # Build a character-offset → page-number lookup
    page_map: list[tuple[int, int]] = []  # (start_char, page_num)
    offset = 0
    for page_num, text in page_texts:
        page_map.append((offset, page_num))
        offset += len(text) + 1

    def char_to_page(pos: int) -> int:
        result = 1
        for start, num in page_map:
            if pos >= start:
                result = num
        return result

    # Find section boundaries
    section_spans: list[tuple[str, int, int]] = []
    current_section = "Introduction"
    current_start = 0
    for m in _SECTION_RE.finditer(full_text):
        if m.start() > current_start:
            section_spans.append((current_section, current_start, m.start()))
        current_section = m.group(0).strip().lstrip("#").strip()
        current_start = m.end()
    section_spans.append((current_section, current_start, len(full_text)))

    chunks: list[Chunk] = []
    cid = 0
    step = max(1, size - overlap)

    for section, start, end in section_spans:
        section_text = full_text[start:end].strip()
        if not section_text:
            continue
        tokens = _tokenise(section_text)
        section_char_start = start

        i = 0
        while i < len(tokens):
            window = tokens[i: i + size]
            text = " ".join(window)
            # Approximate char position for page lookup
            approx_char = section_char_start + len(" ".join(tokens[:i]))
            page = char_to_page(approx_char)
            cid += 1
            chunk_id = f"{doc.source}:{cid:04d}"
            chunks.append(Chunk(
                id=chunk_id,
                text=text,
                source=doc.source,
                section=section,
                page=page,
                doc_version=doc.doc_version,
                superseded=doc.superseded,
            ))
            i += step

    return chunks
