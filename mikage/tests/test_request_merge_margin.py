"""
The row of requests put into one shot (tsudo, 2026-11-11: 「システム側が、
ポリゴンで受け付けるとき、ぴったしだと完全含有判定されなくて、収めるために 2
オーダーされる可能性はないかな」→「頂点数は考慮しなくていいので、パス並行に」
「規定500mにしよう」): written with the tightest box along the track around the
requests' own areas, not the outline of the scenes, and put together only when
`mergeMargin` (500 m) is left inside the shot on every side.
"""
from __future__ import annotations

import math

from mikage.core.plan.orbit import R, ecef_of, track_angles, unit
from mikage.core.request.daily import BUSY, TAKEN
from mikage.core.request.export import Context, raw_value
from mikage.core.request.model import in_rings
from mikage.core.request.shots import area_box
from mikage.core.request.systems import export_systems_of
from mikage.tests.test_plan import BELOW, T
from mikage.tests.test_request_passes import DAY, _middle, along, by_id, fit, point, system


def _km(a, b) -> float:
    """The distance (km) between two places on the sphere."""
    p, q = unit(ecef_of(a)), unit(ecef_of(b))
    return R * math.acos(max(-1.0, min(1.0, sum(x * y for x, y in zip(p, q)))))


def _seconds_for(km: float) -> float:
    """How long after T the satellite is over the place `km` along the track from BELOW."""
    return km / _km(BELOW, along(1.0))


def _across_along(ring, frame) -> tuple:
    angles = [track_angles(unit(ecef_of(p)), frame) for p in ring]
    return min(a[0] for a in angles), max(a[0] for a in angles), min(a[1] for a in angles), max(a[1] for a in angles)


def test_written_with_the_box_along_the_track_around_the_requests():
    a, b = point(1, BELOW, 300), point(2, along(2.5))
    out, systems = fit([a, b])
    c = by_id(out)
    assert c["R2"].member and c["R2"].lead is c["R1"]
    [shot] = c["R1"].shots
    ctx = Context(systems[0], DAY, T)
    ring = raw_value(c["R1"], "geometry", ctx, "geojson", 6, as_object=True)["coordinates"][0]
    assert ring == [[round(x, 6), round(y, 6)] for x, y in shot.area_ring()]
    assert all(in_rings([ring], *_middle(r)) for r in (a, b))
    # Along the track (its edges across and along it) and as tight as the requests' own areas: no swath-wide margin.
    x0, x1, y0, y1 = _across_along(shot.area_ring(), shot.frame)
    want = [area_box(r, shot.frame) for r in (a, b)]
    assert math.isclose(x0, min(w[0] for w in want), abs_tol=1e-9) and math.isclose(x1, max(w[1] for w in want), abs_tol=1e-9)
    assert math.isclose(y0, min(w[2] for w in want), abs_tol=1e-9) and math.isclose(y1, max(w[3] for w in want), abs_tol=1e-9)
    assert (x1 - x0) * R < 1.0 and (shot.window[1] - shot.window[0]) * R > 11.9
    # Inside the scenes, with room on every side.
    w0, w1 = shot.window
    s0, s1 = shot.along
    assert w0 * R + 0.5 < x0 * R and x1 * R < w1 * R - 0.5 and s0 * R + 0.5 < y0 * R and y1 * R < s1 * R - 0.5
    assert ctx.system.merge_margin == 500
    # A request alone keeps its own area in the file; its shot's polygon is the outline of the scenes.
    out, systems = fit([point(4, along(120))])
    [alone] = out
    assert raw_value(alone, "geometry", Context(systems[0], DAY, T), "geojson", 6, as_object=True)["coordinates"][0] == [
        [round(x, 6), round(y, 6)] for x, y in alone.request.rings[0]]
    assert alone.shots[0].area_ring() == alone.shots[0].ring()


def test_put_together_only_with_the_margin_left_inside():
    # 4 scenes of 10 km in a row take 40 km; two places 39.5 km apart fit with no margin, not with 500 m at either end.
    a, b = point(1, BELOW, 300), point(2, along(_seconds_for(39.5)))
    out, _ = fit([a, b])
    c = by_id(out)
    assert c["R1"].result == TAKEN and not c["R1"].merged
    # Apart, the minute between shots on the same pass holds the second back.
    assert c["R2"].result != TAKEN and c["R2"].reason == BUSY
    out, _ = fit([a, b], systems=[system(merge_margin=0)])
    c = by_id(out)
    assert c["R2"].member and c["R1"].scenes == 4
    # 30 km apart: 4 scenes with the margin.
    out, _ = fit([a, point(2, along(_seconds_for(30)))])
    c = by_id(out)
    assert c["R2"].member and c["R1"].scenes == 4
    [shot] = c["R1"].shots
    x0, x1, y0, y1 = shot.box()
    assert (y0 - shot.along[0]) * R >= 0.5 - 1e-6 and (shot.along[1] - y1) * R >= 0.5 - 1e-6


def test_the_margin_in_the_settings():
    problems = []
    systems = export_systems_of([
        {"system": "A", "columns": [{"key": "ID", "source": "id"}]},
        {"system": "B", "mergeMargin": 0, "columns": [{"key": "ID", "source": "id"}]},
        {"system": "C", "mergeMargin": 1200.5, "columns": [{"key": "ID", "source": "id"}]},
        {"system": "D", "mergeMargin": -1, "columns": [{"key": "ID", "source": "id"}]},
    ], problems)
    assert [s.merge_margin for s in systems] == [500, 0, 1200.5, 500]
    assert problems == ["requestExports[3] の mergeMargin は 0 以上の数（メートル）で指定"]
