"""
判読の練習の成績 (core/training/grades.py): everyone's tries of a course as a
grid (a person's best or latest score on each exercise, how many tries, how
far through the course), the period, the classes taken for others over the
tries' details, the advice, and the grid as CSV.
"""
import csv
import io
from datetime import datetime

from mikage.core.esri import EsriClient
from mikage.core.training import grades as g
from mikage.core.training import tables
from mikage.core.training.api import EXAM, PRACTICE
from mikage.tests.fake_esri import FakeEsri, FakeLayer

NOW = datetime(2026, 11, 22, 12, 0, 0)
COLUMNS = [g.Column("組織:EX-1", "港の船", "入門", 1, PRACTICE, 70), g.Column("組織:EX-2", "船の種別", "入門", 2, PRACTICE, 70),
           g.Column("組織:EX-3", "確認試験", "入門", 3, EXAM, 80), g.Column("組織:EX-9", "中級の問題", "中級", 1)]


def try_of(user, eid, score, finished, passed=None, detail=(), course="入門", title=None):
    return {"exercise": f"組織:{eid}", "exerciseId": eid, "user": user, "score": score, "passed": score >= 70 if passed is None else passed,
            "finished": finished, "seconds": 300, "course": course, "title": title or eid, "detail": list(detail)}


def said(truth, given, state="正しい"):
    return {"no": 1, "state": state, "kind": truth, "given": given}


TRIES = [
    try_of("佐藤", "EX-1", 60, "2026-11-20T10:00:00", detail=[said("船舶 > 貨物船", "船舶 > タンカー", "種別ちがい")]),
    try_of("佐藤", "EX-1", 90, "2026-11-21T10:00:00", detail=[said("船舶 > 貨物船", "船舶 > 貨物船")]),
    try_of("佐藤", "EX-1", 75, "2026-11-22T09:00:00", detail=[said("船舶 > 貨物船", "船舶 > タンカー", "種別ちがい")]),
    try_of("佐藤", "EX-2", 80, "2026-11-21T11:00:00", detail=[said("船舶 > 支援船", "船舶 > 支援船 > 〇〇級")]),
    try_of("鈴木", "EX-1", 40, "2026-11-19T10:00:00", detail=[said("船舶 > 貨物船", "船舶 > タンカー", "種別ちがい"),
                                                           said("船舶 > 支援船", "", "見落とし")]),
    try_of("鈴木", "EX-3", 50, "2026-11-19T11:00:00", detail=[said("船舶 > 支援船", "", "見落とし"), said("船舶 > 支援船", "", "見落とし")]),
    try_of("田中", "EX-3", 85, "2026-08-01T10:00:00"),
    try_of("田中", "EX-OLD", 70, "2026-11-01T10:00:00", title="消した問題"),
    try_of("佐藤", "EX-9", 95, "2026-11-21T10:00:00", course="中級"),
]


def test_the_grid_of_a_course_with_each_persons_best_or_latest():
    table = g.grades(TRIES, COLUMNS, "入門", best=True)
    assert table.people == ["佐藤", "田中", "鈴木"]
    # The course's exercises in their order, then those only the results name (an exercise deleted since).
    assert [c.title for c in table.columns] == ["港の船", "船の種別", "確認試験", "消した問題"]
    best = table.cell("佐藤", "組織:EX-1")
    assert (best.score, best.passed, best.tries) == (90, True, 3)
    assert table.cell("佐藤", "組織:EX-3") is None and table.cell("鈴木", "組織:EX-1").passed is False
    assert table.progress("佐藤") == (2, 4) and table.progress("鈴木") == (0, 4)
    assert table.passed_by("組織:EX-1") == (1, 2)
    latest = g.grades(TRIES, COLUMNS, "入門", best=False)
    assert latest.cell("佐藤", "組織:EX-1").score == 75
    # Another course: its own.
    other = g.grades(TRIES, COLUMNS, "中級")
    assert other.people == ["佐藤"] and [c.title for c in other.columns] == ["中級の問題"]
    assert g.courses_of(COLUMNS, [*TRIES, try_of("x", "S1", 1, "", course="船の判読（入門・横須賀の見本）")]) == [
        "入門", "中級", "船の判読（入門・横須賀の見本）"]


def test_the_period_leaves_out_older_tries():
    recent = g.in_period(TRIES, 30, NOW)
    assert "田中" not in {r["user"] for r in recent if r["exerciseId"] == "EX-3"} and len(recent) == len(TRIES) - 1
    assert g.in_period(TRIES, None, NOW) == TRIES


def test_the_classes_taken_for_others_and_the_advice():
    kinds, counts, people = g.confusion(TRIES)
    # (the misses are not what was said; a class below the answer's is right)
    assert counts[("船舶 > 貨物船", "船舶 > タンカー")] == 3 and counts[("船舶 > 貨物船", "船舶 > 貨物船")] == 1
    assert ("船舶 > 支援船", "") not in counts and kinds[0] == "船舶 > 貨物船"
    assert people[("船舶 > 貨物船", "船舶 > タンカー")] == {"佐藤", "鈴木"}
    assert g.taken_for_others(counts) == [("船舶 > 貨物船", "船舶 > タンカー", 3)]
    table = g.grades(TRIES, COLUMNS, "入門")
    lines = g.advice(TRIES, table, counts, people)
    assert lines[0].startswith("「船舶 > 貨物船」を「船舶 > タンカー」と答えたのが 3 回（2 人、75 %）")
    # Few passed it (港の船: 1 of 2), and what is most often missed.
    assert any(line.startswith("「港の船」は合格が 1 / 2 人。") for line in lines)
    assert lines[-1] == "見落としが多いのは「船舶 > 支援船」（3 回）。探し方の問題（見つける）を追加"


def test_the_grid_as_csv():
    text = g.grades_csv(g.grades(TRIES, COLUMNS, "入門"))
    rows = list(csv.reader(io.StringIO(text)))
    assert rows[0][:4] == ["解いた人", "港の船（点）", "港の船（合否）", "港の船（回数）"] and rows[0][-2:] == ["合格した問題", "問題の数"]
    assert rows[1][:4] == ["佐藤", "90", "合格", "3"] and rows[1][-2:] == ["2", "4"]
    assert rows[3][7:10] == ["50", "不合格", "1"]


def test_every_try_is_read_with_its_detail():
    fake = FakeEsri()
    layer = fake.add_layer("https://org/arcgis/rest/services/T/FeatureServer/2",
                           FakeLayer({n: "esriFieldTypeString" for n in tables.RESULT_FIELDS}, geometry=""))
    for t in TRIES[:2]:
        layer.add(tables.result_attributes({**t, "source": "組織", "mode": PRACTICE}))
    found = g.read_all_results(EsriClient(fake), "https://org/arcgis/rest/services/T/FeatureServer/2", None)
    assert [(r["user"], r["score"], r["detail"][0]["given"]) for r in found] == [("佐藤", 60, "船舶 > タンカー"), ("佐藤", 90, "船舶 > 貨物船")]
