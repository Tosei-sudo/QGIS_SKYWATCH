"""
The day's work on the satellites' passes (tsudo, 2026-11-11: 「撮像地点のマージや、
撮像条件（連続で n シーン、次への間隔）は考慮されてる？」 → 「2 で実装して」):
each request on a pass of a satellite over it, requests close together on
one pass put into one shot (連続シーン) and handed out as one with the
outline of the shot's scenes, the satellite's interval between shots and
its shots a day kept; the history and the schedule stay one row per request.
"""
from __future__ import annotations

from dataclasses import replace

from mikage.core.plan.scene import sub_satellite_point
from mikage.core.request.adoption import answer_changes, match_answers
from mikage.core.request.daily import BUSY, NO_PASS, OVER, PART, TAKEN, allocate, gather, merge_text, pass_text, room_text
from mikage.core.request.export import Context, csv_text, files_of, raw_value, render
from mikage.core.request.model import ImagingRequest, in_rings, jst_day, square_rings
from mikage.core.request.passes import day_passes, size_km, target_of
from mikage.core.request.result_file import ADOPTED, Answer, result_file_of
from mikage.core.request.simulate import run_passes, simulate
from mikage.core.request.store import HistoryRow
from mikage.core.request.systems import ExportSystem, export_systems_of
from mikage.core.request.workflow import history_of, plan_day
from mikage.tests.test_plan import BELOW, T, iss

H = 3600000
#: A satellite looking either way at night and day (SAR), 12 km wide, up to 4 scenes of 10 km in a row, a minute between shots.
SAT = iss(sar=True, swath=12, length=10, scenesPerPass=4, minInterval=1)
DAY = jst_day(T)


def system(**kw) -> ExportSystem:
    columns = export_systems_of([{"system": "SYS", "series": ["ISS"], "columns": [
        {"key": "ID", "source": "id"}, {"key": "IDS", "source": "mergedIds"}, {"key": "N", "source": "mergedCount"},
        {"key": "SCENES", "source": "scenes"}, {"key": "AREA", "source": "geometry", "geometry": "wkt"},
        {"key": "AT", "source": "passTime"}, {"key": "DIR", "source": "passDirection"}, {"key": "SIDE", "source": "passLookSide"}]}], [])[0].columns
    return ExportSystem(system="SYS", label="SYS", series=["ISS"], columns=columns, **kw)


def point(i: int, where, priority: int = 500, **kw) -> ImagingRequest:
    return ImagingRequest(id=f"R{i}", name=f"点{i}", priority=priority, start=T - 6 * H, end=T + 6 * H, shape_kind="点",
                          rings=square_rings(where[0], where[1], 0.1), **kw)


def along(seconds: float):
    """The place under the satellite `seconds` after T."""
    return sub_satellite_point(SAT, T + seconds * 1000)


def fit(reqs, sats=(SAT,), systems=None, day=DAY, cands=None):
    systems = systems or [system()]
    passes = day_passes(reqs, day, list(sats))
    cands = cands if cands is not None else gather(reqs, day, list(sats), {}, set())
    return allocate(cands, systems, list(sats), passes, day), systems


def by_id(cands):
    return {c.request.id: c for c in cands}


def test_one_request_on_its_pass():
    out, _ = fit([point(1, BELOW)])
    [c] = out
    assert c.result == TAKEN and c.satellite.name == "ISS" and c.scenes == 1 and not c.merged
    assert abs(c.planned - T) < 60000
    assert "（日本時間）" in pass_text(c) and "連続 1 シーン" in pass_text(c)


def test_close_on_one_pass_put_into_one_shot():
    # 点2 is about 17 km on along the track: apart, the minute between shots would hold it back;
    # in one shot of 2 scenes in a row it goes (「連続シーンで取れば次への間隔をクリアできるときはマージ」).
    a, b = point(1, BELOW, 300), point(2, along(2.5))
    out, systems = fit([a, b])
    c = by_id(out)
    assert c["R1"].result == c["R2"].result == TAKEN
    assert c["R1"].merged and not c["R1"].member and c["R2"].member and c["R2"].lead is c["R1"]
    assert c["R1"].scenes == 2 and c["R2"].scenes == 0
    assert merge_text(c["R1"]) == "まとめ 2 件（点2 と 1 回の撮像）"
    assert merge_text(c["R2"]) == "まとめ（「点1」の撮像に同乗）"
    assert room_text(out, systems) == {"SYS": "1 件・2 枚"}
    # One row in the file: the first request's, with both ids and the outline of the shot's scenes as its area.
    [written] = render(out, systems, DAY, T)
    assert written.count == 1
    ctx = Context(systems[0], DAY, T)
    ring = raw_value(c["R1"], "geometry", ctx, "geojson", 6, as_object=True)["coordinates"][0]
    assert all(in_rings([ring], *_middle(r)) for r in (a, b))
    assert raw_value(c["R1"], "mergedIds", ctx) == ["R1", "R2"] and raw_value(c["R1"], "mergedCount", ctx) == 2
    text, warnings = csv_text([c["R1"]], ctx)
    assert warnings == [] and text.splitlines()[1].startswith('R1,"R1,R2",2,2,"POLYGON ((')
    assert raw_value(c["R1"], "passDirection", ctx) in ("昇交", "降交") and raw_value(c["R1"], "passLookSide", ctx) in ("右", "左")
    assert abs(raw_value(c["R1"], "passTime", ctx) - T) < 60000


def _middle(r: ImagingRequest):
    ring = r.rings[0]
    return (sum(p[0] for p in ring[:-1]) / 4, sum(p[1] for p in ring[:-1]) / 4)


def test_not_put_together_when_the_system_says_no():
    out, _ = fit([point(1, BELOW, 300), point(2, along(2.5))], systems=[system(merge=False)])
    c = by_id(out)
    assert c["R1"].result == TAKEN and not c["R1"].merged
    assert (c["R2"].result, c["R2"].reason) == ("見送り", BUSY)


def test_interval_and_shots_a_day():
    far = point(3, along(30))   # 30 s on: too far for 4 scenes, inside the minute
    later = point(4, along(120))  # 2 minutes on: its own shot
    out, _ = fit([point(1, BELOW, 300), far, later])
    c = by_id(out)
    assert (c["R3"].result, c["R3"].reason) == ("見送り", BUSY)
    assert c["R4"].result == TAKEN and not c["R4"].merged
    # Without an interval both go on their own shots; one shot a day leaves only what fits in the first.
    free = replace(SAT, minInterval=0)
    assert all(x.result == TAKEN for x in fit([point(1, BELOW, 300), far, later], sats=[free])[0])
    once = replace(SAT, maxPerDay=1)
    c = by_id(fit([point(1, BELOW, 300), point(2, along(2.5)), later], sats=[once])[0])
    assert c["R1"].result == c["R2"].result == TAKEN and c["R2"].member
    assert (c["R4"].result, c["R4"].reason) == ("見送り", BUSY)


def test_no_pass_in_its_window():
    # Asked only for an hour without a pass over it.
    quiet = replace(point(1, BELOW), start=T - 3 * H, end=T - 2 * H)
    [c] = fit([quiet])[0]
    assert (c.result, c.reason) == ("見送り", NO_PASS)


def test_count_limit_leaves_room_for_requests_put_into_a_shot():
    out, _ = fit([point(1, BELOW, 300), point(2, along(2.5)), point(4, along(120))], systems=[system(limit={"count": 1})])
    c = by_id(out)
    assert c["R1"].result == c["R2"].result == TAKEN and c["R2"].member
    assert (c["R4"].result, c["R4"].reason) == ("見送り", OVER)


def test_taken_by_hand_over_the_interval():
    cands = gather([point(1, BELOW, 300), point(3, along(30))], DAY, [SAT], {}, set())
    cands[1].manual = TAKEN
    c = by_id(fit([x.request for x in cands], cands=cands)[0])
    assert c["R3"].result == TAKEN and f"{BUSY}（手動で採用）" in c["R3"].warnings
    # Fitting again does not stack the warning.
    again = by_id(fit([x.request for x in cands], cands=list(c.values()))[0])
    assert again["R3"].warnings.count(f"{BUSY}（手動で採用）") == 1


def test_satellites_without_orbit_are_counted_as_before():
    blind = replace(SAT, tle1="", tle2="", maxPerDay=1)
    out, _ = fit([point(1, BELOW, 300), point(2, along(2.5))], sats=[blind])
    c = by_id(out)
    assert c["R1"].result == TAKEN and c["R1"].shots == [] and c["R1"].planned is None
    assert (c["R2"].result, c["R2"].reason) == ("見送り", OVER)
    # … and so is a system that says `orbit: false`.
    out, _ = fit([point(1, BELOW, 300), point(2, along(2.5))], systems=[system(orbit=False)])
    assert all(x.result == TAKEN and not x.shots for x in out)


def test_an_area_larger_than_a_shot_takes_passes_one_after_another():
    lon, lat = BELOW
    wide = ImagingRequest(id="W", name="広域", start=T - 6 * H, end=T + 6 * H, rings=square_rings(lon, lat, 60))
    assert size_km(wide) > 80 and target_of(wide, 12).cells and not target_of(wide, 100).cells
    [c] = fit([wide])[0]
    assert c.result == TAKEN and c.shots and c.coverage is not None
    assert c.scenes == sum(s.frames for s in c.shots)
    assert any(w.startswith(PART) for w in c.warnings) == (c.coverage < 0.95)


def test_history_keeps_a_row_per_request():
    a, b = point(1, BELOW, 300), point(2, along(2.5))
    systems = [system()]
    passes = day_passes([a, b], DAY, [SAT])
    plan = plan_day([a, b], DAY, systems, [SAT], {}, passes=passes)
    rows = {r.request_id: r for r in history_of(plan, T, {"SYS": "SYS.csv"})}
    assert (rows["R1"].merged_into, rows["R1"].scenes, rows["R1"].file) == ("", 2, "SYS.csv")
    assert (rows["R2"].merged_into, rows["R2"].scenes, rows["R2"].file, rows["R2"].result) == ("R1", 0, "SYS.csv", TAKEN)
    # Each with its own area (the schedule shows the requests as asked).
    assert rows["R2"].rings == [[list(p[:2]) for p in b.rings[0]]]
    # Split into files of one row each (maxPerFile): the request put into the shot is in its lead's file.
    four = point(4, along(120))
    split = [system(max_per_file=1)]
    two = plan_day([a, b, four], DAY, split, [SAT], {}, passes=day_passes([a, b, four], DAY, [SAT]))
    written = render(two.candidates, split, DAY, T)
    assert [(w.part, w.ids) for w in written] == [(1, ("R1", "R2")), (2, ("R4",))]
    rows = {r.request_id: r for r in history_of(two, T, files_of(written))}
    assert rows["R2"].result == TAKEN and rows["R2"].file == rows["R1"].file != rows["R4"].file
    # Fitting again after a change by hand keeps the passes.
    plan.candidates[0].manual = "見送り"
    plan.refit()
    assert by_id(plan.candidates)["R2"].result == TAKEN and not by_id(plan.candidates)["R2"].merged


def test_the_systems_answer_for_a_shot_goes_to_each_request_in_it():
    rows = [HistoryRow(run_id="X", run_date=T, day=DAY.isoformat(), request_id="R1", system="SYS", object_id=1),
            HistoryRow(run_id="X", run_date=T, day=DAY.isoformat(), request_id="R2", system="SYS", object_id=2, merged_into="R1"),
            HistoryRow(run_id="X", run_date=T, day=DAY.isoformat(), request_id="R4", system="SYS", object_id=3)]
    requests = [point(1, BELOW), point(2, along(2.5)), point(4, along(120))]
    spec = result_file_of({"key": "ID", "result": "R"}, "x", [])
    answers = [Answer(2, "R1", ADOPTED, keys={"id": "R1"}, stated=True)]
    reading = match_answers(answers, rows, requests, "SYS", DAY.isoformat(), spec, "r.csv")
    assert [r.request_id for r in reading.missing] == ["R4"]
    assert [oid for oid, v in answer_changes(reading) if v["adoption"] == ADOPTED] == [1, 2]
    # A row of its own in the file answers for itself.
    answers.append(Answer(3, "R2", "不採用", keys={"id": "R2"}, stated=True))
    reading = match_answers(answers, rows, requests, "SYS", DAY.isoformat(), spec, "r.csv")
    assert {oid: v["adoption"] for oid, v in answer_changes(reading)} == {1: ADOPTED, 2: "不採用"}


def test_simulation_runs_on_the_passes():
    reqs = [point(1, BELOW, 300), point(2, along(2.5)), point(3, along(30))]
    passes = run_passes(reqs, DAY, 1, [SAT])
    sim = simulate(reqs, DAY, 1, [system()], [SAT], passes=passes)
    [d] = sim.days
    assert (d.taken, d.merged, d.over, d.used) == (2, 1, 1, {"SYS": (1, 2)})


def test_settings_say_orbit_and_merge():
    problems = []
    [s] = export_systems_of([{"system": "A", "columns": [{"key": "ID", "source": "id"}], "merge": False, "orbit": "no"}], problems)
    assert (s.merge, s.orbit) == (False, True) and problems == ["requestExports[0] の orbit は true か false で指定"]
