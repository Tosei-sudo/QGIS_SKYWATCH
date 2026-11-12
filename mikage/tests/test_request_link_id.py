"""
外部連携ID (tsudo, 2026-11-12: 「一部のシステムが名前にUUIDの長さに対応していないので、
12文字程度の、完全な唯一性はいいので、識別できる番号を。システムからのインポート時に、
日付の蓋然性とそのIDから特定の依頼と照合できるように」): a short id per history
row, written in the order files (`linkId`) and matched back from the result
files among the system's rows of the nearest day.
"""
from __future__ import annotations

import csv
import io
from datetime import timedelta

from mikage.core.esri import EsriClient
from mikage.core.request import link_id as link_module
from mikage.core.request.adoption import answer_changes, link_window, match_answers, nearest_linked
from mikage.core.request.export import files_of, render, uses_link_id
from mikage.core.request.link_id import ALPHABETS, link_id, link_ids
from mikage.core.request.model import ImagingRequest
from mikage.core.request.result_file import LINK_ID, read_answers, result_file_of
from mikage.core.request.store import (
    DEFAULT_HISTORY_FIELDS, HistoryRow, add_history, history_attributes, history_row_of, load_linked, request_store_of,
)
from mikage.core.request.systems import ExportSystem, export_systems_of
from mikage.core.request.workflow import history_of, keep_answers, plan_day
from mikage.core.request.passes import day_passes
from mikage.tests.fake_esri import FakeEsri, FakeLayer
from mikage.tests.test_plan import BELOW, T
from mikage.tests.test_request_passes import DAY, SAT, along, point

HISTORY = "https://gis.example/rest/services/R/FeatureServer/1"
LEDGER = "https://gis.example/rest/services/R/FeatureServer/0"


def linked_system(**kw) -> ExportSystem:
    problems: list = []
    entry = {"system": "SYS", "series": ["ISS"], "columns": [
        {"key": "ID", "source": "id"}, {"key": "LINK", "source": "linkId"}, {"key": "NAME", "source": "linkId", "format": "SW-%s"},
        {"key": "IDS", "source": "mergedIds"}]}
    entry.update(kw)
    [system] = export_systems_of([entry], problems)
    assert problems == []
    return system


def _plan(reqs, systems):
    return plan_day(reqs, DAY, systems, [SAT], {}, passes=day_passes(reqs, DAY, [SAT]))


def _csv(text: str) -> list[dict]:
    return list(csv.DictReader(io.StringIO(text)))


# --- The id ----------------------------------------------------------------------------------

def test_an_id_is_short_and_made_of_plain_characters():
    value = link_id("3F2504E0-4F89-11D3-9A0C-0305E82C3301", "2026-11-12", "SYS-A")
    assert len(value) == 12 and set(value) <= set(ALPHABETS["base32"])
    assert not set(value) & set("ILOU")
    # Worked out, not drawn: the same request, day, system and time give the same id.
    assert link_id("3F2504E0-4F89-11D3-9A0C-0305E82C3301", "2026-11-12", "SYS-A") == value
    others = {link_id("3F2504E0-4F89-11D3-9A0C-0305E82C3301", "2026-11-13", "SYS-A"),
              link_id("3F2504E0-4F89-11D3-9A0C-0305E82C3301", "2026-11-12", "SYS-B"),
              link_id("3F2504E0-4F89-11D3-9A0C-0305E82C3301", "2026-11-12", "SYS-A", 2),
              link_id("R-0002", "2026-11-12", "SYS-A")}
    assert value not in others and len(others) == 4
    assert link_id("R1", "2026-11-12", "S", 1, 8, "digits").isdigit() and len(link_id("R1", "2026-11-12", "S", 1, 8, "digits")) == 8
    assert set(link_id("R1", "2026-11-12", "S", 1, 20, "hex")) <= set("0123456789ABCDEF")
    # Out of range lengths are kept to 6 … 32.
    assert len(link_id("R1", "2026-11-12", "S", 1, 2)) == 6 and len(link_id("R1", "2026-11-12", "S", 1, 99)) == 32


def test_ids_of_a_day_are_different_even_when_the_hash_meets(monkeypatch):
    plain = link_module.link_id
    # Every request hashed to the same id without salt: the second by key order is worked out again.
    monkeypatch.setattr(link_module, "link_id", lambda rid, day, system, occ=1, length=12, chars="base32", salt=0:
                        "SAME00000000" if salt == 0 else plain(rid, day, system, occ, length, chars, salt))
    a, b = point(1, BELOW, 300), point(4, along(120))
    system = linked_system(merge=False)
    plan = _plan([a, b], [system])
    ids = link_ids(plan.candidates, DAY.isoformat(), system)
    assert ids["R1"] == "SAME00000000" and ids["R4"] != "SAME00000000" and len(ids["R4"]) == 12


def test_the_system_settings_of_the_id():
    problems: list = []
    [s] = export_systems_of([{"system": "A", "columns": [{"key": "L", "source": "linkId"}], "linkIdLength": 8, "linkIdChars": "digits"}], problems)
    assert problems == [] and (s.link_length, s.link_chars) == (8, "digits")
    problems = []
    [s] = export_systems_of([{"system": "A", "columns": [{"key": "L", "source": "id"}], "linkIdLength": 3, "linkIdChars": ["x"]}], problems)
    assert (s.link_length, s.link_chars) == (12, "base32")
    assert problems == ["requestExports[0] の linkIdLength は 6 から 32 の整数（外部連携IDの文字数）で指定",
                        "requestExports[0] の linkIdChars は base32・digits・hex のどれかで指定"]


# --- The file and the history -------------------------------------------------------------------

def test_the_file_writes_the_id_its_history_row_keeps():
    a, b, alone = point(1, BELOW, 300), point(2, along(2.5)), point(4, along(120))
    system = linked_system()
    plan = _plan([a, b, alone], [system])
    files = render(plan.candidates, [system], DAY, T)
    assert files[0].links
    rows_of_file = _csv(files[0].text)
    history = {r.request_id: r for r in history_of(plan, T, files_of(files), "RUN")}
    # R2 is put into R1's shot: the file's row is R1's, with R1's id; R2's history row has its own.
    assert [(r["ID"], r["LINK"], r["NAME"]) for r in rows_of_file] == [
        ("R1", history["R1"].link_id, "SW-" + history["R1"].link_id), ("R4", history["R4"].link_id, "SW-" + history["R4"].link_id)]
    assert history["R2"].merged_into == "R1" and history["R2"].link_id and history["R2"].link_id != history["R1"].link_id
    assert all(len(r.link_id) == 12 for r in history.values())
    # Writing the day again gives the same ids (a system answering the earlier file still finds the rows).
    again = {r.request_id: r.link_id for r in history_of(_plan([a, b, alone], [system]), T + 3600000, files_of(files), "RUN2")}
    assert again == {k: r.link_id for k, r in history.items()}
    # 置き換え: the new rows keep their own ids (the same ones), whatever the earlier rows held.
    earlier = [HistoryRow(run_id="RUN", run_date=T, day=DAY.isoformat(), request_id="R1", system="SYS", adoption="採用",
                          object_id=1, link_id="OLDOLDOLDOLD")]
    rewrite = keep_answers(earlier, history_of(plan, T, files_of(files), "RUN3"))
    assert next(r for r in rewrite.rows if r.request_id == "R1").link_id == history["R1"].link_id


def test_a_row_held_back_or_not_written_has_no_id():
    a = point(1, BELOW, 300)
    system = linked_system()
    plan = _plan([a], [system])
    [row] = history_of(plan, T, {}, "RUN")
    assert row.result == "見送り" and row.link_id == ""


def test_a_json_file_with_the_id_and_one_without():
    schema = {"type": "object", "properties": {"requests": {"type": "array", "x-source": "requests", "items": {
        "type": "object", "properties": {"id": {"type": "string", "x-source": "id"},
                                         "ref": {"type": "string", "x-source": "linkId"}}}}}}
    json_system = ExportSystem(system="SYS", label="SYS", series=["ISS"], format="json", schema=schema, link_length=16, link_chars="hex")
    plan = _plan([point(4, along(120))], [json_system])
    [w] = render(plan.candidates, [json_system], DAY, T)
    [row] = history_of(plan, T, files_of([w]), "RUN")
    assert w.links and f'"ref": "{row.link_id}"' in w.text and len(row.link_id) == 16
    assert not uses_link_id(ExportSystem(system="X", label="X", format="json", schema={"x-source": "id"}))
    assert not uses_link_id(linked_system(columns=[{"key": "ID", "source": "id"}]))


def test_the_history_table_keeps_the_id():
    f = dict(DEFAULT_HISTORY_FIELDS)
    row = HistoryRow(run_id="RUN", run_date=T, day="2026-11-12", request_id="R1", system="SYS", link_id="7K3QX9M2PA4T")
    attributes = history_attributes(row, f)
    assert attributes["LINK_ID"] == "7K3QX9M2PA4T"
    assert history_attributes(HistoryRow(run_id="RUN", run_date=T, day="2026-11-12", request_id="R1"), f)["LINK_ID"] is None
    back = history_row_of({**attributes, "OBJECTID": 5}, None, f, "OBJECTID")
    assert back.link_id == "7K3QX9M2PA4T"
    # A digits-only id kept in a numeric field comes back without 「.0」.
    assert history_row_of({**attributes, "LINK_ID": 123456789012.0, "OBJECTID": 5}, None, f, "OBJECTID").link_id == "123456789012"


def _server(with_link: bool = True) -> FakeEsri:
    fake = FakeEsri()
    names = [n for n in DEFAULT_HISTORY_FIELDS.values() if with_link or n != "LINK_ID"]
    fake.add_layer(HISTORY, FakeLayer({n: "esriFieldTypeString" for n in names}, "esriGeometryPolygon"))
    fake.add_layer(LEDGER, FakeLayer({"REQ_ID": "esriFieldTypeString", "REQ_NAME": "esriFieldTypeString"}, "esriGeometryPolygon"))
    return fake


def _row(rid, day, link, system="SYS", **kw) -> HistoryRow:
    return HistoryRow(run_id="RUN", run_date=T, day=day, request_id=rid, system=system, name=kw.pop("name", rid), adoption="不明",
                      link_id=link, **kw)


def test_the_rows_of_the_days_round_are_read_for_their_ids():
    fake = _server()
    client = EsriClient(fake)
    store = request_store_of({"url": LEDGER, "history": HISTORY}, [], 0)
    rows = [_row("R1", "2026-11-05", "AAAA"), _row("R2", "2026-11-11", "BBBB"), _row("R3", "2026-11-11", "CCCC", system="OTHER"),
            _row("R4", "2026-11-12", ""), _row("R5", "2026-11-11", "DDDD", result="見送り")]
    assert add_history(client, store, None, rows) == 5
    found = load_linked(client, store, None, "SYS", *link_window("2026-11-12", 3))
    assert link_window("2026-11-12", 3) == ("2026-11-09", "2026-11-13")
    # Only that system's rows handed out in the window, with an id.
    assert [(r.request_id, r.link_id) for r in found] == [("R2", "BBBB")]
    # A table without LINK_ID (made before it): nothing to look up, and no failure.
    old = _server(with_link=False)
    assert load_linked(EsriClient(old), store, None, "SYS", "2026-11-09", "2026-11-13") == []


# --- Matching a result file ------------------------------------------------------------------

def _spec(columns, **kw):
    problems: list = []
    spec = result_file_of({"format": "csv", "columns": columns, **kw}, "r", problems)
    assert problems == []
    return spec


LINK_SPEC = [{"key": "REF", "target": "linkId", "pattern": r"^SW-(\w+)$"}, {"key": "STATUS", "target": "result"},
             {"key": "REQ", "target": "id"}]


def _answers(text: str, spec):
    answers, problems = read_answers(text.encode("utf-8"), spec)
    assert problems == []
    return answers


def test_a_file_may_name_its_rows_by_the_id_alone():
    spec = _spec([{"key": "REF", "target": "linkId"}], linkDays=3)
    assert spec.link_days == 3
    [a] = _answers("REF\n7k3qx9m2pa4t\n", spec)
    assert a.keys == {LINK_ID: "7k3qx9m2pa4t"} and a.key == "7k3qx9m2pa4t"
    problems: list = []
    result_file_of({"format": "csv", "columns": [{"key": "REF", "target": "linkId"}], "linkDays": 99}, "r", problems)
    assert problems == ["r.linkDays は 0 から 60 の整数（日）で指定"]


def test_the_id_answers_its_very_row_of_the_day():
    spec = _spec(LINK_SPEC)
    day = [_row("R1", "2026-11-12", "AAAA1111BBBB", object_id=1), _row("R2", "2026-11-12", "CCCC2222DDDD", object_id=2)]
    # Case does not matter; the file's request column names another request: the id's row wins, and it is told.
    answers = _answers("REF,STATUS,REQ\nSW-aaaa1111bbbb,OK,R2\nSW-CCCC2222DDDD,NG,\n", spec)
    reading = match_answers(answers, day, [ImagingRequest("R1", name="R1"), ImagingRequest("R2", name="R2")], "SYS", "2026-11-12",
                            spec, "res.csv")
    assert [(m.request.id, [r.object_id for r in m.rows], m.answer.answer) for m in reading.matches] == [
        ("R1", [1], "採用"), ("R2", [2], "不採用")]
    assert reading.missing == []
    assert reading.problems == ["2 行目: 外部連携ID aaaa1111bbbb は「R1」の行（依頼の番号の「R2」でなく ID の行とした）"]


def test_an_id_of_a_day_before_answers_that_days_row():
    # The system planned on the 12th a request handed out for the 10th: its answer goes to the 10th's row.
    spec = _spec(LINK_SPEC)
    day = [_row("R1", "2026-11-12", "TODAY0000000", object_id=1)]
    nearby = [_row("R9", "2026-11-10", "EARLIER00000", object_id=9), _row("R9", "2026-11-01", "TOOOLD000000", object_id=8)]
    answers = _answers("REF,STATUS,REQ\nSW-TODAY0000000,OK,\nSW-EARLIER00000,OK,\nSW-TOOOLD000000,OK,\n", spec)
    reading = match_answers(answers, day, [], "SYS", "2026-11-12", spec, "res.csv", nearby=nearby)
    assert [(m.request.id if m.request else None, m.day, [r.object_id for r in m.rows]) for m in reading.matches] == [
        ("R1", "2026-11-12", [1]), ("R9", "2026-11-10", [9]), (None, "2026-11-12", [])]
    # The one out of the window (7 days) is not ours: listed 採用, so 要求外.
    assert reading.matches[2].unrequested
    assert reading.problems == ["外部連携IDで 2026-11-10 の出力の行に照合した 1 行（その日の採否として記録）",
                                "外部連携IDが 2026-11-05〜2026-11-13 の出力履歴にない 1 行（依頼の番号もなし）"]
    changes = dict(answer_changes(reading))
    assert changes[9]["adoption"] == "採用" and changes[9]["adoptionSource"] == "res.csv" and 8 not in changes


def test_the_nearest_day_wins_when_an_id_repeats():
    rows = [_row("A", "2026-11-05", "X"), _row("B", "2026-11-11", "X"), _row("C", "2026-11-13", "X"), _row("D", "2026-11-12", "Y")]
    assert [r.request_id for r in nearest_linked(rows[:3], "2026-11-12", 7)] == ["B"]
    assert [r.request_id for r in nearest_linked([rows[0], rows[2]], "2026-11-12", 7)] == ["C"]
    assert nearest_linked([rows[0]], "2026-11-12", 3) == []
    # The day read comes first.
    assert [r.request_id for r in nearest_linked(rows, "2026-11-11", 7)] == ["B"]
    # Two requests of one day with one id: the file's request key chooses, else the id is not used.
    spec = _spec(LINK_SPEC)
    day = [_row("R1", "2026-11-12", "SAME", object_id=1), _row("R2", "2026-11-12", "SAME", object_id=2)]
    reading = match_answers(_answers("REF,STATUS,REQ\nSW-SAME,OK,R2\n", spec), day, [ImagingRequest("R1"), ImagingRequest("R2")], "SYS",
                            "2026-11-12", spec, "f")
    assert [r.object_id for r in reading.matches[0].rows] == [2] and reading.problems == []
    reading = match_answers(_answers("REF,STATUS,REQ\nSW-SAME,OK,\n", spec), day, [ImagingRequest("R1")], "SYS", "2026-11-12", spec, "f")
    assert reading.matches[0].unrequested
    assert reading.problems[0] == "2 行目: 外部連携ID SAME が 2026-11-12 の複数の依頼にあるため依頼の番号で照合"


def test_an_id_not_found_falls_back_to_the_request_key():
    spec = _spec(LINK_SPEC)
    day = [_row("R1", "2026-11-12", "", object_id=1)]  # written before LINK_ID
    reading = match_answers(_answers("REF,STATUS,REQ\nSW-UNKNOWN,OK,R1\n", spec), day, [ImagingRequest("R1")], "SYS",
                            "2026-11-12", spec, "f")
    assert [(m.request.id, [r.object_id for r in m.rows]) for m in reading.matches] == [("R1", [1])] and reading.problems == []


def test_the_id_answers_the_requests_put_into_its_shot_and_only_its_time():
    spec = _spec(LINK_SPEC)
    day = [_row("R1", "2026-11-12", "LEAD", object_id=1, shot_id="RUN-1"),
           _row("R2", "2026-11-12", "MEMBER", object_id=2, shot_id="RUN-1", merged_into="R1"),
           _row("R3", "2026-11-12", "T1", object_id=3, shot_id="RUN-2", occurrence=1),
           _row("R3", "2026-11-12", "T2", object_id=4, shot_id="RUN-3", occurrence=2)]
    reading = match_answers(_answers("REF,STATUS,REQ\nSW-LEAD,OK,\nSW-T2,NG,\n", spec), day, [], "SYS", "2026-11-12", spec, "f")
    assert [(m.request.id, m.time, [r.object_id for r in m.rows]) for m in reading.matches] == [("R1", 1, [1, 2]), ("R3", 2, [4])]
    # The first time is not in the file (不明 as the file states 採否).
    assert [r.object_id for r in reading.missing] == [3]


def test_ids_of_the_order_file_come_back_through_the_result_file():
    # End to end: written, kept, answered by the file built from the order's ids.
    a, b, alone = point(1, BELOW, 300), point(2, along(2.5)), point(4, along(120))
    system = linked_system()
    plan = _plan([a, b, alone], [system])
    files = render(plan.candidates, [system], DAY, T)
    rows = history_of(plan, T, files_of(files), "RUN")
    for i, r in enumerate(rows, 1):
        r.object_id = i
    order = _csv(files[0].text)
    result = "REF,STATUS,REQ\n" + "".join(f"{r['NAME']},{'OK' if r['ID'] == 'R1' else 'NG'},\n" for r in order)
    spec = _spec(LINK_SPEC)
    reading = match_answers(_answers(result, spec), rows, [], "SYS", DAY.isoformat(), spec, "back.csv")
    answered = {r.request_id: m.answer.answer for m in reading.matches for r in m.rows}
    assert answered == {"R1": "採用", "R2": "採用", "R4": "不採用"} and reading.missing == [] and reading.problems == []
    assert link_window(DAY.isoformat(), 7)[0] == (DAY - timedelta(days=7)).isoformat()
