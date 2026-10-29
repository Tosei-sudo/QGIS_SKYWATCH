"""
The trained network written as an ONNX model, and read back to train it
further, without the `onnx` package: ONNX is protocol buffers, and the few
messages needed here (model, graph, node, attribute, tensor, value info,
metadata) are written and read by hand.

The model gives YOLOv8's output (`[1, 4 + classes, cells]`: box centre and
size in input pixels, then the class scores) and carries Ultralytics'
metadata (`task`, `imgsz`, `names`), so the plugin's 物体検出（AI） runs it
as any YOLOv8 model; `satobs` holds what training needs to carry on (the
architecture, the input's bands and their statistics, the history).
"""
from __future__ import annotations

import json
import struct
from typing import Any, Iterator, Optional

import numpy as np

from .net import STRIDE, Conv, Up, grid_centres

#: ONNX's element types.
FLOAT, INT64 = 1, 7
OPSET = 13
IR_VERSION = 7
#: The metadata key of the training state.
META_KEY = "satobs"
FORMAT = "satobs-detector"


# Writing protocol buffers.

def _varint(n: int) -> bytes:
    if n < 0:
        n += 1 << 64
    out = bytearray()
    while True:
        b = n & 0x7F
        n >>= 7
        if n:
            out.append(b | 0x80)
        else:
            out.append(b)
            return bytes(out)


def _key(field: int, wire: int) -> bytes:
    return _varint(field << 3 | wire)


def _int(field: int, n: int) -> bytes:
    return _key(field, 0) + _varint(n)


def _bytes(field: int, data: bytes) -> bytes:
    return _key(field, 2) + _varint(len(data)) + data


def _str(field: int, text: str) -> bytes:
    return _bytes(field, text.encode("utf-8"))


def _float(field: int, v: float) -> bytes:
    return _key(field, 5) + struct.pack("<f", v)


def tensor(name: str, array: np.ndarray) -> bytes:
    """A TensorProto (float32 or int64, raw data)."""
    a = np.ascontiguousarray(array)
    kind = INT64 if a.dtype == np.int64 else FLOAT
    a = a.astype("<i8" if kind == INT64 else "<f4", copy=False)
    return b"".join(_int(1, d) for d in a.shape) + _int(2, kind) + _str(8, name) + _bytes(9, a.tobytes())


def _attr_ints(name: str, values) -> bytes:
    return _str(1, name) + b"".join(_int(8, v) for v in values) + _int(20, 7)


def _attr_int(name: str, v: int) -> bytes:
    return _str(1, name) + _int(3, v) + _int(20, 2)


def _attr_str(name: str, v: str) -> bytes:
    return _str(1, name) + _bytes(4, v.encode()) + _int(20, 3)


def node(op: str, inputs, outputs, name: str = "", attrs=()) -> bytes:
    return (b"".join(_str(1, i) for i in inputs) + b"".join(_str(2, o) for o in outputs) + _str(3, name or outputs[0])
            + _str(4, op) + b"".join(_bytes(5, a) for a in attrs))


def value_info(name: str, dims) -> bytes:
    shape = b"".join(_bytes(1, _int(1, d) if isinstance(d, int) else _str(2, d)) for d in dims)
    return _str(1, name) + _bytes(2, _bytes(1, _int(1, FLOAT) + _bytes(2, shape)))


def model_bytes(layers: list, params: dict[str, np.ndarray], channels: int, size: tuple[int, int], metadata: dict[str, str]) -> bytes:
    """The ONNX model of the network for an input of `channels` × `size` (width, height)."""
    width, height = size
    gw, gh = width // STRIDE, height // STRIDE
    nodes: list[bytes] = []
    inits: list[bytes] = [tensor(k, v) for k, v in params.items()]
    for layer in layers:
        if isinstance(layer, Conv):
            p = layer.k // 2
            nodes.append(node("Conv", [layer.source if layer.source != "x" else "images", f"{layer.name}.w", f"{layer.name}.b"],
                              [layer.name + ("_pre" if layer.relu else "")], layer.name + "_conv",
                              [_attr_ints("kernel_shape", [layer.k, layer.k]), _attr_ints("strides", [layer.stride] * 2),
                               _attr_ints("pads", [p] * 4)]))
            if layer.relu:
                nodes.append(node("Relu", [layer.name + "_pre"], [layer.name]))
        elif isinstance(layer, Up):
            nodes.append(node("Resize", [layer.source, "", "up_scales"], [layer.name], layer.name,
                              [_attr_str("mode", "nearest"), _attr_str("coordinate_transformation_mode", "asymmetric"),
                               _attr_str("nearest_mode", "floor")]))
        else:
            nodes.append(node("Concat", list(layer.sources), [layer.name], layer.name, [_attr_int("axis", 1)]))
    inits.append(tensor("up_scales", np.array([1, 1, 2, 2], np.float32)))
    inits.append(tensor("grid", grid_centres(gh, gw)[None]))
    inits.append(tensor("stride", np.array(float(STRIDE), np.float32)))
    inits.append(tensor("out_shape", np.array([1, -1, gw * gh], np.int64)))
    classes = params["cls.w"].shape[0]
    nodes += [
        node("Add", ["off", "grid"], ["xy_cells"]),
        node("Mul", ["xy_cells", "stride"], ["xy"]),
        node("Exp", ["size"], ["wh_cells"]),
        node("Mul", ["wh_cells", "stride"], ["wh"]),
        node("Sigmoid", ["cls"], ["scores"]),
        node("Concat", ["xy", "wh", "scores"], ["joined"], attrs=[_attr_int("axis", 1)]),
        node("Reshape", ["joined", "out_shape"], ["output0"]),
    ]
    graph = (b"".join(_bytes(1, n) for n in nodes) + _str(2, "satobs_detector") + b"".join(_bytes(5, t) for t in inits)
             + _bytes(11, value_info("images", [1, channels, height, width]))
             + _bytes(12, value_info("output0", [1, 4 + classes, gw * gh])))
    meta = b"".join(_bytes(14, _str(1, k) + _str(2, v)) for k, v in metadata.items())
    return (_int(1, IR_VERSION) + _str(2, "satellite_observation") + _str(3, "1") + _bytes(7, graph)
            + _bytes(8, _str(1, "") + _int(2, OPSET)) + meta)


# Reading them back.

def _read_varint(data: bytes, i: int) -> tuple[int, int]:
    shift = n = 0
    while True:
        b = data[i]
        i += 1
        n |= (b & 0x7F) << shift
        if not b & 0x80:
            return n, i
        shift += 7


def fields(data: bytes) -> Iterator[tuple[int, int, Any]]:
    """The fields of a message: (number, wire type, value: int, bytes, or the raw 4 / 8 bytes)."""
    i = 0
    while i < len(data):
        key, i = _read_varint(data, i)
        field, wire = key >> 3, key & 7
        if wire == 0:
            v, i = _read_varint(data, i)
        elif wire == 2:
            n, i = _read_varint(data, i)
            v, i = data[i:i + n], i + n
        elif wire == 5:
            v, i = data[i:i + 4], i + 4
        elif wire == 1:
            v, i = data[i:i + 8], i + 8
        else:
            raise ValueError("ONNX の読み込みに失敗（未対応の形式）")
        yield field, wire, v


def _tensor_of(data: bytes) -> tuple[str, np.ndarray]:
    dims, kind, name, raw = [], FLOAT, "", b""
    floats: list[float] = []
    for f, wire, v in fields(data):
        if f == 1:
            if wire == 2:  # packed
                j = 0
                while j < len(v):
                    d, j = _read_varint(v, j)
                    dims.append(d)
            else:
                dims.append(v)
        elif f == 2:
            kind = v
        elif f == 8:
            name = v.decode()
        elif f == 9:
            raw = v
        elif f == 4:
            floats += list(struct.unpack(f"<{len(v) // 4}f", v)) if wire == 2 else [struct.unpack("<f", v)[0]]
    dtype = "<i8" if kind == INT64 else "<f4"
    array = np.frombuffer(raw, dtype).copy() if raw else np.array(floats, dtype)
    return name, array.reshape(dims) if dims else array.reshape(())


def read_model(data: bytes) -> tuple[dict[str, str], dict[str, np.ndarray]]:
    """An ONNX model's metadata and initializers (float32 / int64)."""
    meta: dict[str, str] = {}
    inits: dict[str, np.ndarray] = {}
    for f, _, v in fields(data):
        if f == 14:
            kv = {k: x.decode("utf-8") for k, _, x in fields(v)}
            meta[kv.get(1, "")] = kv.get(2, "")
        elif f == 7:
            for g, _, w in fields(v):
                if g == 5:
                    name, array = _tensor_of(w)
                    inits[name] = array
    return meta, inits


def training_state(meta: dict[str, str]) -> Optional[dict]:
    """What a model trained here says of itself (`satobs` metadata), None for any other model."""
    try:
        state = json.loads(meta.get(META_KEY, ""))
    except ValueError:
        return None
    return state if isinstance(state, dict) and state.get("format") == FORMAT else None


def names_text(classes: list[str]) -> str:
    """Class names as Ultralytics writes them (`{0: "船舶", 1: "航空機"}`)."""
    return "{" + ", ".join(f"{i}: {json.dumps(c, ensure_ascii=False)}" for i, c in enumerate(classes)) + "}"
