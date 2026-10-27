"""The viewer's imaging-plan.test.ts and sensor-modes.test.ts, ported."""
import math
from dataclasses import replace

import pytest

from satellite_observation.core.config import parse_config
from satellite_observation.core.plan.access import PlanOptions, PlanTarget, plan_access, tle_epoch
from satellite_observation.core.plan.cover import cover_plan
from satellite_observation.core.plan.modes import SensorMode, sample_sensor_modes, sensor_catalog_of, sensor_mode_of, with_modes
from satellite_observation.core.plan.orbit import SGP4_ACCELERATED, ground_range, sun_elevation
from satellite_observation.core.plan.satellites import (
    look_side_of, platform_key, sample_satellites, satellite_catalog_of, satellite_label, satellite_of, satellites_from_text,
    series_of, tle_lines,
)
from satellite_observation.core.plan.scene import pass_shapes, scenes_taken, shift_scene, sub_satellite_point

# An ISS-like orbit (51.6°, about 420 km) with its epoch on 2026-10-07 12:00 UTC.
LINE1 = "1 25544U 98067A   26280.50000000  .00016717  00000-0  10270-3 0  9994"
LINE2 = "2 25544  51.6416 247.4627 0006703 130.5360 325.0288 15.49815399432717"
EPOCH = 1767225600000 + 279.5 * 86400000
DAY = 86400000
T = EPOCH + 3600000


def iss(**spec):
    return replace(satellites_from_text(f"ISS\n{LINE1}\n{LINE2}", 45, 0, 10)[0], **spec)


def around(t, minutes=30, **kw):
    return PlanOptions(t - minutes * 60000, t + minutes * 60000, None, "none", 10, **kw)


def east(p, km):
    lon, lat = p
    return (lon + km / (111.32 * math.cos(lat * math.pi / 180)), lat)


def box_target(label, center, w, h):
    lon, lat = center
    d_lon = w / (111.32 * math.cos(lat * math.pi / 180))
    d_lat = h / 111.32
    cells = [(lon - d_lon / 2 + d_lon * (i + 0.5) / 20, lat - d_lat / 2 + d_lat * (j + 0.5) / 20) for i in range(20) for j in range(20)]
    points = [(lon - d_lon / 2, lat - d_lat / 2), (lon + d_lon / 2, lat - d_lat / 2), (lon + d_lon / 2, lat + d_lat / 2), (lon - d_lon / 2, lat + d_lat / 2)]
    return PlanTarget(label, (lon, lat), points, cells)


def point(label, center, priority=None):
    return PlanTarget(label, tuple(center), [], priority=priority)


BELOW = sub_satellite_point(iss(), T)


def near(ops, t, within):
    return [o for o in ops if abs(o.time - t) < within]


# --- TLE and catalog attributes ---------------------------------------------------------


def test_tle_lines():
    assert tle_lines(f"ISS (ZARYA)\n{LINE1}\n{LINE2}") == {"name": "ISS (ZARYA)", "tle1": LINE1, "tle2": LINE2}
    assert tle_lines(f"{LINE1}\r\n{LINE2}") == {"name": "", "tle1": LINE1, "tle2": LINE2}
    assert tle_lines("nothing") is None
    assert abs(tle_epoch(iss()) - EPOCH) < 500


def test_catalog_attributes_by_role():
    problems = []
    catalog = satellite_catalog_of({
        "label": "衛星",
        "url": "https://example.com/arcgis/rest/services/Sats/FeatureServer/0?x=1",
        "fields": {"name": "SAT_NAME", "tle1": "L1", "tle2": "L2", "maxOffNadir": "MAX_ANGLE", "swath": "SWATH_M", "lookSide": "LOOK", "kind": "SENSOR",
                   "minInterval": "GAP_MIN", "maxPerDay": "PER_DAY", "series": "SERIES", "leadTime": "LEAD_H"},
        "swathUnit": "m",
        "defaults": {"maxOffNadir": 25},
    }, problems, 0)
    assert problems == []
    assert catalog.url == "https://example.com/arcgis/rest/services/Sats/FeatureServer/0"
    assert catalog.defaults == {"maxOffNadir": 25, "minOffNadir": 0, "swath": 10, "scenesPerPass": 1, "minInterval": 0, "maxPerDay": 0, "leadTime": 0}
    sat = satellite_of({"SAT_NAME": "SAR-1", "L1": LINE1, "L2": LINE2, "MAX_ANGLE": 40, "SWATH_M": 30000, "LOOK": "R", "SENSOR": "SAR", "GAP_MIN": 15,
                        "PER_DAY": 4, "SERIES": "SAR-X", "LEAD_H": 24}, catalog)
    assert (sat.name, sat.maxOffNadir, sat.swath, sat.length, sat.lookSide, sat.sar, sat.minInterval, sat.maxPerDay, sat.series, sat.leadTime) == \
        ("SAR-1", 40, 30, 30, "right", True, 15, 4, "SAR-X", 24)
    plain = satellite_of({"SAT_NAME": "OPT-1", "L1": LINE1, "L2": LINE2, "MAX_ANGLE": None, "SWATH_M": ""}, catalog)
    assert (plain.maxOffNadir, plain.swath, plain.lookSide, plain.sar, plain.minInterval, plain.maxPerDay, plain.leadTime, plain.series) == \
        (25, 10, "both", False, 0, 0, 0, "OPT")
    assert satellite_of({"SAT_NAME": "X"}, catalog) == "X: TLE なし"


def test_one_field_with_both_lines():
    catalog = satellite_catalog_of({"url": "https://e.com/a/FeatureServer/2", "fields": {"tle": "TLE"}}, [], 0)
    sat = satellite_of({"TLE": f"0 ISS\n{LINE1}\n{LINE2}"}, catalog)
    assert (sat.name, sat.tle1) == ("ISS", LINE1)


def test_refuses_catalogs():
    problems = []
    assert satellite_catalog_of({"url": "https://e.com/a/FeatureServer", "fields": {"tle": "T"}}, problems, 0) is None
    assert satellite_catalog_of({"url": "https://e.com/a/FeatureServer/0", "fields": {"tle1": "A"}}, problems, 1) is None
    assert len(problems) == 2


def test_catalogs_from_config():
    config, problems = parse_config({"satelliteCatalogs": [{"url": "https://e.com/a/FeatureServer/0", "fields": {"tle": "TLE", "name": "NAME"}}, "bad"]})
    assert len(config.satellite_catalogs) == 1
    assert problems == ["satelliteCatalogs[1] はオブジェクトで指定"]


def test_look_side():
    assert [look_side_of(v) for v in ["right", "R", "右", "left", "l", "左", "", "both", None]] == ["right"] * 3 + ["left"] * 3 + ["both"] * 3


def test_pasted_satellites():
    sats = satellites_from_text(f"A\n{LINE1}\n{LINE2}\n{LINE1}\n{LINE2}", 20, 0, 5, sar=True)
    assert [(s.name, s.maxOffNadir, s.swath, s.sar) for s in sats] == [("A", 20, 5, True), ("NORAD 25544", 20, 5, True)]


def test_series():
    names = ["SkySat-C12", "ICEYE-X27", "WorldView-3", "ALOS-2", "Sentinel-1A", "GRUS 1C", "ASNARO", "Pleiades Neo 4 (PNEO4)"]
    assert [series_of(n) for n in names] == ["SkySat", "ICEYE", "WorldView", "ALOS", "Sentinel", "GRUS", "ASNARO", "Pleiades Neo"]


# --- Planning -------------------------------------------------------------------------------


def test_overhead_pass():
    result = plan_access([iss()], [point("a", BELOW)], around(T))
    assert result.problems == []
    op = near(result.opportunities, T, 60000)[0]
    assert abs(op.time - T) < 2000
    assert op.offNadir < 0.5
    assert op.elevation > 89
    assert 380 < op.range < 460


def test_side_and_angle():
    op = near(plan_access([iss()], [point("b", east(BELOW, 150))], around(T)).opportunities, T, 600000)[0]
    assert op.ascending is True
    assert op.side == "right"
    assert 8 < op.offNadir < 20
    assert op.incidence > op.offNadir


def test_leaves_out_passes():
    target = east(BELOW, 150)

    def ops(sat, options=None):
        return near(plan_access([sat], [point("b", target)], options or around(T)).opportunities, T, 600000)

    assert ops(iss(maxOffNadir=5)) == []
    assert ops(iss(), replace(around(T), maxOffNadir=5)) == []
    assert ops(iss(lookSide="left")) == []
    assert len(ops(iss(lookSide="right"))) == 1
    assert ops(iss(minOffNadir=25)) == []
    sun = sun_elevation(target, T)
    assert sun > 10
    assert ops(iss(), replace(around(T), daylight="optical", minSunElevation=sun + 5)) == []
    assert len(ops(iss(sar=True), replace(around(T), daylight="optical", minSunElevation=sun + 5))) == 1


def test_week_in_time_order():
    ops = plan_access([iss()], [point("tokyo", (139.7, 35.7))], PlanOptions(EPOCH, EPOCH + 7 * DAY, None, "none", 10)).opportunities
    assert len(ops) > 5
    assert all(o.time > p.time for p, o in zip(ops, ops[1:]))
    assert all(o.offNadir <= 45 for o in ops)


def test_many_targets_as_each_alone():
    centers = [(139.7, 35.7), (139.9, 35.6), (140.3, 36.1), (-74, 40.7), (2.35, 48.85), (151.2, -33.9), (179.9, 0), (-179.9, 0.2), (0, 89)]
    targets = [point(f"t{i}", c) for i, c in enumerate(centers)]
    options = PlanOptions(EPOCH, EPOCH + DAY, None, "none", 10)
    together = plan_access([iss()], targets, options).opportunities
    alone = [(i, o.time) for i, t in enumerate(targets) for o in plan_access([iss()], [t], options).opportunities]
    assert sorted((o.target, o.time) for o in together) == sorted(alone)
    assert len(together) > 10
    coarse = plan_access([iss()], targets, replace(options, step=150)).opportunities
    for o in together:
        match = next((c for c in coarse if c.target == o.target and abs(c.time - o.time) < 60000), None)
        if match:
            assert abs(match.time - o.time) < 200
    assert sum(1 for c in coarse if any(o.target == c.target and abs(c.time - o.time) < 200 for o in together)) > 10


def test_scenes_side_by_side():
    lon, lat = BELOW
    box = [east((lon, lat - 0.05), -30), east((lon, lat - 0.05), 30), east((lon, lat + 0.05), 30), east((lon, lat + 0.05), -30)]
    target = PlanTarget("box", BELOW, box)
    op = near(plan_access([iss(swath=12)], [target], around(T)).opportunities, T, 60000)[0]
    assert 30 < op.width < 65
    assert op.strips == math.ceil(op.width / 12)
    assert op.frames == max(1, math.ceil(op.length / 10 - 1e-6))
    assert len(pass_shapes(iss(swath=12), target, op).scenes) == 1
    agile = iss(swath=12, scenesPerPass=3)
    wide = near(plan_access([agile], [target], around(T)).opportunities, T, 60000)[0]
    shapes = pass_shapes(agile, target, wide)
    assert len(shapes.scenes) == scenes_taken(wide, agile)
    assert len(shapes.scenes) <= min(3, wide.frames)
    assert len(shapes.reach) == 2
    assert len(shapes.track) > 50
    assert math.hypot(shapes.position[0] - lon, shapes.position[1] - lat) < 0.1
    lons = [c[0] for ring in shapes.scenes for c in ring]
    assert min(lons) < lon < max(lons)


def test_bad_tle():
    bad = iss(name="BAD", tle1="1 garbage", tle2="2 garbage")
    assert plan_access([bad], [point("a", BELOW)], around(T)).problems == ["BAD: TLE の読み込み不可"]


def test_ground_range():
    assert ground_range(0, 6371 + 500) == pytest.approx(0, abs=0.005)
    assert 280 < ground_range(30, 6371 + 500) < 310


def test_samples_over_tokyo():
    samples = sample_satellites()
    assert [(s.name, s.sar, s.lookSide) for s in samples] == [("サンプル光学衛星", False, "both"), ("サンプルSAR衛星", True, "right")]
    result = plan_access(samples, [point("tokyo", (139.7, 35.68))], PlanOptions(EPOCH, EPOCH + 14 * DAY, None, "optical", 10))
    assert result.problems == []
    optical = [o for o in result.opportunities if o.satellite == 0]
    sar = [o for o in result.opportunities if o.satellite == 1]
    assert optical and sar
    assert all(o.sunElevation >= 10 for o in optical)
    assert all(o.side == "right" and 20 <= o.offNadir <= 45 for o in sar)


def test_one_swath_per_pass():
    target = box_target("box", BELOW, 60, 20)
    one = near(plan_access([iss(swath=12)], [target], around(T)).opportunities, T, 120000)[0]
    assert 0.06 < one.coverage < 0.2
    assert len(one.cells) == 400
    long = near(plan_access([iss(swath=12, scenesPerPass=6)], [target], around(T)).opportunities, T, 120000)[0]
    assert one.coverage * 1.5 < long.coverage < 0.4
    assert len(pass_shapes(iss(swath=12, scenesPerPass=6), target, long).scenes) <= 6


def test_scenes_along_the_pass():
    sat = iss(swath=10, length=20, scenesPerPass=4)
    ahead = sub_satellite_point(sat, T + 4500)
    behind = sub_satellite_point(sat, T - 4500)
    target = PlanTarget("strip", BELOW, [ahead, behind], [behind, BELOW, ahead])
    op = near(plan_access([sat], [target], around(T)).opportunities, T, 60000)[0]
    assert op.length > 55
    assert op.frames == math.ceil(op.length / 20)
    scenes = pass_shapes(sat, target, op).scenes
    assert len(scenes) == op.frames

    def km(p, q):
        r = math.pi / 180
        h = math.sin((q[1] - p[1]) * r / 2) ** 2 + math.cos(p[1] * r) * math.cos(q[1] * r) * math.sin((q[0] - p[0]) * r / 2) ** 2
        return 2 * 6371 * math.asin(math.sqrt(h))

    for ring in scenes:
        assert round(km(ring[0], ring[4])) == 20
        assert round(km(ring[4], ring[5])) == 10


def test_combines_satellites_over_a_wide_area():
    target = box_target("wide", (139.7, 35.7), 150, 60)
    sats = [iss(swath=30, scenesPerPass=4), iss(name="ISS-2", swath=30, scenesPerPass=4, tle2=LINE2.replace("325.0288", "145.0288"))]
    ops = plan_access(sats, [target], PlanOptions(EPOCH, EPOCH + 14 * DAY, None, "none", 10)).opportunities
    [plan] = cover_plan(ops, [target], sats, 0.9)
    assert len(plan.steps) > 2
    assert plan.coverage >= 0.9
    assert plan.done == plan.steps[-1].time
    for prev, step in zip([None, *plan.steps], plan.steps):
        assert step.gain > 0
        if prev:
            assert step.time > prev.time
            assert step.cumulative > prev.cumulative
    assert len({s.satellite for s in plan.steps}) == 2


def _pair():
    a = point("a", BELOW)
    b = point("b", sub_satellite_point(iss(), T + 50000))
    options = replace(around(T, 60 * 24 * 2), start=T - 60000)
    return a, b, options


def test_interval_between_shots():
    a, b, options = _pair()
    free = iss()
    free2 = cover_plan(plan_access([free], [a, b], options).opportunities, [a, b], [free])
    assert abs(free2[1].done - free2[0].done) < 120000
    busy = iss(minInterval=10)
    plans = cover_plan(plan_access([busy], [a, b], options).opportunities, [a, b], [busy])
    assert plans[0].done == free2[0].done
    assert plans[1].done - plans[0].done >= 10 * 60000
    assert plans[1].blocked > 0
    daily = iss(maxPerDay=1)
    once = cover_plan(plan_access([daily], [a, b], options).opportunities, [a, b], [daily])
    assert once[1].done - once[0].done >= DAY


def test_more_important_first():
    a, b, options = _pair()
    busy = iss(minInterval=10)
    ops = plan_access([busy], [a, b], options).opportunities
    even = cover_plan(ops, [replace(a, priority=1), replace(b, priority=1)], [busy])
    assert even[0].done < even[1].done
    ranked = cover_plan(ops, [replace(a, priority=1), replace(b, priority=2)], [busy])
    assert abs(ranked[1].done - T) < 120000
    assert ranked[0].done - ranked[1].done >= 10 * 60000
    assert ranked[0].blocked > 0
    assert ranked[1].steps[0].shot == 1
    assert ranked[0].steps[0].shot == 2
    unranked = cover_plan(ops, [a, replace(b, priority=0)], [busy])
    assert unranked[1].done < unranked[0].done
    daily = iss(maxPerDay=1)
    once = cover_plan(plan_access([daily], [a, b], options).opportunities, [replace(a, priority=1), replace(b, priority=2)], [daily])
    assert once[0].done - once[1].done >= DAY - 120000


def test_too_soon_to_order():
    now = T - 30 * 60000
    every = plan_access([iss()], [point("a", BELOW)], around(T, now=now))
    assert near(every.opportunities, T, 60000)
    assert every.tooSoon == 0
    late = plan_access([iss(leadTime=2)], [point("a", BELOW)], around(T, now=now))
    assert not near(late.opportunities, T, 60000)
    assert late.tooSoon > 0
    assert plan_access([iss(leadTime=2)], [point("a", BELOW)], around(T)).tooSoon == 0


def test_points_share_a_scene():
    a, b, c = point("a", BELOW), point("b", east(BELOW, 4)), point("c", east(BELOW, 60))
    sat = iss(swath=10, minInterval=10)
    options = replace(around(T, 60 * 24 * 2), start=T - 60000)
    ops = plan_access([sat], [a, b, c], options).opportunities
    pa, pb, pc = (p.steps[0] for p in cover_plan(ops, [a, b, c], [sat]))
    assert pa.shot == pb.shot
    assert pa.shotTargets == 2
    assert abs(pa.time - T) < 60000
    assert abs(pb.time - pa.time) < 5000
    assert pc.shot != pa.shot
    assert pc.time - pa.time >= 10 * 60000
    ring = pass_shapes(sat, a, pa).scenes[0]
    lons = [p[0] for p in ring]
    lats = [p[1] for p in ring]
    for t in (a, b):
        assert min(lons) < t.center[0] < max(lons)
        assert min(lats) < t.center[1] < max(lats)
    apart = cover_plan(ops, [a, b, c], [sat], 0.95, 0.02, False)
    assert apart[1].steps[0].time - apart[0].steps[0].time >= 10 * 60000


def test_shift_scene():
    """Not in the viewer's tests: a dragged scene stays within reach and is worked out again."""
    target = box_target("box", BELOW, 60, 20)
    sat = iss(swath=12)
    op = near(plan_access([sat], [target], around(T)).opportunities, T, 120000)[0]
    moved = shift_scene(op, sat, target, 0.0, 0.0)
    assert moved.coverage == pytest.approx(op.coverage)
    assert moved.adjusted
    far = shift_scene(op, sat, target, 1.0, 0.0)
    assert far.offNadir == pytest.approx(45, abs=0.01)
    assert far.coverage == 0
    right = shift_scene(op, iss(swath=12, lookSide="right"), target, 0.5, 0.0)
    assert right.side == "right"


# --- Sensor modes ------------------------------------------------------------------------------

SAR_MODES = [
    SensorMode("ISS", "ストリップマップ", minOffNadir=15, maxOffNadir=30, swath=30, length=50, resolution=3),
    SensorMode("ISS", "スポットライト", minOffNadir=10, maxOffNadir=40, swath=5, resolution=0.5, primary=True),
    SensorMode("ISS", "スキャンSAR", minOffNadir=20, maxOffNadir=45, swath=100, length=100, scenesPerPass=3, resolution=15),
]


def test_sensor_modes_in_config():
    problems = []
    catalog = sensor_catalog_of({
        "label": "センサー",
        "swathUnit": "m",
        "sensors": [
            {"sensor": "ICEYE", "kind": "SAR", "lookSide": "right", "minInterval": 5,
             "modes": [{"mode": "Strip", "swath": 30000, "length": 50000, "maxOffNadir": 30}, {"name": "Spot", "swath": 5000, "lookSide": "both", "default": True, "primary": "はい"}]},
            {"sensor": "WorldView", "kind": "optical", "maxOffNadir": 30},
            {"modes": []},
        ],
    }, problems, 0)
    assert problems == ["sensorCatalogs[0].sensors[2] に sensor（センサー名）なし"]
    assert [m.values() for m in catalog.modes] == [
        {"sensor": "ICEYE", "mode": "Strip", "sar": True, "swath": 30, "length": 50, "maxOffNadir": 30, "minInterval": 5, "lookSide": "right"},
        {"sensor": "ICEYE", "mode": "Spot", "sar": True, "swath": 5, "minInterval": 5, "lookSide": "both", "primary": True},
        {"sensor": "WorldView", "mode": "", "sar": False, "maxOffNadir": 30},
    ]


def test_sensor_table():
    problems = []
    table = sensor_catalog_of({"url": "https://example.com/arcgis/rest/services/Sensors/FeatureServer/1",
                               "fields": {"sensor": "SENSOR", "mode": "MODE", "swath": "SWATH_KM", "resolution": "RES_M", "kind": "TYPE", "primary": "IS_DEFAULT"}}, problems, 1)
    assert problems == []
    assert table.url == "https://example.com/arcgis/rest/services/Sensors/FeatureServer/1"
    assert sensor_mode_of({"SENSOR": "ICEYE", "MODE": "Spot", "SWATH_KM": 5, "RES_M": "0.5", "TYPE": "SAR", "IS_DEFAULT": 1}, table).values() == \
        {"sensor": "ICEYE", "mode": "Spot", "sar": True, "swath": 5, "resolution": 0.5, "primary": True}
    assert sensor_mode_of({"MODE": "Spot"}, table) is None
    assert sensor_catalog_of({"url": "https://example.com/x"}, problems, 2) is None
    assert sensor_catalog_of({"url": "https://example.com/arcgis/rest/services/S/FeatureServer/1", "fields": {"mode": "MODE"}}, problems, 3) is None
    assert problems == ["sensorCatalogs[2] は sensors（モードの一覧）か、…/FeatureServer/1 のようなテーブルの url を記述", "sensorCatalogs[3] に fields.sensor（センサー名の属性）なし"]
    config, _ = parse_config({
        "satelliteCatalogs": [{"url": "https://example.com/arcgis/rest/services/Sat/FeatureServer/0", "fields": {"tle": "TLE", "sensor": "SENSOR"}}],
        "sensorCatalogs": [{"sensors": [{"sensor": "ICEYE", "modes": [{"mode": "Spot"}]}]}],
    })
    assert config.satellite_catalogs[0].fields["sensor"] == "SENSOR"
    assert [m.values() for m in config.sensor_catalogs[0].modes] == [{"sensor": "ICEYE", "mode": "Spot"}]


def test_satellite_per_mode():
    sats = with_modes([iss(minInterval=7), iss(name="Other", id="99999", series="Other")], SAR_MODES)
    assert [(s.name, s.mode, s.primary) for s in sats] == [
        ("ISS", "ストリップマップ", False), ("ISS", "スポットライト", True), ("ISS", "スキャンSAR", False), ("Other", None, None)]
    strip, spot, scan = sats[:3]
    assert [strip.minOffNadir, strip.maxOffNadir, strip.swath, strip.length, strip.resolution] == [15, 30, 30, 50, 3]
    assert [spot.swath, spot.length, spot.minInterval, spot.lookSide] == [5, 5, 7, "both"]
    assert scan.scenesPerPass == 3
    assert len({platform_key(s) for s in sats[:3]}) == 1
    assert satellite_label(spot) == "ISS（スポットライト）"
    assert satellite_label(sats[3]) == "Other"
    assert [s.primary for s in with_modes([iss()], [replace(m, primary=None) for m in SAR_MODES])] == [True, False, False]


def test_sensor_by_sensor_series_or_name():
    assert len(with_modes([iss(name="X1", series="ISS")], SAR_MODES)) == 3
    assert len(with_modes([iss(name="X1", series="X", sensor="iss ")], SAR_MODES)) == 3
    assert len(with_modes([iss(name="X1", series="X")], SAR_MODES)) == 1


def test_catalog_per_satellite_and_mode():
    catalog = satellite_catalog_of({"url": "https://example.com/arcgis/rest/services/Sat/FeatureServer/0",
                                    "fields": {"name": "NAME", "id": "ID", "tle": "TLE", "mode": "MODE", "swath": "SWATH", "sensor": "SENSOR", "resolution": "RES"}}, [], 0)
    rows = [satellite_of(r, catalog) for r in [
        {"NAME": "ISS", "ID": "A", "TLE": f"{LINE1}\n{LINE2}", "MODE": "Wide", "SWATH": 100, "SENSOR": "S", "RES": 10},
        {"NAME": "ISS", "ID": "A", "TLE": f"{LINE1}\n{LINE2}", "MODE": "Fine", "SWATH": 10, "SENSOR": "S", "RES": "1"},
    ]]
    assert [(s.mode, s.swath, s.sensor, s.resolution) for s in rows] == [("Wide", 100, "S", 10), ("Fine", 10, "S", 1)]
    assert [(s.mode, s.primary) for s in with_modes(rows, [])] == [("Wide", True), ("Fine", False)]
    assert [s.maxOffNadir for s in with_modes(rows, [SensorMode("S", "fine", maxOffNadir=12)])] == [30, 12]


def test_sample_modes():
    sats = with_modes(sample_satellites(), sample_sensor_modes())
    assert [satellite_label(s) for s in sats] == ["サンプル光学衛星", "サンプルSAR衛星（ストリップマップ）", "サンプルSAR衛星（スポットライト）", "サンプルSAR衛星（スキャンSAR）"]
    assert [s.primary for s in sats] == [None, True, False, False]


MODE_OPTIONS = PlanOptions(T - 30 * 60000, T + 30 * 60000, None, "none", 10)


def test_mode_finds_same_passes():
    target = point("side", east(BELOW, 420))
    sats = with_modes([iss()], SAR_MODES)
    together = plan_access(sats, [target], MODE_OPTIONS).opportunities
    alone = [plan_access([s], [target], MODE_OPTIONS).opportunities for s in sats]
    assert [sats[o.satellite].mode for o in together] == [s.mode for i, s in enumerate(sats) if alone[i]]
    for op in together:
        assert op.time == alone[op.satellite][0].time
    assert [sats[o.satellite].mode for o in together] == ["スポットライト", "スキャンSAR"]
    assert together[0].offNadir > 30


def test_modes_share_interval():
    a, b, _ = _pair()
    sats = with_modes([iss(minInterval=10)], [replace(m, minOffNadir=0) for m in SAR_MODES])
    ops = plan_access(sats, [a, b], replace(MODE_OPTIONS, end=T + 2 * DAY)).opportunities
    assert len([o for o in ops if o.time < T + 120000]) == 6
    plans = cover_plan(ops, [a, b], sats)
    assert sats[plans[0].steps[0].satellite].mode == "スポットライト"
    assert plans[1].done - plans[0].done >= 10 * 60000
    assert plans[1].blocked > 0


@pytest.mark.parametrize("km, mode", [(3, "スポットライト"), (20, "ストリップマップ"), (60, "スキャンSAR")])
def test_sharpest_mode_that_holds(km, mode):
    d_lon = km / 2 / (111.32 * math.cos(BELOW[1] * math.pi / 180))
    d_lat = km / 2 / 111.32
    cells = [(BELOW[0] - d_lon + 2 * d_lon * (i + 0.5) / 12, BELOW[1] - d_lat + 2 * d_lat * (j + 0.5) / 12) for i in range(12) for j in range(12)]
    area = PlanTarget(f"{km} km", BELOW, [c for k, c in enumerate(cells) if k % 11 == 0], cells)
    sats = with_modes([iss()], [replace(m, minOffNadir=0) for m in SAR_MODES])
    [plan] = cover_plan(plan_access(sats, [area], MODE_OPTIONS).opportunities, [area], sats, 0.9)
    assert len(plan.steps) == 1
    assert sats[plan.steps[0].satellite].mode == mode


@pytest.mark.skipif(not SGP4_ACCELERATED, reason="the bundled pure-Python SGP4 is slower (about 3 s)")
def test_speed_20_satellites_week():
    """Spec: 20 satellites × 7 days × 1 point under a second."""
    import time

    sats = [iss(name=f"S{i}", id=str(i), tle2=LINE2.replace("247.4627", f"{(247.4627 + i * 18) % 360:8.4f}")) for i in range(20)]
    t0 = time.perf_counter()
    plan_access(sats, [point("tokyo", (139.7, 35.7))], PlanOptions(EPOCH, EPOCH + 7 * DAY, None, "none", 10))
    assert time.perf_counter() - t0 < 1.0
