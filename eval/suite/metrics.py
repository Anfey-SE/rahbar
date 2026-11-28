"""Component-level retrieval metrics and hit rules (Assignment 03, Part B).

Hit rule:
    A retrieved chunk is a hit when it has the SAME source AND
    (the SAME page OR its section / clause range contains the relevant clause).
"""

from __future__ import annotations

import math
import re
from typing import Any

_CLAUSE_RE = re.compile(r"^(\d+(?:\.\d+)*)$")
_RANGE_RE = re.compile(r"^(\d+(?:\.\d+)*)(?:-(\d+(?:\.\d+)*))?")


def _parse_clause(s: str) -> tuple[int, ...] | None:
    """Parse '5.4' into (5, 4). Returns None if not a clause number."""
    try:
        return tuple(int(x) for x in s.split("."))
    except ValueError:
        return None


def section_matches(relevant_section: str, chunk_section: str) -> bool:
    """Return True when relevant_section matches chunk_section.

    - If relevant_section is a bare clause number (e.g. '5.4'), it matches if
      chunk_section starts with a clause range [start, end] containing it
      (with matching major clause), or contains the standalone clause token.
    - Otherwise, matches by case-insensitive substring containment.
    """
    rel = (relevant_section or "").strip()
    chk = (chunk_section or "").strip()
    if not rel or not chk:
        return False

    if rel.lower() == chk.lower():
        return True

    rel_clause_m = _CLAUSE_RE.match(rel)
    if rel_clause_m:
        rel_tuple = _parse_clause(rel_clause_m.group(1))
        chk_range_m = _RANGE_RE.match(chk)
        if rel_tuple and chk_range_m:
            start_tuple = _parse_clause(chk_range_m.group(1))
            end_str = chk_range_m.group(2)
            end_tuple = _parse_clause(end_str) if end_str else start_tuple
            if start_tuple and end_tuple:
                if start_tuple[0] == rel_tuple[0] and start_tuple <= rel_tuple <= end_tuple:
                    return True

        if re.search(r"(?:^|\s)" + re.escape(rel) + r"(?:\s|$|[^.\d])", chk):
            return True
        return False

    return rel.lower() in chk.lower() or chk.lower() in rel.lower()


def is_hit(relevant_item: dict[str, Any], chunk_meta: dict[str, Any]) -> bool:
    """Check if a retrieved chunk matches a golden relevant item.

    A retrieved chunk is a hit when it has the same source AND
    (the same page OR its section or clause range contains the relevant clause).
    """
    rel_source = str(relevant_item.get("source", "")).strip()
    chk_source = str(chunk_meta.get("source", "")).strip()
    if not rel_source or rel_source != chk_source:
        return False

    rel_page = relevant_item.get("page")
    chk_page = chunk_meta.get("page")
    if rel_page is not None and chk_page is not None:
        try:
            if int(rel_page) == int(chk_page):
                return True
        except (TypeError, ValueError):
            pass

    rel_section = str(relevant_item.get("section", "")).strip()
    chk_section = str(chunk_meta.get("section", "")).strip()
    if rel_section and chk_section and section_matches(rel_section, chk_section):
        return True

    return False


def score_question(
    relevant: list[dict[str, Any]],
    retrieved_metas: list[dict[str, Any]],
) -> dict[str, Any]:
    """Compute recall@3, recall@5, MRR, nDCG@5 and first_hit_rank for one question.

    - Binary relevance.
    - For questions with multiple relevant items, recall@k is the fraction of
      relevant items matched within top k, and nDCG@5 uses all matched items
      (each relevant item credited at most once at its highest-ranked hit).
    """
    if not relevant:
        return {
            "recall_at_3": 0.0,
            "recall_at_5": 0.0,
            "mrr": 0.0,
            "ndcg_at_5": 0.0,
            "first_hit_rank": None,
        }

    top5 = retrieved_metas[:5]
    top3 = retrieved_metas[:3]

    # Recall@3 and Recall@5 (fraction of relevant items found)
    matched_3 = sum(1 for r in relevant if any(is_hit(r, m) for m in top3))
    matched_5 = sum(1 for r in relevant if any(is_hit(r, m) for m in top5))
    recall_3 = matched_3 / len(relevant)
    recall_5 = matched_5 / len(relevant)

    # First hit rank (1-indexed) and MRR over top 5
    first_hit_rank: int | None = None
    for idx, meta in enumerate(top5, start=1):
        if any(is_hit(r, meta) for r in relevant):
            first_hit_rank = idx
            break
    mrr = (1.0 / first_hit_rank) if first_hit_rank is not None else 0.0

    # nDCG@5 with binary relevance, crediting each relevant item at most once
    unmatched = list(range(len(relevant)))
    dcg = 0.0
    for rank, meta in enumerate(top5, start=1):
        hit_rel_idx = None
        for r_idx in unmatched:
            if is_hit(relevant[r_idx], meta):
                hit_rel_idx = r_idx
                break
        if hit_rel_idx is not None:
            unmatched.remove(hit_rel_idx)
            dcg += 1.0 / math.log2(rank + 1)

    ideal_count = min(len(relevant), 5)
    idcg = sum(1.0 / math.log2(rank + 1) for rank in range(1, ideal_count + 1))
    ndcg_5 = (dcg / idcg) if idcg > 0 else 0.0

    return {
        "recall_at_3": round(recall_3, 6),
        "recall_at_5": round(recall_5, 6),
        "mrr": round(mrr, 6),
        "ndcg_at_5": round(ndcg_5, 6),
        "first_hit_rank": first_hit_rank,
    }


def aggregate_component_metrics(rows: list[dict[str, Any]]) -> dict[str, Any]:
    """Aggregate mean recall@3, recall@5, MRR, and nDCG@5 across evaluated questions."""
    n = len(rows)
    if n == 0:
        return {
            "recall_at_3": 0.0,
            "recall_at_5": 0.0,
            "mrr": 0.0,
            "ndcg_at_5": 0.0,
            "n_evaluated": 0,
        }
    return {
        "recall_at_3": round(sum(r["recall_at_3"] for r in rows) / n, 4),
        "recall_at_5": round(sum(r["recall_at_5"] for r in rows) / n, 4),
        "mrr": round(sum(r["mrr"] for r in rows) / n, 4),
        "ndcg_at_5": round(sum(r["ndcg_at_5"] for r in rows) / n, 4),
        "n_evaluated": n,
    }
