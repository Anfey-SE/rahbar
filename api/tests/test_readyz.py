"""Tests for /readyz and the embedding-pin check."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient


class MockCollection:
    def __init__(self, count: int = 3, embed_model: str = "dummy-768") -> None:
        self._count = count
        self.metadata = {"embed_model": embed_model, "dimension": 768}

    def count(self) -> int:
        return self._count


class MockChromaClient:
    def __init__(
        self,
        reachable: bool = True,
        collection_exists: bool = True,
        count: int = 3,
        embed_model: str = "dummy-768",
    ) -> None:
        self.reachable = reachable
        self.collection_exists = collection_exists
        self.count_val = count
        self.embed_model = embed_model

    def heartbeat(self) -> int:
        if not self.reachable:
            raise ConnectionError("Chroma server unreachable")
        return 123456789

    def get_collection(self, name: str) -> MockCollection:
        if not self.reachable:
            raise ConnectionError("Chroma server unreachable")
        if not self.collection_exists:
            raise ValueError(f"Collection '{name}' does not exist")
        return MockCollection(count=self.count_val, embed_model=self.embed_model)


class MockRedisClient:
    def __init__(self, reachable: bool = True) -> None:
        self.reachable = reachable

    def ping(self) -> bool:
        if not self.reachable:
            import redis

            raise redis.ConnectionError("Redis connection refused")
        return True


@pytest.fixture()
def client(monkeypatch: pytest.MonkeyPatch) -> TestClient:
    monkeypatch.setenv("RETRIEVER", "stub")
    monkeypatch.setenv("LLM_PROVIDER", "stub")
    monkeypatch.setenv("EMBED_MODEL", "dummy-768")
    monkeypatch.setenv("CHROMA_COLLECTION", "rahbar")

    from app.main import app

    with TestClient(app) as c:
        yield c


def test_readyz_all_healthy(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    mock_chroma = MockChromaClient(
        reachable=True, collection_exists=True, count=3, embed_model="dummy-768"
    )
    mock_redis = MockRedisClient(reachable=True)

    monkeypatch.setattr("chromadb.HttpClient", lambda **kwargs: mock_chroma)
    monkeypatch.setattr("redis.Redis", lambda **kwargs: mock_redis)

    r = client.get("/readyz")
    assert r.status_code == 200
    data = r.json()
    assert data["status"] == "ready"
    assert data["checks"]["chroma"] == "ok"
    assert data["checks"]["collection"] == "ok"
    assert data["checks"]["count"] == 3
    assert data["checks"]["embed_model"] == "dummy-768"
    assert data["checks"]["redis"] == "ok"


def test_readyz_empty_collection(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    mock_chroma = MockChromaClient(
        reachable=True, collection_exists=True, count=0, embed_model="dummy-768"
    )
    mock_redis = MockRedisClient(reachable=True)

    monkeypatch.setattr("chromadb.HttpClient", lambda **kwargs: mock_chroma)
    monkeypatch.setattr("redis.Redis", lambda **kwargs: mock_redis)

    r = client.get("/readyz")
    assert r.status_code == 503
    data = r.json()
    assert data["status"] == "not_ready"
    assert data["failing_dependency"] == "collection"
    assert "empty" in data["detail"]


def test_readyz_chroma_down(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    mock_chroma = MockChromaClient(reachable=False)
    mock_redis = MockRedisClient(reachable=True)

    monkeypatch.setattr("chromadb.HttpClient", lambda **kwargs: mock_chroma)
    monkeypatch.setattr("redis.Redis", lambda **kwargs: mock_redis)

    r = client.get("/readyz")
    assert r.status_code == 503
    data = r.json()
    assert data["status"] == "not_ready"
    assert data["failing_dependency"] == "chroma"


def test_readyz_redis_down(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    mock_chroma = MockChromaClient(
        reachable=True, collection_exists=True, count=3, embed_model="dummy-768"
    )
    mock_redis = MockRedisClient(reachable=False)

    monkeypatch.setattr("chromadb.HttpClient", lambda **kwargs: mock_chroma)
    monkeypatch.setattr("redis.Redis", lambda **kwargs: mock_redis)

    r = client.get("/readyz")
    assert r.status_code == 503
    data = r.json()
    assert data["status"] == "not_ready"
    assert data["failing_dependency"] == "redis"


def test_readyz_model_mismatch(
    client: TestClient, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    # Collection was embedded with nomic-embed-text, but API expects dummy-768
    mock_chroma = MockChromaClient(
        reachable=True, collection_exists=True, count=3, embed_model="nomic-embed-text"
    )
    mock_redis = MockRedisClient(reachable=True)

    monkeypatch.setattr("chromadb.HttpClient", lambda **kwargs: mock_chroma)
    monkeypatch.setattr("redis.Redis", lambda **kwargs: mock_redis)

    with caplog.at_level("ERROR"):
        r = client.get("/readyz")

    assert r.status_code == 503
    data = r.json()
    assert data["status"] == "not_ready"
    assert data["failing_dependency"] == "embed_model"
    assert data["expected"] == "dummy-768"
    assert data["found"] == "nomic-embed-text"

    # Verify error log was emitted
    assert any("embedding model mismatch" in record.message for record in caplog.records)
