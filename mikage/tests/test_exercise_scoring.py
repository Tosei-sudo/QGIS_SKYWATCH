"""
判読の練習's scoring (core/training/exercises.py), its results (results.py) and
the sample's exercises (sample_exercises.py), without QGIS.
"""
from datetime import datetime

import pytest

from mikage.core.training.api import (
    Answer, CAME, CHANGE, Exercise, FIND, fitted, GONE, hint, IDENTIFY, image_plans, Mark, MEASURE, result_record,
    sample_exercises, score, scoring_of, STAYED,
)
from mikage.core.training.exercises import heading_error, inside, kind_credit, measure_credit, metres
from mikage.core.training.results import add_result, best_of, exam_taken, mistaken, read_results

LON, LAT = 139.75, 35.30
# 0.001° of longitude here is about 91 m, of latitude about 111 m.


def ship(dx: float = 0, dy: float = 0, **kw) -> Answer:
    return Answer(lon=LON + dx, lat=LAT + dy, **kw)


def mark(a: Answer, i: int = 0, dx: float = 0.0002, **kw) -> Mark:
    values = dict(kind=a.kind, activity=a.activity, length=a.length, width=a.width, heading=a.heading, change=a.change)
    values.update(kw)
    return Mark(f"m{i}", a.lon + dx, a.lat, **values)


def find_exercise(**kw) -> Exercise:
    answers = [ship(0, 0, kind="船舶 > 貨物船 > コンテナ船", activity="停泊", length=180),
               ship(0.01, 0, kind="船舶 > 支援船 > 〇〇級", activity="航行"),
               ship(0.02, 0, kind="船舶 > 貨物船", activity="停泊"),
               ship(0.03, 0, kind="船舶 > 支援船", activity="航行", required=False)]
    area = [[[LON - 0.01, LAT - 0.01], [LON - 0.01, LAT + 0.01], [LON + 0.04, LAT + 0.01], [LON + 0.04, LAT - 0.01], [LON - 0.01, LAT - 0.01]]]
    return Exercise("e1", "船を見つける", FIND, "IMG", area, target_kinds=["船舶"], answers=answers, **kw).numbered()


def test_all_right_is_full_marks_and_the_bonus_adds_nothing_more():
    e = find_exercise()
    o = score(e, [mark(a, i) for i, a in enumerate(e.answers)])
    assert o.score == 100 and o.passed and o.found == 4 and o.missed == 0 and o.extras == []
    assert [i.state for i in o.items] == ["正しい"] * 4
    # Each part is its weight's share of 100 (the items of 見つける: find 50, kind 30, activity 10 → made up to 100).
    assert o.parts == {"find": (55.6, 55.6), "kind": (33.3, 33.3), "activity": (11.1, 11.1)}
    assert o.kind_score() == pytest.approx(1.0)


def test_misses_wrong_classes_and_records_of_nothing():
    e = find_exercise()
    a = e.answers
    marks = [mark(a[0], 0, kind="船舶 > 貨物船"),           # one level up: half the kind
             mark(a[1], 1, kind="船舶 > 貨物船", activity="停泊"),  # the top class only, the activity wrong
             Mark("x", LON + 0.005, LAT, kind="船舶"),         # a record of nothing in the area: 誤り
             Mark("y", LON + 0.005, LAT, kind="建物"),         # another class than the targets: not counted
             Mark("z", LON + 0.5, LAT, kind="船舶")]           # outside the area: not counted
    o = score(e, marks)
    states = [i.state for i in o.items]
    assert states == ["種別ちがい", "種別ちがい", "見落とし", "見落とし（加点）"]
    assert [m.id for m in o.extras] == ["x"] and o.penalty == 5
    third = 100 / 3
    first = third * (50 + 30 * 0.5 + 10) / 90
    second = third * (50 + 30 * 0.2) / 90
    assert o.score == pytest.approx(round(first + second - 5, 1))
    assert o.missed == 1 and not o.passed


def test_the_nearest_record_is_taken_once_within_the_tolerance():
    e = find_exercise()
    a = e.answers
    # 50 m, or half the thing's length when longer (the container ship: 90 m).
    far = Mark("far", a[0].lon + 0.0009, a[0].lat, kind=a[0].kind, activity="停泊")  # about 82 m east
    o = score(e, [far])
    assert o.items[0].found and o.items[0].distance == pytest.approx(82, abs=2)
    o = score(e, [Mark("far", a[1].lon + 0.0009, a[1].lat, kind=a[1].kind)])
    assert not o.items[1].found and len(o.extras) == 1
    # Two records on one thing: the nearer is taken, the other is 誤り.
    o = score(e, [mark(a[2], 1, dx=0.0003), mark(a[2], 2, dx=0.0001)])
    assert o.items[2].mark.id == "m2" and [m.id for m in o.extras] == ["m1"]


def test_kind_credit_and_the_measures():
    assert kind_credit("船舶 > 貨物船", "船舶 > 貨物船 > コンテナ船") == 1  # said more closely: right
    assert kind_credit("船舶 > 貨物船 > コンテナ船", "船舶 > 貨物船") == 0.5
    assert kind_credit("船舶 > 貨物船 > コンテナ船", "船舶 > 支援船") == 0.2
    assert kind_credit("船舶 > 貨物船", "航空機") == 0
    assert kind_credit("船舶", "船舶 > 支援船") == 1
    s = scoring_of({"kindCredit": {"parent": 0.6, "top": 0}}, [])
    assert kind_credit("船舶 > 貨物船 > コンテナ船", "船舶 > 貨物船", s) == 0.6 and kind_credit("船舶 > 貨物船 > コンテナ船", "船舶 > 支援船", s) == 0
    a = Answer(LON, LAT, length=100, width=20, heading=90)
    assert measure_credit(a, Mark("m", LON, LAT, length=110, width=22, heading=100)) == 1
    # 30 % off: 15 past the tolerance of 15 → half; not measured: none.
    assert measure_credit(Answer(LON, LAT, length=100), Mark("m", LON, LAT, length=130)) == pytest.approx(0.5)
    assert measure_credit(Answer(LON, LAT, length=100, heading=0), Mark("m", LON, LAT, length=100)) == pytest.approx(0.5)
    assert heading_error(350, 10) == 20 and heading_error(10, 190) == 180 and heading_error(10, 190, either_end=True) == 0
    assert metres((LON, LAT), (LON + 0.001, LAT)) == pytest.approx(91, abs=1)
    assert inside(LON, LAT, [[[LON - 1, LAT - 1], [LON - 1, LAT + 1], [LON + 1, LAT + 1], [LON + 1, LAT - 1]]])


def test_measure_identify_and_change_score_their_own_items():
    m = Exercise("m", "測る", MEASURE, "IMG", answers=[ship(kind="船舶", length=120, width=20, heading=45)]).numbered()
    o = score(m, [mark(m.answers[0], length=125, width=19, heading=50)])
    assert o.score == 100 and list(o.parts) == ["measure"]
    o = score(m, [mark(m.answers[0], length=180, width=None, heading=45)])
    assert o.items[0].state == "測るのずれ" and o.score == pytest.approx(round(100 * (0 + 0 + 1) / 3, 1))
    i = Exercise("i", "見分ける", IDENTIFY, "IMG", answers=[ship(kind="船舶 > 貨物船", activity="停泊")]).numbered()
    assert score(i, [mark(i.answers[0], activity="航行")]).score == pytest.approx(75.0)
    # Nothing recorded at a marked thing: nothing for it, and no 誤り in 見分ける.
    assert score(i, [Mark("x", LON + 0.01, LAT, kind="船舶")]).score == 0
    c = Exercise("c", "変化", CHANGE, "AFTER", before_image_id="BEFORE", answers=[
        ship(0, 0, kind="船舶", change=STAYED), ship(0.01, 0, kind="船舶", change=CAME), ship(0.02, 0, kind="船舶", change=GONE)]).numbered()
    o = score(c, [mark(c.answers[0], 0), mark(c.answers[1], 1), mark(c.answers[2], 2, change=STAYED)])
    assert [it.state for it in o.items] == ["正しい", "正しい", "変化ちがい"] and list(o.parts) == ["find", "change"]
    assert o.score == pytest.approx(round(100 * (1 + 1 + 0.5) / 3, 1))


def test_scoring_settings_and_their_problems():
    problems = []
    s = scoring_of({"tolerance": 30, "weights": {"find": 70, "activity": 0}, "passScore": 80, "extraPenalty": 2,
                    "headingEitherEnd": True}, problems)
    assert problems == [] and s.tolerance == 30 and s.weight("find") == 70 and s.weight("kind") == 30 and s.pass_score == 80
    assert s.heading_either_end and s.extra_penalty == 2
    # An exercise's own over the organisation's.
    t = scoring_of({"passScore": 60}, problems, base=s)
    assert t.pass_score == 60 and t.tolerance == 30
    scoring_of({"weights": {"speed": 1}, "passScore": 120, "x": 1, "kindCredit": {"exact": 2}}, problems)
    assert len(problems) == 4
    # Activity weighted 0: not scored at all.
    e = find_exercise(scoring=s)
    assert "activity" not in score(e, []).parts or score(e, []).parts["activity"] == (0.0, 0.0)


def test_fitted_to_the_classes_in_use_and_the_hint():
    e = find_exercise()
    f = fitted(e, ["船舶", "船舶 > 貨物船", "船舶 > 支援船"], {"船舶 > 貨物船": ["停泊"], "船舶 > 支援船": ["移動"]})
    assert [a.kind for a in f.answers] == ["船舶 > 貨物船", "船舶 > 支援船", "船舶 > 貨物船", "船舶 > 支援船"]
    assert [a.activity for a in f.answers] == ["停泊", "", "停泊", ""]
    assert fitted(e, ["航空機"]).answers[0].kind == ""
    assert hint(e, [mark(e.answers[0])]) == "あと 2 隻"
    assert "すべて見つかっている" in hint(e, [mark(a, i) for i, a in enumerate(e.answers[:3])])


def test_results_are_kept_and_the_classes_taken_for_others(tmp_path):
    path = str(tmp_path / "exercise_results.json")
    e = find_exercise()
    a = e.answers
    o = score(e, [mark(a[0], 0, kind="船舶 > 貨物船"), mark(a[1], 1, kind="船舶 > 貨物船"), mark(a[2], 2)])
    record = result_record(o, datetime(2026, 11, 22, 20, 0), datetime(2026, 11, 22, 20, 4, 12), 1, "tanaka", hints=1)
    assert record["seconds"] == 252 and record["found"] == 3 and record["missed"] == 0 and record["user"] == "tanaka"
    assert record["detail"][0] == {"no": 1, "state": "種別ちがい", "required": True, "kind": "船舶 > 貨物船 > コンテナ船",
                                   "given": "船舶 > 貨物船", "earned": pytest.approx(record["detail"][0]["earned"]),
                                   "possible": 33.3, "activity": "停泊", "givenActivity": "停泊", "length": 180, "givenLength": 180,
                                   "distance": 18}
    add_result(path, record)
    add_result(path, {**record, "score": 90.0, "mode": "試験"})
    results = read_results(path)
    assert len(results) == 2 and best_of(results, e.key) == 90.0 and exam_taken(results, e.key)
    assert mistaken(results)[0] == ("船舶 > 支援船 > 〇〇級", "船舶 > 貨物船", 2)
    # (said more closely than the answer is not a mistake)
    assert all(t != "船舶 > 貨物船" for t, _g, _n in mistaken(results))
    (tmp_path / "bad.json").write_text("{", encoding="utf-8")
    assert read_results(str(tmp_path / "bad.json")) == []


def test_the_samples_exercises_are_where_the_ships_are():
    plans = image_plans(now=1_795_000_000_000)
    found = sample_exercises(plans)
    assert [e.type for e in found] == [FIND, IDENTIFY, MEASURE, CHANGE, FIND, FIND]
    assert [e.order for e in found] == [1, 2, 3, 4, 5, 6] and found[-1].exam and found[-1].time_limit == 10
    by_id = {p["id"]: p for p in plans}
    for e in found:
        ships = by_id[e.image_id]["ships"]
        assert e.area and all(inside(a.lon, a.lat, e.area) for a in e.answers if a.change != GONE), e.id
        assert [a.number for a in e.answers] == list(range(1, len(e.answers) + 1))
        if e.type != CHANGE:
            assert len(e.answers) == len(ships)
    change = next(e for e in found if e.type == CHANGE)
    assert sorted(a.change for a in change.answers) == sorted([STAYED, STAYED, CAME, GONE])
    measure = next(e for e in found if e.type == MEASURE)
    assert all(a.length and a.width and a.heading is not None for a in measure.answers)
    # Recorded right where the ships are: full marks.
    for e in found:
        marks = [Mark(str(i), a.lon, a.lat, a.kind, a.activity, a.length, a.width, a.heading, a.change) for i, a in enumerate(e.answers)]
        assert score(e, marks).score == 100, e.id
    assert sample_exercises(plans[:3]) == []
