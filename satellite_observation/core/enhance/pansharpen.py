"""
Pan-sharpening, as browser-image-enhancement's pansharpen.ts: the detail of
a sharp panchromatic band put into a coarser multispectral image already
resampled onto the panchromatic grid. Values keep their range and sample
type (a 16-bit image stays 16-bit).

Each method takes an intensity I, a weighted mean of the multispectral
bands, and the panchromatic band matched to it (same mean and spread), P:

- "gram-schmidt" (default): band k gets g_k · (P − I), g_k = cov(k, I) / var(I)
  (adaptive Gram-Schmidt). Keeps colours best.
- "ihs": every band gets P − I (fast IHS). The sharpest; colours can shift.
- "brovey": every band is scaled by P / I. Strong contrast, keeps hue.

With weights "auto" the intensity weights are fitted to the panchromatic
band by least squares. The weights and gains (the model) come from
statistics of the whole image or of windows spread over it; passing the
same model to every block of a large image makes the blocks join without
seams.
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Optional, Sequence, Union

import numpy as np

METHODS = ("gram-schmidt", "ihs", "brovey")
#: Pixels whose statistics are taken: about this many, evenly spread.
SAMPLES = 1 << 20

_INTEGER = {np.dtype(t): (np.iinfo(t).min, np.iinfo(t).max) for t in (np.uint8, np.int8, np.uint16, np.int16, np.uint32, np.int32)}


@dataclass
class PanSharpenModel:
    """What was fitted: everything needed to sharpen another piece of the same image the same way."""
    #: Intensity weights of the sharpened bands (summing to 1).
    weights: list
    #: Detail gains of the sharpened bands.
    gains: list
    #: The panchromatic band matched to the intensity: pan_gain · pan + pan_offset.
    pan_gain: float
    pan_offset: float


def valid_pixels(pan: np.ndarray, ms: np.ndarray, bands: Sequence[int], pan_no_data=None, ms_no_data=None,
                 alpha: Optional[int] = None) -> np.ndarray:
    """Pixels with a value in pan and in every sharpened band (and not transparent)."""
    ok = np.isfinite(pan) if pan.dtype.kind == "f" else np.ones(pan.shape, dtype=bool)
    if pan_no_data is not None:
        ok &= pan != pan_no_data
    if alpha is not None:
        ok &= ms[alpha] != 0
    for b in bands:
        v = ms[b]
        if v.dtype.kind == "f":
            ok &= np.isfinite(v)
        if ms_no_data is not None:
            ok &= v != ms_no_data
    return ok


def _solve(a: list, b: list) -> Optional[list]:
    """a x = b by Gaussian elimination with partial pivoting (the library's); None when singular."""
    n = len(b)
    m = [list(r) + [b[i]] for i, r in enumerate(a)]
    scale = max([1e-300] + [abs(r[i]) for i, r in enumerate(a)])
    for c in range(n):
        p = c
        for r in range(c + 1, n):
            if abs(m[r][c]) > abs(m[p][c]):
                p = r
        if abs(m[p][c]) <= scale * 1e-12:
            return None
        m[c], m[p] = m[p], m[c]
        for r in range(n):
            if r == c:
                continue
            f = m[r][c] / m[c][c]
            for j in range(c, n + 1):
                m[r][j] -= f * m[c][j]
    return [r[n] / r[i] for i, r in enumerate(m)]


def _intensity_weights(choice: Union[str, Sequence[float]], n: int, cov) -> list:
    equal = [1 / n] * n
    if choice == "equal":
        return equal
    if choice != "auto":
        w = [float(x) for x in choice]
        if len(w) != n or any(not math.isfinite(x) or x < 0 for x in w):
            raise ValueError(f"weights must give {n} numbers of 0 or more.")
        total = sum(w)
        if not total > 0:
            raise ValueError("weights must not all be 0.")
        return [x / total for x in w]
    # Least squares of pan on the bands (centred), dropping bands that come out negative.
    active = list(range(n))
    for _ in range(n):
        w = _solve([[cov(a, b) for b in active] for a in active], [cov(a, n) for a in active])
        if w is None:
            return equal
        if all(v >= 0 for v in w):
            total = sum(w)
            if not total > 0:
                return equal
            out = [0.0] * n
            for i, j in enumerate(active):
                out[j] = w[i] / total
            return out
        active = [j for i, j in enumerate(active) if w[i] > 0]
        if not active:
            return equal
    return equal


def fit_model(pan: np.ndarray, ms: np.ndarray, bands: Sequence[int], valid: np.ndarray, method: str = "gram-schmidt",
              weights: Union[str, Sequence[float]] = "auto") -> PanSharpenModel:
    """Fits the weights and gains on about SAMPLES valid pixels evenly spread (in reading order)."""
    pixels = valid.size
    n = len(bands)
    step = max(1, pixels // SAMPLES)
    idx = np.arange(0, pixels, step)
    idx = idx[valid.ravel()[idx]]
    rows = np.empty((idx.size, n + 1), dtype=np.float64)
    for j, b in enumerate(bands):
        rows[:, j] = ms[b].ravel()[idx]
    rows[:, n] = pan.ravel()[idx]
    count = idx.size
    mean = rows.sum(axis=0) / count if count else np.zeros(n + 1)
    cross = rows.T @ rows

    def cov(a: int, b: int) -> float:
        return float(cross[a, b] / count - mean[a] * mean[b]) if count else 0.0

    w = _intensity_weights(weights, n, cov)
    mean_i = sum(w[j] * mean[j] for j in range(n))
    var_i = 0.0
    for a in range(n):
        for b in range(n):
            var_i += w[a] * w[b] * cov(a, b)
    var_p = cov(n, n)
    gain_p = math.sqrt(var_i / var_p) if var_p > 0 and var_i > 0 else 1.0
    offset_p = float(mean_i - gain_p * mean[n])
    gains = []
    for j in range(n):
        if method == "ihs":
            gains.append(1.0)
        elif method == "brovey":
            gains.append(0.0)
        else:
            c = sum(w[b] * cov(j, b) for b in range(n))
            gains.append(c / var_i if var_i > 0 else 1.0)
    return PanSharpenModel(w, gains, gain_p, offset_p)


def pan_sharpen(pan: np.ndarray, ms: np.ndarray, method: str = "gram-schmidt", weights: Union[str, Sequence[float]] = "auto",
                bands: Optional[Sequence[int]] = None, strength: float = 1.0, model: Optional[PanSharpenModel] = None,
                pan_no_data=None, ms_no_data=None, alpha: bool = False) -> tuple:
    """
    Pan-sharpens `ms` (B×H×W, on the panchromatic grid) with `pan` (H×W).
    Returns (the B×H×W result in ms's type, its no-data value, the model).
    Pixels that are no data (or NaN) in either stay no data; bands not
    sharpened (and alpha) are copied.
    """
    if method not in METHODS:
        raise ValueError(f"Unknown method {method}.")
    if pan.shape != ms.shape[1:]:
        raise ValueError(f"pan {pan.shape} and ms {ms.shape[1:]} must be the same size: resample ms onto the pan grid first.")
    if not (math.isfinite(strength) and strength >= 0):
        raise ValueError(f"strength must be 0 or more, got {strength}.")
    count = ms.shape[0]
    alpha_band = count - 1 if alpha else None
    bands = list(bands) if bands is not None else [b for b in range(count) if b != alpha_band]
    if not bands or len(set(bands)) != len(bands) or any(b < 0 or b >= count or b == alpha_band for b in bands):
        raise ValueError(f"bands must name distinct value bands of ms, got {bands}.")
    n = len(bands)
    valid = valid_pixels(pan, ms, bands, pan_no_data, ms_no_data, alpha_band)
    if model is not None and (len(model.weights) != n or len(model.gains) != n):
        raise ValueError(f"model must have {n} weights and gains, one per sharpened band.")
    if model is None:
        model = fit_model(pan, ms, bands, valid, method, weights)

    rng = _INTEGER.get(ms.dtype)
    no_data = ms_no_data if ms_no_data is not None else (0 if rng else float("nan"))
    out = ms.copy()
    m = [ms[b].astype(np.float64) for b in bands]
    intensity = np.zeros(pan.shape, dtype=np.float64)
    for j in range(n):
        intensity += model.weights[j] * m[j]
    detail = strength * (model.pan_gain * pan.astype(np.float64) + model.pan_offset - intensity)
    with np.errstate(divide="ignore", invalid="ignore"):
        if method == "brovey":
            # Below a tiny intensity the ratio blows up: add the detail instead.
            ratio = np.where(np.abs(intensity) > 1e-9, (intensity + detail) / intensity, np.nan)
            sharp = [np.where(np.isnan(ratio), mj + detail, mj * ratio) for mj in m]
        else:
            sharp = [mj + model.gains[j] * detail for j, mj in enumerate(m)]
    for j, b in enumerate(bands):
        v = sharp[j]
        if rng:
            v = np.floor(np.clip(v, rng[0], rng[1]) + 0.5)
            # A valid pixel must not turn into no data.
            v = np.where(v == no_data, no_data + 1 if no_data < rng[1] else no_data - 1, v)
        out[b] = np.where(valid, v, no_data).astype(ms.dtype)
    return out, no_data, model
