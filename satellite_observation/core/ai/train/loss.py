"""
What the network should give for a tile's objects, and how far it is from
it (as FCOS / CenterNet): the cells near an object's centre are positives
of its class and regress its centre and size; every other cell is
background. The class scores are scored with the focal loss, so the many
easy background cells do not drown the few objects.
"""
from __future__ import annotations

import math
from dataclasses import dataclass

import numpy as np

from .net import STRIDE

ALPHA = 0.25
GAMMA = 2.0
#: Weight of the box regression against the class scores.
BOX_WEIGHT = 1.0


@dataclass
class Targets:
    """A batch's targets on the output grid."""
    #: (N, h, w, classes): 1 at positives of a class.
    cls: np.ndarray
    #: (N, h, w): a positive cell.
    positive: np.ndarray
    #: (N, h, w, 2): the centre's offset from the cell centre, in cells.
    off: np.ndarray
    #: (N, h, w, 2): log of the width and height in cells.
    size: np.ndarray


def tile_targets(boxes: np.ndarray, height: int, width: int, classes: int) -> tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray]:
    """
    The targets of one tile (`height` × `width` cells) for its `boxes` (rows
    of centre x, centre y, width, height in pixels, class). A cell is a
    positive of a box when its centre lies within the box and within about a
    quarter of the box (at least a cell) from the box's centre; the cell
    holding the centre always is. Where boxes overlap the smaller wins.
    """
    cls = np.zeros((height, width, classes), np.float32)
    positive = np.zeros((height, width), bool)
    off = np.zeros((height, width, 2), np.float32)
    size = np.zeros((height, width, 2), np.float32)
    area = np.full((height, width), np.inf, np.float32)
    for cx, cy, bw, bh, c in boxes:
        c = int(c)
        if not (bw > 0 and bh > 0) or not 0 <= c < classes:
            continue
        gx, gy = cx / STRIDE, cy / STRIDE
        rx = max(1.0, 0.25 * bw / STRIDE)
        ry = max(1.0, 0.25 * bh / STRIDE)
        hx, hy = max(0.5, bw / STRIDE / 2), max(0.5, bh / STRIDE / 2)
        x0, x1 = max(0, math.floor(gx - rx)), min(width - 1, math.ceil(gx + rx))
        y0, y1 = max(0, math.floor(gy - ry)), min(height - 1, math.ceil(gy + ry))
        a = bw * bh
        for y in range(y0, y1 + 1):
            for x in range(x0, x1 + 1):
                dx, dy = gx - (x + 0.5), gy - (y + 0.5)
                centre = math.floor(gx) == x and math.floor(gy) == y
                if not centre and (abs(dx) > min(rx, hx) or abs(dy) > min(ry, hy)):
                    continue
                if a >= area[y, x]:
                    continue
                area[y, x] = a
                positive[y, x] = True
                cls[y, x] = 0
                cls[y, x, c] = 1
                off[y, x] = (dx, dy)
                size[y, x] = (math.log(bw / STRIDE), math.log(bh / STRIDE))
    return cls, positive, off, size


def batch_targets(boxes: list[np.ndarray], height: int, width: int, classes: int) -> Targets:
    parts = [tile_targets(b, height, width, classes) for b in boxes]
    return Targets(*(np.stack([p[i] for p in parts]) for i in range(4)))


def focal(logits: np.ndarray, target: np.ndarray) -> tuple[float, np.ndarray]:
    """The focal loss (summed) of sigmoid scores and its gradient."""
    z = np.clip(logits, -30, 30)
    p = 1 / (1 + np.exp(-z))
    p = np.clip(p, 1e-6, 1 - 1e-6)
    pos = target > 0.5
    loss = np.where(pos, -ALPHA * (1 - p) ** GAMMA * np.log(p), -(1 - ALPHA) * p ** GAMMA * np.log(1 - p))
    grad = np.where(pos, ALPHA * (1 - p) ** GAMMA * (GAMMA * p * np.log(p) - (1 - p)),
                    (1 - ALPHA) * p ** GAMMA * (p - GAMMA * (1 - p) * np.log(1 - p)))
    return float(loss.sum()), grad.astype(np.float32)


def smooth_l1(diff: np.ndarray, beta: float = 0.1) -> tuple[np.ndarray, np.ndarray]:
    a = np.abs(diff)
    loss = np.where(a < beta, 0.5 * diff * diff / beta, a - 0.5 * beta)
    grad = np.where(a < beta, diff / beta, np.sign(diff))
    return loss, grad


def detection_loss(heads: dict[str, np.ndarray], t: Targets) -> tuple[dict[str, float], dict[str, np.ndarray]]:
    """The losses (class, box, total; per object) and the heads' gradients."""
    count = max(1.0, float(t.positive.sum()))
    cls_loss, g_cls = focal(heads["cls"], t.cls)
    mask = t.positive[..., None].astype(np.float32)
    off_loss, g_off = smooth_l1(heads["off"] - t.off)
    size_loss, g_size = smooth_l1(heads["size"] - t.size)
    box_loss = float((off_loss * mask).sum() + (size_loss * mask).sum())
    grads = {
        "cls": g_cls / count,
        "off": (g_off * mask * BOX_WEIGHT / count).astype(np.float32),
        "size": (g_size * mask * BOX_WEIGHT / count).astype(np.float32),
    }
    losses = {"cls": cls_loss / count, "box": box_loss * BOX_WEIGHT / count}
    losses["total"] = losses["cls"] + losses["box"]
    return losses, grads
