"""Ingestion CLI entry point (Assignment 03, Part A).

    python -m pipeline.run --input <dir> --data-dir <dir> --collection <name> [--prune]

Chroma and Ollama settings come from env vars. --collection overrides CHROMA_COLLECTION.

Exit code 0 when the run completed (quarantined documents are a normal outcome);
non-zero on any real failure (Chroma unreachable, embedding failure, bad input dir).

Containers run non-root with a read-only root filesystem: writes only under
--data-dir and /tmp (TMPDIR is set for OCR temp files).
"""

from __future__ import annotations

import argparse
import hashlib
import json
import logging
import os
import shutil
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .config import IndexerSettings, get_settings
from .models import Chunk, Document
from .store import get_client
from .verify import (
    compute_doc_vector,
    derive_title,
    derive_version,
    extract_doc_samples,
    normalize_title,
    verify_document,
)

log = logging.getLogger("rahbar.indexer")


def _utc_stamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def _log(msg: str, **fields: object) -> None:
    print(
        json.dumps(
            {
                "ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                "logger": "rahbar.indexer",
                "msg": msg,
                **fields,
            }
        ),
        flush=True,
    )


def _sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _doc_id_from_stem(stem: str) -> str:
    """sha256 of the lower-cased file stem, first 16 hex characters."""
    return _sha256_hex(stem.lower().encode("utf-8"))[:16]


def _front_matter(path: Path) -> dict[str, str]:
    """Read `key: value` lines between leading '---' markers."""
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except Exception:
        return {}
    if not lines or lines[0].strip() != "---":
        return {}
    meta: dict[str, str] = {}
    for line in lines[1:]:
        if line.strip() == "---":
            break
        key, _, value = line.partition(":")
        meta[key.strip()] = value.strip()
    return meta


def discover(
    input_dir: Path | str, index_path: Path | None = None
) -> list[tuple[Path, str, bool]]:
    """List (path, doc_version, superseded) for every document to index.

    The old corpus/raw/_index.json still gives doc_version and superseded
    for our six PDFs when it exists; otherwise derive them from front matter.
    """
    root = Path(input_dir)
    index_entries: dict[str, tuple[str, bool]] = {}

    # Check corpus/raw/_index.json as baseline for the six corpus PDFs
    for candidate in (
        Path("corpus/raw/_index.json"),
        Path("../corpus/raw/_index.json"),
        root.parent / "corpus" / "raw" / "_index.json",
        root / "_index.json",
    ):
        if candidate.exists():
            try:
                for e in json.loads(candidate.read_text(encoding="utf-8")):
                    index_entries[e["file"]] = (
                        str(e.get("doc_version", "unknown")),
                        bool(e.get("superseded", False)),
                    )
                break
            except Exception:
                pass

    if index_path and index_path.exists():
        try:
            for e in json.loads(index_path.read_text(encoding="utf-8")):
                index_entries[e["file"]] = (
                    str(e.get("doc_version", "unknown")),
                    bool(e.get("superseded", False)),
                )
        except Exception:
            pass

    found = []
    for path in sorted(root.iterdir()):
        if path.suffix.lower() not in {".pdf", ".md", ".txt"}:
            continue
        if path.name.lower().startswith("readme") or path.name.lower().startswith("planted"):
            continue
        if path.name in index_entries:
            ver, sup = index_entries[path.name]
            found.append((path, ver, sup))
        elif path.suffix.lower() in {".md", ".txt"}:
            meta = _front_matter(path)
            found.append(
                (
                    path,
                    meta.get("doc_version") or meta.get("version") or meta.get("date", "unknown"),
                    meta.get("superseded", "false").lower() == "true",
                )
            )
        else:
            found.append((path, "unknown", False))
    return found


def _lookup_index_name(path: Path, input_dir: Path) -> str | None:
    """Lookup document name from _index.json if present."""
    for candidate in (
        input_dir / "_index.json",
        Path("corpus/raw/_index.json"),
        Path("../corpus/raw/_index.json"),
        input_dir.parent / "corpus" / "raw" / "_index.json",
    ):
        if candidate.exists():
            try:
                for e in json.loads(candidate.read_text(encoding="utf-8")):
                    if e.get("file") == path.name:
                        return str(e.get("name", ""))
            except Exception:
                pass
    return None


def _ensure_dirs(data_dir: Path) -> dict[str, Path]:
    """Create the required subdirectories under --data-dir."""
    dirs = {}
    for name in ("lake", "quarantine", "archive", "reports", "traces", "results"):
        d = data_dir / name
        d.mkdir(parents=True, exist_ok=True)
        dirs[name] = d
    return dirs


def _get_collection(settings: IndexerSettings) -> Any:
    """Get or create the main Chroma collection."""
    client = get_client(settings)
    return client.get_or_create_collection(
        name=settings.collection,
        metadata={"hnsw:space": "cosine"},
    )


def _get_docs_collection(settings: IndexerSettings) -> Any:
    """Get or create the secondary Chroma collection for document embeddings."""
    client = get_client(settings)
    return client.get_or_create_collection(
        name=f"{settings.collection}__docs",
        metadata={"hnsw:space": "cosine"},
    )


def _get_stored_doc_hashes(collection: Any, doc_id: str) -> tuple[str | None, int]:
    """Query Chroma for the stored doc_hash and chunk count for a doc_id.

    Returns (doc_hash, chunk_count). If the doc_id is not in the collection,
    returns (None, 0).
    """
    result = collection.get(
        where={"doc_id": doc_id},
        include=["metadatas"],
    )
    ids = result.get("ids", [])
    if not ids:
        return None, 0
    metadatas = result.get("metadatas", [])
    stored_hash = metadatas[0].get("doc_hash", "") if metadatas else ""
    return stored_hash, len(ids)


def _delete_old_chunks(collection: Any, doc_id: str, new_doc_hash: str) -> int:
    """Delete old chunks for a doc_id whose doc_hash does not match the new hash."""
    result = collection.get(
        where={"doc_id": doc_id},
        include=["metadatas"],
    )
    ids = result.get("ids", [])
    metadatas = result.get("metadatas", [])
    stale_ids = [
        cid for cid, m in zip(ids, metadatas) if m.get("doc_hash") != new_doc_hash
    ]
    if stale_ids:
        collection.delete(ids=stale_ids)
    return len(stale_ids)


def _delete_doc_chunks(collection: Any, doc_id: str) -> int:
    """Delete all chunks for a doc_id. Returns the count deleted."""
    result = collection.get(
        where={"doc_id": doc_id},
        include=[],
    )
    ids = result.get("ids", [])
    if ids:
        collection.delete(ids=ids)
    return len(ids)


def _get_all_doc_ids(collection: Any) -> set[str]:
    """Return the set of distinct doc_ids in the collection."""
    result = collection.get(include=["metadatas"])
    metadatas = result.get("metadatas", [])
    return {str(m.get("doc_id", "")) for m in metadatas if m.get("doc_id")}


def _read_pdf_meta(path: Path) -> tuple[dict[str, Any], int | None]:
    """Extract metadata and total page count from PDF."""
    if path.suffix.lower() != ".pdf":
        return {}, None
    try:
        import pdfplumber

        with pdfplumber.open(str(path)) as pdf:
            meta = dict(pdf.metadata) if pdf.metadata else {}
            n_pages = len(pdf.pages)
            return meta, n_pages
    except Exception:
        return {}, None


def _extract_and_chunk(
    path: Path, doc_version: str, superseded: bool, settings: IndexerSettings
) -> Document:
    """Extract text and return a Document (no chunking yet)."""
    from .clean import clean
    from .extract import extract

    return clean(extract(str(path), doc_version, superseded))


def _safe_extract(
    path: Path, doc_version: str, superseded: bool, settings: IndexerSettings
) -> tuple[Document | None, str | None]:
    """Extract text and return (Document, error_str)."""
    try:
        doc = _extract_and_chunk(path, doc_version, superseded, settings)
        return doc, None
    except Exception as exc:
        return None, str(exc)


def _chunk_document(
    doc: Document,
    doc_id: str,
    doc_hash: str,
    title: str,
    page_count: int,
    settings: IndexerSettings,
    status: str = "active",
) -> list[Chunk]:
    """Chunk a Document and attach the new metadata fields."""
    from .chunk import chunk

    raw_chunks = chunk(doc, settings.chunk_size, settings.chunk_overlap)
    enriched: list[Chunk] = []
    for n, c in enumerate(raw_chunks, 1):
        enriched.append(
            Chunk(
                id=f"{doc_id}:{n:04d}",
                text=c.text,
                source=c.source,
                section=c.section,
                page=c.page,
                doc_version=c.doc_version,
                superseded=c.superseded,
                doc_id=doc_id,
                doc_hash=doc_hash,
                title=title,
                page_count=page_count,
                status=status,
            )
        )
    return enriched


def _embed_and_store(
    chunks: list[Chunk], collection: Any, settings: IndexerSettings
) -> int:
    """Embed chunks and upsert them into the collection. Returns count stored."""
    from .embed import embed

    if not chunks:
        return 0
    vectors = embed([c.text for c in chunks], settings)
    dim = len(vectors[0]) if vectors else 0

    batch_size = 100
    for i in range(0, len(chunks), batch_size):
        batch_c = chunks[i : i + batch_size]
        batch_v = vectors[i : i + batch_size]
        collection.upsert(
            ids=[c.id for c in batch_c],
            documents=[c.text for c in batch_c],
            embeddings=batch_v,
            metadatas=[c.metadata() for c in batch_c],
        )

    # Update collection metadata with fresh index_version
    index_version = _utc_stamp()
    collection.modify(
        metadata={
            "embed_model": settings.embed_model,
            "dimension": dim,
            "index_version": index_version,
        }
    )
    return len(chunks)


def _quarantine_doc(
    path: Path, quarantine_dir: Path, verification_dict: dict[str, Any]
) -> None:
    """Move a failed document to quarantine with a reason JSON file."""
    dest = quarantine_dir / path.name
    shutil.copy2(str(path), str(dest))
    reason_file = quarantine_dir / f"{path.name}.reason.json"
    reason_file.write_text(
        json.dumps(verification_dict, indent=2), encoding="utf-8"
    )


def _copy_to_lake(path: Path, lake_dir: Path) -> None:
    """Copy an accepted document to the lake directory."""
    dest = lake_dir / path.name
    shutil.copy2(str(path), str(dest))


def run_ingest(
    input_dir: Path,
    data_dir: Path,
    settings: IndexerSettings,
    prune: bool = False,
) -> tuple[dict[str, int], str, str]:
    """Main ingestion pipeline. Returns (counts_dict, verify_report_path, ingest_report_path)."""
    dirs = _ensure_dirs(data_dir)

    if "TMPDIR" not in os.environ:
        os.environ["TMPDIR"] = "/tmp" if os.path.isdir("/tmp") else str(data_dir)

    index_path = input_dir / "_index.json"
    documents = discover(input_dir, index_path if index_path.exists() else None)
    if not documents:
        raise SystemExit(f"no documents found in {input_dir}")

    collection = _get_collection(settings)
    docs_collection = _get_docs_collection(settings)

    counts = {
        "documents_seen": 0,
        "documents_passed": 0,
        "documents_quarantined": 0,
        "chunks_embedded": 0,
        "chunks_skipped": 0,
        "chunks_deleted": 0,
        "documents_archived": 0,
    }
    verification_results: list[dict[str, Any]] = []
    input_doc_ids: set[str] = set()

    # Load existing active documents and uniqueness reference data
    existing_active_docs: list[dict[str, Any]] = []
    existing_uniqueness_docs: list[dict[str, Any]] = []

    # First load from docs_collection
    try:
        d_res = docs_collection.get(include=["metadatas", "embeddings", "documents"])
        d_ids = d_res.get("ids", [])
        d_metas = d_res.get("metadatas", [])
        d_embs = d_res.get("embeddings", [])
        for did, meta, emb in zip(d_ids, d_metas, d_embs if d_embs is not None else [None] * len(d_ids)):
            st = str(meta.get("status", "active"))
            d_dict = {
                "doc_id": did,
                "doc_hash": meta.get("doc_hash", ""),
                "title": meta.get("title", ""),
                "logical_key": meta.get("logical_key", normalize_title(str(meta.get("title", "")))),
                "doc_version": meta.get("doc_version", ""),
                "status": st,
                "embedding": emb,
                "norm_text_hash": meta.get("norm_text_hash", ""),
            }
            if st == "active":
                existing_active_docs.append(d_dict)
            existing_uniqueness_docs.append(d_dict)
    except Exception as exc:
        log.warning("could not load __docs collection", extra={"error": str(exc)})

    # Also recover from main collection if __docs was empty
    if not existing_active_docs:
        try:
            m_res = collection.get(include=["metadatas"])
            seen_dids = set()
            for m in m_res.get("metadatas", []):
                did = str(m.get("doc_id", ""))
                if did and did not in seen_dids:
                    seen_dids.add(did)
                    st = str(m.get("status", "active"))
                    title = str(m.get("title", ""))
                    doc_item = {
                        "doc_id": did,
                        "doc_hash": str(m.get("doc_hash", "")),
                        "title": title,
                        "logical_key": normalize_title(title),
                        "doc_version": str(m.get("doc_version", "")),
                        "status": st,
                        "embedding": None,
                        "norm_text_hash": "",
                    }
                    if st == "active":
                        existing_active_docs.append(doc_item)
                    existing_uniqueness_docs.append(doc_item)
        except Exception as exc:
            log.warning("could not load main collection metadatas", extra={"error": str(exc)})

    for path, doc_version, superseded in documents:
        index_name = _lookup_index_name(path, input_dir)
        counts["documents_seen"] += 1
        doc_id = _doc_id_from_stem(path.stem)
        input_doc_ids.add(doc_id)

        try:
            file_bytes = path.read_bytes()
        except Exception as exc:
            _log("cannot read file", source=path.name, error=str(exc))
            raise SystemExit(f"unreadable file: {path}") from exc

        doc_hash = _sha256_hex(file_bytes)

        # Check Chroma for existing doc
        stored_hash, stored_count = _get_stored_doc_hashes(collection, doc_id)

        if stored_hash == doc_hash and stored_count > 0:
            # Unchanged document: skipped before extraction
            _log("unchanged, skipping", source=path.name, doc_id=doc_id, chunks=stored_count)
            counts["chunks_skipped"] += stored_count
            counts["documents_passed"] += 1
            verification_results.append(
                {
                    "file": path.name,
                    "doc_id": doc_id,
                    "doc_hash": doc_hash,
                    "checks": {
                        "quality": {"pass": True, "reason": "unchanged, skipped", "details": {}},
                        "completeness": {"pass": True, "reason": "unchanged, skipped", "details": {}},
                        "recency": {"pass": True, "reason": "unchanged, skipped", "details": {}},
                        "uniqueness": {"pass": True, "reason": "unchanged, skipped", "details": {}},
                    },
                    "warnings": [],
                    "decision": "unchanged, skipped",
                    "actions": ["skipped"],
                }
            )
            continue

        # Extract document
        doc, extraction_err = _safe_extract(path, doc_version, superseded, settings)
        pdf_meta, pdf_meta_pages = _read_pdf_meta(path)
        front_matter_dict = _front_matter(path)

        # Derive title and version for embedding sampling
        title = derive_title(path, front_matter_dict, pdf_meta, index_name, doc) or path.stem
        version = derive_version(path, front_matter_dict, doc_version, pdf_meta) or doc_version

        # Compute document embedding vector
        full_text = " ".join(p.text for p in doc.pages) if doc and doc.pages else ""
        doc_vec: list[float] | None = None
        if full_text.strip():
            samples = extract_doc_samples(full_text)
            try:
                doc_vec = compute_doc_vector(samples, settings)
            except Exception as exc:
                log.warning("doc vector computation failed", extra={"error": str(exc)})

        # Verification
        import inspect

        sig = inspect.signature(verify_document)
        if "path" in sig.parameters:
            vr = verify_document(
                path=path,
                doc_id=doc_id,
                doc_hash=doc_hash,
                doc=doc,
                raw_bytes=file_bytes,
                settings=settings,
                front_matter_dict=front_matter_dict,
                index_version=version,
                index_name=index_name,
                pdf_meta=pdf_meta,
                pdf_meta_pages=pdf_meta_pages,
                extraction_error=extraction_err,
                existing_active_docs=existing_active_docs,
                existing_uniqueness_docs=existing_uniqueness_docs,
                doc_vec=doc_vec,
                source=path.name,
                text_length=len(full_text),
                page_count=len(doc.pages) if doc and doc.pages else 0,
                doc_version=version,
                title=title,
            )
        else:
            vr = verify_document(  # type: ignore[call-arg]
                source=path.name,
                doc_id=doc_id,
                doc_hash=doc_hash,
                text_length=len(full_text),
                page_count=len(doc.pages) if doc and doc.pages else 0,
                doc_version=version,
                title=title,
            )
        to_archive = getattr(vr, "to_archive", [])

        verification_results.append(vr.to_dict())

        if not vr.passed:
            # Quarantine failing document
            _quarantine_doc(path, dirs["quarantine"], vr.to_dict())
            counts["documents_quarantined"] += 1
            fail_reason = next(
                (c.reason for c in vr.checks if c.status == "fail" or not c.passed),
                vr.checks[0].reason,
            )
            _log("quarantined", source=path.name, doc_id=doc_id, reason=fail_reason)
            continue

        # Document PASSED verification
        # Recency action: archive older active versions
        for old_id in to_archive:
            try:
                old_chunks = collection.get(where={"doc_id": old_id}, include=["metadatas"])
                if old_chunks and old_chunks.get("ids"):
                    updated_metas = []
                    old_source = ""
                    old_h = ""
                    for m in old_chunks["metadatas"]:
                        m_copy = dict(m)
                        m_copy["status"] = "archived"
                        m_copy["superseded"] = True
                        updated_metas.append(m_copy)
                        if not old_source and m.get("source"):
                            old_source = str(m.get("source"))
                        if not old_h and m.get("doc_hash"):
                            old_h = str(m.get("doc_hash"))
                    collection.update(ids=old_chunks["ids"], metadatas=updated_metas)

                    # Update __docs
                    d_get = docs_collection.get(ids=[old_id], include=["metadatas"])
                    if d_get and d_get.get("ids"):
                        dm = dict(d_get["metadatas"][0])
                        dm["status"] = "archived"
                        docs_collection.update(ids=[old_id], metadatas=[dm])

                    # Move file from lake to archive
                    if old_source:
                        lake_file = dirs["lake"] / old_source
                        if lake_file.is_file():
                            dest_d = dirs["archive"] / old_id / (old_h or "nohash")
                            dest_d.mkdir(parents=True, exist_ok=True)
                            shutil.move(str(lake_file), str(dest_d / old_source))

                    counts["documents_archived"] += 1
                    _log("archived older version", old_doc_id=old_id, incoming=doc_id)

                    # Update in-memory active list
                    for item in existing_active_docs:
                        if item["doc_id"] == old_id:
                            item["status"] = "archived"
            except Exception as exc:
                log.warning("failed to archive old document", extra={"old_id": old_id, "error": str(exc)})

        # Chunk document
        status = "archived" if superseded else "active"
        assert doc is not None
        page_count_est = len(doc.pages)
        chunks = _chunk_document(
            doc, doc_id, doc_hash, title, page_count_est, settings, status
        )

        # Crash safety of replace: embed and store new chunks FIRST
        embedded = _embed_and_store(chunks, collection, settings)
        counts["chunks_embedded"] += embedded
        counts["documents_passed"] += 1

        # Delete old chunks only AFTER new chunks are successfully stored
        if stored_hash is not None and stored_hash != doc_hash:
            _delete_old_chunks(collection, doc_id, doc_hash)
            counts["chunks_deleted"] += stored_count
            _log("replaced", source=path.name, doc_id=doc_id, old_chunks=stored_count)

        # Store doc vector in __docs collection
        norm_text_h = hashlib.sha256(" ".join(full_text.split()).encode("utf-8")).hexdigest()
        logical_key = normalize_title(title)
        meta_entry = {
            "doc_id": doc_id,
            "doc_hash": doc_hash,
            "title": title,
            "logical_key": logical_key,
            "doc_version": version,
            "status": status,
            "norm_text_hash": norm_text_h,
        }
        if doc_vec:
            docs_collection.upsert(
                ids=[doc_id],
                embeddings=[doc_vec],
                metadatas=[meta_entry],
                documents=[title],
            )
        else:
            docs_collection.upsert(
                ids=[doc_id],
                embeddings=[[0.0] * 768],
                metadatas=[meta_entry],
                documents=[title],
            )

        # Update in-memory uniqueness and active doc trackers for subsequent items in run
        existing_unique_entry = {**meta_entry, "embedding": doc_vec}
        if status == "active":
            existing_active_docs.append(existing_unique_entry)
        existing_uniqueness_docs.append(existing_unique_entry)

        # Copy to lake
        _copy_to_lake(path, dirs["lake"])
        _log("ingested", source=path.name, doc_id=doc_id, chunks=embedded)

    # Prune missing documents
    if prune:
        all_stored_ids = _get_all_doc_ids(collection)
        to_prune = all_stored_ids - input_doc_ids
        for pid in to_prune:
            deleted = _delete_doc_chunks(collection, pid)
            try:
                docs_collection.delete(ids=[pid])
            except Exception:
                pass
            counts["chunks_deleted"] += deleted
            _log("pruned", doc_id=pid, chunks=deleted)
        if to_prune:
            collection.modify(metadata={"index_version": _utc_stamp()})

    stamp = _utc_stamp()
    verify_report_path = dirs["reports"] / f"verify-{stamp}.json"
    verify_report_path.write_text(
        json.dumps({"timestamp": stamp, "results": verification_results}, indent=2),
        encoding="utf-8",
    )

    ingest_report_path = dirs["reports"] / f"ingest-{stamp}.json"
    ingest_report = {
        **counts,
        "verification_report": str(verify_report_path),
        "timestamp": stamp,
        "collection": settings.collection,
        "embed_model": settings.embed_model,
    }
    ingest_report_path.write_text(
        json.dumps(ingest_report, indent=2), encoding="utf-8"
    )

    return counts, str(verify_report_path), str(ingest_report_path)


def main() -> int:
    parser = argparse.ArgumentParser(description="RAHBAR ingestion CLI (Assignment 03).")
    parser.add_argument("--input", required=True, help="Input directory containing documents")
    parser.add_argument("--data-dir", required=True, help="Data directory for lake/quarantine/reports")
    parser.add_argument(
        "--collection", default=None, help="Chroma collection name (overrides CHROMA_COLLECTION env var)"
    )
    parser.add_argument(
        "--prune", action="store_true", help="Delete chunks for documents missing from --input"
    )
    parser.add_argument("--dummy", action="store_true", help="Write three fake chunks (legacy)")
    args = parser.parse_args()

    input_dir = Path(args.input)
    data_dir = Path(args.data_dir)

    if not input_dir.is_dir():
        print(f"ERROR: input directory does not exist: {input_dir}", file=sys.stderr)
        return 1

    settings = get_settings()
    if args.collection:
        settings = IndexerSettings(
            embed_model=settings.embed_model,
            chroma_host=settings.chroma_host,
            chroma_port=settings.chroma_port,
            collection=args.collection,
            corpus_dir=str(input_dir),
            chunk_size=settings.chunk_size,
            chunk_overlap=settings.chunk_overlap,
            ollama_url=settings.ollama_url,
            embed_provider=settings.embed_provider,
            verify_min_chars_per_page=settings.verify_min_chars_per_page,
            verify_min_alpha_ratio=settings.verify_min_alpha_ratio,
            verify_max_replacement_chars=settings.verify_max_replacement_chars,
            verify_near_dup_threshold=settings.verify_near_dup_threshold,
        )

    _log(
        "ingestion starting",
        input=str(input_dir),
        data_dir=str(data_dir),
        collection=settings.collection,
        embed_model=settings.embed_model,
        prune=args.prune,
        chroma=f"{settings.chroma_host}:{settings.chroma_port}",
        pod=os.environ.get("HOSTNAME", "-"),
    )

    started = time.perf_counter()
    try:
        if args.dummy:
            from .dummy import write_dummy_index
            from .store import get_client

            count = write_dummy_index(settings, get_client(settings))
            _log("dummy indexer finished", chunks=count)
            return 0

        counts, verify_path, ingest_path = run_ingest(
            input_dir, data_dir, settings, prune=args.prune
        )
    except SystemExit as exc:
        _log("ingestion failed", error=str(exc))
        return 1
    except Exception as exc:
        _log("ingestion failed", error=str(exc))
        return 1

    elapsed = round(time.perf_counter() - started, 1)
    _log("ingestion finished", seconds=elapsed)

    # Print the required summary lines
    print(f"documents_seen={counts['documents_seen']}")
    print(f"documents_passed={counts['documents_passed']}")
    print(f"documents_quarantined={counts['documents_quarantined']}")
    print(f"chunks_embedded={counts['chunks_embedded']}")
    print(f"chunks_skipped={counts['chunks_skipped']}")
    print(f"chunks_deleted={counts['chunks_deleted']}")
    print(f"documents_archived={counts['documents_archived']}")
    print(f"verification_report={verify_path}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
