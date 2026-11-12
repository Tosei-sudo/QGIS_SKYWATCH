"""
A request taken several times a day (1 日の回数, tsudo 2026-11-11: 「n回以上、
m回以下って指定はどうかな。n回を超えたら合格って感じ？」「最大が未指定の場合は、
n回？」, and for a day short of n: 「取れる分だけ出す」): one candidate per
time, the first n at the request's priority and the rest in the room left,
each on its own pass `times_gap` hours apart; a row per time in the file
and the history; the day counts when n of them are taken.
"""
from __future__ import annotations

from dataclasses import replace

from mikage.core.esri import EsriClient
from mikage.core.catalog import catalog_of
from mikage.core.plan.satellites import satellites_from_text
from mikage.core.request.adoption import ADOPTED, answer_changes, match_answers
from mikage.core.request.daily import (
    FEW, GAP, HELD, OVER, TAKEN, allocate, gather, occurrence_key, occurrence_text, ordered,
)
from mikage.core.request.export import Context, csv_text, files_of, raw_value, render
from mikage.core.request.follow import follow_up, request_changes, undone_changes
from mikage.core.request.last import remembered, with_remembered
from mikage.core.request.model import ImagingRequest
from mikage.core.request.passes import day_passes
from mikage.core.request.result_file import Answer, result_file_of
from mikage.core.request.rows import request_of_row, row_of
from mikage.core.request.simulate import SUMMARY, run_passes, simulate
from mikage.core.request.store import (
    DEFAULT_HISTORY_FIELDS, DEFAULT_REQUEST_FIELDS, HistoryRow, attributes_of, history_attributes, history_row_of, request_of,
)
from mikage.core.request.systems import ExportSystem, export_systems_of
from mikage.core.request.workflow import history_of, keep_answers, plan_day, shots_of
from mikage.tests.fake_esri import FakeEsri, FakeLayer
from mikage.tests.test_plan import BELOW, LINE1, LINE2, T, iss
from mikage.tests.test_request_passes import DAY, point

H = 3600000
MIN = 60000
#: Two SAR satellites over BELOW the same day: ISS at T, ISS 2 (the same orbit, later) 20 minutes on.
A = iss(sar=True, swath=12, length=10, scenesPerPass=4, minInterval=1)
B = replace(satellites_from_text("ISS 2\n" + LINE1.replace("25544U", "25545U") + "\n"
                                 + LINE2.replace("2 25544", "2 25545").replace("247.4627", "252.4728").replace("325.0288", "247.5288"),
                                 45, 0, 10)[0], sar=True, swath=12, length=10, scenesPerPass=4, minInterval=1)
SATS = [A, B]


def system(**kw) -> ExportSystem:
    columns = export_systems_of([{"system": "SYS", "series": ["ISS"], "columns": [
        {"key": "ID", "source": "id"}, {"key": "TIME", "source": "occurrence"}, {"key": "ROW", "source": "occurrenceId"},
        {"key": "N", "source": "timesMin"}, {"key": "M", "source": "timesMax"}]}], [])[0].columns
    return ExportSystem(system="SYS", label="SYS", series=["ISS"], columns=columns, **kw)


def fit(reqs, sats=SATS, systems=None, cands=None):
    systems = systems or [system()]
    passes = day_passes(reqs, DAY, list(sats))
    cands = cands if cands is not None else gather(reqs, DAY, list(sats), {}, set())
    return allocate(cands, systems, list(sats), passes, DAY), systems


def at(i: int, priority: int = 500, **kw) -> ImagingRequest:
    """A point request below the satellite (see test_request_passes.point), with `kw` changed."""
    return replace(point(i, BELOW, priority), **kw)


def keyed(cands) -> dict:
    return {c.key: c for c in cands}


def test_the_times_a_request_asks():
    r = ImagingRequest(id="R")
    assert (r.times, r.gap_ms) == ((1, 1), H)
    assert replace(r, times_min=2).times == (2, 2)  # 最大が未指定なら n 回
    assert replace(r, times_min=2, times_max=3).times == (2, 3)
    assert replace(r, times_min=3, times_max=1).times == (3, 3)
    assert replace(r, times_min=0).times == (1, 1)
    assert replace(r, times_gap=0.5).gap_ms == H / 2 and replace(r, times_gap=0).gap_ms == 0
    assert occurrence_key("R", 1) == "R" and occurrence_key("R", 2) == "R#2"


def test_kept_in_the_ledger_and_history():
    req = point(1, BELOW, times_min=2, times_max=3, times_gap=1.5)
    a = attributes_of(req, DEFAULT_REQUEST_FIELDS)
    assert (a["TIMES_MIN"], a["TIMES_MAX"], a["TIMES_GAP"]) == (2, 3, 1.5)
    # Once a day (the default) leaves the columns empty.
    once = attributes_of(point(2, BELOW), DEFAULT_REQUEST_FIELDS)
    assert (once["TIMES_MIN"], once["TIMES_MAX"], once["TIMES_GAP"]) == (None, None, None)
    back = request_of({**a, "START_AT": req.start, "END_AT": req.end}, None, DEFAULT_REQUEST_FIELDS, "OBJECTID")
    assert (back.times_min, back.times_max, back.times_gap) == (2, 3, 1.5)
    # A table without the columns: once a day.
    plain = request_of({"REQ_ID": "R9"}, None, DEFAULT_REQUEST_FIELDS, "OBJECTID")
    assert (plain.times, plain.gap_ms) == ((1, 1), H)
    row = HistoryRow("RUN", T, DAY.isoformat(), "R1", "SYS", occurrence=2)
    h = history_attributes(row, DEFAULT_HISTORY_FIELDS)
    assert h["OCCURRENCE"] == 2
    assert history_row_of(h, None, DEFAULT_HISTORY_FIELDS, "OBJECTID").key == ("R1", 2)
    assert history_row_of({"REQ_ID": "R1"}, None, DEFAULT_HISTORY_FIELDS, "OBJECTID").occurrence == 1
    # The ledger's CSV rows and the form's remembered inputs.
    roles = {"timesMin": "TIMES_MIN", "timesMax": "TIMES_MAX", "timesGap": "TIMES_GAP"}
    values = {k: v for k, v in row_of(req).items() if k in roles.values()}
    assert values == {"TIMES_MIN": 2, "TIMES_MAX": 3, "TIMES_GAP": 1.5}
    read = request_of_row(values, roles, req.rings, "点")
    assert (read.times_min, read.times_max, read.times_gap) == (2, 3, 1.5)
    again = with_remembered(ImagingRequest(id="X"), remembered(req))
    assert (again.times_min, again.times_max, again.times_gap) == (2, 3, 1.5)


def test_one_candidate_a_time_the_extra_ones_last():
    raised = at(1, 300, times_max=3, achieve="期間内1回", end=T + 20 * H)
    reqs = [raised, point(2, BELOW, 500, times_min=2), point(3, BELOW, 900, times_max=2)]
    cands = gather(reqs, DAY, SATS, {}, {"R1"})
    assert [c.key for c in cands] == ["R1", "R1#2", "R1#3", "R2", "R2#2", "R3", "R3#2"]
    c = keyed(cands)
    assert [c[k].extra for k in ("R1", "R1#2", "R2#2", "R3#2")] == [False, True, False, True]
    # The raise is offered once, on the first time; the others follow it.
    assert c["R1"].boost_offer == 100 and c["R1#2"].boost_offer is None and c["R1#2"].boost == 100
    c["R1"].boost_taken = False
    assert c["R1#2"].boost == 0
    # The n times at the priority, then the extra ones round by round (every request's second before any third).
    assert [x.key for x in ordered(cands)] == ["R1", "R2", "R2#2", "R3", "R1#2", "R3#2", "R1#3"]
    assert [occurrence_text(c[k]) for k in ("R1", "R1#3", "R2#2")] == ["1/1 回目", "3 回目・追加", "2/2 回目"]
    assert occurrence_text(gather([point(4, BELOW)], DAY, SATS, {}, set())[0]) == ""


def test_each_time_on_its_own_pass_apart():
    twice = point(1, BELOW, times_min=2, times_gap=0.25)
    out, _ = fit([twice])
    c = keyed(out)
    assert c["R1"].result == c["R1#2"].result == TAKEN
    assert abs(c["R1#2"].planned - c["R1"].planned) >= 15 * MIN
    assert {c["R1"].satellite.name, c["R1#2"].satellite.name} == {"ISS", "ISS 2"}
    assert not c["R1"].merged and not any(w.startswith(FEW) for w in c["R1"].warnings)
    # An hour apart (the default): the second pass is 20 minutes on, so the second time finds none.
    out, _ = fit([replace(twice, times_gap=None)])
    c = keyed(out)
    assert c["R1"].result == TAKEN and (c["R1#2"].result, c["R1#2"].reason) == (HELD, GAP)
    # Taken fewer times than the day needs: what fits goes, told (「取れる分だけ出す」).
    assert f"{FEW}1/2 回）" in c["R1"].warnings
    # Past n, a time that finds no room is no shortfall.
    out, _ = fit([point(1, BELOW, times_max=2)])
    c = keyed(out)
    assert c["R1"].result == TAKEN and c["R1#2"].result == HELD and not any(w.startswith(FEW) for w in c["R1"].warnings)


def test_never_twice_on_one_pass():
    # One satellite, one pass, no interval, no gap asked: the second time still needs another pass.
    free = replace(A, minInterval=0)
    out, _ = fit([point(1, BELOW, times_min=2, times_gap=0)], sats=[free])
    c = keyed(out)
    assert c["R1"].result == TAKEN and (c["R1#2"].result, c["R1#2"].reason) == (HELD, GAP)
    # Others on the pass still go into its shot.
    out, _ = fit([point(1, BELOW, 300, times_min=2, times_gap=0), point(2, BELOW, 400)], sats=[free])
    c = keyed(out)
    assert c["R2"].member and c["R2"].lead is c["R1"] and c["R1#2"].result == HELD


def test_extra_times_take_only_the_room_left():
    reqs = [point(1, BELOW, 100, times_max=2, times_gap=0.25), point(2, BELOW, 900)]
    # One shot a day for each satellite: R1 on one, R2 (lower) on the other; R1's second time finds no room.
    once = [replace(s, maxPerDay=1) for s in SATS]
    c = keyed(fit(reqs, sats=once, systems=[system(merge=False)])[0])
    assert c["R1"].result == c["R2"].result == TAKEN and c["R1#2"].result == HELD
    # With room (no interval between shots: R2 beside R1 on the first pass), it goes.
    free = [replace(s, minInterval=0) for s in SATS]
    c = keyed(fit(reqs, sats=free, systems=[system(merge=False)])[0])
    assert c["R1#2"].result == TAKEN and c["R1#2"].satellite.name != c["R1"].satellite.name


def test_counted_without_the_orbits():
    sats = [replace(A, maxPerDay=2)]
    reqs = [point(1, BELOW, 100, times_min=2), point(2, BELOW, 900)]
    out, _ = fit(reqs, sats=sats, systems=[system(orbit=False)])
    c = keyed(out)
    assert c["R1"].result == c["R1#2"].result == TAKEN and (c["R2"].result, c["R2"].reason) == (HELD, OVER)


def test_a_row_per_time_in_the_file_and_history():
    reqs = [point(1, BELOW, times_min=2, times_gap=0.25), point(2, BELOW, 900, times_max=2)]
    systems = [system(merge=False)]
    passes = day_passes(reqs, DAY, SATS)
    plan = plan_day(reqs, DAY, systems, SATS, {}, passes=passes)
    c = keyed(plan.candidates)
    # Both passes taken by R1 (a minute between shots, nothing put together): R2 finds no room, its time past n neither.
    assert c["R1"].result == c["R1#2"].result == TAKEN and c["R2"].result == c["R2#2"].result == HELD
    [written] = render(plan.candidates, systems, DAY, T)
    assert written.ids == ("R1", "R1#2")
    ctx = Context(systems[0], DAY, T)
    text, _ = csv_text([c["R1"], c["R1#2"]], ctx)
    assert text.splitlines()[1:] == ["R1,1,R1-1,2,2", "R1,2,R1-2,2,2"]
    assert raw_value(c["R2"], "occurrenceId", ctx) == "R2-1"
    assert raw_value(gather([point(3, BELOW)], DAY, SATS, {}, set())[0], "occurrenceId", ctx) == "R3"
    rows = history_of(plan, T, files_of([written]), "RUN")
    got = {(r.request_id, r.occurrence): r for r in rows}
    assert got[("R1", 1)].result == got[("R1", 2)].result == TAKEN
    assert got[("R1", 1)].shot_id and got[("R1", 1)].shot_id != got[("R1", 2)].shot_id
    shots = shots_of(plan, T, files_of([written]), "RUN")
    assert {s.shot_id for s in shots} >= {got[("R1", 1)].shot_id, got[("R1", 2)].shot_id}
    # A time past n that found no room has no row; one of the n held back has its own.
    assert ("R2", 2) not in got and got[("R2", 1)].result == HELD
    # Split into files of one: each time to its own file, the history following.
    split = [system(merge=False, max_per_file=1)]
    plan = plan_day(reqs, DAY, split, SATS, {}, passes=passes)
    files = render(plan.candidates, split, DAY, T)
    names = files_of(files)["SYS"]
    assert names["R1"] != names["R1#2"]
    rows = {(r.request_id, r.occurrence): r for r in history_of(plan, T, files_of(files), "RUN")}
    assert rows[("R1", 2)].file == names["R1#2"] and rows[("R1", 1)].file == names["R1"]


def test_handed_out_today_pinned_per_time():
    reqs = [point(1, BELOW, times_min=2, times_gap=0.25)]
    passes = day_passes(reqs, DAY, SATS)
    other = [system(), replace(system(), system="OTHER", label="OTHER")]
    today = [HistoryRow("RUN", T, DAY.isoformat(), "R1", "OTHER", "ISS 2", result=TAKEN, captured="未判定", occurrence=2)]
    plan = plan_day(reqs, DAY, other, SATS, {}, today, target="SYS", passes=passes)
    c = keyed(plan.candidates)
    assert plan.written == {"R1#2": ("OTHER", "ISS 2")}
    assert c["R1#2"].pinned == ("OTHER", "ISS 2") and c["R1"].pinned is None


def test_answers_kept_per_time():
    day = DAY.isoformat()
    earlier = [HistoryRow("OLD", T, day, "R1", "SYS", result=TAKEN, adoption="採用", object_id=1),
               HistoryRow("OLD", T, day, "R1", "SYS", result=TAKEN, adoption="不採用", object_id=2, occurrence=2)]
    new = [HistoryRow("NEW", T + H, day, "R1", "SYS", result=TAKEN, adoption="不明"),
           HistoryRow("NEW", T + H, day, "R1", "SYS", result=TAKEN, adoption="不明", occurrence=2)]
    out = keep_answers(earlier, new)
    assert [(r.occurrence, r.adoption) for r in out.rows] == [(1, "採用"), (2, "不採用")]
    assert out.carried == 2 and out.gone == [1, 2]


def test_the_systems_answer_for_one_time():
    day = DAY.isoformat()
    rows = [HistoryRow("X", T, day, "R1", "SYS", result=TAKEN, object_id=1, shot_id="X-1"),
            HistoryRow("X", T, day, "R1", "SYS", result=TAKEN, object_id=2, occurrence=2, shot_id="X-2"),
            HistoryRow("X", T, day, "R2", "SYS", result=TAKEN, object_id=3, merged_into="R1", shot_id="X-2")]
    requests = [point(1, BELOW, times_min=2), point(2, BELOW)]
    spec = result_file_of({"key": "ID", "result": "R"}, "x", [])
    # The time named (occurrenceId): that row, and the request put into its shot.
    answers = [Answer(2, "R1-2", "不採用", keys={"id": "R1-2"}, stated=True)]
    reading = match_answers(answers, rows, requests, "SYS", day, spec, "r.csv")
    assert [m.time for m in reading.matches] == [2]
    assert {oid: v["adoption"] for oid, v in answer_changes(reading)} == {2: "不採用", 3: "不採用"}
    assert [r.object_id for r in reading.missing] == [1]
    # The request named: every time; a time named too wins for that time.
    answers = [Answer(2, "R1", ADOPTED, keys={"id": "R1"}, stated=True), Answer(3, "R1-2", "不採用", keys={"id": "R1-2"}, stated=True)]
    reading = match_answers(answers, rows, requests, "SYS", day, spec, "r.csv")
    assert {oid: v["adoption"] for oid, v in answer_changes(reading)} == {1: ADOPTED, 2: "不採用", 3: "不採用"}
    assert reading.missing == [] and reading.problems == []
    # A hyphen in the id of a request taken once a day is not a time.
    once = [HistoryRow("X", T, day, "R-5", "SYS", result=TAKEN, object_id=9)]
    reading = match_answers([Answer(2, "R-5", ADOPTED, keys={"id": "R-5"}, stated=True)], once,
                            [replace(point(5, BELOW), id="R-5")], "SYS", day, spec, "r.csv")
    assert [r.object_id for m in reading.matches for r in m.rows] == [9]


CATALOG = "https://gis.example/rest/services/I/FeatureServer/0"


def _catalog(*images):
    fake = FakeEsri()
    layer = fake.add_layer(CATALOG, FakeLayer({"IMAGE_ID": "esriFieldTypeString", "ACQ": "esriFieldTypeDate", "SENSOR": "esriFieldTypeString",
                                               "URL": "esriFieldTypeString"}, "esriGeometryPolygon"))
    ring = [[BELOW[0] - 0.2, BELOW[1] - 0.2], [BELOW[0] - 0.2, BELOW[1] + 0.2], [BELOW[0] + 0.2, BELOW[1] + 0.2],
            [BELOW[0] + 0.2, BELOW[1] - 0.2], [BELOW[0] - 0.2, BELOW[1] - 0.2]]
    for image_id, at in images:
        layer.add({"IMAGE_ID": image_id, "ACQ": at, "SENSOR": "ISS"}, {"rings": [ring], "spatialReference": {"wkid": 4326}})
    catalog = catalog_of({"label": "画像", "url": CATALOG, "fields": {"id": "IMAGE_ID", "acquired": "ACQ", "sensor": "SENSOR", "source": "URL"}},
                         [], 0)
    return EsriClient(fake), catalog


def test_followed_up_as_many_images_as_times():
    req = replace(point(1, BELOW, times_min=2), object_id=7, achieve="期間内1回")
    day = DAY.isoformat()
    rows = [HistoryRow("RUN", T, day, "R1", "SYS", "ISS", result=TAKEN, captured="未判定", object_id=1),
            HistoryRow("RUN", T, day, "R1", "SYS", "ISS 2", result=TAKEN, captured="未判定", object_id=2, occurrence=2)]
    after = T + 30 * H
    # Two images an hour and more apart: each time has its own.
    client, catalog = _catalog(("IMG-1", T), ("IMG-2", T - 2 * H))
    found = follow_up(client, catalog, None, rows, {"R1": req}, after, 3)
    assert [(f.row.occurrence, f.captured, f.image_id) for f in found] == [(1, "撮像済み", "IMG-1"), (2, "撮像済み", "IMG-2")]
    assert request_changes(found, {"R1": req}) == [(7, {"boost": 0, "status": "完了"})]
    # Two images of one pass (minutes apart) are one time: the second waits, and the day does not count.
    client, catalog = _catalog(("IMG-1", T), ("IMG-1b", T + 2 * MIN))
    found = follow_up(client, catalog, None, rows, {"R1": req}, after, 3)
    assert [(f.row.occurrence, f.captured) for f in found] == [(1, "撮像済み")]
    assert request_changes(found, {"R1": req}) == []
    later = follow_up(client, catalog, None, rows, {"R1": req}, after + 4 * 86400000, 3)
    assert [(f.row.occurrence, f.captured) for f in later] == [(1, "撮像済み"), (2, "未撮像")]
    # A time settled earlier keeps its image; the other takes another, and the day counts with both.
    settled = [replace(rows[0], captured="撮像済み", image_id="IMG-1"), rows[1]]
    client, catalog = _catalog(("IMG-1", T), ("IMG-2", T - 3 * H))
    found = follow_up(client, catalog, None, settled, {"R1": req}, after, 3)
    assert [(f.row.occurrence, f.image_id) for f in found] == [(2, "IMG-2")]
    assert request_changes(found, {"R1": req}) == []
    assert request_changes(found, {"R1": req}, settled) == [(7, {"boost": 0, "status": "完了"})]
    # Marked 未撮像 by hand afterwards: back to 有効 (one time is not the day).
    done = replace(req, status="完了")
    from mikage.core.request.follow import Finding
    back = [Finding(replace(settled[1], captured="撮像済み", image_id="IMG-2"), "未撮像")]
    history = [settled[0], replace(settled[1], captured="撮像済み", image_id="IMG-2")]
    assert undone_changes(back, {"R1": done}, history) == [(7, {"status": "有効"})]
    # Once a day as before: a row each, whatever the others.
    once = replace(req, times_min=1)
    found = follow_up(client, catalog, None, [rows[0]], {"R1": once}, after, 3)
    assert [(f.captured, f.image_id) for f in found] == [("撮像済み", "IMG-1")]


def test_the_limit_simulation_counts_a_day_with_n_times():
    short = at(1, achieve="期間内1回", times_min=2, end=T + 30 * H)
    passes = run_passes([short], DAY, 2, SATS)
    sim = simulate([short], DAY, 2, [system()], SATS, passes=passes)
    first = sim.days[0]
    assert (first.taken, first.short, first.carried, first.candidates) == (1, 1, 1, 2)
    assert "回数不足（1 日の回数に届かず）" in [label for label, _ in SUMMARY]
    # Apart enough: both times on the first day, so 完了 (nothing left the second day).
    apart = replace(short, times_gap=0.25)
    sim = simulate([apart], DAY, 2, [system()], SATS, passes=run_passes([apart], DAY, 2, SATS))
    assert (sim.days[0].taken, sim.days[0].short, sim.days[0].carried) == (2, 0, 0)
    assert sim.days[1].candidates == 0
