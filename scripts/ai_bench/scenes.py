"""
Synthetic satellite-like scenes to measure the trained detectors: textured
land and sea, ships (thin rotated hulls with a wake), aircraft (crosses),
vehicles (small boxes), and unlabelled bright blobs as distractors.
"""
from __future__ import annotations

import math

import numpy as np

from satellite_observation.core.ai.train.trainer import Window

CLASSES = ["ship", "aircraft", "vehicle"]


def _smooth(rng, size, cells):
    small = rng.normal(0, 1, (cells, cells))
    reps = math.ceil(size / cells)
    big = np.kron(small, np.ones((reps, reps)))[:size, :size]
    # A box blur to soften the cells' edges.
    k = max(1, reps // 2)
    c = np.cumsum(np.cumsum(np.pad(big, k, mode="edge"), 0), 1)
    return (c[2 * k:, 2 * k:] - c[:-2 * k, 2 * k:] - c[2 * k:, :-2 * k] + c[:-2 * k, :-2 * k])[:size, :size] / (2 * k) ** 2


def _rect(cx, cy, length, width, angle):
    ux, uy = math.cos(angle), math.sin(angle)
    vx, vy = -uy, ux
    return [(cx + ux * a * length / 2 + vx * b * width / 2, cy + uy * a * length / 2 + vy * b * width / 2)
            for a, b in ((-1, -1), (1, -1), (1, 1), (-1, 1))]


def _fill(img, corners, value, size):
    xs = [p[0] for p in corners]
    ys = [p[1] for p in corners]
    x0, x1 = max(0, int(min(xs))), min(size, int(math.ceil(max(xs))) + 1)
    y0, y1 = max(0, int(min(ys))), min(size, int(math.ceil(max(ys))) + 1)
    if x1 <= x0 or y1 <= y0:
        return
    yy, xx = np.mgrid[y0:y1, x0:x1] + 0.5
    inside = np.ones(yy.shape, bool)
    for i in range(4):
        (ax, ay), (bx, by) = corners[i], corners[(i + 1) % 4]
        inside &= (bx - ax) * (yy - ay) - (by - ay) * (xx - ax) >= 0
    if not inside.any():
        inside = np.ones(yy.shape, bool)
        for i in range(4):
            (ax, ay), (bx, by) = corners[i], corners[(i + 1) % 4]
            inside &= (bx - ax) * (yy - ay) - (by - ay) * (xx - ax) <= 0
    img[y0:y1, x0:x1][inside] = value


def scene(rng: np.random.Generator, size: int = 320, objects: int = 18) -> Window:
    land = _smooth(rng, size, 6) > 0.2
    img = np.zeros((size, size, 3))
    texture = _smooth(rng, size, 40)
    img[land] = np.array([95, 100, 80]) + 18 * texture[land, None]
    img[~land] = np.array([35, 55, 75]) + 6 * texture[~land, None]
    boxes = []
    taken = []
    tries = 0
    while len(boxes) < objects and tries < objects * 30:
        tries += 1
        c = int(rng.choice([0, 1, 2, 3]))  # 3: distractor
        for _ in range(50):
            cx, cy = rng.uniform(24, size - 24, 2)
            wanted_land = c in (1, 2) or (c == 3 and rng.random() < 0.5)
            if land[int(cy), int(cx)] == wanted_land and not any(math.hypot(cx - p, cy - q) < 26 for p, q in taken):
                break
        else:
            continue
        a = rng.uniform(0, math.pi)
        tone = rng.uniform(120, 240)
        if c == 0:
            length = rng.uniform(14, 42)
            width = length * rng.uniform(0.18, 0.28)
            wake = _rect(cx - math.cos(a) * length * 0.9, cy - math.sin(a) * length * 0.9, length * 0.9, width * 1.6, a)
            _fill(img, wake, img[int(cy), int(cx)] + 25, size)
            corners = _rect(cx, cy, length, width, a)
            _fill(img, corners, (tone, tone, tone * 0.95), size)
        elif c == 1:
            span = rng.uniform(14, 30)
            body = _rect(cx, cy, span, span * 0.16, a)
            wing = _rect(cx + math.cos(a) * span * 0.05, cy + math.sin(a) * span * 0.05, span * 0.9, span * 0.14, a + math.pi / 2)
            _fill(img, body, (tone, tone, tone), size)
            _fill(img, wing, (tone, tone, tone), size)
            corners = body + wing
        elif c == 2:
            length = rng.uniform(5, 9)
            corners = _rect(cx, cy, length, length * 0.5, a)
            col = rng.uniform(120, 230, 3)
            _fill(img, corners, col, size)
        else:
            r = rng.uniform(3, 9)
            yy, xx = np.mgrid[0:size, 0:size]
            img[(xx - cx) ** 2 + (yy - cy) ** 2 < r * r] = tone
            taken.append((cx, cy))
            continue
        xs = [p[0] for p in corners]
        ys = [p[1] for p in corners]
        x1, x2, y1, y2 = max(0, min(xs)), min(size, max(xs)), max(0, min(ys)), min(size, max(ys))
        boxes.append(((x1 + x2) / 2, (y1 + y2) / 2, x2 - x1, y2 - y1, c))
        taken.append((cx, cy))
    # Sensor noise, a little blur and an overall brightness.
    img = img * rng.uniform(0.8, 1.2) + rng.normal(0, 7, img.shape)
    img = (img + np.roll(img, 1, 0) + np.roll(img, 1, 1) + np.roll(np.roll(img, 1, 0), 1, 1)) / 4
    rgba = np.concatenate([img.clip(0, 255).astype(np.uint8), np.full((size, size, 1), 255, np.uint8)], axis=2)
    return Window(rgba, np.array(boxes, np.float32).reshape(-1, 5))


def dataset(seed: int, count: int, size: int = 320) -> list[Window]:
    rng = np.random.default_rng(seed)
    return [scene(rng, size) for _ in range(count)]
