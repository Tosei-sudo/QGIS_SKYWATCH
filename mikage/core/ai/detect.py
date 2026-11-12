"""
Object detection over a large picture with a YOLO (or Detectron2) model, as
the viewer does it (ai/detect.ts, ai/detect-tile.ts): the picture is cut
into tiles of the model's input size (overlapping, so an object cut by one
tile is whole in the next), each tile's output is decoded into boxes,
and duplicates across tiles and classes are removed with NMS.

Everything here is NumPy only (no ONNX Runtime, no QGIS), in the picture's
pixels; the model is a function given by the caller.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, replace
from typing import Callable, Optional, Sequence

import numpy as np

from .mask_outline import mask_outline, ring_area
from .model_config import DetectorSpec

#: What a tile is padded with outside the picture (Ultralytics' letterbox gray).
PAD = 114
#: How many times a tile that reaches the model's limit of objects is split again into quarters.
SPLITS = 2

Ring = list[tuple[float, float]]


def tile_starts(length: int, tile: int, overlap: float) -> list[int]:
    """The starts of tiles of `tile` pixels covering `length`, overlapping by about `overlap` (0–0.9) of a tile."""
    if length <= tile:
        return [0]
    step = max(1, math.floor(tile * (1 - overlap)))
    count = math.ceil((length - tile) / step) + 1
    # Spread evenly, the last tile ending at the edge.
    return [round(i * (length - tile) / (count - 1)) for i in range(count)]


def tiles_of(width: int, height: int, size: Sequence[int], overlap: float) -> list[tuple[int, int]]:
    """The tiles (top left corners) covering a `width` × `height` picture."""
    xs = tile_starts(width, size[0], overlap)
    ys = tile_starts(height, size[1], overlap)
    return [(x, y) for y in ys for x in xs]


#: Pixels (width × height) read at once: larger pictures are read in blocks of about this size.
BLOCK = 4096


def blocks_of(width: int, height: int, margin: int, block: int = BLOCK) -> list[tuple[int, int, int, int]]:
    """
    The blocks (x, y, width, height) to read a picture in: one when it fits
    in `block` squared, else blocks overlapping by `margin` pixels (a model
    input), so an object cut at one block's edge is whole in the next.
    """
    if width * height <= block * block:
        return [(0, 0, width, height)]
    step = max(1, block - margin)

    def starts(length: int) -> list[int]:
        if length <= block:
            return [0]
        out = list(range(0, length - block, step))
        out.append(length - block)
        return out

    return [(x, y, min(block, width - x), min(block, height - y)) for y in starts(height) for x in starts(width)]


def tile_tensor(rgba: np.ndarray, x0: int, y0: int, size: Sequence[int], mean: Sequence[float], std: Sequence[float], bgr: bool = False) -> np.ndarray:
    """
    The model input (CHW float32, RGB, or BGR with `bgr`) for the `size` tile
    at (`x0`, `y0`) of an RGBA picture (height × width × 4, uint8):
    (value − mean) / std per channel (mean and std in R, G, B order). Pixels
    outside the picture, and transparent ones (no data), are the padding gray.

    A picture of band values (float, height × width × (bands + 1), the last
    channel whether the pixel has data; see `band_picture`) gives its bands
    scaled the same way, no data at the mean (0 once scaled).
    """
    tw, th = int(size[0]), int(size[1])
    height, width = rgba.shape[:2]
    channels = rgba.shape[2] - 1
    bands = rgba.dtype != np.uint8
    mean_ = np.asarray(mean, np.float32)
    tile = np.empty((th, tw, channels), np.float32)
    tile[...] = mean_ if bands else PAD
    xa, ya = max(0, x0), max(0, y0)
    xb, yb = min(width, x0 + tw), min(height, y0 + th)
    if xb > xa and yb > ya:
        part = rgba[ya:yb, xa:xb]
        visible = part[..., -1] != 0
        target = tile[ya - y0:yb - y0, xa - x0:xb - x0]
        target[visible] = part[..., :-1][visible]
    tile = (tile - mean_) / np.asarray(std, np.float32)
    if bgr:
        tile = tile[..., ::-1]
    return np.ascontiguousarray(tile.transpose(2, 0, 1))


def band_picture(values: np.ndarray, valid: np.ndarray) -> np.ndarray:
    """A picture of band values (height × width × bands) and where they have data, as `tile_tensor` takes it."""
    return np.concatenate([values.astype(np.float32, copy=False), valid[..., None].astype(np.float32)], axis=2)


def is_empty(rgba: np.ndarray, x0: int, y0: int, tw: int, th: int) -> bool:
    """Whether the tile at (x0, y0) has no visible pixel."""
    return not rgba[y0:y0 + th:2, x0:x0 + tw:2, -1].any()


def crop_of(rgba: np.ndarray, x0: int, y0: int, tw: int, th: int) -> np.ndarray:
    """The tile at (x0, y0) of an RGBA picture, `tw` × `th` (transparent past the edges)."""
    out = np.zeros((th, tw, rgba.shape[2]), rgba.dtype)
    part = rgba[y0:y0 + th, x0:x0 + tw]
    out[:part.shape[0], :part.shape[1]] = part
    return out


@dataclass
class Detection:
    """A found object, in pixels."""
    cls: int
    score: float
    #: Centre x, centre y, width, height.
    box: tuple[float, float, float, float]
    #: Rotation (radians, clockwise on the picture) of an oriented box.
    angle: Optional[float] = None
    #: Mask coefficients (instance segmentation).
    coeffs: Optional[np.ndarray] = None
    #: The outline from the mask (instance segmentation): rings of (x, y).
    outline: Optional[list[Ring]] = None


@dataclass
class Layout:
    format: str
    values: int
    count: int
    transposed: bool
    classes: int


def layout_of(dims: Sequence[int], spec: DetectorSpec) -> Layout:
    """
    How the first output is laid out, from its shape and the spec: the number
    of values per candidate and whether candidates run along the last axis
    (YOLOv8: `[1, 4 + classes, N]`) or the middle one (YOLOv5: `[1, N, 5 + classes]`).
    """
    a, b = (dims[1], dims[2]) if len(dims) == 3 else tuple(dims[-2:])
    extra = 1 if spec.task == "obb" else 32 if spec.task == "segment" else 0
    known = len(spec.classes)

    def values_of(fmt: str) -> int:
        """Values per candidate of a format, with the class names known."""
        return 4 + extra + known + (1 if fmt == "yolov5" else 0)

    if spec.format == "detectron2":
        raise ValueError("Detectron2 の出力は decode_detectron2 で読み込み")
    fmt = spec.format
    if fmt == "auto":
        # End-to-end exports (NMS inside, YOLOv10, YOLO26): [1, ≤ 1000, 6] (7 with an angle, 38 with mask coefficients).
        fits_known = known > 0 and any(n in (values_of("yolov8"), values_of("yolov5")) for n in (a, b))
        if b == 6 + extra and a <= 1000 and not fits_known:
            fmt = "end2end"
        # With the class names known, the number of values tells v8 (no objectness) from v5; without, the
        # axes do: v8 exports put the candidates last, v5 exports in the middle.
        elif known:
            fmt = "yolov8" if values_of("yolov8") in (a, b) else "yolov5"
        else:
            fmt = "yolov8" if a < b else "yolov5"
    if fmt == "end2end":
        return Layout(fmt, b, a, False, known)
    # YOLOX: [1, N, 5 + classes], N being the cells of all its grids.
    if fmt == "yolox":
        return Layout(fmt, b, a, False, b - 5)
    # Which axis holds the values: the one of the expected count, else the shorter one.
    expected = values_of(fmt)
    transposed = (a == expected) if known and expected in (a, b) else a < b
    values, count = (a, b) if transposed else (b, a)
    return Layout(fmt, values, count, transposed, values - 4 - extra - (1 if fmt == "yolov5" else 0))


def decode_yolo(dims: Sequence[int], data: np.ndarray, spec: DetectorSpec, min_score: float) -> list[Detection]:
    """The candidates of one tile's first output scoring at least `min_score`, in the tile's pixels."""
    layout = layout_of(dims, spec)
    flat = np.asarray(data, np.float32).reshape(-1)
    # Candidates as rows of values.
    rows = flat[:layout.values * layout.count].reshape((layout.values, layout.count)).T if layout.transposed \
        else flat[:layout.values * layout.count].reshape((layout.count, layout.values))
    with np.errstate(invalid="ignore", over="ignore"):
        if layout.format == "end2end":
            scores = rows[:, 4]
            keep = scores >= min_score
            rows, scores = rows[keep], scores[keep]
            classes = np.rint(rows[:, 5]).astype(np.int64)
            if spec.task == "obb":
                boxes = rows[:, :4]
                angles = rows[:, 6]
            else:
                x1, y1, x2, y2 = rows[:, 0], rows[:, 1], rows[:, 2], rows[:, 3]
                boxes = np.stack([(x1 + x2) / 2, (y1 + y2) / 2, x2 - x1, y2 - y1], axis=1)
                angles = None
            coeff_at = 6 if spec.task == "segment" else -1
        else:
            with_objectness = layout.format in ("yolov5", "yolox")
            first = 5 if with_objectness else 4
            objectness = rows[:, 4] if with_objectness else np.ones(len(rows), np.float32)
            keep = objectness >= min_score
            index = np.nonzero(keep)[0]
            rows, objectness = rows[keep], objectness[keep]
            if layout.classes > 0:
                class_scores = rows[:, first:first + layout.classes]
                classes = np.argmax(class_scores, axis=1)
                scores = class_scores[np.arange(len(rows)), classes] * objectness
            else:
                classes = np.zeros(len(rows), np.int64)
                scores = np.zeros(len(rows), np.float32)
            keep = scores >= min_score
            rows, scores, classes, index = rows[keep], scores[keep], classes[keep], index[keep]
            if layout.format == "yolox":
                # YOLOX's raw output: offsets in the cell and log sizes, in strides.
                grid = yolox_grid(spec.input_size, spec.strides, layout.count)[index]
                s = grid[:, 2]
                boxes = np.stack([(rows[:, 0] + grid[:, 0]) * s, (rows[:, 1] + grid[:, 1]) * s, np.exp(rows[:, 2]) * s, np.exp(rows[:, 3]) * s], axis=1)
            else:
                boxes = rows[:, :4]
            angles = rows[:, first + layout.classes] if spec.task == "obb" else None
            coeff_at = first + layout.classes if spec.task == "segment" else -1
    found: list[Detection] = []
    for i in range(len(rows)):
        box = tuple(float(v) for v in boxes[i])
        if not (box[2] > 0 and box[3] > 0):
            continue
        d = Detection(int(classes[i]), float(scores[i]), box)
        if angles is not None and math.isfinite(float(angles[i])):
            d.angle = float(angles[i])
        if coeff_at >= 0:
            d.coeffs = np.array(rows[i, coeff_at:coeff_at + 32], np.float32)
        found.append(d)
    return found


def detectron2_outputs(outputs: Sequence[tuple[str, str, Sequence]]) -> Optional[tuple[str, str, str]]:
    """
    Which of a model's outputs (name, type, dims) are Detectron2's boxes `[N, 4]`,
    scores `[N]` and classes `[N]`, by name; None when they are not laid out so.
    Classes are the first integer output of length N (Detectron2 writes the
    image size, an integer pair, after them), or one named class / label.
    """
    def is_float(t: str) -> bool:
        return "float" in t or "double" in t

    boxes = next((o for o in outputs if len(o[2]) == 2 and o[2][1] == 4 and is_float(o[1])), None)
    if boxes is None:
        return None
    n = boxes[2][0]
    flat = [o for o in outputs if o is not boxes and len(o[2]) == 1 and o[2][0] == n]
    scores = next((o for o in flat if "score" in o[0].lower() and is_float(o[1])), None) or next((o for o in flat if is_float(o[1])), None)
    ints = [o for o in flat if not is_float(o[1])]
    classes = next((o for o in ints if "class" in o[0].lower() or "label" in o[0].lower()), None) or (ints[0] if ints else None) \
        or next((o for o in flat if o is not scores and ("class" in o[0].lower() or "label" in o[0].lower())), None)
    return (boxes[0], scores[0], classes[0]) if scores and classes else None


def decode_detectron2(boxes: np.ndarray, scores: np.ndarray, classes: np.ndarray, min_score: float) -> list[Detection]:
    """Detectron2's outputs as objects scoring at least `min_score`, in the tile's pixels."""
    boxes = np.asarray(boxes, np.float64).reshape(-1, 4)
    found = []
    for i, score in enumerate(np.asarray(scores, np.float64).reshape(-1)):
        if not score >= min_score:
            continue
        x1, y1, x2, y2 = boxes[i]
        if not (x2 > x1 and y2 > y1):
            continue
        found.append(Detection(int(np.asarray(classes).reshape(-1)[i]), float(score), ((x1 + x2) / 2, (y1 + y2) / 2, x2 - x1, y2 - y1)))
    return found


def yolox_grid(size: Sequence[int], strides: Sequence[int], count: int) -> np.ndarray:
    """The cell (x, y) and stride of each of YOLOX's `count` candidates: the grids of its strides one after the other, row by row."""
    cells = sum((size[0] // s) * (size[1] // s) for s in strides)
    if cells != count:
        raise ValueError(f"YOLOX の出力 {count} 個が入力 {size[0]}×{size[1]}・ストライド {'/'.join(map(str, strides))} の格子（{cells} 個）と不一致")
    parts = []
    for s in strides:
        gw, gh = size[0] // s, size[1] // s
        ys, xs = np.mgrid[0:gh, 0:gw]
        parts.append(np.stack([xs.ravel(), ys.ravel(), np.full(gw * gh, s)], axis=1))
    return np.concatenate(parts).astype(np.float32)


def box_corners(d: Detection) -> Ring:
    """The corners of a detection's box, clockwise on the picture (y down)."""
    cx, cy, w, h = d.box
    a = d.angle or 0.0
    cos, sin = math.cos(a), math.sin(a)
    return [(cx + x * cos - y * sin, cy + x * sin + y * cos) for x, y in ((-w / 2, -h / 2), (w / 2, -h / 2), (w / 2, h / 2), (-w / 2, h / 2))]


def _clip_convex(subject: Ring, clip: Ring) -> Ring:
    """The part of convex polygon `subject` inside convex polygon `clip` (Sutherland–Hodgman); both in the same winding."""
    out = subject
    sign = math.copysign(1, ring_area(clip)) if ring_area(clip) else 1
    for i in range(len(clip)):
        if not out:
            break
        ax, ay = clip[i]
        bx, by = clip[(i + 1) % len(clip)]

        def inside(p) -> bool:
            return sign * ((bx - ax) * (p[1] - ay) - (by - ay) * (p[0] - ax)) >= 0

        source, out = out, []
        for j in range(len(source)):
            p, q = source[j], source[(j + 1) % len(source)]
            pin, qin = inside(p), inside(q)
            if pin:
                out.append(p)
            if pin != qin:
                dx, dy = q[0] - p[0], q[1] - p[1]
                den = (bx - ax) * dy - (by - ay) * dx
                t = 0 if den == 0 else ((p[0] - ax) * dy - (p[1] - ay) * dx) / den
                out.append((ax + (bx - ax) * t, ay + (by - ay) * t))
    return out


def overlap(a: Detection, b: Detection) -> tuple[float, float]:
    """The overlap of two boxes (rotated or not): intersection over union, and over the smaller one."""
    ra, rb = math.hypot(a.box[2], a.box[3]) / 2, math.hypot(b.box[2], b.box[3]) / 2
    if abs(a.box[0] - b.box[0]) > ra + rb or abs(a.box[1] - b.box[1]) > ra + rb:
        return 0.0, 0.0
    if not a.angle and not b.angle:
        w = min(a.box[0] + a.box[2] / 2, b.box[0] + b.box[2] / 2) - max(a.box[0] - a.box[2] / 2, b.box[0] - b.box[2] / 2)
        h = min(a.box[1] + a.box[3] / 2, b.box[1] + b.box[3] / 2) - max(a.box[1] - a.box[3] / 2, b.box[1] - b.box[3] / 2)
        inter = w * h if w > 0 and h > 0 else 0.0
    else:
        inter = abs(ring_area(_clip_convex(box_corners(a), box_corners(b))))
    area_a, area_b = a.box[2] * a.box[3], b.box[2] * b.box[3]
    return inter / ((area_a + area_b - inter) or 1), inter / (min(area_a, area_b) or 1)


def nms(detections: Sequence[Detection], iou: float, within: float = 0.85) -> list[Detection]:
    """
    Non-maximum suppression per class: the best box stays, and any weaker box
    of its class overlapping it by more than `iou` (intersection over union),
    or lying mostly inside it (`within` of the smaller box: a piece of an
    object cut at a tile's edge), goes.
    """
    kept: list[Detection] = []
    by_class: dict[int, list[Detection]] = {}
    for d in sorted(detections, key=lambda x: -x.score):
        same = by_class.setdefault(d.cls, [])
        if any((lambda o: o[0] > iou or o[1] > within)(overlap(k, d)) for k in same):
            continue
        same.append(d)
        kept.append(d)
    return kept


def offset(d: Detection, x0: float, y0: float) -> Detection:
    """A detection moved from a tile's pixels into the picture's (the tile's corner at `x0`, `y0`)."""
    moved = replace(d, box=(d.box[0] + x0, d.box[1] + y0, d.box[2], d.box[3]))
    if d.outline:
        moved.outline = [[(x + x0, y + y0) for x, y in ring] for ring in d.outline]
    return moved


def instance_mask(d: Detection, protos: np.ndarray, input_size: Sequence[int]) -> Optional[tuple[np.ndarray, int, int]]:
    """
    The mask of an instance-segmentation detection (in its tile's pixels):
    sigmoid(coefficients · prototypes) inside its box, the prototypes
    (`[1, 32, mh, mw]`, covering the model input) sampled bilinearly.
    Returns the mask of the box's pixels (height × width, uint8) and its corner.
    """
    if d.coeffs is None:
        return None
    protos = np.asarray(protos, np.float32)
    k, mh, mw = protos.shape[-3:]
    protos = protos.reshape(k, mh, mw)
    iw, ih = input_size
    x0 = max(0, math.floor(d.box[0] - d.box[2] / 2))
    y0 = max(0, math.floor(d.box[1] - d.box[3] / 2))
    x1 = min(iw, math.ceil(d.box[0] + d.box[2] / 2))
    y1 = min(ih, math.ceil(d.box[1] + d.box[3] / 2))
    if x1 <= x0 or y1 <= y0:
        return None
    # The linear mask on the prototypes' grid, over the box (and one cell around it).
    gx0 = max(0, math.floor(x0 * mw / iw) - 1)
    gy0 = max(0, math.floor(y0 * mh / ih) - 1)
    gx1 = min(mw - 1, math.ceil(x1 * mw / iw) + 1)
    gy1 = min(mh - 1, math.ceil(y1 * mh / ih) + 1)
    grid = np.tensordot(d.coeffs[:k], protos[:, gy0:gy1 + 1, gx0:gx1 + 1], axes=1)
    fy = np.clip((np.arange(y0, y1) + 0.5) * mh / ih - 0.5, gy0, gy1) - gy0
    fx = np.clip((np.arange(x0, x1) + 0.5) * mw / iw - 0.5, gx0, gx1) - gx0
    ya = np.floor(fy).astype(int)
    yb = np.minimum(ya + 1, gy1 - gy0)
    ty = (fy - ya)[:, None]
    xa = np.floor(fx).astype(int)
    xb = np.minimum(xa + 1, gx1 - gx0)
    tx = (fx - xa)[None, :]
    v = (grid[ya][:, xa] * (1 - tx) + grid[ya][:, xb] * tx) * (1 - ty) + (grid[yb][:, xa] * (1 - tx) + grid[yb][:, xb] * tx) * ty
    # sigmoid(v) > 0.5
    return (v > 0).astype(np.uint8), x0, y0


#: A model run: the input tensor in, the outputs (name → array) out, in the model's order.
RunModel = Callable[[np.ndarray], "dict[str, np.ndarray]"]


def run_tile(run: RunModel, output_types: dict[str, str], rgba: np.ndarray, x0: int, y0: int, spec: DetectorSpec) -> tuple[list[Detection], bool]:
    """
    One run of the model over the tile at (`x0`, `y0`) of an RGBA picture: the
    objects in the picture's pixels (instance outlines traced from their
    masks), and whether it gave as many objects as it can.
    """
    tw, th = spec.input_size
    feed = tile_tensor(rgba, x0, y0, spec.input_size, spec.mean, spec.std, spec.bgr)
    outputs = run(feed if spec.unbatched else feed[None])
    names = list(outputs)
    height, width = rgba.shape[:2]
    full = False
    if spec.format == "detectron2":
        # Boxes, scores and classes apart, after the model's own NMS (the tiles' overlaps are merged later).
        parts = detectron2_outputs([(n, output_types.get(n, str(outputs[n].dtype)), outputs[n].shape) for n in names])
        if parts is None:
            raise ValueError("Detectron2 の出力（boxes [N, 4]・scores [N]・classes [N]）なし")
        found = decode_detectron2(outputs[parts[0]], outputs[parts[1]], outputs[parts[2]], spec.score)
        full = spec.max_detections > 0 and outputs[parts[1]].shape[0] >= spec.max_detections
    else:
        first = outputs[names[0]]
        found = nms(decode_yolo(first.shape, first, spec, spec.score), spec.iou)
    if spec.task == "segment":
        protos_name = next((n for n in names if outputs[n].ndim == 4), names[1] if len(names) > 1 else None)
        protos = outputs[protos_name] if protos_name else None
        traced = []
        for d in found:
            m = instance_mask(d, protos, spec.input_size) if protos is not None else None
            if m is None:
                continue
            mask, mx, my = m
            polygons = mask_outline(mask)
            if not polygons:
                continue
            # The largest part is the object; specks around it are left out.
            largest = max(polygons, key=lambda p: abs(ring_area(p[0])))
            traced.append(replace(d, coeffs=None, outline=[[(x + mx, y + my) for x, y in ring] for ring in largest]))
        found = traced
    # What is centred outside the picture (the padding, transparent no-data) is dropped; the rest moves into the picture.
    out = []
    for d in found:
        x, y = math.floor(d.box[0] + x0), math.floor(d.box[1] + y0)
        if 0 <= x < width and 0 <= y < height and rgba[y, x, -1] != 0:
            out.append(offset(d, x0, y0))
    return out, full


def detect_tile(run: RunModel, output_types: dict[str, str], rgba: np.ndarray, x0: int, y0: int, spec: DetectorSpec,
                depth: int = 0, region: Optional[Sequence[int]] = None) -> list[Detection]:
    """
    The objects in the tile at (`x0`, `y0`), in the picture's pixels. When the
    model gives as many objects as it can (`spec.max_detections`), more may
    have been left out: the tile is analysed again as four overlapping
    quarters, each alone in an input of the same size (the same scale, a
    quarter of the objects), up to SPLITS times.
    """
    found, full = run_tile(run, output_types, rgba, x0, y0, spec)
    if not full or depth >= SPLITS:
        return found
    region = region or spec.input_size
    tw, th = spec.input_size
    height, width = rgba.shape[:2]
    # Each quarter is a bit more than half the part analysed, so an object on the middle lines is whole in one of them.
    qw, qh = math.ceil(region[0] * 0.6), math.ceil(region[1] * 0.6)
    out: list[Detection] = []
    for sy in (0, region[1] - qh):
        for sx in (0, region[0] - qw):
            # The quarter alone at the corner of an input-size picture, the rest transparent (padding).
            piece = np.zeros((th, tw, rgba.shape[2]), rgba.dtype)
            ya, xa = max(0, y0 + sy), max(0, x0 + sx)
            yb, xb = min(height, y0 + sy + qh), min(width, x0 + sx + qw)
            if yb > ya and xb > xa:
                piece[ya - (y0 + sy):yb - (y0 + sy), xa - (x0 + sx):xb - (x0 + sx)] = rgba[ya:yb, xa:xb]
            inside = detect_tile(run, output_types, piece, 0, 0, spec, depth + 1, (qw, qh))
            out += [offset(d, x0 + sx, y0 + sy) for d in inside]
    return out


def class_filter(text: str) -> list[str]:
    """The classes asked for (comma separated, names or numbers, lower case); empty for all."""
    import re

    return [s.strip().lower() for s in re.split(r"[,、，]", text or "") if s.strip()]


def wanted(d: Detection, classes: Sequence[str], names: Sequence[str]) -> bool:
    name = names[d.cls] if 0 <= d.cls < len(names) else f"class {d.cls}"
    return not classes or name.lower() in classes or str(d.cls) in classes


def class_name(cls: int, names: Sequence[str]) -> str:
    return names[cls] if 0 <= cls < len(names) and names[cls] else f"class {cls}"
