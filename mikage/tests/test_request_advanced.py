"""
撮像の高度化 (tsudo, 2026-11-12: 「撮像の高度化の観点から…シンプルだけど奥
深いツールにしたい」; picked 1・4・5・6・7・10 of the proposals):

1. A request of either kind (任意) that optical and SAR satellites can both
   take weighs the clouds on its optical passes only: a cloudy day goes to SAR.
"""
from __future__ import annotations

from dataclasses import replace

import pytest

from mikage.core.request.daily import ANY_KIND_NO_GO, HELD, TAKEN, allocate, cloud_tip, gather, kind_text, mixed_kinds
from mikage.core.request.export import Context, raw_value
from mikage.core.request.model import ImagingRequest, square_rings
from mikage.core.request.passes import day_passes
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
