"""Active learning (core/ai/review): the queue least sure first, and how a rejection names the result (its AI columns)."""
from mikage.core.ai.review import (
    ACCEPTED, REJECTED_STATE, SURE, TO_REVIEW, is_rejection, queue, reject_margin, rejected_tags, review_order, state_of,
)
from mikage.core.ai_results import AiResult, AiResultSource, origin_for
from mikage.core.obs.ai_origin import AI_DETECTED, AI_REJECTED, AiOrigin
from mikage.core.obs.areas import CheckedArea


def result(key, score, cls="ship"):
    return AiResult(source="検出@1", oid=key, key=str(key), image_id="i", cls=cls, score=score, model="m", detected_at=1, name="",
                    kind="", activity="", length=None, heading=None, note="", geometry=None, attributes={})


def test_the_queue_is_least_sure_first_without_the_sure_and_the_decided():
    rs = [result(0, 0.9), result(1, 0.3), result(2, None), result(3, 0.6), result(4, 0.5), result(5, 0.1), result(6, 0.85)]
    accepted, rejected = {rs[4].tag}, {rs[5].tag}
    assert queue(rs, accepted, rejected, 0.8) == [1, 3, 2]
    assert [state_of(r, accepted, rejected, 0.8) for r in rs] == [SURE, TO_REVIEW, TO_REVIEW, TO_REVIEW, ACCEPTED, REJECTED_STATE, SURE]
    assert review_order(rs, accepted, rejected, 0.8) == [1, 3, 2, 6, 0, 4, 5]
    # Everything is looked at above 1.
    assert queue(rs, set(), set(), 1.01) == [5, 1, 4, 3, 6, 0, 2]


def _area(ai=None, note=""):
    return CheckedArea(id="a", image_id="i", image_name="i", image_time=None, ring=[[0, 0], [1, 0], [1, 1], [0, 0]], kind="", note=note,
                       created_at=0, updated_at=0, ai=ai)


def test_a_rejection_names_the_result_in_its_ai_columns_not_its_note():
    r = result(3, 0.42)
    layer = AiResultSource(label="検出結果 A", url="", fields={"imageId": "image_id"}, local=True)
    ai = origin_for(r, layer, rejected=True)
    assert ai == AiOrigin(source=AI_REJECTED, layer="検出結果 A", result_id="3", score=0.42, model="m", detected_at=1)
    # Sent to a table first: the table's row.
    sent = origin_for(r, layer, ("転記先", "120"), rejected=True)
    assert (sent.layer, sent.result_id) == ("転記先", "120")
    assert is_rejection(_area(ai)) and not is_rejection(_area(AiOrigin(AI_DETECTED, "x", "3"))) and not is_rejection(_area())
    # The note says nothing (2026-11-27: the old mark there is not read).
    assert not is_rejection(_area(note="AI誤検出: ship 0.42 [AI:検出@1#3]")) and not is_rejection(None)
    assert rejected_tags([_area(ai), _area(note="港の確認"), _area(AiOrigin(AI_DETECTED, "検出結果 A", "4"))]) == {"[AI:検出結果 A#3]"}


def test_the_margin_around_a_wrong_result():
    assert reject_margin(10) == 5 and reject_margin(100) == 20
