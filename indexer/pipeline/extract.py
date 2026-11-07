"""Stage 1: Extract. PDF (or Markdown/text) to text, page by page."""

from __future__ import annotations

import gc
import logging
import re
from pathlib import Path

from .models import Document, Page

log = logging.getLogger("rahbar.indexer.extract")

_MIN_TEXT_CHARS = 50
_OCR_DPI = 300


def _extract_pdf(path: str, doc_version: str, superseded: bool) -> Document:
    try:
        import pdfplumber  # type: ignore[import-untyped]
    except ImportError:
        raise RuntimeError("pdfplumber not installed: pip install pdfplumber")

    source = Path(path).name
    pages: list[Page] = []
    ocr_page_count = 0

    with pdfplumber.open(path) as pdf:
        total_pages = len(pdf.pages)
        for i, pdf_page in enumerate(pdf.pages, 1):
            text = pdf_page.extract_text() or ""
            ocr = len(text.strip()) < _MIN_TEXT_CHARS

            if ocr:
                ocr_text = ""
                try:
                    from pdf2image import convert_from_path  # type: ignore[import-untyped]
                    import pytesseract  # type: ignore[import-untyped]

                    images = convert_from_path(
                        path, first_page=i, last_page=i, dpi=_OCR_DPI
                    )
                    if images:
                        ocr_text = pytesseract.image_to_string(images[0]) or ""
                        del images
                        gc.collect()
                    log.info("applied OCR to page", extra={"source": source, "page": i, "chars": len(ocr_text)})
                except Exception as exc:
                    log.warning(
                        "OCR failed for page",
                        extra={"source": source, "page": i, "error": str(exc)},
                    )
                text = ocr_text
                ocr_page_count += 1

            pages.append(Page(source=source, page=i, text=text, ocr=ocr))

    if ocr_page_count > 0:
        log.info(
            "document extraction required OCR",
            extra={"source": source, "total_pages": total_pages, "ocr_pages": ocr_page_count},
        )
    else:
        log.info(
            "document extraction completed without OCR",
            extra={"source": source, "total_pages": total_pages},
        )

    return Document(
        source=source,
        doc_version=doc_version,
        superseded=superseded,
        pages=pages,
    )


def _extract_text(path: str, doc_version: str, superseded: bool) -> Document:
    source = Path(path).name
    text = Path(path).read_text(encoding="utf-8", errors="replace")
    # Strip Markdown front matter (--- ... ---) handling both CRLF and LF
    text = re.sub(r"^---\r?\n.*?\r?\n---\r?\n?", "", text, flags=re.S)
    return Document(
        source=source,
        doc_version=doc_version,
        superseded=superseded,
        pages=[Page(source=source, page=1, text=text, ocr=False)],
    )


def extract(path: str, doc_version: str, superseded: bool) -> Document:
    """Return the document's text, page by page."""
    suffix = Path(path).suffix.lower()
    if suffix == ".pdf":
        return _extract_pdf(path, doc_version, superseded)
    elif suffix in {".md", ".txt", ".html"}:
        return _extract_text(path, doc_version, superseded)
    else:
        raise ValueError(f"Unsupported format: {suffix}")
