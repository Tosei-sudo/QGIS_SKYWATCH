"""
Statistics and automatic stretching (DRA), as browser-image-enhancement's
core/histogram.ts: 256-bin histograms of 8-bit codes that add up across tiles,
and `autoStretch` steps turned into fixed `stretch` steps from them.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Optional, Sequence

import numpy as np

from .ops import COLOR_ONLY_OPS, for_gray, is_identity, normalize_auto_stretch, to_stage
from .srgb import LUMA_B, LUMA_G, LUMA_R, SRGB_TO_LINEAR, linear_to_srgb, quantize


@dataclass
class Histogram:
    #: 'rgb' (three channels) or 'gray' (one).
    mode: str
    bins: list = field(default_factory=list)
    count: int = 0


def empty_histogram(mode: str) -> Histogram:
    return Histogram(mode, [np.zeros(256) for _ in range(3 if mode == "rgb" else 1)], 0)


def gray_codes(rgba: np.ndarray) -> np.ndarray:
    """Each pixel's gray code: its value when R = G = B, else its Rec. 709 luminance (linear light)."""
    r, g, b = rgba[..., 0], rgba[..., 1], rgba[..., 2]
    same = (r == g) & (g == b)
    if same.all():
        return r.copy()
    y = LUMA_R * SRGB_TO_LINEAR[r] + LUMA_G * SRGB_TO_LINEAR[g] + LUMA_B * SRGB_TO_LINEAR[b]
    return np.where(same, r, quantize(y)).astype(np.uint8)


def count_pixels(rgba: np.ndarray, mode: str) -> Histogram:
    """The histogram of an H×W×4 uint8 image; pixels with alpha 0 are not counted."""
    seen = rgba[..., 3] != 0
    h = empty_histogram(mode)
    if mode == "rgb":
        h.bins = [np.bincount(rgba[..., c][seen], minlength=256).astype(np.float64) for c in range(3)]
    else:
        h.bins = [np.bincount(gray_codes(rgba)[seen], minlength=256).astype(np.float64)]
    h.count = int(seen.sum())
    return h


def merge_histograms(histograms: Sequence[Histogram]) -> Histogram:
    mode = "gray" if histograms and all(h.mode == "gray" for h in histograms) else "rgb"
    out = empty_histogram(mode)
    for h in histograms:
        bins = h.bins if mode == "gray" or h.mode == "rgb" else [h.bins[0]] * 3
        for c in range(len(out.bins)):
            out.bins[c] = out.bins[c] + bins[c]
        out.count += h.count
    return out


def range_of(values: np.ndarray, counts: np.ndarray, o: dict) -> Optional[tuple]:
    """[black, white] for a distribution (values ascending, their counts), or None when nothing can be stretched."""
    keep = counts > 0
    values = values[keep]
    counts = counts[keep]
    total = counts.sum()
    if total == 0:
        return None
    lo, hi = values[0], values[-1]
    if o["method"] == "standardDeviation":
        mean = float((values * counts).sum() / total)
        sd = math.sqrt(float((((values - mean) ** 2) * counts).sum() / total))
        black = max(lo, mean - o["stdDevs"] * sd)
        white = min(hi, mean + o["stdDevs"] * sd)
    else:
        low = 0 if o["method"] == "minMax" else total * o["lowPercent"] / 100
        high = 0 if o["method"] == "minMax" else total * o["highPercent"] / 100
        # The first value whose running count passes `low`, from each end.
        cum = np.cumsum(counts)
        i = int(np.searchsorted(cum, low, side="right"))
        black = values[min(i, len(values) - 1)]
        rcum = np.cumsum(counts[::-1])
        j = int(np.searchsorted(rcum, high, side="right"))
        white = values[max(len(values) - 1 - j, 0)]
    black, white = float(black), float(white)
    return (black, white) if white > black else None


def _distribution(bins: Sequence[np.ndarray], fns) -> tuple:
    values = np.concatenate([fn(np.arange(256)) for fn in fns])
    counts = np.concatenate(list(bins))
    order = np.argsort(values, kind="stable")
    return values[order], counts[order]


def _codes(k: np.ndarray) -> np.ndarray:
    return k / 255.0


def stretch_range(h: Histogram, options: Optional[dict] = None, fns=None) -> dict:
    """Black and white points (sRGB-encoded, per channel) for a histogram; channels with nothing to stretch stay 0, 1."""
    o = normalize_auto_stretch(options)
    fns = fns or [_codes] * len(h.bins)
    black = [0.0, 0.0, 0.0]
    white = [1.0, 1.0, 1.0]
    if o["linked"] or h.mode == "gray":
        r = range_of(*_distribution(h.bins, fns), o)
        if r:
            black = [r[0]] * 3
            white = [r[1]] * 3
    else:
        for c in range(3):
            r = range_of(*_distribution([h.bins[c]], [fns[c]]), o)
            if r:
                black[c], white[c] = r
    return {"black": black, "white": white}


def _value_after(fns, c: int):
    def f(k: np.ndarray) -> np.ndarray:
        v = SRGB_TO_LINEAR[k]
        for fn in fns:
            v = fn(v, c)
        return linear_to_srgb(v)
    return f


def resolve_ops(ops: Sequence[dict], stats: Optional[Histogram]) -> list:
    """
    Every `autoStretch` replaced with a `stretch` from `stats`, using the values
    as they reach it (per-channel steps before it applied to the histogram's
    codes; saturation and sharpen cannot be followed and are ignored). With
    `stats` None, `autoStretch` steps are dropped.
    """
    out: list = []
    for op in ops:
        if op["op"] != "autoStretch":
            out.append(op)
            continue
        if stats is None:
            continue
        before = [p for p in out if not is_identity(p) and not (stats.mode == "gray" and p["op"] in COLOR_ONLY_OPS)]
        fns = []
        for p in before:
            s = to_stage(for_gray(p) if stats.mode == "gray" else p)
            if s[0] == "channel":
                fns.append(s[1])
        values = [_value_after(fns, c) for c in range(len(stats.bins))] if fns else None
        out.append({"op": "stretch", **stretch_range(stats, op, values)})
    return out
