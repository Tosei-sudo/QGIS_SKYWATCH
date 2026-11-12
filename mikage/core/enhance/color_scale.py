"""
Colour scales (pseudo-colour), as browser-image-enhancement's color-scale.ts:
a gray value, 0 (black) to 255 (white), picks a colour along a ramp. A
one-band image (a SAR scene, a panchromatic image) in a red scale goes from
black to red; two dates in red and cyan, the later screened over the earlier,
show where they differ.

A scale is a preset's name or a list of stops `[position 0..1, [r, g, b(, a)]]`;
colours in between are interpolated linearly on the 8-bit codes.
`color_scale_palette` turns a scale into a 256-entry RGBA table.
"""
from __future__ import annotations

import math
from typing import Sequence, Union

import numpy as np


def _js_round(x):
    """JavaScript's Math.round: halves go up."""
    return np.floor(np.asarray(x, dtype=np.float64) + 0.5)


def _even(*colors: str) -> list:
    """Evenly spaced stops from hex colours."""
    n = len(colors)
    return [(i / (n - 1), [int(c[k:k + 2], 16) for k in (1, 3, 5)]) for i, c in enumerate(colors)]


def _from(r: int, g: int, b: int) -> list:
    """Black to (r, g, b)."""
    return [(0, [0, 0, 0]), (1, [r, g, b])]


#: The preset scales: black to a colour, perceptual ramps (inferno, viridis), turbo, and a diverging blue-white-red.
COLOR_SCALES = {
    "gray": _from(255, 255, 255),
    "red": _from(255, 0, 0),
    "green": _from(0, 255, 0),
    "blue": _from(0, 0, 255),
    "cyan": _from(0, 255, 255),
    "magenta": _from(255, 0, 255),
    "yellow": _from(255, 255, 0),
    "orange": _from(255, 128, 0),
    "inferno": _even("#000004", "#320a5e", "#781c6d", "#bc3754", "#ed6925", "#fbb61a", "#fcffa4"),
    "viridis": _even("#440154", "#46327e", "#365c8d", "#277f8e", "#1fa187", "#4ac16d", "#a0da39", "#fde725"),
    "turbo": _even("#30123b", "#4662d7", "#36aaf9", "#1ae4b6", "#72fe5e", "#c8ef34", "#faba39", "#f66b19", "#ca2a04", "#7a0403"),
    "blueWhiteRed": _even("#2166ac", "#67a9cf", "#d1e5f0", "#f7f7f7", "#fddbc7", "#ef8a62", "#b2182b"),
}

ColorScale = Union[str, Sequence]


def color_scale_stops(scale: ColorScale) -> list:
    """The stops of a scale, sorted, positions clamped to 0..1, colours RGBA. Raises ValueError for an unknown name or no stops."""
    stops = COLOR_SCALES.get(scale) if isinstance(scale, str) else scale
    if stops is None:
        raise ValueError(f"Unknown color scale: {scale}")
    if not isinstance(stops, (list, tuple)) or len(stops) == 0:
        raise ValueError("A color scale needs at least one stop.")

    def channel(c, k: int, fallback: float) -> float:
        v = c[k] if k < len(c) else None
        if isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v):
            return min(255.0, max(0.0, float(v)))
        return fallback

    def position(p) -> float:
        try:
            v = float(p)
        except (TypeError, ValueError):
            v = 0.0
        return min(1.0, max(0.0, v if math.isfinite(v) else 0.0))

    out = [(position(p), [channel(c, 0, 0), channel(c, 1, 0), channel(c, 2, 0), channel(c, 3, 255)]) for p, c in stops]
    return sorted(out, key=lambda s: s[0])


def color_scale_palette(scale: ColorScale, reverse: bool = False, fade: bool = False) -> np.ndarray:
    """
    The scale as a 256×4 uint8 RGBA table: row v is the colour of gray v.
    `reverse` runs it from white to black; `fade` makes the dark end
    transparent, fading in along the scale.
    """
    stops = color_scale_stops(scale)
    out = np.zeros((256, 4), dtype=np.uint8)
    k = 0
    for v in range(256):
        t = (255 - v if reverse else v) / 255
        while k < len(stops) - 1 and stops[k + 1][0] < t:
            k += 1
        p0, c0 = stops[k]
        p1, c1 = stops[min(k + 1, len(stops) - 1)]
        f = 0.0 if t <= p0 else (min(1.0, (t - p0) / (p1 - p0)) if p1 > p0 else 1.0)
        for c in range(4):
            out[v, c] = int(_js_round(c0[c] + (c1[c] - c0[c]) * f))
        if fade:
            out[v, 3] = int(_js_round(int(out[v, 3]) * v / 255))
    return out


def apply_palette(rgba: np.ndarray, palette: np.ndarray) -> np.ndarray:
    """
    H×W×4 RGBA coloured with a palette: each pixel by its gray value (its
    luminance when it is coloured), its alpha multiplied by the palette's.
    """
    r, g, b = (rgba[..., c].astype(np.int32) for c in range(3))
    gray = (r == g) & (g == b)
    luma = _js_round(0.2126 * r + 0.7152 * g + 0.0722 * b).astype(np.int32)
    v = np.where(gray, r, luma)
    out = palette[v].copy()
    out[..., 3] = _js_round(rgba[..., 3].astype(np.float64) * palette[v, 3] / 255).astype(np.uint8)
    return out
