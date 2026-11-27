"""
観測記録をピクセル座標で書き出す: the arithmetic (core/ai/pixel_boxes.py) —
a ground rectangle's corners in pixels made the box along its length, the
angle clockwise from the image's up, the box of a record without a heading,
and the GeoJSON written.
"""
import json
import math

import pytest

from mikage.core.ai.pixel_boxes import (
    angle_of, box_properties, collection, counterclockwise, feature, turned_box, upright_box,
)


def offsets(length, width, angle_deg, sx=1.0, sy=1.0):
    """A rectangle's corners (rear-left, front-left, front-right, rear-right) in pixels from its centre, front at `angle_deg`
    clockwise from the image's up, the pixels `sx` × `sy` times the ground's."""
    a = math.radians(angle_deg)
    ux, uy = math.sin(a), -math.cos(a)
    vx, vy = -uy, ux
    out = []
    for sl, sw in ((-1, -1), (1, -1), (1, 1), (-1, 1)):
        gx = sl * length / 2 * ux + sw * width / 2 * vx
        gy = sl * length / 2 * uy + sw * width / 2 * vy
        out.append((gx * sx, gy * sy))
    return out


@pytest.mark.parametrize("dx, dy, want", [(0, -1, 0), (1, 0, 90), (0, 1, 180), (-1, 0, 270), (1, -1, 45), (-1, -1, 315)])
def test_angle_is_clockwise_from_the_images_up(dx, dy, want):
    assert angle_of(dx, dy) == pytest.approx(want)


@pytest.mark.parametrize("angle", [0, 30, 90, 135, 200, 359])
def test_a_rectangle_keeps_its_size_and_angle(angle):
    box = turned_box(100, 50, offsets(40, 10, angle))
    assert (box.cx, box.cy) == pytest.approx((100, 50))
    assert box.length == pytest.approx(40)
    assert box.width == pytest.approx(10)
    assert box.angle == pytest.approx(angle % 360, abs=1e-9)
    want = [(100 + x, 50 + y) for x, y in offsets(40, 10, angle)]
    for got, w in zip(box.corners, want):
        assert got == pytest.approx(w)


def test_corners_are_rear_left_front_left_front_right_rear_right():
    """Facing the image's up: the rear side at the bottom, the left at the left (x right, y down)."""
    box = turned_box(0, 0, offsets(20, 4, 0))
    assert box.corners == [pytest.approx(p) for p in [(-2, 10), (-2, -10), (2, -10), (2, 10)]]


def test_pixels_not_square_on_the_ground_make_the_box_round_the_parallelogram():
    """A SAR image (3 m pixels along the track, 1 m across): a ship at 45° on the ground is a parallelogram in the pixels."""
    sheared = offsets(30, 6, 45, sx=1.0, sy=1 / 3)
    box = turned_box(0, 0, sheared)
    assert box.angle == pytest.approx(math.degrees(math.atan2(1, 1 / 3)))
    # Every corner of the parallelogram inside the box.
    a = math.radians(box.angle)
    ux, uy = math.sin(a), -math.cos(a)
    for x, y in sheared:
        assert abs(x * ux + y * uy) <= box.length / 2 + 1e-9
        assert abs(-x * uy + y * ux) <= box.width / 2 + 1e-9
    # Its length along the axis: from the rear's middle to the front's.
    assert box.length == pytest.approx(max(x * ux + y * uy for x, y in sheared) * 2)


def test_no_direction_no_box():
    assert turned_box(0, 0, [(0, 0)] * 4) is None
    assert turned_box(0, 0, [(0, 0)] * 3) is None


def test_a_record_without_heading_is_upright_and_holds_its_circle():
    # 2 px a metre east, 1 px a metre north (radius 10 m).
    box = upright_box(50, 60, (20, 0), (0, -10))
    assert box.angle is None
    assert (box.length, box.width) == (20, 40)
    assert box.corners == [(30, 70), (30, 50), (70, 50), (70, 70)]
    # Turned image (north to the right): the circle's box still along the image's axes.
    turned = upright_box(0, 0, (0, 10), (10, 0))
    assert (turned.length, turned.width) == (20, 20)


def test_properties_are_rounded():
    box = turned_box(10.123456, 20.987654, offsets(12.3456, 4.5678, 33.333333))
    p = box_properties(box)
    assert p["cx"] == 10.12 and p["cy"] == 20.99
    assert p["length_px"] == 12.35 and p["width_px"] == 4.57
    assert p["angle"] == 33.33
    assert len(p["corners_px"]) == 8 and p["corners_px"][:2] == [round(box.corners[0][0], 2), round(box.corners[0][1], 2)]
    assert box_properties(upright_box(0, 0, (1, 0), (0, -1)))["angle"] is None


def test_outer_ring_counterclockwise_and_closed():
    clockwise = [(0, 0), (0, 1), (1, 1), (1, 0)]
    ring = counterclockwise(clockwise)
    assert ring[0] == ring[-1]
    assert sum(x1 * y2 - x2 * y1 for (x1, y1), (x2, y2) in zip(ring, ring[1:])) > 0
    assert counterclockwise(ring) == ring


def test_the_file_is_geojson_with_its_notes():
    f = feature({"obs_id": "r1", "cx": 1.0}, [(139.0, 35.0), (139.0, 35.001), (139.001, 35.001), (139.001, 35.0)])
    nowhere = feature({"obs_id": "r2"}, None)
    data = json.loads(json.dumps(collection([f, nowhere], [{"image_id": "a.tif"}], "out", created="2026-11-27T00:00:00+00:00")))
    assert data["type"] == "FeatureCollection" and data["name"] == "out"
    assert data["mikage"]["format"] == "mikage-pixel-boxes" and data["mikage"]["images"] == [{"image_id": "a.tif"}]
    assert {"pixels", "angle", "corners", "geometry"} <= set(data["mikage"]["notes"])
    poly = data["features"][0]["geometry"]
    assert poly["type"] == "Polygon" and len(poly["coordinates"][0]) == 5
    assert data["features"][1]["geometry"] is None
