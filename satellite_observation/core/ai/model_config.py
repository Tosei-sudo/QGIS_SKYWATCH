"""
AI models brought in from outside (trained elsewhere, exported to ONNX), as
the viewer reads them (ai/model-config.ts): what the settings file's
`aiModels` says of them, and what an Ultralytics export says of itself in
its metadata (class names, input size, task), so a YOLO model chosen as a
file needs no settings at all.

Two kinds of model:
- detection: one ONNX file (YOLOv5 / v8 / v11 and later, end-to-end
  exports, rotated boxes (OBB) and instance segmentation, YOLOX's raw grid
  output, and R-CNN style outputs of boxes, scores and classes apart, as
  Detectron2's ONNX export writes them), run over an image in tiles;
- click segmentation: a Segment Anything (SAM, MobileSAM, EfficientSAM…)
  image encoder and prompt decoder, as exported by segment-anything's
  `export_onnx_model.py`.

ONNX Runtime gives a model's metadata and the shapes of its inputs and
outputs itself, so the viewer's protobuf readers are not needed here.
"""
from __future__ import annotations

import math
import re
from dataclasses import dataclass, field
from typing import Any, Optional, Union

DETECT_TASKS = ("detect", "obb", "segment")
#: How a detection model's outputs are laid out (`auto`: from their shapes).
DETECT_FORMATS = ("auto", "yolov8", "yolov5", "end2end", "yolox", "detectron2")

#: The 80 classes of COCO, which most published detection weights were trained on (`"classes": "coco"`).
COCO_CLASSES = [
    "person", "bicycle", "car", "motorcycle", "airplane", "bus", "train", "truck", "boat", "traffic light",
    "fire hydrant", "stop sign", "parking meter", "bench", "bird", "cat", "dog", "horse", "sheep", "cow",
    "elephant", "bear", "zebra", "giraffe", "backpack", "umbrella", "handbag", "tie", "suitcase", "frisbee",
    "skis", "snowboard", "sports ball", "kite", "baseball bat", "baseball glove", "skateboard", "surfboard", "tennis racket", "bottle",
    "wine glass", "cup", "fork", "knife", "spoon", "bowl", "banana", "apple", "sandwich", "orange",
    "broccoli", "carrot", "hot dog", "pizza", "donut", "cake", "chair", "couch", "potted plant", "bed",
    "dining table", "toilet", "tv", "laptop", "mouse", "remote", "keyboard", "cell phone", "microwave", "oven",
    "toaster", "sink", "refrigerator", "book", "clock", "vase", "scissors", "teddy bear", "hair drier", "toothbrush",
]


@dataclass
class DetectModelConfig:
    """A detection model of the settings file (what is not given comes from the model, then YOLO's defaults)."""
    label: str
    url: str
    task: Optional[str] = None
    #: Width, height.
    input_size: Optional[tuple[int, int]] = None
    classes: Optional[list[str]] = None
    #: Per channel (R, G, B, in 0–255): the input is (value − mean) / std.
    mean: Optional[tuple[float, float, float]] = None
    std: Optional[tuple[float, float, float]] = None
    #: "rgb" or "bgr".
    channels: Optional[str] = None
    format: Optional[str] = None
    strides: Optional[list[int]] = None
    #: The most objects the model gives for one input (0: no limit); a tile reaching it is analysed again in quarters.
    max_detections: Optional[int] = None
    score: Optional[float] = None
    iou: Optional[float] = None
    kind: str = "detect"


@dataclass
class SamModelConfig:
    """A click-segmentation model (encoder and decoder) of the settings file."""
    label: str
    encoder: str
    decoder: str
    #: The encoder's input side (default 1024).
    input_size: Optional[int] = None
    mean: Optional[tuple[float, float, float]] = None
    std: Optional[tuple[float, float, float]] = None
    kind: str = "sam"


AiModelConfig = Union[DetectModelConfig, SamModelConfig]

_URL = re.compile(r"^(https?:)?//|^\.{0,2}/|^[\w-][\w./-]*\.onnx(\?.*)?$|^[A-Za-z]:[\\/]|^\\\\", re.IGNORECASE)


def _is_url(v: Any) -> bool:
    """An http(s) URL, a path (absolute, or relative to the settings file) or a bare `name.onnx`."""
    return isinstance(v, str) and bool(_URL.search(v.strip()))


def _number(v: Any) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v)


def _integer(v: Any) -> bool:
    return isinstance(v, int) and not isinstance(v, bool)


def _triple(v: Any) -> bool:
    return isinstance(v, list) and len(v) == 3 and all(_number(x) for x in v)


def _fraction(v: Any) -> bool:
    return _number(v) and 0 < v < 1


def ai_model_of(value: Any, problems: list[str], index: int) -> Optional[AiModelConfig]:
    """One `aiModels` entry; None (with the reason in `problems`) when it cannot be used."""
    at = f"aiModels[{index}]"
    if not isinstance(value, dict):
        problems.append(f"{at} はオブジェクトで指定")
        return None
    label = value["label"] if isinstance(value.get("label"), str) and value["label"] else f"モデル {index + 1}"
    normalize: dict[str, tuple] = {}
    for key in ("mean", "std"):
        if value.get(key) is None:
            continue
        if _triple(value[key]) and (key == "mean" or all(x != 0 for x in value[key])):
            normalize[key] = tuple(float(x) for x in value[key])
        else:
            problems.append(f"{at} の {key} は R・G・B の 3 つの数で指定")

    if value.get("encoder") is not None or value.get("decoder") is not None or value.get("task") == "sam":
        if not _is_url(value.get("encoder")) or not _is_url(value.get("decoder")):
            problems.append(f"{at} の encoder と decoder に ONNX の URL を記述")
            return None
        size = value.get("inputSize")
        return SamModelConfig(label, value["encoder"].strip(), value["decoder"].strip(),
                              size if _integer(size) and 32 <= size <= 4096 else None, normalize.get("mean"), normalize.get("std"))

    if not _is_url(value.get("url")):
        problems.append(f"{at} の url に ONNX の URL を記述")
        return None
    model = DetectModelConfig(label, value["url"].strip(), mean=normalize.get("mean"), std=normalize.get("std"))
    if value.get("task") is not None:
        if value["task"] in DETECT_TASKS:
            model.task = value["task"]
        else:
            problems.append(f"{at} の task は detect・obb・segment・sam のいずれかで指定")
    if value.get("inputSize") is not None:
        size = [value["inputSize"]] * 2 if _integer(value["inputSize"]) else value["inputSize"]
        if isinstance(size, list) and len(size) == 2 and all(_integer(s) and 16 <= s <= 4096 for s in size):
            model.input_size = (size[0], size[1])
        else:
            problems.append(f"{at} の inputSize は 640 か [幅, 高さ] で指定")
    if value.get("classes") is not None:
        if value["classes"] == "coco":
            model.classes = list(COCO_CLASSES)
        elif isinstance(value["classes"], list) and all(isinstance(c, str) for c in value["classes"]):
            model.classes = list(value["classes"])
        else:
            problems.append(f"{at} の classes はクラス名の配列か \"coco\" で指定")
    if value.get("format") is not None:
        if value["format"] in DETECT_FORMATS:
            model.format = value["format"]
        else:
            problems.append(f"{at} の format は {'・'.join(DETECT_FORMATS)} のいずれかで指定")
    if value.get("channels") is not None:
        if value["channels"] in ("rgb", "bgr"):
            model.channels = value["channels"]
        else:
            problems.append(f"{at} の channels は rgb か bgr で指定")
    if value.get("maxDetections") is not None:
        if _integer(value["maxDetections"]) and value["maxDetections"] >= 0:
            model.max_detections = value["maxDetections"]
        else:
            problems.append(f"{at} の maxDetections は 0 以上の整数で指定（0: 上限なし）")
    if value.get("strides") is not None:
        s = value["strides"]
        if isinstance(s, list) and s and all(_integer(n) and n > 0 for n in s):
            model.strides = list(s)
        else:
            problems.append(f"{at} の strides は [8, 16, 32] のような正の整数の配列で指定")
    for key in ("score", "iou"):
        if value.get(key) is None:
            continue
        if _fraction(value[key]):
            setattr(model, key, float(value[key]))
        else:
            problems.append(f"{at} の {key} は 0 と 1 の間の数で指定")
    return model


@dataclass
class OnnxValue:
    """A graph input or output: its name, element type (ONNX Runtime's `tensor(float)` …) and shape (a name or None for a free axis)."""
    name: str
    type: str
    dims: list


@dataclass
class OnnxSignature:
    inputs: list[OnnxValue] = field(default_factory=list)
    outputs: list[OnnxValue] = field(default_factory=list)


def _float_type(t: str) -> bool:
    return t in ("tensor(float)", "tensor(float16)", "tensor(double)")


@dataclass
class DetectorSpec:
    """Everything a detection run needs to know of its model."""
    task: str
    #: Width and height of the model's input.
    input_size: tuple[int, int]
    #: Class names; may be empty when neither the model nor the settings give them.
    classes: list[str]
    mean: tuple[float, float, float]
    std: tuple[float, float, float]
    #: The input's channels are B, G, R.
    bgr: bool
    format: str
    strides: list[int]
    score: float
    iou: float
    #: The input is one image, `[3, H, W]`, not a batch `[1, 3, H, W]`.
    unbatched: bool = False
    #: The most objects the model gives for one input (0: no limit).
    max_detections: int = 0
    #: The bands (1 first) the model takes as they are, scaled by `mean` and `std` per band (a model trained
    #: here on band values); empty for the image as shown (RGB).
    bands: list[int] = field(default_factory=list)


def trained_input(metadata: dict[str, str]) -> Optional[dict]:
    """The input of a model trained by this plugin (`satobs` metadata, see core/ai/train): mode, bands, mean, std."""
    import json

    try:
        state = json.loads(metadata.get("satobs", ""))
    except ValueError:
        return None
    value = state.get("input") if isinstance(state, dict) else None
    return value if isinstance(value, dict) else None


def detector_spec(config: Optional[DetectModelConfig], metadata: dict[str, str], signature: Optional[OnnxSignature] = None) -> DetectorSpec:
    """
    The spec of a detection model: what the settings file says, then what the
    model's own metadata says (Ultralytics: `task`, `imgsz`, `names`), then
    YOLO's defaults.
    """
    c = config or DetectModelConfig("", "")
    task = c.task or (metadata.get("task") if metadata.get("task") in DETECT_TASKS else "detect")
    imgsz = parse_numbers(metadata.get("imgsz"))
    # Ultralytics writes imgsz as [height, width].
    from_meta = (int(imgsz[1]), int(imgsz[0])) if len(imgsz) == 2 else (int(imgsz[0]),) * 2 if len(imgsz) == 1 else None
    # A fixed input size (an export traced at one size, as Detectron2's is), else none.
    first = signature.inputs[0] if signature and signature.inputs else None
    hw = first.dims[-2:] if first and len(first.dims) >= 2 else []
    fixed = (hw[1], hw[0]) if len(hw) == 2 and all(isinstance(d, int) and d > 0 for d in hw) else None
    fmt = c.format or "auto"
    detectron2 = fmt == "detectron2" or (fmt == "auto" and signature is not None and has_detectron2_outputs(signature))
    # YOLOX and Detectron2 take 0–255 B, G, R (Detectron2 normalizes inside the model).
    raw = fmt == "yolox" or detectron2
    trained = trained_input(metadata) or {}
    bands = [int(b) for b in trained.get("bands", [])] if trained.get("mode") == "bands" else []
    if bands:
        mean = [float(v) for v in trained.get("mean", [])]
        std = [float(v) for v in trained.get("std", [])]
        if len(mean) != len(bands) or len(std) != len(bands):
            mean, std = [0.0] * len(bands), [1.0] * len(bands)
    spec = DetectorSpec(
        task="detect" if detectron2 else task,
        # Detectron2's models are trained with the shorter side about 800.
        input_size=c.input_size or from_meta or fixed or ((800, 800) if detectron2 else (640, 640)),
        classes=list(c.classes) if c.classes is not None else python_names(metadata.get("names", "")),
        mean=c.mean or (0.0, 0.0, 0.0),
        std=c.std or ((1.0, 1.0, 1.0) if raw else (255.0, 255.0, 255.0)),
        bgr=(c.channels or ("bgr" if raw else "rgb")) == "bgr",
        format="detectron2" if detectron2 else fmt,
        strides=list(c.strides or [8, 16, 32]),
        score=c.score if c.score is not None else (0.5 if detectron2 else 0.25),
        iou=c.iou if c.iou is not None else 0.45,
        unbatched=bool(first) and len(first.dims) == 3,
        # Detectron2 keeps the best 100 of an image unless told otherwise when exported.
        max_detections=c.max_detections if c.max_detections is not None else (100 if detectron2 else 0),
    )
    if bands:
        spec.bands, spec.mean, spec.std, spec.bgr = bands, tuple(mean), tuple(std), False
    return spec


def has_detectron2_outputs(signature: OnnxSignature) -> bool:
    """Whether a model's outputs are Detectron2's: boxes `[N, 4]`, and scores and classes `[N]` (float, integer)."""
    outs = signature.outputs
    return (
        # (A traced export may leave the 4 as a free axis.)
        any(_float_type(o.type) and len(o.dims) == 2 and (o.dims[1] == 4 or not isinstance(o.dims[1], int)) for o in outs)
        and any(_float_type(o.type) and len(o.dims) == 1 for o in outs)
        and any(not _float_type(o.type) and len(o.dims) == 1 for o in outs)
    )


def parse_numbers(text: Optional[str]) -> list[float]:
    """The numbers in `[640, 640]` or `640`."""
    return [float(n) for n in re.findall(r"\d+(?:\.\d+)?", text or "") if float(n) > 0]


def _unescape(s: str) -> str:
    return re.sub(r"\\(.)", r"\1", s)


def python_names(text: str) -> list[str]:
    """Class names from Ultralytics' metadata: a Python dict as text, `{0: 'person', 1: 'bicycle'}` (or JSON, or a list)."""
    names: dict[int, str] = {}
    for m in re.finditer(r"[\"']?(\d+)[\"']?\s*:\s*(?:'((?:[^'\\]|\\.)*)'|\"((?:[^\"\\]|\\.)*)\")", text):
        names[int(m.group(1))] = _unescape(m.group(2) if m.group(2) is not None else m.group(3))
    if names:
        return [names.get(i, f"class {i}") for i in range(max(names) + 1)]
    # A list: ['a', 'b'] or ["a", "b"].
    return [_unescape(m.group(1) if m.group(1) is not None else m.group(2)) for m in re.finditer(r"'((?:[^'\\]|\\.)*)'|\"((?:[^\"\\]|\\.)*)\"", text)]
