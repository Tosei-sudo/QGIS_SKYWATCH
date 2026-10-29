"""
Training a detection model on windows of images and their labelled boxes,
from scratch or on from a model trained before (追加学習): random tiles of
the windows, turned and flipped (as satellite images have no up), and
brightness and contrast jittered; a part of the windows held back to check
the model on, keeping the best epoch.

The input is the image as shown (RGB, 0–255, as 物体検出（AI） draws it), or
the values of chosen bands (one or several), each scaled by the mean and
standard deviation of the training pixels, which the model keeps so
detection scales them the same.
"""
from __future__ import annotations

import datetime
import math
import time
from dataclasses import dataclass, field
from typing import Callable, Optional, Sequence

import numpy as np

from ..detect import PAD, Detection, decode_yolo, nms
from ..model_config import detector_spec
from .loss import batch_targets, detection_loss
from .net import STRIDE, WIDTHS, Adam, Conv, Network, architecture, grow_classes, init_params, yolo_output
from .onnx_io import FORMAT, META_KEY, model_bytes, names_text, read_model, training_state

#: Input sizes offered (multiples of 16, the network's coarsest level).
TILE_SIZES = (128, 256, 384, 512)
#: A box cut by a tile's edge is kept when this much of it is inside.
KEEP_CUT = 0.4
#: Updates per epoch at least (few windows are seen several times an epoch).
MIN_STEPS = 20
#: Score and overlap of the check on held-back windows (as detection's defaults).
SCORE, IOU, MATCH_IOU = 0.25, 0.45, 0.5


class TrainError(Exception):
    """Training that cannot go on, in the person's words."""


@dataclass
class Window:
    """
    A part of an image read for training and its objects.

    `pixels` is height × width × (channels + 1), the last channel whether
    the pixel has data: RGBA uint8 for the image as shown, else the bands'
    values (float32, scaled with `normalize` before training).
    """
    pixels: np.ndarray
    #: Rows of centre x, centre y, width, height (pixels of the window), class.
    boxes: np.ndarray
    image: str = ""

    @property
    def channels(self) -> int:
        return self.pixels.shape[2] - 1


@dataclass
class InputSpec:
    """What the model takes: the image as shown (`rgb`) or band values, and how they are scaled."""
    mode: str = "rgb"
    #: Band numbers (1 first), for `bands`.
    bands: list[int] = field(default_factory=list)
    mean: list[float] = field(default_factory=list)
    std: list[float] = field(default_factory=list)

    @property
    def channels(self) -> int:
        return 3 if self.mode == "rgb" else len(self.bands)

    def to_json(self) -> dict:
        return {"mode": self.mode, "bands": self.bands, "mean": self.mean, "std": self.std}


@dataclass
class TrainOptions:
    epochs: int = 30
    tile: int = 256
    size: str = "small"
    batch: int = 8
    lr: float = 3e-3
    #: Share of the windows held back to check on (0: none).
    validation: float = 0.15
    seed: int = 0


@dataclass
class Check:
    """The model checked on the held-back windows."""
    precision: float
    recall: float
    f1: float
    found: int
    objects: int

    def to_json(self) -> dict:
        return {"precision": round(self.precision, 3), "recall": round(self.recall, 3), "f1": round(self.f1, 3),
                "found": self.found, "objects": self.objects}


@dataclass
class Model:
    """A model in training: its network, classes, input and history."""
    network: Network
    widths: str
    classes: list[str]
    input: InputSpec
    tile: int
    history: list[dict] = field(default_factory=list)
    #: Depth of the classification the classes are cut to (0: as recorded).
    depth: int = 0

    def state(self) -> dict:
        return {"format": FORMAT, "version": 1, "arch": {"name": "fpn4", "size": self.widths, "widths": list(WIDTHS[self.widths]),
                                                         "stride": STRIDE},
                "input": self.input.to_json(), "tile": self.tile, "classes": self.classes, "depth": self.depth, "history": self.history}

    def onnx(self) -> bytes:
        import json

        meta = {"task": "detect", "imgsz": f"[{self.tile}, {self.tile}]", "names": names_text(self.classes), "stride": str(STRIDE),
                "description": "satellite_observation で学習した物体検出モデル", "author": "satellite_observation",
                META_KEY: json.dumps(self.state(), ensure_ascii=False)}
        return model_bytes(self.network.layers, self.network.params, self.input.channels, (self.tile, self.tile), meta)


def new_model(classes: Sequence[str], input_spec: InputSpec, tile: int, size: str, depth: int = 0, seed: int = 0) -> Model:
    if tile not in TILE_SIZES:
        raise TrainError(f"入力の大きさは {'・'.join(map(str, TILE_SIZES))} のいずれか")
    if size not in WIDTHS:
        raise TrainError("モデルの大きさは small・medium・large のいずれか")
    layers = architecture(WIDTHS[size], len(classes))
    params = init_params(layers, input_spec.channels, np.random.default_rng(seed))
    return Model(Network(layers, params), size, list(classes), input_spec, tile, depth=depth)


def load_model(data: bytes) -> Model:
    """A model trained here, read back from its ONNX file to train it further."""
    try:
        meta, inits = read_model(data)
    except (ValueError, IndexError) as e:
        raise TrainError(f"モデルを読めず（{e}）")
    state = training_state(meta)
    if state is None:
        raise TrainError("このプラグインで学習したモデルのみ追加学習可能（他の ONNX は物体検出（AI）で利用）")
    size = state.get("arch", {}).get("size")
    if size not in WIDTHS:
        raise TrainError("モデルの形式が不明（新しい版のプラグインで学習したモデルの可能性）")
    classes = [str(c) for c in state.get("classes", [])]
    layers = architecture(WIDTHS[size], len(classes))
    params = {}
    for layer in layers:
        if not isinstance(layer, Conv):
            continue
        for part in ("w", "b"):
            key = f"{layer.name}.{part}"
            if key not in inits:
                raise TrainError(f"モデルの重み {key} なし")
            params[key] = inits[key].astype(np.float32)
    i = state.get("input", {})
    spec = InputSpec(i.get("mode", "rgb"), [int(b) for b in i.get("bands", [])], [float(v) for v in i.get("mean", [])],
                     [float(v) for v in i.get("std", [])])
    return Model(Network(layers, params), size, classes, spec, int(state.get("tile", 256)), list(state.get("history", [])),
                 int(state.get("depth", 0)))


def add_classes(model: Model, classes: Sequence[str], seed: int = 0) -> list[str]:
    """Grows the model's classes to hold `classes` (new ones after the old); gives the new ones."""
    new = [c for c in classes if c not in model.classes]
    if new:
        model.classes = model.classes + new
        model.network.params = grow_classes(model.network.params, len(model.classes), np.random.default_rng(seed + 1))
        for layer in model.network.layers:
            if layer.name == "cls":
                layer.cout = len(model.classes)
    return new


# The data.

def band_statistics(windows: Sequence[Window]) -> tuple[list[float], list[float]]:
    """The mean and standard deviation of each band over the pixels with data."""
    channels = windows[0].channels
    total = np.zeros(channels)
    square = np.zeros(channels)
    count = 0
    for w in windows:
        valid = w.pixels[..., -1] > 0
        values = w.pixels[..., :-1][valid].astype(np.float64)
        total += values.sum(axis=0)
        square += (values * values).sum(axis=0)
        count += len(values)
    if not count:
        raise TrainError("学習に使える画素なし（すべて値なし）")
    mean = total / count
    std = np.sqrt(np.maximum(square / count - mean * mean, 0))
    return [float(v) for v in mean], [float(max(s, 1e-6)) for s in std]


def normalize(window: Window, spec: InputSpec) -> Window:
    """The window's band values scaled (float16 to halve memory), no-data at 0, the mask last."""
    if spec.mode == "rgb":
        return window
    valid = window.pixels[..., -1:] > 0
    values = (window.pixels[..., :-1] - np.asarray(spec.mean, np.float32)) / np.asarray(spec.std, np.float32)
    values = np.where(valid, values, 0)
    return Window(np.concatenate([values, valid], axis=2).astype(np.float16), window.boxes, window.image)


def input_of(pixels: np.ndarray, spec: InputSpec) -> np.ndarray:
    """The network's input (HWC float32) for a part of a window: as 物体検出（AI） makes it."""
    valid = pixels[..., -1:] > 0
    if spec.mode == "rgb":
        rgb = np.where(valid, pixels[..., :3].astype(np.float32), np.float32(PAD))
        return rgb / np.float32(255)
    return np.where(valid, pixels[..., :-1].astype(np.float32), np.float32(0))


def crop(window: Window, x0: int, y0: int, tile: int) -> tuple[np.ndarray, np.ndarray]:
    """The `tile` square at (x0, y0) of a window (no data past its edges) and the boxes in it (cut ones clipped)."""
    h, w = window.pixels.shape[:2]
    out = np.zeros((tile, tile, window.pixels.shape[2]), window.pixels.dtype)
    xa, ya, xb, yb = max(0, x0), max(0, y0), min(w, x0 + tile), min(h, y0 + tile)
    if xb > xa and yb > ya:
        out[ya - y0:yb - y0, xa - x0:xb - x0] = window.pixels[ya:yb, xa:xb]
    boxes = []
    for cx, cy, bw, bh, c in window.boxes:
        x1, y1, x2, y2 = cx - bw / 2 - x0, cy - bh / 2 - y0, cx + bw / 2 - x0, cy + bh / 2 - y0
        cx1, cy1, cx2, cy2 = max(x1, 0), max(y1, 0), min(x2, tile), min(y2, tile)
        if cx2 - cx1 < 2 or cy2 - cy1 < 2 or (cx2 - cx1) * (cy2 - cy1) < KEEP_CUT * bw * bh:
            continue
        boxes.append(((cx1 + cx2) / 2, (cy1 + cy2) / 2, cx2 - cx1, cy2 - cy1, c))
    return out, np.array(boxes, np.float32).reshape(-1, 5)


def turn(x: np.ndarray, boxes: np.ndarray, k: int, flip: bool) -> tuple[np.ndarray, np.ndarray]:
    """A square tile turned k quarter turns (counter-clockwise) and flipped left–right, with its boxes."""
    t = x.shape[0]
    b = boxes.copy()
    for _ in range(k % 4):
        # np.rot90: the new x is the old y, the new y is the side minus the old x.
        b[:, 0], b[:, 1], b[:, 2], b[:, 3] = b[:, 1].copy(), t - b[:, 0], b[:, 3].copy(), b[:, 2].copy()
    x = np.rot90(x, k, axes=(0, 1))
    if flip:
        x = x[:, ::-1]
        b[:, 0] = t - b[:, 0]
    return np.ascontiguousarray(x), b


def sample(window: Window, spec: InputSpec, tile: int, rng: np.random.Generator, augment: bool = True) -> tuple[np.ndarray, np.ndarray]:
    """A random training tile of a window and its boxes."""
    h, w = window.pixels.shape[:2]
    x0 = int(rng.integers(0, w - tile + 1)) if w > tile else -int(rng.integers(0, tile - w + 1))
    y0 = int(rng.integers(0, h - tile + 1)) if h > tile else -int(rng.integers(0, tile - h + 1))
    pixels, boxes = crop(window, x0, y0, tile)
    x = input_of(pixels, spec)
    if augment:
        valid = (pixels[..., -1:] > 0).astype(np.float32)
        both, boxes = turn(np.concatenate([x, valid], axis=2), boxes, int(rng.integers(4)), bool(rng.integers(2)))
        x, valid = both[..., :-1], both[..., -1:] > 0
        gain = np.float32(rng.uniform(0.8, 1.2))
        shift = np.float32(rng.uniform(-0.1, 0.1) if spec.mode == "rgb" else rng.uniform(-0.2, 0.2))
        x = np.where(valid, x * gain + shift, x)
    return x.astype(np.float32, copy=False), boxes


def split(windows: Sequence[Window], share: float, rng: np.random.Generator) -> tuple[list[Window], list[Window]]:
    """Windows to train on and to check on (none held back when too few to spare)."""
    order = list(rng.permutation(len(windows)))
    held = int(round(len(windows) * share)) if share > 0 and len(windows) >= 8 else 0
    return [windows[i] for i in order[held:]], [windows[i] for i in order[:held]]


# Checking.

def iou(a: Sequence[float], b: Sequence[float]) -> float:
    ax1, ay1, ax2, ay2 = a[0] - a[2] / 2, a[1] - a[3] / 2, a[0] + a[2] / 2, a[1] + a[3] / 2
    bx1, by1, bx2, by2 = b[0] - b[2] / 2, b[1] - b[3] / 2, b[0] + b[2] / 2, b[1] + b[3] / 2
    w, h = min(ax2, bx2) - max(ax1, bx1), min(ay2, by2) - max(ay1, by1)
    if w <= 0 or h <= 0:
        return 0.0
    inter = w * h
    return inter / (a[2] * a[3] + b[2] * b[3] - inter)


def detect_window(model: Model, window: Window, score: float = SCORE) -> list[Detection]:
    """The model run over a whole window in overlapping tiles, as 物体検出（AI） runs it over an image."""
    from ..detect import offset, tiles_of

    tile = model.tile
    spec = detector_spec(None, {"imgsz": f"[{tile}, {tile}]", "names": names_text(model.classes)})
    spec.score, spec.iou = score, IOU
    h, w = window.pixels.shape[:2]
    found: list[Detection] = []
    for x0, y0 in tiles_of(w, h, (tile, tile), 0.25):
        pixels, _ = crop(window, x0, y0, tile)
        if not (pixels[..., -1] > 0).any():
            continue
        out = yolo_output(model.network.forward(input_of(pixels, model.input)[None]))
        for d in nms(decode_yolo(out.shape, out[0], spec, score), IOU):
            x, y = int(d.box[0]), int(d.box[1])
            if 0 <= x < tile and 0 <= y < tile and pixels[y, x, -1] > 0:
                found.append(offset(d, x0, y0))
    return nms(found, IOU)


def check(model: Model, windows: Sequence[Window]) -> Check:
    """Precision and recall on windows: a find is right when it overlaps an object of its class by IoU 0.5."""
    hits = found = objects = 0
    for window in windows:
        truth = [list(b) for b in window.boxes]
        objects += len(truth)
        used = [False] * len(truth)
        detections = sorted(detect_window(model, window), key=lambda d: -d.score)
        found += len(detections)
        for d in detections:
            best, at = MATCH_IOU, -1
            for i, t in enumerate(truth):
                if not used[i] and int(t[4]) == d.cls:
                    v = iou(d.box, t[:4])
                    if v >= best:
                        best, at = v, i
            if at >= 0:
                used[at] = True
                hits += 1
    precision = hits / found if found else 0.0
    recall = hits / objects if objects else 0.0
    f1 = 2 * precision * recall / (precision + recall) if precision + recall else 0.0
    return Check(precision, recall, f1, found, objects)


# Training.

@dataclass
class TrainRun:
    epochs: int = 0
    steps: int = 0
    losses: list[float] = field(default_factory=list)
    checks: list[Check] = field(default_factory=list)
    best: Optional[Check] = None
    best_epoch: int = 0
    seconds: float = 0.0
    stopped: bool = False


def learning_rate(step: int, total: int, base: float, warmup: int) -> float:
    """Warmed up linearly, then down a cosine to 5 %."""
    if step < warmup:
        return base * (step + 1) / warmup
    p = (step - warmup) / max(1, total - warmup)
    return base * (0.05 + 0.95 * 0.5 * (1 + math.cos(math.pi * min(1.0, p))))


def train(model: Model, train_windows: Sequence[Window], check_windows: Sequence[Window], options: TrainOptions,
          progress: Optional[Callable[[str, float], None]] = None, log: Optional[Callable[[str], None]] = None,
          cancelled: Callable[[], bool] = lambda: False) -> TrainRun:
    """
    Trains the model in place for `options.epochs` over the windows; when
    some are held back, the weights of the epoch that found them best are
    kept. Stopped (cancelled) early, it keeps what it had learnt.
    """
    if not train_windows:
        raise TrainError("学習する画像の範囲なし")
    rng = np.random.default_rng(options.seed)
    net = model.network
    tile = model.tile
    cells = tile // STRIDE
    per_epoch = max(MIN_STEPS, math.ceil(len(train_windows) / options.batch))
    total = per_epoch * options.epochs
    warmup = min(100, max(1, total // 10))
    optimizer = Adam(net.params, options.lr)
    run = TrainRun()
    started = time.monotonic()
    best_params = None
    best_f1 = -1.0
    for epoch in range(options.epochs):
        order = np.concatenate([rng.permutation(len(train_windows)) for _ in range(math.ceil(per_epoch * options.batch / len(train_windows)))])
        epoch_loss = 0.0
        for s in range(per_epoch):
            if cancelled():
                run.stopped = True
                break
            picks = order[s * options.batch:(s + 1) * options.batch]
            batch = [sample(train_windows[i], model.input, tile, rng) for i in picks]
            x = np.stack([b[0] for b in batch])
            targets = batch_targets([b[1] for b in batch], cells, cells, len(model.classes))
            heads = net.forward(x, keep=True)
            losses, grads = detection_loss(heads, targets)
            optimizer.step(net.params, net.backward(grads), learning_rate(run.steps, total, options.lr, warmup))
            run.steps += 1
            epoch_loss += losses["total"]
            if progress:
                progress(f"学習中… エポック {epoch + 1} / {options.epochs}・損失 {losses['total']:.3f}", run.steps / total)
        if run.stopped:
            break
        run.epochs = epoch + 1
        run.losses.append(epoch_loss / per_epoch)
        text = f"エポック {epoch + 1}: 損失 {run.losses[-1]:.3f}"
        last = epoch + 1 == options.epochs
        if check_windows and (last or (epoch + 1) % max(1, options.epochs // 10) == 0):
            if progress:
                progress(f"検証中… エポック {epoch + 1}", run.steps / total)
            c = check(model, check_windows)
            run.checks.append(c)
            text += f"・検証 適合率 {c.precision:.2f}・再現率 {c.recall:.2f}・F1 {c.f1:.2f}"
            if c.f1 > best_f1:
                best_f1, run.best, run.best_epoch = c.f1, c, epoch + 1
                best_params = {k: v.copy() for k, v in net.params.items()}
        if log:
            log(text)
    if best_params is not None and run.best_epoch != run.epochs:
        net.params.update(best_params)
        if log:
            log(f"検証で最もよいエポック {run.best_epoch} の重みを採用")
    run.seconds = time.monotonic() - started
    return run


def history_entry(run: TrainRun, images: Sequence[str], counts: dict[str, int], options: TrainOptions, base: str = "",
                  new_classes: Sequence[str] = ()) -> dict:
    """What one training adds to the model's history."""
    entry = {
        "at": datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "images": list(images), "objects": counts, "epochs": run.epochs, "steps": run.steps,
        "options": {"tile": options.tile, "batch": options.batch, "lr": options.lr},
        "loss": round(run.losses[-1], 4) if run.losses else None, "seconds": round(run.seconds, 1),
    }
    if run.best is not None:
        entry["check"] = run.best.to_json()
    if base:
        entry["base"] = base
    if new_classes:
        entry["newClasses"] = list(new_classes)
    if run.stopped:
        entry["stopped"] = True
    return entry
