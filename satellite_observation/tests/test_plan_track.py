"""The viewer's satellite-track.test.ts, ported."""
import calendar
import math
import time
from dataclasses import replace

import pytest

from satellite_observation.core.plan.orbit import ground_range
from satellite_observation.core.plan.satellites import sample_satellites
from satellite_observation.core.plan.scene import sub_satellite_point
from satellite_observation.core.plan.track import (
    band_rings, reach_angles, sample_at, satellite_track, step_for, tick_minutes, track_layers, track_rows, track_ticks,
    wrap_line, wrap_polygon,
)

OPTICAL, SAR = sample_satellites()
T0 = calendar.timegm((2026, 10, 8, 0, 0, 0)) * 1000


def km(a, b):
    r = math.pi / 180
    h = math.sin((b[1] - a[1]) * r / 2) ** 2 + math.cos(a[1] * r) * math.cos(b[1] * r) * math.sin((b[0] - a[0]) * r / 2) ** 2
    return 2 * 6371.0088 * math.asin(math.sqrt(h))


def test_reach_angles_both_ways():
    assert reach_angles(OPTICAL) == [("both", -20, 20)]
    assert reach_angles(OPTICAL, 10) == [("both", -10, 10)]


def test_reach_angles_keep_the_smallest_and_the_side():
    assert reach_angles(SAR) == [("right", -20, -45)]
    assert [a[0] for a in reach_angles(replace(SAR, lookSide="both"))] == ["left", "right"]
    # A limit below the smallest angle leaves an empty band, not a reversed one.
    assert reach_angles(SAR, 10) == [("right", -10, -10)]


def test_follows_the_sub_satellite_point():
    track = satellite_track(OPTICAL, T0, T0 + 30 * 60000, step=30)
    assert len(track.times) == 61
    assert track.times[0] == T0 and track.times[-1] == T0 + 30 * 60000
    mid = 30
    assert km(track.track[mid], sub_satellite_point(OPTICAL, track.times[mid])) < 0.01
    assert 700_000 < track.heights[mid] < 900_000
    assert len(track.bands) == 1
    band = track.bands[0]
    reach = ground_range(20, 6371 + track.heights[mid] / 1000)
    for edge in (band.inner, band.outer):
        assert abs(km(edge[mid], track.track[mid]) - reach) < 15
    assert track.offNadir == (20, 20)


def test_sar_band_on_its_side():
    track = satellite_track(SAR, T0, T0 + 10 * 60000, step=60)
    assert len(track.bands) == 1
    band = track.bands[0]
    near = km(band.inner[5], track.track[5])
    far = km(band.outer[5], track.track[5])
    assert near > 200 and far > near + 300
    assert track.offNadir == (20, 45)


def test_cuts_the_reach_at_night_for_optical_only():
    day = satellite_track(OPTICAL, T0, T0 + 200 * 60000, min_sun=10)
    assert any(not o for o in day.open) and any(day.open)
    assert len(day.bands) == 2
    assert sum(len(b.inner) for b in day.bands) < len(day.times)
    assert all(len(b.inner) == len(b.outer) for b in day.bands)
    radar = satellite_track(SAR, T0, T0 + 100 * 60000, min_sun=10)
    assert all(radar.open)


def test_step_and_sample():
    assert step_for(60_000) == 10
    assert step_for(7 * 86400000) == 60
    track = satellite_track(OPTICAL, T0, T0 + 600000, step=10)
    assert track.times[sample_at(track, T0 + 300000)] == T0 + 300000
    assert satellite_track(replace(OPTICAL, tle1="x", tle2="y"), T0, T0 + 1) is None


def test_wrap_line():
    pieces = wrap_line([(170, 0), (178, 2), (-178, 4), (-170, 6)])
    assert len(pieces) == 2
    assert pieces[0][-1][0] == pytest.approx(180) and pieces[0][-1][1] == pytest.approx(3)
    assert pieces[1][0][0] == pytest.approx(-180) and pieces[1][0][1] == pytest.approx(3)
    assert all(abs(lon) <= 180 + 1e-9 for p in pieces for lon, _ in p)
    assert wrap_line([(0, 0), (10, 10)]) == [[(0, 0), (10, 10)]]


def test_wrap_polygon():
    parts = wrap_polygon([(175, 0), (-175, 0), (-175, 5), (175, 5), (175, 0)])
    assert len(parts) == 2
    for ring in parts:
        assert ring[0] == ring[-1]
        lons = [lon for lon, _ in ring]
        assert max(lons) - min(lons) == pytest.approx(5)


def test_band_rings():
    track = satellite_track(OPTICAL, T0, T0 + 100 * 60000, step=20)
    rings = band_rings(track.bands[0], 30)
    assert len(rings) == math.ceil((len(track.times) - 1) / 30)
    assert all(r[0] == r[-1] for r in rings)
    assert all(abs(lon) <= 180 + 1e-9 for r in rings for part in wrap_polygon(r) for lon, _ in part)


def test_tick_minutes():
    assert tick_minutes(30) == 1
    assert tick_minutes(200) == 5
    assert tick_minutes(1440) == 30
    assert tick_minutes(7 * 1440) == 360


def test_rows_ticks_and_layers():
    track = satellite_track(SAR, T0, T0 + 30 * 60000, step=10)
    [row] = track_rows([track], T0 + 15 * 60000)
    assert row[0] == "サンプルSAR衛星" and row[2].endswith(" km") and row[3] in ("北行", "南行") and row[4] == "20〜45°・右"
    ticks = track_ticks(track, T0 + 15 * 60000)
    assert ticks and all(abs(t - (T0 + 15 * 60000)) >= 30000 for _, t in ticks)
    fields, lines, areas = track_layers([track])
    assert [f.name for f in fields][:2] == ["kind", "satellite"]
    assert lines[0][1]["kind"] == "地上軌跡" and areas[0][1]["kind"] == "撮像可能範囲"
    assert areas[0][1]["max_off_nadir"] == 45


def test_a_week_is_quick():
    sats = [replace(OPTICAL, id=f"S{i}") for i in range(20)]
    began = time.perf_counter()
    for s in sats[:2]:
        satellite_track(s, T0, T0 + 7 * 86400000)
    # Two satellites over a week (the dock computes them in a task).
    assert time.perf_counter() - began < 10
