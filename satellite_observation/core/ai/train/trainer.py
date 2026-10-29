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
from .loss import batch_box_targets, batch_targets, box_loss, detection_loss
from .metrics import Scores, evaluate
from .net import (
    RES_WIDTHS, STRIDE, WIDTHS, Adam, Network, architecture, grow_classes, init_buffers, init_params, res_architecture, yolo_output,
)
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


#: The networks: 高精度 (residual, batch-normalized, GIoU and quality scores) and 軽量 (the first, small plain one).
ARCHS = ("res", "fpn4")


def layers_of(arch: str, size: str, classes: int) -> list:
    if arch == "res":
        return res_architecture(RES_WIDTHS[size], classes)
    return architecture(WIDTHS[size], classes)


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
    arch: str = "fpn4"

    def state(self) -> dict:
        widths = RES_WIDTHS if self.arch == "res" else WIDTHS
        return {"format": FORMAT, "version": 1,
                "arch": {"name": self.arch, "size": self.widths, "widths": list(widths[self.widths]), "stride": STRIDE},
                "input": self.input.to_json(), "tile": self.tile, "classes": self.classes, "depth": self.depth, "history": self.history}

    def onnx(self) -> bytes:
        import json

        meta = {"task": "detect", "imgsz": f"[{self.tile}, {self.tile}]", "names": names_text(self.classes), "stride": str(STRIDE),
                "description": "satellite_observation で学習した物体検出モデル", "author": "satellite_observation",
                META_KEY: json.dumps(self.state(), ensure_ascii=False)}
        return model_bytes(self.network.layers, {**self.network.params, **self.network.buffers}, self.input.channels,
                           (self.tile, self.tile), meta)


def new_model(classes: Sequence[str], input_spec: InputSpec, tile: int, size: str, depth: int = 0, seed: int = 0, arch: str = "res") -> Model:
    if tile not in TILE_SIZES:
        raise TrainError(f"入力の大きさは {'・'.join(map(str, TILE_SIZES))} のいずれか")
    if size not in WIDTHS:
        raise TrainError("モデルの大きさは small・medium・large のいずれか")
    if arch not in ARCHS:
        raise TrainError("ネットワークは res・fpn4 のいずれか")
    layers = layers_of(arch, size, len(classes))
    params = init_params(layers, input_spec.channels, np.random.default_rng(seed))
    return Model(Network(layers, params), size, list(classes), input_spec, tile, depth=depth, arch=arch)


def load_model(data: bytes) -> Model:
    """A model trained here, read back from its ONNX file to train it further."""
    try:
        meta, inits = read_model(data)
    except (ValueError, IndexError) as e:
        raise TrainError(f"モデルを読めず（{e}）")
    state = training_state(meta)
    if state is None:
        raise TrainError("このプラグインで学習したモデルのみ追加学習可能（他の ONNX は物体検出（AI）で利用）")
    arch = state.get("arch", {}).get("name", "fpn4")
    size = state.get("arch", {}).get("size")
    if arch not in ARCHS or size not in WIDTHS:
        raise TrainError("モデルの形式が不明（新しい版のプラグインで学習したモデルの可能性）")
    classes = [str(c) for c in state.get("classes", [])]
    layers = layers_of(arch, size, len(classes))
    wanted = init_params(layers, 1, np.random.default_rng(0))
    params, buffers = {}, {}
    for key in list(wanted) + list(init_buffers(layers)):
        if key not in inits:
            raise TrainError(f"モデルの重み {key} なし")
        (buffers if key.endswith((".bn_m", ".bn_v")) else params)[key] = inits[key].astype(np.float32)
    i = state.get("input", {})
    spec = InputSpec(i.get("mode", "rgb"), [int(b) for b in i.get("bands", [])], [float(v) for v in i.get("mean", [])],
                     [float(v) for v in i.get("std", [])])
    return Model(Network(layers, params, buffers), size, classes, spec, int(state.get("tile", 256)), list(state.get("history", [])),
                 int(state.get("depth", 0)), arch)


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


#: The range of the random zoom of training tiles (objects seen a little smaller and larger than they are).
SCALES = (0.75, 1.33)


def sample(window: Window, spec: InputSpec, tile: int, rng: np.random.Generator, augment: bool = True,
           scales: Optional[tuple[float, float]] = SCALES) -> tuple[np.ndarray, np.ndarray]:
    """A random training tile of a window and its boxes: zoomed, turned, flipped and brightened at random when `augment`."""
    h, w = window.pixels.shape[:2]
    scale = math.exp(rng.uniform(math.log(scales[0]), math.log(scales[1]))) if augment and scales else 1.0
    side = max(16, int(round(tile / scale)))
    x0 = int(rng.integers(0, w - side + 1)) if w > side else -int(rng.integers(0, side - w + 1))
    y0 = int(rng.integers(0, h - side + 1)) if h > side else -int(rng.integers(0, side - h + 1))
    pixels, boxes = crop(window, x0, y0, side)
    if side != tile:
        # Nearest neighbour: cheap, and the band values stay values.
        index = np.minimum(((np.arange(tile) + 0.5) * side / tile).astype(np.int64), side - 1)
        pixels = pixels[index][:, index]
        boxes = boxes.copy()
        boxes[:, :4] *= tile / side
        boxes = boxes[(boxes[:, 2] >= 1.5) & (boxes[:, 3] >= 1.5)]
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

def detect_window(model: Model, window: Window, score: float = SCORE, network: Optional[Network] = None) -> list[Detection]:
    """The model run over a whole window in overlapping tiles, as 物体検出（AI） runs it over an image."""
    from ..detect import offset, tiles_of

    net = network or model.network
    tile = model.tile
    spec = detector_spec(None, {"imgsz": f"[{tile}, {tile}]", "names": names_text(model.classes)})
    spec.score, spec.iou = score, IOU
    h, w = window.pixels.shape[:2]
    found: list[Detection] = []
    for x0, y0 in tiles_of(w, h, (tile, tile), 0.25):
        pixels, _ = crop(window, x0, y0, tile)
        if not (pixels[..., -1] > 0).any():
            continue
        out = yolo_output(net.forward(input_of(pixels, model.input)[None], training=False))
        for d in nms(decode_yolo(out.shape, out[0], spec, score), IOU):
            x, y = int(d.box[0]), int(d.box[1])
            if 0 <= x < tile and 0 <= y < tile and pixels[y, x, -1] > 0:
                found.append(offset(d, x0, y0))
    return nms(found, IOU)


def check(model: Model, windows: Sequence[Window], network: Optional[Network] = None) -> Scores:
    """mAP50, mAP50-95 (and precision and recall at the score 0.25) on labelled windows."""
    found = []
    for w in windows:
        found.append(np.array([[*d.box, d.cls, d.score] for d in detect_window(model, w, 0.05, network)], float).reshape(-1, 6))
    return evaluate(found, [w.boxes for w in windows], model.classes, SCORE)


# Training.

@dataclass
class TrainRun:
    epochs: int = 0
    steps: int = 0
    losses: list[float] = field(default_factory=list)
    checks: list[Scores] = field(default_factory=list)
    best: Optional[Scores] = None
    best_epoch: int = 0
    seconds: float = 0.0
    stopped: bool = False


def learning_rate(step: int, total: int, base: float, warmup: int) -> float:
    """Warmed up linearly, then down a cosine to 5 %."""
    if step < warmup:
        return base * (step + 1) / warmup
    p = (step - warmup) / max(1, total - warmup)
    return base * (0.05 + 0.95 * 0.5 * (1 + math.cos(math.pi * min(1.0, p))))


class Average:
    """
    The exponential moving average of the weights (and batch statistics),
    what is checked and kept: steadier than the last step's weights. It
    reaches over about a tenth of the training, and starts short.
    """

    def __init__(self, net: Network, total: int):
        self.params = {k: v.copy() for k, v in net.params.items()}
        self.buffers = {k: v.copy() for k, v in net.buffers.items()}
        self.most = min(0.9999, max(0.9, 1 - 10 / max(1, total)))
        self.ramp = max(20.0, total / 20)
        self.steps = 0

    def update(self, net: Network) -> None:
        self.steps += 1
        d = self.most * (1 - math.exp(-self.steps / self.ramp))
        for store, now in ((self.params, net.params), (self.buffers, net.buffers)):
            for k, v in now.items():
                store[k] *= d
                store[k] += (1 - d) * v

    def network(self, net: Network) -> Network:
        return Network(net.layers, self.params, self.buffers)


def train(model: Model, train_windows: Sequence[Window], check_windows: Sequence[Window], options: TrainOptions,
          progress: Optional[Callable[[str, float], None]] = None, log: Optional[Callable[[str], None]] = None,
          cancelled: Callable[[], bool] = lambda: False) -> TrainRun:
    """
    Trains the model in place for `options.epochs` over the windows, and
    keeps the moving average of its weights; when some windows are held
    back, the average of the epoch that found them best (mAP50-95). Stopped
    (cancelled) early, it keeps what it had learnt.
    """
    if not train_windows:
        raise TrainError("学習する画像の範囲なし")
    rng = np.random.default_rng(options.seed)
    net = model.network
    tile = model.tile
    cells = tile // STRIDE
    residual = "box" in net.heads
    per_epoch = max(MIN_STEPS, math.ceil(len(train_windows) / options.batch))
    total = per_epoch * options.epochs
    warmup = min(100, max(1, total // 10))
    optimizer = Adam(net.params, options.lr, decay=5e-4 if residual else 1e-4)
    average = Average(net, total)
    run = TrainRun()
    started = time.monotonic()
    best = None
    best_score = -1.0
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
            heads = net.forward(x, keep=True)
            if residual:
                losses, grads = box_loss(heads, batch_box_targets([b[1] for b in batch], cells, cells))
            else:
                losses, grads = detection_loss(heads, batch_targets([b[1] for b in batch], cells, cells, len(model.classes)))
            optimizer.step(net.params, net.backward(grads), learning_rate(run.steps, total, options.lr, warmup))
            average.update(net)
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
            c = check(model, check_windows, average.network(net))
            run.checks.append(c)
            text += f"・検証 mAP50 {c.map50:.3f}・mAP50-95 {c.map:.3f}"
            if c.map > best_score:
                best_score, run.best, run.best_epoch = c.map, c, epoch + 1
                best = ({k: v.copy() for k, v in average.params.items()}, {k: v.copy() for k, v in average.buffers.items()})
        if log:
            log(text)
    if run.steps:
        params, buffers = best if best is not None else (average.params, average.buffers)
        net.params.update(params)
        net.buffers.update(buffers)
        if best is not None and run.best_epoch != run.epochs and log:
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
