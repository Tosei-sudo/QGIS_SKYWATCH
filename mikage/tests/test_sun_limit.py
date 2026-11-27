"""
太陽高度の下限を衛星ごとに (tsudo 2026-11-27: 「太陽高度の限界値を、衛星ごとに持たせたい」): a satellite
(`MIN_SUN_ELEVATION` of the satellite catalog, role `minSunElevation`) or a sensor mode may say the lowest sun it
images in; the mode's value first, then the satellite's, then the common one (the imaging plan's 太陽高度, 10° in the
day's work, the pass dock's choice). Only satellites of a kind that images only in daylight; 考慮なし ignores it.
"""
from __future__ import annotations

from dataclasses import replace

from mikage.core.config import parse_config
from mikage.core.plan.access import plan_access
from mikage.core.plan.modes import SensorMode, sensor_catalog_of, sensor_mode_of, with_modes
from mikage.core.plan.orbit import sun_elevation
from mikage.core.plan.satellites import SUN_RANGE, satellite_catalog_of, satellite_of, sun_limit, sun_of
from mikage.core.plan.track import satellite_track
from mikage.core.request.model import jst_day
from mikage.core.request.passes import DAYLIGHT_SUN, day_passes
from mikage.tests.test_plan import BELOW, LINE1, LINE2, T, around, east, iss, near, point
from mikage.tests.test_request_passes import point as request_at

TARGET = east(BELOW, 150)
SUN = sun_elevation(TARGET, T)
CATALOG = {"url": "https://gis.example/arcgis/rest/services/S/FeatureServer/0",
           "fields": {"name": "SAT_NAME", "tle1": "TLE_LINE1", "tle2": "TLE_LINE2", "kind": "SENSOR_TYPE", "sensor": "SENSOR",
                      "minSunElevation": "MIN_SUN_ELEVATION"}}


def ops(sat, options):
    return near(plan_access([sat], [point("b", TARGET)], options).opportunities, T, 600000)


def daylight(minimum: float):
    return replace(around(T), daylight="optical", minSunElevation=minimum)


def test_the_sun_at_the_target_is_daylight():
    # What the tests below lean on: the pass at T is in the day, well above the horizon.
    assert 15 < SUN < 80


def test_a_satellite_reads_its_lowest_sun_from_the_catalog():
    problems: list = []
    catalog = satellite_catalog_of(CATALOG, problems, 0)
    assert problems == [] and catalog.fields["minSunElevation"] == "MIN_SUN_ELEVATION"

    def sat(value):
        return satellite_of({"SAT_NAME": "X", "TLE_LINE1": LINE1, "TLE_LINE2": LINE2, "MIN_SUN_ELEVATION": value}, catalog)

    assert sat(20).minSunElevation == 20
    assert sat("5.5").minSunElevation == 5.5
    # Below the horizon too (a satellite imaging at dusk), kept within −18〜90.
    assert sat(-6).minSunElevation == -6
    assert sat(-40).minSunElevation == SUN_RANGE[0] and sat(120).minSunElevation == SUN_RANGE[1]
    # Not given: None (the common value then), not 0.
    for empty in (None, "", "  ", "x", True):
        assert sat(empty).minSunElevation is None
    assert sun_of(0) == 0 and sun_of(None) is None


def test_a_mode_says_its_own_else_the_satellites():
    problems: list = []
    table = sensor_catalog_of({"url": "https://gis.example/arcgis/rest/services/S/FeatureServer/1",
                               "fields": {"sensor": "SENSOR", "mode": "SENSOR_MODE", "minSunElevation": "MIN_SUN_ELEVATION"}}, problems, 0)
    assert problems == []
    pan = sensor_mode_of({"SENSOR": "CAM", "SENSOR_MODE": "PAN", "MIN_SUN_ELEVATION": 25}, table)
    dusk = sensor_mode_of({"SENSOR": "CAM", "SENSOR_MODE": "LOW", "MIN_SUN_ELEVATION": -3}, table)
    plain = sensor_mode_of({"SENSOR": "CAM", "SENSOR_MODE": "MS", "MIN_SUN_ELEVATION": None}, table)
    assert (pan.minSunElevation, dusk.minSunElevation, plain.minSunElevation) == (25, -3, None)
    sat = iss(sensor="CAM", minSunElevation=12)
    out = {s.mode: s.minSunElevation for s in with_modes([sat], [pan, dusk, plain])}
    assert out == {"PAN": 25, "LOW": -3, "MS": 12}
    # Neither says: the common value.
    assert [s.minSunElevation for s in with_modes([iss(sensor="CAM")], [SensorMode("CAM", "MS")])] == [None]
    assert sun_limit(iss(), 10) == 10 and sun_limit(iss(minSunElevation=3), 10) == 3


def test_the_settings_file_takes_it_for_satellites_and_written_modes():
    config, problems = parse_config({
        "satelliteCatalogs": [CATALOG],
        "sensorCatalogs": [{"sensors": [{"sensor": "CAM", "minSunElevation": 15, "modes": [{"mode": "PAN"}, {"mode": "LOW", "minSunElevation": -6}]}]}],
    })
    assert problems == []
    [modes] = [c.modes for c in config.sensor_catalogs]
    assert [(m.mode, m.minSunElevation) for m in modes] == [("PAN", 15), ("LOW", -6)]


def test_the_plan_takes_a_satellites_own_lowest_sun():
    # The plan's value above the sun: the optical satellite is left out, unless its own is below the sun.
    assert ops(iss(), daylight(SUN + 5)) == []
    assert len(ops(iss(minSunElevation=SUN - 5), daylight(SUN + 5))) == 1
    # The plan's value below the sun: its own above the sun leaves it out.
    assert len(ops(iss(), daylight(SUN - 5))) == 1
    assert ops(iss(minSunElevation=SUN + 5), daylight(SUN - 5)) == []
    # Kinds imaging by night (SAR, IR) are not held by it.
    assert len(ops(iss(sar=True, minSunElevation=SUN + 5), daylight(0))) == 1
    assert len(ops(iss(kind_given="IR", minSunElevation=SUN + 5), daylight(0))) == 1
    # 考慮なし: day and night, its own value too.
    assert len(ops(iss(minSunElevation=SUN + 5), around(T))) == 1


def test_the_days_work_takes_it_with_10_degrees_for_the_others():
    assert DAYLIGHT_SUN == 10
    day = jst_day(T)
    req = request_at(1, TARGET)

    def found(sat):
        return [o for o in day_passes([req], day, [sat]).ops[req.id] if abs(o.time - T) < 600000]

    assert len(found(iss())) == 1
    assert found(iss(minSunElevation=SUN + 5)) == []
    assert len(found(iss(minSunElevation=SUN - 5))) == 1
    # SAR: day and night whatever it says.
    assert len(found(iss(sar=True, minSunElevation=SUN + 5))) == 1


def test_the_pass_dock_cuts_the_reach_at_a_satellites_own_lowest_sun():
    start, end = T - 100 * 60000, T + 100 * 60000
    common = satellite_track(iss(), start, end, step=60, min_sun=10)
    high = satellite_track(iss(minSunElevation=60), start, end, step=60, min_sun=10)
    dusk = satellite_track(iss(minSunElevation=-12), start, end, step=60, min_sun=10)
    assert sum(high.open) < sum(common.open) < sum(dusk.open)
    # 考慮なし (no min_sun): everywhere, its own value or not.
    assert all(satellite_track(iss(minSunElevation=60), start, end, step=60).open)
