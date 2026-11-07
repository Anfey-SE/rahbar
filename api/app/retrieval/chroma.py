"""The real retriever. Sections 3.2 and 4.4."""

from __future__ import annotations

import json
import logging
import urllib.request

from ..config import Settings
from ..schemas import Passage

log = logging.getLogger("rahbar.chroma")


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
        }).encode()
        req = urllib.request.Request(
            url, data=payload,
            headers={"Content-Type": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=30) as resp:
            data = json.loads(resp.read())
        return data["embeddings"][0]

    def retrieve(self, question: str, k: int) -> list[Passage]:
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

        # Over-fetch to account for superseded filtering
        results = collection.query(
            query_embeddings=[vec],  # type: ignore[arg-type]
            n_results=min(k + 5, 20),
            include=["documents", "metadatas", "distances"],
        )

        passages: list[Passage] = []
        raw_ids = results.get("ids")
        raw_docs = results.get("documents")
        raw_metas = results.get("metadatas")
        raw_dists = results.get("distances")

        ids = raw_ids[0] if raw_ids else []
        docs = raw_docs[0] if raw_docs else []
        metas = raw_metas[0] if raw_metas else []
        dists = raw_dists[0] if raw_dists else []

        for doc_id, text, meta, dist in zip(ids, docs, metas, dists):
            m = dict(meta) if meta else {}
            # Filter out superseded documents (section 3.2)
            if m.get("superseded", False):
                continue
            page_val = m.get("page")
            passages.append(Passage(
                chunk_id=str(doc_id),
                text=str(text or ""),
                source=str(m.get("source", "")),
                section=str(m.get("section", "")),
                page=int(str(page_val)) if page_val is not None else None,
                score=round(1.0 - float(dist), 4),  # cosine distance → similarity
            ))
            if len(passages) >= k:
                break

        log.info("retrieved",
                 extra={"k": k, "returned": len(passages),
                        "scores": [p.score for p in passages]})
        return passages
