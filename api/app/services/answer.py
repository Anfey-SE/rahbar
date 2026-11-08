"""Turns a question into an AskResponse. Query pipeline with cache, floor, abstention."""

from __future__ import annotations

import logging
import re
import time

from ..config import Settings
from ..logjson import request_id_var
from ..providers.llm.base import LLMProvider
from ..retrieval.base import Retriever
from ..routes.metrics import (
    ABSTENTIONS,
    FALLBACKS,
    GENERATION_LATENCY,
    RETRIEVAL_LATENCY,
)
from ..schemas import Answer, AskResponse, Citation, Passage, RetrievedRef
from .cache import SemanticCache

log = logging.getLogger("rahbar.answer")

_CANNOT_ANSWER_MSG = (
    "I do not have enough information in my knowledge base to answer this question reliably."
)
_FALLBACK_MSG = "I could not generate an answer at this time. Please try again."


def _parse_clause_tuple(c: str) -> tuple[int, ...] | None:
    parts = c.split(".")
    try:
        return tuple(int(x) for x in parts)
    except ValueError:
        return None


def _citation_matches_passage(c: Citation, p: Passage) -> bool:
    if c.source != p.source:
        return False

    c_sec = c.section.strip()
    p_sec = p.section.strip()

    # Exact or substring match (case-insensitive)
    if c_sec == p_sec or c_sec.lower() in p_sec.lower() or p_sec.lower() in c_sec.lower():
        return True

    # Clause range match (e.g. c.section = "5.4", p.section = "5.4-5.7 Academic Warning/Probation")
    c_m = re.match(r"^(\d+\.\d+(?:\.\d+)*)$", c_sec)
    p_m = re.match(r"^(\d+\.\d+(?:\.\d+)*)(?:-(\d+\.\d+(?:\.\d+)*))?", p_sec)
    if c_m and p_m:
        c_k = _parse_clause_tuple(c_m.group(1))
        start_k = _parse_clause_tuple(p_m.group(1))
        end_k = _parse_clause_tuple(p_m.group(2)) if p_m.group(2) else start_k
        if c_k and start_k and end_k:
            if start_k[0] == c_k[0] and start_k <= c_k <= end_k:
                return True

    # Check if clause is explicitly mentioned in passage text
    if re.search(r"(?:^|\s)" + re.escape(c_sec) + r"(?:\s|[.:\-]|\Z)", p.text):
        return True

    return False


def _normalize_citation(c: Citation, valid_passages: list[Passage]) -> Citation:
    raw_src = c.source.strip()
    idx_m = re.match(r"^\[?(?:passage\s*)?(\d+)\]?$", raw_src, re.IGNORECASE)
    if idx_m:
        idx = int(idx_m.group(1))
        if 1 <= idx <= len(valid_passages):
            target_p = valid_passages[idx - 1]
            return Citation(
                source=target_p.source,
                section=c.section if c.section else target_p.section,
                page=c.page if c.page is not None else target_p.page,
            )
    return c


def _verify_citations(
    answer_citations: list[Citation], valid_passages: list[Passage]
) -> list[Citation]:
    verified: list[Citation] = []
    for raw_c in answer_citations:
        norm_c = _normalize_citation(raw_c, valid_passages)
        if any(_citation_matches_passage(norm_c, p) for p in valid_passages):
            verified.append(norm_c)
    return verified


def answer_question(
    question: str,
    retriever: Retriever,
    provider: LLMProvider,
    settings: Settings,
) -> AskResponse:
    # 1. Semantic Cache check
    cache: SemanticCache | None = None
    if settings.llm_provider != "stub":
        cache = SemanticCache(settings)
        cached = cache.lookup(question)
        if cached is not None:
            cached.request_id = request_id_var.get()
            return cached

    # 2. Retrieve passages
    t_ret_start = time.perf_counter()
    passages = retriever.retrieve(question, k=settings.top_k)
    RETRIEVAL_LATENCY.observe(max(0.0, time.perf_counter() - t_ret_start))

    # 3. Relevance floor check
    relevance_floor = getattr(settings, "relevance_floor", 0.64)
    valid_passages = [p for p in passages if p.score >= relevance_floor]

    if not valid_passages:
        ABSTENTIONS.labels(reason="below_relevance_floor").inc()
        log.info(
            "request_completed",
            extra={
                "top_k": settings.top_k,
                "chunk_ids": [p.chunk_id for p in passages],
                "scores": [round(p.score, 4) for p in passages],
                "abstention": True,
                "reason": "below_relevance_floor",
                "provider": provider.name,
            },
        )
        return AskResponse(
            request_id=request_id_var.get(),
            answer=_CANNOT_ANSWER_MSG,
            citations=[],
            answered_from_context=False,
            confidence="low",
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

    # 4. Generate answer with one retry on failure
    t_gen_start = time.perf_counter()
    answer: Answer | None = None
    for attempt in range(1, 3):
        try:
            answer = provider.generate(question, valid_passages)
            if isinstance(answer, Answer):
                break
        except Exception as exc:
            log.warning(
                "provider generation attempt failed",
                extra={"attempt": attempt, "error": str(exc)},
            )

    GENERATION_LATENCY.observe(max(0.0, time.perf_counter() - t_gen_start))

    if answer is None:
        FALLBACKS.labels(reason="validation_failure").inc()
        log.error("all generation attempts failed; using canned fallback")
        answer = Answer(
            reasoning="provider error after retry",
            answer=_FALLBACK_MSG,
            citations=[],
            answered_from_context=False,
            confidence="low",
        )

    # 5. Citation verification
    valid_citations = _verify_citations(answer.citations, valid_passages)
    if len(valid_citations) < len(answer.citations):
        dropped_count = len(answer.citations) - len(valid_citations)
        log.warning("dropped invalid citations", extra={"dropped": dropped_count})

    answered_from_context = answer.answered_from_context
    answer_text = answer.answer
    confidence = answer.confidence
    is_abstention = False

    # Honest abstention: claimed answered_from_context but no valid citations remain
    if answered_from_context and not valid_citations:
        answered_from_context = False
        answer_text = _CANNOT_ANSWER_MSG
        confidence = "low"
        is_abstention = True
        ABSTENTIONS.labels(reason="no_valid_citations").inc()
    elif not answered_from_context:
        is_abstention = True
        ABSTENTIONS.labels(reason="model_abstention").inc()

    res = AskResponse(
        request_id=request_id_var.get(),
        answer=answer_text,
        citations=valid_citations,
        answered_from_context=answered_from_context,
        confidence=confidence,
        retrieved=[
            RetrievedRef(
                chunk_id=p.chunk_id,
                source=p.source,
                section=p.section,
                page=p.page,
                score=p.score,
            )
            for p in valid_passages
        ],
        provider=provider.name,
    )

    # 6. One JSON log line per request with top_k, chunk ids, scores, abstention flag
    log.info(
        "request_completed",
        extra={
            "top_k": settings.top_k,
            "chunk_ids": [p.chunk_id for p in valid_passages],
            "scores": [round(p.score, 4) for p in valid_passages],
            "abstention": is_abstention,
            "provider": provider.name,
        },
    )

    # 7. Semantic cache storage
    if cache is not None and not is_abstention:
        cache.store(question, res)

    return res
