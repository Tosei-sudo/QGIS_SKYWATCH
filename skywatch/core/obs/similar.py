"""
「似たものを探す」 (tsudo, 2026-11-09): the things on an image that look like
one recorded with its outline (the 見本), found without a model or any
training — NumPy only, so the regular zip has it too.

Both pictures are blurred a little (a thing drawn on another grid of pixels
is a pixel off). The 見本's outline (its length, width and heading, with a
margin of the ground round it) is cut out of the image and turned to each
heading looked for; each turned copy is compared with every place of the searched picture
by normalized cross-correlation over the outline only (its mean and
contrast taken out, so a darker or lighter car of the same shape is found,
though not a place of far weaker or stronger contrast), through FFTs. The best heading per place is kept, and the places scoring
highest (at least `threshold`, −1…1) are taken one by one, a place inside
one taken (or inside a record already there) left out.

Pictures are grey levels (float) with where they have data, north up, the
pixels square; places are pixels (x right, y down), headings degrees from
north, clockwise.
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Callable, Optional, Sequence

import numpy as np

#: The turns from the 見本's heading looked for (degrees): every way round, 15° apart.
ANGLES_ALL = tuple(range(0, 360, 15))
#: Near the 見本's heading, either way round (a car parked nose in or out): ±30° in 10° steps.
ANGLES_NEAR = tuple(sorted({a % 360 for a in (*range(-30, 31, 10), *range(150, 211, 10))}))
#: Two places within this share of the 見本's half length and half width are one thing.
SAME_THING = 0.9
#: The most places found at once.
MOST = 1000
#: A place whose grey levels vary less than this (of 255) under the outline is flat ground, not a thing.
FLAT = 2.0
#: A place's contrast under the outline against the 見本's: a lighter or darker thing of its shape is
#: found, but not faint ground texture that happens to have its shape, nor a building's sharp edge.
CONTRAST = (0.4, 2.5)


@dataclass
class Found:
    """A place that looks like the 見本: its centre (pixels), heading and score (−1…1)."""
    x: float
    y: float
    heading: float
    score: float


class Cancelled(Exception):
    pass


def gray_of(rgba: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """A drawn picture (H × W × 4, 0–255) as grey levels and where it has data (not transparent)."""
    rgb = rgba[..., :3].astype(np.float64)
    gray = rgb[..., 0] * 0.299 + rgb[..., 1] * 0.587 + rgb[..., 2] * 0.114
    valid = rgba[..., 3] > 0
    gray[~valid] = 0.0
    return gray, valid


def _bilinear(gray: np.ndarray, valid: np.ndarray, x: np.ndarray, y: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """Values at fractional places, and where all four pixels round them have data."""
    h, w = gray.shape
    x0 = np.floor(x).astype(np.int64)
    y0 = np.floor(y).astype(np.int64)
    ok = (x0 >= 0) & (y0 >= 0) & (x0 + 1 < w) & (y0 + 1 < h)
    xi = np.clip(x0, 0, w - 2)
    yi = np.clip(y0, 0, h - 2)
    fx = x - xi
    fy = y - yi
    out = (gray[yi, xi] * (1 - fx) * (1 - fy) + gray[yi, xi + 1] * fx * (1 - fy)
           + gray[yi + 1, xi] * (1 - fx) * fy + gray[yi + 1, xi + 1] * fx * fy)
    ok &= valid[yi, xi] & valid[yi, xi + 1] & valid[yi + 1, xi] & valid[yi + 1, xi + 1]
    return np.where(ok, out, 0.0), ok


def half_sizes(length_px: float, width_px: Optional[float], margin_px: Optional[float] = None) -> tuple[float, float, float]:
    """Half the outline's length and width (pixels) with the margin of ground round it, and the margin."""
    width = width_px if width_px and width_px > 0 else length_px
    margin = margin_px if margin_px is not None else max(1.5, 0.15 * min(length_px, width))
    return length_px / 2 + margin, width / 2 + margin, margin


def template(gray: np.ndarray, valid: np.ndarray, cx: float, cy: float, heading: float, length_px: float,
             width_px: Optional[float], turn: float, margin_px: Optional[float] = None) -> tuple[np.ndarray, np.ndarray]:
    """
    The 見本 at (cx, cy) of a picture, heading `heading`, turned by `turn`
    degrees (as a thing heading `heading + turn` looks): a square of values and
    the outline's pixels in it. Every turn of one 見本 has the same square.
    """
    half_l, half_w, _ = half_sizes(length_px, width_px, margin_px)
    r = int(math.ceil(math.hypot(half_l, half_w)))
    dy, dx = np.mgrid[-r:r + 1, -r:r + 1].astype(np.float64)
    p = math.radians(heading + turn)
    # The thing's own frame at the turned heading: v towards its front, u to its right.
    v = dx * math.sin(p) - dy * math.cos(p)
    u = dx * math.cos(p) + dy * math.sin(p)
    inside = (np.abs(v) <= half_l) & (np.abs(u) <= half_w)
    # The same point of the thing on the 見本 as it lies.
    h = math.radians(heading)
    sx = cx + u * math.cos(h) + v * math.sin(h)
    sy = cy + u * math.sin(h) - v * math.cos(h)
    values, ok = _bilinear(gray, valid, sx, sy)
    return values, inside & ok


def _smooth_along(a: np.ndarray, kernel: np.ndarray, axis: int) -> np.ndarray:
    r = len(kernel) // 2
    pad = [(0, 0), (0, 0)]
    pad[axis] = (r, r)
    padded = np.pad(a, pad, mode="constant")
    n = a.shape[axis]
    out = np.zeros_like(a)
    for i, k in enumerate(kernel):
        out += k * (padded[i:i + n, :] if axis == 0 else padded[:, i:i + n])
    return out


def smooth(gray: np.ndarray, valid: np.ndarray, sigma: float) -> np.ndarray:
    """
    The picture blurred a little (Gaussian, `sigma` pixels; where it has no
    data left out), so a thing a pixel off (drawn on another grid of pixels)
    still matches.
    """
    if not sigma > 0:
        return gray
    r = max(1, int(math.ceil(3 * sigma)))
    kernel = np.exp(-0.5 * (np.arange(-r, r + 1) / sigma) ** 2)
    kernel /= kernel.sum()
    weight = valid.astype(np.float64)
    num = _smooth_along(_smooth_along(np.where(valid, gray, 0.0), kernel, 0), kernel, 1)
    den = _smooth_along(_smooth_along(weight, kernel, 0), kernel, 1)
    return np.where(valid & (den > 1e-9), num / np.maximum(den, 1e-9), 0.0)


def blur_of(length_px: float, width_px: Optional[float]) -> float:
    """How much to blur (pixels): an eighth of the 見本's width, 0.6–1.5."""
    short = min(length_px, width_px) if width_px and width_px > 0 else length_px
    return min(1.5, max(0.6, short / 8))


def _fast(n: int) -> int:
    """The smallest size ≥ n made of 2, 3 and 5 (quick for the FFT)."""
    best = 1 << max(0, (n - 1).bit_length())
    p5 = 1
    while p5 < best:
        p35 = p5
        while p35 < best:
            p = p35
            while p < n:
                p *= 2
            best = min(best, p)
            p35 *= 3
        p5 *= 5
    return max(best, 1)


class Correlator:
    """Normalized cross-correlation of templates with one picture (its FFTs made once)."""

    def __init__(self, gray: np.ndarray, valid: np.ndarray):
        self.h, self.w = gray.shape
        self.shape = (_fast(self.h), _fast(self.w))
        g = np.where(valid, gray, 0.0)
        self.f_gray = np.fft.rfft2(g, self.shape)
        self.f_square = np.fft.rfft2(g * g, self.shape)
        self.f_missing = np.fft.rfft2((~valid).astype(np.float64), self.shape)

    def _corr(self, f_image: np.ndarray, f_kernel: np.ndarray, h: int, w: int) -> np.ndarray:
        out = np.fft.irfft2(f_image * np.conj(f_kernel), self.shape)
        return out[: self.h - h + 1, : self.w - w + 1]

    def scores(self, values: np.ndarray, inside: np.ndarray) -> Optional[np.ndarray]:
        """
        The score of the template with its top left corner at each place it
        fits (−1…1; 0 where the picture has no data under it or is flat); None
        when it does not fit or is flat itself.
        """
        h, w = values.shape
        n = float(inside.sum())
        if h > self.h or w > self.w or n < 4:
            return None
        t = np.where(inside, values, 0.0)
        t0 = np.where(inside, values - t.sum() / n, 0.0)
        norm = math.sqrt(float((t0 * t0).sum()))
        if norm < FLAT * math.sqrt(n):
            return None
        mask = inside.astype(np.float64)
        f_t = np.fft.rfft2(t0, self.shape)
        f_m = np.fft.rfft2(mask, self.shape)
        num = self._corr(self.f_gray, f_t, h, w)
        total = self._corr(self.f_gray, f_m, h, w)
        squares = self._corr(self.f_square, f_m, h, w)
        missing = self._corr(self.f_missing, f_m, h, w)
        var = squares - total * total / n
        # The template's own spread is norm² / n: the place's must be near it.
        low, high = CONTRAST
        good = (missing < 0.5) & (var > (FLAT * FLAT) * n) & (var >= (low * norm) ** 2) & (var <= (high * norm) ** 2)
        out = np.zeros_like(num)
        out[good] = num[good] / (norm * np.sqrt(var[good]))
        return np.clip(out, -1.0, 1.0)


def _local_peaks(score: np.ndarray, threshold: float, most: int) -> tuple[np.ndarray, np.ndarray]:
    """The places (rows, columns) at least `threshold` and not under a neighbour, best first (at most `most`)."""
    if score.size == 0:
        return np.zeros(0, int), np.zeros(0, int)
    padded = np.pad(score, 1, mode="constant", constant_values=-np.inf)
    peak = score >= threshold
    h, w = score.shape
    for dy in (0, 1, 2):
        for dx in (0, 1, 2):
            if dy == 1 and dx == 1:
                continue
            peak &= score >= padded[dy:dy + h, dx:dx + w]
    ys, xs = np.nonzero(peak)
    order = np.argsort(-score[ys, xs], kind="stable")[:most]
    return ys[order], xs[order]


def find_similar(search_gray: np.ndarray, search_valid: np.ndarray, sample_gray: np.ndarray, sample_valid: np.ndarray,
                 sx: float, sy: float, heading: float, length_px: float, width_px: Optional[float],
                 angles: Sequence[float] = ANGLES_ALL, threshold: float = 0.6, taken: Sequence[tuple[float, float]] = (),
                 most: int = MOST, progress: Optional[Callable[[float], None]] = None,
                 cancelled: Optional[Callable[[], bool]] = None) -> list[Found]:
    """
    The places of the searched picture looking like the 見本 (at sx, sy of its
    own picture, drawn at the same scale), best first. `taken`: places of the
    searched picture already recorded (the 見本 itself among them), not found
    again. `progress` gets 0…1, `cancelled` stops the work (Cancelled).
    """
    if not (length_px > 0):
        return []
    sigma = blur_of(length_px, width_px)
    search_gray = smooth(search_gray, search_valid, sigma)
    sample_gray = smooth(sample_gray, sample_valid, sigma)
    corr = Correlator(search_gray, search_valid)
    best: Optional[np.ndarray] = None
    turn_of: Optional[np.ndarray] = None
    size = 0
    # Turns from the 見本's heading (0 among them: as it lies).
    turns = [float(a) for a in angles]
    for i, turn in enumerate(turns):
        if cancelled is not None and cancelled():
            raise Cancelled()
        values, inside = template(sample_gray, sample_valid, sx, sy, heading, length_px, width_px, turn)
        size = values.shape[0]
        s = corr.scores(values, inside)
        if s is not None:
            if best is None:
                best = s
                turn_of = np.zeros(s.shape, dtype=np.int16)
            else:
                better = s > best
                best = np.where(better, s, best)
                turn_of[better] = i
        if progress is not None:
            progress((i + 1) / len(turns))
    if best is None:
        return []
    half_l, half_w, _ = half_sizes(length_px, width_px)
    reach_l, reach_w = (half_l * SAME_THING, half_w * SAME_THING)
    r = size // 2
    ys, xs = _local_peaks(best, threshold, 20 * most)
    kept_x: list[float] = [float(p[0]) for p in taken]
    kept_y: list[float] = [float(p[1]) for p in taken]
    found: list[Found] = []
    for y, x in zip(ys.tolist(), xs.tolist()):
        h = (heading + turns[int(turn_of[y, x])]) % 360
        cx, cy = float(x + r), float(y + r)
        if kept_x:
            p = math.radians(h)
            dx = np.asarray(kept_x) - cx
            dy = np.asarray(kept_y) - cy
            v = dx * math.sin(p) - dy * math.cos(p)
            u = dx * math.cos(p) + dy * math.sin(p)
            if bool(np.any((np.abs(v) <= reach_l) & (np.abs(u) <= reach_w))):
                continue
        found.append(Found(cx, cy, h, float(best[y, x])))
        kept_x.append(cx)
        kept_y.append(cy)
        if len(found) >= most:
            break
    return found
