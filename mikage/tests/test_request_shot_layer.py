"""
The shots kept apart from the requests (tsudo, 2026-11-11: 「マージした結果、
どの範囲になったかも見たい。他方、要求にどう呼応したかも知りたい」): one shot
row a row of the files (TBL_REQUEST_SHOT, the outline of the scenes), the
history one row per request with its own area naming its shot (SHOT_ID).
"""
from __future__ import annotations

from mikage.core.request.export import files_of, render
from mikage.core.request.model import in_rings
from mikage.core.request.passes import day_passes
from mikage.core.request.shot_view import companions, shot_text, shot_tip
from mikage.core.request.store import (
    DEFAULT_SHOT_FIELDS, HistoryRow, request_store_of, shot_attributes, shot_row_of, shots_replaced_where,
)
from mikage.core.request.workflow import history_of, new_run_id, plan_day, shots_of
from mikage.tests.test_plan import BELOW, T
from mikage.tests.test_request_passes import DAY, SAT, _middle, along, point, system


def _plan(reqs, systems=None):
    systems = systems or [system()]
    return plan_day(reqs, DAY, systems, [SAT], {}, passes=day_passes(reqs, DAY, [SAT])), systems


def test_one_shot_a_row_of_the_file_and_each_request_names_it():
    a, b, alone = point(1, BELOW, 300), point(2, along(2.5)), point(4, along(120))
    plan, systems = _plan([a, b, alone])
    written = files_of(render(plan.candidates, systems, DAY, T))
    run_id = new_run_id()
    shots = shots_of(plan, T, written, run_id)
    assert [(s.request_id, s.request_ids, s.count) for s in shots] == [("R1", ["R1", "R2"], 2), ("R4", ["R4"], 1)]
    merged = shots[0]
    assert merged.shot_id == f"{run_id}-1" and merged.system == "SYS" and merged.satellite == "ISS" and merged.scenes == 2
    assert merged.names == ["点1", "点2"] and merged.file == written["SYS"] and merged.shots == 1 and merged.coverage is None
    assert merged.orbit_dir in ("昇交", "降交") and merged.look_side in ("右", "左") and abs(merged.pass_time - T) < 60000
    # The outline of the scenes takes both requests (the area the file's row is written with).
    [ring] = merged.rings
    assert all(in_rings([ring], *_middle(r)) for r in (a, b))
    rows = {r.request_id: r for r in history_of(plan, T, written, run_id)}
    assert rows["R1"].shot_id == rows["R2"].shot_id == merged.shot_id and rows["R4"].shot_id == shots[1].shot_id
    # Each history row keeps the request's own area.
    assert rows["R2"].rings == [[list(p[:2]) for p in b.rings[0]]]


def test_a_shot_also_without_the_orbits_but_none_without_a_file():
    # Fitted by counting (tsudo, 2026-11-11: 「個の画像に関して管理する情報はショットのほうがいい」): a shot a row of the
    # file all the same, with the request's area and no pass; each request names its own.
    a, b = point(1, BELOW, 300), point(2, along(2.5))
    plan, systems = _plan([a, b], [system(orbit=False)])
    written = files_of(render(plan.candidates, systems, DAY, T))
    shots = shots_of(plan, T, written, "X")
    assert [(s.shot_id, s.request_ids, s.pass_time, s.orbit_dir, s.shots, s.captured) for s in shots] == [
        ("X-1", ["R1"], None, "", 1, "未判定"), ("X-2", ["R2"], None, "", 1, "未判定")]
    assert shots[0].rings == [[list(p[:2]) for p in a.rings[0]]] and shots[0].satellite == "ISS"
    assert {r.request_id: r.shot_id for r in history_of(plan, T, written, "X")} == {"R1": "X-1", "R2": "X-2"}
    # A system whose file was not written has no shots.
    plan, systems = _plan([point(1, BELOW, 300)])
    assert shots_of(plan, T, {}, "X") == [] and history_of(plan, T, {}, "X")[0].shot_id == ""


def test_a_shot_row_reads_back_as_written():
    a, b = point(1, BELOW, 300), point(2, along(2.5))
    plan, systems = _plan([a, b])
    [shot] = shots_of(plan, T, files_of(render(plan.candidates, systems, DAY, T)), "RUN")
    attributes = shot_attributes(shot, DEFAULT_SHOT_FIELDS)
    assert attributes["REQ_IDS"] == "R1,R2" and attributes["REQ_NAMES"] == "点1、点2" and attributes["REQ_COUNT"] == 2
    assert attributes["TARGET_DAY"] == DAY.isoformat() and attributes["SHOT_ID"] == "RUN-1" and attributes["COVERAGE"] is None
    back = shot_row_of({**attributes, "OBJECTID": 7}, {"rings": shot.rings}, DEFAULT_SHOT_FIELDS, "OBJECTID")
    assert (back.shot_id, back.request_ids, back.names, back.count, back.scenes, back.object_id) == ("RUN-1", ["R1", "R2"], ["点1", "点2"], 2, 2, 7)
    assert back.pass_time == shot.pass_time and back.rings == shot.rings


def test_writing_again_replaces_the_days_shots_but_those_of_rows_kept():
    store = request_store_of({"url": "https://x/FeatureServer/0", "history": "https://x/FeatureServer/1",
                              "shots": "https://x/FeatureServer/22"}, [], 0)
    assert store.shots == "https://x/FeatureServer/22"
    assert shots_replaced_where(store, "2026-11-11") == "TARGET_DAY = '2026-11-11'"
    assert shots_replaced_where(store, "2026-11-11", "SYS", ["A-1", "", "A-1"]) == (
        "TARGET_DAY = '2026-11-11' AND SYSTEM = 'SYS' AND SHOT_ID NOT IN ('A-1')")
    problems = []
    assert request_store_of({"url": "https://x/FeatureServer/0", "shots": "nowhere"}, problems, 0).shots is None
    assert problems and "shots" in problems[0]


def _row(i, **kw):
    return HistoryRow(run_id="X", run_date=T, day="2026-11-11", request_id=f"R{i}", name=f"点{i}", system="SYS", **kw)


def test_the_list_tells_what_became_of_each_request():
    from mikage.core.request.store import ShotRow
    lead, rider, alone, held = (_row(1, shot_id="X-1"), _row(2, shot_id="X-1", merged_into="R1"), _row(4, shot_id="X-2"),
                                _row(5, result="見送り"))
    rows = [lead, rider, alone, held]
    shots = {"X-1": ShotRow("X-1", "X", T, "2026-11-11", system="SYS", satellite="ISS", pass_time=T, off_nadir=12.34, incidence=13.6,
                            orbit_dir="降交", look_side="右", scenes=2, request_id="R1", request_ids=["R1", "R2"], names=["点1", "点2"]),
             "X-2": ShotRow("X-2", "X", T, "2026-11-11", pass_time=T + 120000, scenes=1, request_id="R4", request_ids=["R4"])}
    assert companions(rider, rows) == [lead, rider] and companions(alone, rows) == [] and companions(held, rows) == []
    assert shot_text(lead, rows, shots) == "22:00 まとめ 2 件（点2 と）"
    assert shot_text(rider, rows, shots) == "22:00 同乗（「点1」の撮像）"
    assert shot_text(alone, rows, shots) == "22:02 単独"
    assert shot_text(held, rows, shots) == ""
    tip = shot_tip(shots["X-1"])
    assert tip.startswith("22:00（日本時間）・ISS・オフナディア角 12.3°・入射角 13.6°・降交・右観測・連続 2 シーン")
    assert tip.endswith("1 回の撮像に 2 件: 点1・点2")
    # Without the shot layer (or rows written before it), MERGED_INTO alone tells the same.
    plain = [_row(1), _row(2, merged_into="R1"), _row(3)]
    assert shot_text(plain[0], plain, {}) == "まとめ 2 件（点2 と）"
    assert shot_text(plain[1], plain, {}) == "同乗（「点1」の撮像）"
    assert shot_text(plain[2], plain, {}) == ""
