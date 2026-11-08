"""Unit tests for ChromaRetriever with mocked ChromaDB and Ollama."""

from __future__ import annotations

import json
from unittest.mock import MagicMock, patch

from app.config import Settings
from app.retrieval.chroma import ChromaRetriever
from app.schemas import Passage


def test_chroma_retriever_normal_query() -> None:
    settings = Settings(
        chroma_host="localhost",
        chroma_port=8000,
        collection="rahbar",
        top_k=2,
        embed_model="nomic-embed-text",
        ollama_url="http://mock-ollama:11434",
    )

    with patch("chromadb.HttpClient") as mock_http:
        mock_client = MagicMock()
        mock_col = MagicMock()
        mock_http.return_value = mock_client
        mock_client.get_collection.return_value = mock_col

        mock_col.query.return_value = {
            "ids": [["chunk-1", "chunk-2"]],
            "documents": [["First passage text", "Second passage text"]],
            "metadatas": [[
                {"source": "rules.pdf", "section": "1.1", "page": 5, "superseded": False},
                {"source": "rules.pdf", "section": "1.2", "page": 6, "superseded": False},
            ]],
            "distances": [[0.15, 0.25]],
        }

        retriever = ChromaRetriever(settings)

        # Mock embedding
        with patch.object(retriever, "_embed", return_value=[0.1] * 768):
            passages = retriever.retrieve("What is the rule?", k=2)

        assert len(passages) == 2
        p1, p2 = passages
        assert isinstance(p1, Passage)
        assert p1.chunk_id == "chunk-1"
        assert p1.score == round(1.0 - 0.15, 4)
        assert p1.page == 5
        assert p2.score == round(1.0 - 0.25, 4)

        # Verify where filter excluded superseded
        mock_col.query.assert_called_once()
        _, kwargs = mock_col.query.call_args
        assert kwargs["where"] == {"superseded": False}


def test_chroma_retriever_older_version_query() -> None:
    settings = Settings(
        collection="rahbar",
        top_k=3,
    )

    with patch("chromadb.HttpClient") as mock_http:
        mock_client = MagicMock()
        mock_col = MagicMock()
        mock_http.return_value = mock_client
        mock_client.get_collection.return_value = mock_col

        mock_col.query.return_value = {
            "ids": [["old-chunk"]],
            "documents": [["Old 2023 rule text"]],
            "metadatas": [[{"source": "rules-2023.pdf", "section": "4.1", "page": 14}]],
            "distances": [[0.20]],
        }

        retriever = ChromaRetriever(settings)

        with patch.object(retriever, "_embed", return_value=[0.2] * 768):
            passages = retriever.retrieve("What was the 2023 course registration rule?", k=3)

        assert len(passages) == 1
        assert passages[0].chunk_id == "old-chunk"
        # Superseded filter should be None for older version query
        _, kwargs = mock_col.query.call_args
        assert kwargs["where"] is None


def test_chroma_retriever_error_handling() -> None:
    settings = Settings()
    with patch("chromadb.HttpClient") as mock_http:
        mock_client = MagicMock()
        mock_http.return_value = mock_client
        mock_client.get_collection.side_effect = RuntimeError("Chroma connection lost")

        retriever = ChromaRetriever(settings)
        assert retriever.retrieve("test question") == []


def test_chroma_retriever_embed_call() -> None:
    settings = Settings(ollama_url="http://mock-ollama:11434", embed_model="nomic-embed-text")
    with patch("chromadb.HttpClient"):
        retriever = ChromaRetriever(settings)

    fake_resp = MagicMock()
    fake_resp.read.return_value = json.dumps({"embeddings": [[0.5, 0.6, 0.7]]}).encode("utf-8")
    fake_resp.__enter__.return_value = fake_resp

    with patch("urllib.request.urlopen", return_value=fake_resp):
        vec = retriever._embed("hello world")
        assert vec == [0.5, 0.6, 0.7]
