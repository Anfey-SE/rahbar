"""Admin CLI for the RAHBAR pipeline (Assignment 03).

Commands:
    python -m pipeline.admin count --collection <name> [--status]
    python -m pipeline.admin report <path>
    python -m pipeline.admin list --collection <name> [--data-dir <dir>]
    python -m pipeline.admin archive --doc-id <id> --collection <name> [--data-dir <dir>]
    python -m pipeline.admin restore --doc-id <id> --collection <name> [--data-dir <dir>] [--force]
    python -m pipeline.admin delete --doc-id <id> --collection <name> [--data-dir <dir>]
"""

from __future__ import annotations

import argparse
import json
import shutil
import sys
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .config import IndexerSettings, get_settings
from .store import get_client
from .verify import normalize_title


def _utc_stamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def count_chunks(settings: IndexerSettings, show_status: bool = False) -> int:
    """Print chunk counts per doc_id and per source, optionally with status breakdown."""
    client = get_client(settings)
    try:
        collection = client.get_collection(name=settings.collection)
    except Exception as exc:
        print(f"ERROR: collection '{settings.collection}' not found: {exc}", file=sys.stderr)
        return 1

    result = collection.get(include=["metadatas"])
    ids = result.get("ids", [])
    metadatas = result.get("metadatas", [])

    total = len(ids)
    print(f"collection: {settings.collection}")
    print(f"total_chunks: {total}")

    if show_status:
        active_cnt = sum(1 for m in metadatas if m.get("status", "active") == "active")
        archived_cnt = sum(1 for m in metadatas if m.get("status", "active") == "archived")
        print(f"active_chunks: {active_cnt}")
        print(f"archived_chunks: {archived_cnt}")

    print()

    # Count per doc_id
    doc_id_counter: Counter[str] = Counter()
    doc_id_status: dict[str, Counter[str]] = defaultdict(Counter)
    source_counter: Counter[str] = Counter()

    for m in metadatas:
        did = str(m.get("doc_id", "unknown"))
        st = str(m.get("status", "active"))
        src = str(m.get("source", "unknown"))
        doc_id_counter[did] += 1
        doc_id_status[did][st] += 1
        source_counter[src] += 1

    print("chunks_per_doc_id:")
    for did, cnt in sorted(doc_id_counter.items()):
        if show_status:
            st_info = ", ".join(f"{s}:{c}" for s, c in sorted(doc_id_status[did].items()))
            print(f"  {did}: {cnt} ({st_info})")
        else:
            print(f"  {did}: {cnt}")

    print()
    print("chunks_per_source:")
    for src, cnt in sorted(source_counter.items()):
        print(f"  {src}: {cnt}")

    return 0


def print_report_table(report_path: Path | str) -> int:
    """Print compact verification report table:
    file | quality | completeness | recency | uniqueness | decision | reason
    """
    path = Path(report_path)
    if not path.is_file():
        print(f"ERROR: report file not found: {path}", file=sys.stderr)
        return 1

    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        print(f"ERROR: cannot read report JSON: {exc}", file=sys.stderr)
        return 1

    results = data.get("results", [])

    print(
        f"{'file':<40} | {'quality':<7} | {'completeness':<12} | {'recency':<7} | {'uniqueness':<10} | {'decision':<11} | {'reason'}"
    )
    print("-" * 125)

    for r in results:
        fname = str(r.get("file", ""))
        raw_checks = r.get("checks", {})
        if isinstance(raw_checks, list):
            checks = {c.get("name", ""): c for c in raw_checks}
        else:
            checks = raw_checks

        def _get_status(chk: dict[str, Any]) -> str:
            st = str(chk.get("status", "")).lower()
            if st in {"skipped", "skip"} or chk.get("skipped") or chk.get("details", {}).get("skipped"):
                return "skipped"
            return "pass" if chk.get("pass") or chk.get("passed") else "FAIL"

        q_pass = _get_status(checks.get("quality", {}))
        c_pass = _get_status(checks.get("completeness", {}))
        r_pass = _get_status(checks.get("recency", {}))
        u_pass = _get_status(checks.get("uniqueness", {}))
        decision = str(r.get("decision", "unknown"))

        reason = "-"
        if decision == "quarantined":
            for cname in ("quality", "completeness", "recency", "uniqueness"):
                chk = checks.get(cname, {})
                st = str(chk.get("status", "")).lower()
                if st == "fail" or (
                    not chk.get("pass")
                    and not chk.get("passed")
                    and st != "skipped"
                    and not chk.get("details", {}).get("skipped")
                ):
                    reason = str(chk.get("reason", "failed"))
                    break
        elif decision == "unchanged, skipped":
            reason = "unchanged, skipped"
        elif decision == "accepted":
            warnings = r.get("warnings", [])
            if warnings:
                reason = f"accepted (warning: {warnings[0]})"
            else:
                reason = "pass"

        print(
            f"{fname:<40} | {q_pass:<7} | {c_pass:<12} | {r_pass:<7} | {u_pass:<10} | {decision:<11} | {reason}"
        )

    return 0


def list_documents(settings: IndexerSettings) -> int:
    """Print table of documents with doc_id, title, version, status, chunk_count."""
    client = get_client(settings)
    try:
        collection = client.get_collection(name=settings.collection)
    except Exception as exc:
        print(f"ERROR: collection '{settings.collection}' not found: {exc}", file=sys.stderr)
        return 1

    result = collection.get(include=["metadatas"])
    metadatas = result.get("metadatas", [])

    docs: dict[str, dict[str, Any]] = {}
    for m in metadatas:
        did = str(m.get("doc_id", "unknown"))
        if did not in docs:
            docs[did] = {
                "doc_id": did,
                "title": m.get("title", ""),
                "version": m.get("doc_version", ""),
                "status": m.get("status", "active"),
                "source": m.get("source", ""),
                "chunks": 0,
            }
        docs[did]["chunks"] += 1

    print(f"collection: {settings.collection} ({len(docs)} documents)")
    print(f"{'doc_id':<18} | {'title':<38} | {'version':<10} | {'status':<9} | {'chunks':<6}")
    print("-" * 92)

    for did, d in sorted(docs.items(), key=lambda x: str(x[1].get("title", ""))):
        title = str(d["title"])[:36]
        ver = str(d["version"])[:10]
        st = str(d["status"])
        chunks = str(d["chunks"])
        print(f"{did:<18} | {title:<38} | {ver:<10} | {st:<9} | {chunks:<6}")

    return 0


def archive_document(settings: IndexerSettings, doc_id: str, data_dir: Path | None = None) -> int:
    """Set status=archived and superseded=true on chunks without re-embedding.

    Move file to archive/<doc_id>/<doc_hash>/.
    """
    client = get_client(settings)
    try:
        collection = client.get_collection(name=settings.collection)
    except Exception as exc:
        print(f"ERROR: collection '{settings.collection}' not found: {exc}", file=sys.stderr)
        return 1

    result = collection.get(where={"doc_id": doc_id}, include=["metadatas"])
    ids = result.get("ids", [])
    metadatas = result.get("metadatas", [])

    if not ids:
        print(f"ERROR: no chunks found for doc_id '{doc_id}' in {settings.collection}", file=sys.stderr)
        return 1

    # Update chunks in main collection
    updated_metas = []
    source_name = ""
    doc_hash = ""
    for m in metadatas:
        m_copy = dict(m)
        m_copy["status"] = "archived"
        m_copy["superseded"] = True
        updated_metas.append(m_copy)
        if not source_name and m.get("source"):
            source_name = str(m.get("source"))
        if not doc_hash and m.get("doc_hash"):
            doc_hash = str(m.get("doc_hash"))

    collection.update(ids=ids, metadatas=updated_metas)
    collection.modify(metadata={"index_version": _utc_stamp()})

    # Update __docs collection if it exists
    try:
        docs_coll = client.get_collection(name=f"{settings.collection}__docs")
        docs_res = docs_coll.get(ids=[doc_id], include=["metadatas"])
        if docs_res and docs_res.get("ids"):
            d_meta = dict(docs_res["metadatas"][0])
            d_meta["status"] = "archived"
            docs_coll.update(ids=[doc_id], metadatas=[d_meta])
    except Exception:
        pass

    # Move file to archive/<doc_id>/<doc_hash>/
    if data_dir:
        lake_path = data_dir / "lake" / source_name
        if lake_path.is_file():
            arch_dir = data_dir / "archive" / doc_id / (doc_hash or "nohash")
            arch_dir.mkdir(parents=True, exist_ok=True)
            dest = arch_dir / source_name
            shutil.move(str(lake_path), str(dest))
            print(f"moved {lake_path.name} -> {dest}")

    print(f"archived document '{doc_id}' ({len(ids)} chunks updated to status=archived, superseded=True)")
    return 0


def restore_document(
    settings: IndexerSettings, doc_id: str, data_dir: Path | None = None, force: bool = False
) -> int:
    """Restore an archived document: set status=active, superseded=false.

    Refuses if another active document with the same logical key exists, unless --force.
    """
    client = get_client(settings)
    try:
        collection = client.get_collection(name=settings.collection)
    except Exception as exc:
        print(f"ERROR: collection '{settings.collection}' not found: {exc}", file=sys.stderr)
        return 1

    result = collection.get(where={"doc_id": doc_id}, include=["metadatas"])
    ids = result.get("ids", [])
    metadatas = result.get("metadatas", [])

    if not ids:
        print(f"ERROR: no chunks found for doc_id '{doc_id}' in {settings.collection}", file=sys.stderr)
        return 1

    title = str(metadatas[0].get("title", ""))
    logical_key = normalize_title(title)
    source_name = str(metadatas[0].get("source", ""))
    doc_hash = str(metadatas[0].get("doc_hash", "nohash"))

    # Check conflict with currently active documents
    all_res = collection.get(include=["metadatas"])
    all_metas = all_res.get("metadatas", [])
    active_conflicts: set[str] = set()
    for m in all_metas:
        other_did = str(m.get("doc_id", ""))
        other_st = str(m.get("status", "active"))
        if other_did != doc_id and other_st == "active":
            other_title = str(m.get("title", ""))
            if normalize_title(other_title) == logical_key:
                active_conflicts.add(other_did)

    if active_conflicts and not force:
        conflict_list = ", ".join(sorted(active_conflicts))
        print(
            f"ERROR: cannot restore '{doc_id}': active document(s) {conflict_list} with key '{logical_key}' already exist. Use --force to restore anyway.",
            file=sys.stderr,
        )
        return 1

    # Update chunks in main collection
    updated_metas = []
    for m in metadatas:
        m_copy = dict(m)
        m_copy["status"] = "active"
        m_copy["superseded"] = False
        updated_metas.append(m_copy)

    collection.update(ids=ids, metadatas=updated_metas)
    collection.modify(metadata={"index_version": _utc_stamp()})

    # Update __docs collection if it exists
    try:
        docs_coll = client.get_collection(name=f"{settings.collection}__docs")
        docs_res = docs_coll.get(ids=[doc_id], include=["metadatas"])
        if docs_res and docs_res.get("ids"):
            d_meta = dict(docs_res["metadatas"][0])
            d_meta["status"] = "active"
            docs_coll.update(ids=[doc_id], metadatas=[d_meta])
    except Exception:
        pass

    # Move file back to lake/
    if data_dir:
        arch_file = data_dir / "archive" / doc_id / doc_hash / source_name
        if arch_file.is_file():
            lake_dir = data_dir / "lake"
            lake_dir.mkdir(parents=True, exist_ok=True)
            dest = lake_dir / source_name
            shutil.move(str(arch_file), str(dest))
            print(f"moved {arch_file.name} -> {dest}")

    print(f"restored document '{doc_id}' ({len(ids)} chunks updated to status=active, superseded=False)")
    return 0


def delete_document(settings: IndexerSettings, doc_id: str, data_dir: Path | None = None) -> int:
    """Delete all chunks for a doc_id, remove from __docs, and delete files.

    Prints count before and after.
    """
    client = get_client(settings)
    try:
        collection = client.get_collection(name=settings.collection)
    except Exception as exc:
        print(f"ERROR: collection '{settings.collection}' not found: {exc}", file=sys.stderr)
        return 1

    count_before = collection.count()
    result = collection.get(where={"doc_id": doc_id}, include=["metadatas"])
    ids = result.get("ids", [])
    metadatas = result.get("metadatas", [])

    source_name = metadatas[0].get("source", "") if metadatas else ""

    print(f"chunks_before: {count_before}")
    if ids:
        collection.delete(ids=ids)
        collection.modify(metadata={"index_version": _utc_stamp()})

    count_after = collection.count()
    print(f"chunks_deleted: {len(ids)}")
    print(f"chunks_after: {count_after}")

    # Delete from __docs collection
    try:
        docs_coll = client.get_collection(name=f"{settings.collection}__docs")
        docs_coll.delete(ids=[doc_id])
    except Exception:
        pass

    # Remove files if data_dir given
    if data_dir:
        if source_name:
            lake_f = data_dir / "lake" / str(source_name)
            if lake_f.is_file():
                lake_f.unlink()
        arch_d = data_dir / "archive" / doc_id
        if arch_d.is_dir():
            shutil.rmtree(str(arch_d))

    print(f"deleted document '{doc_id}' successfully")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="RAHBAR admin tools.")
    sub = parser.add_subparsers(dest="command")

    # count
    count_parser = sub.add_parser("count", help="Count chunks per doc_id and source.")
    count_parser.add_argument("--collection", required=True, help="Chroma collection name")
    count_parser.add_argument("--data-dir", default=None, help="Optional data directory")
    count_parser.add_argument("--status", action="store_true", help="Show active/archived chunk breakdown")

    # report
    report_parser = sub.add_parser("report", help="Print compact verification report table.")
    report_parser.add_argument("path", help="Path to verify-<timestamp>.json")

    # list
    list_parser = sub.add_parser("list", help="List documents with status and chunk counts.")
    list_parser.add_argument("--collection", required=True, help="Chroma collection name")
    list_parser.add_argument("--data-dir", default=None, help="Optional data directory")

    # archive
    archive_parser = sub.add_parser("archive", help="Archive a document.")
    archive_parser.add_argument("--doc-id", required=True, help="Document ID to archive")
    archive_parser.add_argument("--collection", required=True, help="Chroma collection name")
    archive_parser.add_argument("--data-dir", default=None, help="Optional data directory")

    # restore
    restore_parser = sub.add_parser("restore", help="Restore an archived document.")
    restore_parser.add_argument("--doc-id", required=True, help="Document ID to restore")
    restore_parser.add_argument("--collection", required=True, help="Chroma collection name")
    restore_parser.add_argument("--data-dir", default=None, help="Optional data directory")
    restore_parser.add_argument("--force", action="store_true", help="Force restore despite active key conflict")

    # delete
    delete_parser = sub.add_parser("delete", help="Delete a document and its chunks.")
    delete_parser.add_argument("--doc-id", required=True, help="Document ID to delete")
    delete_parser.add_argument("--collection", required=True, help="Chroma collection name")
    delete_parser.add_argument("--data-dir", default=None, help="Optional data directory")

    args = parser.parse_args()

    if args.command is None:
        parser.print_help()
        return 1

    if args.command == "report":
        return print_report_table(args.path)

    settings = get_settings()
    collection_name = getattr(args, "collection", None)
    if collection_name:
        settings = IndexerSettings(
            embed_model=settings.embed_model,
            chroma_host=settings.chroma_host,
            chroma_port=settings.chroma_port,
            collection=collection_name,
            corpus_dir=settings.corpus_dir,
            chunk_size=settings.chunk_size,
            chunk_overlap=settings.chunk_overlap,
            ollama_url=settings.ollama_url,
            embed_provider=settings.embed_provider,
            verify_min_chars_per_page=settings.verify_min_chars_per_page,
            verify_min_alpha_ratio=settings.verify_min_alpha_ratio,
            verify_max_replacement_chars=settings.verify_max_replacement_chars,
            verify_near_dup_threshold=settings.verify_near_dup_threshold,
        )

    data_dir = Path(args.data_dir) if getattr(args, "data_dir", None) else None

    if args.command == "count":
        return count_chunks(settings, show_status=args.status)
    elif args.command == "list":
        return list_documents(settings)
    elif args.command == "archive":
        return archive_document(settings, args.doc_id, data_dir)
    elif args.command == "restore":
        return restore_document(settings, args.doc_id, data_dir, force=args.force)
    elif args.command == "delete":
        return delete_document(settings, args.doc_id, data_dir)

    return 0


if __name__ == "__main__":
    sys.exit(main())
