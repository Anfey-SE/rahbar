"""The real retriever. Sections 3.2 and 4.4."""

from __future__ import annotations

import json
import logging
import re
import urllib.request

from ..config import Settings
from ..schemas import Passage

log = logging.getLogger("rahbar.chroma")

_OLDER_PATTERN = re.compile(r"\b(2023|old|previous|earlier)\b", re.IGNORECASE)


class ChromaRetriever:
    name = "chroma"

    def __init__(self, settings: Settings) -> None:
        import chromadb  # type: ignore[import-untyped]
        from chromadb.config import Settings as ChromaSettings

        self.settings = settings
        self.client = chromadb.HttpClient(
            host=settings.chroma_host,
            port=settings.chroma_port,
            settings=ChromaSettings(anonymized_telemetry=False),
        )

    def _embed(self, text: str) -> list[float]:
        """Embed via Ollama /api/embed (same model as the indexer used)."""
        url = f"{self.settings.ollama_url.rstrip('/')}/api/embed"
        payload = json.dumps({
            "model": self.settings.embed_model,
            "input": text,
        }).encode("utf-8")
        req = urllib.request.Request(
            url,
            data=payload,
            headers={"Content-Type": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=30) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        embeddings = data.get("embeddings", [])
        if not embeddings or not embeddings[0]:
            raise ValueError("empty embedding returned from embedding service")
        return embeddings[0]

    def retrieve(self, question: str, k: int | None = None) -> list[Passage]:
        target_k = k if k is not None and k > 0 else self.settings.top_k
        try:
            collection = self.client.get_collection(self.settings.collection)
        except Exception as exc:
            log.error("chroma get_collection failed", extra={"error": str(exc)})
            return []

        try:
            vec = self._embed(question)
        except Exception as exc:
            log.error("embed failed", extra={"error": str(exc)})
            return []

        # Filter out superseded documents unless the question asks for an older version
        where_filter = None if _OLDER_PATTERN.search(question) else {"superseded": False}

        try:
            results = collection.query(
                query_embeddings=[vec],  # type: ignore[arg-type]
                n_results=target_k,
                where=where_filter,  # type: ignore[arg-type]
                include=["documents", "metadatas", "distances"],
            )
        except Exception as exc:
            log.error("chroma query failed", extra={"error": str(exc)})
            return []

        passages: list[Passage] = []
        raw_ids = results.get("ids")
        raw_docs = results.get("documents")
        raw_metas = results.get("metadatas")
        raw_dists = results.get("distances")

        ids = raw_ids[0] if raw_ids else []
        docs = raw_docs[0] if raw_docs else []
        metas = raw_metas[0] if raw_metas else []
        dists = raw_dists[0] if raw_dists else []

        for doc_id, text, meta, dist in zip(ids, docs, metas, dists, strict=False):
            m = dict(meta) if meta else {}
            page_val = m.get("page")
            parsed_page: int | None = None
            if page_val is not None:
                try:
                    parsed_page = int(page_val)
                except (ValueError, TypeError):
                    parsed_page = None

            # Cosine distance in Chroma: similarity = 1 - cosine distance
            score = round(1.0 - float(dist), 4)
            passages.append(Passage(
                chunk_id=str(doc_id),
                text=str(text or ""),
                source=str(m.get("source", "")),
                section=str(m.get("section", "")),
                page=parsed_page,
                score=score,
            ))

        log.info(
            "retrieved",
            extra={
                "k": target_k,
                "returned": len(passages),
                "scores": [p.score for p in passages],
            },
        )
        return passages
