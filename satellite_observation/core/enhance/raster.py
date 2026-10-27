"""
Rasters beyond 8 bits (16-bit, float), as browser-image-enhancement's
raster.ts: statistics are taken on the raw values, the stretch from them maps
black..white onto 0..255, and the 8-bit picture then goes through the
correction like any other image. Bands are H×W arrays; `valid` marks the
pixels that count (not no data, not NaN, not transparent).
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional, Sequence

import numpy as np

from .ops import normalize_auto_stretch
from .stats import range_of

BINS = 4096


@dataclass
class RasterHistogram:
    #: The value range the bins cover: bin 0 is range[0], the last bin range[1].
    range: tuple
    #: Counts per picture band (1 or 3).
    bins: list
    count: int


def valid_mask(bands: Sequence[np.ndarray], no_data: Sequence = ()) -> np.ndarray:
    """Pixels where every picture band has a finite value that is not a no-data value."""
    ok = np.ones(bands[0].shape, dtype=bool)
    for b in bands:
        if b.dtype.kind == "f":
            ok &= np.isfinite(b)
        for v in no_data:
            ok &= b != v
    return ok


def _bin_of(v: np.ndarray, rng: tuple, n: int) -> np.ndarray:
    lo, hi = rng
    scale = (n - 1) / (hi - lo) if hi > lo else 0.0
    k = np.floor((v - lo) * scale + 0.5)
    return np.clip(k, 0, n - 1).astype(np.int64)


def _histogram(values: Sequence[np.ndarray], rng: tuple, n: int, skip: Sequence[Sequence[float]]) -> list:
    out = []
    for c, v in enumerate(values):
        if skip[c]:
            v = v[~np.isin(v, skip[c])]
        out.append(np.bincount(_bin_of(v, rng, n), minlength=n).astype(np.float64))
    return out


def _quantile(bins: np.ndarray, q: float, rng: tuple) -> Optional[float]:
    total = bins.sum()
    if total <= 0:
        return None
    k = int(np.searchsorted(np.cumsum(bins), total * q, side="right"))
    if k >= len(bins):
        return float(rng[1])
    return rng[0] + (rng[1] - rng[0]) * k / (len(bins) - 1)


def sample_raster_histogram(bands: Sequence[np.ndarray], valid: np.ndarray, bins: int = BINS) -> Optional[RasterHistogram]:
    """
    Value counts of the picture bands made to hold up on real imagery, as the
    library's sampleRasterHistogram: an extreme value standing far apart from
    the rest (an untagged fill value) is left out, and the bins zoom in on
    the 1–99 % range while a few outliers spread them thin.
    """
    n = max(2, int(bins))
    values = [np.asarray(b, dtype=np.float64)[valid] for b in bands]
    if not values or values[0].size == 0:
        return None
    lo = [float(v.min()) for v in values]
    hi = [float(v.max()) for v in values]
    rng = (min(lo), max(hi))
    none: list = [[] for _ in values]
    coarse = _histogram(values, rng, n, none)
    skip: list = []
    for c, h in enumerate(coarse):
        rest = h.copy()
        rest[_bin_of(np.array([lo[c]]), rng, n)[0]] -= np.count_nonzero(values[c] == lo[c])
        if hi[c] != lo[c]:
            rest[_bin_of(np.array([hi[c]]), rng, n)[0]] -= np.count_nonzero(values[c] == hi[c])
        p1 = _quantile(rest, 0.01, rng)
        p99 = _quantile(rest, 0.99, rng)
        out: list = []
        if p1 is not None and p99 is not None:
            spread = p99 - p1
            if p1 - lo[c] > spread and lo[c] < p1:
                out.append(lo[c])
            if hi[c] - p99 > spread and hi[c] > p99:
                out.append(hi[c])
        skip.append(out)
    first = _histogram(values, rng, n, skip) if any(skip) else coarse
    current = rng
    for _ in range(16):
        q1, q99 = np.inf, -np.inf
        for h in first:
            a = _quantile(h, 0.01, current)
            b = _quantile(h, 0.99, current)
            if a is not None and b is not None:
                q1, q99 = min(q1, a), max(q99, b)
        if not q1 <= q99:
            break
        margin = max(q99 - q1, (current[1] - current[0]) / (n - 1))
        narrow = (max(current[0], q1 - margin), min(current[1], q99 + margin))
        if (narrow[1] - narrow[0]) * 16 >= current[1] - current[0]:
            break
        current = narrow
        first = _histogram(values, current, n, skip)
    return RasterHistogram(current, first, int(valid.sum()))


def compute_raster_stretch(stats: RasterHistogram, options: Optional[dict] = None) -> dict:
    """Raw black and white values per picture band for an automatic stretch (one pair for all with `linked`)."""
    o = normalize_auto_stretch(options)
    lo, hi = stats.range

    def distribution(bins: Sequence[np.ndarray]) -> tuple:
        vals = []
        for b in bins:
            v = lo + np.arange(len(b)) * ((hi - lo) / (len(b) - 1))
            v[-1] = hi
            vals.append(v)
        values = np.concatenate(vals)
        counts = np.concatenate(list(bins))
        order = np.argsort(values, kind="stable")
        return values[order], counts[order]

    fallback = (lo, hi) if hi > lo else (lo, lo + 1)
    if o["linked"] or len(stats.bins) == 1:
        r = range_of(*distribution(stats.bins), o) or fallback
        return {"black": [r[0]] * len(stats.bins), "white": [r[1]] * len(stats.bins)}
    ranges = [range_of(*distribution([b]), o) or fallback for b in stats.bins]
    return {"black": [r[0] for r in ranges], "white": [r[1] for r in ranges]}


def _codes(band: np.ndarray, black: float, white: float) -> np.ndarray:
    """Raw values -> 0..255 (rounded half to even, clipped; NaN 0). 8- and 16-bit integers go through a table."""
    span = white - black
    scale = 255.0 / span if span > 0 else 0.0

    def convert(v: np.ndarray) -> np.ndarray:
        with np.errstate(invalid="ignore", over="ignore"):
            x = np.rint(np.clip((np.asarray(v, dtype=np.float64) - black) * scale, 0, 255))
        return np.nan_to_num(x).astype(np.uint8)

    if band.dtype in (np.uint8, np.uint16):
        return convert(np.arange(np.iinfo(band.dtype).max + 1))[band]
    if band.dtype in (np.int8, np.int16):
        info = np.iinfo(band.dtype)
        return convert(np.arange(info.min, info.max + 1))[band.astype(np.int32) - info.min]
    return convert(band)


def stretch_to_rgba(bands: Sequence[np.ndarray], valid: np.ndarray, black: Sequence[float], white: Sequence[float]) -> np.ndarray:
    """
    H×W×4 uint8 RGBA from raw bands (1 gives gray): black..white maps linearly
    onto 0..255 (rounded half to even, clipped), invalid pixels are transparent.
    """
    h, w = bands[0].shape
    out = np.zeros((h, w, 4), dtype=np.uint8)
    all_valid = bool(valid.all())
    for c in range(3 if len(bands) == 3 else 1):
        codes = _codes(bands[c], black[c], white[c])
        out[..., c] = codes if all_valid else np.where(valid, codes, 0)
    if len(bands) == 1:
        out[..., 1] = out[..., 0]
        out[..., 2] = out[..., 0]
    out[..., 3] = np.where(valid, 255, 0)
    return out
