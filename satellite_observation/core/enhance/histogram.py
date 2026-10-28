"""
The histogram of the picture as shown (the viewer's histogram-panel.ts):
256 counts per channel of the opaque pixels, each channel's smallest, mean,
median and largest code, and bar heights on one scale.
"""
from __future__ import annotations

from typing import Optional, Sequence

import numpy as np


def channel_bins(rgba: np.ndarray, gray: bool) -> list:
    """256 counts per channel (one for gray) of the pixels that are not transparent."""
    seen = rgba[..., 3] != 0
    return [np.bincount(rgba[..., c][seen], minlength=256) for c in range(1 if gray else 3)]


def channel_stats(bins: Sequence[int]) -> Optional[dict]:
    """Smallest, largest, mean and median code and the count of one channel; None when it counted nothing."""
    bins = np.asarray(bins)
    count = int(bins.sum())
    if count == 0:
        return None
    used = np.flatnonzero(bins)
    v = np.arange(len(bins))
    median = int(np.searchsorted(np.cumsum(bins), count / 2))
    return {"min": int(used[0]), "max": int(used[-1]), "mean": float((bins * v).sum() / count), "median": median, "count": count}


def bar_heights(channels: Sequence[Sequence[int]], log: bool) -> list:
    """
    Bar heights 0..1 for every channel's bins, on one scale. Linear: the
    tallest bin away from 0 and 255 is full height (the ends, where clipped
    pixels pile up, may go over and are cut). Logarithmic: log(1 + count).
    """
    arrays = [np.asarray(b, dtype=np.float64) for b in channels]
    peak = max((float(a.max() if log else a[1:-1].max(initial=0)) for a in arrays), default=0.0)
    if not log and peak == 0:
        peak = max((float(a.max(initial=0)) for a in arrays), default=0.0)
    scale = np.log1p(peak) if log else peak
    if scale <= 0:
        return [np.zeros(len(a)) for a in arrays]
    return [np.minimum(1.0, (np.log1p(a) if log else a) / scale) for a in arrays]
