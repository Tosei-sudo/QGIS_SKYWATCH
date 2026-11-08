"""
Segment Anything (SAM and its small relatives) on a picture, as the viewer
runs it (ai/sam.ts): the encoder sees the picture once, scaled so its
longer side is the input side (1024) and padded; each click then only runs
the light decoder with the clicked points, and the best of its masks
becomes a polygon.

The decoder is the one segment-anything's `export_onnx_model.py` writes:
inputs `image_embeddings`, `point_coords`, `point_labels`, `mask_input`,
`has_mask_input`, `orig_im_size`; outputs `masks` (logits, the picture's
size, or a smaller low-resolution mask) and `iou_predictions`.

Everything here is NumPy only.
"""
from __future__ import annotations

from collections import deque
from dataclasses import dataclass
from typing import Optional, Sequence

import numpy as np

#: SAM's normalization (ImageNet, in 0–255).
SAM_MEAN = (123.675, 116.28, 103.53)
SAM_STD = (58.395, 57.12, 57.375)


def sam_scale(width: int, height: int, side: int = 1024) -> tuple[float, int, int]:
    """How a picture is scaled into the encoder's input: the scale, and the scaled width and height."""
    scale = side / max(width, height)
    return scale, round(width * scale), round(height * scale)


def sam_tensor(rgba: np.ndarray, side: int = 1024, mean: Sequence[float] = SAM_MEAN, std: Sequence[float] = SAM_STD) -> np.ndarray:
    """
    The encoder input (CHW float32, `side` × `side`) from the scaled picture
    (RGBA, height × width × 4, at its top left): normalized, and zero (the
    mean) in the padding and under transparent pixels.
    """
    out = np.zeros((side, side, 3), np.float32)
    part = rgba[:side, :side]
    h, w = part.shape[:2]
    values = (part[..., :3].astype(np.float32) - np.asarray(mean, np.float32)) / np.asarray(std, np.float32)
    values[part[..., 3] == 0] = 0
    out[:h, :w] = values
    return np.ascontiguousarray(out.transpose(2, 0, 1))


def sam_hwc(rgba: np.ndarray) -> np.ndarray:
    """For an encoder with its preprocessing inside: the scaled picture as it is, height × width × RGB (float32, 0–255)."""
    return np.ascontiguousarray(rgba[..., :3].astype(np.float32))


@dataclass
class SamPoint:
    """A clicked point on the picture (pixels): `positive` to take it in, otherwise to leave it out."""
    x: float
    y: float
    positive: bool = True


def sam_prompt(points: Sequence[SamPoint], scale: float) -> tuple[np.ndarray, np.ndarray]:
    """
    The decoder's prompt tensors for `points` on a picture scaled by `scale`
    (`[1, n + 1, 2]` and `[1, n + 1]`): the points in the encoder's input
    pixels, then the padding point (0, 0) labelled −1 that stands for "no box".
    """
    count = len(points) + 1
    coords = np.zeros((1, count, 2), np.float32)
    labels = np.zeros((1, count), np.float32)
    for i, p in enumerate(points):
        coords[0, i] = (p.x * scale, p.y * scale)
        labels[0, i] = 1 if p.positive else 0
    labels[0, len(points)] = -1
    return coords, labels


def sam_mask(masks: np.ndarray, scores: Optional[np.ndarray], width: int, height: int, scale: float, side: int = 1024) -> np.ndarray:
    """
    The picture-sized mask (height × width, 1 = the object) from the decoder's
    `masks` output (`[1, k, h, w]` logits) and `iou_predictions` (`[1, k]`):
    the mask the decoder rates best. A mask of the picture's own size is used
    as it is; a smaller (low-resolution) one covers the padded encoder
    input, and is sampled bilinearly.
    """
    masks = np.asarray(masks, np.float32)
    k, mh, mw = masks.shape[-3:]
    masks = masks.reshape(k, mh, mw)
    best = int(np.argmax(np.asarray(scores, np.float32).reshape(-1)[:k])) if scores is not None and k > 1 else 0
    data = masks[best]
    if mw == width and mh == height:
        return (data > 0).astype(np.uint8)
    # The low-resolution mask covers the side × side input; a picture pixel's centre is at (x + ½)·scale there.
    sx, sy = scale * mw / side, scale * mh / side
    fy = np.clip((np.arange(height) + 0.5) * sy - 0.5, 0, mh - 1)
    fx = np.clip((np.arange(width) + 0.5) * sx - 0.5, 0, mw - 1)
    ya = np.floor(fy).astype(int)
    yb = np.minimum(ya + 1, mh - 1)
    ty = (fy - ya)[:, None]
    xa = np.floor(fx).astype(int)
    xb = np.minimum(xa + 1, mw - 1)
    tx = (fx - xa)[None, :]
    v = (data[ya][:, xa] * (1 - tx) + data[ya][:, xb] * tx) * (1 - ty) + (data[yb][:, xa] * (1 - tx) + data[yb][:, xb] * tx) * ty
    return (v > 0).astype(np.uint8)


def part_at(mask: np.ndarray, x: float, y: float) -> np.ndarray:
    """The connected part (4-neighbours) of `mask` holding (`x`, `y`); the whole mask when that pixel is outside it."""
    height, width = mask.shape
    if x < 0 or y < 0 or x >= width or y >= height or not mask[int(y), int(x)]:
        return mask
    try:
        from scipy import ndimage  # quicker, when QGIS has SciPy

        labels, _ = ndimage.label(mask != 0)
        return (labels == labels[int(y), int(x)]).astype(np.uint8)
    except ImportError:
        pass
    out = np.zeros_like(mask, np.uint8)
    on = mask != 0
    todo = deque([(int(y), int(x))])
    out[int(y), int(x)] = 1
    while todo:
        py, px = todo.pop()
        for qy, qx in ((py, px - 1), (py, px + 1), (py - 1, px), (py + 1, px)):
            if 0 <= qy < height and 0 <= qx < width and on[qy, qx] and not out[qy, qx]:
                out[qy, qx] = 1
                todo.append((qy, qx))
    return out
