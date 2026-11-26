"""
判読の練習's Esri tables (core/training/tables.py): the organisation's
exercises and answers as their rows say, the rows that can not be used said
and left out, `training.exercises` in the settings file, and a try as the
results table's row and back (sent through EsriClient to a fake table).
"""
import json
from datetime import datetime

from mikage.core.esri import EsriClient
from mikage.core.training import tables
from mikage.core.training.api import (
    CHANGE, DEFAULT_SCORING, EXAM, FIND, GONE, IDENTIFY, MEASURE, merged_results, PRACTICE, STAYED, unsent,
)
from mikage.core.training.results import add_result, mark_sent, read_results
from mikage.core.training.settings import training_settings_of
from mikage.tests.fake_esri import FakeEsri, FakeLayer

ORG = "https://org.example.jp/arcgis/rest/services/Training/FeatureServer"
AREA = [[[139.70, 35.30], [139.72, 35.30], [139.72, 35.32], [139.70, 35.32], [139.70, 35.30]]]


def exercise_row(**values) -> dict:
    a = {"EXERCISE_ID": "EX-1", "TITLE": "港の船", "EXERCISE_TYPE": "見つける", "IMAGE_ID": "IMG-1", "EXERCISE_MODE": "練習",
         "COURSE_NAME": "入門", "COURSE_ORDER": 1, "ACTIVE": 1}
    a.update(values)
    return {"attributes": {k: v for k, v in a.items() if v is not ...}, "geometry": {"rings": AREA}}


def answer_row(eid="EX-1", x=139.71, y=35.31, **values) -> dict:
    return {"attributes": {"EXERCISE_ID": eid, "KIND": "船舶 > 貨物船", **values}, "geometry": {"x": x, "y": y}}


def test_an_exercise_and_its_answers_are_read_as_the_rows_say():
    problems = []
    rows = [exercise_row(INSTRUCTION="範囲の船をすべて", TARGET_KINDS="船舶; 航空機", DIFFICULTY="中", TIME_LIMIT_MIN=15,
                         SHOW_ANSWERS=0, PASS_SCORE=80, ROLES="OBSERVER, ANALYST", SCORING='{"tolerance": 25}')]
    answers = [answer_row(ANSWER_NO=2, ACTIVITY="停泊", ANSWER_POINTS=20, TOLERANCE_M=40, REQUIRED=0, EXPLANATION="船首が細い"),
               answer_row(x=139.712, y=35.312, LENGTH_M=180, WIDTH_M=28, HEADING=34.5, REFERENCE_IMAGE="\\\\share\\ref\\cargo.jpg")]
    found = tables.org_exercises(rows, answers, DEFAULT_SCORING, problems)
    assert problems == [] and len(found) == 1
    e = found[0]
    assert (e.id, e.key, e.source, e.title, e.type, e.image_id, e.image_source) == ("EX-1", "組織:EX-1", "組織", "港の船", FIND, "IMG-1", "")
    assert e.area == [[tuple(p) for p in AREA[0]]] and e.instruction == "範囲の船をすべて"
    assert e.target_kinds == ["船舶", "航空機"] and e.difficulty == "中" and e.time_limit == 15 and e.mode == PRACTICE
    assert not e.show_answers and e.pass_score == 80 and e.scoring.tolerance == 25 and e.roles == ["OBSERVER", "ANALYST"]
    assert (e.course, e.order) == ("入門", 1)
    # The answers by their numbers (one without: the next free number).
    first, second = e.answers
    assert (first.number, first.length, first.width, first.heading, first.points, first.required) == (1, 180, 28, 34.5, 10, True)
    assert first.reference == "\\\\share\\ref\\cargo.jpg"
    assert (second.number, second.activity, second.points, second.tolerance, second.required, second.explanation) == (
        2, "停泊", 20, 40, False, "船首が細い")
    assert (second.lon, second.lat, second.kind) == (139.71, 35.31, "船舶 > 貨物船")


def test_the_columns_are_read_whatever_their_case_and_the_words_in_english_too():
    row = {"attributes": {"exercise_id": "EX-2", "Exercise_Type": "measure", "image_source": "C:\\images\\a.tif",
                          "exercise_mode": "exam", "active": "true", "show_answers": "はい", "course_order": "3"},
           "geometry": None}
    e = tables.exercise_of(row, DEFAULT_SCORING, [])
    assert (e.type, e.mode, e.image_id, e.image_source, e.area, e.order, e.show_answers) == (MEASURE, EXAM, "", "C:\\images\\a.tif", [], 3, True)
    assert e.exam and e.title == "EX-2"
    # Numbers kept as numbers by the service (EXERCISE_ID 12.0) are their text without ".0".
    assert tables.exercise_of(exercise_row(EXERCISE_ID=12.0), DEFAULT_SCORING, []).id == "12"
    assert tables.exercise_of(exercise_row(EXERCISE_TYPE="identify"), DEFAULT_SCORING, []).type == IDENTIFY
    # 変化を見つける with the image before by its file; the answers' changes in Japanese or English.
    e = tables.exercise_of(exercise_row(EXERCISE_TYPE="変化を見つける", BEFORE_IMAGE_SOURCE="/data/before.tif"), DEFAULT_SCORING, [])
    assert e.type == CHANGE and e.before_image_source == "/data/before.tif" and e.before_image_id == ""
    assert tables.answer_of(answer_row(CHANGE_KIND="消失"), [])[1].change == GONE
    assert tables.answer_of(answer_row(CHANGE_KIND="stayed"), [])[1].change == STAYED


def test_rows_that_can_not_be_used_are_said_and_left_out():
    problems = []
    rows = [
        exercise_row(EXERCISE_ID="OK"),
        exercise_row(EXERCISE_ID="DRAFT", ACTIVE=0),
        exercise_row(EXERCISE_ID="NO-IMAGE", IMAGE_ID=...),
        exercise_row(EXERCISE_ID="BAD-TYPE", EXERCISE_TYPE="数える"),
        exercise_row(EXERCISE_ID="NO-BEFORE", EXERCISE_TYPE="変化を見つける"),
        exercise_row(EXERCISE_ID="NO-ANSWERS"),
        exercise_row(EXERCISE_ID="OK", TITLE="同じ ID"),
        exercise_row(EXERCISE_ID=""),
        exercise_row(EXERCISE_ID="ODD", EXERCISE_MODE="本番", PASS_SCORE=120, TIME_LIMIT_MIN="十分", SCORING="{"),
    ]
    answers = [answer_row("OK"), answer_row("DRAFT"), answer_row("NO-IMAGE"), answer_row("BAD-TYPE"), answer_row("NO-BEFORE"),
               answer_row("ODD", ANSWER_POINTS="多い"), {"attributes": {"EXERCISE_ID": "OK"}, "geometry": None}]
    found = tables.org_exercises(rows, answers, DEFAULT_SCORING, problems)
    assert [e.id for e in found] == ["ODD", "OK"] or [e.id for e in found] == ["OK", "ODD"]
    odd = next(e for e in found if e.id == "ODD")
    # (what could be read of it: 練習, the base's pass score, no limit; the answer's points the default)
    assert odd.mode == PRACTICE and odd.pass_score == DEFAULT_SCORING.pass_score and odd.time_limit is None
    assert odd.answers[0].points == 10
    said = "\n".join(problems)
    for words in ("問題 NO-IMAGE: 画像なし", "問題 BAD-TYPE: EXERCISE_TYPE", "問題 NO-BEFORE: 変化を見つける問題に前の画像",
                  "問題 NO-ANSWERS: 答えの表に答えなし", "問題 OK: EXERCISE_ID が重複", "EXERCISE_ID が空の行",
                  "問題 ODD: EXERCISE_MODE は 練習 か 試験", "問題 ODD: PASS_SCORE は 0〜100", "問題 ODD: TIME_LIMIT_MIN が数ではない",
                  "問題 ODD: SCORING が JSON ではない", "問題 ODD の答え: ANSWER_POINTS が数ではない", "答えの表: EXERCISE_ID か位置のない行"):
        assert words in said, (words, said)
    # A draft (ACTIVE 0) is not said: it is not to be shown yet.
    assert "DRAFT" not in said


def test_the_exercises_come_by_course_as_entered_and_in_their_order():
    rows = [exercise_row(EXERCISE_ID="B2", COURSE_NAME="応用", COURSE_ORDER=2, TITLE="b2"),
            exercise_row(EXERCISE_ID="A2", COURSE_NAME="入門", COURSE_ORDER=2, TITLE="a2"),
            exercise_row(EXERCISE_ID="B1", COURSE_NAME="応用", COURSE_ORDER=1, TITLE="b1"),
            exercise_row(EXERCISE_ID="A1", COURSE_NAME="入門", COURSE_ORDER=1, TITLE="a1")]
    answers = [answer_row(e) for e in ("A1", "A2", "B1", "B2")]
    # (the courses as the rows have them first: 応用 was entered first)
    assert [e.id for e in tables.org_exercises(rows, answers)] == ["B1", "B2", "A1", "A2"]


def test_training_exercises_in_the_settings_file():
    problems = []
    s = training_settings_of({"exercises": {"exercises": f"{ORG}/0/", "answers": f"{ORG}/1", "results": f"{ORG}/2"}}, problems)
    assert problems == [] and s.exercises == tables.ExerciseTables(f"{ORG}/0", f"{ORG}/1", f"{ORG}/2")
    # Without the results table: the tries are kept on the computer only.
    assert training_settings_of({"exercises": {"exercises": f"{ORG}/0", "answers": f"{ORG}/1"}}, []).exercises.results == ""
    assert training_settings_of({}, []).exercises is None
    for value, words in (({"exercises": f"{ORG}/0"}, "答えの表（answers）"), ("x", "の形で指定"),
                         ({"exercises": f"{ORG}/0", "answers": f"{ORG}/1", "extra": 1}, "不明なキー「extra」")):
        problems = []
        training_settings_of({"exercises": value}, problems)
        assert any(words in p for p in problems), (value, problems)


def record(**values) -> dict:
    r = {"exercise": "組織:EX-1", "exerciseId": "EX-1", "source": "組織", "title": "港の船", "course": "入門", "type": FIND, "mode": PRACTICE,
         "attempt": 1, "user": "山田", "started": "2026-11-22T10:00:00", "finished": "2026-11-22T10:12:30", "seconds": 750, "score": 85.5,
         "passed": True, "found": 3, "missed": 1, "extra": 0, "kindScore": 66.7, "hints": 2,
         "parts": {"見つける": [40, 50]}, "penalty": 0, "detail": [{"no": 1, "state": "正解", "kind": "船舶 > 貨物船"}]}
    r.update(values)
    return r


def result_layer() -> FakeLayer:
    types = {"EXERCISE_ID": "esriFieldTypeString", "ATTEMPT": "esriFieldTypeInteger", "STARTED": "esriFieldTypeDate",
             "FINISHED": "esriFieldTypeDate", "SCORE": "esriFieldTypeDouble", "KIND_SCORE": "esriFieldTypeDouble"}
    return FakeLayer({n: types.get(n, "esriFieldTypeString") for n in tables.RESULT_FIELDS}, geometry="")


def test_a_try_is_one_row_of_the_results_table_and_read_back():
    a = tables.result_attributes(record())
    assert set(a) == set(tables.RESULT_FIELDS)
    assert (a["EXERCISE_ID"], a["USER_NAME"], a["EXERCISE_MODE"], a["ATTEMPT"], a["SCORE"], a["PASSED"], a["KIND_SCORE"], a["HINTS"]) == (
        "EX-1", "山田", PRACTICE, 1, 85.5, 1, 66.7, 2)
    assert a["FINISHED"] == int(datetime(2026, 11, 22, 10, 12, 30).timestamp() * 1000) and a["SECONDS"] == 750
    assert json.loads(a["DETAIL"]) == {"parts": {"見つける": [40, 50]}, "penalty": 0, "detail": [{"no": 1, "state": "正解", "kind": "船舶 > 貨物船"}]}
    # Too long a detail: the counts kept, the detail cut.
    long = tables.result_attributes(record(detail=[{"no": i, "state": "正解", "note": "x" * 100} for i in range(400)]))
    assert len(long["DETAIL"]) <= tables.DETAIL_LENGTH and json.loads(long["DETAIL"])["cut"] is True
    back = tables.record_of({"attributes": a})
    for key in ("exercise", "exerciseId", "title", "course", "mode", "attempt", "user", "started", "finished", "seconds", "score", "passed",
                "found", "missed", "extra", "kindScore", "hints"):
        assert back[key] == record()[key], key
    assert back["fromTable"] and tables.record_of({"attributes": {"SCORE": 1}}) is None


def test_tries_are_sent_and_a_persons_read_back_from_the_table(tmp_path):
    fake = FakeEsri()
    layer = fake.add_layer(f"{ORG}/2", result_layer())
    fake.add_layer(f"{ORG}/0", FakeLayer({n: "esriFieldTypeString" for n in tables.EXERCISE_FIELDS}, geometry="esriGeometryPolygon"))
    fake.add_layer(f"{ORG}/1", FakeLayer({n: "esriFieldTypeString" for n in tables.ANSWER_FIELDS}))
    fake.layers[f"{ORG}/0"].add(exercise_row()["attributes"], exercise_row()["geometry"])
    fake.layers[f"{ORG}/1"].add(answer_row()["attributes"], answer_row()["geometry"])
    client = EsriClient(fake)
    t = tables.ExerciseTables(f"{ORG}/0", f"{ORG}/1", f"{ORG}/2")
    assert [e.id for e in tables.read_exercises(client, t, None)] == ["EX-1"]
    ok, why = tables.send_results(client, t, [record(), record(user="O'Brien", mode=EXAM, finished="2026-11-22T11:00:00")], None)
    assert ok == [True, True] and why == [] and len(layer.rows) == 2
    mine = tables.read_results_of(client, t, "O'Brien", None)
    assert [(r["exercise"], r["mode"], r["user"]) for r in mine] == [("組織:EX-1", EXAM, "O'Brien")]
    # (DETAIL is not read back: the lists do not need it)
    asked = [form for url, form in fake.asked if url == f"{ORG}/2/query"]
    assert asked and "DETAIL" not in asked[-1]["outFields"]
    # A table without some of the columns (and its names in lower case) takes the rest.
    small = fake.add_layer(f"{ORG}/3", FakeLayer({"exercise_id": "esriFieldTypeString", "score": "esriFieldTypeDouble"}, geometry=""))
    assert tables.send_results(client, tables.ExerciseTables(f"{ORG}/0", f"{ORG}/1", f"{ORG}/3"), [record()], None) == ([True], [])
    assert {k: v for k, v in small.rows[0]["attributes"].items() if k != "OBJECTID"} == {"exercise_id": "EX-1", "score": 85.5}
    # Without the results table nothing is sent.
    assert tables.send_results(client, tables.ExerciseTables(f"{ORG}/0", f"{ORG}/1"), [record()], None) == ([False], [])
    assert tables.read_results_of(client, tables.ExerciseTables(f"{ORG}/0", f"{ORG}/1"), "山田", None) == []


def test_the_tries_not_sent_are_marked_once_sent_and_counted_once_with_the_tables(tmp_path):
    path = str(tmp_path / "exercise_results.json")
    add_result(path, record(sent=False))
    add_result(path, record(finished="2026-11-22T11:00:00", sent=False))
    add_result(path, record(exercise="見本:sample-find", finished="2026-11-22T12:00:00"))
    here = read_results(path)
    assert [r["finished"] for r in unsent(here)] == ["2026-11-22T10:12:30", "2026-11-22T11:00:00"]
    mark_sent(path, [here[0]])
    assert [r["finished"] for r in unsent(read_results(path))] == ["2026-11-22T11:00:00"]
    # The table's rows of this computer's tries are not counted twice; another computer's are added.
    table = [tables.record_of({"attributes": tables.result_attributes(r)}) for r in (here[0], record(finished="2026-11-21T09:00:00"))]
    merged = merged_results(read_results(path), table)
    assert sorted(r["finished"] for r in merged) == ["2026-11-21T09:00:00", "2026-11-22T10:12:30", "2026-11-22T11:00:00", "2026-11-22T12:00:00"]
