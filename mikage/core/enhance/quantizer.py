"""
Exact `quantize(F(v))` for a non-decreasing F, as browser-image-enhancement's
core/quantizer.ts: the 255 values s_k where F crosses each code boundary are
found once by bisection on the doubles, and each pixel's code is the count of
s_k at or below it — found from a bucket table and a short correction, not by
evaluating F (often several powers) for every pixel.
"""
from __future__ import annotations

from typing import Callable, Optional

import numpy as np

from .srgb import THRESHOLDS

BUCKETS = 4096
#: Search range (as the library's): pixel values never get near it.
LIMIT = 2.0 ** 50


def _keys(v: np.ndarray) -> np.ndarray:
    """Doubles -> int64 keys in the same order."""
    bits = np.asarray(v, dtype=np.float64).view(np.int64)
    return np.where(bits < 0, np.int64(-(2 ** 63)) - bits - 1, bits)


def _values(k: np.ndarray) -> np.ndarray:
    bits = np.where(k < 0, np.int64(-(2 ** 63)) - k - 1, k)
    return bits.astype(np.int64).view(np.float64)


def inverse(F: Callable[[np.ndarray], np.ndarray], targets: np.ndarray) -> np.ndarray:
    """For each target, the smallest double v in [-LIMIT, LIMIT] with F(v) >= target (-inf if all, +inf if none)."""
    targets = np.asarray(targets, dtype=np.float64)
    n = targets.size
    lo_all = F(np.full(n, -LIMIT)) >= targets
    hi_none = ~(F(np.full(n, LIMIT)) >= targets)
    lo = np.full(n, _keys(np.array(-LIMIT)), dtype=np.int64)
    hi = np.full(n, _keys(np.array(LIMIT)), dtype=np.int64)
    while True:
        open_ = hi - 1 > lo
        if not open_.any():
            break
        # floor((lo + hi) / 2) without overflowing int64.
        mid = (lo >> 1) + (hi >> 1) + (lo & hi & 1)
        ok = F(_values(mid)) >= targets
        hi = np.where(open_ & ok, mid, hi)
        lo = np.where(open_ & ~ok, mid, lo)
    out = _values(hi)
    out = np.where(lo_all, -np.inf, out)
    return np.where(hi_none, np.inf, out)


class Quantizer:
    """`quantize(F(v))` for arrays. Omit F for plain linear -> 8-bit rounding."""

    def __init__(self, F: Optional[Callable[[np.ndarray], np.ndarray]] = None, thresholds: Optional[np.ndarray] = None):
        if thresholds is None:
            thresholds = THRESHOLDS if F is None else inverse(F, THRESHOLDS)
        t = np.append(np.asarray(thresholds, dtype=np.float64), np.inf)
        self.thresholds = t
        finite = np.nonzero(np.isfinite(t[:255]))[0]
        self.floor = int(np.count_nonzero(t[:255] == -np.inf))
        if finite.size:
            first, last = int(finite[0]), int(finite[-1])
            self.lo, self.hi, self.ceil = t[first], t[last], last + 1
        else:
            self.lo = self.hi = np.inf
            self.ceil = self.floor
        span = self.hi - self.lo
        self.scale = BUCKETS / span if span > 0 and np.isfinite(span) else 0.0
        starts = self.lo + np.arange(BUCKETS) / self.scale if self.scale > 0 else np.full(BUCKETS, self.lo)
        # base[b]: never above the code of any value in bucket b.
        self.base = np.maximum(np.searchsorted(t[:255], starts, side="right") - 1, self.floor).astype(np.int64)

    def quantize(self, v) -> np.ndarray:
        v = np.asarray(v, dtype=np.float64)
        with np.errstate(invalid="ignore"):
            inside = (v >= self.lo) & (v < self.hi)
            b = np.clip(((np.where(inside, v, self.lo) - self.lo) * self.scale), 0, BUCKETS - 1).astype(np.int64)
        code = self.base[b]
        t = self.thresholds
        while True:
            step = inside & (v >= t[code])
            if not step.any():
                break
            code = code + step
        code = np.where(inside, code, np.where(v >= self.hi, self.ceil, self.floor))
        return code.astype(np.uint8)


#: Plain rounding of linear values.
PLAIN = Quantizer()
