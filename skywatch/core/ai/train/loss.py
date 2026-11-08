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


# The residual network's loss: boxes as distances from the cell (FCOS), GIoU, and class scores that learn the box's IoU.

#: Positives: cells whose centre is in the box and within this many cells of its centre (each way), at least the centre cell.
CENTRE_RADIUS = 2.5
GIOU_WEIGHT = 2.0
QFL_BETA = 2.0


@dataclass
class BoxTargets:
    """A batch's targets for the residual network."""
    #: (N, h, w): a positive cell.
    positive: np.ndarray
    #: (N, h, w): the class of a positive cell.
    cls: np.ndarray
    #: (N, h, w, 4): the box's distances (left, top, right, bottom) from the cell centre, in cells.
    ltrb: np.ndarray


def tile_box_targets(boxes: np.ndarray, height: int, width: int) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """One tile's positives, classes and distances for its boxes (centre x, centre y, width, height in pixels, class)."""
    positive = np.zeros((height, width), bool)
    cls = np.zeros((height, width), np.int64)
    ltrb = np.zeros((height, width, 4), np.float32)
    area = np.full((height, width), np.inf, np.float32)
    gy, gx = np.mgrid[0:height, 0:width].astype(np.float32) + 0.5
    for cx, cy, bw, bh, c in boxes:
        if not (bw > 0 and bh > 0):
            continue
        x1, y1, x2, y2 = (cx - bw / 2) / STRIDE, (cy - bh / 2) / STRIDE, (cx + bw / 2) / STRIDE, (cy + bh / 2) / STRIDE
        ccx, ccy = cx / STRIDE, cy / STRIDE
        inside = (gx > x1) & (gx < x2) & (gy > y1) & (gy < y2)
        near = (np.abs(gx - ccx) <= CENTRE_RADIUS) & (np.abs(gy - ccy) <= CENTRE_RADIUS)
        take = inside & near
        ix, iy = min(width - 1, max(0, int(ccx))), min(height - 1, max(0, int(ccy)))
        take[iy, ix] = True
        a = bw * bh
        take &= a < area
        area[take] = a
        positive |= take
        cls[take] = int(c)
        # The centre cell of a box thinner than a cell may lie outside it: distances kept above a twentieth of a cell.
        ltrb[take] = np.stack([np.maximum(gx - x1, 0.05), np.maximum(gy - y1, 0.05), np.maximum(x2 - gx, 0.05), np.maximum(y2 - gy, 0.05)], -1)[take]
    return positive, cls, ltrb


def batch_box_targets(boxes: list[np.ndarray], height: int, width: int) -> BoxTargets:
    parts = [tile_box_targets(b, height, width) for b in boxes]
    return BoxTargets(*(np.stack([p[i] for p in parts]) for i in range(3)))


def giou(pred: np.ndarray, truth: np.ndarray) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """
    IoU and GIoU of boxes given by distances (left, top, right, bottom) from
    the same point, and the gradient of GIoU with respect to the predicted
    distances.
    """
    l, t, r, b = (pred[:, i] for i in range(4))
    lg, tg, rg, bg = (truth[:, i] for i in range(4))
    ap = (l + r) * (t + b)
    ag = (lg + rg) * (tg + bg)
    wi = np.minimum(l, lg) + np.minimum(r, rg)
    hi = np.minimum(t, tg) + np.minimum(b, bg)
    inter = wi * hi
    union = ap + ag - inter + 1e-9
    wc = np.maximum(l, lg) + np.maximum(r, rg)
    hc = np.maximum(t, tg) + np.maximum(b, bg)
    c = wc * hc + 1e-9
    iou = inter / union
    g = iou - (c - union) / c
    grads = np.zeros_like(pred)
    for i, (mine, theirs, wi_or_hi, wc_or_hc, across) in enumerate((
            (l, lg, hi, hc, t + b), (t, tg, wi, wc, l + r), (r, rg, hi, hc, t + b), (b, bg, wi, wc, l + r))):
        d_inter = np.where(mine < theirs, wi_or_hi, 0.0)
        d_union = across - d_inter
        d_c = np.where(mine > theirs, wc_or_hc, 0.0)
        d_iou = (d_inter * union - inter * d_union) / (union * union)
        grads[:, i] = d_iou + (d_union * c - union * d_c) / (c * c)
    return iou, g, grads


def quality_focal(logits: np.ndarray, target: np.ndarray) -> tuple[float, np.ndarray]:
    """The quality focal loss (GFL): BCE towards a soft target (the IoU at positives, 0 elsewhere) scaled by |target − score|^β."""
    z = np.clip(logits, -30, 30)
    p = 1 / (1 + np.exp(-z))
    p = np.clip(p, 1e-6, 1 - 1e-6)
    diff = p - target
    m = np.abs(diff) ** QFL_BETA
    bce = -(target * np.log(p) + (1 - target) * np.log(1 - p))
    grad = m * diff + bce * QFL_BETA * np.abs(diff) ** (QFL_BETA - 1) * np.sign(diff) * p * (1 - p)
    return float((m * bce).sum()), grad.astype(np.float32)


def box_loss(heads: dict[str, np.ndarray], t: BoxTargets) -> tuple[dict[str, float], dict[str, np.ndarray]]:
    """The residual network's losses (class, box, total; per positive cell) and the heads' gradients."""
    count = max(1.0, float(t.positive.sum()))
    box = heads["box"]
    pos = t.positive
    grad_box = np.zeros_like(box)
    target = np.zeros_like(heads["cls"])
    box_total = 0.0
    if pos.any():
        z = np.clip(box[pos], -10, 10)
        d = np.exp(z)
        iou, g, dg = giou(d.astype(np.float64), t.ltrb[pos].astype(np.float64))
        box_total = float((1 - g).sum())
        grad_box[pos] = (-dg * d * GIOU_WEIGHT / count).astype(np.float32)
        n, y, x = np.nonzero(pos)
        target[n, y, x, t.cls[pos]] = np.clip(iou, 0, 1).astype(np.float32)
    cls_total, grad_cls = quality_focal(heads["cls"], target)
    losses = {"cls": cls_total / count, "box": box_total * GIOU_WEIGHT / count}
    losses["total"] = losses["cls"] + losses["box"]
    return losses, {"cls": grad_cls / count, "box": grad_box}
