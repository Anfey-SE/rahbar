"""Unit tests for Redis semantic cache with mocked Chroma and Redis."""

from __future__ import annotations

import json
from unittest.mock import MagicMock, patch

from app.config import Settings
from app.schemas import AskResponse, Citation, RetrievedRef
from app.services.cache import SemanticCache, _cosine_similarity, should_never_cache


def _sample_response(answered: bool = True) -> AskResponse:
    citations = [Citation(source="rules.pdf", section="5.4", page=25)] if answered else []
    return AskResponse(
        request_id="req-123",
        answer="A CGPA of 2.00 is required.",
        citations=citations,
        answered_from_context=answered,
        confidence="high" if answered else "low",
        retrieved=[
            RetrievedRef(chunk_id="c1", source="rules.pdf", section="5.4", page=25, score=0.88)
        ],
        provider="ollama",
    )


def test_cosine_similarity() -> None:
    assert _cosine_similarity([1.0, 0.0], [1.0, 0.0]) == 1.0
    assert _cosine_similarity([1.0, 0.0], [0.0, 1.0]) == 0.0
    assert _cosine_similarity([], [1.0]) == 0.0


def test_should_never_cache_rules() -> None:
    # Follow-ups must never cache
    assert should_never_cache("And what about the fee?") is True
    assert should_never_cache("Also tell me about probation.") is True
    assert should_never_cache("What about the deadline?") is True

    # Abstentions must never cache
    assert should_never_cache("What is the fee?", _sample_response(answered=False)) is True

    # Fallbacks must never cache
    fallback_resp = _sample_response(answered=True)
    fallback_resp.answer = "I could not generate an answer at this time. Please try again."
    assert should_never_cache("test", fallback_resp) is True

    # Valid answers can be cached
    valid_resp = _sample_response(answered=True)
    assert should_never_cache("What is the CGPA required?", valid_resp) is False


def test_semantic_cache_lookup_hit() -> None:
    settings = Settings(
        llm_provider="ollama-host",
        cache_threshold=0.90,
        redis_host="localhost",
        redis_port=6379,
    )
    cache = SemanticCache(settings)

    mock_redis = MagicMock()
    cache._client = mock_redis
    cache._cached_index_version = "20261108T120000Z"

    stored_entry = {
        "question": "What CGPA is required to avoid warning?",
        "embedding": [1.0, 0.0],
        "response": _sample_response(answered=True).model_dump(),
    }
    mock_redis.keys.return_value = ["cache:20261108T120000Z:abc12345"]
    mock_redis.get.return_value = json.dumps(stored_entry)

    # Query with identical vector -> sim 1.0 >= 0.90
    result = cache.lookup(
        "What CGPA is needed to stay off probation?",
        question_embedding=[1.0, 0.0],
    )
    assert result is not None
    assert result.answer == "A CGPA of 2.00 is required."


def test_semantic_cache_lookup_miss_below_threshold() -> None:
    settings = Settings(
        llm_provider="ollama-host",
        cache_threshold=0.90,
    )
    cache = SemanticCache(settings)

    mock_redis = MagicMock()
    cache._client = mock_redis
    cache._cached_index_version = "20261108T120000Z"

    stored_entry = {
        "question": "What CGPA is required?",
        "embedding": [1.0, 0.0],
        "response": _sample_response(answered=True).model_dump(),
    }
    mock_redis.keys.return_value = ["cache:20261108T120000Z:abc12345"]
    mock_redis.get.return_value = json.dumps(stored_entry)

    # Orthogonal query vector -> sim 0.0 < 0.90
    result = cache.lookup(
        "Different question",
        question_embedding=[0.0, 1.0],
    )
    assert result is None


def test_semantic_cache_store() -> None:
    settings = Settings(
        llm_provider="ollama-host",
    )
    cache = SemanticCache(settings)

    mock_redis = MagicMock()
    cache._client = mock_redis
    cache._cached_index_version = "20261108T120000Z"

    resp = _sample_response(answered=True)
    stored = cache.store(
        "What is the CGPA required?",
        resp,
        question_embedding=[1.0, 0.5],
    )
    assert stored is True
    mock_redis.set.assert_called_once()
    key, val = mock_redis.set.call_args[0]
    assert key.startswith("cache:20261108T120000Z:")
    entry = json.loads(val)
    assert entry["question"] == "What is the CGPA required?"
    assert entry["embedding"] == [1.0, 0.5]


def test_semantic_cache_reads_index_version_from_chroma() -> None:
    settings = Settings(
        llm_provider="ollama-host",
        collection="rahbar",
    )
    cache = SemanticCache(settings)

    with patch("chromadb.HttpClient") as mock_http:
        mock_client = MagicMock()
        mock_col = MagicMock()
        mock_col.metadata = {"index_version": "20261108T999999Z"}
        mock_client.get_collection.return_value = mock_col
        mock_http.return_value = mock_client

        ver = cache.get_index_version()
        assert ver == "20261108T999999Z"
