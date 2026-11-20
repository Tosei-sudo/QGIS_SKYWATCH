"""
A result file read for the days its rows are for, whatever day the 撮像予定
dock shows (tsudo, 2026-11-20: 「撮像計画の、戻りファイルのインポート、別の日の選択でも
正しくインポートしてほしい。また、計画外のやつは、選択している日付じゃなくて、
その撮像予定の日付のところに出してほしい」): each row's day is its 対象日, else
the day of its 撮像予定, else the file's day; 要求外 go on the day of their
撮像予定; what the file leaves out is missing only on the days it answers for.
"""
from __future__ import annotations

from datetime import datetime

from mikage.core.esri import EsriClient
from mikage.core.request.adoption import (
    NOT_HANDED, NOT_IN_FILE, answer_changes, match_answers, own_day, planned_day, read_window, unrequested_rows,
)
from mikage.core.request.model import JST, ImagingRequest
from mikage.core.request.result_file import ADOPTED, REJECTED, WAITING, read_answers, result_file_of
from mikage.core.request.store import MANUAL, UNREQUESTED, HistoryRow, add_history, load_system_rows
from mikage.core.time_zone import parse_time_zone
from mikage.tests.test_request_adoption import server, store

T = datetime(2026, 11, 19, 9, tzinfo=JST).timestamp() * 1000
SHOWN, NEXT, AFTER = "2026-11-20", "2026-11-21", "2026-11-22"
REQUESTS = [ImagingRequest("R1", name="東京湾"), ImagingRequest("R2", name="大阪湾"), ImagingRequest("R3", name="横浜"),
            ImagingRequest("R5", name="名古屋"), ImagingRequest("R6", name="博多")]


def at(day: str, hm: str) -> float:
    return datetime.fromisoformat(f"{day}T{hm}").replace(tzinfo=JST).timestamp() * 1000


def handed(rid: str, day: str, oid: int, system: str = "SYS", **kw) -> HistoryRow:
    return HistoryRow("RUN", T, day, rid, system, result="採用", adoption="不明", name=rid, object_id=oid, **kw)


def spec_of(columns, **kw):
    problems: list = []
    spec = result_file_of({"format": "csv", "columns": columns, **kw}, "r", problems, parse_time_zone("Asia/Tokyo"))
    assert problems == [], problems
    return spec


WITH_RESULT = [{"key": "ID", "target": "id"}, {"key": "ST", "target": "result", "map": {"OK": "採用", "NG": "不採用"}},
               {"key": "PLAN", "target": "plannedAt"}, {"key": "NAME", "target": "name"}]


def read(text: str, spec):
    answers, problems = read_answers(text.encode("utf-8"), spec)
    assert problems == [], problems
    return answers


def by_oid(reading) -> dict:
    return {oid: v["adoption"] for oid, v in answer_changes(reading)}


def test_a_file_of_another_day_answers_that_days_rows():
    # The dock shows the 20th; the file is the system's plan for the 21st (and a routine shot of its own on the 22nd).
    rows = [handed("R1", SHOWN, 1), handed("R2", SHOWN, 2), handed("R1", NEXT, 3), handed("R3", NEXT, 4)]
    spec = spec_of(WITH_RESULT)
    answers = read(f"ID,ST,PLAN,NAME\nR1,OK,{NEXT} 10:30,\nR3,NG,,\nX-1,OK,{AFTER} 09:00,定常 東京\n", spec)
    assert [own_day(a) for a in answers] == [NEXT, "", AFTER] and planned_day(answers[2]) == AFTER
    reading = match_answers(answers, rows, REQUESTS, "SYS", SHOWN, spec, "a.csv")
    # The file's day: the day its rows say most (one each: the nearer to the day shown); R3 says none, so it is the 21st's.
    assert (reading.day, reading.shown, reading.days) == (NEXT, SHOWN, [NEXT])
    assert [(m.request.id if m.request else m.answer.key, m.day, [r.object_id for r in m.rows]) for m in reading.matches] == [
        ("R1", NEXT, [3]), ("R3", NEXT, [4]), ("X-1", AFTER, [])]
    # The 20th's rows are left as they are (before: answered by the 21st's file, R2 missing and X-1 put on the 20th).
    assert by_oid(reading) == {3: ADOPTED, 4: REJECTED} and reading.missing == [] and reading.problems == []
    assert reading.placed() == [NEXT, AFTER] and reading.touched() == [NEXT, AFTER]
    [own] = unrequested_rows(reading, T, "RUN2")
    assert (own.day, own.name, own.result, own.planned_at) == (AFTER, "定常 東京", UNREQUESTED, at(AFTER, "09:00"))
    assert reading.tally(NEXT) == {ADOPTED: 1, REJECTED: 1, WAITING: 0, UNREQUESTED: 0, NOT_IN_FILE: 0}
    assert reading.tally(AFTER)[UNREQUESTED] == 1


def test_what_a_file_leaves_out_is_missing_on_its_days_only():
    # No result column (listed = 採用, left out = 不採用) and a 対象日 column: two days in one file.
    rows = [handed("R2", SHOWN, 1), handed("R1", NEXT, 2), handed("R3", NEXT, 3), handed("R5", AFTER, 4), handed("R6", AFTER, 5)]
    spec = spec_of([{"key": "DAY", "target": "day", "format": "%Y%m%d"}, {"key": "ID", "target": "id"}])
    answers = read("DAY,ID\n20261121,R1\n20261122,R5\n", spec)
    reading = match_answers(answers, rows, REQUESTS, "SYS", SHOWN, spec, "week.csv")
    assert reading.days == [NEXT, AFTER] and reading.missing_as == REJECTED
    assert [r.object_id for r in reading.missing] == [3, 5]
    assert by_oid(reading) == {2: ADOPTED, 4: ADOPTED, 3: REJECTED, 5: REJECTED}
    assert dict(answer_changes(reading))[3]["adoptionReason"] == NOT_IN_FILE
    assert reading.tally(NEXT)[NOT_IN_FILE] == 1 and reading.tally(SHOWN) == {ADOPTED: 0, REJECTED: 0, WAITING: 0, UNREQUESTED: 0,
                                                                                NOT_IN_FILE: 0}
    # Before 2026-11-20 the rows of days other than the one shown were skipped ("対象日が … 以外の n 行を読み飛ばし").
    assert reading.problems == []


def test_a_file_without_dates_is_for_the_day_its_requests_were_handed_out():
    rows = [handed("R1", SHOWN, 1), handed("R2", SHOWN, 2), handed("R1", NEXT, 3), handed("R3", NEXT, 4)]
    spec = spec_of(WITH_RESULT[:2])
    # R3 was handed out on the 21st, R1 on both days: the file is the 21st's.
    reading = match_answers(read("ID,ST\nR3,OK\nR1,NG\n", spec), rows, REQUESTS, "SYS", SHOWN, spec, "a.csv")
    assert reading.day == NEXT and by_oid(reading) == {4: ADOPTED, 3: REJECTED} and reading.missing == []
    # Naming none of them: the day shown, as before.
    reading = match_answers(read("ID,ST\nX-9,OK\n", spec), rows, REQUESTS, "SYS", SHOWN, spec, "a.csv")
    assert reading.day == SHOWN and [(m.day, m.unrequested) for m in reading.matches] == [(SHOWN, True)]
    # A tie: the day shown first.
    reading = match_answers(read("ID,ST\nR1,OK\n", spec), rows, REQUESTS, "SYS", SHOWN, spec, "a.csv")
    assert reading.day == SHOWN and by_oid(reading) == {1: ADOPTED}
    # Requests handed out more than linkDays from the day shown do not decide it.
    far = [handed("R3", "2026-12-20", 9)]
    reading = match_answers(read("ID,ST\nR3,OK\n", spec), far, REQUESTS, "SYS", SHOWN, spec, "a.csv")
    assert reading.day == SHOWN and reading.matches[0].unrequested and reading.matches[0].day == SHOWN


def test_rows_without_a_date_go_with_the_dated_ones():
    rows = [handed("R1", NEXT, 1), handed("R3", NEXT, 2), handed("R3", SHOWN, 3)]
    spec = spec_of(WITH_RESULT)
    reading = match_answers(read(f"ID,ST,PLAN,NAME\nR1,OK,{NEXT} 08:00,\nR3,NG,,\nX-2,OK,,定常\n", spec), rows, REQUESTS, "SYS",
                            SHOWN, spec, "a.csv")
    assert [(m.answer.key, m.day) for m in reading.matches] == [("R1", NEXT), ("R3", NEXT), ("X-2", NEXT)]
    assert by_oid(reading) == {1: ADOPTED, 2: REJECTED}


def test_a_request_planned_off_its_day_answers_the_files_days_row():
    rows = [handed("R1", NEXT, 1), handed("R3", NEXT, 2)]
    spec = spec_of(WITH_RESULT)
    # R1 handed out for the 21st, planned by the system just after midnight: the 21st's row, with that time.
    reading = match_answers(read(f"ID,ST,PLAN,NAME\nR1,OK,{AFTER} 00:30,\nR3,OK,{NEXT} 23:00,\n", spec), rows, REQUESTS, "SYS",
                            SHOWN, spec, "a.csv")
    assert reading.day == NEXT and reading.days == [NEXT] and by_oid(reading) == {1: ADOPTED, 2: ADOPTED}
    assert dict(answer_changes(reading))[1]["plannedAt"] == at(AFTER, "00:30")
    assert reading.problems == ["ほかの日の 1 行（その日に出力していない依頼）を 2026-11-21 の出力の行に照合（2026-11-21 の採否として記録）"]
    # The file answering that row too, the other one is a plan of the system's own on its day (要求外).
    reading = match_answers(read(f"ID,ST,PLAN,NAME\nR1,OK,{AFTER} 10:00,\nR1,OK,{NEXT} 10:00,\nR3,OK,{NEXT} 11:00,\n", spec), rows,
                            REQUESTS, "SYS", SHOWN, spec, "a.csv")
    assert by_oid(reading) == {1: ADOPTED, 2: ADOPTED}
    [extra] = [m for m in reading.matches if m.unrequested]
    assert (extra.request.id, extra.day) == ("R1", AFTER)
    assert unrequested_rows(reading, T)[0].reason == NOT_HANDED and reading.days == [NEXT]


def test_the_ids_of_a_file_are_looked_for_round_each_rows_day():
    spec = spec_of([{"key": "REF", "target": "linkId"}, {"key": "PLAN", "target": "plannedAt"}])
    rows = [handed("R1", NEXT, 1, link_id="AAAA"), handed("R3", "2026-11-12", 2, link_id="BBBB")]
    # The 23rd's row names the 21st's id: that day's row (planned late); the 12th is more than 7 days before the 23rd.
    reading = match_answers(read("REF,PLAN\nAAAA,2026-11-23 10:00\nBBBB,2026-11-23 11:00\n", spec), rows, REQUESTS, "SYS", SHOWN,
                            spec, "a.csv")
    assert [(m.request.id if m.request else None, m.day, [r.object_id for r in m.rows]) for m in reading.matches] == [
        ("R1", NEXT, [1]), (None, "2026-11-23", [])]
    assert "外部連携IDで 2026-11-21 の出力の行に照合した 1 行（その日の採否として記録）" in reading.problems
    # A late answer does not make its row's day one the file answers for.
    assert reading.days == ["2026-11-23"]


def test_reading_again_replaces_the_files_unrequested_rows():
    spec = spec_of(WITH_RESULT)
    rows = [handed("R1", NEXT, 1),
            # From a file before: on the day the file answers for, replaced.
            HistoryRow("OLD", T, NEXT, "", "SYS", result=UNREQUESTED, adoption="採用", external_id="X-0", adoption_source="old.csv",
                       object_id=10),
            # Put on the day shown by a reading before 2026-11-20: the same plan (id and time), replaced wherever it is.
            HistoryRow("OLD", T, SHOWN, "", "SYS", result=UNREQUESTED, adoption="採用", external_id="X-1", planned_at=at(AFTER, "09:00"),
                       adoption_source="a.csv", object_id=11),
            # Another plan on another day, and one added by hand: kept.
            HistoryRow("OLD", T, AFTER, "", "SYS", result=UNREQUESTED, adoption="採用", external_id="Z-1", planned_at=at(AFTER, "12:00"),
                       adoption_source="b.csv", object_id=12),
            HistoryRow("OLD", T, NEXT, "", "SYS", result=UNREQUESTED, adoption="採用", adoption_source=MANUAL, object_id=13)]
    reading = match_answers(read(f"ID,ST,PLAN,NAME\nR1,OK,{NEXT} 10:30,\nX-1,OK,{AFTER} 09:00,定常\n", spec), rows, REQUESTS, "SYS",
                            SHOWN, spec, "a.csv")
    assert sorted(r.object_id for r in reading.replaced) == [10, 11]
    [own] = unrequested_rows(reading, T)
    assert (own.day, own.external_id) == (AFTER, "X-1")


def test_the_window_read_round_the_files_days():
    spec = spec_of(WITH_RESULT)
    answers = read(f"ID,ST,PLAN,NAME\nR1,OK,{NEXT} 10:30,\nX-1,OK,2026-11-25 09:00,\nR3,NG,,\n", spec)
    assert read_window(answers, SHOWN, 7) == ("2026-11-13", "2026-12-02")
    # Without dates: round the day shown; linkDays 0 still takes the day after.
    assert read_window(read("ID,ST,PLAN,NAME\nR1,OK,,\n", spec), SHOWN, 7) == ("2026-11-13", "2026-11-27")
    assert read_window([], SHOWN, 0) == (SHOWN, NEXT)
    # Another system's rows do not widen it.
    other = spec_of(WITH_RESULT + [{"key": "SYS", "target": "system"}])
    assert read_window(read("ID,ST,PLAN,NAME,SYS\nR1,OK,2026-12-25 10:00,,B\n", other), SHOWN, 1, "SYS") == ("2026-11-19", "2026-11-21")


def test_the_systems_rows_of_the_days_are_read_without_their_areas():
    fake = server()
    client, s = EsriClient(fake), store()
    rings = [[[139.0, 35.0], [139.1, 35.0], [139.1, 35.1], [139.0, 35.0]]]
    rows = [handed("R1", SHOWN, None, "SYS-A", rings=rings), handed("R2", NEXT, None, "SYS-A"), handed("R3", NEXT, None, "SYS-B"),
            HistoryRow("RUN", T, NEXT, "R5", "SYS-A", result="見送り"),
            HistoryRow("RUN", T, AFTER, "", "SYS-A", result=UNREQUESTED, adoption="採用", external_id="X-1"),
            handed("R6", "2026-11-30", None, "SYS-A")]
    assert add_history(client, s, None, rows) == len(rows)
    got = load_system_rows(client, s, None, "SYS-A", SHOWN, AFTER)
    assert [(r.day, r.request_id, r.result) for r in got] == [(SHOWN, "R1", "採用"), (NEXT, "R2", "採用"), (AFTER, "", UNREQUESTED)]
    assert all(r.object_id is not None and r.rings == [] for r in got)
