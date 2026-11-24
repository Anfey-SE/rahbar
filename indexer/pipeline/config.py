"""Indexer configuration, from the same ConfigMap the API reads (section 4.1)."""

from __future__ import annotations

import os
from dataclasses import dataclass


@dataclass(frozen=True)
class IndexerSettings:
    embed_model: str        # EMBED_MODEL: must equal the API's value (section 4.4)
    chroma_host: str
    chroma_port: int
    collection: str
    corpus_dir: str         # where the documents and _index.json sit inside the Job's pod
    chunk_size: int
    chunk_overlap: int
    ollama_url: str
    embed_provider: str = "ollama"  # EMBED_PROVIDER: ollama | hash
    verify_min_chars_per_page: int = 50
    verify_min_alpha_ratio: float = 0.60
    verify_max_replacement_chars: int = 10
    verify_near_dup_threshold: float = 0.95


def get_settings() -> IndexerSettings:
    embed_provider = os.environ.get("EMBED_PROVIDER", "ollama")
    default_model = "hash-768" if embed_provider == "hash" else "nomic-embed-text"
    return IndexerSettings(
        embed_model=os.environ.get("EMBED_MODEL", default_model),
        chroma_host=os.environ.get("CHROMA_HOST", "chroma"),
        chroma_port=int(os.environ.get("CHROMA_PORT", 8000)),
        collection=os.environ.get("CHROMA_COLLECTION", "rahbar"),
        corpus_dir=os.environ.get("CORPUS_DIR", "/corpus"),
        chunk_size=int(os.environ.get("CHUNK_SIZE", 400)),
        chunk_overlap=int(os.environ.get("CHUNK_OVERLAP", 50)),
        ollama_url=os.environ.get("OLLAMA_URL", "http://host.minikube.internal:11434"),
        embed_provider=embed_provider,
        verify_min_chars_per_page=int(os.environ.get("VERIFY_MIN_CHARS_PER_PAGE", 50)),
        verify_min_alpha_ratio=float(os.environ.get("VERIFY_MIN_ALPHA_RATIO", 0.60)),
        verify_max_replacement_chars=int(os.environ.get("VERIFY_MAX_REPLACEMENT_CHARS", 10)),
        verify_near_dup_threshold=float(os.environ.get("VERIFY_NEAR_DUP_THRESHOLD", 0.95)),
    )
