"""アノテーション変換 (core.ai.annotation_boxes): rectangles of outlines, the rules for direction, length and width."""
import math

import numpy as np
import pytest

from mikage.core.ai.annotation_boxes import (
    ConvertError, Rules, Shape, convert, dota_row, is_point_box, length_for, mask_shape, min_area_rect, parse_lengths, threshold_mask,
    window_at, window_side,
)


def bar(angle: float, length=60, width=12, size=200, cx=100.0, cy=100.0):
    """A mask of a bar turned `angle` degrees clockwise from up."""
    ys, xs = np.mgrid[0:size, 0:size] + 0.5
    a = math.radians(angle)
    ux, uy = math.sin(a), -math.cos(a)
    along = (xs - cx) * ux + (ys - cy) * uy
    across = (xs - cx) * -uy + (ys - cy) * ux
    return ((abs(along) <= length / 2) & (abs(across) <= width / 2)).astype(np.uint8)


@pytest.mark.parametrize("angle", [0, 30, 75, 90, 135, 170])
def test_the_outline_of_a_bar_gives_its_direction_and_sides(angle):
    s = mask_shape(bar(angle), 100, 100)
    assert s is not None
    assert abs(((s.angle - angle + 90) % 180) - 90) < 3
    assert s.length == pytest.approx(60, abs=3) and s.width == pytest.approx(12, abs=3)
    assert s.cx == pytest.approx(100, abs=1.5) and s.cy == pytest.approx(100, abs=1.5)


def test_an_outline_filling_the_window_or_nothing_is_no_object():
    assert mask_shape(np.zeros((50, 50), np.uint8), 25, 25) is None
    assert mask_shape(np.ones((50, 50), np.uint8), 25, 25) is None
    # A band across the whole window (a coastline, not a ship).
    band = np.zeros((50, 50), np.uint8)
    band[20:26, :] = 1
    assert mask_shape(band, 25, 22) is None


def test_the_part_under_the_point_is_taken():
    m = bar(0, cx=60) | bar(90, cx=150, length=40)
    s = mask_shape(m, 150, 100)
    assert abs(((s.angle - 90 + 90) % 180) - 90) < 3 and s.length == pytest.approx(40, abs=3)


def test_min_area_rect_of_a_square_and_too_few_points():
    s = min_area_rect([(0, 0), (10, 0), (10, 10), (0, 10)])
    assert s.length == pytest.approx(10) and s.width == pytest.approx(10)
    assert min_area_rect([(0, 0), (1, 1)]) is None


def test_the_threshold_takes_the_side_the_point_is_on():
    rgba = np.zeros((100, 100, 4), np.uint8)
    rgba[..., 3] = 255
    rgba[..., :3] = 30
    rgba[45:55, 20:80, :3] = 220
    assert threshold_mask(rgba, 50, 50)[50, 50] == 1 and threshold_mask(rgba, 50, 50)[10, 10] == 0
    # A dark object on a bright ground.
    inverse = rgba.copy()
    inverse[..., :3] = 255 - rgba[..., :3]
    m = threshold_mask(inverse, 50, 50)
    assert m[50, 50] == 1 and m[10, 10] == 0
    s = mask_shape(m, 50, 50)
    assert abs(((s.angle - 90 + 90) % 180) - 90) < 3 and s.length == pytest.approx(60, abs=2)


SHAPE = Shape(100, 100, 50, 10, 45)


def test_attributes_come_first():
    box, _ = convert(10, 20, 0.5, Rules(length=99), SHAPE, "SAM", (), length=30, width=8, heading=200)
    assert (box.length_m, box.width_m, box.angle) == (30, 8, 200)
    assert (box.length, box.width) == (60, 16) and (box.cx, box.cy) == (10, 20)
    assert box.size_from == "属性・幅 属性" and box.dir_from == "属性"


def test_the_length_given_and_the_width_by_the_outlines_ratio():
    box, _ = convert(10, 20, 1.0, Rules(length=100), SHAPE, "SAM")
    assert box.length_m == 100 and box.width_m == pytest.approx(20) and box.angle == 45
    # Moved to the outline's centre (the point may be off the middle).
    assert (box.cx, box.cy) == (100, 100)
    assert box.size_from == "入力値・幅 SAMの縦横比" and box.dir_from == "SAM"
    stay, _ = convert(10, 20, 1.0, Rules(length=100, recenter=False), SHAPE, "SAM")
    assert (stay.cx, stay.cy) == (10, 20)


def test_lengths_by_class_and_their_upper_levels():
    rules = Rules(length=10, lengths={"船舶": 120, "car": 4})
    assert convert(0, 0, 1, rules, SHAPE, "SAM", ["船舶 > 支援船"])[0].length_m == 120
    assert convert(0, 0, 1, rules, SHAPE, "SAM", ["car", "車両"])[0].length_m == 4
    assert convert(0, 0, 1, rules, SHAPE, "SAM", ["航空機"])[0].length_m == 10
    assert length_for(["a > b > c"], {"a > b": 3.0}) == 3.0


def test_width_modes():
    measured, _ = convert(0, 0, 2.0, Rules(length=0, width_mode="measured"), SHAPE, "SAM")
    assert measured.length_m == 100 and measured.width_m == 20 and measured.size_from == "SAM・幅 SAM"
    ratio, _ = convert(0, 0, 1.0, Rules(length=40, width_mode="ratio", ratio=0.5), SHAPE, "SAM")
    assert ratio.width_m == 20
    fixed, _ = convert(0, 0, 1.0, Rules(length=40, width_mode="fixed", width=7), SHAPE, "SAM")
    assert fixed.width_m == 7
    # No outline: the ratio stands in.
    none, _ = convert(0, 0, 1.0, Rules(length=40, ratio=0.25), None, "SAM", heading=10)
    assert none.width_m == 10 and none.size_from == "入力値・幅 比率"


def test_a_point_without_direction():
    square, _ = convert(5, 5, 1.0, Rules(length=40), None)
    assert square.angle is None and square.dir_from == "不明"
    xs = [c[0] for c in square.corners()]
    assert max(xs) - min(xs) == pytest.approx(40)
    north, _ = convert(5, 5, 1.0, Rules(length=40, unknown="north"), None)
    assert north.angle == 0 and north.dir_from == "不明（北向き）"
    assert convert(5, 5, 1.0, Rules(length=40, unknown="skip"), None) == (None, "向きが決まらない")
    assert convert(5, 5, 1.0, Rules(), None)[0] is None


def test_corners_run_along_the_direction():
    box, _ = convert(0, 0, 1.0, Rules(length=20, width_mode="fixed", width=4), None, heading=90)
    c = box.corners()
    # East: rear left, front left, front right, rear right (y down: left of east is north, up).
    assert c[0] == pytest.approx((-10, -2)) and c[1] == pytest.approx((10, -2)) and c[2] == pytest.approx((10, 2))


def test_parse_lengths_and_points_and_rows():
    assert parse_lengths("船舶=120, 車両: 5m、 plane → 30") == {"船舶": 120, "車両": 5, "plane": 30}
    assert parse_lengths("") == {}
    with pytest.raises(ConvertError):
        parse_lengths("船舶")
    assert is_point_box(10, 10, 10, 10, 2) and is_point_box(10, 10, 11.5, 12, 2) and not is_point_box(0, 0, 10, 1, 2)
    assert dota_row([(0, 0), (1, 0), (1, 1), (0, 1)], "small car") == "0.00 0.00 1.00 0.00 1.00 1.00 0.00 1.00 small_car 0"


def test_windows():
    assert window_side(10, 1.0) == 64 and window_side(100, 1.0) == 400 and window_side(None, 0.5) == 400
    assert window_side(5000, 1.0) == 4096
    x0, y0 = window_at(130, 140, 100, 1000, 1000)
    assert 25 <= 130 - x0 <= 75 and 25 <= 140 - y0 <= 75
    assert window_at(131, 141, 100, 1000, 1000) == (x0, y0)
