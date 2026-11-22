"""The shapes passed between stages. Metadata fields are required (section 3.1)."""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class Page:
    """Text of one page, as extraction produced it."""

    source: str              # file name, e.g. Academic_Rules_Handbook_2026.pdf
    page: int                # 1-based
    text: str
    ocr: bool = False        # True when this page had no text layer and was OCR'd


@dataclass
class Document:
    source: str
    doc_version: str         # e.g. "2026-06"; from corpus/sources.yaml
    superseded: bool         # True for an older version of a policy
    pages: list[Page] = field(default_factory=list)
    # New fields for Assignment 03
    doc_id: str = ""         # sha256(lower(stem))[:16]
    doc_hash: str = ""       # sha256(file bytes)
    title: str = ""
    page_count: int = 0
    file_path: str = ""      # original input path


@dataclass
class Chunk:
    id: str                  # stable: f"{doc_id}:{n:04d}"
    text: str
    source: str
    section: str             # the heading this chunk sits under
    page: int
    doc_version: str
    superseded: bool
    # New fields for Assignment 03
    doc_id: str = ""
    doc_hash: str = ""
    title: str = ""
    page_count: int = 0
    status: str = "active"   # "active" or "archived"

    def metadata(self) -> dict[str, str | int | bool]:
        """Chroma metadata values must be str, int, float or bool."""
        return {
            "source": self.source,
            "section": self.section,
            "page": self.page,
            "doc_version": self.doc_version,
            "superseded": self.superseded,
            "doc_id": self.doc_id,
            "doc_hash": self.doc_hash,
            "title": self.title,
            "page_count": self.page_count,
            "status": self.status,
        }
