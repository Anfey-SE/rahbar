"""Unit tests for OllamaProvider and HostedProvider with mocked HTTP."""

from __future__ import annotations

import json
from unittest.mock import MagicMock, patch

from app.config import Settings
from app.providers.llm.hosted import HostedProvider
from app.providers.llm.ollama import OllamaProvider
from app.schemas import Answer, Passage


def _make_passages() -> list[Passage]:
    return [
        Passage(
            chunk_id="c1",
            text="Undergraduate students must maintain CGPA >= 2.00.",
            source="rules.pdf",
            section="5.4",
            page=25,
            score=0.88,
        )
    ]


def test_ollama_provider_success() -> None:
    settings = Settings(
        llm_model="llama3.2:3b",
        temperature=0.2,
        ollama_url="http://mock-ollama:11434",
    )
    provider = OllamaProvider(settings)

    model_answer = {
        "reasoning": "Clause 5.4 specifies minimum CGPA.",
        "answer": "An undergraduate needs a CGPA of 2.00 or higher.",
        "citations": [{"source": "rules.pdf", "section": "5.4", "page": 25}],
        "answered_from_context": True,
        "confidence": "high",
    }
    ollama_response = {
        "model": "llama3.2:3b",
        "message": {"role": "assistant", "content": json.dumps(model_answer)},
        "prompt_eval_count": 120,
        "eval_count": 45,
    }

    fake_resp = MagicMock()
    fake_resp.read.return_value = json.dumps(ollama_response).encode("utf-8")
    fake_resp.__enter__.return_value = fake_resp

    with patch("urllib.request.urlopen", return_value=fake_resp) as mock_urlopen:
        ans = provider.generate("What CGPA is needed?", _make_passages())

        assert isinstance(ans, Answer)
        assert ans.answered_from_context is True
        assert ans.confidence == "high"
        assert len(ans.citations) == 1
        assert "2.00" in ans.answer

        mock_urlopen.assert_called_once()
        req = mock_urlopen.call_args[0][0]
        payload = json.loads(req.data.decode("utf-8"))
        assert payload["model"] == "llama3.2:3b"
        assert payload["options"]["temperature"] == 0.2
        # Verifies structured format schema was supplied
        assert isinstance(payload["format"], dict)
        assert "properties" in payload["format"]


def test_ollama_provider_fallback_on_failure() -> None:
    settings = Settings(ollama_url="http://mock-ollama:11434")
    provider = OllamaProvider(settings)

    with patch("urllib.request.urlopen", side_effect=RuntimeError("Connection refused")):
        ans = provider.generate("test", _make_passages())
        assert ans.answered_from_context is False
        assert ans.confidence == "low"
        assert "could not generate" in ans.answer


def test_hosted_provider_success() -> None:
    settings = Settings(
        llm_model="gpt-4o-mini",
        temperature=0.1,
        hosted_base_url="https://api.hosted.ai/v1",
        llm_api_key="secret-key-123",
    )
    provider = HostedProvider(settings)

    model_answer = {
        "reasoning": "Retrieved from clause 5.4.",
        "answer": "CGPA of at least 2.00.",
        "citations": [{"source": "rules.pdf", "section": "5.4", "page": 25}],
        "answered_from_context": True,
        "confidence": "high",
    }
    mock_post_resp = MagicMock()
    mock_post_resp.json.return_value = {
        "choices": [{"message": {"content": json.dumps(model_answer)}}],
        "usage": {"prompt_tokens": 85, "completion_tokens": 28},
    }

    with patch("httpx.Client") as mock_client_cls:
        mock_client = MagicMock()
        mock_client.__enter__.return_value = mock_client
        mock_client.post.return_value = mock_post_resp
        mock_client_cls.return_value = mock_client

        ans = provider.generate("What is required?", _make_passages())

        assert isinstance(ans, Answer)
        assert ans.answered_from_context is True
        assert ans.confidence == "high"
        mock_client.post.assert_called_once()
        _, kwargs = mock_client.post.call_args
        assert kwargs["headers"]["Authorization"] == "Bearer secret-key-123"


def test_hosted_provider_fallback_on_failure() -> None:
    settings = Settings(
        hosted_base_url="https://api.hosted.ai/v1",
        llm_api_key="key",
    )
    provider = HostedProvider(settings)

    with patch("httpx.Client") as mock_client_cls:
        mock_client = MagicMock()
        mock_client.__enter__.return_value = mock_client
        mock_client.post.side_effect = RuntimeError("HTTP error")
        mock_client_cls.return_value = mock_client

        ans = provider.generate("test", _make_passages())
        assert ans.answered_from_context is False
        assert ans.confidence == "low"

