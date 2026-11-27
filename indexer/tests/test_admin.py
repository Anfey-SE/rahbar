"""Unit tests for Admin CLI tools (Assignment 03, Task 3).

All tests run offline with MockClient (no network, no live Chroma).
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

from pipeline.admin import (
    archive_document,
    count_chunks,
    delete_document,
    list_documents,
    restore_document,
)
from pipeline.config import IndexerSettings
from tests.test_ingest_cli import MockClient


def _settings(collection: str = "test-admin") -> IndexerSettings:
    return IndexerSettings(
        embed_model="hash-768",
        chroma_host="localhost",
        chroma_port=8000,
        collection=collection,
        corpus_dir="/corpus",
        chunk_size=400,
        chunk_overlap=50,
        ollama_url="http://localhost:11434",
        embed_provider="hash",
    )


def _setup_mock_collection(client: MockClient, collection_name: str) -> None:
    coll = client.get_or_create_collection(collection_name)
    coll.upsert(
        ids=["doc1:0001", "doc1:0002", "doc2:0001"],
        documents=["Doc 1 text page 1", "Doc 1 text page 2", "Doc 2 text page 1"],
        embeddings=[[0.1] * 768, [0.1] * 768, [0.2] * 768],
        metadatas=[
            {
                "doc_id": "doc1",
                "doc_hash": "hash1",
                "source": "handbook.pdf",
                "title": "Academic Rules Handbook",
                "doc_version": "2026-06",
                "status": "active",
                "superseded": False,
            },
            {
                "doc_id": "doc1",
                "doc_hash": "hash1",
                "source": "handbook.pdf",
                "title": "Academic Rules Handbook",
                "doc_version": "2026-06",
                "status": "active",
                "superseded": False,
            },
            {
                "doc_id": "doc2",
                "doc_hash": "hash2",
                "source": "policy.pdf",
                "title": "Startup Policy",
                "doc_version": "2022-05",
                "status": "active",
                "superseded": False,
            },
        ],
    )


def test_admin_count_and_status(capsys) -> None:  # type: ignore[no-untyped-def]
    client = MockClient()
    settings = _settings("test-count")
    _setup_mock_collection(client, "test-count")

    with patch("pipeline.admin.get_client", return_value=client):
        rc = count_chunks(settings, show_status=True)
        assert rc == 0
        out = capsys.readouterr().out
        assert "total_chunks: 3" in out
        assert "active_chunks: 3" in out
        assert "archived_chunks: 0" in out
        assert "doc1: 2 (active:2)" in out
        assert "doc2: 1 (active:1)" in out


def test_admin_list(capsys) -> None:  # type: ignore[no-untyped-def]
    client = MockClient()
    settings = _settings("test-list")
    _setup_mock_collection(client, "test-list")

    with patch("pipeline.admin.get_client", return_value=client):
        rc = list_documents(settings)
        assert rc == 0
        out = capsys.readouterr().out
        assert "collection: test-list (2 documents)" in out
        assert "Academic Rules Handbook" in out
        assert "Startup Policy" in out


def test_admin_archive_and_restore(tmp_path: Path) -> None:
    client = MockClient()
    settings = _settings("test-arch")
    _setup_mock_collection(client, "test-arch")

    data_dir = tmp_path / "data"
    lake_dir = data_dir / "lake"
    lake_dir.mkdir(parents=True)
    f = lake_dir / "handbook.pdf"
    f.write_text("dummy", encoding="utf-8")

    with patch("pipeline.admin.get_client", return_value=client):
        # Archive doc1
        rc = archive_document(settings, doc_id="doc1", data_dir=data_dir)
        assert rc == 0

        # Check metadata updated in collection
        coll = client.get_collection("test-arch")
        chunks = coll.get(where={"doc_id": "doc1"}, include=["metadatas"])
        for m in chunks["metadatas"]:
            assert m["status"] == "archived"
            assert m["superseded"] is True

        # Check file moved to archive
        archived_file = data_dir / "archive" / "doc1" / "hash1" / "handbook.pdf"
        assert archived_file.is_file()
        assert not f.is_file()

        # Restore doc1
        rc2 = restore_document(settings, doc_id="doc1", data_dir=data_dir)
        assert rc2 == 0

        # Check restored metadata
        chunks2 = coll.get(where={"doc_id": "doc1"}, include=["metadatas"])
        for m in chunks2["metadatas"]:
            assert m["status"] == "active"
            assert m["superseded"] is False
        assert f.is_file()


def test_admin_restore_conflict_refusal(tmp_path: Path) -> None:
    """Restoring an archived document when an active doc with same key exists is refused without --force."""
    client = MockClient()
    settings = _settings("test-conflict")
    coll = client.get_or_create_collection("test-conflict")

    # Add active doc3 (newer version of Academic Rules Handbook)
    # and archived doc1 (older version of Academic Rules Handbook)
    coll.upsert(
        ids=["doc1:0001", "doc3:0001"],
        documents=["Old", "New"],
        embeddings=[[0.1] * 768, [0.1] * 768],
        metadatas=[
            {
                "doc_id": "doc1",
                "doc_hash": "hash1",
                "source": "handbook-old.pdf",
                "title": "Academic Rules Handbook",
                "doc_version": "2026-06",
                "status": "archived",
                "superseded": True,
            },
            {
                "doc_id": "doc3",
                "doc_hash": "hash3",
                "source": "handbook-new.pdf",
                "title": "Academic Rules Handbook",
                "doc_version": "2026-12",
                "status": "active",
                "superseded": False,
            },
        ],
    )

    with patch("pipeline.admin.get_client", return_value=client):
        # Refuses without force
        rc = restore_document(settings, doc_id="doc1", force=False)
        assert rc == 1

        # Succeeds with force
        rc_force = restore_document(settings, doc_id="doc1", force=True)
        assert rc_force == 0


def test_admin_delete(tmp_path: Path) -> None:
    client = MockClient()
    settings = _settings("test-del")
    _setup_mock_collection(client, "test-del")

    data_dir = tmp_path / "data"
    lake_dir = data_dir / "lake"
    lake_dir.mkdir(parents=True)
    f = lake_dir / "policy.pdf"
    f.write_text("policy bytes", encoding="utf-8")

    with patch("pipeline.admin.get_client", return_value=client):
        rc = delete_document(settings, doc_id="doc2", data_dir=data_dir)
        assert rc == 0

        coll = client.get_collection("test-del")
        assert coll.count() == 2  # doc2's chunk deleted
        remaining = coll.get(where={"doc_id": "doc2"})
        assert len(remaining["ids"]) == 0
        assert not f.is_file()
