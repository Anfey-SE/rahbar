"""Stage 1: Extract. PDF (or Markdown/text) to text, page by page."""

from __future__ import annotations

import logging
import re
from pathlib import Path

from .models import Document, Page

log = logging.getLogger("rahbar.indexer.extract")


def _extract_pdf(path: str, doc_version: str, superseded: bool) -> Document:
    try:
        import pdfplumber  # type: ignore[import-untyped]
    except ImportError:
        raise RuntimeError("pdfplumber not installed: pip install pdfplumber") from None from None

    source = Path(path).name
    pages: list[Page] = []
    with pdfplumber.open(path) as pdf:
        for i, pdf_page in enumerate(pdf.pages, 1):
            text = pdf_page.extract_text() or ""
            ocr = len(text.strip()) < 20  # heuristic: very little text → scanned
            if ocr:
                try:
                    import pytesseract  # type: ignore[import-untyped]
                    img = pdf_page.to_image(resolution=200).original
                    text = pytesseract.image_to_string(img)
                    log.info("OCR applied", extra={"path": path, "page": i})
                except Exception as exc:
                    log.warning("OCR failed",
                                extra={"path": path, "page": i, "error": str(exc)})
            pages.append(Page(source=source, page=i, text=text, ocr=ocr))

    return Document(source=source, doc_version=doc_version,
                    superseded=superseded, pages=pages)


def _extract_text(path: str, doc_version: str, superseded: bool) -> Document:
    source = Path(path).name
    text = Path(path).read_text(encoding="utf-8", errors="replace")
    # Strip Markdown front matter (--- ... ---)
    text = re.sub(r"^---\n.*?\n---\n", "", text, flags=re.S)
    return Document(source=source, doc_version=doc_version,
                    superseded=superseded,
                    pages=[Page(source=source, page=1, text=text, ocr=False)])


def extract(path: str, doc_version: str, superseded: bool) -> Document:
    """Return the document's text, page by page."""
    suffix = Path(path).suffix.lower()
    if suffix == ".pdf":
        return _extract_pdf(path, doc_version, superseded)
    elif suffix in {".md", ".txt", ".html"}:
        return _extract_text(path, doc_version, superseded)
    else:
        raise ValueError(f"Unsupported format: {suffix}")
