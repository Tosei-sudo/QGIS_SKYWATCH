"""
判読の練習の問題づくり (core/training/authoring.py): every exercise of the
organisation's tables read as it is (作りかけ too), a draft written back —
its row added or changed, its answers added, changed and deleted by their
object ids — and read by the practice as the instructor made it; what keeps a
draft from being written; answers taken from observation records; and the
sample's tries kept apart from the organisation's in the results table.
"""
from datetime import datetime
from types import SimpleNamespace

import pytest

from mikage.core.esri import EsriClient, EsriError
from mikage.core.training import authoring, tables
from mikage.core.training.api import (
    CAME, CHANGE, DEFAULT_SCORING, Draft, DraftAnswer, EXAM, FIND, GONE, IDENTIFY, MEASURE, PRACTICE, STAYED,
)
from mikage.tests.fake_esri import FakeEsri, FakeLayer

ORG = "https://org.example.jp/arcgis/rest/services/Training/FeatureServer"
TABLES = tables.ExerciseTables(f"{ORG}/0", f"{ORG}/1", f"{ORG}/2")
AREA = [[[139.70, 35.30], [139.70, 35.32], [139.72, 35.32], [139.72, 35.30], [139.70, 35.30]]]
NUMBERS = {"COURSE_ORDER", "TIME_LIMIT_MIN", "PASS_SCORE", "ANSWER_NO", "LENGTH_M", "WIDTH_M", "HEADING", "ANSWER_POINTS", "TOLERANCE_M",
           "SHOW_ANSWERS", "ACTIVE", "REQUIRED"}


def _types(fields, lengths=None) -> dict:
    return {n: "esriFieldTypeDouble" if n in NUMBERS else "esriFieldTypeString" for n in fields}


def service():
    fake = FakeEsri()
    layers = {
        "exercises": fake.add_layer(f"{ORG}/0", FakeLayer(_types(tables.EXERCISE_FIELDS), geometry="esriGeometryPolygon")),
        "answers": fake.add_layer(f"{ORG}/1", FakeLayer(_types(tables.ANSWER_FIELDS))),
    }
    return EsriClient(fake), fake, layers


def a_draft(**values) -> Draft:
    d = Draft("EX-20261122-100000", title="港の船を見つける", type=FIND, instruction="範囲の中の船をすべて記録", image_id="IMG-1", area=AREA,
              target_kinds=["船舶"], difficulty="中", time_limit=15, pass_score=80, scoring={"tolerance": 30}, course="入門", order=2,
              roles=["OBSERVER"], active=False,
              answers=[DraftAnswer(139.71, 35.31, kind="船舶 > 貨物船", activity="停泊", length=120, explanation="上部構造が船尾"),
                       DraftAnswer(139.712, 35.315, kind="船舶 > 支援船", points=20, required=False)])
    for k, v in values.items():
        setattr(d, k, v)
    return d


def test_a_draft_is_written_read_back_and_read_by_the_practice_as_made():
    client, _fake, layers = service()
    written = authoring.save_draft(client, TABLES, a_draft(), None)
    # One row of the exercise (its area the polygon), one per answer (numbered as listed); the object ids kept.
    assert len(layers["exercises"].rows) == 1 and len(layers["answers"].rows) == 2
    row = layers["exercises"].rows[0]
    a = row["attributes"]
    assert (a["EXERCISE_ID"], a["TITLE"], a["EXERCISE_TYPE"], a["IMAGE_ID"], a["ACTIVE"], a["SHOW_ANSWERS"]) == (
        "EX-20261122-100000", "港の船を見つける", FIND, "IMG-1", 0, 1)
    assert (a["TARGET_KINDS"], a["TIME_LIMIT_MIN"], a["PASS_SCORE"], a["SCORING"], a["ROLES"]) == ("船舶", 15, 80, '{"tolerance": 30}', "OBSERVER")
    assert row["geometry"]["spatialReference"] == {"wkid": 4326} and len(row["geometry"]["rings"][0]) == 5
    assert written.object_id == a["OBJECTID"] and len(written.answer_ids_read) == 2
    assert [r["attributes"]["ANSWER_NO"] for r in layers["answers"].rows] == [1, 2]
    assert layers["answers"].rows[1]["attributes"]["REQUIRED"] == 0 and layers["answers"].rows[0]["geometry"]["x"] == 139.71
    # Read back as it is (作りかけ too), the same as written.
    (back,) = authoring.read_drafts(client, TABLES, None)
    assert (back.title, back.active, back.scoring, back.pass_score, back.area[0][0], back.object_id) == (
        "港の船を見つける", False, {"tolerance": 30}, 80, (139.70, 35.30), written.object_id)
    assert [(x.kind, x.activity, x.length, x.points, x.required, x.object_id) for x in back.answers] == [
        ("船舶 > 貨物船", "停泊", 120, 10, True, written.answer_ids_read[0]), ("船舶 > 支援船", "", None, 20, False, written.answer_ids_read[1])]
    # 作りかけ: not offered; once offered, the practice reads it as the editor's own reading did.
    assert tables.org_exercises(layers["exercises"].rows, layers["answers"].rows) == []
    tried = authoring.exercise_of_draft(back, DEFAULT_SCORING)
    layers["exercises"].rows[0]["attributes"]["ACTIVE"] = 1
    (offered,) = tables.org_exercises(layers["exercises"].rows, layers["answers"].rows)
    assert (offered.key, offered.pass_score, offered.scoring.tolerance, offered.time_limit) == ("組織:EX-20261122-100000", 80, 30, 15)
    assert [(x.number, x.kind, x.points, x.required) for x in offered.answers] == [(x.number, x.kind, x.points, x.required) for x in tried.answers]


def test_a_draft_written_again_changes_keeps_and_deletes_its_answers_rows():
    client, fake, layers = service()
    first = authoring.save_draft(client, TABLES, a_draft(), None)
    ids = first.answer_ids_read
    # The first answer changed, the second taken off, a new one added; the exercise offered.
    first.answers[0].activity = "航行"
    first.answers = [first.answers[0], DraftAnswer(139.705, 35.305, kind="船舶", explanation="新しい")]
    first.active, first.title = True, "港の船（直した）"
    second = authoring.save_draft(client, TABLES, first, None)
    assert len(layers["exercises"].rows) == 1 and layers["exercises"].rows[0]["attributes"]["TITLE"] == "港の船（直した）"
    assert layers["exercises"].rows[0]["attributes"]["ACTIVE"] == 1
    rows = {r["attributes"]["OBJECTID"]: r["attributes"] for r in layers["answers"].rows}
    assert ids[0] in rows and ids[1] not in rows and len(rows) == 2
    assert rows[ids[0]]["ACTIVITY"] == "航行" and rows[ids[0]]["ANSWER_NO"] == 1
    new = next(v for k, v in rows.items() if k != ids[0])
    assert new["EXPLANATION"] == "新しい" and new["ANSWER_NO"] == 2
    assert second.answer_ids_read == (ids[0], new["OBJECTID"])
    # 「出す」 alone, and the exercise deleted with its answers.
    hidden = authoring.set_active(client, TABLES, second, False, None)
    assert not hidden.active and layers["exercises"].rows[0]["attributes"]["ACTIVE"] == 0
    authoring.delete_draft(client, TABLES, hidden, None)
    assert layers["exercises"].rows == [] and layers["answers"].rows == []


def test_only_the_columns_the_tables_have_are_written_and_texts_are_cut():
    fake = FakeEsri()
    short = {k: v for k, v in _types(tables.EXERCISE_FIELDS).items() if k not in ("SCORING", "ROLES")}
    exercises = fake.add_layer(f"{ORG}/0", FakeLayer({k.lower(): v for k, v in short.items()}, geometry="esriGeometryPolygon"))
    exercises.extra = {"fields": [{"name": n, "type": t, **({"length": 8} if n == "title" else {})} for n, t in exercises.types.items()]}
    fake.add_layer(f"{ORG}/1", FakeLayer(_types(tables.ANSWER_FIELDS)))
    authoring.save_draft(EsriClient(fake), TABLES, a_draft(), None)
    a = exercises.rows[0]["attributes"]
    assert "scoring" not in a and "SCORING" not in a and a["exercise_id"] == "EX-20261122-100000" and a["title"] == "港の船を見つける"[:8]


def test_a_write_that_fails_says_why():
    client, fake, layers = service()
    fake.layers.pop(f"{ORG}/1")
    with pytest.raises(EsriError):
        authoring.save_draft(client, TABLES, a_draft(), None)


def test_what_keeps_a_draft_from_being_written_and_what_is_worth_a_look():
    problems, notes = authoring.check(a_draft())
    assert problems == [] and notes == ["「出す」に印がないため、作りかけとして書きます（練習の一覧には出ません）"]
    problems, _notes = authoring.check(a_draft(title=" ", image_id="", answers=[]))
    assert problems[:3] == ["題名がありません", "画像がありません（画像 ID か画像のファイル）", "正解がありません（観測記録から取り込むか、地図で足してください）"]
    # 測る: answers measured; 変化を見つける: the image before and what changed; 見分ける: a kind or an activity.
    problems, _ = authoring.check(a_draft(type=MEASURE))
    assert problems == ["正解 2: 測る問題の正解に長さ・幅・向きがありません"]
    problems, _ = authoring.check(a_draft(type=CHANGE))
    assert problems[0] == "変化を見つける問題に前の画像がありません" and "正解 1: 変化（来た・継続・消失）がありません" in problems
    problems, _ = authoring.check(a_draft(type=IDENTIFY, answers=[DraftAnswer(139.71, 35.31)]))
    assert problems == ["正解 1: 見分ける問題の正解に種別も活動もありません"]
    # An answer outside the area, of a class the exercise does not ask for, no area: written all the same.
    d = a_draft(active=True)
    d.answers.append(DraftAnswer(139.80, 35.31, kind="航空機"))
    problems, notes = authoring.check(d)
    assert problems == [] and notes == ["範囲の外の正解があります（3 番）", "対象の種別に入らない正解があります（3 番）"]
    assert authoring.check(a_draft(area=[], active=True))[1] == ["範囲がありません（画像の全体を探す問題になります）"]
    assert authoring.check(a_draft(pass_score=120))[0] == ["合格点は 0〜100 です"]


def test_new_ids_and_copies():
    now = datetime(2026, 11, 22, 15, 30, 12)
    assert authoring.new_exercise_id(now) == "EX-20261122-153012"
    assert authoring.new_exercise_id(now, {"EX-20261122-153012", "EX-20261122-153012-2"}) == "EX-20261122-153012-3"
    d = a_draft(object_id=7, active=True)
    d.answers[0].object_id = 3
    c = authoring.copied(d, "EX-2")
    assert (c.id, c.title, c.object_id, c.active, c.order, c.answer_ids_read) == ("EX-2", "港の船を見つける（コピー）", None, False, 3, ())
    assert [a.object_id for a in c.answers] == [None, None] and d.answers[0].object_id == 3


def record(image, lon, lat, kind="船舶 > 貨物船", **values):
    v = dict(image_id=image, lon=lon, lat=lat, kind=kind, activity="停泊", length=None, width=None, heading=None, present=True, unseen="",
             deleted_at=None, object_id=f"obj-{lon}", note="", confidence="確実", recorded_by="ベテラン")
    v.update(values)
    return SimpleNamespace(**v)


def test_answers_are_taken_from_the_records_of_the_image_in_the_area():
    records = [record("IMG-1", 139.71, 35.31, length=118.4, width=19.6, heading=42.0, note="白い上部構造"),
               record("IMG-1", 139.715, 35.318, kind="船舶 > 支援船"),
               record("IMG-1", 139.80, 35.31),  # outside the area
               record("IMG-2", 139.71, 35.31),  # another image
               record("IMG-1", 139.711, 35.311, unseen="雲"),  # 不可
               record("IMG-1", 139.712, 35.312, deleted_at=1)]
    found = authoring.answers_from_records(records, FIND, "IMG-1", area=AREA)
    assert [(a.kind, a.activity, a.length, a.heading, a.explanation) for _o, a in found] == [
        ("船舶 > 支援船", "停泊", None, None, ""), ("船舶 > 貨物船", "停泊", 118.4, 42.0, "白い上部構造")]
    # Without an area: all of the image. 消失 is not an answer to find.
    found = authoring.answers_from_records([*records, record("IMG-1", 139.70, 35.30, present=False)], FIND, "IMG-1")
    assert len(found) == 3
    # 変化を見つける: what was on the image before carried on (継続), gone (消失), or came.
    before = [record("OLD", 139.71, 35.31, object_id="a"), record("OLD", 139.715, 35.315, object_id="b")]
    after = [record("NEW", 139.7101, 35.3101, object_id="a"), record("NEW", 139.715, 35.315, object_id="b", present=False, activity=""),
             record("NEW", 139.705, 35.305, object_id="c")]
    found = authoring.answers_from_records([*before, *after], CHANGE, "NEW", "OLD")
    assert sorted((o.object_id, a.change) for o, a in found) == [("a", STAYED), ("b", GONE), ("c", CAME)]


def test_the_samples_tries_are_kept_apart_from_the_organisations_in_the_results_table():
    base = {"user": "u1", "mode": PRACTICE, "score": 90, "passed": True, "finished": "2026-11-22T10:00:00",
            "detail": [{"no": 1, "state": "正しい", "kind": "船舶 > 貨物船", "given": "船舶 > 貨物船"}]}
    sample = tables.result_attributes({**base, "exercise": "見本:sample-find", "exerciseId": "sample-find", "source": "見本"})
    org = tables.result_attributes({**base, "exercise": "組織:EX-1", "exerciseId": "EX-1", "source": "組織", "mode": EXAM})
    assert (sample["EXERCISE_ID"], org["EXERCISE_ID"]) == ("見本:sample-find", "EX-1")
    back = [tables.record_of({"attributes": x}) for x in (sample, org)]
    assert [(r["exercise"], r["exerciseId"], r["source"]) for r in back] == [("見本:sample-find", "sample-find", "見本"), ("組織:EX-1", "EX-1", "組織")]
    # Read with its DETAIL (the instructors' 取り違え): the answers as they came out.
    assert back[0]["detail"] == base["detail"] and back[1]["mode"] == EXAM
