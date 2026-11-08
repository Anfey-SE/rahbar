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
        raise RuntimeError("pdfplumber not installed: pip install pdfplumber") from None

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
            else:
                # Structure-aware line extraction with font and bold detection
                try:
                    words = pdf_page.extract_words(extra_attrs=["fontname", "size"])
                    if words:
                        from collections import Counter

                        sizes = [round(w.get("size", 0), 1) for w in words]
                        size_counts = Counter(sizes)
                        body_size = size_counts.most_common(1)[0][0] if size_counts else 10.0

                        lines_words: list[list[dict]] = []
                        cur_line: list[dict] = []
                        cur_top = None
                        for w in words:
                            if cur_top is None or abs(w["top"] - cur_top) < 3.5:
                                cur_line.append(w)
                                cur_top = w["top"]
                            else:
                                lines_words.append(cur_line)
                                cur_line = [w]
                                cur_top = w["top"]
                        if cur_line:
                            lines_words.append(cur_line)

                        out_lines = []
                        for l_words in lines_words:
                            line_text = " ".join(w["text"] for w in l_words).strip()
                            if not line_text:
                                continue
                            line_size = max(round(w.get("size", 0), 1) for w in l_words)
                            line_fonts = [w.get("fontname", "").lower() for w in l_words]
                            is_bold = any(
                                "bold" in f or "black" in f or "heavy" in f for f in line_fonts
                            )

                            is_heading_font = (line_size >= body_size + 1.5) or (
                                is_bold and line_size >= body_size
                            )
                            # Data rows with multiple numbers are tables, not headings
                            if re.search(r"\d+\s+\d+", line_text):
                                is_heading_font = False

                            is_numbered = bool(
                                re.match(r"^(?:[IVXLCDM]+\.|\d+\.)\s+[A-Za-z]", line_text)
                            )

                            if (
                                (is_heading_font or is_numbered)
                                and len(line_text) < 80
                                and not line_text.endswith(".")
                            ):
                                if not any(
                                    b in line_text.lower()
                                    for b in ["handbook", "annual report", "page", "nuces"]
                                ):
                                    out_lines.append(f"# {line_text}")
                                else:
                                    out_lines.append(line_text)
                            else:
                                out_lines.append(line_text)
                        text = "\n".join(out_lines)
                except Exception as exc:
                    log.warning(
                        "Word extraction with font attributes failed, using extract_text",
                        extra={"error": str(exc)},
                    )

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
