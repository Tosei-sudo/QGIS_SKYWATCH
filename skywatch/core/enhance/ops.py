"""
The correction steps, as browser-image-enhancement's ops/index.ts, ops/info.ts
and ops/curve.ts: parameter ranges and defaults, normalization (values out of
range are clamped, never refused) and the math of each step.

All math runs on linear-light values in float64. A step is either per channel
(`("channel", fn, falls)`, `fn(values, c)` for c = 0, 1, 2 or 0 for gray),
`("saturation", factor)` which mixes channels, or `("sharpen", options)` which
reads neighbouring pixels (filter.py).
"""
from __future__ import annotations

import math
from typing import Any, Callable, Optional

import numpy as np

from .srgb import LUMA_B, LUMA_G, LUMA_R, linear_to_srgb, srgb_to_linear


def _amount(step: float = 0.01) -> dict:
    return {"type": "number", "min": -1.0, "max": 1.0, "default": 0.0, "step": step}


def _num(lo: float, hi: float, default: float, step: float) -> dict:
    return {"type": "number", "min": lo, "max": hi, "default": default, "step": step}


_CURVE = {"type": "curve", "default": [[0.0, 0.0], [1.0, 1.0]]}

#: Every step's parameters (range, default, slider step), its one-number parameter, and what kind of step it is (ops/info.ts).
OP_INFO: dict[str, dict] = {
    "brightness": {"params": {"amount": _amount()}, "value": "amount", "colorOnly": False, "spatial": False, "auto": False},
    "contrast": {"params": {"amount": _amount()}, "value": "amount", "colorOnly": False, "spatial": False, "auto": False},
    "exposure": {"params": {"ev": _num(-10, 10, 0, 0.05)}, "value": "ev", "colorOnly": False, "spatial": False, "auto": False},
    "gamma": {"params": {"gamma": _num(0.1, 10, 1, 0.01)}, "value": "gamma", "colorOnly": False, "spatial": False, "auto": False},
    "saturation": {"params": {"amount": _amount()}, "value": "amount", "colorOnly": True, "spatial": False, "auto": False},
    "temperature": {"params": {"amount": _amount()}, "value": "amount", "colorOnly": True, "spatial": False, "auto": False},
    "tint": {"params": {"amount": _amount()}, "value": "amount", "colorOnly": True, "spatial": False, "auto": False},
    "whiteBalance": {
        "params": {k: _num(1 / 255, 1, 0.5, 1 / 255) for k in ("r", "g", "b")},
        "value": None, "colorOnly": True, "spatial": False, "auto": False,
    },
    "shadows": {"params": {"amount": _amount()}, "value": "amount", "colorOnly": False, "spatial": False, "auto": False},
    "highlights": {"params": {"amount": _amount()}, "value": "amount", "colorOnly": False, "spatial": False, "auto": False},
    "curve": {"params": {"points": _CURVE, "red": _CURVE, "green": _CURVE, "blue": _CURVE},
              "value": None, "colorOnly": False, "spatial": False, "auto": False},
    "levels": {
        "params": {
            "inBlack": _num(0, 1, 0, 1 / 255), "inWhite": _num(0, 1, 1, 1 / 255), "gamma": _num(0.1, 10, 1, 0.01),
            "outBlack": _num(0, 1, 0, 1 / 255), "outWhite": _num(0, 1, 1, 1 / 255),
        },
        "value": None, "colorOnly": False, "spatial": False, "auto": False,
    },
    "stretch": {
        "params": {"black": {"type": "rgb", "min": 0, "max": 1, "default": 0, "step": 1 / 255},
                   "white": {"type": "rgb", "min": 0, "max": 1, "default": 1, "step": 1 / 255}},
        "value": None, "colorOnly": False, "spatial": False, "auto": False,
    },
    "autoStretch": {
        "params": {
            "method": {"type": "enum", "values": ["percentClip", "minMax", "standardDeviation"], "default": "percentClip"},
            "lowPercent": _num(0, 50, 0.5, 0.1), "highPercent": _num(0, 50, 0.5, 0.1), "stdDevs": _num(0.1, 10, 2, 0.1),
            "linked": {"type": "boolean", "default": False},
        },
        "value": None, "colorOnly": False, "spatial": False, "auto": True,
    },
    "sharpen": {
        "params": {"amount": _num(0, 5, 0.5, 0.05), "radius": _num(0.1, 50, 1, 0.1), "threshold": _num(0, 1, 0, 1 / 255)},
        "value": "amount", "colorOnly": False, "spatial": True, "auto": False,
    },
}

#: Steps that only change colours; nothing on a monochrome image.
COLOR_ONLY_OPS = frozenset(name for name, info in OP_INFO.items() if info["colorOnly"])
STRETCH_METHODS = ("percentClip", "minMax", "standardDeviation")

#: Contrast pivots around sRGB 50 % gray.
CONTRAST_PIVOT = srgb_to_linear(0.5)
TEMPERATURE_STRENGTH = 0.4
TINT_STRENGTH = 0.3
TONE_STRENGTH = 0.945
#: Upper bound for stretch points: far above white, but finite.
STRETCH_MAX = 1000.0


def _is_number(v: Any) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool) and not math.isnan(v)


def _clamp(value: Any, lo: float, hi: float, fallback: float) -> float:
    if not _is_number(value):
        return float(fallback)
    return float(min(hi, max(lo, value)))


def _param(op: str, name: str, value: Any) -> float:
    p = OP_INFO[op]["params"][name]
    return _clamp(value, p["min"], p["max"], p["default"])


# ---- curves (Fritsch-Carlson monotone cubic) ----

def normalize_curve(raw: Any) -> list:
    """Valid [input, output] points in 0-1, sorted, reaching inputs 0 and 1 (a straight line for nothing usable)."""
    if raw is None:
        return [[0.0, 0.0], [1.0, 1.0]]
    by_x: dict[float, float] = {}
    for p in raw if isinstance(raw, (list, tuple)) else []:
        if not isinstance(p, (list, tuple)) or len(p) < 2 or not all(_is_number(v) and math.isfinite(v) for v in p[:2]):
            continue
        by_x[min(1.0, max(0.0, float(p[0])))] = min(1.0, max(0.0, float(p[1])))
    points = [[x, by_x[x]] for x in sorted(by_x)]
    if not points or points[0][0] > 0:
        points.insert(0, [0.0, 0.0])
    if points[-1][0] < 1:
        points.append([1.0, 1.0])
    return points


def is_identity_curve(points) -> bool:
    return all(x == y for x, y in points)


def curve_function(points) -> Callable[[np.ndarray], np.ndarray]:
    """The curve on [0, 1] (inputs outside clamped), for arrays."""
    if is_identity_curve(points):
        return lambda x: np.clip(x, 0.0, 1.0)
    n = len(points)
    xs = [p[0] for p in points]
    ys = [p[1] for p in points]
    d = [(ys[i + 1] - ys[i]) / (xs[i + 1] - xs[i]) for i in range(n - 1)]
    m = [0.0] * n
    m[0] = d[0]
    m[n - 1] = d[n - 2]
    for i in range(1, n - 1):
        m[i] = 0.0 if d[i - 1] * d[i] <= 0 else (d[i - 1] + d[i]) / 2
    for i in range(n - 1):
        if d[i] == 0:
            m[i] = 0.0
            m[i + 1] = 0.0
            continue
        a = m[i] / d[i]
        b = m[i + 1] / d[i]
        h = a * a + b * b
        if h > 9:
            t = 3 / math.sqrt(h)
            m[i] = t * a * d[i]
            m[i + 1] = t * b * d[i]
    xa = np.array(xs)
    ya = np.array(ys)
    ma = np.array(m)

    def f(x: np.ndarray) -> np.ndarray:
        x = np.asarray(x, dtype=np.float64)
        # The piece i with xs[i] < x <= xs[i + 1], as the library's scan finds it.
        i = np.clip(np.searchsorted(xa, x, side="left") - 1, 0, n - 2)
        h = xa[i + 1] - xa[i]
        t = (x - xa[i]) / h
        t2 = t * t
        t3 = t2 * t
        y = (2 * t3 - 3 * t2 + 1) * ya[i] + (t3 - 2 * t2 + t) * h * ma[i] + (-2 * t3 + 3 * t2) * ya[i + 1] + (t3 - t2) * h * ma[i + 1]
        y = np.clip(y, 0.0, 1.0)
        with np.errstate(invalid="ignore"):
            y = np.where(~(x > xa[0]), ya[0], y)
            y = np.where(x >= xa[n - 1], ya[n - 1], y)
        return y

    return f


# ---- normalization ----

def _rgb(value: Any, fallback: float) -> list:
    if isinstance(value, (list, tuple)):
        return [_clamp(value[c] if c < len(value) else None, 0, STRETCH_MAX, fallback) for c in range(3)]
    v = _clamp(value, 0, STRETCH_MAX, fallback)
    return [v, v, v]


def normalize_auto_stretch(o: Optional[dict] = None) -> dict:
    o = o or {}
    method = o.get("method") if o.get("method") in STRETCH_METHODS else "percentClip"
    return {
        "method": method,
        "lowPercent": _param("autoStretch", "lowPercent", o.get("lowPercent")),
        "highPercent": _param("autoStretch", "highPercent", o.get("highPercent")),
        "stdDevs": _param("autoStretch", "stdDevs", o.get("stdDevs")),
        "linked": bool(o.get("linked", False)),
    }


def normalize_op(raw: Any) -> dict:
    """A step from code or JSON with its parameters filled and clamped. Raises ValueError only for an unknown step."""
    o = raw if isinstance(raw, dict) else {}
    name = o.get("op")
    if name in ("brightness", "contrast", "saturation", "temperature", "tint", "shadows", "highlights"):
        return {"op": name, "amount": _param(name, "amount", o.get("amount"))}
    if name == "exposure":
        return {"op": name, "ev": _param(name, "ev", o.get("ev"))}
    if name == "gamma":
        return {"op": name, "gamma": _param(name, "gamma", o.get("gamma"))}
    if name == "whiteBalance":
        return {"op": name, **{k: _param(name, k, o.get(k)) for k in ("r", "g", "b")}}
    if name == "curve":
        return {"op": name, **{k: normalize_curve(o.get(k)) for k in ("points", "red", "green", "blue")}}
    if name == "levels":
        lv = {k: _param(name, k, o.get(k)) for k in ("inBlack", "inWhite", "gamma", "outBlack", "outWhite")}
        if lv["inWhite"] <= lv["inBlack"]:
            gap = 1 / 255
            if lv["inBlack"] + gap <= 1:
                lv["inWhite"] = lv["inBlack"] + gap
            else:
                lv["inBlack"], lv["inWhite"] = 1 - gap, 1.0
        return {"op": name, **lv}
    if name == "stretch":
        black = _rgb(o.get("black"), 0.0)
        white = _rgb(o.get("white"), 1.0)
        for c in range(3):
            if not white[c] > black[c]:
                black[c], white[c] = 0.0, 1.0
        return {"op": name, "black": black, "white": white}
    if name == "autoStretch":
        return {"op": name, **normalize_auto_stretch(o)}
    if name == "sharpen":
        return {"op": name, **{k: _param(name, k, o.get(k)) for k in ("amount", "radius", "threshold")}}
    raise ValueError(f"Unknown correction: {name}")


def is_identity(op: dict) -> bool:
    """True when the step leaves every pixel as it is."""
    name = op["op"]
    if name in ("brightness", "contrast", "saturation", "temperature", "tint", "shadows", "highlights"):
        return op["amount"] == 0
    if name == "whiteBalance":
        return op["r"] == op["g"] == op["b"]
    if name == "curve":
        return all(is_identity_curve(op[k]) for k in ("points", "red", "green", "blue"))
    if name == "exposure":
        return op["ev"] == 0
    if name == "gamma":
        return op["gamma"] == 1
    if name == "levels":
        return (op["inBlack"], op["inWhite"], op["gamma"], op["outBlack"], op["outWhite"]) == (0, 1, 1, 0, 1)
    if name == "stretch":
        return all(op["black"][c] == 0 and op["white"][c] == 1 for c in range(3))
    if name == "sharpen":
        return op["amount"] == 0
    return False


def kernel_radius(radius: float) -> int:
    """How far (pixels) the blur of a sharpen with this radius reaches."""
    return math.ceil(3 * radius)


def margin_of(ops) -> int:
    """Pixels of context each side the steps need (0 when every step is per pixel)."""
    return sum(kernel_radius(op["radius"]) for op in ops if op["op"] == "sharpen" and not is_identity(op))


# ---- math ----

def _balanced(r: float, g: float, b: float) -> tuple:
    n = LUMA_R * r + LUMA_G * g + LUMA_B * b
    return (r / n, g / n, b / n)


def temperature_gains(amount: float) -> tuple:
    return _balanced(1 + TEMPERATURE_STRENGTH * amount, 1, 1 - TEMPERATURE_STRENGTH * amount)


def tint_gains(amount: float) -> tuple:
    side = 1 + (TINT_STRENGTH * amount) / 2
    return _balanced(side, 1 - TINT_STRENGTH * amount, side)


def white_balance_gains(op: dict) -> tuple:
    return _balanced(1 / srgb_to_linear(op["r"]), 1 / srgb_to_linear(op["g"]), 1 / srgb_to_linear(op["b"]))


def tone_shift(x: np.ndarray, amount: float, high: bool) -> np.ndarray:
    k = TONE_STRENGTH * amount
    return x + k * (x * x * (1 - x) if high else x * (1 - x) * (1 - x))


def for_gray(op: dict) -> dict:
    """In gray mode: a stretch uses the mean of its points, a curve only its curve for all channels."""
    if op["op"] == "curve":
        line = [[0.0, 0.0], [1.0, 1.0]]
        return {**op, "red": line, "green": line, "blue": line}
    if op["op"] != "stretch":
        return op
    b = sum(op["black"]) / 3
    w = sum(op["white"]) / 3
    return {"op": "stretch", "black": [b, b, b], "white": [w, w, w]}


def _pos_pow(v: np.ndarray, e: float) -> np.ndarray:
    with np.errstate(invalid="ignore", over="ignore", divide="ignore"):
        return np.where(v > 0, np.power(np.where(v > 0, v, 1.0), e), 0.0)


def to_stage(op: dict) -> tuple:
    """The math of a normalized step."""
    name = op["op"]
    if name == "brightness":
        b = op["amount"]
        if b == 1:
            return ("channel", lambda v, c: np.ones_like(v), False)
        if b == -1:
            return ("channel", lambda v, c: np.zeros_like(v), False)
        s = 1 - abs(b)
        offset = b if b > 0 else 0.0
        return ("channel", lambda v, c: v * s + offset, False)
    if name == "contrast":
        a = op["amount"]
        k = 1 / max(1 - a, 1 / 1024) if a >= 0 else 1 + a
        p = CONTRAST_PIVOT
        return ("channel", lambda v, c: p * _pos_pow(v / p, k), False)
    if name == "exposure":
        m = 2.0 ** op["ev"]
        return ("channel", lambda v, c: v * m, False)
    if name == "gamma":
        e = 1 / op["gamma"]
        return ("channel", lambda v, c: _pos_pow(v, e), False)
    if name == "saturation":
        return ("saturation", 1 + op["amount"])
    if name in ("temperature", "tint", "whiteBalance"):
        gains = (temperature_gains(op["amount"]) if name == "temperature"
                 else tint_gains(op["amount"]) if name == "tint" else white_balance_gains(op))
        return ("channel", lambda v, c: v * gains[c], False)
    if name in ("shadows", "highlights"):
        amount = op["amount"]
        high = name == "highlights"

        def tone(v, c):
            inside = (v > 0) & (v < 1)
            x = linear_to_srgb(np.where(inside, v, 0.5))
            return np.where(inside, srgb_to_linear(tone_shift(x, amount, high)), v)

        return ("channel", tone, False)
    if name == "curve":
        all_ = curve_function(op["points"])
        own = [curve_function(op[k]) for k in ("red", "green", "blue")]
        falls = any(pts[i][1] < pts[i - 1][1] for pts in (op["points"], op["red"], op["green"], op["blue"]) for i in range(1, len(pts)))
        return ("channel", lambda v, c: srgb_to_linear(own[c](all_(linear_to_srgb(v)))), falls)
    if name == "levels":
        in_black, in_range = op["inBlack"], op["inWhite"] - op["inBlack"]
        out_black, out_range = op["outBlack"], op["outWhite"] - op["outBlack"]
        e = 1 / op["gamma"]

        def levels(v, c):
            x = (linear_to_srgb(v) - in_black) / in_range
            x = np.where(x <= 0, 0.0, np.where(x >= 1, 1.0, _pos_pow(x, e)))
            return srgb_to_linear(out_black + x * out_range)

        return ("channel", levels, out_range < 0)
    if name == "stretch":
        black = op["black"]
        scale = [1 / (op["white"][c] - black[c]) for c in range(3)]

        def stretch(v, c):
            x = (linear_to_srgb(v) - black[c]) * scale[c]
            return np.where(x <= 0, 0.0, np.where(x >= 1, 1.0, srgb_to_linear(np.clip(x, 0, 1))))

        return ("channel", stretch, False)
    if name == "sharpen":
        return ("sharpen", {"amount": op["amount"], "radius": op["radius"], "threshold": op["threshold"]})
    raise ValueError("autoStretch has no fixed math; resolve it with image statistics first.")
