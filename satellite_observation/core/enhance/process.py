"""
Runs a correction on 8-bit RGBA pixels, as browser-image-enhancement's
core/process.ts: pixels are decoded to linear light, every step runs in
float64 and the result is rounded to 8 bits once.

As in the library, per-channel steps at the start fold into 256-entry tables
(a chain of per-channel steps alone is a table lookup), and per-channel steps
at the end fold into the rounding (quantizer.py), so only the steps that mix
channels run per pixel. Around a sharpen the image is held as sRGB-encoded
float32 planes.
"""
from __future__ import annotations

from typing import Optional, Sequence, Union

import numpy as np

from .filter import sharpen_planes
from .ops import COLOR_ONLY_OPS, for_gray, is_identity, to_stage
from .pipeline import Pipeline
from .quantizer import PLAIN, Quantizer
from .srgb import LUMA_B, LUMA_G, LUMA_R, SRGB_TO_LINEAR, linear_to_srgb, srgb_to_linear
from .stats import Histogram, count_pixels, resolve_ops


def is_monochrome(rgba: np.ndarray) -> bool:
    return bool(((rgba[..., 0] == rgba[..., 1]) & (rgba[..., 1] == rgba[..., 2])).all())


def resolve_mode(rgba: np.ndarray, color_mode: str = "auto") -> str:
    if color_mode == "auto":
        return "gray" if is_monochrome(rgba) else "rgb"
    return color_mode


def _run(stages: Sequence[tuple], v: list) -> list:
    """Runs per-pixel steps on linear values (3 arrays, or 1 for gray)."""
    for s in stages:
        if s[0] == "channel":
            v = [s[1](x, c) for c, x in enumerate(v)]
        else:
            y = LUMA_R * v[0] + LUMA_G * v[1] + LUMA_B * v[2]
            f = s[1]
            v = [y + (x - y) * f for x in v]
    return v


def _chain(stages: Sequence[tuple], c: int, decode: bool = False):
    def f(v):
        if decode:
            v = srgb_to_linear(v)
        for s in stages:
            v = s[1](v, c)
        return v
    return f


def _split_tail(stages: Sequence[tuple]) -> tuple:
    """The steps, and the trailing per-channel steps that never fall (which fold into the rounding)."""
    start = len(stages)
    while start > 0 and stages[start - 1][0] == "channel" and not stages[start - 1][2]:
        start -= 1
    return list(stages[:start]), list(stages[start:])


def stages_of(ops: Sequence[dict], mode: str) -> list:
    return [to_stage(for_gray(op) if mode == "gray" else op)
            for op in ops if not is_identity(op) and not (mode == "gray" and op["op"] in COLOR_ONLY_OPS)]


def _tail(trailing: Sequence[tuple], c: int, encoded: bool) -> Quantizer:
    if not trailing and not encoded:
        return PLAIN
    return Quantizer(_chain(trailing, c, decode=encoded))


def process(rgba: np.ndarray, ops: Union[Pipeline, Sequence[dict]], color_mode: str = "auto",
            stats: Optional[Histogram] = None, mode: Optional[str] = None) -> np.ndarray:
    """
    The corrected copy of an H×W×4 uint8 RGBA image. `autoStretch` steps take
    their range from `stats` (a histogram of the whole picture, for tiles) or
    from the image itself. Alpha is copied; transparent pixels take no part
    in statistics or in the sharpen blur.
    """
    ops = list(ops.ops if isinstance(ops, Pipeline) else ops)
    rgba = np.ascontiguousarray(rgba, dtype=np.uint8)
    mode = mode or resolve_mode(rgba, color_mode)
    if any(op["op"] == "autoStretch" for op in ops):
        ops = resolve_ops(ops, stats if stats is not None else count_pixels(rgba, mode))
    stages = stages_of(ops, mode)
    out = rgba.copy()
    if not stages:
        return out
    channels = 3 if mode == "rgb" else 1
    cut = next((i for i, s in enumerate(stages) if s[0] == "sharpen"), len(stages))
    before = stages[:cut]
    lead = 0
    while lead < len(before) and before[lead][0] == "channel":
        lead += 1
    leading, rest = before[:lead], before[lead:]
    lut = [_chain(leading, c)(SRGB_TO_LINEAR) for c in range(channels)]

    if mode == "rgb":
        src = [rgba[..., c] for c in range(3)]
        same = None
    else:
        r, g, b = rgba[..., 0], rgba[..., 1], rgba[..., 2]
        same = (r == g) & (g == b)
        src = [r]
        if same.all():
            same = None

    def head() -> list:
        """Linear values after the steps before the first sharpen (or all, without one)."""
        v = [lut[c][src[c]] for c in range(channels)]
        if same is not None:
            # Colored pixels in forced gray mode: computed on their luminance.
            y = LUMA_R * SRGB_TO_LINEAR[r] + LUMA_G * SRGB_TO_LINEAR[g] + LUMA_B * SRGB_TO_LINEAR[b]
            v = [np.where(same, v[0], _chain(leading, 0)(y))]
        return v

    def write(codes: list) -> np.ndarray:
        if channels == 3:
            for c in range(3):
                out[..., c] = codes[c]
        else:
            out[..., :3] = codes[0][..., None]
        return out

    if cut == len(stages):
        if not rest and same is None:
            return write([PLAIN.quantize(lut[c])[src[c]] for c in range(channels)])
        middle, trailing = _split_tail(rest)
        values = _run(middle, head())
        return write([_tail(trailing, c, False).quantize(values[c]) for c in range(channels)])

    # Around sharpen: sRGB-encoded float32 planes.
    if not rest and same is None:
        planes = [linear_to_srgb(lut[c]).astype(np.float32)[src[c]] for c in range(channels)]
    else:
        planes = [linear_to_srgb(v).astype(np.float32) for v in _run(rest, head())]
    groups: list = []
    for s in stages[cut:]:
        if s[0] == "sharpen":
            groups.append((s[1], []))
        else:
            groups[-1][1].append(s)
    alpha = rgba[..., 3]
    for i, (stage, after) in enumerate(groups):
        sharpen_planes(planes, alpha, stage)
        if after and i < len(groups) - 1:
            vals = _run(after, [srgb_to_linear(p) for p in planes])
            planes = [linear_to_srgb(v).astype(np.float32) for v in vals]
    middle, trailing = _split_tail(groups[-1][1])
    if not middle:
        # Decoding folds into the rounding too.
        return write([_tail(trailing, c, True).quantize(planes[c]) for c in range(channels)])
    values = _run(middle, [srgb_to_linear(p) for p in planes])
    return write([_tail(trailing, c, False).quantize(values[c]) for c in range(channels)])
