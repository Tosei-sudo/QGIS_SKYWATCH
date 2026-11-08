"""
sRGB <-> linear light, as browser-image-enhancement's color/srgb.ts.

`quantize` gives exactly round(encode(v) * 255) from the 255 decision
thresholds between adjacent codes, as the library does.
"""
from __future__ import annotations

import numpy as np

#: Rec. 709 / sRGB luminance weights for linear RGB.
LUMA_R = 0.2126
LUMA_G = 0.7152
LUMA_B = 0.0722


def srgb_to_linear(v):
    """Encoded value in [0, 1] -> linear light (scalar or array)."""
    v = np.asarray(v, dtype=np.float64)
    out = np.where(v <= 0.04045, v / 12.92, np.power(np.maximum((v + 0.055) / 1.055, 0.0), 2.4))
    return out if out.ndim else float(out)


def linear_to_srgb(v):
    """Linear light -> encoded value; negative and NaN give 0 (scalar or array)."""
    v = np.asarray(v, dtype=np.float64)
    with np.errstate(invalid="ignore"):
        pos = v > 0
        safe = np.where(pos, v, 0.0)
        out = np.where(safe <= 0.0031308, safe * 12.92, 1.055 * np.power(safe, 1 / 2.4) - 0.055)
        out = np.where(pos, out, 0.0)
    return out if out.ndim else float(out)


#: Linear value of every 8-bit code.
SRGB_TO_LINEAR = srgb_to_linear(np.arange(256) / 255.0)
#: THRESHOLDS[k]: the linear value between codes k and k + 1.
THRESHOLDS = srgb_to_linear((np.arange(255) + 0.5) / 255.0)


def quantize(v) -> np.ndarray:
    """Linear light -> 8-bit code (half up, clamped; NaN and negative give 0)."""
    v = np.asarray(v, dtype=np.float64)
    v = np.where(np.isnan(v), -np.inf, v)
    return np.searchsorted(THRESHOLDS, v, side="right").astype(np.uint8)
