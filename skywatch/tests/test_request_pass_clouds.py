"""
The weather at the passes' moments (tsudo, 2026-11-11: 「全体雲量はいいや。
撮像時刻の予報に変えてみようか。もちろん、3時間間隔とかだから、バランス取ってね」):
with the orbits an optical request's cover is taken at each of its passes
(between the forecast's steps by time), the least cloudy pass of the day
sets the order, the priority's ones digit and the hold; passes too cloudy
at their moment are not used, the less cloudy go first by tenths (a few
percent between passes minutes apart tell nothing), and what is written
is the cover at the pass taken. Without the orbits, the daylight mean.
"""
from __future__ import annotations

from dataclasses import replace

import pytest

from skywatch.core.plan.satellites import satellites_from_text
from skywatch.core.plan.weather import CloudSeries, key_of
from skywatch.core.request.daily import CLOUDY, HELD, SHOT_CLOUD, TAKEN, allocate, cloud_tip, gather
from skywatch.core.request.export import Context, raw_value
from skywatch.core.request.model import ImagingRequest, day_bounds, square_rings
from skywatch.core.request.passes import day_passes
from skywatch.core.request.simulate import simulate
from skywatch.core.request.weather import Clouds, cloud_when, daytime_cloud, places_of
from skywatch.core.request.workflow import clouds_of, history_of, orbiting, plan_day
from skywatch.tests.test_plan import BELOW, LINE1, LINE2, T, iss
from skywatch.tests.test_request_passes import DAY, along, system

H = 3600000
MIN = 60000
#: Two optical satellites over BELOW the same day: ISS at T, ISS 2 (the same orbit, later) 20 minutes on.
A = iss(swath=12, length=10, scenesPerPass=4, minInterval=1)
B = replace(satellites_from_text("ISS 2\n" + LINE1.replace("25544U", "25545U") + "\n"
                                 + LINE2.replace("2 25544", "2 25545").replace("247.4627", "252.4728").replace("325.0288", "247.5288"),
                                 45, 0, 10)[0], swath=12, length=10, scenesPerPass=4, minInterval=1)
SATS = [A, B]


def optical(i: int, where=BELOW, priority: int = 300, **kw) -> ImagingRequest:
    """An optical point request every day (毎回: no raise offered near its end)."""
    req = ImagingRequest(id=f"R{i}", name=f"点{i}", priority=priority, kind="光学", achieve="毎回", start=T - 6 * H, end=T + 6 * H,
                         shape_kind="点", rings=square_rings(where[0], where[1], 0.1))
    return replace(req, **kw)


def sky(reqs, steps) -> dict:
    """Forecasts over the requests' places: `steps` [(ms, percent)], or a function of the place giving them."""
    out = {}
    for r in reqs:
        for place in places_of(r):
            got = steps(place) if callable(steps) else steps
            out[key_of(place)] = CloudSeries([t for t, _ in got], [c for _, c in got])
    return out


def day_of(reqs, forecasts, sats=SATS, systems=None, clouds=None):
    systems = systems or [system()]
    passes = day_passes(reqs, DAY, sats)
    clouds = clouds if clouds is not None else clouds_of(reqs, DAY, forecasts, passes=passes, sats=orbiting(systems, sats))
    return plan_day(reqs, DAY, systems, sats, clouds, passes=passes), passes, clouds


def by_id(plan):
    return {c.request.id: c for c in plan.candidates}


def test_between_the_steps_by_time_over_the_places():
    # Three hours apart: a moment takes both steps, the nearer the more.
    series = {key_of(BELOW): CloudSeries([T - 90 * MIN, T + 90 * MIN], [20.0, 80.0])}
    req = optical(1)
    assert places_of(req) == [BELOW]
    assert cloud_when(req, series, T) == pytest.approx(50)
    assert cloud_when(req, series, T + 20 * MIN) == pytest.approx(50 + 60 * 20 / 180)
    assert cloud_when(req, series, T + 3 * H) is None
    # An area: the mean of its grid points at that moment.
    area = replace(optical(2), shape_kind="範囲", rings=square_rings(BELOW[0], BELOW[1], 70))
    points = places_of(area)
    assert len(points) > 1
    forecasts = {key_of(p): CloudSeries([T - H, T + H], [10.0 * i, 10.0 * i]) for i, p in enumerate(points)}
    assert cloud_when(area, forecasts, T) == pytest.approx(sum(10.0 * i for i in range(len(points))) / len(points))


def test_each_pass_told_its_own_and_the_least_cloudy_counts():
    req = optical(1)
    # Clearing fast between T and T + 30 min (an hourly forecast): ISS at 70 %, ISS 2 at 34 %.
    plan, passes, clouds = day_of([req], sky([req], [(T - H, 70.0), (T, 70.0), (T + 30 * MIN, 16.0)]))
    by_sat = {o.satellite: o.cloud for o in passes.of(req, DAY)}
    assert by_sat[0] == pytest.approx(70, abs=0.1) and by_sat[1] == pytest.approx(34, abs=0.1)
    assert isinstance(clouds, Clouds) and clouds.timed == {"R1"} and clouds["R1"] == pytest.approx(34, abs=0.1)
    [c] = plan.candidates
    # The order and the ones digit by the least cloudy pass; taken on it, on the other satellite.
    assert c.timed and c.priority == 303 and c.result == TAKEN and c.satellite.name == "ISS 2"
    assert c.shot_cloud == pytest.approx(34, abs=0.1) and c.forecast == c.shot_cloud
    assert abs(c.planned - (T + 20 * MIN)) < MIN
    assert cloud_tip(c).startswith("撮像時刻 22:20 の予報（前後の予報を時刻の近さで按分")


def test_passes_minutes_apart_in_one_tenth_are_not_told_apart():
    req = optical(1)
    # Three-hourly: 47 % at T, 41 % twenty minutes on; the same tenth, so the satellites' own order (ISS first) stands.
    plan, _, clouds = day_of([req], sky([req], [(T - 90 * MIN, 74.0), (T + 90 * MIN, 20.0)]))
    [c] = plan.candidates
    assert clouds["R1"] == pytest.approx(41, abs=0.1)
    assert c.satellite.name == "ISS" and c.shot_cloud == pytest.approx(47, abs=0.1)
    # The digit is the day's least cloudy pass (4); what is written is the pass taken (47 %).
    assert c.priority == 304
    ctx = Context(plan.systems[0], DAY, T)
    assert raw_value(c, "cloudForecast", ctx) == 47 and raw_value(c, "weatherFlag", ctx) == "OK"
    [row] = history_of(plan, T, {"SYS": "a.csv"}, "RUN")
    assert row.cloud == 47.0
    assert "この日いちばん雲の少ないパスは 41%" in cloud_tip(c)


def test_every_pass_too_cloudy_is_held_back_at_their_moment():
    req = optical(1, cloud_no_go=50)
    plan, _, _ = day_of([req], sky([req], [(T - H, 88.0), (T + H, 58.0)]))
    [c] = plan.candidates
    assert c.result == HELD and c.reason.startswith("雲量 ") and "撮像時刻の予報でどのパスも 50% 以上" in c.reason
    # Near its deadline it goes all the same, with the warning, on its least cloudy pass.
    first, last = day_bounds(DAY)
    near = optical(1, cloud_no_go=50, achieve="期間内1回", end=last)
    plan, _, _ = day_of([near], sky([near], [(T - H, 88.0), (T + H, 58.0)]))
    [c] = plan.candidates
    assert c.result == TAKEN and c.satellite.name == "ISS 2" and any("期限間近のため出力" in w for w in c.warnings)


def test_a_pass_too_cloudy_at_its_moment_is_not_used():
    req = optical(1, cloud_no_go=50, satellites=["ISS"])
    passes = day_passes([req], DAY, SATS)
    # The day's cover says 30 %, but the only pass of ISS is at 70 %.
    cands = gather([req], DAY, SATS, Clouds({"R1": 30.0}, timed=["R1"]), set())
    for o in passes.of(req, DAY):
        o.cloud = 70.0 if o.satellite == 0 else 30.0
    [c] = allocate(cands, [system()], SATS, passes, DAY)
    assert c.result == HELD and c.reason == CLOUDY
    # Taken by hand it goes on it, saying so.
    c.manual = TAKEN
    [c] = allocate([c], [system()], SATS, passes, DAY)
    assert c.result == TAKEN and c.shot_cloud == 70.0
    assert any(w.startswith(SHOT_CLOUD) and "手動で採用" in w for w in c.warnings)
    # Fitted again, the warning is made again, not doubled.
    [c] = allocate([c], [system()], SATS, passes, DAY)
    assert sum(w.startswith(SHOT_CLOUD) for w in c.warnings) == 1


def test_not_put_into_a_shot_too_cloudy_over_its_own_area():
    # 点2, 34 km on along the track (another grid cell), would ride 点1's shot on ISS; but over 点2 it is cloudy then
    # and clear 20 minutes on.
    a, b = optical(1, priority=200), optical(2, along(5), cloud_no_go=50, satellites=["ISS", "ISS 2"])

    def steps(place):
        if key_of(place) == key_of(places_of(b)[0]):
            return [(T - H, 80.0), (T, 80.0), (T + 30 * MIN, 10.0)]
        return [(T - H, 10.0), (T + H, 10.0)]

    plan, _, _ = day_of([a, b], sky([a, b], steps))
    c = by_id(plan)
    assert c["R1"].result == c["R2"].result == TAKEN
    assert not c["R1"].merged and not c["R2"].merged
    assert c["R1"].satellite.name == "ISS" and c["R2"].satellite.name == "ISS 2"
    # Without the weather the two are one shot.
    plan, _, _ = day_of([a, b], {})
    c = by_id(plan)
    assert c["R2"].member and c["R2"].lead is c["R1"]


def test_without_the_orbits_the_daylight_mean():
    req = optical(1)
    forecasts = sky([req], [(day_bounds(DAY)[0] - 12 * H, 20.0), (day_bounds(DAY)[1] + 12 * H, 20.0)])
    systems = [system(orbit=False)]
    plan, passes, clouds = day_of([req], forecasts, systems=systems)
    [c] = plan.candidates
    assert clouds.timed == set() and clouds["R1"] == pytest.approx(daytime_cloud(req, forecasts, DAY))
    assert all(o.cloud is None for o in passes.of(req, DAY))
    assert c.result == TAKEN and not c.timed and c.shot_cloud is None and c.forecast == pytest.approx(20)
    assert cloud_tip(c).startswith("日中（地方時 9〜15 時）の平均")
    # SAR asks no weather.
    sar = replace(req, kind="SAR")
    assert cloud_tip(gather([sar], DAY, SATS, {}, set())[0]) == ""


def test_the_simulation_counts_held_back_at_the_passes_moments():
    req = optical(1, cloud_no_go=50, start=day_bounds(DAY)[0], end=day_bounds(DAY)[1])
    forecasts = sky([req], [(T - H, 88.0), (T + H, 58.0)])
    passes = day_passes([req], DAY, SATS)
    used = orbiting([system()], SATS)
    run = simulate([req], DAY, 1, [system()], SATS, lambda day: clouds_of([req], day, forecasts, passes=passes, sats=used), passes=passes)
    assert run.days[0].cloud == 1 and run.days[0].taken == 0
