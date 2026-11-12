"""
How well a detector does on labelled windows: average precision per class
(area under the precision–recall curve, all points, as COCO / VOC), its
mean over the classes at IoU 0.5 (mAP50) and over IoU 0.5–0.95 (mAP50-95).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Sequence

import numpy as np

#: The IoU thresholds of mAP50-95.
THRESHOLDS = tuple(np.round(np.arange(0.5, 0.96, 0.05), 2))


def box_iou(a: np.ndarray, b: np.ndarray) -> np.ndarray:
    """IoU of every box of `a` with every box of `b` (rows of centre x, centre y, width, height)."""
    if not len(a) or not len(b):
        return np.zeros((len(a), len(b)))
    ax1, ay1, ax2, ay2 = a[:, 0] - a[:, 2] / 2, a[:, 1] - a[:, 3] / 2, a[:, 0] + a[:, 2] / 2, a[:, 1] + a[:, 3] / 2
    bx1, by1, bx2, by2 = b[:, 0] - b[:, 2] / 2, b[:, 1] - b[:, 3] / 2, b[:, 0] + b[:, 2] / 2, b[:, 1] + b[:, 3] / 2
    w = np.clip(np.minimum(ax2[:, None], bx2) - np.maximum(ax1[:, None], bx1), 0, None)
    h = np.clip(np.minimum(ay2[:, None], by2) - np.maximum(ay1[:, None], by1), 0, None)
    inter = w * h
    return inter / (a[:, 2:3] * a[:, 3:4] + (b[:, 2] * b[:, 3])[None] - inter + 1e-9)


def average_precision(recall: np.ndarray, precision: np.ndarray) -> float:
    """The area under a precision–recall curve (precision made non-increasing)."""
    r = np.concatenate([[0.0], recall, [1.0]])
    p = np.concatenate([[1.0], precision, [0.0]])
    p = np.maximum.accumulate(p[::-1])[::-1]
    i = np.nonzero(r[1:] != r[:-1])[0]
    return float(((r[i + 1] - r[i]) * p[i + 1]).sum())


@dataclass
class Scores:
    """mAP50, mAP50-95 and AP50 by class; precision and recall at a score threshold."""
    map50: float
    map: float
    per_class: dict = field(default_factory=dict)
    precision: float = 0.0
    recall: float = 0.0
    objects: int = 0

    def to_json(self) -> dict:
        return {"mAP50": round(self.map50, 3), "mAP50-95": round(self.map, 3), "precision": round(self.precision, 3),
                "recall": round(self.recall, 3), "objects": self.objects,
                "AP50": {k: round(v, 3) for k, v in self.per_class.items()}}


def evaluate(found: Sequence[np.ndarray], truth: Sequence[np.ndarray], classes: Sequence[str], at: float = 0.25) -> Scores:
    """
    Scores of detections against labels, image by image: `found` rows of
    centre x, centre y, width, height, class, score; `truth` rows of centre
    x, centre y, width, height, class. Precision and recall are those of
    the detections scoring at least `at`, at IoU 0.5.
    """
    aps = np.zeros((len(classes), len(THRESHOLDS)))
    present = np.zeros(len(classes), bool)
    hits_at = total_at = objects = 0
    for c in range(len(classes)):
        records = []  # (score, hit at each threshold)
        count = 0
        for f, t in zip(found, truth):
            f = f[f[:, 4] == c] if len(f) else f.reshape(0, 6)
            t = t[t[:, 4] == c] if len(t) else t.reshape(0, 5)
            count += len(t)
            order = np.argsort(-f[:, 5]) if len(f) else []
            f = f[order] if len(f) else f
            iou = box_iou(f[:, :4], t[:, :4])
            for k, threshold in enumerate(THRESHOLDS):
                used = np.zeros(len(t), bool)
                for i in range(len(f)):
                    hit = False
                    if len(t):
                        candidates = np.where(~used & (iou[i] >= threshold), iou[i], -1)
                        j = int(np.argmax(candidates))
                        if candidates[j] >= 0:
                            used[j] = hit = True
                    if k == 0:
                        records.append([f[i, 5], hit])
                        if f[i, 5] >= at:
                            total_at += 1
                            hits_at += hit
                    else:
                        records[len(records) - len(f) + i].append(hit)
        objects += count
        if not count:
            continue
        present[c] = True
        if not records:
            continue
        r = np.array(records, dtype=float)
        r = r[np.argsort(-r[:, 0], kind="stable")]
        for k in range(len(THRESHOLDS)):
            tp = np.cumsum(r[:, 1 + k])
            fp = np.cumsum(1 - r[:, 1 + k])
            aps[c, k] = average_precision(tp / count, tp / np.maximum(tp + fp, 1e-9))
    used = aps[present]
    return Scores(
        map50=float(used[:, 0].mean()) if len(used) else 0.0, map=float(used.mean()) if len(used) else 0.0,
        per_class={classes[c]: float(aps[c, 0]) for c in range(len(classes)) if present[c]},
        precision=hits_at / total_at if total_at else 0.0, recall=hits_at / objects if objects else 0.0, objects=objects)
