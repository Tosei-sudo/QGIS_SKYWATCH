"""
Colour classes (discrete pseudo-colour), as browser-image-enhancement's
color-classes.ts: pixel values sorted into classes, each class one flat
colour (QGIS's "discrete" and "exact" ramps). Elevations in 100 m steps, an
index cut at a few thresholds, or a land cover map whose codes each mean a
class.

Classes hold values of the image (raw values, not the 8-bit codes it is shown
with) as a dict, the viewer's ColorClasses:
`{"kind": "range" | "exact", "classes": [{"value", "color", "label"}], "min", "method"}`.
`class_palette` turns them into a 256-entry RGBA table for the codes a value
range is shown over; `color_classes` makes them from a value histogram
(`raster.RasterHistogram`).
"""
from __future__ import annotations

import math
from decimal import ROUND_HALF_UP, Decimal
from typing import Optional, Sequence

import numpy as np

from .color_scale import ColorScale, color_scale_palette
from .raster import RasterHistogram


def _sorted(classes: dict) -> list:
    return sorted(classes["classes"], key=lambda c: c["value"])


def _is_integer(v: float) -> bool:
    return math.isfinite(v) and float(v).is_integer()


def class_range(classes: dict) -> tuple:
    """
    The raw values codes 0 and 255 stand for to show `classes`: integer values
    that span at most 255 one code each, else the classes' whole range.
    """
    items = _sorted(classes)
    if not items:
        return (0.0, 255.0)
    first, last = items[0]["value"], items[-1]["value"]
    if classes["kind"] == "exact":
        if all(_is_integer(c["value"]) for c in items) and last - first <= 255:
            return (first, first + 255)
        return (first, last) if last > first else (first, first + 255)
    lo = min(classes.get("min", first) if classes.get("min") is not None else first, first)
    return (lo, last) if last > lo else (lo, lo + 1)


def _rgba(color: Sequence) -> list:
    c = list(color) + [None] * 4
    return [c[0] if c[0] is not None else 0, c[1] if c[1] is not None else 0, c[2] if c[2] is not None else 0,
            c[3] if c[3] is not None else 255]


def class_palette(classes: dict, value_range: Optional[Sequence[float]] = None) -> np.ndarray:
    """
    The classes as a 256×4 uint8 RGBA table for an image shown linearly from
    range[0] (code 0) to range[1] (code 255): row v is the colour of the class
    of the value code v stands for (transparent when none).
    """
    out = np.zeros((256, 4), dtype=np.uint8)
    items = _sorted(classes)
    if not items:
        return out
    lo, hi = value_range if value_range is not None else class_range(classes)
    step = (hi - lo) / 255
    tolerance = abs(step) * 1e-6
    for v in range(256):
        value = lo + step * v
        pick = None
        if classes["kind"] == "range":
            pick = next((c for c in items if value <= c["value"] + tolerance), items[-1])
        else:
            best = math.inf
            for c in items:
                d = abs(c["value"] - value)
                if d < best:
                    best, pick = d, c
            if best > abs(step) / 2 + tolerance:
                pick = None
        if pick is not None:
            out[v] = np.clip(np.floor(np.array(_rgba(pick["color"]), dtype=np.float64) + 0.5), 0, 255)
    return out


def _bin_value(hist: RasterHistogram, k: int, bins: int) -> float:
    return hist.range[0] + (hist.range[1] - hist.range[0]) * k / max(1, bins - 1)


def _bins(hist: RasterHistogram, band: int):
    return hist.bins[band] if band < len(hist.bins) else (hist.bins[0] if hist.bins else None)


def histogram_value_range(hist: RasterHistogram, band: int = 0) -> Optional[tuple]:
    """The smallest and largest value counted in `band`; None when it counted nothing."""
    bins = _bins(hist, band)
    if bins is None:
        return None
    used = np.flatnonzero(np.asarray(bins) > 0)
    if used.size == 0:
        return None
    return (_bin_value(hist, int(used[0]), len(bins)), _bin_value(hist, int(used[-1]), len(bins)))


def equal_interval_breaks(lo: float, hi: float, count: int) -> list:
    """`count` upper bounds of classes of equal width from lo to hi."""
    n = max(1, int(math.floor(count + 0.5)))
    return [hi if i == n - 1 else lo + (hi - lo) * (i + 1) / n for i in range(n)]


def quantile_breaks(hist: RasterHistogram, count: int, band: int = 0) -> list:
    """Upper bounds of classes of about as many pixels each (at most `count`; bounds that fall together merge)."""
    bins = _bins(hist, band)
    span = histogram_value_range(hist, band)
    if bins is None or span is None:
        return []
    n = max(1, int(math.floor(count + 0.5)))
    total = 0.0
    for b in bins:
        total += float(b)
    out: list = []
    cum = 0.0
    nxt = 1
    for k in range(len(bins)):
        if nxt >= n:
            break
        cum += float(bins[k])
        crossed = False
        while nxt < n and cum >= total * nxt / n:
            nxt += 1
            crossed = True
        if crossed:
            value = _bin_value(hist, k, len(bins))
            if value < span[1] and (not out or out[-1] != value):
                out.append(value)
    out.append(span[1])
    return out


def unique_values(hist: RasterHistogram, limit: int = 64, band: int = 0) -> Optional[list]:
    """
    The values counted in `band` and their pixel counts, smallest first
    ([{"value", "count"}]); None when there are more than `limit`. Values of
    a histogram of integers are rounded to integers.
    """
    bins = _bins(hist, band)
    if bins is None:
        return None
    lo, hi = hist.range
    width = (hi - lo) / max(1, len(bins) - 1)
    integers = _is_integer(lo) and _is_integer(hi) and width <= 1
    out: list = []
    for k in range(len(bins)):
        if bins[k] <= 0:
            continue
        raw = _bin_value(hist, k, len(bins))
        value = float(math.floor(raw + 0.5)) if integers else raw
        if out and out[-1]["value"] == value:
            out[-1]["count"] += float(bins[k])
        else:
            if len(out) >= limit:
                return None
            out.append({"value": value, "count": float(bins[k])})
    return out


def class_colors(scale: ColorScale, count: int, reverse: bool = False) -> list:
    """`count` colours taken evenly along `scale`, dark end to bright end (the other way with `reverse`)."""
    palette = color_scale_palette(scale, reverse=reverse)
    n = max(1, int(math.floor(count + 0.5)))
    return [[int(x) for x in palette[128 if n == 1 else int(math.floor(255 * i / (n - 1) + 0.5))]] for i in range(n)]


def _to_fixed(value: float, decimals: int) -> str:
    """JavaScript's Number.prototype.toFixed (halves away from zero, on the exact binary value)."""
    if value == 0:
        value = 0.0
    return str(Decimal(value).quantize(Decimal(1).scaleb(-decimals), rounding=ROUND_HALF_UP))


def format_class_value(value: float, width: float) -> str:
    """A value with as many decimals as a class `width` needs (two digits finer, at most 6; trailing zeros dropped)."""
    decimals = min(6, max(0, math.ceil(-math.log10(width)) + 2)) if width > 0 and math.isfinite(width) else 0
    text = _to_fixed(value, 0 if _is_integer(value) and width >= 1 else decimals)
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    return "0" if text == "-0" else text


def range_labels(lo: float, bounds: Sequence[float]) -> list:
    """Labels of range classes, "low – high" (the first from `lo`)."""
    width = (bounds[-1] - lo) / len(bounds) if bounds else 1
    return [f"{format_class_value(lo if i == 0 else bounds[i - 1], width)} – {format_class_value(b, width)}" for i, b in enumerate(bounds)]


def color_classes(hist: RasterHistogram, method: str = "equal", count: int = 5, scale: ColorScale = "turbo",
                  reverse: bool = False, max_values: int = 64, band: int = 0) -> Optional[dict]:
    """
    Classes for the values counted in `hist`, coloured along `scale`:
    "equal" intervals, "quantile" (classes of about equal area) or "unique"
    (a class per value). None when there is nothing to class, or more than
    `max_values` values for "unique".
    """
    span = histogram_value_range(hist, band)
    if span is None:
        return None
    if method == "unique":
        values = unique_values(hist, max_values, band)
        if not values:
            return None
        colors = class_colors(scale, len(values), reverse)
        width = (values[-1]["value"] - values[0]["value"]) / (len(values) - 1) if len(values) > 1 else 1
        return {"kind": "exact", "method": method,
                "classes": [{"value": v["value"], "color": colors[i], "label": format_class_value(v["value"], width)}
                            for i, v in enumerate(values)]}
    n = max(1, int(math.floor(count + 0.5)))
    exact = quantile_breaks(hist, n, band) if method == "quantile" else equal_interval_breaks(span[0], span[1], n)
    # Bounds as their labels write them (the last stays the largest value, so every value has a class).
    width = (span[1] - span[0]) / max(1, len(exact))
    bounds = [b if i == len(exact) - 1 else float(format_class_value(b, width)) for i, b in enumerate(exact)]
    colors = class_colors(scale, len(bounds), reverse)
    labels = range_labels(span[0], bounds)
    return {"kind": "range", "method": method, "min": span[0],
            "classes": [{"value": b, "color": colors[i], "label": labels[i]} for i, b in enumerate(bounds)]}


def class_shares(hist: RasterHistogram, classes: dict, band: int = 0) -> list:
    """The share of the pixels counted in `hist` that each class gets (0..1, in the order of classes["classes"])."""
    bins = _bins(hist, band)
    items = classes["classes"]
    counts = [0.0] * len(items)
    if bins is None or not items:
        return counts
    order = sorted(((c["value"], i) for i, c in enumerate(items)), key=lambda o: o[0])
    width = (hist.range[1] - hist.range[0]) / max(1, len(bins) - 1)
    total = 0.0
    for k in range(len(bins)):
        n = float(bins[k])
        if n <= 0:
            continue
        total += n
        value = _bin_value(hist, k, len(bins))
        pick = -1
        if classes["kind"] == "range":
            pick = next((o for o in order if value <= o[0] + width * 1e-6), order[-1])[1]
        else:
            near = order[0]
            for o in order[1:]:
                if abs(o[0] - value) < abs(near[0] - value):
                    near = o
            if abs(near[0] - value) <= max(width, 0.5):
                pick = near[1]
        if pick >= 0:
            counts[pick] += n
    return [c / total for c in counts] if total > 0 else counts


# ---- editing classes (the viewer's colorize-dialog.ts) ----

def class_label(classes: dict, index: int) -> str:
    """The name a class is listed with: its label, else its value or range."""
    c = classes["classes"][index]
    if c.get("label"):
        return c["label"]
    if classes["kind"] == "exact":
        return _js_number(c["value"])
    items = _sorted(classes)
    k = next(i for i, x in enumerate(items) if x is c)
    lo = classes.get("min")
    return range_labels(items[0]["value"] if lo is None else lo, [x["value"] for x in items])[k]


def auto_label(classes: dict, index: int) -> str:
    """The name a range class gets from its bounds (an exact class from its value)."""
    bare = [dict(c, label=None) if j == index else c for j, c in enumerate(classes["classes"])]
    return class_label(dict(classes, classes=bare), index)


def recolor(classes: dict, scale: ColorScale, reverse: bool = False) -> dict:
    """`classes` coloured again along `scale`, in order of their values (bounds and names kept)."""
    order = sorted(range(len(classes["classes"])), key=lambda i: classes["classes"][i]["value"])
    colors = class_colors(scale, len(order), reverse)
    out = [dict(c) for c in classes["classes"]]
    for k, i in enumerate(order):
        out[i]["color"] = colors[k]
    return dict(classes, classes=out)


def resize_classes(classes: dict, count: int) -> dict:
    """Range classes made `count` long: the top class split in two, or the top classes merged (named after their ranges)."""
    items = [dict(c) for c in _sorted(classes)]
    while len(items) > count and len(items) > 1:
        top = items.pop()
        items[-1]["value"] = top["value"]
    while len(items) < count:
        top = items[-1]
        lower = items[-2]["value"] if len(items) > 1 else (top["value"] - 1 if classes.get("min") is None else classes["min"])
        items.insert(len(items) - 1, dict(top, value=(lower + top["value"]) / 2))
    out = dict(classes, classes=items)
    out["classes"] = [dict(c, label=auto_label(out, j)) for j, c in enumerate(items)]
    return out


def set_bound(classes: dict, index: int, value: float) -> dict:
    """Class `index` with a new upper bound (by hand): names that followed the bounds follow the new ones."""
    auto = [c.get("label") == auto_label(classes, j) for j, c in enumerate(classes["classes"])]
    items = [dict(c, value=value) if j == index else dict(c) for j, c in enumerate(classes["classes"])]
    out = dict(classes, method="manual", classes=items)
    if out.get("min") is not None and value < out["min"]:
        out["min"] = value
    out["classes"] = [dict(c, label=auto_label(out, j)) if auto[j] else c for j, c in enumerate(items)]
    return out


def _js_number(v: float) -> str:
    """String(v) for the values classes hold (integers without '.0')."""
    return str(int(v)) if _is_integer(v) and abs(v) < 1e21 else repr(float(v))
