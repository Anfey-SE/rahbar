"""Turns a question into an AskResponse. Query pipeline with cache, floor, abstention."""

from __future__ import annotations

import logging

from ..config import Settings
from ..logjson import request_id_var
from ..providers.llm.base import LLMProvider
from ..retrieval.base import Retriever
from ..schemas import AskResponse, RetrievedRef
from .cache import SemanticCache

log = logging.getLogger("rahbar.answer")

_FOLLOWUP_PREFIXES = ("and ", "also ", "what about ", "follow", "additionally ")


def _is_followup(question: str) -> bool:
    q = question.strip().lower()
    return any(q.startswith(p) for p in _FOLLOWUP_PREFIXES)


def answer_question(
    question: str,
    retriever: Retriever,
    provider: LLMProvider,
    settings: Settings,
) -> AskResponse:
    if settings.llm_provider != "stub":
        cache = SemanticCache(settings)
        cached = cache.lookup(question)
        if cached is not None:
            cached.request_id = request_id_var.get()
            return cached

    relevance_floor: float = getattr(settings, "relevance_floor", 0.35)

    passages = retriever.retrieve(question, k=settings.top_k)

    # Relevance floor: drop passages that are too dissimilar
    passages = [p for p in passages if p.score >= relevance_floor]

    # Honest abstention: nothing passes the floor
    if not passages:
        log.info(
            "abstaining: no passages above relevance floor",
            extra={"retriever": retriever.name, "top_k": settings.top_k,
                   "floor": relevance_floor},
        )
        return AskResponse(
            request_id=request_id_var.get(),
            answer=(
                "I do not have enough information in my knowledge base "
                "to answer this question reliably."
            ),
            citations=[],
            answered_from_context=False,
            confidence="low",
            retrieved=[],
            provider=provider.name,
        )

    answer = provider.generate(question, passages)

    # Citation check: drop any citation not matching a retrieved passage
    retrieved_pairs = {(p.source, p.section) for p in passages}
    valid_citations = [
        c for c in answer.citations
        if (c.source, c.section) in retrieved_pairs
    ]
    if len(valid_citations) < len(answer.citations):
        log.warning(
            "dropped invalid citations",
            extra={"dropped": len(answer.citations) - len(valid_citations)},
        )

    log.info(
        "answered",
        extra={
            "retriever": retriever.name,
            "provider": provider.name,
            "top_k": settings.top_k,
            "chunk_ids": [p.chunk_id for p in passages],
            "scores": [round(p.score, 4) for p in passages],
            "answered_from_context": answer.answered_from_context,
        },
    )
    res = AskResponse(
        request_id=request_id_var.get(),
        answer=answer.answer,
        citations=valid_citations,
        answered_from_context=answer.answered_from_context,
        confidence=answer.confidence,
        retrieved=[
            RetrievedRef(
                chunk_id=p.chunk_id,
                source=p.source,
                section=p.section,
                page=p.page,
                score=p.score,
            )
            for p in passages
        ],
        provider=provider.name,
    )
    if settings.llm_provider != "stub":
        cache = SemanticCache(settings)
        cache.store(question, res)
    return res
