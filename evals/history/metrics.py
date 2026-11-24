"""Retrieval metrics over ranked result ids: recall@k, reciprocal rank, nDCG@k (graded). Standard library."""

from __future__ import annotations

import math
from typing import Dict, Sequence


def recall_at(ranked: Sequence[str], relevant: Dict[str, int], k: int = 5) -> float:
    """Relevant ids (any grade) in the top k, over min(|relevant|, k), so a perfect top k scores 1."""
    if not relevant:
        return 0.0
    hits = sum(1 for r in ranked[:k] if relevant.get(r, 0) > 0)
    return hits / min(len(relevant), k)


def reciprocal_rank(ranked: Sequence[str], relevant: Dict[str, int]) -> float:
    for i, r in enumerate(ranked, start=1):
        if relevant.get(r, 0) > 0:
            return 1.0 / i
    return 0.0


def ndcg_at(ranked: Sequence[str], relevant: Dict[str, int], k: int = 5) -> float:
    """nDCG with gain 2^grade - 1 and a log2(rank + 1) discount."""
    def dcg(grades: Sequence[int]) -> float:
        return sum((2 ** g - 1) / math.log2(i + 1) for i, g in enumerate(grades, start=1))

    ideal = dcg(sorted(relevant.values(), reverse=True)[:k])
    if ideal == 0:
        return 0.0
    return dcg([relevant.get(r, 0) for r in ranked[:k]]) / ideal


def mean(values: Sequence[float]) -> float:
    return sum(values) / len(values) if values else 0.0
