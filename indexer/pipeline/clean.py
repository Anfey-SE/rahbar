"""Stage 2: Clean. Remove artefacts without destroying structure."""

from __future__ import annotations

import re

from .models import Document, Page

_HEADER_FOOTER = re.compile(
    r"(Page\s+\d+\s+of\s+\d+|^\s*\d+\s*$|FAST.?NUCES\s+Confidential|www\.\S+\.pk)",
    re.I | re.M,
)
_NOISE_RUNS = re.compile(r"[|_\-]{4,}")
_MULTI_BLANK = re.compile(r"\n{3,}")


def _clean_text(text: str) -> str:
    text = _HEADER_FOOTER.sub("", text)
    text = _NOISE_RUNS.sub("", text)
    text = _MULTI_BLANK.sub("\n\n", text)
    return text.strip()


def clean(doc: Document) -> Document:
    """Strip headers/footers and noise; keep headings and section boundaries."""
    cleaned = [
        Page(source=p.source, page=p.page, text=_clean_text(p.text), ocr=p.ocr)
        for p in doc.pages
    ]
    return Document(source=doc.source, doc_version=doc.doc_version,
                    superseded=doc.superseded, pages=cleaned)
