"""
A small object detection network trained with NumPy alone: no PyTorch or
ONNX Runtime training, so it runs in any QGIS (both zips, closed networks)
and needs nothing bundled.

The network is a four-level encoder (3×3 convolutions with ReLU, halving
at each level) with two upsampling steps back to a quarter of the input
(a small FPN / U-Net), and three 1×1 heads at each cell of that grid, as
CenterNet / FCOS: class scores (sigmoid), the object centre's offset from
the cell centre, and the object's log size, both in cells.

Arrays are NHWC inside (convolutions as one matrix product per kernel
tap); weights are kept as ONNX lays them out (out, in, kh, kw) so the
trained model is written out as it is (see onnx_io).
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Optional, Sequence

import numpy as np

#: The cells of the output grid, in input pixels.
STRIDE = 4
#: Channels of the levels (½, ¼, ⅛, ¹⁄₁₆) and of the head, by model size.
WIDTHS = {
    "small": (16, 32, 48, 64, 32),
    "medium": (24, 48, 64, 96, 48),
    "large": (32, 64, 96, 128, 64),
}
#: The numbers' type (float64 for checking the gradients).
DTYPE = np.float32
#: Starting class score: about 1 % (the focal loss' prior), so the many empty cells do not swamp the first steps.
PRIOR = 0.01


@dataclass
class Conv:
    name: str
    source: str
    cout: int
    k: int = 3
    stride: int = 1
    relu: bool = True


@dataclass
class Up:
    """Nearest-neighbour upsampling ×2."""
    name: str
    source: str


@dataclass
class Cat:
    """Channels joined (first the upsampled level, then the skip)."""
    name: str
    sources: tuple[str, str]


HEADS = ("cls", "off", "size")


def architecture(widths: Sequence[int], classes: int) -> list:
    """The layers, in order, of a network of `widths` with `classes` classes (input: `x`)."""
    w1, w2, w3, w4, wh = widths
    return [
        Conv("a1", "x", w1, stride=2), Conv("a2", "a1", w1),
        Conv("b1", "a2", w2, stride=2), Conv("b2", "b1", w2),
        Conv("c1", "b2", w3, stride=2), Conv("c2", "c1", w3),
        Conv("d1", "c2", w4, stride=2), Conv("d2", "d1", w4),
        Up("u1", "d2"), Cat("k1", ("u1", "c2")), Conv("e1", "k1", w3),
        Up("u2", "e1"), Cat("k2", ("u2", "b2")), Conv("f1", "k2", wh), Conv("f2", "f1", wh),
        Conv("cls", "f2", classes, k=1, relu=False), Conv("off", "f2", 2, k=1, relu=False), Conv("size", "f2", 2, k=1, relu=False),
    ]


def channels_of(layers: list, cin: int) -> dict[str, int]:
    """The channels of every tensor, the input having `cin`."""
    out = {"x": cin}
    for layer in layers:
        if isinstance(layer, Conv):
            out[layer.name] = layer.cout
        elif isinstance(layer, Up):
            out[layer.name] = out[layer.source]
        else:
            out[layer.name] = out[layer.sources[0]] + out[layer.sources[1]]
    return out


def init_params(layers: list, cin: int, rng: np.random.Generator) -> dict[str, np.ndarray]:
    """He-initialized weights (ONNX layout), zero biases, the class bias at the prior."""
    channels = channels_of(layers, cin)
    params: dict[str, np.ndarray] = {}
    for layer in layers:
        if not isinstance(layer, Conv):
            continue
        fan_in = channels[layer.source] * layer.k * layer.k
        scale = math.sqrt(2.0 / fan_in) if layer.relu else 0.01
        params[f"{layer.name}.w"] = (rng.standard_normal((layer.cout, channels[layer.source], layer.k, layer.k)) * scale).astype(DTYPE)
        params[f"{layer.name}.b"] = np.zeros(layer.cout, DTYPE)
    params["cls.b"][:] = -math.log((1 - PRIOR) / PRIOR)
    return params


def grow_classes(params: dict[str, np.ndarray], classes: int, rng: np.random.Generator) -> dict[str, np.ndarray]:
    """The weights with the class head grown to `classes` (new classes start untrained, the others as they were)."""
    w, b = params["cls.w"], params["cls.b"]
    have = w.shape[0]
    if classes <= have:
        return params
    out = dict(params)
    extra = (rng.standard_normal((classes - have,) + w.shape[1:]) * 0.01).astype(np.float32)
    out["cls.w"] = np.concatenate([w, extra])
    out["cls.b"] = np.concatenate([b, np.full(classes - have, -math.log((1 - PRIOR) / PRIOR), np.float32)])
    return out


def _taps(w: np.ndarray) -> np.ndarray:
    """ONNX weights (out, in, kh, kw) as one matrix (in × out) per kernel tap: (kh·kw, in, out)."""
    cout, cin, k, _ = w.shape
    return np.ascontiguousarray(w.transpose(2, 3, 1, 0).reshape(k * k, cin, cout))


def _patches(x: np.ndarray, k: int, stride: int) -> tuple[np.ndarray, int, int]:
    """
    The input under each kernel tap (padded by k // 2): (kh·kw, output
    pixels, channels). Tap by tap, each a contiguous copy (much faster than
    one row of all taps per pixel when the channels are few).
    """
    n, h, w, c = x.shape
    if k == 1 and stride == 1:
        return x.reshape(1, n * h * w, c), h, w
    p = k // 2
    ho, wo = (h + 2 * p - k) // stride + 1, (w + 2 * p - k) // stride + 1
    xp = np.pad(x, ((0, 0), (p, p), (p, p), (0, 0)))
    cols = np.empty((k * k, n, ho, wo, c), DTYPE)
    for i in range(k):
        for j in range(k):
            cols[i * k + j] = xp[:, i:i + stride * (ho - 1) + 1:stride, j:j + stride * (wo - 1) + 1:stride, :]
    return cols.reshape(k * k, n * ho * wo, c), ho, wo


def _conv(cols: np.ndarray, taps: np.ndarray) -> np.ndarray:
    out = cols[0] @ taps[0]
    for t in range(1, len(taps)):
        out += cols[t] @ taps[t]
    return out


def _unpatch(g: np.ndarray, taps: np.ndarray, shape: tuple, k: int, stride: int, ho: int, wo: int) -> np.ndarray:
    """The gradient of the input from that of the output (rows of output pixels)."""
    n, h, w, c = shape
    if k == 1 and stride == 1:
        return (g @ taps[0].T).reshape(shape)
    p = k // 2
    dxp = np.zeros((n, h + 2 * p, w + 2 * p, c), DTYPE)
    for i in range(k):
        for j in range(k):
            dxp[:, i:i + stride * (ho - 1) + 1:stride, j:j + stride * (wo - 1) + 1:stride, :] += (g @ taps[i * k + j].T).reshape(n, ho, wo, c)
    return dxp[:, p:p + h, p:p + w, :]


class Network:
    """The layers and their weights; `forward` keeps what `backward` needs."""

    def __init__(self, layers: list, params: dict[str, np.ndarray]):
        self.layers = layers
        self.params = params
        self._cache: dict = {}

    def forward(self, x: np.ndarray, keep: bool = False) -> dict[str, np.ndarray]:
        """The heads (NHWC) for a batch `x` (NHWC float32); `keep` to call `backward` after."""
        values = {"x": x.astype(DTYPE, copy=False)}
        cache: dict = {}
        for layer in self.layers:
            if isinstance(layer, Conv):
                src = values[layer.source]
                cols, ho, wo = _patches(src, layer.k, layer.stride)
                out = _conv(cols, _taps(self.params[f"{layer.name}.w"]))
                out += self.params[f"{layer.name}.b"]
                if layer.relu:
                    np.maximum(out, 0, out=out)
                out = out.reshape(src.shape[0], ho, wo, layer.cout)
                if keep:
                    cache[layer.name] = (cols, src.shape, ho, wo)
                values[layer.name] = out
            elif isinstance(layer, Up):
                values[layer.name] = values[layer.source].repeat(2, axis=1).repeat(2, axis=2)
            else:
                values[layer.name] = np.concatenate([values[s] for s in layer.sources], axis=3)
        if keep:
            self._cache = {"values": values, "layers": cache}
        return {h: values[h] for h in HEADS}

    def backward(self, grads: dict[str, np.ndarray]) -> dict[str, np.ndarray]:
        """The weights' gradients from those of the heads (as `forward` gave them)."""
        values, cache = self._cache["values"], self._cache["layers"]
        d: dict[str, np.ndarray] = {h: grads[h] for h in HEADS}
        out: dict[str, np.ndarray] = {}

        def add(name: str, g: np.ndarray) -> None:
            if name == "x":
                return
            d[name] = d[name] + g if name in d else g

        for layer in reversed(self.layers):
            g = d.pop(layer.name, None)
            if g is None:
                continue
            if isinstance(layer, Conv):
                if layer.relu:
                    g = g * (values[layer.name] > 0)
                cols, shape, ho, wo = cache[layer.name]
                gm = g.reshape(-1, layer.cout)
                w = self.params[f"{layer.name}.w"]
                cout, cin, k, _ = w.shape
                dtaps = np.stack([cols[t].T @ gm for t in range(k * k)])
                out[f"{layer.name}.w"] = dtaps.reshape(k, k, cin, cout).transpose(3, 2, 0, 1)
                out[f"{layer.name}.b"] = gm.sum(axis=0)
                if layer.source != "x":
                    add(layer.source, _unpatch(gm, _taps(w), shape, k, layer.stride, ho, wo))
            elif isinstance(layer, Up):
                n, h, w, c = g.shape
                add(layer.source, g.reshape(n, h // 2, 2, w // 2, 2, c).sum(axis=(2, 4)))
            else:
                a = values[layer.sources[0]].shape[3]
                add(layer.sources[0], g[..., :a])
                add(layer.sources[1], g[..., a:])
        self._cache = {}
        return out


class Adam:
    """AdamW: Adam with weight decay on the weights (not the biases)."""

    def __init__(self, params: dict[str, np.ndarray], lr: float = 1e-3, decay: float = 1e-4, betas=(0.9, 0.999), eps: float = 1e-8):
        self.lr, self.decay, self.betas, self.eps = lr, decay, betas, eps
        self.m = {k: np.zeros_like(v) for k, v in params.items()}
        self.v = {k: np.zeros_like(v) for k, v in params.items()}
        self.t = 0

    def step(self, params: dict[str, np.ndarray], grads: dict[str, np.ndarray], lr: Optional[float] = None, clip: float = 10.0) -> float:
        """One update; the gradients are clipped to a norm of `clip`. Gives the norm before clipping."""
        lr = self.lr if lr is None else lr
        norm = math.sqrt(sum(float(np.vdot(g, g)) for g in grads.values()))
        scale = clip / norm if clip and norm > clip else 1.0
        self.t += 1
        b1, b2 = self.betas
        c1, c2 = 1 - b1 ** self.t, 1 - b2 ** self.t
        for k, g in grads.items():
            g = g * scale if scale != 1.0 else g
            m, v = self.m[k], self.v[k]
            m *= b1
            m += (1 - b1) * g
            v *= b2
            v += (1 - b2) * g * g
            p = params[k]
            if self.decay and k.endswith(".w"):
                p -= lr * self.decay * p
            p -= lr * (m / c1) / (np.sqrt(v / c2) + self.eps)
        return norm


def grid_centres(height: int, width: int) -> np.ndarray:
    """The centres of the output cells, in cells: (2, h, w), x then y."""
    gy, gx = np.mgrid[0:height, 0:width].astype(np.float32)
    return np.stack([gx + 0.5, gy + 0.5])


def yolo_output(heads: dict[str, np.ndarray]) -> np.ndarray:
    """
    The heads as one YOLOv8 output, `[N, 4 + classes, cells]` (centre x,
    centre y, width, height in input pixels, then the class scores), as the
    ONNX model gives it: so the plugin's object detection reads it as any
    YOLOv8 model.
    """
    cls, off, size = heads["cls"], heads["off"], heads["size"]
    n, h, w, _ = cls.shape
    centres = grid_centres(h, w).transpose(1, 2, 0)[None]
    with np.errstate(over="ignore"):
        xy = (off + centres) * STRIDE
        wh = np.exp(size) * STRIDE
        scores = 1 / (1 + np.exp(-cls))
    out = np.concatenate([xy, wh, scores], axis=3)
    return out.reshape(n, h * w, -1).transpose(0, 2, 1)
