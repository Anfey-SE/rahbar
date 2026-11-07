"""Unit tests for the five indexer stages: extract, clean, chunk, embed, store.

These tests run without network, cluster, or real ML models.
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock, patch
import urllib.error

from pipeline.chunk import chunk
from pipeline.clean import clean
from pipeline.config import IndexerSettings
from pipeline.embed import embed
from pipeline.extract import extract
from pipeline.models import Chunk, Document, Page
from pipeline.store import store


def _dummy_settings() -> IndexerSettings:
    return IndexerSettings(
        embed_model="nomic-embed-text",
        chroma_host="localhost",
        chroma_port=8000,
        collection="test-rahbar",
        corpus_dir="/tmp/corpus",
        chunk_size=100,
        chunk_overlap=20,
        ollama_url="http://localhost:11434",
    )


# --- Stage 1: Extract ---


def test_extract_markdown_strips_front_matter(tmp_path: Path) -> None:
    doc_file = tmp_path / "policy.md"
    doc_file.write_text(
        "---\nversion: 2026\nsuperseded: false\n---\n\n# Policy Title\nBody paragraph text here.\n",
        encoding="utf-8",
    )

    doc = extract(str(doc_file), doc_version="2026", superseded=False)
    assert doc.source == "policy.md"
    assert doc.doc_version == "2026"
    assert not doc.superseded
    assert len(doc.pages) == 1
    assert "---" not in doc.pages[0].text
    assert "# Policy Title" in doc.pages[0].text
    assert not doc.pages[0].ocr


# --- Stage 2: Clean ---


def test_clean_removes_repeating_headers_and_page_numbers() -> None:
    pages = [
        Page(source="test.pdf", page=1, text="FAST National University\nPage 1 of 4\nIntro text."),
        Page(source="test.pdf", page=2, text="FAST National University\n5.4 Academic Warning\nStudent rule.\nPage 2 of 4"),
        Page(source="test.pdf", page=3, text="FAST National University\n5.5 Probation Details\nPage 3 of 4"),
        Page(source="test.pdf", page=4, text="FAST National University\nConclusion.\nPage 4 of 4"),
    ]
    raw_doc = Document(source="test.pdf", doc_version="2026", superseded=False, pages=pages)

    cleaned = clean(raw_doc)

    # Repeating line on >50% pages ("FAST National University") must be stripped
    for p in cleaned.pages:
        assert "FAST National University" not in p.text
        assert "Page " not in p.text

    # But clause numbers like 5.4 and 5.5 must be preserved
    assert "5.4 Academic Warning" in cleaned.pages[1].text
    assert "5.5 Probation Details" in cleaned.pages[2].text


# --- Stage 3: Chunk ---


def test_chunk_generates_stable_ids_and_clause_section_range() -> None:
    text = (
        "1. Attendance Regulations\n\n"
        "1.21 Minimum attendance requirement is 80 percent of scheduled classes.\n\n"
        "1.22 A student failing to meet the attendance requirement will be awarded grade F.\n\n"
        "1.23 The Director may condone absence up to 5 percent under extenuating circumstances."
    )
    doc = Document(
        source="handbook.pdf",
        doc_version="2026",
        superseded=False,
        pages=[Page(source="handbook.pdf", page=7, text=text)],
    )

    chunks = chunk(doc, size=50, overlap=10)
    assert len(chunks) >= 1

    # Stable IDs check
    for idx, c in enumerate(chunks, 1):
        assert c.id == f"handbook.pdf:{idx:04d}"
        assert c.source == "handbook.pdf"
        assert c.page == 7
        assert c.doc_version == "2026"
        assert not c.superseded

    # Section clause range check
    c0 = chunks[0]
    assert "1.21" in c0.section
    assert "Attendance" in c0.section or "1." in c0.section


# --- Stage 4: Embed ---


def test_embed_batches_and_validates_dimensions() -> None:
    settings = _dummy_settings()
    sample_texts = [f"Text chunk {i}" for i in range(5)]

    fake_embeddings = [[0.1] * 768 for _ in range(5)]

    mock_resp = MagicMock()
    mock_resp.read.return_value = json.dumps({"embeddings": fake_embeddings}).encode("utf-8")
    mock_resp.__enter__.return_value = mock_resp

    with patch("urllib.request.urlopen", return_value=mock_resp) as mock_urlopen:
        vecs = embed(sample_texts, settings)
        assert len(vecs) == 5
        assert len(vecs[0]) == 768
        assert mock_urlopen.called


def test_embed_retries_on_transient_error() -> None:
    settings = _dummy_settings()
    sample_texts = ["A test chunk"]

    fake_embeddings = [[0.5] * 768]
    success_resp = MagicMock()
    success_resp.read.return_value = json.dumps({"embeddings": fake_embeddings}).encode("utf-8")
    success_resp.__enter__.return_value = success_resp

    with patch(
        "urllib.request.urlopen",
        side_effect=[urllib.error.URLError("Connection refused"), success_resp],
    ), patch("time.sleep") as mock_sleep:
        vecs = embed(sample_texts, settings)
        assert len(vecs) == 1
        assert len(vecs[0]) == 768
        assert mock_sleep.called


# --- Stage 5: Store ---


class _MockCollection:
    def __init__(self, name: str) -> None:
        self.name = name
        self.metadata: dict[str, object] = {}
        self.rows: dict[str, dict[str, object]] = {}

    def get(self, include: list[str] | None = None) -> dict[str, list[str]]:
        return {"ids": list(self.rows.keys())}

    def upsert(self, ids: list[str], documents: list[str],
               embeddings: list[list[float]], metadatas: list[dict[str, object]]) -> None:
        for i, d, e, m in zip(ids, documents, embeddings, metadatas):
            self.rows[i] = {"document": d, "embedding": e, "metadata": m}

    def delete(self, ids: list[str]) -> None:
        for i in ids:
            self.rows.pop(i, None)

    def modify(self, metadata: dict[str, object]) -> None:
        self.metadata = metadata

    def count(self) -> int:
        return len(self.rows)


class _MockClient:
    def __init__(self) -> None:
        self.collections: dict[str, _MockCollection] = {}

    def get_or_create_collection(self, name: str, metadata: dict[str, object] | None = None) -> _MockCollection:
        if name not in self.collections:
            self.collections[name] = _MockCollection(name)
        return self.collections[name]

    def delete_collection(self, name: str) -> None:
        raise AssertionError("delete_collection must never be called during store!")


def test_store_upserts_prunes_and_updates_metadata() -> None:
    settings = _dummy_settings()
    mock_client = _MockClient()

    # Pre-populate collection with an old chunk that should be pruned
    existing_col = mock_client.get_or_create_collection(settings.collection)
    existing_col.rows["stale_id_0001"] = {
        "document": "stale doc",
        "embedding": [0.0] * 768,
        "metadata": {},
    }

    chunks = [
        Chunk(
            id="doc1.pdf:0001",
            text="Chunk one",
            source="doc1.pdf",
            section="1.1 Overview",
            page=1,
            doc_version="2026",
            superseded=False,
        ),
        Chunk(
            id="doc1.pdf:0002",
            text="Chunk two",
            source="doc1.pdf",
            section="1.2 Rules",
            page=2,
            doc_version="2026",
            superseded=False,
        ),
    ]
    vectors = [[0.1] * 768, [0.2] * 768]

    with patch("pipeline.store.get_client", return_value=mock_client):
        count = store(chunks, vectors, settings)

    assert count == 2
    assert "stale_id_0001" not in existing_col.rows
    assert "doc1.pdf:0001" in existing_col.rows
    assert "doc1.pdf:0002" in existing_col.rows

    # Verify collection metadata fields
    meta = existing_col.metadata
    assert meta["embed_model"] == settings.embed_model
    assert meta["dimension"] == 768
    assert "index_version" in meta
    assert isinstance(meta["index_version"], str)
