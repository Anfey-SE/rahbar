"""Tests for the ingestion CLI (Assignment 03).

No network, no model: uses a fake embedder and in-memory Chroma mock.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest

from pipeline.admin import count_chunks
from pipeline.config import IndexerSettings
from pipeline.run import (
    _doc_id_from_stem,
    _ensure_dirs,
    _front_matter,
    _sha256_hex,
    discover,
    main,
    run_ingest,
)
from pipeline.verify import CheckResult, VerificationResult, verify_document

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _test_settings(collection: str = "test-ingest") -> IndexerSettings:
    return IndexerSettings(
        embed_model="hash-768",
        chroma_host="localhost",
        chroma_port=8000,
        collection=collection,
        corpus_dir="/tmp/corpus",
        chunk_size=400,
        chunk_overlap=50,
        ollama_url="http://localhost:11434",
        embed_provider="hash",
    )


class MockCollection:
    """In-memory Chroma collection mock supporting the operations used by run.py."""

    def __init__(self, name: str) -> None:
        self.name = name
        self._meta: dict[str, object] = {}
        self._rows: dict[str, dict[str, Any]] = {}

    def get(
        self,
        where: dict[str, str] | None = None,
        include: list[str] | None = None,
    ) -> dict[str, list[Any]]:
        ids: list[str] = []
        metas: list[dict[str, Any]] = []
        docs: list[str] = []
        for rid, row in self._rows.items():
            m = row.get("metadata", {})
            if where:
                match = all(m.get(k) == v for k, v in where.items())
                if not match:
                    continue
            ids.append(rid)
            metas.append(m)
            docs.append(row.get("document", ""))
        result: dict[str, list[Any]] = {"ids": ids}
        inc = include or []
        if "metadatas" in inc:
            result["metadatas"] = metas
        if "documents" in inc:
            result["documents"] = docs
        return result

    def upsert(
        self,
        ids: list[str],
        documents: list[str],
        embeddings: list[list[float]],
        metadatas: list[dict[str, Any]],
    ) -> None:
        for i, d, e, m in zip(ids, documents, embeddings, metadatas):
            self._rows[i] = {"document": d, "embedding": e, "metadata": m}

    def delete(self, ids: list[str]) -> None:
        for i in ids:
            self._rows.pop(i, None)

    def update(
        self,
        ids: list[str],
        metadatas: list[dict[str, Any]] | None = None,
        documents: list[str] | None = None,
        embeddings: list[list[float]] | None = None,
    ) -> None:
        for idx, cid in enumerate(ids):
            if cid in self._rows:
                if metadatas and idx < len(metadatas):
                    self._rows[cid]["metadata"].update(metadatas[idx])
                if documents and idx < len(documents):
                    self._rows[cid]["document"] = documents[idx]
                if embeddings and idx < len(embeddings):
                    self._rows[cid]["embedding"] = embeddings[idx]

    def modify(self, metadata: dict[str, object]) -> None:
        self._meta.update(metadata)

    def count(self) -> int:
        return len(self._rows)


class MockClient:
    """In-memory Chroma client mock."""

    def __init__(self) -> None:
        self.collections: dict[str, MockCollection] = {}

    def get_or_create_collection(
        self, name: str, metadata: dict[str, object] | None = None
    ) -> MockCollection:
        if name not in self.collections:
            self.collections[name] = MockCollection(name)
        return self.collections[name]

    def get_collection(self, name: str) -> MockCollection:
        if name not in self.collections:
            raise ValueError(f"Collection {name} not found")
        return self.collections[name]

    def delete_collection(self, name: str) -> None:
        self.collections.pop(name, None)


# ---------------------------------------------------------------------------
# Tests: utility functions
# ---------------------------------------------------------------------------

def test_doc_id_from_stem() -> None:
    """doc_id is first 16 hex chars of sha256(lower(stem))."""
    did = _doc_id_from_stem("Academic-Rules-2026")
    assert len(did) == 16
    assert all(c in "0123456789abcdef" for c in did)
    # Deterministic
    assert did == _doc_id_from_stem("Academic-Rules-2026")
    # Case insensitive
    assert did == _doc_id_from_stem("academic-rules-2026")


def test_sha256_hex() -> None:
    h = _sha256_hex(b"hello")
    assert len(h) == 64
    assert h == _sha256_hex(b"hello")
    assert h != _sha256_hex(b"world")


def test_front_matter(tmp_path: Path) -> None:
    f = tmp_path / "doc.md"
    f.write_text("---\ndoc_version: 2026\nsuperseded: false\n---\n# Title\nBody.\n")
    meta = _front_matter(f)
    assert meta["doc_version"] == "2026"
    assert meta["superseded"] == "false"


def test_front_matter_missing(tmp_path: Path) -> None:
    f = tmp_path / "doc.md"
    f.write_text("# No front matter\nJust text.\n")
    meta = _front_matter(f)
    assert meta == {}


def test_discover_with_index(tmp_path: Path) -> None:
    idx = [{"file": "a.pdf", "doc_version": "2026", "superseded": False}]
    (tmp_path / "_index.json").write_text(json.dumps(idx))
    (tmp_path / "a.pdf").write_bytes(b"%PDF-fake")
    result = discover(tmp_path, tmp_path / "_index.json")
    assert len(result) == 1
    assert result[0][1] == "2026"


def test_discover_without_index(tmp_path: Path) -> None:
    (tmp_path / "rules.md").write_text(
        "---\ndoc_version: 2026\nsuperseded: false\n---\n# Rules\n"
    )
    (tmp_path / "notes.txt").write_text("plain text")
    (tmp_path / "skip.csv").write_text("a,b,c")
    result = discover(tmp_path)
    assert len(result) == 2  # .md and .txt only
    sources = {r[0].name for r in result}
    assert "rules.md" in sources
    assert "notes.txt" in sources
    assert "skip.csv" not in sources


def test_ensure_dirs(tmp_path: Path) -> None:
    dirs = _ensure_dirs(tmp_path)
    for name in ("lake", "quarantine", "archive", "reports", "traces", "results"):
        assert (tmp_path / name).is_dir()
        assert name in dirs


# ---------------------------------------------------------------------------
# Tests: verification stub
# ---------------------------------------------------------------------------

def test_verify_stub_all_pass() -> None:
    vr = verify_document(
        source="test.md", doc_id="abc123", doc_hash="deadbeef",
        text_length=1000, page_count=5, doc_version="2026", title="Test Doc",
    )
    assert vr.passed
    assert len(vr.checks) == 4
    for c in vr.checks:
        assert c.passed
        assert c.reason


def test_verify_result_to_dict() -> None:
    vr = verify_document(
        source="test.md", doc_id="abc", doc_hash="def",
        text_length=100, page_count=1, doc_version="1", title="T",
    )
    d = vr.to_dict()
    assert d["source"] == "test.md"
    assert d["passed"] is True
    checks = d["checks"]
    assert isinstance(checks, list)
    assert len(checks) == 4


# ---------------------------------------------------------------------------
# Tests: full ingestion pipeline (fake embedder, mock Chroma)
# ---------------------------------------------------------------------------

def _create_test_docs(tmp_path: Path) -> Path:
    """Create a small input directory with 3 markdown files."""
    input_dir = tmp_path / "input"
    input_dir.mkdir()
    for name, version, superseded in [
        ("rules-2026", "2026", False),
        ("rules-2023", "2023", True),
        ("fees-2026", "2026", False),
    ]:
        f = input_dir / f"{name}.md"
        content = (
            f"---\nsource: {name}.md\ndoc_version: {version}\n"
            f"superseded: {'true' if superseded else 'false'}\n---\n"
            f"# {name.replace('-', ' ').title()}\n\n"
            f"This is the content of {name}. It has enough words to form "
            f"at least one chunk for testing purposes. The quick brown fox "
            f"jumps over the lazy dog multiple times to ensure we have "
            f"sufficient text for the chunking algorithm to work with.\n"
        )
        f.write_text(content, encoding="utf-8")
    return input_dir


@pytest.fixture
def mock_chroma() -> MockClient:
    return MockClient()


@pytest.fixture
def _patch_chroma(mock_chroma: MockClient):  # type: ignore[no-untyped-def]
    with patch("pipeline.run.get_client", return_value=mock_chroma), \
         patch("pipeline.store.get_client", return_value=mock_chroma):
        yield mock_chroma


def test_first_run_embeds_all(tmp_path: Path, _patch_chroma: MockClient) -> None:
    """First run on new documents embeds N chunks."""
    input_dir = _create_test_docs(tmp_path)
    data_dir = tmp_path / "data"
    settings = _test_settings()

    counts, vr_path, ir_path = run_ingest(input_dir, data_dir, settings)

    assert counts["documents_seen"] == 3
    assert counts["documents_passed"] == 3
    assert counts["documents_quarantined"] == 0
    assert counts["chunks_embedded"] > 0
    assert counts["chunks_skipped"] == 0
    assert counts["chunks_deleted"] == 0
    # Reports exist
    assert Path(vr_path).exists()
    assert Path(ir_path).exists()
    # Lake has copies
    assert (data_dir / "lake" / "rules-2026.md").exists()


def test_unchanged_rerun_embeds_zero(
    tmp_path: Path, _patch_chroma: MockClient
) -> None:
    """Unchanged rerun embeds 0 and never calls the extractor."""
    input_dir = _create_test_docs(tmp_path)
    data_dir = tmp_path / "data"
    settings = _test_settings()

    # First run
    run_ingest(input_dir, data_dir, settings)

    # Second run on same files
    with patch("pipeline.run._extract_and_chunk") as mock_extract:
        counts2, _, _ = run_ingest(input_dir, data_dir, settings)

    assert counts2["chunks_embedded"] == 0
    assert counts2["chunks_skipped"] > 0
    assert counts2["documents_seen"] == 3
    assert counts2["documents_passed"] == 3
    # Extractor must NOT be called for unchanged docs
    mock_extract.assert_not_called()


def test_changed_document_replaces_own_chunks(
    tmp_path: Path, _patch_chroma: MockClient
) -> None:
    """A changed document replaces only its own chunks."""
    input_dir = _create_test_docs(tmp_path)
    data_dir = tmp_path / "data"
    settings = _test_settings()

    # First run
    counts1, _, _ = run_ingest(input_dir, data_dir, settings)
    assert counts1["chunks_embedded"] > 0

    # Modify one file
    rules_file = input_dir / "rules-2026.md"
    rules_file.write_text(
        "---\nsource: rules-2026.md\ndoc_version: 2026\nsuperseded: false\n---\n"
        "# Rules 2026 Updated\n\nThis is completely new content. "
        "The old content has been replaced with new regulations. "
        "More words here to ensure chunking works properly.\n",
        encoding="utf-8",
    )

    # Second run
    counts2, _, _ = run_ingest(input_dir, data_dir, settings)

    assert counts2["chunks_deleted"] > 0  # old chunks deleted
    assert counts2["chunks_embedded"] > 0  # new chunks embedded
    assert counts2["chunks_skipped"] > 0  # other docs skipped

    # Verify no orphans: total in collection should be consistent
    client = _patch_chroma
    col = client.get_or_create_collection("test-ingest")
    # Every chunk should have valid doc_id
    result = col.get(include=["metadatas"])
    for m in result["metadatas"]:
        assert m.get("doc_id"), "chunk has no doc_id"


def test_prune_deletes_by_doc_id_no_orphans(
    tmp_path: Path, _patch_chroma: MockClient
) -> None:
    """--prune deletes by doc_id with no orphans."""
    input_dir = _create_test_docs(tmp_path)
    data_dir = tmp_path / "data"
    settings = _test_settings()

    # First run with 3 docs
    counts1, _, _ = run_ingest(input_dir, data_dir, settings)
    assert counts1["chunks_embedded"] > 0

    # Remove one file
    (input_dir / "fees-2026.md").unlink()

    # Run with --prune
    counts2, _, _ = run_ingest(input_dir, data_dir, settings, prune=True)

    assert counts2["chunks_deleted"] > 0
    assert counts2["documents_seen"] == 2

    # Check no orphans: every remaining chunk belongs to a doc that was in input
    client = _patch_chroma
    col = client.get_or_create_collection("test-ingest")
    result = col.get(include=["metadatas"])
    fees_doc_id = _doc_id_from_stem("fees-2026")
    for m in result["metadatas"]:
        assert m.get("doc_id") != fees_doc_id, "orphan chunk from deleted doc"


def test_without_prune_missing_docs_kept(
    tmp_path: Path, _patch_chroma: MockClient
) -> None:
    """Without --prune, documents already in the collection but missing from input stay."""
    input_dir = _create_test_docs(tmp_path)
    data_dir = tmp_path / "data"
    settings = _test_settings()

    # First run with 3 docs
    run_ingest(input_dir, data_dir, settings)

    # Remove one file
    (input_dir / "fees-2026.md").unlink()

    # Run WITHOUT prune
    counts2, _, _ = run_ingest(input_dir, data_dir, settings, prune=False)

    assert counts2["chunks_deleted"] == 0  # nothing pruned
    # fees chunks are still in the collection
    client = _patch_chroma
    col = client.get_or_create_collection("test-ingest")
    fees_doc_id = _doc_id_from_stem("fees-2026")
    result = col.get(where={"doc_id": fees_doc_id}, include=["metadatas"])
    assert len(result["ids"]) > 0, "fees chunks should still exist without prune"


def test_quarantine_on_verification_failure(
    tmp_path: Path, _patch_chroma: MockClient
) -> None:
    """A forced-failing document goes to quarantine."""
    input_dir = _create_test_docs(tmp_path)
    data_dir = tmp_path / "data"
    settings = _test_settings()

    # Patch verify to fail for one specific doc
    original_verify = verify_document

    def _failing_verify(
        source: str, doc_id: str, doc_hash: str,
        text_length: int, page_count: int, doc_version: str, title: str,
    ) -> VerificationResult:
        if "rules-2023" in source:
            return VerificationResult(
                source=source,
                doc_id=doc_id,
                checks=[
                    CheckResult("quality", False, "forced failure for test"),
                    CheckResult("completeness", True, "ok"),
                    CheckResult("recency", True, "ok"),
                    CheckResult("uniqueness", True, "ok"),
                ],
            )
        return original_verify(
            source=source, doc_id=doc_id, doc_hash=doc_hash,
            text_length=text_length, page_count=page_count,
            doc_version=doc_version, title=title,
        )

    with patch("pipeline.run.verify_document", side_effect=_failing_verify):
        counts, vr_path, _ = run_ingest(input_dir, data_dir, settings)

    assert counts["documents_quarantined"] == 1
    assert counts["documents_passed"] == 2
    # Quarantine dir has the file and reason
    assert (data_dir / "quarantine" / "rules-2023.md").exists()
    assert (data_dir / "quarantine" / "rules-2023.md.reason.json").exists()
    # Verification report records the failure
    report = json.loads(Path(vr_path).read_text())
    failed = [r for r in report["results"] if not r["passed"]]
    assert len(failed) == 1
    assert "rules-2023" in failed[0]["source"]


# ---------------------------------------------------------------------------
# Tests: count lines and exit codes
# ---------------------------------------------------------------------------

def test_main_prints_count_lines(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """The CLI prints the required count lines."""
    input_dir = _create_test_docs(tmp_path)
    data_dir = tmp_path / "data"

    mock_client = MockClient()
    env_patch = {
        "EMBED_PROVIDER": "hash",
        "EMBED_MODEL": "hash-768",
        "CHROMA_HOST": "localhost",
        "CHROMA_PORT": "8000",
    }
    with patch("pipeline.run.get_client", return_value=mock_client), \
         patch("pipeline.store.get_client", return_value=mock_client), \
         patch.dict(os.environ, env_patch), \
         patch(
             "sys.argv",
             [
                 "pipeline.run",
                 "--input", str(input_dir),
                 "--data-dir", str(data_dir),
                 "--collection", "test-counts",
             ],
         ):
        rc = main()

    assert rc == 0
    out = capsys.readouterr().out
    assert "documents_seen=3" in out
    assert "documents_passed=3" in out
    assert "documents_quarantined=0" in out
    assert "chunks_embedded=" in out
    assert "chunks_skipped=0" in out
    assert "chunks_deleted=0" in out
    assert "verification_report=" in out


def test_main_exit_zero_on_quarantine(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Exit 0 even when documents are quarantined (normal outcome)."""
    input_dir = _create_test_docs(tmp_path)
    data_dir = tmp_path / "data"

    original_verify = verify_document

    def _fail_one(
        source: str, doc_id: str, doc_hash: str,
        text_length: int, page_count: int, doc_version: str, title: str,
    ) -> VerificationResult:
        if "rules-2023" in source:
            return VerificationResult(
                source=source, doc_id=doc_id,
                checks=[CheckResult("quality", False, "bad")],
            )
        return original_verify(
            source=source, doc_id=doc_id, doc_hash=doc_hash,
            text_length=text_length, page_count=page_count,
            doc_version=doc_version, title=title,
        )

    mock_client = MockClient()
    env_patch = {
        "EMBED_PROVIDER": "hash",
        "EMBED_MODEL": "hash-768",
        "CHROMA_HOST": "localhost",
        "CHROMA_PORT": "8000",
    }
    with patch("pipeline.run.get_client", return_value=mock_client), \
         patch("pipeline.store.get_client", return_value=mock_client), \
         patch.dict(os.environ, env_patch), \
         patch("pipeline.run.verify_document", side_effect=_fail_one), \
         patch(
             "sys.argv",
             [
                 "pipeline.run",
                 "--input", str(input_dir),
                 "--data-dir", str(data_dir),
                 "--collection", "test-exit",
             ],
         ):
        rc = main()

    assert rc == 0
    out = capsys.readouterr().out
    assert "documents_quarantined=1" in out


def test_main_exit_nonzero_bad_input_dir(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Exit non-zero when input directory does not exist."""
    with patch(
        "sys.argv",
        [
            "pipeline.run",
            "--input", str(tmp_path / "nonexistent"),
            "--data-dir", str(tmp_path / "data"),
            "--collection", "test-bad",
        ],
    ):
        rc = main()
    assert rc == 1


# ---------------------------------------------------------------------------
# Tests: admin count
# ---------------------------------------------------------------------------

def test_admin_count(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """admin count prints per-doc_id and per-source counts."""
    mock_client = MockClient()
    col = mock_client.get_or_create_collection("test-admin")
    # Seed some data
    col.upsert(
        ids=["abc:0001", "abc:0002", "def:0001"],
        documents=["t1", "t2", "t3"],
        embeddings=[[0.1] * 10, [0.2] * 10, [0.3] * 10],
        metadatas=[
            {"doc_id": "abc", "source": "a.md"},
            {"doc_id": "abc", "source": "a.md"},
            {"doc_id": "def", "source": "b.md"},
        ],
    )

    settings = _test_settings(collection="test-admin")
    with patch("pipeline.admin.get_client", return_value=mock_client):
        rc = count_chunks(settings)

    assert rc == 0
    out = capsys.readouterr().out
    assert "total_chunks: 3" in out
    assert "abc: 2" in out
    assert "def: 1" in out
    assert "a.md: 2" in out
    assert "b.md: 1" in out


# ---------------------------------------------------------------------------
# Tests: chunk metadata fields
# ---------------------------------------------------------------------------

def test_chunk_metadata_fields(tmp_path: Path, _patch_chroma: MockClient) -> None:
    """All required metadata fields are present in stored chunks."""
    input_dir = _create_test_docs(tmp_path)
    data_dir = tmp_path / "data"
    settings = _test_settings()

    run_ingest(input_dir, data_dir, settings)

    col = _patch_chroma.get_or_create_collection("test-ingest")
    result = col.get(include=["metadatas"])
    assert len(result["ids"]) > 0

    required_keys = {
        "source", "section", "page", "doc_version", "superseded",
        "doc_id", "doc_hash", "title", "page_count", "status",
    }
    for m in result["metadatas"]:
        assert required_keys.issubset(m.keys()), f"missing keys: {required_keys - m.keys()}"
        assert m["doc_id"]  # non-empty
        assert m["doc_hash"]  # non-empty
        assert m["status"] in ("active", "archived")


def test_chunk_id_format(tmp_path: Path, _patch_chroma: MockClient) -> None:
    """Chunk IDs follow the format doc_id:NNNN."""
    input_dir = _create_test_docs(tmp_path)
    data_dir = tmp_path / "data"
    settings = _test_settings()

    run_ingest(input_dir, data_dir, settings)

    col = _patch_chroma.get_or_create_collection("test-ingest")
    result = col.get(include=["metadatas"])

    for cid, m in zip(result["ids"], result["metadatas"]):
        doc_id = m["doc_id"]
        # Chunk id must start with the doc_id
        assert cid.startswith(doc_id + ":"), f"bad chunk id: {cid}"
        # The suffix must be a 4-digit number
        suffix = cid.split(":")[-1]
        assert suffix.isdigit() and len(suffix) == 4, f"bad suffix: {suffix}"


# ---------------------------------------------------------------------------
# Tests: reports
# ---------------------------------------------------------------------------

def test_reports_written(tmp_path: Path, _patch_chroma: MockClient) -> None:
    """Verification and ingestion reports are written as JSON."""
    input_dir = _create_test_docs(tmp_path)
    data_dir = tmp_path / "data"
    settings = _test_settings()

    counts, vr_path, ir_path = run_ingest(input_dir, data_dir, settings)

    # Verification report
    vr = json.loads(Path(vr_path).read_text())
    assert "timestamp" in vr
    assert "results" in vr
    assert len(vr["results"]) == 3

    # Ingestion report
    ir = json.loads(Path(ir_path).read_text())
    assert ir["documents_seen"] == 3
    assert ir["chunks_embedded"] == counts["chunks_embedded"]
    assert "verification_report" in ir
    assert "collection" in ir
    assert "embed_model" in ir


# ---------------------------------------------------------------------------
# Tests: CLI backward compatibility & env var defaults
# ---------------------------------------------------------------------------


def test_cli_defaults_from_env(
    tmp_path: Path,
    _patch_chroma: MockClient,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Omitting --input, --data-dir, and --collection falls back to env vars."""
    input_dir = _create_test_docs(tmp_path)
    data_dir = tmp_path / "env_data"

    monkeypatch.setenv("CORPUS_DIR", str(input_dir))
    monkeypatch.setenv("DATA_DIR", str(data_dir))
    monkeypatch.setenv("CHROMA_COLLECTION", "env-collection")
    monkeypatch.setenv("EMBED_PROVIDER", "hash")
    monkeypatch.setenv("EMBED_MODEL", "hash-768")
    monkeypatch.setattr(sys, "argv", ["pipeline.run"])

    rc = main()
    out = capsys.readouterr().out

    assert rc == 0
    assert "documents_seen=3" in out
    assert "documents_passed=3" in out
    assert "chunks_embedded=" in out
    assert _patch_chroma.get_or_create_collection("env-collection").count() > 0


def test_cli_explicit_unwritable_data_dir_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Explicitly passing an unwritable --data-dir fails with exit code 1 and no fallback."""
    input_dir = _create_test_docs(tmp_path)
    bad_dir = tmp_path / "readonly_data"

    monkeypatch.setattr("pipeline.run._is_dir_writable", lambda path: False)
    monkeypatch.setattr(
        sys, "argv", ["pipeline.run", "--input", str(input_dir), "--data-dir", str(bad_dir)]
    )

    rc = main()
    captured = capsys.readouterr()

    assert rc == 1
    assert "data directory is not writable" in captured.err
    assert "falling back" not in captured.out


def test_cli_default_unwritable_data_dir_falls_back(
    tmp_path: Path,
    _patch_chroma: MockClient,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """When --data-dir is omitted and DATA_DIR is unwritable, fall back to /tmp/rahbar-data with warning."""
    from pipeline.run import _FALLBACK_DATA_DIR

    input_dir = _create_test_docs(tmp_path)
    bad_dir = tmp_path / "readonly_env_data"

    monkeypatch.setenv("CORPUS_DIR", str(input_dir))
    monkeypatch.setenv("DATA_DIR", str(bad_dir))
    monkeypatch.setenv("CHROMA_COLLECTION", "fallback-col")
    monkeypatch.setenv("EMBED_PROVIDER", "hash")
    monkeypatch.setenv("EMBED_MODEL", "hash-768")
    monkeypatch.setattr("pipeline.run._is_dir_writable", lambda path: path == _FALLBACK_DATA_DIR)
    monkeypatch.setattr(sys, "argv", ["pipeline.run"])

    rc = main()
    out = capsys.readouterr().out

    assert rc == 0
    assert "falling back to /tmp/rahbar-data" in out
    assert "documents_seen=3" in out


def test_cli_dummy_without_corpus_dir(
    tmp_path: Path,
    _patch_chroma: MockClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """--dummy succeeds even when CORPUS_DIR points to a non-existent directory."""
    monkeypatch.setenv("CORPUS_DIR", str(tmp_path / "missing_corpus"))
    monkeypatch.setenv("CHROMA_COLLECTION", "dummy-col")
    monkeypatch.setattr(sys, "argv", ["pipeline.run", "--dummy"])
    monkeypatch.setattr("pipeline.dummy.write_dummy_index", lambda settings, client: 3)

    rc = main()
    assert rc == 0


def test_hash_embedder_ingestion_and_doc_vectors(
    tmp_path: Path,
    _patch_chroma: MockClient,
) -> None:
    """Hash embedder produces 768-dim chunk and document vectors and supports incremental runs."""
    input_dir = _create_test_docs(tmp_path)
    data_dir = tmp_path / "hash_data"
    settings = IndexerSettings(
        embed_model="hash-768",
        chroma_host="localhost",
        chroma_port=8000,
        collection="hash-col",
        corpus_dir=str(input_dir),
        chunk_size=300,
        chunk_overlap=50,
        ollama_url="http://127.0.0.1:19999",
        embed_provider="hash",
    )

    counts1, _, _ = run_ingest(input_dir, data_dir, settings)
    assert counts1["documents_seen"] == 3
    assert counts1["documents_passed"] == 3
    assert counts1["chunks_embedded"] > 0

    docs_col = _patch_chroma.get_or_create_collection("hash-col__docs")
    assert len(docs_col._rows) == 3
    assert all(len(row["embedding"]) == 768 for row in docs_col._rows.values())

    counts2, _, _ = run_ingest(input_dir, data_dir, settings)
    assert counts2["chunks_embedded"] == 0
    assert counts2["chunks_skipped"] == counts1["chunks_embedded"]
