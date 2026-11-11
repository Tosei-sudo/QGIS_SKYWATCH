"""
A system's day split into files of at most `maxPerFile` requests (tsudo,
2026-11-11: some systems take only so many in one file): the numbered
names (`{part}`, `{parts}`, or `_1`, `_2` … before the extension), each
file whole, the history naming the file each request is in, and the files
of an earlier writing split otherwise found to be deleted.
"""
from __future__ import annotations

import csv
import io
import json

from skywatch.core.request.daily import HELD, TAKEN
from skywatch.core.request.export import file_name_of, files_of, leftover_files, render, split
from skywatch.core.request.systems import export_systems_of
from skywatch.core.request.workflow import file_of, history_of, not_written, plan_day, written_summary
from skywatch.tests.test_request import DAY, FIRST, H, SATS, request
from skywatch.tests.test_request_export import _system, _taken

COLUMNS = [{"key": "ID", "source": "id"}, {"key": "N", "source": "count"}, {"key": "P", "source": "part"}, {"key": "OF", "source": "parts"}]


def _five(system):
    return _taken([request(f"R-{i}", priority=100 * i) for i in range(1, 6)], system)


def test_max_per_file_in_the_settings():
    problems = []
    out = export_systems_of([
        {"system": "A", "columns": COLUMNS, "maxPerFile": 100},
        {"system": "B", "columns": COLUMNS},
        {"system": "C", "columns": COLUMNS, "maxPerFile": 0},
        {"system": "D", "columns": COLUMNS, "maxPerFile": 2.5},
        {"system": "E", "columns": COLUMNS, "maxPerFile": "50"},
    ], problems)
    assert [s.max_per_file for s in out] == [100, 0, 0, 0, 0]
    assert problems == [f"requestExports[{i}] の maxPerFile は 1 以上の整数で指定" for i in (2, 3, 4)]


def test_csv_split_into_numbered_files():
    system = _system(columns=COLUMNS, maxPerFile=2, limit={"count": 10})
    taken = _five(system)
    assert len(taken) == 5
    files = render(taken, [system], DAY, FIRST)
    assert [w.file_name for w in files] == ["SYS-T_20261108_1.csv", "SYS-T_20261108_2.csv", "SYS-T_20261108_3.csv"]
    assert [(w.count, w.part, w.parts) for w in files] == [(2, 1, 3), (2, 2, 3), (1, 3, 3)]
    assert [w.ids for w in files] == [("R-1", "R-2"), ("R-3", "R-4"), ("R-5",)]
    # Each a whole file: the header, then its rows; `count` is the file's, `part` / `parts` its number.
    rows = [list(csv.reader(io.StringIO(w.text))) for w in files]
    assert all(r[0] == ["ID", "N", "P", "OF"] for r in rows)
    assert rows[0][1:] == [["R-1", "2", "1", "3"], ["R-2", "2", "1", "3"]] and rows[2][1:] == [["R-5", "1", "3", "3"]]
    assert written_summary(files) == "SYS-T 5 件（3 ファイル）"


def test_one_file_keeps_its_plain_name():
    system = _system(columns=COLUMNS, maxPerFile=5, limit={"count": 10})
    files = render(_five(system), [system], DAY, FIRST)
    assert [(w.file_name, w.count, w.part, w.parts) for w in files] == [("SYS-T_20261108.csv", 5, 1, 1)]
    assert written_summary(files) == "SYS-T 5 件"
    # Without a limit, as before.
    assert split([1, 2, 3], 0) == [[1, 2, 3]] and split([1, 2, 3], 2) == [[1, 2], [3]] and split([], 2) == [[]]


def test_part_and_parts_in_the_file_name():
    system = _system(columns=COLUMNS, maxPerFile=2, limit={"count": 10}, fileName="{system}_{date:%Y%m%d}_{part:%02d}of{parts}.{ext}")
    files = render(_five(system), [system], DAY, FIRST)
    assert [w.file_name for w in files] == ["SYS-T_20261108_01of3.csv", "SYS-T_20261108_02of3.csv", "SYS-T_20261108_03of3.csv"]
    # Not split: {part} is 1 (the name is the same every day, numbered or not).
    assert file_name_of(system, DAY, FIRST) == "SYS-T_20261108_01of1.csv"
    plain = _system(columns=COLUMNS, fileName="order.{ext}")
    assert file_name_of(plain, DAY, FIRST, 2, 3) == "order_2.csv" and file_name_of(plain, DAY, FIRST) == "order.csv"
    bare = _system(columns=COLUMNS, fileName="ORDER")
    assert file_name_of(bare, DAY, FIRST, 2, 3) == "ORDER_2"


def test_json_split_each_file_whole_and_checked():
    schema = {"type": "object", "required": ["file", "tasks"], "properties": {
        "file": {"type": "object", "properties": {"no": {"type": "integer", "x-source": "part"}, "of": {"type": "integer", "x-source": "parts"},
                                                  "count": {"type": "integer", "x-source": "count"}}},
        "tasks": {"type": "array", "x-source": "requests", "maxItems": 2, "items": {"type": "object", "properties": {"id": {"type": "string", "x-source": "id"}}}}}}
    system = _system(format="json", schema=schema, maxPerFile=2, limit={"count": 10})
    files = render(_five(system), [system], DAY, FIRST)
    assert [w.file_name for w in files] == ["SYS-T_20261108_1.json", "SYS-T_20261108_2.json", "SYS-T_20261108_3.json"]
    got = [json.loads(w.text) for w in files]
    assert got[1] == {"file": {"no": 2, "of": 3, "count": 2}, "tasks": [{"id": "R-3"}, {"id": "R-4"}]}
    assert all(w.errors == [] for w in files)
    # The system's own maxItems would stop one file of all five.
    one = _system(format="json", schema=schema, limit={"count": 10})
    whole = render(_five(one), [one], DAY, FIRST)
    assert [w.file_name for w in whole] == ["SYS-T_20261108.json"] and whole[0].errors


def test_the_history_names_the_file_of_each_request():
    system = _system(columns=COLUMNS, maxPerFile=2, limit={"count": 10})
    plan = plan_day([request(f"R-{i}", priority=100 * i) for i in range(1, 6)], DAY, [system], SATS, {})
    files = render(plan.candidates, plan.systems, DAY, FIRST)
    assert len(files) == 3
    names = files_of(files)
    assert names == {"SYS-T": {"R-1": "SYS-T_20261108_1.csv", "R-2": "SYS-T_20261108_1.csv", "R-3": "SYS-T_20261108_2.csv",
                               "R-4": "SYS-T_20261108_2.csv", "R-5": "SYS-T_20261108_3.csv"}}
    rows = history_of(plan, FIRST + H, names, "RUN")
    assert {r.request_id: (r.result, r.file) for r in rows} == {f"R-{i}": (TAKEN, f"SYS-T_20261108_{(i + 1) // 2}.csv") for i in range(1, 6)}
    # The last file not written (a write error): its request is not handed out, the others are.
    partial = files_of(files[:2])
    assert file_of(partial, "SYS-T", "R-5") is None and file_of(partial, "SYS-T", "R-1") == "SYS-T_20261108_1.csv"
    assert not_written(plan, partial) == {"R-5"}
    rows = {r.request_id: r for r in history_of(plan, FIRST + H, partial, "RUN", failures={"SYS-T": "アクセス拒否"})}
    assert (rows["R-5"].result, rows["R-5"].reason, rows["R-5"].file) == (HELD, "書き出し失敗（アクセス拒否）", "")
    assert (rows["R-4"].result, rows["R-4"].file) == (TAKEN, "SYS-T_20261108_2.csv")
    # One file: the system's name as before.
    one = _system(columns=COLUMNS, limit={"count": 10})
    assert files_of(render(_five(one), [one], DAY, FIRST)) == {"SYS-T": "SYS-T_20261108.csv"}
    assert file_of({"SYS-T": "a.csv"}, "SYS-T", "R-1") == "a.csv" and file_of({}, "SYS-T", "R-1") is None


def test_files_left_from_an_earlier_writing_split_otherwise():
    system = _system(columns=COLUMNS, maxPerFile=2)
    folder = ["SYS-T_20261108.csv", "SYS-T_20261108_1.csv", "SYS-T_20261108_2.csv", "SYS-T_20261108_3.csv", "sys-t_20261108_4.CSV",
              "SYS-T_20261107_3.csv", "SYS-T_20261108_note.csv", "SYS-T_20261108_2.json", "SYS-U_20261108_3.csv", "SYS-T_20261108_2.csv.bak"]
    # Written in two: the plain name and the 3rd, 4th files are left over (any case); other days, systems and names are not.
    mine = ["SYS-T_20261108_1.csv", "SYS-T_20261108_2.csv"]
    assert leftover_files(system, DAY, FIRST, mine, folder) == ["SYS-T_20261108.csv", "SYS-T_20261108_3.csv", "sys-t_20261108_4.CSV"]
    # Written in one (plain name): every numbered one is left over.
    assert leftover_files(system, DAY, FIRST, ["SYS-T_20261108.csv"], folder) == [
        "SYS-T_20261108_1.csv", "SYS-T_20261108_2.csv", "SYS-T_20261108_3.csv", "sys-t_20261108_4.CSV"]
    # With {part} / {parts} in the name: any number there.
    named = _system(columns=COLUMNS, maxPerFile=2, fileName="{system}_{date:%Y%m%d}_{part:%02d}of{parts}.{ext}")
    folder = ["SYS-T_20261108_01of3.csv", "SYS-T_20261108_02of3.csv", "SYS-T_20261108_03of3.csv", "SYS-T_20261108_01of2.csv",
              "SYS-T_20261108_02of2.csv", "SYS-T_20261108_01.csv"]
    assert leftover_files(named, DAY, FIRST, ["SYS-T_20261108_01of2.csv", "SYS-T_20261108_02of2.csv"], folder) == [
        "SYS-T_20261108_01of3.csv", "SYS-T_20261108_02of3.csv", "SYS-T_20261108_03of3.csv"]
    # Characters a file name cannot hold are written as _ in both.
    odd = _system(columns=COLUMNS, maxPerFile=2, fileName="{system}:{date:%Y/%m/%d}.{ext}")
    assert file_name_of(odd, DAY, FIRST, 1, 2) == "SYS-T_2026_11_08_1.csv"
    assert leftover_files(odd, DAY, FIRST, ["SYS-T_2026_11_08_1.csv"], ["SYS-T_2026_11_08_1.csv", "SYS-T_2026_11_08_2.csv"]) == [
        "SYS-T_2026_11_08_2.csv"]
