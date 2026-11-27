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
from .verify import VerificationResult, verify_document


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
        lines = path.read_text(encoding="utf-8").splitlines()
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

    # Check input_dir/_index.json or explicitly provided index_path (takes precedence)
    if index_path is None:
        idx = root / "_index.json"
        if idx.exists():
            index_path = idx

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
        if path.name.lower().startswith("readme"):
            continue
        if path.name in index_entries:
            ver, sup = index_entries[path.name]
            found.append((path, ver, sup))
        elif path.suffix.lower() in {".md", ".txt"}:
            meta = _front_matter(path)
            found.append(
                (
                    path,
                    meta.get("doc_version", "unknown"),
                    meta.get("superseded", "false").lower() == "true",
                )
            )
        else:
            found.append((path, "unknown", False))
    return found


def _ensure_dirs(data_dir: Path) -> dict[str, Path]:
    """Create the required subdirectories under --data-dir."""
    dirs = {}
    for name in ("lake", "quarantine", "archive", "reports", "traces", "results"):
        d = data_dir / name
        d.mkdir(parents=True, exist_ok=True)
        dirs[name] = d
    return dirs


def _get_collection(settings: IndexerSettings) -> Any:
    """Get or create the Chroma collection."""
    client = get_client(settings)
    return client.get_or_create_collection(
        name=settings.collection,
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
    return {m.get("doc_id", "") for m in metadatas if m.get("doc_id")}


def _extract_and_chunk(
    path: Path, doc_version: str, superseded: bool, settings: IndexerSettings
) -> Document:
    """Extract text and return a Document (no chunking yet)."""
    from .clean import clean
    from .extract import extract

    return clean(extract(str(path), doc_version, superseded))


def _chunk_document(
    doc: Document, doc_id: str, doc_hash: str, title: str, page_count: int,
    settings: IndexerSettings, status: str = "active",
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


def _derive_title(path: Path, doc: Document | None = None) -> str:
    """Derive a document title from the file or its content."""
    stem = path.stem.replace("-", " ").replace("_", " ").title()
    return stem


def _quarantine_doc(
    path: Path, quarantine_dir: Path, verification: VerificationResult
) -> None:
    """Move a failed document to quarantine with a reason JSON file."""
    dest = quarantine_dir / path.name
    shutil.copy2(str(path), str(dest))
    reason_file = quarantine_dir / f"{path.name}.reason.json"
    reason_file.write_text(
        json.dumps(verification.to_dict(), indent=2), encoding="utf-8"
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

    # Set TMPDIR for OCR temp files (containers have read-only root fs)
    if "TMPDIR" not in os.environ:
        os.environ["TMPDIR"] = "/tmp" if os.path.isdir("/tmp") else str(data_dir)

    # Check for _index.json in the input dir (corpus/raw compatibility)
    index_path = input_dir / "_index.json"

    documents = discover(input_dir, index_path if index_path.exists() else None)
    if not documents:
        raise SystemExit(f"no documents found in {input_dir}")

    collection = _get_collection(settings)

    counts = {
        "documents_seen": 0,
        "documents_passed": 0,
        "documents_quarantined": 0,
        "chunks_embedded": 0,
        "chunks_skipped": 0,
        "chunks_deleted": 0,
    }
    verification_results: list[dict[str, object]] = []

    input_doc_ids: set[str] = set()

    for path, doc_version, superseded in documents:
        counts["documents_seen"] += 1
        doc_id = _doc_id_from_stem(path.stem)
        input_doc_ids.add(doc_id)

        # Compute doc_hash BEFORE any extraction
        try:
            file_bytes = path.read_bytes()
        except Exception as exc:
            _log("cannot read file", source=path.name, error=str(exc))
            raise SystemExit(f"unreadable file: {path}") from exc

        doc_hash = _sha256_hex(file_bytes)
        page_count_est = 0  # will be updated after extraction if needed

        # Check Chroma for existing doc
        stored_hash, stored_count = _get_stored_doc_hashes(collection, doc_id)

        if stored_hash == doc_hash and stored_count > 0:
            # Unchanged document: skip completely
            _log("unchanged, skipping", source=path.name, doc_id=doc_id,
                 chunks=stored_count)
            counts["chunks_skipped"] += stored_count
            counts["documents_passed"] += 1
            # Still record verification as passed (stub)
            vr = verify_document(
                source=path.name, doc_id=doc_id, doc_hash=doc_hash,
                text_length=len(file_bytes), page_count=0,
                doc_version=doc_version, title=_derive_title(path),
            )
            verification_results.append(vr.to_dict())
            continue

        # Need extraction: either new or changed
        status = "active"
        if superseded:
            status = "archived"

        doc = _extract_and_chunk(path, doc_version, superseded, settings)
        page_count_est = len(doc.pages)
        title = _derive_title(path, doc)
        total_text = sum(len(p.text) for p in doc.pages)

        # Verification
        vr = verify_document(
            source=path.name, doc_id=doc_id, doc_hash=doc_hash,
            text_length=total_text, page_count=page_count_est,
            doc_version=doc_version, title=title,
        )
        verification_results.append(vr.to_dict())

        if not vr.passed:
            # Quarantine
            _quarantine_doc(path, dirs["quarantine"], vr)
            counts["documents_quarantined"] += 1
            _log("quarantined", source=path.name, doc_id=doc_id)
            continue

        # Changed document: delete old chunks first
        if stored_hash is not None and stored_hash != doc_hash:
            deleted = _delete_doc_chunks(collection, doc_id)
            counts["chunks_deleted"] += deleted
            _log("replaced", source=path.name, doc_id=doc_id,
                 old_chunks=deleted)

        # Chunk and embed
        chunks = _chunk_document(
            doc, doc_id, doc_hash, title, page_count_est, settings, status
        )
        embedded = _embed_and_store(chunks, collection, settings)
        counts["chunks_embedded"] += embedded
        counts["documents_passed"] += 1

        # Copy to lake
        _copy_to_lake(path, dirs["lake"])
        _log("ingested", source=path.name, doc_id=doc_id, chunks=embedded)

    # Prune: delete chunks for documents missing from input
    if prune:
        all_stored_ids = _get_all_doc_ids(collection)
        to_prune = all_stored_ids - input_doc_ids
        for pid in to_prune:
            deleted = _delete_doc_chunks(collection, pid)
            counts["chunks_deleted"] += deleted
            _log("pruned", doc_id=pid, chunks=deleted)
        if to_prune:
            # Update index_version after pruning
            collection.modify(
                metadata={"index_version": _utc_stamp()}
            )

    # Write verification report
    stamp = _utc_stamp()
    verify_report_path = dirs["reports"] / f"verify-{stamp}.json"
    verify_report_path.write_text(
        json.dumps({"timestamp": stamp, "results": verification_results}, indent=2),
        encoding="utf-8",
    )

    # Write ingestion report
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
    parser = argparse.ArgumentParser(
        description="RAHBAR ingestion CLI (Assignment 03)."
    )
    parser.add_argument(
        "--input", required=True, help="Input directory containing documents"
    )
    parser.add_argument(
        "--data-dir", required=True, help="Data directory for lake/quarantine/reports"
    )
    parser.add_argument(
        "--collection", default=None,
        help="Chroma collection name (overrides CHROMA_COLLECTION env var)"
    )
    parser.add_argument(
        "--prune", action="store_true",
        help="Delete chunks for documents missing from --input"
    )
    parser.add_argument(
        "--dummy", action="store_true", help="Write three fake chunks (legacy)"
    )
    args = parser.parse_args()

    input_dir = Path(args.input)
    data_dir = Path(args.data_dir)

    if not input_dir.is_dir():
        print(f"ERROR: input directory does not exist: {input_dir}", file=sys.stderr)
        return 1

    settings = get_settings()
    if args.collection:
        # Override collection from CLI
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
    print(f"verification_report={verify_path}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
