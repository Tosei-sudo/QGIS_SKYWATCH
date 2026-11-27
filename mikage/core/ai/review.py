"""
Active learning (能動学習) for the AI results: the person looks first at
what the AI is least sure of, and what they decide goes straight into the
training data (core/ai/train via qgis_io/train_io).

- A result judged right is recorded as an observation (観測記録), so it is
  a labelled object for 「AI 学習（物体検出）」 as any record is.
- A result judged wrong (誤検出) is recorded as a small checked area
  (確認範囲, every kind) around it, its AI columns naming the result with
  SOURCE 「AI誤検出」 (core/obs/ai_origin.py; not its note, 2026-11-27):
  nothing is there. Training reads such an area as a hard negative, a window
  of background around what the model mistook, not left to chance as other
  checked areas are.

Deleting that checked area takes the judgement back.

The queue to look at (要確認): results neither recorded nor rejected, under
the score above which the AI is trusted (確認不要の確信度), least sure first;
results without a score come after those with one.
"""
from __future__ import annotations

from typing import Any, Iterable, Sequence

from ..ai_results import AiResult, recorded_tags
from ..obs.api import AI_REJECTED as REJECTED  # noqa: F401 (read from here too)
#: Results at or above this score need no look (確認不要) by default.
SURE_SCORE = 0.8
#: A wrong result's checked area reaches this share of its size around it, and at least `REJECT_MARGIN_M` metres.
REJECT_MARGIN = 0.2
REJECT_MARGIN_M = 5.0
#: The side (m) of a wrong result's checked area when the result is a point without a length.
REJECT_POINT_M = 20.0

#: A result's review state.
ACCEPTED = "accepted"
REJECTED_STATE = "rejected"
TO_REVIEW = "review"
SURE = "sure"


def is_rejection(area: Any) -> bool:
    """Whether a checked area records a wrong AI result (a hard negative): its SOURCE is 「AI誤検出」."""
    ai = getattr(area, "ai", None)
    return ai is not None and ai.rejected


def rejected_tags(areas: Iterable[Any]) -> set[str]:
    """The keys of results recorded as wrong (by the AI columns of living checked areas)."""
    return recorded_tags(a for a in areas if is_rejection(a))


def state_of(result: AiResult, accepted: set[str], rejected: set[str], sure: float) -> str:
    """Recorded, rejected, sure enough to need no look, or to be looked at."""
    if result.tag in accepted:
        return ACCEPTED
    if result.tag in rejected:
        return REJECTED_STATE
    if result.score is not None and result.score >= sure:
        return SURE
    return TO_REVIEW


def queue(results: Sequence[AiResult], accepted: set[str], rejected: set[str], sure: float = SURE_SCORE) -> list[int]:
    """The indexes of the results to look at, least sure first (no score last, then in order)."""
    todo = [i for i, r in enumerate(results) if state_of(r, accepted, rejected, sure) == TO_REVIEW]
    return sorted(todo, key=lambda i: (results[i].score is None, results[i].score or 0.0, i))


def review_order(results: Sequence[AiResult], accepted: set[str], rejected: set[str], sure: float = SURE_SCORE) -> list[int]:
    """
    The order of a list for review: the queue (least sure first), then the
    sure ones not looked at (most sure last), then those decided (recorded,
    then rejected), each in their order.
    """
    first = queue(results, accepted, rejected, sure)
    states = [state_of(r, accepted, rejected, sure) for r in results]
    sure_rows = sorted((i for i, s in enumerate(states) if s == SURE), key=lambda i: (results[i].score or 0.0, i))
    done = [i for i, s in enumerate(states) if s == ACCEPTED] + [i for i, s in enumerate(states) if s == REJECTED_STATE]
    return first + sure_rows + done


def reject_margin(size_m: float) -> float:
    """How far (m) a wrong result's checked area reaches beyond it."""
    return max(REJECT_MARGIN_M, REJECT_MARGIN * max(size_m, 0.0))
