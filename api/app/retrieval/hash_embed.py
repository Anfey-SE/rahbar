"""Deterministic feature-hashing embedder with no network calls."""

from __future__ import annotations

import hashlib
import math
import re

_TOKEN_RE = re.compile(r"\b\w+\b")
HASH_DIMENSION = 768


def hash_embed(text: str, dim: int = HASH_DIMENSION) -> list[float]:
    """Deterministic feature-hashing embedder.

    Extracts lowercase word tokens, hashes each token with hashlib.sha256,
    maps to dim dimensions with sign hashing, and applies L2 normalization.
    Completely local with no network calls.
    """
    tokens = _TOKEN_RE.findall(text.lower())
    if not tokens:
        vec = [0.0] * dim
        vec[0] = 1.0
        return vec

    vec = [0.0] * dim
    for tok in tokens:
        h = hashlib.sha256(tok.encode("utf-8")).digest()
        idx = int.from_bytes(h[:4], "big") % dim
        sign = 1.0 if h[4] % 2 == 0 else -1.0
        vec[idx] += sign

    norm = math.sqrt(sum(x * x for x in vec))
    if norm == 0.0:
        vec[0] = 1.0
        return vec
    return [x / norm for x in vec]
