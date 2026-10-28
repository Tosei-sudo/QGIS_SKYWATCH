"""
The spectral graph (スペクトル, the viewer's spectral.ts): the value of every
band of an image at the points clicked, or the mean over a box dragged, as
lines over the bands, so vegetation, water and built-up ground can be told
apart before choosing bands. The values are the file's own (DN,
reflectance…), not the corrected display.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Callable, Optional, Sequence

import numpy as np

#: Most lines kept; the oldest goes when another is added.
MAX_SERIES = 8
#: A box larger than this many pixels is read at a lower resolution.
MAX_READ = 1_000_000
COLORS = ["#3e7bfa", "#e5484d", "#30a46c", "#f5a524", "#8e4ec6", "#12a5b0", "#d6409f", "#7c5d3a"]


@dataclass
class BandStat:
    """The statistics of one band over the pixels read (NaN and a count of 0 when none had a value)."""
    mean: float
    min: float
    max: float
    std: float
    count: int


@dataclass
class Spectrum:
    """One line of the graph: its number on the map, 点 or 範囲 with where, its colour, one BandStat per band, the pixels read."""
    number: int
    label: str
    color: str
    bands: list = field(default_factory=list)
    pixels: int = 1
    #: Where it was taken (lon, lat) or the box (lon0, lat0, lon1, lat1), for the map.
    where: tuple = ()


def band_stats(bands: Sequence, is_no_data: Optional[Callable[[np.ndarray], np.ndarray]] = None) -> list[BandStat]:
    """
    Mean, range and spread of each band, leaving out no data (`is_no_data`
    gives a mask of an array's no-data values) and NaN.
    """
    out = []
    for values in bands:
        a = np.asarray(values, dtype=np.float64).ravel()
        keep = ~np.isnan(a)
        if is_no_data is not None:
            keep &= ~np.asarray(is_no_data(a), dtype=bool)
        a = a[keep]
        if not a.size:
            out.append(BandStat(math.nan, math.nan, math.nan, math.nan, 0))
            continue
        mean = float(a.mean())
        std = math.sqrt(max(0.0, float((a * a).mean()) - mean * mean))
        out.append(BandStat(mean, float(a.min()), float(a.max()), std, int(a.size)))
    return out


def band_labels(count: int, names: Sequence[Optional[str]] = ()) -> list[str]:
    """The band labels of the graph: `B1`, or `B4 NIR` with the band's name."""
    return [f"B{i + 1} {names[i]}" if i < len(names) and names[i] else f"B{i + 1}" for i in range(count)]


def _quote(s: str) -> str:
    return '"' + s.replace('"', '""') + '"' if any(c in s for c in '",\n') else s


def _num(v: float) -> str:
    if v is None or math.isnan(v):
        return ""
    text = f"{v:.7g}"
    if "e" in text:
        mantissa, exponent = text.split("e")
        text = f"{mantissa}e{'-' if exponent.startswith('-') else '+'}{exponent.lstrip('+-').lstrip('0') or '0'}"
    return text


def spectral_csv(series: Sequence[Spectrum], labels: Sequence[str]) -> str:
    """The lines as CSV: one row per band, a column of means per line (and the range and spread of a box)."""
    head = ["band"]
    for s in series:
        name = f"{s.number} {s.label}"
        head.append(name)
        if s.pixels > 1:
            head += [f"{name} min", f"{name} max", f"{name} std"]
    rows = [",".join(_quote(h) for h in head)]
    for b, label in enumerate(labels):
        row = [_quote(label)]
        for s in series:
            stat = s.bands[b] if b < len(s.bands) else None
            row.append(_num(stat.mean if stat else math.nan))
            if s.pixels > 1:
                row += [_num(stat.min if stat else math.nan), _num(stat.max if stat else math.nan), _num(stat.std if stat else math.nan)]
        rows.append(",".join(row))
    return "\n".join(rows) + "\n"


def nice_step(span: float, count: int) -> float:
    """A round step giving about `count` ticks over `span`."""
    raw = span / max(1, count)
    p = 10 ** math.floor(math.log10(raw))
    n = raw / p
    return (1 if n < 1.5 else 2 if n < 3.5 else 5 if n < 7.5 else 10) * p


def format_value(v: float) -> str:
    """A value on the graph: `0.1234`, `512.5`, `12345`, `1.20e-3`."""
    if v is None or math.isnan(v):
        return "—"
    a = abs(v)
    if a != 0 and (a < 0.01 or a >= 1e6):
        mantissa, exponent = f"{v:.2e}".split("e")
        return f"{mantissa}e{'-' if exponent.startswith('-') else '+'}{exponent.lstrip('+-').lstrip('0') or '0'}"
    text = f"{v:.{4 if a < 10 else 2 if a < 1000 else 0}f}"
    return text.rstrip("0").rstrip(".") if "." in text else text


def axis_range(series: Sequence[Spectrum]) -> Optional[tuple[float, float, float]]:
    """The value axis (min, max, step) for the lines: a box's range counts, rounded to round steps; None when nothing to draw."""
    values = [v for s in series for b in s.bands for v in ((b.min, b.max) if s.pixels > 1 else (b.mean,)) if math.isfinite(v)]
    if not values:
        return None
    lo, hi = min(values), max(values)
    if hi - lo < 1e-9:
        pad = abs(hi) * 0.1 or 1
        lo, hi = lo - pad, hi + pad
    step = nice_step(hi - lo, 4)
    return math.floor(lo / step) * step, math.ceil(hi / step) * step, step


def pixel_window(cols: Sequence[float], rows: Sequence[float], width: int, height: int) -> Optional[tuple[int, int, int, int]]:
    """The image's pixels a box covers (c0, r0, c1, r1), from where its corners fall (fractional pixels), clipped to the image."""
    c0 = max(0, math.floor(min(cols)))
    r0 = max(0, math.floor(min(rows)))
    c1 = min(width, math.ceil(max(cols)))
    r1 = min(height, math.ceil(max(rows)))
    return (c0, r0, c1, r1) if c1 > c0 and r1 > r0 else None


def read_size(w: int, h: int) -> tuple[int, int, bool]:
    """How many pixels a box of w × h is read at (fewer past MAX_READ), and whether that thins it."""
    scale = min(1.0, math.sqrt(MAX_READ / (w * h)))
    return max(1, round(w * scale)), max(1, round(h * scale)), scale < 1


def box_label(w: int, h: int, thinned: bool) -> str:
    return f"範囲 {w:,} × {h:,} 画素{'（間引き）' if thinned else ''}"
