"""
「似たものを探す」 (tsudo, 2026-11-09): the things on an image that look like
one recorded with its outline (the 見本), found without a model or any
training beforehand — NumPy only, so the regular zip has it too.

How (tsudo, 2026-11-10: 「回転させつつ、誤検出が多かった」): not the grey
levels but the shape's edges are compared, and what sets the 見本 apart from
this picture's own ground is learnt on the spot.

- Edges: the gradient orientations in small square cells (HOG: 8
  orientations, either way round, each cell scaled by the strength of the
  edges round it, so a lighter or darker thing of the shape scores alike).
  The 見本 is about `CELLS_ALONG` cells long, with a cell of ground round it.
- Turns: the picture is turned so a thing heading the way looked for points
  up, and the cells are compared there; a quarter turn more is the same
  cells re-ordered (and the orientations moved round by half), so only the
  turns under 90° are drawn anew.
- Learnt (LDA, "whitened HOG"): windows all over the searched picture give
  the mean and covariance of its ground; the weights are
  Σ⁻¹ (見本 − ground mean), so what is everywhere in this picture (piers,
  roofs, road marks, trees) counts little and what only the 見本 has counts
  much. A score is 1 for the 見本 (the mean of the 見本 when there are
  several) and 0 for the ground on average.
- Several 見本: the records of the same kind and about the same size on the
  image are 見本 too (their mean), and the places recorded as wrong (誤検出
  in 「AI結果」) are taken as ground that looks like the 見本 — so checking
  the first ones found and searching again finds far fewer wrong ones.

- Gaps: a place with things found (or recorded) on both sides nearer than
  a thing of the 見本's width could be is the gap between them (`GAP`): the
  edges are the same either way round, so the dark gap between two light
  cars looks like a dark car.

Measured on two public aerial photos with every thing outlined (DOTA
examples: 531 boats in a marina, 22 planes at an airport; 2026-11-10; the
mean over 4–6 choices of 見本): with one 見本, the wrong ones before half the
planes were found went from 18 (grey-level correlation, the earlier way) to
1, the boats about as before (124 → 96); with the first 30 results checked
and searched again, the wrong ones before 70 % of the boats were found went
from about 300 to about 40, and before 70 % of the planes from 71 to none.

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
#: Near the 見本's heading, either way round (a car parked nose in or out): ±30° and the reverse ±30°, 15° apart.
ANGLES_NEAR = (0, 15, 30, 150, 165, 180, 195, 210, 330, 345)
#: Two places within this share of the 見本's half length and half width (with the margin) are one thing.
SAME_THING = 0.9
#: A place with things found (or recorded) on both sides nearer than this share of the 見本's width (or ends nearer
#: than this share of its length) is the gap between them: a thing there would overlap them.
GAP = 0.9
#: The most places found at once.
MOST = 1000
#: Gradient orientations per cell (either way round, 180° / 8 = 22.5° each: a quarter turn is 4 of them).
BINS = 8
#: The 見本 is about this many cells long.
CELLS_ALONG = 8
#: Cells of ground round the 見本's outline in the window compared.
MARGIN_CELLS = 1
#: A cell's orientations at most this (after scaling by the edges round it): one strong edge does not decide alone.
CLIP = 0.7
#: How much the ground's covariance is evened out (its mean variance times this added to the diagonal).
RIDGE = 1.0
#: Windows of ground taken from the searched picture (over its turns in `GROUND_TURNS` and their quarter turns).
GROUND_WINDOWS = 12000
#: The turns of the searched picture the ground is taken from (each with its quarter turns): edges every way round.
GROUND_TURNS = (0.0, 45.0)
#: A window is compared where at least this share of its cells has data.
COVERED = 0.9
#: The share of the ground made of the places recorded as wrong (with many of them; fewer weigh less).
WRONG_WEIGHT = 0.5
#: How a window stands out from the ground (its weighted sum, in the ground's standard deviations) is said
#: as a score 0…1: 0.5 this many deviations out, where on the photos measured about 85 % of what was found
#: was right with one 見本 or with 30 checked (4.2–5.8), ...
Z_MID = 4.5
#: ... rising from 0.27 to 0.73 over two more deviations (0.8 at about 6).
Z_SPREAD = 1.0


@dataclass
class Found:
    """A place that looks like the 見本: its centre (pixels), heading and score (0…1, 0.5 standing `Z_MID` deviations out of the ground)."""
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


def half_sizes(length_px: float, width_px: Optional[float], margin_px: Optional[float] = None) -> tuple[float, float, float]:
    """Half the outline's length and width (pixels) with the margin of ground round it, and the margin."""
    width = width_px if width_px and width_px > 0 else length_px
    margin = margin_px if margin_px is not None else max(1.5, 0.15 * min(length_px, width))
    return length_px / 2 + margin, width / 2 + margin, margin


def _bilinear(gray: np.ndarray, valid: np.ndarray, x: np.ndarray, y: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """Values at fractional places, and where all four pixels round them have data (only those inside are worked out)."""
    h, w = gray.shape
    out = np.zeros(x.shape)
    ok = np.zeros(x.shape, bool)
    x0 = np.floor(x)
    y0 = np.floor(y)
    inside = (x0 >= 0) & (y0 >= 0) & (x0 + 1 < w) & (y0 + 1 < h)
    if not inside.any():
        return out, ok
    xi = x0[inside].astype(np.int64)
    yi = y0[inside].astype(np.int64)
    fx = x[inside] - xi
    fy = y[inside] - yi
    at = yi * w + xi
    g = gray.ravel()
    v = valid.ravel()
    value = (g[at] * (1 - fx) * (1 - fy) + g[at + 1] * fx * (1 - fy) + g[at + w] * (1 - fx) * fy + g[at + w + 1] * fx * fy)
    has = v[at] & v[at + 1] & v[at + w] & v[at + w + 1]
    out[inside] = np.where(has, value, 0.0)
    ok[inside] = has
    return out, ok


class _Turn:
    """
    The searched picture turned so a thing heading `heading` points up, on a
    square of `size` pixels (a whole number of cells) round the picture's
    centre: a place (u, v) from that centre is the picture's
    centre + u (cos h, sin h) + v (−sin h, cos h).
    """

    def __init__(self, shape: tuple[int, int], heading: float, size: int):
        h, w = shape
        self.c = ((w - 1) / 2, (h - 1) / 2)
        self.size = size
        self.cp = (size - 1) / 2
        r = math.radians(heading)
        self.cos, self.sin = math.cos(r), math.sin(r)

    def draw(self, gray: np.ndarray, valid: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
        out = np.zeros((self.size, self.size))
        ok = np.zeros((self.size, self.size), bool)
        u = np.arange(self.size, dtype=np.float64) - self.cp
        step = 256
        for top in range(0, self.size, step):
            v = (np.arange(top, min(self.size, top + step), dtype=np.float64) - self.cp)[:, None]
            x = self.c[0] + u[None, :] * self.cos - v * self.sin
            y = self.c[1] + u[None, :] * self.sin + v * self.cos
            out[top:top + len(v)], ok[top:top + len(v)] = _bilinear(gray, valid, x, y)
        return out, ok

    def to_picture(self, x: float, y: float) -> tuple[float, float]:
        u, v = x - self.cp, y - self.cp
        return self.c[0] + u * self.cos - v * self.sin, self.c[1] + u * self.sin + v * self.cos


def _box(a: np.ndarray, r: int) -> np.ndarray:
    """The sum over the (2r + 1)² square round each place (outside counts 0)."""
    for axis in (0, 1):
        n = a.shape[axis]
        c = np.cumsum(np.pad(a, [(r + 1, r) if i == axis else (0, 0) for i in range(a.ndim)]), axis=axis)
        a = np.take(c, np.arange(2 * r + 1, 2 * r + 1 + n), axis=axis) - np.take(c, np.arange(n), axis=axis)
    return a


def cells(gray: np.ndarray, valid: np.ndarray, cell: int) -> tuple[np.ndarray, np.ndarray]:
    """
    The picture's cells (rows × columns × BINS, float32): the gradient
    orientations (either way round) weighted by their strength, each cell
    scaled by the edges of the 3 × 3 cells round it and clipped; and the
    cells with data (most of their pixels).
    """
    gx = np.zeros_like(gray)
    gy = np.zeros_like(gray)
    gx[:, 1:-1] = (gray[:, 2:] - gray[:, :-2]) / 2
    gy[1:-1, :] = (gray[2:, :] - gray[:-2, :]) / 2
    ok = valid.copy()
    ok[:, 1:-1] &= valid[:, 2:] & valid[:, :-2]
    ok[1:-1, :] &= valid[2:, :] & valid[:-2, :]
    ok[:, [0, -1]] = False
    ok[[0, -1], :] = False
    rows, cols = gray.shape[0] // cell, gray.shape[1] // cell
    if rows == 0 or cols == 0:
        return np.zeros((rows, cols, BINS), np.float32), np.zeros((rows, cols), bool)
    h, w = rows * cell, cols * cell
    gx, gy, ok = gx[:h, :w], gy[:h, :w], ok[:h, :w]
    # Only the pixels with data (a turned picture's corners have none).
    at = np.flatnonzero(ok)
    ex = gx.ravel()[at].astype(np.float32)
    ey = gy.ravel()[at].astype(np.float32)
    strength = np.hypot(ex, ey)
    place = (np.arctan2(ey, ex) % np.float32(math.pi)) * np.float32(BINS / math.pi)
    low = np.floor(place)
    share = place - low
    low = low.astype(np.int64) % BINS
    high = (low + 1) % BINS
    which = ((at // w) // cell * cols + (at % w) // cell) * BINS
    n = rows * cols * BINS
    out = (np.bincount(which + low, strength * (1 - share), n)
           + np.bincount(which + high, strength * share, n)).reshape(rows, cols, BINS)
    has = ok.reshape(rows, cell, cols, cell).mean(axis=(1, 3)) > 0.5
    energy = _box((out ** 2).sum(-1), 1) / 9.0
    out = np.minimum(out / (np.sqrt(energy)[..., None] + 0.25), CLIP)
    return out.astype(np.float32), has


def _quarter(f: np.ndarray, has: np.ndarray, k: int) -> tuple[np.ndarray, np.ndarray]:
    """The cells of the picture turned a further k quarter turns (the same cells re-ordered, orientations moved round)."""
    k %= 4
    if k == 0:
        return f, has
    f = np.rot90(f, k, axes=(0, 1))
    if k % 2:
        f = np.roll(f, BINS // 2, axis=2)
    return np.ascontiguousarray(f), np.rot90(has, k)


class Shape:
    """The window compared: its cell (pixels) and its cells (rows along the thing, columns across)."""

    def __init__(self, length_px: float, width_px: Optional[float]):
        width = width_px if width_px and width_px > 0 else length_px
        self.cell = max(2, int(round(length_px / CELLS_ALONG)))
        self.rows = int(math.ceil(length_px / self.cell)) + 2 * MARGIN_CELLS
        self.cols = int(math.ceil(width / self.cell)) + 2 * MARGIN_CELLS
        self.size = self.rows * self.cols * BINS


def window_at(gray: np.ndarray, valid: np.ndarray, x: float, y: float, heading: float, shape: Shape) -> Optional[np.ndarray]:
    """The window's cells of a thing at (x, y) heading `heading` (None where the picture has no data under it)."""
    cell = shape.cell
    rows, cols = shape.rows + 2, shape.cols + 2
    r = math.radians(heading)
    c, s = math.cos(r), math.sin(r)
    v = (np.arange(rows * cell, dtype=np.float64) - (rows * cell - 1) / 2)[:, None]
    u = (np.arange(cols * cell, dtype=np.float64) - (cols * cell - 1) / 2)[None, :]
    patch, ok = _bilinear(gray, valid, x + u * c - v * s, y + u * s + v * c)
    f, has = cells(patch, ok, cell)
    if not has[1:-1, 1:-1].all():
        return None
    return f[1:-1, 1:-1].astype(np.float64).ravel()


def _windows(f: np.ndarray, has: np.ndarray, shape: Shape, n: int, rng) -> np.ndarray:
    """Up to n windows of cells (all with data) at random places."""
    rows, cols = f.shape[:2]
    if rows < shape.rows or cols < shape.cols:
        return np.zeros((0, shape.size))
    full = _covered(has, shape) >= 1.0
    ys, xs = np.nonzero(full)
    if len(ys) == 0:
        return np.zeros((0, shape.size))
    pick = rng.choice(len(ys), size=min(n, len(ys)), replace=False)
    return np.stack([f[ys[i]:ys[i] + shape.rows, xs[i]:xs[i] + shape.cols].ravel() for i in pick]).astype(np.float64)


def _covered(has: np.ndarray, shape: Shape) -> np.ndarray:
    """The share of cells with data of the window with its top left at each place it fits."""
    c = np.pad(np.cumsum(np.cumsum(has.astype(np.float64), 0), 1), ((1, 0), (1, 0)))
    r, k = shape.rows, shape.cols
    total = c[r:, k:] - c[:-r, k:] - c[r:, :-k] + c[:-r, :-k]
    return total / (r * k)


class Ground:
    """What the searched picture's ground looks like (the mean and covariance of its windows)."""

    def __init__(self, samples: np.ndarray, size: int):
        if len(samples) >= 2:
            self.mean = samples.mean(0)
            self.cov = np.cov(samples, rowvar=False)
        else:
            self.mean = samples.mean(0) if len(samples) else np.zeros(size)
            self.cov = np.eye(size) * 0.01


class Model:
    """The weights a window is scored with, from the 見本 (one or several), the ground and the places recorded as wrong."""

    def __init__(self, ground: Ground, alike: Sequence[np.ndarray], wrong: Sequence[np.ndarray] = ()):
        mean_alike = np.mean(alike, axis=0)
        mean, cov = ground.mean, ground.cov
        if len(wrong):
            n = len(wrong)
            share = WRONG_WEIGHT * n / (n + 5)
            w = np.asarray(wrong)
            mean = (1 - share) * mean + share * w.mean(0)
            if n >= 5:
                cov = (1 - share) * cov + share * np.cov(w, rowvar=False)
        size = len(mean)
        evened = cov + RIDGE * float(np.mean(np.diag(cov))) * np.eye(size)
        self.weights = np.linalg.solve(evened, mean_alike - mean)
        # 0 for the ground on average, 1 for its spread (one standard deviation).
        self.bias = float(ground.mean @ self.weights)
        spread = math.sqrt(max(float(self.weights @ ground.cov @ self.weights), 0.0))
        self.scale = spread if spread > 1e-12 else 1.0

    def score(self, window: np.ndarray) -> float:
        """How far a window stands out from the ground towards the 見本 (the ground's standard deviations)."""
        return (float(window @ self.weights) - self.bias) / self.scale

    def scores(self, f: np.ndarray, has: np.ndarray, shape: Shape) -> np.ndarray:
        """`score` of the window with its top left at each place it fits (−inf where too little has data)."""
        rows, cols = f.shape[0] - shape.rows + 1, f.shape[1] - shape.cols + 1
        if rows <= 0 or cols <= 0:
            return np.full((max(rows, 0), max(cols, 0)), -np.inf)
        w = self.weights.reshape(shape.rows, shape.cols, BINS).astype(np.float32)
        out = np.zeros((rows, cols), np.float32)
        for i in range(shape.rows):
            for j in range(shape.cols):
                out += f[i:i + rows, j:j + cols] @ w[i, j]
        s = (out.astype(np.float64) - self.bias) / self.scale
        return np.where(_covered(has, shape) >= COVERED, s, -np.inf)


def confidence(z: float) -> float:
    """A window `z` deviations out of the ground as a score 0…1."""
    return 1.0 / (1.0 + math.exp(-max(-50.0, min(50.0, (z - Z_MID) / Z_SPREAD))))


def _peaks(score: np.ndarray, threshold: float) -> tuple[np.ndarray, np.ndarray]:
    """The places at least `threshold` and not under a neighbour."""
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
    return np.nonzero(peak)


def _between(lo: float, mid: float, hi: float) -> float:
    """Where between its neighbours a peak lies (−0.5…0.5), by a parabola through the three."""
    if not (math.isfinite(lo) and math.isfinite(hi)):
        return 0.0
    bend = lo - 2 * mid + hi
    return float(min(0.5, max(-0.5, 0.5 * (lo - hi) / bend))) if bend < -1e-12 else 0.0


def find_similar(search_gray: np.ndarray, search_valid: np.ndarray, sample_gray: np.ndarray, sample_valid: np.ndarray,
                 sx: float, sy: float, heading: float, length_px: float, width_px: Optional[float],
                 angles: Sequence[float] = ANGLES_ALL, threshold: float = 0.5, taken: Sequence[tuple] = (),
                 most: int = MOST, progress: Optional[Callable[[float], None]] = None,
                 cancelled: Optional[Callable[[], bool]] = None,
                 alike: Sequence[tuple[float, float, float]] = (), wrong: Sequence[tuple[float, float]] = (),
                 seed: int = 0) -> list[Found]:
    """
    The places of the searched picture looking like the 見本 (at sx, sy of its
    own picture, drawn at the same scale), best first. `taken`: places of the
    searched picture already recorded (the 見本 itself among them), not found
    again: (x, y) or (x, y, heading). `alike`: more 見本 in the searched picture (x, y, heading);
    `wrong`: places in it recorded as not the thing (x, y). `progress` gets
    0…1, `cancelled` stops the work (Cancelled).
    """
    if not (length_px > 0):
        return []
    shape = Shape(length_px, width_px)
    first = window_at(sample_gray, sample_valid, sx, sy, heading, shape)
    if first is None:
        return []
    examples = [first]
    for x, y, h in alike:
        w = window_at(search_gray, search_valid, x, y, h, shape)
        if w is not None:
            examples.append(w)
    # The turns asked for, by what is left under a quarter turn (each drawn once).
    turns = sorted({float(a) % 360 for a in angles})
    groups: dict[float, list[int]] = {}
    for t in turns:
        base = round(t % 90, 6)
        groups.setdefault(base, []).append(int(round((t - base) / 90)) % 4)
    h, w = search_gray.shape
    size = int(math.ceil((math.hypot(h, w) + 2) / shape.cell)) * shape.cell
    rng = np.random.default_rng(seed)
    drawn: dict[float, tuple[np.ndarray, np.ndarray]] = {}

    def cells_at(base: float) -> tuple[np.ndarray, np.ndarray]:
        if base not in drawn:
            drawn[base] = cells(*_Turn(search_gray.shape, heading + base, size).draw(search_gray, search_valid), shape.cell)
        return drawn[base]

    # The ground: windows of the picture turned every way.
    each = max(1, GROUND_WINDOWS // (4 * len(GROUND_TURNS)))
    ground = Ground(np.concatenate([_windows(*_quarter(*cells_at(g), k), shape, each, rng)
                                    for g in GROUND_TURNS for k in range(4)]), shape.size)
    for g in GROUND_TURNS:
        if g not in groups:
            drawn.pop(g, None)
    model = Model(ground, examples)
    if wrong:
        # Each wrong place as it looks most like the 見本 (the turn the search would take it at).
        hard = []
        for x, y in wrong:
            windows = [window_at(search_gray, search_valid, x, y, heading + t, shape) for t in turns]
            windows = [v for v in windows if v is not None]
            if windows:
                hard.append(max(windows, key=model.score))
        if hard:
            model = Model(ground, examples, hard)
    least = Z_MID + Z_SPREAD * math.log(threshold / (1 - threshold)) if 0 < threshold < 1 else (-math.inf if threshold <= 0 else math.inf)
    candidates: list[tuple[float, float, float, float]] = []
    done = 0
    for base, quarters in sorted(groups.items(), key=lambda g: g[0]):
        if cancelled is not None and cancelled():
            raise Cancelled()
        f0, has0 = cells_at(base)
        drawn.pop(base, None)
        for k in quarters:
            if cancelled is not None and cancelled():
                raise Cancelled()
            f, has = _quarter(f0, has0, k)
            s = model.scores(f, has, shape)
            hh = (heading + base + 90 * k) % 360
            at = _Turn(search_gray.shape, hh, size)
            ys, xs = _peaks(s, least)
            for y, x in zip(ys.tolist(), xs.tolist()):
                dy = _between(s[y - 1, x] if y > 0 else -np.inf, s[y, x], s[y + 1, x] if y + 1 < s.shape[0] else -np.inf)
                dx = _between(s[y, x - 1] if x > 0 else -np.inf, s[y, x], s[y, x + 1] if x + 1 < s.shape[1] else -np.inf)
                cx = (x + dx + shape.cols / 2) * shape.cell - 0.5
                cy = (y + dy + shape.rows / 2) * shape.cell - 0.5
                px, py = at.to_picture(cx, cy)
                if 0 <= px <= w - 1 and 0 <= py <= h - 1:
                    candidates.append((float(s[y, x]), px, py, hh))
            done += 1
            if progress is not None:
                progress(done / len(turns))
    candidates.sort(key=lambda c: -c[0])
    return _apart(candidates, length_px, width_px, taken, most)


def _apart(candidates: Sequence[tuple[float, float, float, float]], length_px: float, width_px: Optional[float],
           taken: Sequence[tuple], most: int) -> list[Found]:
    """
    The candidates (z, x, y, heading; best first) one by one, each left out
    when a place taken (a record or one checked as wrong) or one kept before
    lies inside it (its outline with the margin, `SAME_THING` of it), or when
    it lies in the gap between two of them side by side or end to end, nearer
    to each than a thing of the 見本's size could be (`GAP`): the gap between
    two light cars has a dark car's edges. A window lying partly over one
    only is still a thing of its own (tried: leaving those out lost moored
    boats).
    """
    half_l, half_w, _ = half_sizes(length_px, width_px)
    reach_l, reach_w = half_l * SAME_THING, half_w * SAME_THING
    length = length_px
    width = width_px if width_px and width_px > 0 else length_px
    xs: list[float] = [float(p[0]) for p in taken]
    ys: list[float] = [float(p[1]) for p in taken]
    found: list[Found] = []
    for z, cx, cy, hh in candidates:
        if xs:
            r = math.radians(hh)
            dx = np.asarray(xs) - cx
            dy = np.asarray(ys) - cy
            v = dx * math.sin(r) - dy * math.cos(r)
            u = dx * math.cos(r) + dy * math.sin(r)
            if bool(np.any((np.abs(v) <= reach_l) & (np.abs(u) <= reach_w))):
                continue
            beside = (np.abs(u) <= GAP * width) & (np.abs(v) <= 0.5 * length)
            ahead = (np.abs(v) <= GAP * length) & (np.abs(u) <= 0.5 * width)
            if (bool(np.any(beside & (u < 0))) and bool(np.any(beside & (u > 0)))) or \
                    (bool(np.any(ahead & (v < 0))) and bool(np.any(ahead & (v > 0)))):
                continue
        found.append(Found(cx, cy, hh, confidence(z)))
        xs.append(cx)
        ys.append(cy)
        if len(found) >= most:
            break
    return found
