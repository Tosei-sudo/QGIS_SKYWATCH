"""
Many alike things (core.obs.stamps, core.obs.similar): the 見本 as said and
put down, rows and grids laid out, how many fit, how they lie; and the
search for things like the 見本 on a drawn picture (turned ones too, a
lighter one too, the recorded ones and flat ground left out), what it
learns from more 見本 and from places checked as wrong, and the quarter
turns made by re-ordering cells.
"""
import math

import numpy as np
import pytest

from skywatch.core.obs.geo import bearing, get_distance
from skywatch.core.obs.model import Observation
from skywatch.core.obs.similar import (
    ANGLES_ALL, ANGLES_NEAR, BINS, Ground, Model, Shape, _quarter, _Turn, cells, confidence, find_similar, gray_of, window_at,
)
from skywatch.core.obs.stamps import (
    Layout, Sample, default_count, entry_of, extent_along, grid_places, row_heading, row_places, sample_of, sample_text, size_text,
)

A = (139.70, 35.60)


def record(**changes) -> Observation:
    base = dict(id="r1", image_id="d1.tif", image_name="d1.tif", image_time=None, object_id="o1", object_name="乗用車-3",
                kind="車両 > 乗用車", activity="駐車", present=True, note="", lon=139.7, lat=35.6, created_at=1, updated_at=1,
                length=4.5, heading=45.0, confidence="推定", width=1.8)
    base.update(changes)
    return Observation(**base)


def test_a_record_as_the_sample_and_what_it_puts_down():
    s = sample_of(record())
    assert (s.kind, s.activity, s.confidence, s.length, s.width, s.heading, s.record_id, s.measured) == (
        "車両 > 乗用車", "駐車", "推定", 4.5, 1.8, 45.0, "r1", True)
    assert sample_text(s) == "乗用車-3（乗用車・駐車・推定・4.5 × 1.8 m・045°）"
    assert sample_text(sample_of(record(present=False, length=None, width=None, heading=None, confidence=""))) == "乗用車-3（乗用車）"
    assert sample_text(None).startswith("見本なし")
    assert size_text(120.0, None) == "120 m" and size_text(None, 3.0) == ""
    e = entry_of(s, 139.71, 35.61)
    assert (e.object, e.kind, e.activity, e.confidence, e.length, e.width, e.heading, e.lon) == ("", "車両 > 乗用車", "駐車", "推定", 4.5, 1.8, 45.0, 139.71)
    assert entry_of(s, 0, 0, heading=370).heading == 10
    assert entry_of(Sample("船舶"), 0, 0).heading is None and not Sample("船舶").measured


def test_a_row_and_a_grid_are_laid_out_evenly():
    b = (139.7011, 35.60)
    places = row_places(A, b, 5)
    assert len(places) == 5 and places[0] == pytest.approx(A) and places[-1] == pytest.approx(b)
    gaps = [get_distance(places[i], places[i + 1]) for i in range(4)]
    assert max(gaps) - min(gaps) < 0.01 and gaps[0] == pytest.approx(get_distance(A, b) / 4, rel=1e-3)
    assert row_places(A, b, 1) == [A]
    c = (139.7003, 35.6002)
    grid = grid_places(A, b, c, 4, 3)
    assert len(grid) == 12 and grid[0] == pytest.approx(A) and grid[3] == pytest.approx(b) and grid[8] == pytest.approx(c)
    # Each row parallel to the first (a parallelogram).
    assert bearing(grid[8], grid[11]) == pytest.approx(bearing(A, b), abs=0.01)


def test_how_the_things_of_a_row_lie_and_how_many_fit():
    assert row_heading("sample", 90, 30) == 30
    assert row_heading("along", 90, 260) == 270 and row_heading("along", 90, None) == 90
    assert row_heading("across", 90, 10) == 0 and row_heading("across", 90, 170) == 180 and row_heading("across", 90, None) == 180
    # A car 4.5 × 1.8 m across a row reaches 1.8 m along it, along a row 4.5 m.
    assert extent_along(4.5, 1.8, 0, 90) == pytest.approx(1.8)
    assert extent_along(4.5, 1.8, 90, 90) == pytest.approx(4.5)
    assert extent_along(None, 1.8, 0, 90) is None
    b = (139.7, 35.6)
    east = (b[0] + 20 / (111_195 * math.cos(math.radians(35.6))), b[1])
    s = Sample("車両", length=4.5, width=1.8, heading=0)
    # 20 m: ten cars side by side (1.8 m and a tenth between), five end to end.
    assert default_count(b, east, s, 0) == 11
    assert default_count(b, east, s, 90) == 5
    assert default_count(b, east, Sample("車両"), None) == 2


def test_a_layout_follows_the_clicks_the_counts_and_what_is_left_out():
    L = Layout(columns=3)
    assert not L.ready and L.places() == [] and L.needs == 2
    L.corners = (A,)
    b = (139.7005, 35.60)
    # The pointer stands in for the next corner.
    assert len(L.places(cursor=b)) == 3
    L.corners = (A, b)
    assert L.ready and len(L.places()) == 3
    L.skipped = frozenset({1})
    assert L.kept() == [L.places()[0], L.places()[2]]
    along, across = L.spacing()
    assert along == pytest.approx(get_distance(A, b) / 2) and across is None
    L.shape, L.rows = "grid", 2
    assert not L.ready and L.needs == 3
    L.corners = (A, b, (139.70, 35.6001))
    assert len(L.places()) == 6 and L.spacing()[1] == pytest.approx(get_distance(A, (139.70, 35.6001)))
    L.how = "across"
    assert (L.heading(Sample("車両", heading=350)) + 1) % 360 == pytest.approx(1, abs=1e-3)
    assert L.heading(None) == pytest.approx(180, abs=1e-3)


# ---- the search ------------------------------------------------------------------


def car(img, cx, cy, heading, length=18, width=8, value=200.0):
    """A bright box with a dark windscreen near its front (so its heading shows)."""
    yy, xx = np.mgrid[0:img.shape[0], 0:img.shape[1]]
    p = math.radians(heading)
    dx, dy = xx - cx, yy - cy
    v = dx * math.sin(p) - dy * math.cos(p)
    u = dx * math.cos(p) + dy * math.sin(p)
    img[(np.abs(v) <= length / 2) & (np.abs(u) <= width / 2)] = value
    img[(v > length / 2 - 5) & (v <= length / 2 - 2) & (np.abs(u) <= width / 2 - 1)] = 60.0


def picture():
    rng = np.random.default_rng(1)
    img = rng.normal(90, 6, (300, 520))
    lined = [(40 + i * 14, 60) for i in range(8)]
    for x, y in lined:
        car(img, x, y, 0)
    turned = [(280 + i * 40, 200, 90 + i * 30) for i in range(5)]
    for i, (x, y, h) in enumerate(turned):
        car(img, x, y, h, value=150 + i * 20)
    return img, lined, turned


def test_finds_the_things_like_the_sample_turned_or_lighter_but_not_the_ones_recorded():
    img, lined, turned = picture()
    valid = np.ones(img.shape, bool)
    taken = [lined[0], lined[1]]
    found = find_similar(img, valid, img, valid, lined[0][0], lined[0][1], 0, 18, 8, ANGLES_ALL, 0.6, taken=taken)
    # The things come first, each where it is (within a pixel) and as it lies; the recorded ones not again.
    first = found[:11]
    expected = [*[(x, y, 0) for x, y in lined[2:]], *turned]

    def at(x, y):
        [f] = [f for f in first if math.hypot(f.x - x, f.y - y) <= 1.0]
        return f

    for x, y, h in expected:
        assert abs((at(x, y).heading - h + 180) % 360 - 180) <= 7.5
    assert all(f.score > 0.99 for f in first)
    assert not any(math.hypot(f.x - x, f.y - y) < 4 for f in found for x, y in taken)
    # Near the 見本's heading only (either way round): the lined-up ones and those turned 150–210°, as turned at most 30°.
    near = find_similar(img, valid, img, valid, lined[0][0], lined[0][1], 0, 18, 8, ANGLES_NEAR, 0.6, taken=taken)
    for x, y in [*lined[2:], (360, 200), (400, 200), (440, 200)]:
        assert any(math.hypot(f.x - x, f.y - y) <= 1.0 for f in near[:9])
    assert all(min(abs((f.heading + 90) % 180 - 90), 180) <= 30 for f in near)


def test_a_picture_without_data_or_flat_finds_nothing_and_progress_is_said():
    img, lined, _ = picture()
    valid = np.ones(img.shape, bool)
    flat = np.full((200, 200), 90.0)
    said = []
    assert find_similar(flat, np.ones(flat.shape, bool), img, valid, lined[0][0], lined[0][1], 0, 18, 8, (0, 90), 0.5,
                        progress=said.append) == []
    assert said == [0.5, 1.0]
    # Where the picture has no data nothing is found.
    nothing = np.zeros(img.shape, bool)
    assert find_similar(img, nothing, img, valid, lined[0][0], lined[0][1], 0, 18, 8, (0,), 0.5) == []
    # Nor where the 見本 has none.
    assert find_similar(img, valid, img, nothing, lined[0][0], lined[0][1], 0, 18, 8, (0,), 0.5) == []
    rgba = np.zeros((2, 2, 4), np.uint8)
    rgba[0, 0] = (255, 255, 255, 255)
    gray, ok = gray_of(rgba)
    assert gray[0, 0] == pytest.approx(255) and ok.tolist() == [[True, False], [False, False]]
    shape = Shape(18, 8)
    assert (shape.cell, shape.rows, shape.cols) == (2, 11, 6)
    assert window_at(img, valid, 40, 60, 0, shape).shape == (11 * 6 * BINS,)
    assert window_at(img, valid, 2, 2, 0, shape) is None
    assert confidence(4.5) == pytest.approx(0.5) and confidence(6.0) > 0.8 > 0.2 > confidence(3.0)


def test_a_quarter_turn_is_the_same_cells_reordered():
    img, _, _ = picture()
    valid = np.ones(img.shape, bool)
    size = 600
    a = _Turn(img.shape, 30, size)
    b = _Turn(img.shape, 120, size)
    f, has = cells(*a.draw(img, valid), 3)
    g, has_g = cells(*b.draw(img, valid), 3)
    q, has_q = _quarter(f, has, 1)
    inner = has_g & has_q
    assert inner.sum() > 1000
    assert np.abs(q[inner] - g[inner]).mean() < 0.02 * np.abs(g[inner]).mean() + 1e-3
    # A place on the turned picture is the same place on the picture.
    x, y = a.to_picture(300.0, 250.0)
    u, v = b.to_picture(250.0, size - 1 - 300.0)
    assert (x, y) == pytest.approx((u, v))


def test_more_samples_and_places_checked_wrong_are_learnt():
    """A box of a car's size without its windscreen: told wrong once, it scores lower against the cars."""
    rng = np.random.default_rng(2)
    img = rng.normal(90, 12, (200, 300))
    cars = [(40 + i * 50, 50) for i in range(5)]
    boxes = [(40 + i * 50, 140) for i in range(5)]
    for x, y in cars:
        car(img, x, y, 0)
    for x, y in boxes:
        img[y - 9:y + 10, x - 4:x + 5] = 200.0
        img[y - 1:y + 2, x - 4:x + 5] = 60.0
    valid = np.ones(img.shape, bool)
    shape = Shape(18, 8)
    f, has = cells(img, valid, shape.cell)
    from skywatch.core.obs.similar import _windows
    ground = Ground(_windows(f, has, shape, 3000, np.random.default_rng(0)), shape.size)
    at = [window_at(img, valid, x, y, 0, shape) for x, y in cars]
    wrong = [window_at(img, valid, x, y, 0, shape) for x, y in boxes]
    one = Model(ground, at[:1])
    learnt = Model(ground, at[:2], wrong[:1])
    gap_one = min(one.score(w) for w in at[2:]) - max(one.score(w) for w in wrong[1:])
    gap_learnt = min(learnt.score(w) for w in at[2:]) - max(learnt.score(w) for w in wrong[1:])
    assert gap_learnt > gap_one
    assert all(learnt.score(c) > learnt.score(b) for c in at[2:] for b in wrong[1:])
    # The ground scores 0 on average, its spread 1.
    scores = np.array([one.score(w) for w in _windows(f, has, shape, 500, np.random.default_rng(1))])
    assert abs(scores.mean()) < 0.5 and 0.5 < scores.std() < 1.5
    # find_similar takes them too.
    found = find_similar(img, valid, img, valid, cars[0][0], cars[0][1], 0, 18, 8, (0, 180), 0.6, taken=[cars[0], cars[1], boxes[0]],
                         alike=[(cars[1][0], cars[1][1], 0.0)], wrong=[boxes[0]])
    for x, y in cars[2:]:
        assert any(math.hypot(f.x - x, f.y - y) <= 1.0 for f in found[:3])
