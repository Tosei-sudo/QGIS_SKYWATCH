"""
The unsharp mask, as browser-image-enhancement's core/filter.ts.

It runs on planes of sRGB-encoded values (float32, one per channel, not
clamped). The blur is a separable Gaussian weighted by alpha: transparent
pixels (no data) add nothing, and pixels outside the array count as
transparent, so a tile processed with `kernel_radius` pixels of margin gives
the pixels of the whole image. The taps are added in the library's order.
"""
from __future__ import annotations

import numpy as np

from .ops import kernel_radius
from .srgb import LUMA_B, LUMA_G, LUMA_R


def gaussian(radius: float) -> np.ndarray:
    """Weights for offsets -r..r (not normalized: the blur divides by the weight it used)."""
    r = kernel_radius(radius)
    i = np.arange(-r, r + 1, dtype=np.float64)
    return np.exp(-(i * i) / (2 * radius * radius))


def _blur(a: np.ndarray, k: np.ndarray, axis: int) -> np.ndarray:
    """Sum of k[i] * a shifted by i - r along `axis`, taps in order; taps beyond the edges are skipped."""
    r = (len(k) - 1) // 2
    n = a.shape[axis]
    out = np.zeros_like(a)
    tmp = np.empty_like(a)
    for i in range(len(k)):
        d = i - r
        lo, hi = max(0, -d), n - max(0, d)
        if hi <= lo:
            continue
        if axis == 0:
            dst, src, t = out[lo:hi], a[lo + d:hi + d], tmp[lo:hi]
        else:
            dst, src, t = out[:, lo:hi], a[:, lo + d:hi + d], tmp[:, lo:hi]
        np.multiply(src, k[i], out=t)
        dst += t
    return out


def _opaque_weights(h: int, w: int, k: np.ndarray) -> np.ndarray:
    """The blurred weights of an opaque H×W image: rows differ only near the top and bottom edges."""
    r = (len(k) - 1) // 2
    small_h = 4 * r + 1
    if h <= small_h:
        return _blur(_blur(np.ones((h, w)), k, 1), k, 0)
    small = _blur(_blur(np.ones((small_h, w)), k, 1), k, 0)
    y = np.arange(h)
    rows = np.where(y < r, y, np.where(y >= h - r, small_h - (h - y), 2 * r))
    return small[rows]


def sharpen_planes(planes: list, alpha: np.ndarray, stage: dict) -> None:
    """Sharpens `planes` (H×W float32, 3 or 1) in place; `alpha` (H×W uint8) 0 leaves a pixel alone."""
    k = gaussian(stage["radius"])
    if len(planes) == 3:
        p0, p1, p2 = (p.astype(np.float64) for p in planes)
        lum = LUMA_R * p0 + LUMA_G * p1 + LUMA_B * p2
    else:
        lum = planes[0].astype(np.float64)
    if (alpha == 255).all():
        # Weight 1 everywhere: 1 * luminance is the luminance itself.
        sn = _blur(_blur(lum, k, 1), k, 0)
        sd = _opaque_weights(*lum.shape, k)
        use = np.ones(lum.shape, dtype=bool)
    else:
        a = alpha / 255.0
        wy = np.where(a > 0, a * lum, 0.0)
        sn = _blur(_blur(wy, k, 1), k, 0)
        sd = _blur(_blur(a, k, 1), k, 0)
        use = (alpha != 0) & (sd > 0)
    with np.errstate(invalid="ignore", divide="ignore"):
        diff = lum - sn / sd
    if stage["threshold"] > 0:
        use &= ~(np.abs(diff) < stage["threshold"])
    d = np.where(use, stage["amount"] * diff, 0.0)
    for p in planes:
        p[...] = np.where(use, (p + d).astype(np.float32), p)
