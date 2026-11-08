"""
Change between two dates of one place (the viewer's change.ts): how a
pixel's value after compares with its value before (a difference, a ratio, a
ratio in decibels for SAR, a normalized difference), which pixels changed
more than a threshold, and how far the values spread for showing them
symmetrically in blue (fell), white (no change) and red (rose).
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Optional

import numpy as np


@dataclass(frozen=True)
class ChangeMethod:
    """How before (A) and after (B) are compared; `center` is the value of "no change", `unit` the thresholds' for messages."""
    id: str
    label: str
    expression: str
    center: float
    unit: str


CHANGE_METHODS = [
    ChangeMethod("difference", "差（後 − 前）", "B - A", 0, ""),
    ChangeMethod("logRatio", "対数比 dB（10·log10(後 ÷ 前)、SAR 向け）", "10 * log10(B / A)", 0, " dB"),
    ChangeMethod("ratio", "比（後 ÷ 前）", "B / A", 1, ""),
    ChangeMethod("normalized", "正規化差（(後 − 前) ÷ (後 + 前)）", "(B - A) / (B + A)", 0, ""),
]
#: Which way a change goes.
DIRECTIONS = ["both", "increase", "decrease"]


def method_of(method_id: str) -> ChangeMethod:
    return next((m for m in CHANGE_METHODS if m.id == method_id), CHANGE_METHODS[0])


def compare(a: np.ndarray, b: np.ndarray, method: ChangeMethod) -> np.ndarray:
    """The method's values for before `a` and after `b` (NaN where either is NaN or the value is not a number), float32."""
    a = np.asarray(a, dtype=np.float64)
    b = np.asarray(b, dtype=np.float64)
    with np.errstate(divide="ignore", invalid="ignore"):
        if method.id == "difference":
            out = b - a
        elif method.id == "logRatio":
            out = 10 * np.log10(b / a)
        elif method.id == "ratio":
            out = b / a
        else:
            out = (b - a) / (b + a)
    out[~np.isfinite(out)] = np.nan
    return out.astype(np.float32)


@dataclass(frozen=True)
class Thresholds:
    """Values at or below `low` decreased, at or above `high` increased."""
    low: float
    high: float


def mean_and_deviation(values) -> tuple[float, float, int]:
    """Mean and standard deviation of the values that are numbers, and how many."""
    v = np.asarray(values, dtype=np.float64).ravel()
    v = v[~np.isnan(v)]
    if not v.size:
        return math.nan, math.nan, 0
    mean = float(v.mean())
    return mean, math.sqrt(max(0.0, float((v * v).mean()) - mean * mean)), int(v.size)


def thresholds_of(values, center: float, typed: Optional[float], deviations: float = 2) -> Thresholds:
    """
    `center` ± `typed` when a value is typed, else the mean ± `deviations`
    standard deviations (most pixels did not change, so the mean also takes up
    a general difference of the two dates, such as brightness).
    """
    if typed is not None and typed > 0:
        return Thresholds(center - typed, center + typed)
    mean, deviation, _ = mean_and_deviation(values)
    if not math.isfinite(mean):
        return Thresholds(-math.inf, math.inf)
    # A flat result changed nowhere.
    spread = max(deviation * deviations, 1e-9)
    return Thresholds(mean - spread, mean + spread)


def change_mask(values, thresholds: Thresholds, direction: str) -> np.ndarray:
    """The pixels that changed one way ('increase' or 'decrease'): 1 where they did."""
    v = np.asarray(values, dtype=np.float64)
    with np.errstate(invalid="ignore"):
        hit = v >= thresholds.high if direction == "increase" else v <= thresholds.low
    return (hit & ~np.isnan(v)).astype(np.uint8)


def spread_of(values, center: float, p: float = 0.98) -> float:
    """
    The value `p` of the way (0–1) up the distances of the values from
    `center`, for showing a change image symmetrically: `center` ± that in the
    middle of a diverging colour scale.
    """
    v = np.asarray(values, dtype=np.float64).ravel()
    distances = np.abs(v[~np.isnan(v)] - center)
    n = distances.size
    if not n:
        return 1.0
    d = float(np.partition(distances, min(n - 1, math.floor(p * n)))[min(n - 1, math.floor(p * n))])
    return d if d > 0 else (float(distances.max()) or 1.0)


def describe_thresholds(t: Thresholds, direction: str, method: ChangeMethod) -> str:
    """The thresholds for the summary: `-12.3 以下・15.6 以上`."""
    def n(v: float) -> str:
        return f"{float(f'{v:.3g}'):g}{method.unit}"
    if direction == "increase":
        return f"{n(t.high)} 以上"
    if direction == "decrease":
        return f"{n(t.low)} 以下"
    return f"{n(t.low)} 以下・{n(t.high)} 以上"
