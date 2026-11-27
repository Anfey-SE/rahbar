"""Admin CLI for the RAHBAR pipeline.

    python -m pipeline.admin count --collection <name>

Prints the number of chunks per doc_id and per source.
"""

from __future__ import annotations

import argparse
import sys
from collections import Counter

from .config import IndexerSettings, get_settings
from .store import get_client


def count_chunks(settings: IndexerSettings) -> int:
    """Print chunk counts per doc_id and per source."""
    client = get_client(settings)
    try:
        collection = client.get_collection(name=settings.collection)
    except Exception as exc:
        print(f"ERROR: collection '{settings.collection}' not found: {exc}",
              file=sys.stderr)
        return 1

    result = collection.get(include=["metadatas"])
    ids = result.get("ids", [])
    metadatas = result.get("metadatas", [])

    total = len(ids)
    print(f"collection: {settings.collection}")
    print(f"total_chunks: {total}")
    print()

    # Count per doc_id
    doc_id_counter: Counter[str] = Counter()
    source_counter: Counter[str] = Counter()
    for m in metadatas:
        doc_id_counter[m.get("doc_id", "unknown")] += 1
        source_counter[m.get("source", "unknown")] += 1

    print("chunks_per_doc_id:")
    for did, cnt in sorted(doc_id_counter.items()):
        print(f"  {did}: {cnt}")
    print()
    print("chunks_per_source:")
    for src, cnt in sorted(source_counter.items()):
        print(f"  {src}: {cnt}")

    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="RAHBAR admin tools.")
    sub = parser.add_subparsers(dest="command")

    count_parser = sub.add_parser("count", help="Count chunks per doc_id and source.")
    count_parser.add_argument(
        "--collection", required=True, help="Chroma collection name"
    )

    args = parser.parse_args()

    if args.command is None:
        parser.print_help()
        return 1

    settings = get_settings()
    if args.collection:
        settings = IndexerSettings(
            embed_model=settings.embed_model,
            chroma_host=settings.chroma_host,
            chroma_port=settings.chroma_port,
            collection=args.collection,
            corpus_dir=settings.corpus_dir,
            chunk_size=settings.chunk_size,
            chunk_overlap=settings.chunk_overlap,
            ollama_url=settings.ollama_url,
            embed_provider=settings.embed_provider,
        )

    if args.command == "count":
        return count_chunks(settings)

    return 0


if __name__ == "__main__":
    sys.exit(main())
