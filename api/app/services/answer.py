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
from ..traces.record import RetrievedChunkTrace, TraceRecord
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

    # List-item markers like 'c', 'a', 'b' or single letters are never valid sections
    if (
        len(c_sec) <= 2
        and not c_sec.isdigit()
        or re.match(r"^\(?[a-zA-Z0-9]{1,2}[.)]?$", c_sec)
    ):
        return False

    # Exact or substring match (case-insensitive) for meaningful section names
    if c_sec.lower() == p_sec.lower():
        return True
    if len(c_sec) >= 3 and (c_sec.lower() in p_sec.lower() or p_sec.lower() in c_sec.lower()):
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

    # When model outputs the human document title instead of the PDF filename
    # (e.g. "Undergraduate Academic Rules and Regulations 2026" instead of "academic-rules-handbook-jun-2026.pdf")
    if not raw_src.lower().endswith(".pdf"):
        for p in valid_passages:
            p_base = p.source.replace(".pdf", "").replace("-", " ")
            if (
                p_base.lower() in raw_src.lower()
                or raw_src.lower() in p_base.lower()
                or ("academic rules" in raw_src.lower() and "academic-rules" in p.source)
            ) and _citation_matches_passage(
                Citation(source=p.source, section=c.section, page=c.page), p
            ):
                return Citation(
                    source=p.source,
                    section=c.section if c.section else p.section,
                    page=c.page if c.page is not None else p.page,
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
    t_total_start = time.perf_counter()

    # D1: initialise the trace record for this query
    trace = TraceRecord(
        query=question,
        embed_model=settings.embed_model,
        collection=settings.collection,
        model=settings.llm_model,
        model_params={"temperature": settings.temperature},
    )

    # 1. Semantic Cache check
    cache: SemanticCache | None = None
    if settings.llm_provider != "stub":
        cache = SemanticCache(settings)
        t_cache_start = time.perf_counter()
        cached = cache.lookup(question)
        trace.stage_latencies.cache_lookup_ms = (time.perf_counter() - t_cache_start) * 1000
        if cached is not None:
            cached.request_id = request_id_var.get()
            # D1: emit trace for cache-hit path
            trace.cache_hit = True
            trace.response = cached.answer
            trace.answered_from_context = cached.answered_from_context
            trace.confidence = cached.confidence
            trace.request_id = request_id_var.get()
            trace.stage_latencies.total_ms = (time.perf_counter() - t_total_start) * 1000
            trace.emit()
            return cached

    # 2. Retrieve passages
    t_ret_start = time.perf_counter()
    passages = retriever.retrieve(question, k=settings.top_k)
    retrieval_dt = time.perf_counter() - t_ret_start
    RETRIEVAL_LATENCY.observe(max(0.0, retrieval_dt))
    trace.stage_latencies.retrieval_ms = retrieval_dt * 1000

    # D1: populate retrieved chunk traces
    trace.retrieved = [
        RetrievedChunkTrace(
            chunk_id=p.chunk_id,
            doc_id=p.source,
            doc_version=getattr(p, "doc_version", "unknown"),
            score=p.score,
        )
        for p in passages
    ]

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
        # D4: guardrail — relevance floor abstention
        detail = (
            f"top score={passages[0].score:.3f} below floor={relevance_floor}"
            if passages
            else "no passages retrieved"
        )
        trace.add_guardrail("relevance_floor_abstention", detail=detail, triggered=True)
        trace.answered_from_context = False
        trace.abstention_reason = "below_relevance_floor"
        trace.confidence = "low"
        trace.request_id = request_id_var.get()
        trace.stage_latencies.total_ms = (time.perf_counter() - t_total_start) * 1000
        trace.emit()
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

    # D1: build an approximate prompt for hashing (actual prompt built inside provider)
    _prompt_text = (
        "[SYSTEM] Answer using only the provided passages.\n\n[PASSAGES]\n"
        + "\n---\n".join(f"{i+1}. {p.text}" for i, p in enumerate(valid_passages))
        + f"\n\n[QUESTION]\n{question}"
    )
    trace.set_prompt(_prompt_text)

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

    gen_dt = time.perf_counter() - t_gen_start
    GENERATION_LATENCY.observe(max(0.0, gen_dt))
    trace.stage_latencies.generation_ms = gen_dt * 1000

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

    trace.response = answer.answer

    # 5. Citation verification
    valid_citations = _verify_citations(answer.citations, valid_passages)
    if len(valid_citations) < len(answer.citations):
        dropped_count = len(answer.citations) - len(valid_citations)
        log.warning("dropped invalid citations", extra={"dropped": dropped_count})

    trace.citations = [c.model_dump() for c in valid_citations]

    log.info(
        "answer debug",
        extra={
            "raw_answered_from_context": answer.answered_from_context,
            "raw_citations": [c.model_dump() for c in answer.citations],
            "valid_citations": [c.model_dump() for c in valid_citations],
            "reasoning": answer.reasoning,
        },
    )

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
        # D4: guardrail — honest abstention
        trace.add_guardrail(
            "honest_abstention",
            detail="answered_from_context=True but no valid citations survived verification",
            triggered=True,
        )
    elif not answered_from_context:
        is_abstention = True
        ABSTENTIONS.labels(reason="model_abstention").inc()
        # D4: guardrail — model declined to answer
        trace.add_guardrail(
            "model_abstention",
            detail="model declined to answer from context",
            triggered=True,
        )

    # D1: set trace outcome fields
    trace.answered_from_context = answered_from_context
    trace.confidence = confidence
    if is_abstention:
        trace.abstention_reason = (
            "no_valid_citations"
            if answer.answered_from_context and not valid_citations
            else "model_abstention"
        )

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

    # D1: emit trace at end of normal path
    trace.request_id = request_id_var.get()
    trace.stage_latencies.total_ms = (time.perf_counter() - t_total_start) * 1000
    trace.emit()

    return res
