"""
衛星の使用時間 (tsudo, 2026-11-14: 「システムによっては、軌道の使用時間で上限
管理してるらしい」「上限は、衛星ごとで、準備なども入る」「この衛星をいつから
いつまで使ったって感じで記録する」「そのパス(半周)で最初に取るところから最後
のところまでの差分秒数」「準備秒は足すだけでよい。定数はシステムのコンフィグ
に」「上限は1パスあたり」「1日の上限も欲しいな」): a pass uses its satellite
from its first shot's start less the setup seconds to its last shot's end;
the systems' `useTime.perPass` / `perDay` hold back what would go past them.
"""
from __future__ import annotations

import itertools
import math
from datetime import date

from mikage.core.esri import EsriClient
from mikage.core.plan.orbit import Orbit
from mikage.core.request.daily import TAKEN, allocate, gather, pass_text
from mikage.core.request.export import SOURCES, TIME_SOURCES, Context, raw_value
from mikage.core.request.passes import day_passes
from mikage.core.request.simulate import SUMMARY, simulate
from mikage.core.request.store import DEFAULT_SHOT_FIELDS, shot_attributes, shot_row_of
from mikage.core.request.systems import UseTime, export_systems_of
from mikage.core.request.use_time import (
    USE_DAY, USE_OVER, USE_PASS, OtherUse, Use, UseBook, pass_id, passes_of, read_other_uses, seconds,
)
from mikage.core.request.workflow import plan_day, shots_of
from mikage.tests.test_plan import BELOW, LINE1, LINE2, T
from mikage.tests.test_request import DAY as REQ_DAY, LAST, D, request, sat
from mikage.tests.test_request_passes import DAY, SAT, along, point, system
from mikage.tests.test_request_quota import _history, _services, _shot_row

#: 点1 under the satellite at T and 点4 2 minutes on along the track: the same pass, each its own shot.
NEAR, LATER = point(1, BELOW, 300), point(4, along(120))


def _fit(use_time: UseTime, reqs=(NEAR, LATER), uses=None, cands=None):
    systems = [system(use_time=use_time)]
    passes = day_passes(list(reqs), DAY, [SAT])
    cands = cands if cands is not None else gather(list(reqs), DAY, [SAT], {}, set())
    book = uses if uses is not None else UseBook()
    out = allocate(cands, systems, [SAT], passes, DAY, None, book)
    return {c.request.id: c for c in out}, book, out


def test_the_system_config():
    columns = [{"key": "ID", "source": "id"}]
    problems = []
    [s] = export_systems_of([{"system": "S", "series": ["ISS"], "columns": columns,
                              "useTime": {"setupSeconds": 30, "perPass": 300, "perDay": 1200}}], problems)
    assert problems == [] and s.use_time == UseTime(30, 300, 1200) and s.use_time.limited
    [s] = export_systems_of([{"system": "S", "columns": columns, "useTime": {"setupSeconds": 20}}], problems)
    assert problems == [] and s.use_time == UseTime(20) and not s.use_time.limited
    [s] = export_systems_of([{"system": "S", "columns": columns}], problems)
    assert problems == [] and s.use_time == UseTime()
    [s] = export_systems_of([{"system": "S", "columns": columns, "useTime": {"setupSeconds": -1, "perPass": 0, "perDay": "x"}}], problems)
    assert s.use_time == UseTime() and len(problems) == 3
    assert "useTime.setupSeconds は 0 以上の数（秒）で指定" in problems[0]
    assert "useTime.perPass は 0 より大きい数（秒）で指定（上限なしは書かない）" in problems[1] and "useTime.perDay" in problems[2]
    problems = []
    export_systems_of([{"system": "S", "columns": columns, "useTime": 300}], problems)
    assert problems and '{"setupSeconds": 秒, "perPass": 秒, "perDay": 秒}' in problems[0]


def test_a_pass_is_used_from_its_first_start_to_its_last_end():
    half = 45 * 60000
    m = 60000
    uses = [Use(0, 10_000, True, 5_000), Use(100_000, 130_000, True, 115_000), Use(50 * m, 50 * m + 1000, True, 50 * m),
            Use(20_000, 25_000, False, 22_000)]
    groups = passes_of(uses, half)
    # By pass (direction, within half the period), in order of their start; the time between shots counts.
    assert [len(g) for g in groups] == [2, 1, 1] and [g[0].ascending for g in groups] == [True, False, True]
    assert seconds(groups[0]) == 130 and seconds(groups[1]) == 5 and seconds([]) == 0


def test_no_limits_as_before():
    c, book, _ = _fit(UseTime())
    assert c["R1"].result == c["R4"].result == TAKEN and book.texts() == []
    assert c["R1"].use_text == "" and "使用" not in pass_text(c["R1"])
    # The pass's number all the same (written on the shot).
    assert c["R1"].pass_id == c["R4"].pass_id and c["R1"].pass_id.endswith("昇")


def test_held_past_the_seconds_a_pass():
    # The setup (30 s) and the 2 minutes between the shots: about 151 s on the pass.
    c, book, _ = _fit(UseTime(30, 100))
    assert c["R1"].result == TAKEN and (c["R4"].result, c["R4"].reason) == ("見送り", USE_PASS)
    [(text, low)] = book.texts()
    assert text == f"ISS: 1 パス 100 秒まで（いちばん長いパス {c['R1'].pass_id} 31 秒）" and not low
    assert c["R1"].use_text.startswith("使用 21:59:29〜22:00:00（31 秒・パス ") and c["R1"].use_text in pass_text(c["R1"])
    # Room for both: the pass's total on each.
    c, book, _ = _fit(UseTime(30, 200))
    assert c["R1"].result == c["R4"].result == TAKEN
    assert c["R4"].use_text.endswith(f"パス {c['R4'].pass_id} で計 151 秒）")
    assert book.texts() == [(f"ISS: 1 パス 200 秒まで（いちばん長いパス {c['R1'].pass_id} 151 秒）", False)]
    # Close to the limit: told as running low.
    assert _fit(UseTime(30, 160))[1].texts()[0][1]


def test_one_inside_a_pass_s_use_adds_nothing():
    # 点2 2.5 s on goes into 点1's shot (まとめ) once the pass runs on to 点4: within its use, it adds nothing.
    inside = point(2, along(2.5), 600)
    c, book, _ = _fit(UseTime(30, 160), reqs=(NEAR, LATER, inside))
    assert all(x.result == TAKEN for x in c.values()) and c["R2"].member
    assert book.texts() == [(f"ISS: 1 パス 160 秒まで（いちばん長いパス {c['R1'].pass_id} 151 秒）", True)]


def test_taken_by_hand_past_the_limit_with_a_warning():
    c, book, out = _fit(UseTime(30, 100))
    c["R4"].manual = TAKEN
    c, book, out = _fit(UseTime(30, 100), cands=out)
    assert c["R4"].result == TAKEN
    [w] = [w for w in c["R4"].warnings if w.startswith(USE_OVER)]
    assert w == "使用時間の上限を超える（ISS 1 パス 100 秒まで・このパスで 151 秒・手動で採用）"
    assert book.texts()[0] == (f"ISS: 1 パス 100 秒まで（いちばん長いパス {c['R1'].pass_id} 151 秒）", True)
    # The hand taken off: held again, the warning gone.
    c["R4"].manual = None
    c, _, _ = _fit(UseTime(30, 100), cands=out)
    assert c["R4"].reason == USE_PASS and not any(w.startswith(USE_OVER) for w in c["R4"].warnings)


def test_held_past_the_seconds_a_day():
    c, book, _ = _fit(UseTime(30, None, 100))
    assert c["R1"].result == TAKEN and (c["R4"].result, c["R4"].reason) == ("見送り", USE_DAY)
    assert book.texts() == [("ISS: 1 日 100 秒のうち 31 秒（残り 69 秒）", False)]
    c, book, _ = _fit(UseTime(0, None, 150))
    assert c["R4"].result == TAKEN and book.texts() == [("ISS: 1 日 150 秒のうち 121 秒（残り 29 秒）", False)]
    # Both limits: each said.
    c, book, _ = _fit(UseTime(0, 200, 125))
    assert book.texts() == [(f"ISS: 1 パス 200 秒まで（いちばん長いパス {c['R1'].pass_id} 121 秒）・1 日 125 秒のうち 121 秒（残り 4 秒）",
                             True)]


def test_the_other_days_shots_on_the_same_pass_count():
    # Handed out the day before on the same pass, 2 minutes before 点1: the pass already runs from there.
    before = OtherUse("iss", T - 120_000, T - 119_000, True)
    c, book, _ = _fit(UseTime(0, 100), reqs=(NEAR,), uses=UseBook([before]))
    assert (c["R1"].result, c["R1"].reason) == ("見送り", USE_PASS)
    # Another direction, another satellite, or far enough: not the same pass.
    for other in (OtherUse("ISS", T - 120_000, T - 119_000, False), OtherUse("OTHER", T - 120_000, T - 119_000, True),
                  OtherUse("ISS", T - 60 * 60000, T - 60 * 60000 + 1000, True)):
        assert _fit(UseTime(0, 100), reqs=(NEAR,), uses=UseBook([other]))[0]["R1"].result == TAKEN


def test_the_pass_number():
    orbit = Orbit(LINE1, LINE2)

    def ascending(ms):
        _, _, v = orbit.satrec.sgp4_tsince((ms - orbit.epoch) / 60000)
        return v[2] > 0

    marks = [(ascending(ms), pass_id(orbit, ms, ascending(ms))) for ms in (orbit.epoch + k * 60000 for k in range(400))]
    runs = [list(g) for _, g in itertools.groupby(marks, key=lambda m: m[0])]
    # One number a half revolution, counted on at each ascending node from the TLE's revolution number.
    assert all(len({label for _, label in run}) == 1 for run in runs)
    labels = [run[0][1] for run in runs]
    assert labels[:4] == ["43271降", "43272昇", "43272降", "43273昇"]
    assert all(int(b[:-1]) - int(a[:-1]) == (1 if b.endswith("昇") else 0) for a, b in zip(labels, labels[1:]))
    assert pass_id(None, T, True) == "" and pass_id(orbit, math.nan, True) == ""


def test_the_shot_keeps_its_use():
    systems = [system(use_time=UseTime(30, 200))]
    plan = plan_day([NEAR, LATER], DAY, systems, [SAT], {}, passes=day_passes([NEAR, LATER], DAY, [SAT]))
    c = {x.request.id: x for x in plan.candidates}
    rows = shots_of(plan, T, {"SYS": "f.csv"}, "RUN")
    first = c["R1"].shots[0]
    assert (rows[0].use_start, rows[0].use_end, rows[0].pass_id) == (first.start - 30000, first.end, c["R1"].pass_id)
    a = shot_attributes(rows[0], DEFAULT_SHOT_FIELDS)
    assert (a["USE_START"], a["USE_END"], a["PASS_ID"]) == (rows[0].use_start, rows[0].use_end, rows[0].pass_id)
    back = shot_row_of(a, None, DEFAULT_SHOT_FIELDS, "OBJECTID")
    assert (back.use_start, back.use_end, back.pass_id) == (rows[0].use_start, rows[0].use_end, rows[0].pass_id)
    assert shot_row_of({}, None, DEFAULT_SHOT_FIELDS, "OBJECTID").use_start is None
    # In the system's file too.
    assert {"useStart", "useEnd", "useSeconds", "passId"} <= set(SOURCES) and {"useStart", "useEnd"} <= set(TIME_SOURCES)
    ctx = Context(systems[0], DAY, T)
    assert raw_value(c["R1"], "useStart", ctx) == rows[0].use_start and raw_value(c["R1"], "useSeconds", ctx) == 31
    assert raw_value(c["R1"], "passId", ctx) == c["R1"].pass_id
    # No limits: kept all the same (without setup).
    plan = plan_day([NEAR], DAY, [system()], [SAT], {}, passes=day_passes([NEAR], DAY, [SAT]))
    [row] = shots_of(plan, T, {"SYS": "f.csv"}, "RUN")
    assert row.use_start == plan.candidates[0].shots[0].start and row.pass_id


def _counted_system(**use_time):
    [s] = export_systems_of([{"system": "SYS-A", "label": "A", "kind": "光学", "series": ["OPT-X"], "format": "csv",
                              "columns": [{"key": "ID", "source": "id"}], "useTime": use_time}], [])
    return s


def test_fitted_by_counting_each_row_a_pass():
    sats = [sat("OPT-X 1", "OPT-X")]
    reqs = [request("R-1", priority=300), request("R-2", priority=400)]
    # Tokyo takes 2 scenes of 12 km: the setup and 24 km at 7 km/s, about 13 s a row.
    s = _counted_system(setupSeconds=10, perDay=25)
    book = UseBook()
    out = {c.request.id: c for c in allocate(gather(reqs, REQ_DAY, sats, {}, set()), [s], sats, None, None, None, book)}
    assert out["R-1"].result == TAKEN and (out["R-2"].result, out["R-2"].reason) == ("見送り", USE_DAY)
    assert book.texts() == [("OPT-X 1: 1 日 25 秒のうち 13 秒（残り 12 秒）", False)]
    out = allocate(gather(reqs, REQ_DAY, sats, {}, set()), [_counted_system(setupSeconds=10, perPass=12)], sats, None, None, None)
    assert [c.reason for c in out] == [USE_PASS, USE_PASS]


def test_the_simulation_keeps_the_use_time():
    sats = [sat("OPT-X 1", "OPT-X")]
    reqs = [request(f"R-{i}", category="定点", achieve="毎回", priority=300 + i, end=LAST + 5 * D) for i in (1, 2)]
    run = simulate(reqs, REQ_DAY, 2, [_counted_system(setupSeconds=10, perDay=25)], sats)
    assert [d.taken for d in run.days] == [1, 1] and [d.use_time for d in run.days] == [1, 1]
    assert dict((label, f(run)) for label, f in SUMMARY)["使用時間の上限で見送り"] == 2


def test_reading_the_other_days_shots():
    fake, store = _services()
    client = EsriClient(fake)
    use = {"USE_START": 1000.0, "USE_END": 5000.0, "ORBIT_DIR": "昇交", "PASS_ID": "1昇", "SATELLITE": "ISS"}
    _shot_row(fake, "before", "2026-11-07", **use)
    _shot_row(fake, "after", "2026-11-09", **{**use, "ORBIT_DIR": "降交", "PASS_ID": "1降"})
    _shot_row(fake, "missed", "2026-11-09", **use)
    _shot_row(fake, "rejected", "2026-11-07", **use)
    _shot_row(fake, "same-day", "2026-11-08", **use)
    _shot_row(fake, "too-early", "2026-11-06", **use)
    _shot_row(fake, "no-use", "2026-11-09")
    _history(fake, "R1", "before", day="2026-11-07")
    _history(fake, "R2", "missed", captured="未撮像", day="2026-11-09")
    _history(fake, "R3", "rejected", day="2026-11-07", ADOPTION="不採用")
    _history(fake, "R4", "after", day="2026-11-09")
    got = read_other_uses(client, store, None, date(2026, 11, 8))
    assert sorted((o.satellite, o.start, o.end, o.ascending, o.pass_id) for o in got) == [
        ("ISS", 1000.0, 5000.0, False, "1降"), ("ISS", 1000.0, 5000.0, True, "1昇")]
    assert read_other_uses(client, type(store)(**{**store.__dict__, "shots": None}), None, date(2026, 11, 8)) == []
