"""Active learning (core/ai/review): the queue least sure first, and what a rejection's note says."""
from mikage.core.ai.review import (
    ACCEPTED, REJECTED_STATE, SURE, TO_REVIEW, is_rejection, queue, reject_margin, rejected_tags, rejection_note, review_order, state_of,
)
from mikage.core.ai_results import AiResult


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


def test_a_rejection_note_names_the_result():
    r = result(3, 0.42)
    note = rejection_note(r)
    assert note == "AI誤検出: ship 0.42 [AI:検出@1#3]"
    assert is_rejection(note) and not is_rejection("AI検出: ship 0.42 [AI:検出@1#3]") and not is_rejection("AI誤検出") and not is_rejection(None)
    assert rejected_tags([note, "港の確認", "AI検出: x [AI:検出@1#4]"]) == {r.tag}


def test_the_margin_around_a_wrong_result():
    assert reject_margin(10) == 5 and reject_margin(100) == 20
