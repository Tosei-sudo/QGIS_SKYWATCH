"""
The systems' answers to what was handed out (採否) and their own plans
(要求外), kept in the history (2026-11-09, 「依頼した結果、その日の計画を見たい。
採用されたかどうかを知りたい」): result files, answers by hand, the day's rows.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone

from skywatch.core.esri import EsriClient
from skywatch.core.request.adoption import (
    NOT_HANDED, NOT_IN_FILE, answer_changes, handed_rows, manual_changes, manual_unrequested, match_answers, unrequested_rows,
)
from skywatch.core.request.model import JST
from skywatch.core.request.result_file import ADOPTED, REJECTED, WAITING, read_answers, result_file_of, time_value
from skywatch.core.request.store import (
    ADOPTION_ROLES, AWAITING, DEFAULT_HISTORY_FIELDS, DEFAULT_REQUEST_FIELDS, MANUAL, UNREQUESTED, add_history, day_counts,
    delete_history, delete_history_rows, esri_rings, history_has, load_day, load_history, load_requests, replaced_where, request_store_of,
    save_request, state_of, unrequested_where, update_history,
)
from skywatch.core.request.systems import export_systems_of
from skywatch.core.time_zone import parse_time_zone
from skywatch.core.request.workflow import history_of, keep_answers, plan_day
from skywatch.tests.fake_esri import FakeEsri, FakeLayer
from skywatch.tests.test_request import DAY, FIRST, H, LEDGER, HISTORY, SATS, SYSTEMS_JSON, request, systems

D = 86400000
OSAKA = [[[135.4, 34.6], [135.5, 34.6], [135.5, 34.7], [135.4, 34.7], [135.4, 34.6]]]


def server(geometry: str = "esriGeometryPolygon", fields=None):
    fake = FakeEsri()
    types = {name: "esriFieldTypeString" for name in DEFAULT_REQUEST_FIELDS.values()}
    for n in ("START_AT", "END_AT", "CREATED_AT", "UPDATED_AT"):
        types[n] = "esriFieldTypeDate"
    for n in ("PRIORITY", "BOOST", "OFF_NADIR_MIN", "OFF_NADIR_MAX", "INCIDENCE_MIN", "INCIDENCE_MAX", "RESOLUTION_MAX", "CLOUD_NO_GO"):
        types[n] = "esriFieldTypeDouble"
    fake.add_layer(LEDGER, FakeLayer(types, "esriGeometryPolygon"))
    htypes = {name: "esriFieldTypeString" for name in (fields or DEFAULT_HISTORY_FIELDS.values())}
    htypes.update({n: t for n, t in dict(RUN_DATE="esriFieldTypeDate", TARGET_DATE="esriFieldTypeDate", PLANNED_AT="esriFieldTypeDate",
                                         CLOUD="esriFieldTypeDouble", SCENES="esriFieldTypeInteger",
                                         BOOST_DONE="esriFieldTypeInteger").items() if n in htypes})
    fake.add_layer(HISTORY, FakeLayer(htypes, geometry))
    return fake


def store():
    return request_store_of({"url": LEDGER, "history": HISTORY}, [], 0)


def written_day(fake):
    """The ledger with three requests and the day written out: 東京湾 to SYS-A, 大阪湾 to SYS-B, 横浜 held back."""
    client, s = EsriClient(fake), store()
    for r in (request("R-0001", name="東京湾", management_no="K-1", requester="第三管区", priority=742, series=["OPT-X"],
                      satellites=["OPT-X 1"], systems=["SYS-A"]),
              request("R-0002", name="大阪湾", management_no="K-2", kind="SAR", priority=100, look_side="右", rings=OSAKA),
              request("R-0003", name="横浜", management_no="K-3", priority=900, series=["OPT-X"], systems=["SYS-A"])):
        save_request(client, s, None, r, FIRST - D)
    loaded = load_requests(client, s, None)
    plan = plan_day(loaded, DAY, systems(), SATS, {"R-0001": 30, "R-0003": 30})
    rows = history_of(plan, FIRST - 9 * H, {"SYS-A": "a.csv", "SYS-B": "b.csv", "SYS-C": "c.json"}, "RUN1")
    assert add_history(client, s, None, rows) == len(rows)
    return client, s, loaded


def test_handed_out_rows_wait_for_the_answer_with_their_area():
    fake = server()
    client, s, _ = written_day(fake)
    by_name = {r["attributes"]["REQ_NAME"]: r for r in fake.layers[HISTORY].rows}
    assert {n: (r["attributes"]["RESULT"], r["attributes"]["ADOPTION"]) for n, r in by_name.items()} == {
        "東京湾": ("採用", "不明"), "大阪湾": ("採用", "不明"), "横浜": ("見送り", None)}
    # The history is a polygon layer: each row has the request's area (for the dock and a dashboard's map).
    assert by_name["大阪湾"]["geometry"]["rings"][0][0] == [135.4, 34.6]
    assert by_name["東京湾"]["attributes"]["TARGET_DATE"] == FIRST
    day = load_day(client, s, None, DAY.isoformat())
    # By system (held back without one first), then time and name.
    assert [(r.name, r.system, state_of(r.result, r.adoption)) for r in day] == [
        ("横浜", "", "見送り"), ("東京湾", "SYS-A", AWAITING), ("大阪湾", "SYS-B", AWAITING)]
    assert next(r for r in day if r.name == "大阪湾").rings == esri_rings(OSAKA)
    assert history_has(client, s, None, ADOPTION_ROLES) == []

    # A table without shapes (as before): rows saved without them, read the same.
    bare = server("esriGeometryNone")
    client, s, _ = written_day(bare)
    assert all(r["geometry"] is None for r in bare.layers[HISTORY].rows)
    assert all(r.rings == [] for r in load_day(client, s, None, DAY.isoformat()))
    # Nor the 採否 fields: they are said to be missing (the dock keeps its buttons off).
    old = server(fields=[v for k, v in DEFAULT_HISTORY_FIELDS.items() if k not in ADOPTION_ROLES])
    client, s, _ = written_day(old)
    assert history_has(client, s, None, ADOPTION_ROLES) == ["ADOPTION", "ADOPTION_REASON", "PLANNED_AT", "EXTERNAL_ID", "ADOPTION_SOURCE"]


def test_result_file_settings():
    problems = []
    zone = parse_time_zone("Asia/Tokyo")
    spec = result_file_of({"key": "REQ", "result": "STATUS", "adopted": ["ok", "済"], "rejected": "ng", "plannedAt": "WHEN"}, "x", problems, zone)
    assert problems == []
    # The first form (one name per item) is read as columns.
    assert [(c.key, c.target) for c in spec.columns] == [("REQ", "id"), ("STATUS", "result"), ("WHEN", "plannedAt"), ("REQ", "externalId")]
    assert spec.columns[1].map == {"OK": ADOPTED, "済": ADOPTED, "NG": REJECTED} and spec.missing == WAITING
    # Without a result column: every row listed is 採用, the others 不採用.
    assert result_file_of({"key": "REQ"}, "x", problems).missing == REJECTED
    assert result_file_of({"result": "S"}, "x", problems) is None
    assert result_file_of({"key": "K", "format": "xml"}, "y", problems) is None
    assert result_file_of({"key": "K", "keySource": "name", "missing": "保留", "encoding": "euc", "geometry": {"x": 1}}, "z", problems)
    assert problems == ["x に key（依頼を表す列）なし", "y.format は csv・json のどちらかで指定",
                        "z.encoding は utf-8・utf-8-sig・cp932・euc-jp・utf-16 のどれかで指定",
                        "z.keySource は id・managementNo・identificationNo のどれかで指定",
                        "z.geometry は wkt・geojson・lon と lat・bbox のどれかで指定", "z.missing は 不採用・不明 のどちらかで指定"]
    # In the settings file: a system's `resultFile`, its times in the system's zone.
    problems = []
    value = [dict(SYSTEMS_JSON[0], resultFile={"key": "ID", "plannedAt": "T"}), SYSTEMS_JSON[1]]
    a, b = export_systems_of(value, problems)
    assert problems == [] and a.result_file.columns[0].key == "ID" and b.result_file is None
    assert time_value("2026-11-08 10:30", a.result_file) == datetime(2026, 11, 8, 10, 30, tzinfo=JST).timestamp() * 1000
    assert export_systems_of([dict(SYSTEMS_JSON[0], resultFile=[])], problems)[0].result_file is None
    assert problems == ["requestExports[0].resultFile はオブジェクトで指定"]


def test_times_in_a_result_file():
    problems = []
    tokyo = result_file_of({"key": "K"}, "x", problems, parse_time_zone("Asia/Tokyo"))
    utc = result_file_of({"key": "K"}, "x", problems)
    at = datetime(2026, 11, 8, 1, 30, tzinfo=timezone.utc).timestamp() * 1000
    assert time_value("2026-11-08T10:30", tokyo) == at and time_value("2026/11/08 10:30", tokyo) == at
    assert time_value("2026-11-08T01:30", utc) == at and time_value("2026-11-08T10:30+09:00", utc) == at
    assert time_value("2026-11-08 01:30:00Z", tokyo) == at
    assert time_value(at / 1000, tokyo) == at and time_value(at, tokyo) == at
    assert time_value("2026-11-08", tokyo) == datetime(2026, 11, 8, tzinfo=JST).timestamp() * 1000
    assert time_value("202611081030", tokyo, "%Y%m%d%H%M") == at and time_value("2026-11-08", tokyo, "%Y%m%d%H%M") is None
    # Digits alone: epoch seconds (10) or ms (13), or a compact date and time.
    assert time_value("202611081030", tokyo) == at and time_value(str(int(at / 1000)), tokyo) == at and time_value(str(int(at)), tokyo) == at
    assert time_value(at / 1000, tokyo, "unix") == at and time_value(str(int(at)), tokyo, "unixms") == at
    assert time_value("2026/11/08", tokyo, "iso-date") == datetime(2026, 11, 8, tzinfo=JST).timestamp() * 1000
    assert time_value("", tokyo) is None and time_value("あした", tokyo) is None
    assert problems == []


def test_reading_csv_and_json_files():
    problems = []
    spec = result_file_of({"key": "依頼ID", "result": "判定", "reason": "理由", "plannedAt": "予定", "satellite": "衛星", "encoding": "cp932",
                           "externalId": "番号", "name": "名称", "geometry": {"wkt": "範囲"}}, "x", problems,
                          parse_time_zone("Asia/Tokyo"))
    text = ("依頼ID,判定,理由,予定,衛星,番号,名称,範囲\n"
            "R-0001,OK,,2026/11/08 10:30,OPT-X 1,T-1,,\n"
            "R-0002,NG,雲,,,,,\n"
            ",OK,,2026-11-08 11:00,OPT-X 2,T-9,定常 横須賀,\"POLYGON ((139.6 35.2, 139.7 35.2, 139.7 35.3, 139.6 35.2))\"\n"
            "R-0003,保留,,,,,,\n"
            "R-0004,OK,,明日,,,,\n")
    answers, problems = read_answers(text.encode("cp932"), spec)
    assert [(a.line, a.key, a.answer) for a in answers] == [(2, "R-0001", ADOPTED), (3, "R-0002", REJECTED), (4, "", ADOPTED), (6, "R-0004", ADOPTED)]
    assert problems == ["5 行目: 採否「保留」が不明（列の読み替え map に 保留=採用 のように書く）", "6 行目: 撮像予定「明日」を日時として読めない"]
    assert answers[0].planned_at == datetime(2026, 11, 8, 10, 30, tzinfo=JST).timestamp() * 1000 and answers[0].external_id == "T-1"
    assert answers[1].reason == "雲" and answers[2].name == "定常 横須賀" and answers[2].rings == [[[139.6, 35.2], [139.7, 35.2], [139.7, 35.3], [139.6, 35.2]]]
    # A BOM (Excel's UTF-8) and names with other case are read.
    answers, problems = read_answers("﻿req,STATUS\nR-1,ok\n".encode("utf-8"), result_file_of({"key": "REQ", "result": "status"}, "x", []))
    assert [(a.key, a.answer) for a in answers] == [("R-1", ADOPTED)] and problems == []
    assert read_answers("依頼".encode("cp932"), result_file_of({"key": "K"}, "x", []))[1] == ["文字コード utf-8 で読めない"]

    spec = result_file_of({"format": "json", "records": "result.tasks", "key": "request.id", "result": "accepted", "adopted": [True],
                           "rejected": [False], "plannedAt": "time", "geometry": {"lon": "lon", "lat": "lat", "sizeKm": 20}}, "x", [])
    body = {"result": {"tasks": [
        {"request": {"id": "R-0001"}, "accepted": True, "time": "2026-11-08T01:30:00Z"},
        {"request": {"id": "R-0002"}, "accepted": False},
        {"request": {}, "accepted": True, "lon": 139.7, "lat": 35.3},
    ]}}
    answers, problems = read_answers(json.dumps(body).encode(), spec)
    assert problems == [] and [(a.key, a.answer) for a in answers] == [("R-0001", ADOPTED), ("R-0002", REJECTED), ("", ADOPTED)]
    w, s = answers[2].rings[0][0]
    e, n = answers[2].rings[0][2]
    assert round(n - s, 3) == round(20 / 110.574, 3) and w < 139.7 < e
    assert read_answers(b"{}", spec)[1] == ["result.tasks が配列でない"]
    assert read_answers(b"[", spec)[1][0].startswith("JSON として読めない")


def test_a_file_answers_the_day_of_one_system():
    fake = server()
    client, s, requests = written_day(fake)
    spec = result_file_of({"key": "ID", "result": "R", "reason": "WHY", "plannedAt": "T", "satellite": "SAT", "name": "NAME",
                           "externalId": "TASK", "missing": "不採用", "geometry": {"bbox": ["W", "S", "E", "N"]}}, "x", [],
                          parse_time_zone("Asia/Tokyo"))
    # SYS-A took 東京湾, says nothing of 横浜 (held back: not handed to it), and planned 大阪湾 (handed to SYS-B) and
    # a routine task of its own; 不採用 for a request not handed to it is nothing to it.
    text = ("ID,R,WHY,T,SAT,NAME,TASK,W,S,E,N\n"
            "R-0001,OK,,2026-11-08 10:30,OPT-X 2,,A-1,,,,\n"
            "R-0002,OK,,2026-11-08 11:00,OPT-X 1,,A-2,,,,\n"
            "R-0003,NG,,,,,,,,,\n"
            "X-7,OK,定常,2026-11-08 12:00,OPT-X 1,定常 東京,A-3,139.0,35.0,139.2,35.2\n")
    answers, problems = read_answers(text.encode(), spec)
    assert problems == []
    day = load_day(client, s, None, DAY.isoformat())
    assert [r.request_id for r in handed_rows(day, "SYS-A")] == ["R-0001"]
    reading = match_answers(answers, day, requests, "SYS-A", DAY.isoformat(), spec, "a_result.csv")
    assert reading.problems == ["4 行目: 不採用の 「横浜」 はこの日このシステムに出力していないため読み飛ばし"]
    assert reading.tally() == {ADOPTED: 1, REJECTED: 0, WAITING: 0, UNREQUESTED: 2, NOT_IN_FILE: 0}
    changes = answer_changes(reading)
    assert [v for _, v in changes] == [{"adoption": ADOPTED, "adoptionReason": None, "plannedAt": datetime(2026, 11, 8, 10, 30, tzinfo=JST).timestamp() * 1000,
                                        "externalId": "A-1", "adoptionSource": "a_result.csv", "satellite": "OPT-X 2"}]
    adds = unrequested_rows(reading, FIRST, "RUN-A")
    assert [(r.request_id, r.name, r.result, r.adoption, r.reason, r.external_id, r.system) for r in adds] == [
        ("R-0002", "大阪湾", UNREQUESTED, ADOPTED, NOT_HANDED, "A-2", "SYS-A"), ("", "定常 東京", UNREQUESTED, ADOPTED, "", "A-3", "SYS-A")]
    # Areas: the request's for one of ours, the file's for its own.
    assert adds[0].rings == esri_rings(OSAKA) and adds[1].rings == [[[139.0, 35.0], [139.2, 35.0], [139.2, 35.2], [139.0, 35.2], [139.0, 35.0]]]
    assert update_history(client, s, None, changes) == 1 and add_history(client, s, None, adds) == 2

    day = load_day(client, s, None, DAY.isoformat())
    states = {(r.name, r.system): state_of(r.result, r.adoption) for r in day}
    assert states == {("東京湾", "SYS-A"): ADOPTED, ("大阪湾", "SYS-B"): AWAITING, ("横浜", ""): "見送り",
                      ("大阪湾", "SYS-A"): UNREQUESTED, ("定常 東京", "SYS-A"): UNREQUESTED}
    assert day_counts(client, s, None, "2026-11-01", "2026-11-30") == {DAY.isoformat(): {ADOPTED: 1, AWAITING: 1, "見送り": 1, UNREQUESTED: 2}}
    assert day_counts(client, s, None, "2026-12-01", "2026-12-31") == {}
    # 要求外 rows are not requests handed out: never followed up, raised, or replaced by writing the day again.
    assert {r.request_id for r in load_history(client, s, None)} == {"R-0001", "R-0002", "R-0003"}
    assert len(load_history(client, s, None)) == 3
    delete_history(client, s, None, replaced_where(s, DAY.isoformat(), "SYS-A"))
    # (SYS-A's 東京湾 is replaced; SYS-B's row, the one held back for all and the 要求外 stay.)
    assert sorted(r.name for r in load_day(client, s, None, DAY.isoformat())) == ["大阪湾", "大阪湾", "定常 東京", "横浜"]

    # SYS-B's file leaves 大阪湾 out: 不採用 (結果ファイルになし), by `missing`.
    spec_b = result_file_of({"key": "ID"}, "x", [])
    day = load_day(client, s, None, DAY.isoformat())
    reading = match_answers([], day, requests, "SYS-B", DAY.isoformat(), spec_b, "b.csv")
    assert [r.request_id for r in reading.missing] == ["R-0002"]
    [(oid, values)] = answer_changes(reading)
    assert values == {"adoption": REJECTED, "adoptionReason": NOT_IN_FILE, "plannedAt": None, "externalId": None, "adoptionSource": "b.csv"}
    # … or left 不明.
    spec_b.missing = WAITING
    assert answer_changes(match_answers([], day, requests, "SYS-B", DAY.isoformat(), spec_b, "b.csv")) == []


def test_a_file_read_again_replaces_its_own_unrequested_rows_not_those_by_hand():
    fake = server()
    client, s, requests = written_day(fake)
    d = DAY.isoformat()
    by_hand = manual_unrequested(d, "SYS-A", "電話で聞いた定常", FIRST, OSAKA, satellite="OPT-X 1", note="毎週")
    assert (by_hand.result, by_hand.adoption, by_hand.adoption_source, by_hand.request_id) == (UNREQUESTED, ADOPTED, MANUAL, "")
    spec = result_file_of({"key": "ID", "name": "NAME"}, "x", [])
    for text in ("ID,NAME\nR-0001,\nX-1,定常 1\nX-2,定常 2\n", "ID,NAME\nR-0001,\nX-1,定常 1\n"):
        answers, _ = read_answers(text.encode(), spec)
        reading = match_answers(answers, load_day(client, s, None, d), requests, "SYS-A", d, spec, "a.csv")
        delete_history(client, s, None, unrequested_where(s, d, "SYS-A", by_hand=False))
        update_history(client, s, None, answer_changes(reading))
        add_history(client, s, None, unrequested_rows(reading, FIRST))
        if by_hand:
            add_history(client, s, None, [by_hand])
            by_hand = None
    names = sorted(r.name for r in load_day(client, s, None, d) if r.result == UNREQUESTED)
    assert names == ["定常 1", "電話で聞いた定常"]
    # Taken off by hand.
    gone = [r.object_id for r in load_day(client, s, None, d) if r.name == "定常 1"]
    assert delete_history_rows(client, s, None, gone) == 1
    assert sorted(r.name for r in load_day(client, s, None, d) if r.result == UNREQUESTED) == ["電話で聞いた定常"]


def test_answers_by_hand():
    fake = server()
    client, s, _ = written_day(fake)
    d = DAY.isoformat()
    day = load_day(client, s, None, d)
    tokyo = next(r for r in day if r.name == "東京湾")
    held = next(r for r in day if r.name == "横浜")
    at = FIRST + 10 * H
    # Held-back rows are nobody's to answer.
    changes = manual_changes([tokyo, held], REJECTED, "雲が多い", planned_at=at)
    assert changes == [(tokyo.object_id, {"adoption": REJECTED, "adoptionReason": "雲が多い", "plannedAt": at, "adoptionSource": MANUAL})]
    assert update_history(client, s, None, changes) == 1
    row = next(r for r in load_day(client, s, None, d) if r.name == "東京湾")
    assert (row.adoption, row.adoption_reason, row.planned_at, row.adoption_source) == (REJECTED, "雲が多い", at, MANUAL)
    # Taken back: 不明 again, its answer gone.
    update_history(client, s, None, manual_changes([row], WAITING))
    row = next(r for r in load_day(client, s, None, d) if r.name == "東京湾")
    assert (row.adoption, row.adoption_reason, row.planned_at, row.adoption_source) == (WAITING, "", None, "")


def test_writing_the_day_again_keeps_the_systems_answers():
    # tsudo, 2026-11-09: 「置き換えて書き出し」 used to delete the 採否 read in; now carried over, or kept.
    fake = server()
    client, s, loaded = written_day(fake)
    d = DAY.isoformat()
    day = load_day(client, s, None, d)
    tokyo = next(r for r in day if r.name == "東京湾")
    osaka = next(r for r in day if r.name == "大阪湾")
    at = FIRST + 10 * H
    assert update_history(client, s, None, [
        (tokyo.object_id, {"adoption": ADOPTED, "plannedAt": at, "externalId": "EXT-1", "adoptionSource": "a-result.csv"}),
        (osaka.object_id, {"adoption": REJECTED, "adoptionReason": "枠なし", "adoptionSource": MANUAL})]) == 2
    # Written again without 大阪湾 (and 横浜 still held back).
    plan = plan_day([r for r in loaded if r.id != "R-0002"], DAY, systems(), SATS, {"R-0001": 30, "R-0003": 30})
    rows = history_of(plan, FIRST - 8 * H, {"SYS-A": "a2.csv", "SYS-B": "b2.csv", "SYS-C": "c2.json"}, "RUN2")
    rewrite = keep_answers(load_history(client, s, None, replaced_where(s, d)), rows)
    assert (rewrite.carried, rewrite.kept, len(rewrite.gone)) == (1, 1, 2)
    assert int(delete_history_rows(client, s, None, rewrite.gone)) == 2
    assert add_history(client, s, None, rewrite.rows) == len(rows)
    now = {r.name: r for r in load_day(client, s, None, d)}
    assert set(now) == {"東京湾", "大阪湾", "横浜"}
    # 東京湾 is in SYS-A's new file: the new row, with the system's answer.
    t = now["東京湾"]
    assert (t.run_id, t.file, t.adoption, t.planned_at, t.external_id, t.adoption_source) == ("RUN2", "a2.csv", ADOPTED, at, "EXT-1", "a-result.csv")
    # 大阪湾 is not handed out this time: SYS-B's answer kept as it was; 横浜 (no answer) replaced.
    o = now["大阪湾"]
    assert (o.run_id, o.system, o.adoption, o.adoption_reason) == ("RUN1", "SYS-B", REJECTED, "枠なし")
    assert now["横浜"].run_id == "RUN2"
    # Rows still waiting (不明) are simply replaced, as before.
    again = keep_answers([r for r in load_history(client, s, None, replaced_where(s, d)) if r.name == "横浜"], rows)
    assert (again.carried, again.kept, again.gone) == (0, 0, [now["横浜"].object_id])


def test_an_image_in_tells_it_was_planned():
    """tsudo, 2026-11-09: 「採否がわからないやつもある。入ってきたら、採用されてたんだなとなる」."""
    assert state_of("採用", "", "未判定") == AWAITING == "不明"
    assert state_of("採用", "不明", "撮像済み") == ADOPTED and state_of("採用", "不採用", "撮像済み") == ADOPTED
    assert state_of("採用", "不採用", "未撮像") == REJECTED and state_of("見送り", "", "") == "見送り"
    assert state_of(UNREQUESTED, ADOPTED, "") == UNREQUESTED
