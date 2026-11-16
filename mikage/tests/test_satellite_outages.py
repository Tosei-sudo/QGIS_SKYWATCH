"""
衛星の障害情報 (tsudo, 2026-11-16): the outages table (`satelliteOutages`),
when an outage holds, what it keeps from the passes, and how the plan, the
day's file and the trial of limits take it.
"""
from __future__ import annotations

import math
from dataclasses import replace
from datetime import timedelta

import pytest

from mikage.core.config import parse_config
from mikage.core.esri import EsriClient, EsriError
from mikage.core.plan.access import PlanResult, plan_access
from mikage.core.plan.output import describe_problems
from mikage.core.plan.outages import (
    DEFAULT_OUTAGE_FIELDS, OUTAGE, OUTAGE_WARNING, ONGOING, OVERDUE, PLANNED, RECOVERED, Outage, attach, attributes_of, blocking,
    check_outage, day_range, delete_outages, handed_hit, leave_out, load_outages, matches, new_id, on_day, outage_of, save_outage,
    summary_text, warning_of, when_text,
)
from mikage.core.request.daily import NO_PASS, TAKEN, allocate, gather
from mikage.core.request.passes import day_passes
from mikage.core.request.simulate import SUMMARY, simulate
from mikage.core.request.systems import export_systems_of
from mikage.tests.fake_esri import FakeEsri, FakeLayer
from mikage.tests.test_plan import BELOW, T, around, iss
from mikage.tests.test_request import DAY as REQUEST_DAY, request, sat
from mikage.tests.test_request_passes import DAY, SAT, point, system

H = 3_600_000
URL = "https://x/arcgis/rest/services/MIKAGE/FeatureServer/27"


def outage(**kw) -> Outage:
    base = dict(id="OUT-1", satellite="ISS", start=T - 24 * H)
    base.update(kw)
    return Outage(**base)


def fit(reqs, sats, systems=None, day=DAY, now=None):
    systems = systems or [system()]
    passes = day_passes(reqs, day, list(sats), now)
    day_sats = on_day(list(sats), day, now if now is not None else T)
    cands = gather(reqs, day, day_sats, {}, set())
    return allocate(cands, systems, day_sats, passes, day), passes


# ---- the outage itself ------------------------------------------------------------------------------

def test_when_it_holds_and_what_it_is_said_to_be():
    now = T
    planned = outage(start=now + H, expected=now + 5 * H)
    assert planned.status(now) == PLANNED and planned.open(now) and not planned.at(now, now) and planned.at(now + 2 * H, now)
    ongoing = outage(expected=now + H)
    assert ongoing.status(now) == ONGOING and ongoing.at(now, now) and not ongoing.at(now + 2 * H, now)
    # Past its expected end without a recovery: still on, without an end (decision 2, A).
    overdue = outage(expected=now - H)
    assert overdue.status(now) == OVERDUE and overdue.end(now) == math.inf and overdue.at(now + 100 * H, now)
    assert "を超過" in overdue.short_text(now)
    done = outage(expected=now + H, recovered=now - H)
    assert done.status(now) == RECOVERED and not done.open(now) and not done.at(now, now) and done.at(now - 2 * H, now)
    # Not known when it ends: on and on.
    assert outage().end(now) == math.inf and "未定" in outage().short_text(now)
    assert outage().during(now, now + H, now) and not planned.during(now - 2 * H, now, now)


def test_what_keeps_a_pass_from_being_used():
    assert outage(stop=True).blocks(True, "right", 10, 30) == "撮像停止"
    left_only_ascending = outage(closed=frozenset({(True, "right"), (False, "right"), (False, "left")}))
    assert left_only_ascending.blocks(True, "left", 10, 30) is None
    assert left_only_ascending.blocks(True, "right", 10, 30) == "使えない方向"
    assert left_only_ascending.blocks(False, "left", 10, 30) == "使えない方向"
    # 「昇交は左のみ、降交は自由」: 昇交・右 only.
    free_descending = outage(closed=frozenset({(True, "right")}))
    assert free_descending.blocks(False, "right", 10, 30) is None and free_descending.blocks(True, "right", 10, 30)
    angle = outage(max_off_nadir=20)
    assert angle.blocks(True, "left", 20, 30) is None and angle.blocks(True, "left", 25, 30) == "オフナディア角の上限"
    night = outage(no_night=True)
    assert night.blocks(True, "left", 10, 5) is None and night.blocks(True, "left", 10, -1) == "夜間不可"
    # Only その他の影響: the calculation is as it was.
    other = outage(other="画質の低下")
    assert other.blocks(True, "left", 89, -90) is None and not other.limits and "画質の低下" in other.impact_text()
    assert left_only_ascending.impacts() == ["使えない方向: 昇交右・降交右・降交左"]
    assert outage(max_off_nadir=20.5, max_per_day=2, min_interval=15).impact_text() == "オフナディア角 20.5° まで、1 日 2 回まで、撮像間隔 15 分以上"


def test_which_satellites_it_hits():
    x27 = replace(sat("ICEYE-X27", "ICEYE", sar=True, mode="スポットライト"), id="55049", aliases=("X27",))
    assert matches(outage(satellite="iceye x27"), x27)
    assert matches(outage(satellite="55049"), x27) and matches(outage(satellite="X27"), x27)
    # The series: every satellite of it.
    assert matches(outage(satellite="ICEYE"), x27)
    assert matches(outage(satellite="ICEYE-X27", mode="スポットライト"), x27)
    assert not matches(outage(satellite="ICEYE-X27", mode="ストリップマップ"), x27)
    assert not matches(outage(satellite="ICEYE-X28"), x27)
    # A sensor named: that sensor's modes (a satellite without sensors: the sensor may be its own name).
    wv = replace(sat("WV-3", "WV", mode="パンクロ"), sensor="WV110")
    assert matches(outage(satellite="WV-3", sensor="WV110"), wv) and not matches(outage(satellite="WV-3", sensor="CAVIS"), wv)
    assert matches(outage(satellite="ICEYE-X27", sensor="ICEYE-X27"), x27)


def test_attached_and_made_stricter_on_its_days():
    a, b = sat("OPT-X 1", "OPT-X", per_day=5), sat("CONST-Z 7", "CONST-Z", per_day=0)
    hit = outage(satellite="OPT-X 1", start=day_range(REQUEST_DAY)[0] + 6 * H, recovered=day_range(REQUEST_DAY)[0] + 8 * H,
                 max_per_day=2, max_off_nadir=15, scenes_per_pass=1, min_interval=30)
    sats = attach([a, b], [hit, outage(satellite="ELSEWHERE")])
    assert sats[0].outages == (hit,) and sats[1] is b
    # Attached again with the same outages: the same objects.
    again = attach(sats, [hit])
    assert again[0] is sats[0]
    day = on_day(sats, REQUEST_DAY, T)
    assert (day[0].maxPerDay, day[0].maxOffNadir, day[0].scenesPerPass, day[0].minInterval) == (2, 15, 1, 30)
    assert day[1] is sats[1]
    # Never looser than the satellite's own; the day after, the satellite's own.
    loose = attach([replace(a, maxPerDay=1, maxOffNadir=10, minInterval=60)], [hit])
    assert on_day(loose, REQUEST_DAY, T)[0].maxPerDay == 1 and on_day(loose, REQUEST_DAY, T)[0].maxOffNadir == 10
    assert on_day(loose, REQUEST_DAY, T)[0].minInterval == 60
    assert on_day(sats, REQUEST_DAY + timedelta(days=1), T)[0].maxPerDay == 5
    # A satellite with no daily limit gets the outage's.
    assert on_day(attach([replace(a, maxPerDay=0)], [hit]), REQUEST_DAY, T)[0].maxPerDay == 2
    assert summary_text(sats, *day_range(REQUEST_DAY), T).startswith("障害中の衛星: OPT-X 1（")
    assert summary_text(sats, *day_range(REQUEST_DAY + timedelta(days=1)), T) == ""


# ---- the plan and the day's file ------------------------------------------------------------------

def test_the_plan_leaves_out_the_passes_it_hits():
    plain_sat = iss()
    result = plan_access([plain_sat], [_target()], around(T, 120))
    assert result.opportunities
    op = result.opportunities[0]
    closed = attach([plain_sat], [outage(closed=frozenset({(op.ascending, op.side)}))])
    leave_out(result, closed, T)
    assert result.outaged >= 1 and all((o.ascending, o.side) != (op.ascending, op.side) for o in result.opportunities)
    assert "衛星の障害で使えない機会" in describe_problems([], 0, result.outaged)
    stopped = PlanResult(list(plan_access([plain_sat], [_target()], around(T, 120)).opportunities), [])
    leave_out(stopped, attach([plain_sat], [outage(stop=True)]), T)
    assert stopped.opportunities == [] and stopped.outaged > 0
    # Recovered before the passes: nothing left out.
    fine = PlanResult(list(plan_access([plain_sat], [_target()], around(T, 120)).opportunities), [])
    leave_out(fine, attach([plain_sat], [outage(stop=True, recovered=T - 12 * H)]), T)
    assert fine.outaged == 0 and fine.opportunities


def _target():
    from mikage.core.plan.access import PlanTarget

    return PlanTarget(label="真下", center=tuple(BELOW))


def test_a_request_with_only_passes_it_hits_is_held_back_for_it():
    req = point(1, BELOW)
    [taken], _ = fit([req], [SAT])
    assert taken.result == TAKEN
    stopped = attach([SAT], [outage(stop=True, reason="姿勢制御の不具合", expected=T + 48 * H)])
    [c], passes = fit([req], stopped, now=T)
    assert c.result != TAKEN and c.reason.startswith(OUTAGE + "（ISS: 姿勢制御の不具合、撮像停止、復旧見込み ")
    assert passes.blocked_on(req, DAY) == [stopped[0].outages[0]] and passes.of(req, DAY) == []
    # The direction of the pass closed (both sides of it): the same.
    ops = day_passes([req], DAY, [SAT]).of(req, DAY)
    closed = frozenset((o.ascending, side) for o in ops for side in ("right", "left"))
    [d], _ = fit([req], attach([SAT], [outage(closed=closed)]))
    assert d.result != TAKEN and d.reason.startswith(OUTAGE) and "使えない方向" in d.reason
    # Recovered the day before: back to normal; recovered that morning: taken, told (the outage touched the day).
    [e], _ = fit([req], attach([SAT], [outage(start=T - 72 * H, stop=True, recovered=T - 48 * H)]))
    assert e.result == TAKEN and not any(w.startswith(OUTAGE_WARNING) for w in e.warnings)
    [g], _ = fit([req], attach([SAT], [outage(stop=True, recovered=T - 12 * H)]))
    assert g.result == TAKEN and any(w.startswith(OUTAGE_WARNING) and "復旧" in w for w in g.warnings)
    # A request without passes at all is still 撮像機会なし.
    quiet = replace(req, start=T - 3 * H, end=T - 2 * H)
    [f], _ = fit([quiet], stopped)
    assert f.reason == NO_PASS


def test_taken_on_a_satellite_with_an_outage_carries_a_warning_once():
    req = point(1, BELOW)
    other = attach([SAT], [outage(other="画質の低下（ノイズ）", max_per_day=3)])
    [c], _ = fit([req], other)
    assert c.result == TAKEN
    warnings = [w for w in c.warnings if w.startswith(OUTAGE_WARNING)]
    assert len(warnings) == 1 and "画質の低下（ノイズ）" in warnings[0] and "1 日 3 回まで" in warnings[0]
    # Fitted again (the confirmation's changes): not stacked.
    systems = [system()]
    passes = day_passes([req], DAY, other)
    again = allocate([c], systems, other, passes, DAY)
    assert sum(w.startswith(OUTAGE_WARNING) for w in again[0].warnings) == 1


def test_satellites_counted_are_left_out_on_a_day_they_are_stopped():
    blind = replace(SAT, tle1="", tle2="", maxPerDay=5)
    req = point(1, BELOW)
    [c], _ = fit([req], attach([blind], [outage(stop=True)]))
    assert c.result != TAKEN and c.reason.startswith(OUTAGE)
    # A direction cannot be told without the orbit: taken, with the warning.
    [d], _ = fit([req], attach([blind], [outage(closed=frozenset({(True, "left")}))]))
    assert d.result == TAKEN and any(w.startswith(OUTAGE_WARNING) for w in d.warnings)
    # The day's count: two requests, one a day while the outage holds.
    two = [point(1, BELOW, 300), point(2, BELOW, 400)]
    out, _ = fit(two, attach([blind], [outage(max_per_day=1)]))
    assert [x.result == TAKEN for x in out] == [True, False]


def test_the_trial_of_limits_counts_those_held_back():
    systems_ = export_systems_of([{"system": "SYS-A", "label": "A", "kind": "光学", "series": ["OPT-X"], "limit": {"count": 5},
                                   "format": "csv", "columns": [{"key": "ID", "source": "id"}]}], [])
    sats = attach([sat("OPT-X 1", "OPT-X")], [outage(satellite="OPT-X 1", start=day_range(REQUEST_DAY)[0],
                                                       recovered=day_range(REQUEST_DAY)[1] + 1, stop=True)])
    run = simulate([request("R-1"), request("R-2")], REQUEST_DAY, 2, systems_, sats)
    assert [d.outage for d in run.days] == [2, 0] and [d.taken for d in run.days] == [0, 2]
    assert {label: f(run) for label, f in SUMMARY}["衛星の障害で見送り"] == 2


# ---- shots already handed out (撮像予定) ----------------------------------------------------------

def test_shots_handed_out_are_only_told():
    stop = outage(stop=True)
    assert handed_hit([stop], "ISS", "", "", [], T)[1] == "撮像停止"
    assert handed_hit([stop], "ISS", "", "", [], T - 48 * H) is None
    assert handed_hit([stop], "OTHER", "", "", [], T) is None
    side = outage(closed=frozenset({(True, "left")}))
    assert handed_hit([side], "ISS", "", "", [], T, "昇交", "左")[1] == "使えない方向"
    assert handed_hit([side], "ISS", "", "", [], T, "降交", "左") is None
    # Not known how it is taken: told so, not left out.
    assert handed_hit([side], "ISS", "", "", [], T)[1] == "障害中（撮り方が分からず確かめられない）"
    assert handed_hit([outage(other="画質")], "ISS", "", "", [], T) is None
    night = outage(no_night=True)
    from mikage.core.plan.orbit import sun_elevation

    expected = "夜間不可" if sun_elevation(BELOW, T) < 0 else None
    found = handed_hit([night], "ISS", "", "", [], T, "昇交", "右", 10, BELOW)
    assert (found[1] if found else None) == expected
    # The series or another name of the satellite.
    assert handed_hit([outage(satellite="WV", stop=True)], "WV-3", "", "", ["WV"], T)[1] == "撮像停止"


# ---- the table ----------------------------------------------------------------------------------

S, D, I, F = "esriFieldTypeString", "esriFieldTypeDate", "esriFieldTypeSmallInteger", "esriFieldTypeDouble"
TYPES = {
    "OUTAGE_ID": S, "SAT_NAME": S, "SENSOR": S, "SENSOR_MODE": S, "OUTAGE_REASON": S, "START_TIME": D, "EXPECTED_END": D,
    "RECOVERED_AT": D, "STOP_IMAGING": I, "NO_ASC_RIGHT": I, "NO_ASC_LEFT": I, "NO_DESC_RIGHT": I, "NO_DESC_LEFT": I,
    "LIMIT_OFF_NADIR": F, "LIMIT_PER_DAY": "esriFieldTypeInteger", "LIMIT_SCENES_PER_PASS": "esriFieldTypeInteger",
    "LIMIT_INTERVAL_MIN": F, "NO_NIGHT": I, "OTHER_IMPACT": S, "NOTE": S,
    "CREATED_USER": S, "CREATED_DATE": D, "UPDATED_USER": S, "UPDATED_DATE": D,
}


def setup(fields=None):
    fake = FakeEsri()
    layer = fake.add_layer(URL, FakeLayer(dict(fields or TYPES), "esriGeometryNull"))
    config, problems = parse_config({"satelliteOutages": URL})
    assert problems == []
    return EsriClient(fake), config.satellite_outages, layer


def test_settings_and_the_row():
    config, problems = parse_config({"satelliteOutages": {"url": URL, "fields": {"satellite": "SAT"}, "where": "1=1"}})
    assert problems == [] and config.satellite_outages.fields["satellite"] == "SAT"
    assert config.satellite_outages.fields["start"] == DEFAULT_OUTAGE_FIELDS["start"]
    assert parse_config({})[0].satellite_outages is None
    row = {"OBJECTID": 4, "OUTAGE_ID": "OUT-9", "SAT_NAME": " ICEYE-X27 ", "START_TIME": T, "STOP_IMAGING": 0,
           "NO_ASC_RIGHT": 1, "NO_DESC_LEFT": "はい", "NO_NIGHT": "true", "LIMIT_OFF_NADIR": "20", "LIMIT_PER_DAY": 2.4,
           "LIMIT_SCENES_PER_PASS": 0, "LIMIT_INTERVAL_MIN": None, "OTHER_IMPACT": "画質"}
    o = outage_of(row, DEFAULT_OUTAGE_FIELDS)
    assert (o.satellite, o.object_id, o.stop, o.no_night, o.max_off_nadir, o.max_per_day) == ("ICEYE-X27", 4, False, True, 20.0, 2)
    assert o.closed == frozenset({(True, "right"), (False, "left")}) and o.scenes_per_pass is None and o.min_interval is None
    # Without its id, satellite or start: not an outage.
    assert outage_of({**row, "START_TIME": None}, DEFAULT_OUTAGE_FIELDS) is None
    assert outage_of({**row, "SAT_NAME": ""}, DEFAULT_OUTAGE_FIELDS) is None
    # The row written and read back is the same outage.
    full = outage(sensor="SAR", mode="スポット", reason="電力", expected=T + H, recovered=T + 2 * H, stop=True,
                  closed=frozenset({(False, "right")}), max_off_nadir=25.5, max_per_day=3, scenes_per_pass=2, min_interval=12.5,
                  no_night=True, other="その他", note="メモ")
    lengths = {"OUTAGE_REASON": 3}
    written = attributes_of(full, DEFAULT_OUTAGE_FIELDS, TYPES, lengths)
    assert written["OUTAGE_REASON"] == "電力" and written["NO_DESC_RIGHT"] == 1 and written["NO_ASC_LEFT"] is None
    assert outage_of(written, DEFAULT_OUTAGE_FIELDS) == full
    # Only the attributes the table has.
    assert "NOTE" not in attributes_of(full, DEFAULT_OUTAGE_FIELDS, {k: v for k, v in TYPES.items() if k != "NOTE"}, {})


def test_saved_read_recovered_and_deleted(monkeypatch):
    monkeypatch.setenv("USERNAME", "tsudo")
    client, source, layer = setup()
    first = save_outage(client, source, outage(id=new_id(T), reason="姿勢制御", closed=frozenset({(True, "right")})))
    assert first.object_id == 1 and first.id.startswith("OUT-") and layer.rows[0]["attributes"]["CREATED_USER"] == "tsudo"
    second = save_outage(client, source, outage(id="OUT-2", satellite="WV-3", start=T + H, stop=True))
    read = load_outages(client, source)
    assert [o.id for o in read] == ["OUT-2", first.id] and read[1].closed == frozenset({(True, "right")})
    # Recovered: the same row; saved again after an answer was lost: not added twice.
    save_outage(client, source, replace(read[1], recovered=T + 3 * H))
    save_outage(client, source, replace(second, object_id=None, note="再送"))
    assert len(layer.rows) == 2
    again = {o.id: o for o in load_outages(client, source)}
    assert again[first.id].recovered == T + 3 * H and again["OUT-2"].note == "再送"
    done, failed = delete_outages(client, source, [again["OUT-2"], outage(id="OUT-x")])
    assert done == ["OUT-2"] and failed == ["ISS: 未保存"] and len(layer.rows) == 1


def test_what_cannot_be_saved_or_read():
    assert check_outage(outage(satellite=" ")) == ["衛星を選ぶ"]
    assert check_outage(outage(expected=T - 48 * H)) == ["復旧見込みは発生日時より後に"]
    assert check_outage(outage(recovered=T - 48 * H)) == ["復旧日時は発生日時より後に"]
    client, source, _layer = setup()
    with pytest.raises(ValueError):
        save_outage(client, source, outage(satellite=""))
    bare, source2, _ = setup({"OUTAGE_ID": S, "SAT_NAME": S})
    with pytest.raises(EsriError, match="START_TIME"):
        load_outages(bare, source2)


def test_texts():
    assert when_text(None) == "未定" and when_text(None, "") == ""
    assert "/" in when_text(T) and ":" in when_text(T)
    sats = attach([iss()], [outage(stop=True), outage(id="OUT-2", other="ノイズ")])
    first, last = day_range(DAY)
    assert warning_of(sats[0], first, last, T).startswith(OUTAGE_WARNING) and "；" in warning_of(sats[0], first, last, T)
    assert blocking(sats[0], T, True, "left", 10, 30, T)[1] == "撮像停止"
    assert blocking(sats[0], T - 48 * H, True, "left", 10, 30, T) is None
