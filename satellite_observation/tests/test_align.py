"""自動位置合わせ: the viewer's align.test.ts cases on core.align."""
import math

import numpy as np
import pytest

from satellite_observation.core.align import (
    TiePoint, apply_affine, describe_alignment, fit_affine, invert_affine, phase_correlate, robust_fit,
)

SIZE = 256


def _noise():
    seed = 12345
    raw = np.empty(SIZE * SIZE)
    for i in range(SIZE * SIZE):
        seed = (seed * 1103515245 + 12345) % 2 ** 31
        raw[i] = seed / 2 ** 31
    raw = raw.reshape(SIZE, SIZE)
    out = np.zeros((SIZE, SIZE))
    out[1:-1, 1:-1] = (raw[1:-1, 1:-1] * 4 + raw[1:-1, :-2] + raw[1:-1, 2:] + raw[:-2, 1:-1] + raw[2:, 1:-1]) / 8
    return out


NOISE = _noise()


def scene(x, y):
    x, y = x + 64, y + 64
    i, j = np.floor(x).astype(int), np.floor(y).astype(int)
    fx, fy = x - i, y - j
    v = lambda a, b: NOISE[b, a]  # noqa: E731
    return (v(i, j) * (1 - fx) + v(i + 1, j) * fx) * (1 - fy) + (v(i, j + 1) * (1 - fx) + v(i + 1, j + 1) * fx) * fy


def grid(n, f):
    y, x = np.mgrid[0:n, 0:n].astype(float)
    return f(x, y).astype(np.float32)


def test_finds_a_whole_pixel_shift():
    c = phase_correlate(grid(64, scene), grid(64, lambda x, y: scene(x - 5, y + 3)), 64)
    assert c.dx == pytest.approx(5, abs=0.05) and c.dy == pytest.approx(-3, abs=0.05) and c.strength > 1.5


def test_finds_a_fraction_of_a_pixel_whatever_the_brightness():
    c = phase_correlate(grid(128, scene), grid(128, lambda x, y: 3 * scene(x - 2.4, y - 1.3) + 100), 128)
    assert abs(c.dx - 2.4) < 0.25 and abs(c.dy - 1.3) < 0.25


def test_gives_up_on_a_flat_grid_or_one_mostly_without_data():
    assert phase_correlate(np.zeros(64), grid(8, scene).ravel(), 8) is None
    assert phase_correlate(grid(8, scene), np.full(64, np.nan), 8) is None


T = (10, 1.001, 0.002, -5, -0.002, 0.999)
POINTS = [TiePoint(i * 100, j * 100, *apply_affine(T, i * 100, j * 100)) for i in range(4) for j in range(4)]


def test_fits_an_affine_exactly_and_inverts_it():
    fit = fit_affine(POINTS)
    assert fit == pytest.approx(T, abs=1e-6)
    x, y = apply_affine(invert_affine(fit), *apply_affine(fit, 12, 34))
    assert (x, y) == pytest.approx((12, 34), abs=1e-6)


def test_drops_an_outlier_and_fits_a_shift_by_the_median():
    bad = POINTS + [TiePoint(50, 50, 500, 500)]
    transform, kept, residual = robust_fit(bad, "affine")
    assert len(kept) == len(POINTS) and residual < 1e-6
    shift, _, _ = robust_fit(bad, "shift")
    assert shift[1] == 1 and shift[0] == pytest.approx(10, abs=0.5)


def test_describes_the_correction():
    assert describe_alignment((3, 1, 0, -2, 0, 1), [], 16, math.nan) == "ずれ 東 3.0 m・北 -2.0 m、全体の照合のみ"
    text = describe_alignment(T, POINTS, 16, 0.12)
    assert text.startswith("ずれ 東 ") and "回転 " in text and text.endswith("照合 16/16 か所、残差 0.12 px")
