"""
Requests of any polygon (tsudo 2026-11-14: 「普通のポリゴンでもできない？」「凹みが
おおいポリゴンとかみたいな矩形回転させれば収まるやつ」): the rings grouped
into parts and holes (model.polygons_of), the shape said in words, the
ledger's ring directions, MULTIPOLYGON in the files, and the smallest
rectangle turned any way (model.rotated_box) for a system whose
`areaShape` is `rotatedBox` (docs/imaging-requests.md §32).
"""
from __future__ import annotations

import csv
import io
import json
import math

from mikage.core.request.daily import TAKEN, allocate, gather
from mikage.core.request.export import Context, csv_text, geometry_value
from mikage.core.request.model import bbox_of, in_rings, polygons_of, ring_area, rotated_box, shape_text, square_rings
from mikage.core.request.store import esri_rings
from mikage.core.request.systems import export_systems_of
from mikage.tests.test_request import DAY, FIRST, SATS, TOKYO, request

OUTER = [[0, 0], [10, 0], [10, 10], [0, 10], [0, 0]]  # anticlockwise
HOLE = [[2, 2], [2, 4], [4, 4], [4, 2], [2, 2]]  # clockwise
ISLAND = [[3, 3], [3.5, 3], [3.5, 3.5], [3, 3]]  # in the hole: an outer ring of its own
OTHER = [[20, 0], [22, 0], [22, 2], [20, 0]]
#: A U turned some 35° (two arms with a notch between them): concave.
U = [[139.0, 35.0], [139.3, 35.2], [139.25, 35.27], [139.1, 35.17], [139.05, 35.25], [139.2, 35.35], [139.15, 35.42],
     [138.95, 35.08], [139.0, 35.0]]


def _km2(ring, lat=35.2):
    k = math.cos(math.radians(lat))
    return abs(ring_area([[x * 111.32 * k, y * 110.574] for x, y in ring]))


def _grown(box, f=1.0001):
    """A box a little larger about its middle (so that its own corners count as inside)."""
    cx, cy = sum(p[0] for p in box[:4]) / 4, sum(p[1] for p in box[:4]) / 4
    return [[cx + (x - cx) * f, cy + (y - cy) * f] for x, y in box]


def _system(**kw):
    problems = []
    out = export_systems_of([{"system": "SYS-T", "kind": "光学", "series": ["OPT-X"], "timeZone": "UTC", **kw}], problems)
    assert problems == []
    return out[0]


def test_rings_are_grouped_into_parts_and_holes_whichever_way_they_turn():
    # The hole given first: still the hole of the outer ring; the island in it and the other ring are parts.
    got = polygons_of([HOLE, OUTER, ISLAND, OTHER])
    assert got == [[OUTER, HOLE], [ISLAND], [OTHER]]
    assert polygons_of([OUTER]) == [[OUTER]]
    # Turned the other way: the same.
    assert polygons_of([list(reversed(OUTER)), list(reversed(HOLE))]) == [[list(reversed(OUTER)), list(reversed(HOLE))]]
    assert polygons_of([]) == [] and polygons_of([[[0, 0], [1, 1]]]) == []


def test_the_shape_is_said_in_words():
    assert shape_text(TOKYO) == "矩形"
    assert shape_text(square_rings(139.8, 35.5, 10), "点") == "点"
    assert shape_text([U]) == "多角形（頂点 8）"
    assert shape_text([OUTER, HOLE, OTHER]) == "多角形（2 部分・頂点 11・穴 1）"
    assert shape_text([]) == ""


def test_the_ledger_gets_outer_rings_clockwise_and_holes_anticlockwise():
    rings = esri_rings([OUTER, HOLE, OTHER])
    # Outer rings clockwise (negative), the hole anticlockwise (positive), as Esri reads them; the outer ring first.
    assert [math.copysign(1, ring_area(r)) for r in rings] == [-1, 1, -1]
    assert rings[1] == list(reversed(HOLE))
    # Rings side by side (shots, parts) stay all clockwise as before.
    assert all(ring_area(r) < 0 for r in esri_rings([OUTER, OTHER, list(reversed(TOKYO[0]))]))


def test_files_write_parts_as_a_multipolygon_and_one_part_as_before():
    assert geometry_value(TOKYO, "wkt", 2) == "POLYGON ((139.80 35.45, 139.90 35.45, 139.90 35.55, 139.80 35.55, 139.80 35.45))"
    assert geometry_value([HOLE, OUTER], "wkt", 0) == "POLYGON ((0 0, 10 0, 10 10, 0 10, 0 0), (2 2, 2 4, 4 4, 4 2, 2 2))"
    assert geometry_value([OUTER, HOLE, OTHER], "wkt", 0) == (
        "MULTIPOLYGON (((0 0, 10 0, 10 10, 0 10, 0 0), (2 2, 2 4, 4 4, 4 2, 2 2)), ((20 0, 22 0, 22 2, 20 0)))")
    assert geometry_value([OUTER, OTHER], "geojson", 0, as_object=True) == {"type": "MultiPolygon", "coordinates": [[OUTER], [OTHER]]}
    assert geometry_value([U], "geojson", 2, as_object=True)["type"] == "Polygon"
    # The vertices: the first part's outer ring; the box and the middle: of everything.
    assert geometry_value([HOLE, OUTER, OTHER], "vertices", 0) == "0,0,0,10,10,10,10,0,0,0"
    assert geometry_value([OUTER, OTHER], "bbox", 0) == "0,0,22,10"


def test_the_smallest_rectangle_turned_any_way():
    box = rotated_box([U])
    assert len(box) == 5 and box[0] == box[-1] and ring_area(box) > 0
    # It holds every vertex (to rounding), is smaller than the box along the meridians, and larger than the U.
    assert all(in_rings([_grown(box)], x, y) for x, y in U)
    w, s, e, n = bbox_of([U])
    assert _km2(U) < _km2(box) < _km2([[w, s], [e, s], [e, n], [w, n], [w, s]]) * 0.8
    # Its corners are right angles on the ground (longitudes shrunk by the cosine of the middle latitude).
    k = math.cos(math.radians((s + n) / 2))
    pts = [(x * k, y) for x, y in box[:4]]
    for i in range(4):
        a, b, c = pts[i - 1], pts[i], pts[(i + 1) % 4]
        assert abs((a[0] - b[0]) * (c[0] - b[0]) + (a[1] - b[1]) * (c[1] - b[1])) < 1e-9
    # A rectangle turned 30°: itself.
    t = math.radians(30)
    k = math.cos(math.radians(35 + 0.2 * math.sin(t) / 2 + 0.05 * math.cos(t) / 2))
    turned = [[139 + (u * math.cos(t) - v * math.sin(t)) / k, 35 + u * math.sin(t) + v * math.cos(t)]
              for u, v in ((0, 0), (0.2, 0), (0.2, 0.05), (0, 0.05), (0, 0))]
    again = rotated_box([turned])
    assert abs(_km2(again) - _km2(turned)) < 1e-6 * _km2(turned)
    assert all(min(math.dist(p, q) for q in again) < 1e-9 for p in turned)
    # A box along the meridians: the same box. Several parts: one box round all of them. A line: its box.
    assert sorted(map(tuple, rotated_box(TOKYO)[:4])) == sorted(map(tuple, TOKYO[0][:4]))
    both = rotated_box([OUTER, OTHER])
    assert all(in_rings([_grown(both)], x, y) for x, y in OUTER + OTHER)
    assert _km2(both, 0) >= _km2(OUTER, 0) + _km2(OTHER, 0)
    assert rotated_box([[[0, 0], [1, 1], [2, 2]]]) == [[0, 0], [2, 0], [2, 2], [0, 2], [0, 0]]
    assert rotated_box([]) == []


def test_area_shape_is_checked():
    problems = []
    export_systems_of([{"system": "S", "format": "csv", "columns": [{"key": "a", "source": "id"}], "areaShape": "circle"}], problems)
    assert any("areaShape" in p for p in problems)
    assert _system(columns=[{"key": "a", "source": "id"}]).area_shape == "polygon"
    assert _system(columns=[{"key": "a", "source": "id"}], areaShape="rotatedBox").area_shape == "rotatedBox"


def test_a_rotated_box_system_writes_the_rectangle_and_the_others_the_polygon():
    columns = [{"key": "ID", "source": "id"}, {"key": "範囲", "source": "geometry", "geometry": "geojson", "digits": 6},
               {"key": "西", "source": "bboxWest", "digits": 6}]
    reqs = [request("R-U", rings=[U]), request("R-P", rings=square_rings(139.8, 35.5, 10), shape_kind="点", priority=600)]

    def rows(system):
        cands = allocate(gather(reqs, DAY, SATS, {}, set()), [system], SATS)
        taken = [c for c in cands if c.result == TAKEN]
        text, _ = csv_text(taken, Context(system, DAY, FIRST))
        return {r[0]: r for r in list(csv.reader(io.StringIO(text)))[1:]}

    plain = rows(_system(columns=columns, orbit=False))
    assert json.loads(plain["R-U"][1]) == {"type": "Polygon", "coordinates": [U]}
    boxed = rows(_system(columns=columns, orbit=False, areaShape="rotatedBox"))
    ring = json.loads(boxed["R-U"][1])["coordinates"][0]
    assert len(ring) == 5 and all(abs(a - b) < 1e-5 for p, q in zip(ring, rotated_box([U])) for a, b in zip(p, q))
    # The box's west is the rectangle's (the corners stick out past the U's own box).
    assert float(boxed["R-U"][2]) < float(plain["R-U"][2])
    # A point's square is written as it is.
    assert boxed["R-P"][1] == plain["R-P"][1]
