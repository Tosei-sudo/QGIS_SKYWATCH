"""
チュートリアル's core parts: the progress kept in the practice folder
(core/training/progress.py), and the answer a system would send back made for
「返ってきた採否を入れる」 (core/request/practice_answer.py), read back as the
plugin reads the system's file.
"""
import json
from datetime import datetime

from mikage.core.request.api import HistoryRow, answer_bytes, handed_out, read_answers
from mikage.core.request.result_file import result_file_of
from mikage.core.training.api import kept, read_progress, write_progress


def test_progress_is_kept_per_course(tmp_path):
    path = str(tmp_path / "training" / "progress.json")
    assert read_progress(path) == {}
    at = datetime(2026, 11, 22, 20, 15)
    progress = kept({}, "obs", 4, False, at)
    progress = kept(progress, "plan", 8, True, at)
    write_progress(path, progress)
    back = read_progress(path)
    assert back == {"obs": {"step": 4, "done": False, "at": "2026-11-22T20:15:00"},
                    "plan": {"step": 0, "done": True, "at": "2026-11-22T20:15:00"}}
    # A course done stays done when it is gone through again.
    assert kept(back, "plan", 2, False, at)["plan"] == {"step": 2, "done": True, "at": "2026-11-22T20:15:00"}
    # A file not readable, or of another shape: nothing kept.
    (tmp_path / "bad.json").write_text("[1, 2", encoding="utf-8")
    assert read_progress(str(tmp_path / "bad.json")) == {}
    (tmp_path / "odd.json").write_text(json.dumps({"obs": {"step": -3, "done": "yes"}, "x": 1}), encoding="utf-8")
    assert read_progress(str(tmp_path / "odd.json")) == {"obs": {"step": 0, "done": False, "at": ""}}


def rows(system: str = "SYS") -> list:
    return [HistoryRow(run_id="r", run_date=0, day="2026-11-23", request_id=f"REQ-{i}", system=system, satellite="OPT-X1",
                       name=f"依頼{i}", adoption="不明") for i in range(3)] + [
        HistoryRow(run_id="r", run_date=0, day="2026-11-23", request_id="REQ-9", system=system, result="見送り"),
        HistoryRow(run_id="r", run_date=0, day="2026-11-23", request_id="REQ-8", system="OTHER")]


def test_the_answer_is_made_in_the_systems_form_and_read_back():
    problems = []
    # The practice's optical system's form (core/training/sample.py RESULT_FILE).
    spec = result_file_of({"format": "csv", "encoding": "cp932", "columns": [
        {"key": "依頼ID", "target": "id"}, {"key": "判定", "target": "result", "map": {"○": "採用", "×": "不採用"}},
        {"key": "理由", "target": "reason"}, {"key": "撮像予定", "target": "plannedAt", "format": "%Y/%m/%d %H:%M"}]}, "r", problems)
    assert problems == []
    handed = handed_out(rows(), "SYS")
    assert [r.request_id for r in handed] == ["REQ-0", "REQ-1", "REQ-2"]
    data = answer_bytes(spec, handed)
    assert data.decode("cp932").splitlines()[:2] == ["依頼ID,判定,理由,撮像予定", "REQ-0,○,,2026/11/23 10:30"]
    answers, problems = read_answers(data, spec)
    # All taken but the last (left for the answer heard by phone).
    assert problems == [] and [(a.key, a.answer) for a in answers] == [("REQ-0", "採用"), ("REQ-1", "採用")]
    assert answers[1].planned_at is not None


def test_other_forms_of_the_answer():
    problems = []
    # Without headings, by place; the date and the time in two columns; JSON by dot paths under `records`.
    by_place = result_file_of({"format": "csv", "header": False, "delimiter": "tab", "columns": [
        {"column": 2, "target": "id"}, {"column": 1, "target": "plannedAt", "format": "%Y%m%d %H%M"},
        {"column": 3, "target": "plannedAt"}]}, "r", problems)
    data = answer_bytes(by_place, handed_out(rows(), "SYS"))
    assert data.decode().splitlines()[0] == "20261123\tREQ-0\t1030"
    answers, problems2 = read_answers(data, by_place)
    assert problems2 == [] and [a.key for a in answers] == ["REQ-0", "REQ-1"]
    nested = result_file_of({"format": "json", "records": "result.items", "columns": [
        {"key": "request.id", "target": "id"}, {"key": "state", "target": "result", "map": {"OK": "採用", "NG": "不採用"}}]}, "r", problems)
    data = answer_bytes(nested, handed_out(rows(), "SYS"))
    assert json.loads(data)["result"]["items"][0] == {"request": {"id": "REQ-0"}, "state": "OK"}
    answers, problems3 = read_answers(data, nested)
    assert problems3 == [] and [(a.key, a.answer) for a in answers] == [("REQ-0", "採用"), ("REQ-1", "採用")]
    assert problems == []
    # A form only a JSON Schema describes, or nothing handed out: not made.
    schema = result_file_of({"format": "json", "schema": {"type": "object", "properties": {
        "rows": {"type": "array", "x-target": "records", "items": {"type": "object", "properties": {"id": {"type": "string", "x-target": "id"}}}}}}},
        "r", problems)
    assert answer_bytes(schema, handed_out(rows(), "SYS")) is None
    assert answer_bytes(nested, handed_out(rows(), "NONE")) is None
