"""
The systems' result files (採否) in forms as free as the order files'
(tsudo, 2026-11-10: 「撮像関連、システムから結果ファイルのインポート、オーダーファイル
みたいに様式をフルカスタムできると嬉しい」): CSV columns by heading or place with
targets, value maps, patterns, times in any form, rows kept or dropped by a
column; JSON by dot paths or by the system's JSON Schema with `x-target`.
"""
from __future__ import annotations

import json
from datetime import datetime

import pytest

from skywatch.core.request import export
from skywatch.core.request.adoption import NOT_IN_FILE, answer_changes, match_answers, same_key, unrequested_rows
from skywatch.core.request.model import JST
from skywatch.core.request.result_file import (
    ADOPTED, GEOMETRIES, REJECTED, TARGETS, WAITING, geometry_rings, read_answers, result_file_of, time_value,
)
from skywatch.core.request.store import UNREQUESTED, load_day
from skywatch.core.request.systems import export_systems_of
from skywatch.core.time_zone import parse_time_zone
from skywatch.tests.test_request import DAY, FIRST, SYSTEMS_JSON
from skywatch.tests.test_request_adoption import server, written_day

TOKYO_ZONE = parse_time_zone("Asia/Tokyo")


def jst(*args) -> float:
    return datetime(*args, tzinfo=JST).timestamp() * 1000


def spec_of(value, zone=TOKYO_ZONE, values=None):
    problems = []
    spec = result_file_of(value, "resultFile", problems, zone, values)
    assert problems == [], problems
    return spec


def test_csv_columns_with_maps_patterns_joined_times_and_row_filters():
    spec = spec_of({
        "delimiter": "tab", "skipLines": 2, "encoding": "cp932",
        "columns": [
            {"key": "依頼", "target": "identificationNo", "pattern": r"^REQ-(\d+)"},
            {"key": "判定", "target": "result", "map": {"○": "採用", "×": "不採用", "保留": "不明", "": "不明"}},
            {"key": "予定日", "target": "plannedAt"}, {"key": "予定時刻", "target": "plannedAt", "format": "%Y%m%d %H%M"},
            {"key": "衛星", "target": "satellite", "default": "OPT-X 1"},
            {"key": "理由1", "target": "reason"}, {"key": "理由2", "target": "reason"},
            {"key": "種別", "only": ["計画", "変更"]}, {"key": "取消", "skip": ["1"]},
            {"target": "system", "value": "SYS-A"},
        ]})
    text = ("撮像計画 回答\n作成 2026/11/07\n"
            "依頼\t判定\t予定日\t予定時刻\t衛星\t理由1\t理由2\t種別\t取消\n"
            "REQ-000123-A\t○\t20261108\t1030\t\t\t\t計画\t\n"
            "REQ-77\t×\t\t\tOPT-X 2\t雲\t予報 80%\t変更\t\n"
            "REQ-78\t保留\t\t\t\t\t\t計画\t\n"
            "REQ-79\t\t\t\t\t\t\t計画\t\n"
            "REQ-80\t○\t\t\t\t\t\t参考\t\n"
            "REQ-81\t○\t\t\t\t\t\t計画\t1\n"
            "REQ-82\tOK\t\t\t\t\t\t計画\t\n")
    answers, problems = read_answers(text.encode("cp932"), spec)
    assert [(a.line, a.key, a.answer) for a in answers] == [
        (4, "000123", ADOPTED), (5, "77", REJECTED), (6, "78", WAITING), (7, "79", WAITING)]
    # A value the map does not name is not guessed (OK is 採用 only without a map).
    assert problems == ["10 行目: 採否「OK」が不明（列の読み替え map に OK=採用 のように書く）", "only・skip で 2 行を読み飛ばし"]
    first, second = answers[0], answers[1]
    assert first.planned_at == jst(2026, 11, 8, 10, 30) and first.satellite == "OPT-X 1" and first.system == "SYS-A"
    assert second.reason == "雲 予報 80%" and second.satellite == "OPT-X 2" and first.stated
    assert first.keys == {"identificationNo": "000123"}


def test_csv_without_headings_reads_columns_in_order_or_by_number():
    spec = spec_of({"header": False, "delimiter": ";", "columns": [
        {"key": "ID", "target": "id"}, {"key": "(使わない)"}, {"key": "OK?", "target": "result"},
        {"key": "LAT", "column": 6, "target": "centerLat"}, {"key": "LON", "column": 5, "target": "centerLon"}]})
    answers, problems = read_answers(b"R-1;x;yes;;139.7;35.3\nR-2;x;no\n", spec)
    assert problems == [] and [(a.line, a.key, a.answer) for a in answers] == [(1, "R-1", ADOPTED), (2, "R-2", REJECTED)]
    w, s = answers[0].rings[0][0]
    assert w < 139.7 and s < 35.3 and answers[1].rings == []
    # A heading that is not in the file is told once.
    spec = spec_of({"columns": [{"key": "ID", "target": "id"}, {"key": "WHEN", "target": "plannedAt"}]})
    answers, problems = read_answers(b"ID,TIME\nR-1,x\nR-2,y\n", spec)
    assert problems == ["列「WHEN」が見出しにない"] and len(answers) == 2


def test_json_by_dot_paths():
    spec = spec_of({"format": "json", "records": "data.items", "columns": [
        {"key": "order.managementNo", "target": "managementNo"},
        {"key": "status", "target": "result", "map": {"ACCEPT": "採用", "REJECT": "不採用", "*": "不明"}},
        {"key": "window.start", "target": "plannedAt", "format": "unix"},
        {"key": "footprint", "target": "geometry"},
        {"key": "passes.0.satellite", "target": "satellite"},
    ]})
    body = {"data": {"items": [
        {"order": {"managementNo": "K-1"}, "status": "accept", "window": {"start": 1794101400},
         "footprint": {"type": "Polygon", "coordinates": [[[139, 35], [139.1, 35], [139.1, 35.1], [139, 35]]]},
         "passes": [{"satellite": "OPT-X 2"}, {"satellite": "OPT-X 1"}]},
        {"order": {"managementNo": "K-2"}, "status": "QUEUED"},
    ]}}
    answers, problems = read_answers(json.dumps(body).encode(), spec)
    assert problems == []
    assert [(a.keys, a.answer) for a in answers] == [({"managementNo": "K-1"}, ADOPTED), ({"managementNo": "K-2"}, WAITING)]
    assert answers[0].planned_at == 1794101400000 and answers[0].satellite == "OPT-X 2"
    assert answers[0].rings == [[[139, 35], [139.1, 35], [139.1, 35.1], [139, 35]]]


SCHEMA = {
    "type": "object",
    "required": ["issued", "results"],
    "$defs": {"aoi": {"type": "object", "x-target": "geometry"}},
    "properties": {
        "issued": {"type": "string", "format": "date"},
        "planDay": {"type": "string", "x-target": "day"},
        "system": {"type": "string", "x-target": "system", "x-map": {"A": "SYS-A"}},
        "results": {
            "type": "array", "x-target": "records",
            "items": {
                "type": "object", "required": ["req"],
                "properties": {
                    "req": {"type": "object", "properties": {
                        "id": {"type": "string", "x-target": "id"},
                        "no": {"type": "string", "x-target": "managementNo"}}},
                    "accepted": {"type": "boolean", "x-target": "result"},
                    "eta": {"type": "string", "x-target": "plannedAt"},
                    "aoi": {"$ref": "#/$defs/aoi"},
                    "task": {"type": "string", "x-target": "externalId", "x-pattern": "^T-(.*)$"},
                    "why": {"type": ["string", "null"], "x-target": "reason", "default": ""},
                },
            },
        },
    },
}


def test_json_by_the_systems_schema():
    spec = spec_of({"format": "json", "schema": SCHEMA})
    assert spec.says_result() and spec.missing == WAITING and {"id", "managementNo", "result", "day", "system"} <= spec.targets()
    body = {"issued": "2026-11-07", "planDay": "2026-11-08", "system": "A", "results": [
        {"req": {"id": "R-0001"}, "accepted": True, "eta": "2026-11-08T01:30:00Z", "task": "T-55",
         "aoi": {"type": "Point", "coordinates": [139.75, 35.5]}},
        {"req": {"no": "K-3"}, "accepted": False, "why": "雲"},
    ]}
    answers, problems = read_answers(json.dumps(body).encode(), spec)
    assert problems == []
    a, b = answers
    assert (a.keys, a.answer, a.external_id, a.day, a.system) == ({"id": "R-0001"}, ADOPTED, "55", "2026-11-08", "SYS-A")
    assert a.planned_at == jst(2026, 11, 8, 10, 30) and len(a.rings[0]) == 5
    assert (b.keys, b.answer, b.reason) == ({"managementNo": "K-3"}, REJECTED, "雲")
    # The file is checked against the schema (and read all the same).
    del body["issued"]
    body["results"][1]["accepted"] = "no"
    answers, problems = read_answers(json.dumps(body).encode(), spec)
    assert problems == ["スキーマ: $: 必須の issued なし", "スキーマ: $.results[1].accepted: 型が boolean 以外"]
    assert [x.answer for x in answers] == [ADOPTED, REJECTED]


def test_a_schema_in_a_file_is_read_with_the_file():
    spec = spec_of({"format": "json", "schema": "schemas/sys-a.result.schema.json"})
    assert spec.says_result() is None
    body = json.dumps({"issued": "2026-11-08", "results": [{"req": {"id": "R-1"}, "accepted": True}]}).encode()
    answers, problems = read_answers(body, spec, lambda path: SCHEMA)
    assert problems == [] and [a.key for a in answers] == ["R-1"]

    def gone(path):
        raise OSError("ない")

    assert read_answers(body, spec, gone) == ([], ["スキーマ schemas/sys-a.result.schema.json の読み込み不可（ない）"])
    assert read_answers(body, spec, lambda p: {"type": "object", "properties": {"r": {"x-target": "records"}}})[1] == [
        "スキーマ schemas/sys-a.result.schema.json に依頼を表すプロパティ（x-target が id・managementNo・identificationNo・linkId）なし"]


def test_settings_problems():
    problems = []
    assert result_file_of({"columns": [{"key": "A", "target": "result"}]}, "r", problems) is None
    assert result_file_of({"columns": [{"key": "A", "target": "id"}, {"key": "B", "target": "colour"}, {"target": "id"},
                                       {"key": "C", "target": "plannedAt", "column": 0, "pattern": "("}, "x"],
                           "key": "A", "delimiter": "/", "skipLines": -1, "sizeKm": 0}, "s", problems)
    assert result_file_of({"format": "csv", "schema": SCHEMA, "columns": [{"key": "A", "target": "id"}]}, "c", problems)
    assert result_file_of({"format": "json", "schema": {"type": "array", "items": {"x-target": "id"}}}, "j", problems) is None
    assert result_file_of({"format": "json", "schema": 3}, "k", problems) is None
    assert problems == [
        "r.columns に依頼を表す列（target が id・managementNo・identificationNo・linkId）なし",
        "s.delimiter は ,・tab・;・|・space のどれかで指定", "s.skipLines は 0 以上の整数で指定", "s.sizeKm は正の数（km）で指定",
        "s の key は columns か schema があるときは使わない（列に書く）",
        "s.columns[1].target「colour」は使えない（" + "・".join(TARGETS) + "）",
        "s.columns[2] に key（列名）・column（何列目）・value（固定値）のどれもなし",
        "s.columns[3].column は 1 からの列の番号で指定",
        "s.columns[3].pattern を正規表現として読めない（missing ), unterminated subpattern at position 0）",
        "s.columns[4] はオブジェクトで指定",
        "c.schema は format が json のときだけ",
        "j.schema に x-target: records の配列（結果の一覧）なし",
        "k.schema は JSON Schema かそのファイルのパスで指定",
    ]
    # The geometry kinds are the order files'.
    assert GEOMETRIES == export.GEOMETRIES


def test_geometry_kinds():
    assert geometry_rings("139.0,35.0,139.2,35.2", "bbox") == [[[139.0, 35.0], [139.2, 35.0], [139.2, 35.2], [139.0, 35.2], [139.0, 35.0]]]
    assert geometry_rings("35.0 139.0 35.0 139.2 35.2 139.2", "vertices") == [[[139.0, 35.0], [139.2, 35.0], [139.2, 35.2], [139.0, 35.0]]]
    square = geometry_rings("35.5,139.7", "center", 20)
    assert round(square[0][2][1] - square[0][0][1], 4) == round(20 / 110.574, 4)
    by_radius = geometry_rings("35.5,139.7,5", "centerRadius")
    assert round(by_radius[0][2][1] - by_radius[0][0][1], 4) == round(10 / 110.574, 4)
    assert geometry_rings("POINT (139.7 35.5)", "bbox") == geometry_rings("35.5,139.7", "center")
    assert geometry_rings('{"type":"Point","coordinates":[139.7,35.5]}', "wkt") == geometry_rings("35.5,139.7", "center")
    assert geometry_rings([139.0, 35.0, 139.2, 35.2], "bbox")[0][2] == [139.2, 35.2]
    assert geometry_rings("あ", "bbox") == [] and geometry_rings("1,2,3", "bbox") == [] and geometry_rings("", "wkt") == []


def test_times_as_excel_writes_them():
    spec = spec_of({"columns": [{"key": "ID", "target": "id"}]})
    at = jst(2026, 11, 8, 9, 30)
    assert time_value("2026/11/8 9:30", spec) == at and time_value("2026-11-8 9:30", spec) == at
    assert time_value("2026/11/08 09:30:00", spec) == at and time_value("2026.11.08 09:30", spec) == at


def test_matching_by_any_key_day_and_system():
    fake = server()
    client, s, requests = written_day(fake)
    d = DAY.isoformat()
    day = load_day(client, s, None, d)
    values = {"satellite": {"OPT-X 1": "X1"}}
    spec = spec_of({"columns": [
        {"key": "ID", "target": "id"}, {"key": "KANRI", "target": "managementNo"}, {"key": "R", "target": "result"},
        {"key": "SAT", "target": "satellite"}, {"key": "DAY", "target": "day", "format": "%Y%m%d"}, {"key": "SYS", "target": "system"},
        {"key": "AREA", "target": "geometry", "geometry": "bbox"}]}, values=values)
    text = ("ID,KANRI,R,SAT,DAY,SYS,AREA\n"
            "X-404,k-0001,保留,X1,20261108,a,\n"      # neither an id nor a 管理番号 of ours (K-0001 is not K-1: not a number)
            ",K-2,OK,,20261108,SYS-B,\n"              # another system's row
            ",K-1,OK,,20261109,A,\n"                   # another day's row
            "T-9,,OK,X1,20261108,A,\"139,35,139.1,35.1\"\n")  # its own plan (要求外)
    answers, problems = read_answers(text.encode(), spec)
    assert problems == []
    assert same_key("k-0001") != same_key("K-1") and same_key("000123") == same_key("123")
    reading = match_answers(answers, day, requests, "SYS-A", d, spec, "a.csv", "A")
    assert reading.problems == ["2 行目: 不明の X-404 はこの日このシステムに出力していないため読み飛ばし",
                                "対象日が 2026-11-08 以外の 1 行を読み飛ばし", "ほかのシステム（SYS-B）の行を読み飛ばし"]
    # The first row is not ours and, being 不明, is nothing; 東京湾 is missing (left 不明: the file says 採否).
    assert reading.tally() == {ADOPTED: 0, REJECTED: 0, WAITING: 0, UNREQUESTED: 1, NOT_IN_FILE: 1} and reading.missing_as == WAITING
    [own] = unrequested_rows(reading, FIRST, "RUN")
    assert (own.result, own.satellite, own.external_id, own.rings[0][2]) == (UNREQUESTED, "OPT-X 1", "T-9", [139.1, 35.1])

    text = "ID,KANRI,R,SAT,DAY,SYS,AREA\nX-404,K-0001,保留,X1,2026-11-08,A,\n,K-2,OK,,,,\n"
    spec = spec_of({"columns": [
        {"key": "ID", "target": "id"}, {"key": "KANRI", "target": "managementNo", "pattern": r"K-(\d+)", "map": {}},
        {"key": "R", "target": "result"}, {"key": "SAT", "target": "satellite"}, {"key": "DAY", "target": "day"}]}, values=values)
    for req in requests:
        req.management_no = {"K-1": "1", "K-2": "2", "K-3": "3"}[req.management_no]
    answers, problems = read_answers(text.encode(), spec)
    reading = match_answers(answers, day, requests, "SYS-A", d, spec, "a.csv", "A")
    # 管理番号 0001 = 1: 東京湾, answered 不明 by the file; 大阪湾 (handed to SYS-B) planned by SYS-A too: 要求外.
    assert [(m.request.name, m.answer.answer, m.unrequested) for m in reading.matches] == [("東京湾", WAITING, False), ("大阪湾", ADOPTED, True)]
    [(oid, values)] = answer_changes(reading)
    assert values["adoption"] == WAITING and values["satellite"] == "OPT-X 1" and values["adoptionSource"] == "a.csv"


def test_a_file_without_a_result_column_lists_the_adopted_ones():
    spec = spec_of({"columns": [{"key": "ID", "target": "id"}]})
    assert spec.missing == REJECTED and not spec.says_result()
    answers, _ = read_answers(b"ID\nR-1\n", spec)
    assert [(a.answer, a.stated) for a in answers] == [(ADOPTED, False)]
    # Or a file of the rejected ones only: a fixed value.
    spec = spec_of({"columns": [{"key": "ID", "target": "id"}, {"target": "result", "value": "不採用"}], "missing": "不明"})
    answers, _ = read_answers(b"ID\nR-1\n", spec)
    assert [(a.answer, spec.missing) for a in answers] == [(REJECTED, WAITING)]


def test_a_systems_result_file_in_the_settings():
    problems = []
    value = [dict(SYSTEMS_JSON[0], values={"satellite": {"OPT-X 1": "X1"}},
                  resultFile={"format": "json", "schema": "a.schema.json"}), SYSTEMS_JSON[1]]
    a, _ = export_systems_of(value, problems)
    assert problems == [] and a.result_file.schema == "a.schema.json" and a.result_file.reverse == {"satellite": {"X1": "OPT-X 1"}}


@pytest.mark.parametrize("encoding,codec", [("utf-16", "utf-16"), ("euc-jp", "euc-jp"), ("utf-8-sig", "utf-8-sig")])
def test_more_encodings(encoding, codec):
    spec = spec_of({"encoding": encoding, "delimiter": "\t", "columns": [{"key": "依頼", "target": "id"}]})
    answers, problems = read_answers("依頼\nR-1\n".encode(codec), spec)
    assert problems == [] and [a.key for a in answers] == ["R-1"]
