"""
撮像の高度化 (tsudo, 2026-11-12: 「撮像の高度化の観点から…シンプルだけど奥
深いツールにしたい」; picked 1・4・5・6・7・10 of the proposals):

1. A request of either kind (任意) that optical and SAR satellites can both
   take weighs the clouds on its optical passes only: a cloudy day goes to SAR.
4. A 期間内1回 request's chances left (the passes of the next days, the
   clouds at their moments): with one left it is near its deadline whatever
   the days, first in its hundred; a cloudy day with a much clearer pass
   ahead puts it last there.
5. A 定点 request is taken as its last image was where it can be: that
   satellite first, then the same direction and side, the incidence closest.
6. A yearly quota ahead of its pace: a low priority request tries the other
   modes first; the quotas dialog shows the pace.
7. The request form's one line: how often the satellites can take it.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, replace
from datetime import timedelta

import pytest

from mikage.core.plan.weather import CloudSeries, key_of
from mikage.core.request.chances import Chances, chances_of, look_ahead_days, wanted
from mikage.core.request.daily import (
    ANY_KIND_NO_GO, HELD, OVER, TAKEN, allocate, chance_text, cloud_tip, gather, kind_text, mixed_kinds, ordered,
)
from mikage.core.request.export import Context, raw_value
from mikage.core.request.model import ImagingRequest, day_bounds, square_rings
from mikage.core.request.passes import Passes, day_passes
from mikage.core.request.store import request_store_of
from mikage.core.request.weather import places_of
from mikage.core.request.workflow import clouds_of, forecast_places, orbiting, plan_day, radar_of
from mikage.tests.test_plan import BELOW, T
from mikage.tests.test_request_pass_clouds import A, B, H, MIN, sky
from mikage.tests.test_request_passes import DAY, system

#: ISS optical (fine) at T, ISS 2 a SAR satellite (coarser) 20 minutes on.
OPT = replace(A, resolution=0.5)
SAR = replace(B, sar=True, resolution=3.0)
MIXED = [OPT, SAR]


def either(i: int = 1, **kw) -> ImagingRequest:
    """A point request of either kind (任意) every day."""
    req = ImagingRequest(id=f"R{i}", name=f"点{i}", priority=300, kind="任意", achieve="毎回", start=T - 6 * H, end=T + 6 * H,
                         shape_kind="点", rings=square_rings(BELOW[0], BELOW[1], 0.1))
    return replace(req, **kw)


def plan_of(reqs, forecasts, sats=MIXED, systems=None):
    systems = systems or [system()]
    passes = day_passes(reqs, DAY, sats)
    clouds = clouds_of(reqs, DAY, forecasts, passes=passes, sats=orbiting(systems, sats), radar=radar_of(sats))
    return plan_day(reqs, DAY, systems, sats, clouds, passes=passes), passes, clouds


# --- 1. Either kind: optical when clear, SAR when cloudy ---------------------------------------------

def test_either_kind_with_both_satellites_is_weighed_on_its_optical_passes_only():
    req = either()
    assert mixed_kinds(req, MIXED) and not mixed_kinds(req, [OPT]) and not mixed_kinds(replace(req, kind="光学"), MIXED)
    # Its forecast is asked for (before: not at all, so it went to the finer optical satellite under any cloud).
    assert forecast_places([req], DAY, MIXED) == places_of(req)
    plan, passes, clouds = plan_of([req], sky([req], [(T - H, 70.0), (T + H, 70.0)]))
    # The SAR pass is told no cover; the request's cover is that of its optical pass.
    assert {o.satellite: o.cloud for o in passes.of(req, DAY)}[1] is None
    assert clouds["R1"] == pytest.approx(70)
    [c] = plan.candidates
    assert c.mixed and not c.weather and c.result == TAKEN and c.satellite.name == "ISS 2"
    # Its priority is not touched by the weather, and a SAR shot writes no cover.
    assert c.priority == 300 and c.forecast is None and c.weather_flag == ""
    assert kind_text(c) == "光学は雲量 70% のため SAR"
    assert "種別「任意」" in cloud_tip(c)


def test_either_kind_on_a_clear_day_takes_the_finer_optical_satellite():
    req = either()
    plan, _, _ = plan_of([req], sky([req], [(T - H, 20.0), (T + H, 20.0)]))
    [c] = plan.candidates
    assert c.result == TAKEN and c.satellite.name == "ISS" and kind_text(c) == ""
    assert c.forecast == pytest.approx(20) and c.weather_flag == "OK"
    ctx = Context(plan.systems[0], DAY, T)
    assert raw_value(c, "cloudForecast", ctx) == 20


def test_either_kind_uses_its_own_no_go_else_the_default():
    req = either(cloud_no_go=80)
    plan, _, _ = plan_of([req], sky([req], [(T - H, 70.0), (T + H, 70.0)]))
    # 70 % is under its own 80 %: optical as before.
    assert plan.candidates[0].satellite.name == "ISS"
    assert ANY_KIND_NO_GO == 50


def test_either_kind_without_a_forecast_or_without_sar_goes_as_before():
    req = either()
    plan, _, _ = plan_of([req], {})
    [c] = plan.candidates
    assert c.mixed and c.result == TAKEN and c.satellite.name == "ISS"
    # Only optical satellites: the weather counts as for an optical request (held back when every pass is too cloudy).
    only = either(cloud_no_go=50)
    plan, _, _ = plan_of([only], sky([only], [(T - H, 70.0), (T + H, 70.0)]), sats=[OPT, replace(SAR, sar=False)])
    [c] = plan.candidates
    assert c.weather and not c.mixed and c.result == HELD


def test_either_kind_cloudy_with_no_sar_room_is_held_back_for_the_clouds():
    req = either()
    plan, _, _ = plan_of([req], sky([req], [(T - H, 70.0), (T + H, 70.0)]), sats=[OPT, replace(SAR, maxPerDay=1)])
    [c] = plan.candidates
    assert c.satellite.name == "ISS 2"
    # Another request already on the SAR satellite (1 a day, not put together): the cloudy optical pass is not used.
    other = either(2, kind="SAR", priority=100)
    alone = [system(merge=False)]
    plan, _, _ = plan_of([req, other], sky([req], [(T - H, 70.0), (T + H, 70.0)]), sats=[OPT, replace(SAR, maxPerDay=1)], systems=alone)
    got = {c.request.id: c for c in plan.candidates}
    assert got["R2"].result == TAKEN and got["R1"].result == HELD
    # Near its deadline it goes all the same, on the optical pass, with a warning.
    near = either(achieve="期間内1回")
    plan, _, _ = plan_of([near, other], sky([near], [(T - H, 70.0), (T + H, 70.0)]), sats=[OPT, replace(SAR, maxPerDay=1)],
                         systems=alone)
    got = {c.request.id: c for c in plan.candidates}
    assert got["R1"].result == TAKEN and got["R1"].satellite.name == "ISS"
    assert any(w.startswith("撮像時刻の雲量 70%") for w in got["R1"].warnings)


def test_either_kind_counted_without_orbits_goes_by_the_days_cover():
    req = either()
    flat = system(orbit=False)
    clouds = clouds_of([req], DAY, sky([req], [(T - 24 * H, 75.0), (T + 24 * H, 75.0)]))
    cands = allocate(gather([req], DAY, MIXED, clouds, set()), [flat], MIXED)
    [c] = cands
    assert c.mixed and c.result == TAKEN and c.satellite.name == "ISS 2" and kind_text(c) == "光学は雲量 75% のため SAR"


# --- 4. Chances left ----------------------------------------------------------------------------------

D = 86400000


@dataclass
class Op:
    """Just what the chances look at of a pass."""
    satellite: int
    time: float


def once(i: int = 1, days: int = 4, **kw) -> ImagingRequest:
    """An optical 期間内1回 point request from T's day, `days` days on."""
    first, _ = day_bounds(DAY)
    _, last = day_bounds(DAY + timedelta(days=days))
    req = ImagingRequest(id=f"R{i}", name=f"点{i}", priority=350, kind="光学", achieve="期間内1回", start=first, end=last,
                         shape_kind="点", rings=square_rings(BELOW[0], BELOW[1], 0.1))
    return replace(req, **kw)


def md(d) -> str:
    return f"{d.month}/{d.day}"


def passes_of(req, times) -> Passes:
    """Passes of the request at `times` [(satellite index, ms)]."""
    return Passes(ops={req.id: [Op(i, t) for i, t in times]}, targets={req.id: object()})


def test_chances_are_the_passes_after_the_day_to_the_deadline():
    req = once(days=3)
    two = [OPT, replace(OPT, mode="モード 2")]
    got = passes_of(req, [(0, T), (0, T + D), (1, T + D), (0, T + 5 * D)])
    ch = chances_of([req], DAY, got, got, two)["R1"]
    # The day's pass; after it one (two modes of one satellite at one moment), the one after the deadline not counted.
    assert ch == Chances(today=1, left=1, until=DAY + timedelta(days=3), whole=True)
    assert ch.scarce
    # A deadline past the days looked at: not all its chances are known, so never scarce.
    far = once(days=10)
    ch = chances_of([far], DAY, passes_of(far, [(0, T), (0, T + D)]), passes_of(far, [(0, T), (0, T + D)]), two, days=7)["R1"]
    assert not ch.whole and not ch.scarce and ch.until == DAY + timedelta(days=7)
    # No pass on the day: nothing to put first.
    assert not chances_of([req], DAY, passes_of(req, [(0, T + D)]), passes_of(req, [(0, T + D)]), two)["R1"].scarce
    # Satellites without known orbits: no chances worked out (the days count as before).
    assert chances_of([req], DAY, Passes(), Passes(), two) == {}


def test_too_cloudy_passes_ahead_are_no_chance_and_a_clearer_one_is_told():
    req = once(days=3, cloud_no_go=50)
    got = passes_of(req, [(0, T), (0, T + D), (0, T + 2 * D)])
    forecasts = {key_of(p): CloudSeries([T - D, T + 1.5 * D, T + 1.6 * D, T + 3 * D], [70.0, 80.0, 10.0, 10.0]) for p in places_of(req)}
    ch = chances_of([req], DAY, got, got, [OPT], forecasts, clouds={"R1": 70.0})["R1"]
    # The next day's pass is at 80 % (no chance); the one after at 10 %: one chance, 6 tenths clearer than today.
    assert ch.left == 1 and ch.scarce
    assert ch.clearer[0] == DAY + timedelta(days=2) and ch.clearer[1] == pytest.approx(10)
    # SAR passes are chances whatever the clouds.
    either_ = once(days=3, kind="任意")
    sar = passes_of(either_, [(0, T), (1, T + D), (1, T + 2 * D)])
    assert chances_of([either_], DAY, sar, sar, MIXED, forecasts)["R1"].left == 2


def test_only_the_requests_that_need_it_are_looked_at():
    reqs = [once(1), once(2, achieve="毎回"), once(3, status="停止"), once(4, days=0)]
    assert [r.id for r in wanted(reqs, DAY)] == ["R1"]
    problems = []
    store = request_store_of({"url": "https://h/arcgis/rest/services/x/FeatureServer/0"}, problems, 0)
    assert store.look_ahead_days == 7 and store.same_geometry and problems == []
    store = request_store_of({"url": "https://h/arcgis/rest/services/x/FeatureServer/0", "lookAheadDays": 0, "sameGeometry": False},
                             problems, 0)
    assert store.look_ahead_days == 0 and not store.same_geometry and problems == []
    request_store_of({"url": "https://h/arcgis/rest/services/x/FeatureServer/0", "lookAheadDays": 40, "sameGeometry": "yes"}, problems, 0)
    assert len(problems) == 2 and look_ahead_days(40) == 16


def test_scarce_is_near_its_deadline_and_first_in_its_hundred():
    reqs = [once(1, priority=350), once(2, priority=360, cloud_no_go=50), once(3, priority=340), once(4, priority=250)]
    left = {"R2": Chances(1, 1, DAY + timedelta(days=4), True),
            "R3": Chances(1, 5, DAY + timedelta(days=4), True, (DAY + timedelta(days=1), 5.0)),
            "R4": Chances(1, 5, DAY + timedelta(days=4), True, (DAY + timedelta(days=1), 5.0))}
    cands = gather(reqs, DAY, [OPT], {"R1": 40.0, "R2": 75.0, "R3": 70.0, "R4": 70.0}, set(), chances=left)
    got = {c.request.id: c for c in cands}
    # One chance left: near its deadline (4 days away): goes whatever the clouds (75 % over its 50 %), raise offered.
    assert got["R2"].scarce and got["R2"].near and not got["R2"].held
    assert got["R2"].boost_offer == 100 and got["R2"].boost_reason == f"残りの撮像機会 1 回（{md(DAY + timedelta(days=4))} まで）"
    assert any("期限間近のため出力" in w for w in got["R2"].warnings)
    # Cloudy today with a much clearer pass ahead: last in its hundred.
    assert got["R3"].later and not got["R1"].later
    for c in cands:
        c.boost_taken = False
    # R4 is in the hundred above: it stays first whatever its chances; then R2 (scarce), R1, R3 (clearer later).
    assert [c.request.id for c in ordered(cands)] == ["R4", "R2", "R1", "R3"]


def test_one_waiting_for_a_clearer_day_gives_way_when_room_is_short():
    reqs = [once(1, priority=340), once(2, priority=350)]
    left = {"R1": Chances(1, 5, DAY + timedelta(days=4), True, (DAY + timedelta(days=1), 5.0))}
    flat = system(orbit=False, limit={"count": 1})
    cands = allocate(gather(reqs, DAY, [OPT], {"R1": 70.0, "R2": 40.0}, set(), chances=left), [flat], [OPT])
    got = {c.request.id: c for c in cands}
    assert got["R2"].result == TAKEN and got["R1"].result == HELD and got["R1"].reason == OVER
    assert chance_text(got["R1"]) == f"{md(DAY + timedelta(days=1))} のほうが晴れる見込み（雲 5%）"
    # With room for both, it goes today all the same.
    cands = allocate(gather(reqs, DAY, [OPT], {"R1": 70.0, "R2": 40.0}, set(), chances=left), [system(orbit=False)], [OPT])
    assert all(c.result == TAKEN for c in cands) and chance_text(cands[-1]) == ""


def test_the_day_planned_with_the_passes_ahead():
    # End to end on the orbits: the chances of the next days worked out from the satellites' passes.
    from mikage.core.request.chances import ahead_span
    from mikage.core.request.passes import find_passes

    req = once(days=3)
    sats = [OPT]
    today = day_passes([req], DAY, sats)
    first, last = ahead_span(DAY, 7)
    ahead = find_passes([req], first, last, sats)
    ch = chances_of([req], DAY, today, ahead, sats)["R1"]
    assert ch.today >= 1 and ch.whole and ch.until == DAY + timedelta(days=3)
    assert ch.left == len({round(o.time / 60000) for o in ahead.ops["R1"] if o.time <= req.end})
    plan = plan_day([req], DAY, [system()], sats, {}, passes=today, chances={"R1": ch})
    assert plan.candidates[0].chances is ch


# --- 5. 定点: as the last image was taken ----------------------------------------------------------------

def fixed(i: int = 1, **kw) -> ImagingRequest:
    """A 定点 point request below the satellites, all day."""
    first, last = day_bounds(DAY)
    req = ImagingRequest(id=f"R{i}", name=f"点{i}", priority=300, kind="光学", category="定点", achieve="毎回", start=first, end=last,
                         shape_kind="点", rings=square_rings(BELOW[0], BELOW[1], 0.1))
    return replace(req, **kw)


def looked(req, look, sats=(A, B)):
    sats = list(sats)
    passes = day_passes([req], DAY, sats)
    plan = plan_day([req], DAY, [system(merge=False)], sats, {}, passes=passes, last_looks={req.id: look} if look else None)
    return plan.candidates[0]


def test_the_last_satellite_goes_first_then_a_pass_taken_the_same_way():
    from mikage.core.request.geometry import LastLook, look_text

    req = fixed()
    # As before: ISS (the first of two alike), its pass looking left.
    c = looked(req, None)
    assert c.satellite.name == "ISS" and c.shots[0].side == "左" and look_text(c) == ""
    # Last taken by ISS 2: ISS 2 first, and the note says nothing (taken the same way).
    c = looked(req, LastLook("2026-10-01", "ISS 2", "", "昇交", "右", 0.5))
    assert c.result == TAKEN and c.satellite.name == "ISS 2" and look_text(c) == ""
    # Its satellite gone: the one with a pass looking the same way (右) before the other.
    c = looked(req, LastLook("2026-10-01", "OLD-1", "", "昇交", "右", 0.5))
    assert c.satellite.name == "ISS 2" and look_text(c) == "前回（10/1）と違う撮り方（OLD-1 → ISS 2）"
    # The satellite named first by the request keeps its place (the hand's order wins).
    c = looked(replace(req, satellites=["ISS", "ISS 2"]), LastLook("2026-10-01", "ISS 2"))
    assert c.satellite.name == "ISS"
    # No pass of the day taken that way: taken all the same, the difference said.
    c = looked(req, LastLook("2026-10-01", "ISS", "", "降交", "左", 30.0))
    assert c.result == TAKEN and c.satellite.name == "ISS"
    assert look_text(c) == "前回（10/1）と違う撮り方（降交・入射角 30° → 昇交・入射角 0°）"


def test_mismatch_and_satellite_rank():
    from mikage.core.request.geometry import LastLook, Look, mismatch, same, satellite_rank

    last = LastLook("2026-11-01", "SAR-1", "ストリップ", "降交", "右", 35.0)
    assert mismatch(last, Look(False, "右", 37.0)) == (0, 0)
    assert mismatch(last, Look(False, "右", 39.2)) == (0, 5)
    assert mismatch(last, Look(True, "左", 35.0)) == (2, 0)
    assert mismatch(None, Look(True, "左", 35.0)) == (0, 0)
    # What the last shot does not tell is not compared.
    assert mismatch(LastLook("2026-11-01", "SAR-1"), Look(True, "左", 60.0)) == (0, 0)
    sar = replace(SAR, name="SAR-1", mode="ストリップ")
    assert [satellite_rank(last, s) for s in (sar, replace(sar, mode="スポット"), SAR)] == [0, 1, 2]
    assert satellite_rank(None, sar) == 2 and satellite_rank(LastLook("2026-11-01", "SAR-1"), replace(sar, mode="スポット")) == 0
    assert same(last, sar, Look(False, "右", 33.0)) and not same(last, SAR, Look(False, "右", 35.0))


def test_the_last_looks_are_read_from_the_history_and_the_shots():
    from mikage.core.esri import EsriClient
    from mikage.core.request.geometry import LastLook, read_last_looks
    from mikage.tests.test_request_quota import SHOTS, _history, _services

    fake, store = _services()
    fake.layers[SHOTS].types.update(INCIDENCE="esriFieldTypeDouble", RUN_DATE="esriFieldTypeDate")
    before = [(DAY - timedelta(days=n)).isoformat() for n in (1, 3, 200)]
    _history(fake, "R1", "S1", captured="撮像済み", day=before[1])
    _history(fake, "R1", "S2", captured="撮像済み", day=before[0])
    # Not taken, from too long ago, or of a 臨時 request: not looked at.
    _history(fake, "R1", "S3", captured="未撮像", day=before[0])
    _history(fake, "R2", "S4", captured="撮像済み", day=before[2])
    _history(fake, "R3", "S1", captured="撮像済み", day=before[1])
    for sid, day, sat, d, side, inc in (("S1", before[1], "ISS", "昇交", "左", 12.5), ("S2", before[0], "ISS 2", "降交", "右", 30.0),
                                        ("S3", before[0], "ISS", "昇交", "右", 1.0), ("S4", before[2], "ISS", "昇交", "右", 1.0)):
        fake.layers[SHOTS].add({"SHOT_ID": sid, "RUN_ID": "RUN", "TARGET_DAY": day, "SATELLITE": sat, "ORBIT_DIR": d, "LOOK_SIDE": side,
                                "INCIDENCE": inc, "SENSOR_MODE": ""})
    reqs = [fixed(1), fixed(2), fixed(3, category="臨時", achieve="期間内1回")]
    client = EsriClient(fake)
    looks = read_last_looks(client, store, None, reqs, DAY)
    assert looks == {"R1": LastLook(before[0], "ISS 2", "", "降交", "右", 30.0)}
    # Off, or without a shot layer: nothing is asked.
    assert read_last_looks(client, replace(store, same_geometry=False), None, reqs, DAY) == {}
    assert read_last_looks(client, replace(store, shots=None), None, reqs, DAY) == {}


def test_same_geometry_in_the_config():
    problems: list = []
    assert request_store_of({"url": "https://x/FeatureServer/0"}, problems, 0).same_geometry is True
    assert request_store_of({"url": "https://x/FeatureServer/0", "sameGeometry": False}, problems, 0).same_geometry is False
    assert problems == []
    request_store_of({"url": "https://x/FeatureServer/0", "sameGeometry": "no"}, problems, 0)
    assert problems and "sameGeometry" in problems[0]


# --- 6. Yearly quotas: the pace ---------------------------------------------------------------------------

def _usage(limit=100, used=0, pending=0, sensor="ISS", day=None):
    from mikage.core.request.quota import Quota, Usage, window_of

    q = Quota(sensor=sensor, limit=limit)
    return Usage(q, window_of(day or DAY), used=used, pending=pending)


def test_the_pace_is_the_share_used_over_the_share_of_the_year_gone():
    from datetime import date

    u = _usage(limit=365, used=100)
    mid = date(DAY.year, 7, 2)  # day 183 of 365
    assert u.elapsed(mid) == pytest.approx(183 / 365)
    assert u.pace(mid) == pytest.approx((100 / 365) / (183 / 365))
    assert not u.ahead(mid) and u.ahead(mid, spent=120)
    # The first month tells no pace: it counts as a month gone.
    assert u.pace(date(DAY.year, 1, 2)) == pytest.approx((100 / 365) * 12)
    # 100 in 183 days: the 365 reached in 668 days, past the year (none said).
    assert u.runs_out(mid) is None
    assert u.runs_out(mid, spent=150) == date(DAY.year, 1, 1) + timedelta(days=math.ceil(365 / (250 / 183)) - 1)
    assert u.pace_text(mid, 150).startswith("年の 50% が過ぎて上限の 68% を使用（見込み込み）・このままだと ")
    assert _usage(limit=None).pace(mid) is None and _usage(limit=None).pace_text(mid) == ""


def test_a_low_priority_request_tries_the_modes_on_their_pace_first():
    from mikage.core.request.daily import pace_text
    from mikage.core.request.quota import QuotaBook

    req = fixed(category="臨時")
    passes = day_passes([req], DAY, [A, B])
    # ISS has used 90 of 100 by October: ahead of its pace.
    usage = _usage(limit=100, used=90)
    assert usage.ahead(DAY)

    def plan(priority, day=DAY):
        book = QuotaBook([usage], indices=[0], day=day)
        p = plan_day([replace(req, priority=priority)], DAY, [system(merge=False)], [A, B], {}, passes=passes, quotas=book)
        return p.candidates[0]

    low = plan(700)
    assert low.result == TAKEN and low.satellite.name == "ISS 2"
    assert pace_text(low) == "ISS（全モード）は年間上限のペースより先行のため後回し"
    # 中 and above: as before (the finer, or here the first, still has room).
    high = plan(500)
    assert high.satellite.name == "ISS" and pace_text(high) == ""
    # Without the day (a book of the limits only): no pace.
    assert plan(700, day=None).satellite.name == "ISS"


# --- 7. The request form's chances --------------------------------------------------------------------------

def test_the_form_line_counts_the_passes_ahead():
    from mikage.core.request.chances import FORM_DAYS, form_span, form_text
    from mikage.core.request.passes import find_passes

    first, _ = day_bounds(DAY)
    req = replace(fixed(category="臨時", achieve="期間内1回"), start=first, end=first + 3 * 86400000)
    now = first
    span = form_span(req, now)
    assert span == (first, req.end, True)
    passes = find_passes([req], span[0], span[1], [A, B], now)
    n = len({(o.satellite, round(o.time / 60000)) for o in passes.ops["R1"]})
    text, red = form_text(req, passes, [A, B], span)
    assert n >= 2 and not red
    assert text.startswith(f"撮像機会: {(DAY + timedelta(days=3)).month}/{(DAY + timedelta(days=3)).day} までに {n} 回（最初 ")
    assert text.endswith(" ISS）") or text.endswith(" ISS 2）")
    # A long period: the next FORM_DAYS days only.
    long = replace(req, end=first + 60 * 86400000)
    assert form_span(long, now) == (first, first + FORM_DAYS * 86400000, False)
    # No satellite can take it as written, or no pass over it: red.
    fine = replace(req, kind="SAR")
    assert form_text(fine, find_passes([fine], *span[:2], [A, B], now), [A, B], span) == ("撮像機会なし（この条件で撮れる衛星なし）", True)
    empty = Passes(known=frozenset({0, 1}), ops={"R1": []})
    assert form_text(req, empty, [A, B], span) == (f"撮像機会なし（{(DAY + timedelta(days=3)).month}/{(DAY + timedelta(days=3)).day} まで、"
                                                   "範囲の上を通るパスなし）", True)
    # A 定点 request counts the days of its repeat only.
    weekly = replace(req, category="定点", repeat="曜日:" + "月火水木金土日"[(DAY + timedelta(days=5)).weekday()], end=first + 4 * 86400000)
    text, red = form_text(weekly, find_passes([weekly], first, weekly.end, [A, B], now), [A, B], form_span(weekly, now))
    assert text.startswith("撮像機会なし") and red
    # Its period over, or no orbit known.
    assert form_text(req, None, [A, B], form_span(req, req.end + 1)) == ("撮像機会: 期間が終わっている", False)
    assert form_text(req, Passes(), [A, B], span)[1] is False
