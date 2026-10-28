"""
Automatic alignment of one image to another (the viewer's align.ts): how far
the image to align is off the reference, found by phase correlation (the
peak of the normalized cross-power spectrum, which matches texture whatever
the brightness of the two), first over the whole overlap at a coarse scale,
then in patches at the reference's own resolution. The patches give tie
points, from which a shift (their median) or an affine transformation (least
squares, outliers dropped) is fitted.
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Optional, Sequence

import numpy as np

#: [a, b, c, d, e, f]: u = a + b·x + c·y, v = d + e·x + f·y.
Affine = tuple


@dataclass
class Correlation:
    """How far the second grid's content is moved from the first's, in pixels (x right, y down): b(x) = a(x − d); the peak's height over the next."""
    dx: float
    dy: float
    strength: float


@dataclass
class TiePoint:
    """Where a feature is in the reference (x, y) and where the image to align shows it (u, v), in the same units."""
    x: float
    y: float
    u: float
    v: float


def _prepare(values, n: int) -> Optional[np.ndarray]:
    """The values (n × n) less their mean, NaN as the mean, under a Hann window; None when flat or mostly missing."""
    v = np.asarray(values, dtype=np.float64).reshape(n, n)
    ok = ~np.isnan(v)
    count = int(ok.sum())
    if count < n * n * 0.5:
        return None
    mean = float(v[ok].mean())
    d = np.where(ok, v, mean) - mean
    w = 0.5 - 0.5 * np.cos(2 * np.pi * (np.arange(n) + 0.5) / n)
    energy = float((d * d).sum())
    return d * w[:, None] * w[None, :] if energy > 1e-12 * count else None


def phase_correlate(a, b, n: int) -> Optional[Correlation]:
    """The shift between two n × n grids (n a power of 2), to a fraction of a pixel; None when either is flat or mostly no data."""
    fa, fb = _prepare(a, n), _prepare(b, n)
    if fa is None or fb is None:
        return None
    cross = np.conj(np.fft.fft2(fa)) * np.fft.fft2(fb)
    mag = np.abs(cross)
    cross = np.where(mag > 1e-20, cross / np.where(mag > 1e-20, mag, 1), 0)
    re = np.fft.ifft2(cross).real
    by, bx = np.unravel_index(int(np.argmax(re)), re.shape)
    # The next highest peak away from this one, for how sure the match is.
    ys, xs = np.mgrid[0:n, 0:n]
    ddx = np.minimum(np.abs(xs - bx), n - np.abs(xs - bx))
    ddy = np.minimum(np.abs(ys - by), n - np.abs(ys - by))
    away = (ddx > 2) | (ddy > 2)
    second = max(0.0, float(re[away].max())) if away.any() else 0.0

    def at(x, y):
        return re[(y + n) % n, (x + n) % n]

    def sub(l, c, r):
        d = l - 2 * c + r
        return max(-0.5, min(0.5, (l - r) / (2 * d))) if d < 0 else 0.0

    fx = sub(at(bx - 1, by), at(bx, by), at(bx + 1, by))
    fy = sub(at(bx, by - 1), at(bx, by), at(bx, by + 1))

    def wrap(v):
        return v - n if v > n / 2 else v

    peak = float(re[by, bx])
    return Correlation(wrap(int(bx)) + fx, wrap(int(by)) + fy, peak / second if second > 0 else math.inf)


def apply_affine(t: Affine, x: float, y: float) -> tuple[float, float]:
    return t[0] + t[1] * x + t[2] * y, t[3] + t[4] * x + t[5] * y


def fit_shift(points: Sequence[TiePoint]) -> Affine:
    """The shift of most tie points (the median each way), as an affine transformation."""
    return (float(np.median([p.u - p.x for p in points])), 1, 0, float(np.median([p.v - p.y for p in points])), 0, 1)


def fit_affine(points: Sequence[TiePoint]) -> Optional[Affine]:
    """The least-squares affine transformation through the tie points; None with fewer than 3 or when they are in a line."""
    if len(points) < 3:
        return None
    # About the centroid, for a well-conditioned system.
    cx = sum(p.x for p in points) / len(points)
    cy = sum(p.y for p in points) / len(points)
    m = np.array([[1, p.x - cx, p.y - cy] for p in points], dtype=np.float64)
    normal = m.T @ m
    if abs(np.linalg.det(normal)) < 1e-12:
        return None
    su = np.linalg.solve(normal, m.T @ np.array([p.u for p in points]))
    sv = np.linalg.solve(normal, m.T @ np.array([p.v for p in points]))
    # Back from centroid coordinates.
    return (float(su[0] - su[1] * cx - su[2] * cy), float(su[1]), float(su[2]),
            float(sv[0] - sv[1] * cx - sv[2] * cy), float(sv[1]), float(sv[2]))


def residuals(t: Affine, points: Sequence[TiePoint]) -> list[float]:
    """How far each tie point is from where `t` puts it."""
    out = []
    for p in points:
        u, v = apply_affine(t, p.x, p.y)
        out.append(math.hypot(u - p.u, v - p.v))
    return out


def rms(values: Sequence[float]) -> float:
    return math.sqrt(sum(v * v for v in values) / len(values)) if values else 0.0


def robust_fit(points: Sequence[TiePoint], how: str, floor: float = 0) -> Optional[tuple[Affine, list, float]]:
    """
    Fits `how` ('shift' or 'affine') to the tie points, dropping the worst
    while it lies more than 3 times the median residual (and `floor`) off,
    keeping at least half (and 3 for an affine). Returns the fit, the points
    kept and their RMS residual.
    """
    kept = list(points)
    minimum = 3 if how == "affine" else 1
    if len(kept) < minimum:
        return None
    while True:
        transform = fit_affine(kept) if how == "affine" else fit_shift(kept)
        if transform is None:
            return None
        r = residuals(transform, kept)
        median = sorted(r)[len(r) >> 1]
        worst = max(range(len(r)), key=lambda i: (r[i], -i))
        if r[worst] <= max(3 * median, floor) or len(kept) - 1 < max(minimum, math.ceil(len(points) / 2)):
            return transform, kept, rms(r)
        kept.pop(worst)


def invert_affine(t: Affine) -> Optional[Affine]:
    """The inverse of an affine transformation; None when singular."""
    a, b, c, d, e, f = t
    det = b * f - c * e
    if abs(det) < 1e-15:
        return None
    ib, ic, ie, iff = f / det, -c / det, -e / det, b / det
    return -(ib * a + ic * d), ib, ic, -(ie * a + iff * d), ie, iff


def is_shift(t: Affine) -> bool:
    return t[1] == 1 and t[2] == 0 and t[4] == 0 and t[5] == 1


def describe_alignment(t: Affine, ties: Sequence[TiePoint], tried: int, rms_pixels: float, units: str = " m") -> str:
    """The correction in words: the shift (at the middle of the tie points), rotation and scale for an affine, the residual."""
    cx = sum(p.x for p in ties) / len(ties) if ties else 0
    cy = sum(p.y for p in ties) / len(ties) if ties else 0

    def n(v: float) -> str:
        return f"{v:.6f}" if units == "°" else f"{v:.1f}"

    parts = [f"ずれ 東 {n(t[0] + (t[1] - 1) * cx + t[2] * cy)}{units}・北 {n(t[3] + t[4] * cx + (t[5] - 1) * cy)}{units}"]
    if not is_shift(t):
        rotation = math.degrees(math.atan2(t[4], t[1]))
        scale = math.sqrt(abs(t[1] * t[5] - t[2] * t[4]))
        parts.append(f"回転 {rotation:.3f}°・倍率 {scale:.5f}")
    parts.append(f"照合 {len(ties)}/{tried} か所、残差 {rms_pixels:.2f} px" if ties else "全体の照合のみ")
    return "、".join(parts)
