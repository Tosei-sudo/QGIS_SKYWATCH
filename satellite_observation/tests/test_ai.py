"""
The AI tools without QGIS or ONNX Runtime, as the viewer's test/ai.test.ts
and test/ai-split.test.ts: the models' settings, tiles, decoding of every
output layout, NMS, mask outlines and Segment Anything's tensors.
"""
import math

import numpy as np
import pytest

from satellite_observation.core.ai.detect import (
    PAD, Detection, blocks_of, box_corners, decode_detectron2, decode_yolo, detect_tile, detectron2_outputs, instance_mask, layout_of,
    nms, overlap, tile_starts, tile_tensor, tiles_of, yolox_grid,
)
from satellite_observation.core.ai.mask_outline import mask_outline
from satellite_observation.core.ai.model_config import (
    COCO_CLASSES, DetectModelConfig, OnnxSignature, OnnxValue, SamModelConfig, ai_model_of, detector_spec, python_names,
)
from satellite_observation.core.ai.sam import SamPoint, part_at, sam_mask, sam_prompt, sam_scale, sam_tensor
from satellite_observation.core.config import parse_config


def spec(**over):
    s = detector_spec(None, {})
    for k, v in over.items():
        setattr(s, k, v)
    return s


# The models' settings.

def test_ultralytics_metadata_gives_task_size_and_classes():
    meta = {"task": "detect", "imgsz": "[64, 64]", "names": "{0: 'bright', 1: 'dark'}"}
    s = detector_spec(None, meta)
    assert (s.task, s.input_size, s.classes, s.mean, s.std) == ("detect", (64, 64), ["bright", "dark"], (0, 0, 0), (255, 255, 255))


def test_class_names_as_python_json_or_a_list():
    assert python_names("{0: 'small-vehicle', 1: \"ship\", 3: 'it\\'s'}") == ["small-vehicle", "ship", "class 2", "it's"]
    assert python_names('{"0": "a", "1": "b"}') == ["a", "b"]
    assert python_names("['plane', 'ship']") == ["plane", "ship"]


def test_the_settings_override_the_metadata_which_overrides_the_defaults():
    meta = {"task": "obb", "imgsz": "[1024, 768]", "names": "{0: 'plane'}"}
    s = detector_spec(None, meta)
    assert (s.task, s.input_size, s.classes) == ("obb", (768, 1024), ["plane"])
    s = detector_spec(DetectModelConfig("", "", task="detect", input_size=(320, 320), classes=["x", "y"], score=0.5), meta)
    assert (s.task, s.input_size, s.classes, s.score) == ("detect", (320, 320), ["x", "y"], 0.5)


def test_detectron2_is_known_by_its_outputs():
    # export_model.py --format onnx --export-method tracing: traced at one size, image size as a last output.
    signature = OnnxSignature(
        [OnnxValue("image", "tensor(float)", [3, 800, 1216])],
        [OnnxValue("3305", "tensor(float)", ["n", "four"]), OnnxValue("3306", "tensor(float)", ["n"]),
         OnnxValue("3307", "tensor(int64)", ["n"]), OnnxValue("3308", "tensor(int64)", [2])],
    )
    s = detector_spec(None, {}, signature)
    assert (s.format, s.unbatched, s.input_size, s.bgr, s.mean, s.std, s.score, s.max_detections) == \
        ("detectron2", True, (1216, 800), True, (0, 0, 0), (1, 1, 1), 0.5, 100)
    s = detector_spec(DetectModelConfig("", "", format="detectron2", input_size=(640, 640)), {})
    assert (s.format, s.unbatched, s.input_size, s.bgr) == ("detectron2", False, (640, 640), True)
    yolo = OnnxSignature([OnnxValue("images", "tensor(float)", [1, 3, 640, 640])], [OnnxValue("output0", "tensor(float)", [1, 84, 8400])])
    s = detector_spec(None, {}, yolo)
    assert (s.format, s.unbatched, s.bgr, s.input_size) == ("auto", False, False, (640, 640))


def test_coco_classes_and_yolox_settings():
    problems = []
    m = ai_model_of({"url": "./models/yolox.onnx", "format": "yolox", "classes": "coco", "channels": "bgr", "strides": [8, 16, 32]}, problems, 0)
    assert problems == [] and (m.format, m.channels, m.strides, m.classes) == ("yolox", "bgr", [8, 16, 32], COCO_CLASSES)
    assert len(COCO_CLASSES) == 80
    ai_model_of({"url": "a.onnx", "classes": "voc", "channels": "hsv", "strides": [0]}, problems, 1)
    assert len(problems) == 3


def test_ai_models_in_the_settings_file():
    config, problems = parse_config({"aiModels": [
        {"label": "車両", "url": "./models/vehicles.onnx", "inputSize": 1024, "classes": ["car", "truck"], "score": 0.3},
        {"label": "SAM", "encoder": "https://models.example/sam-encoder.onnx", "decoder": "https://models.example/sam-decoder.onnx"},
        {"label": "bad", "url": "javascript:alert(1)"},
        {"url": "./a.onnx", "task": "pose", "score": 2},
        {"label": "Windows", "url": "C:\\models\\ships.onnx"},
    ]})
    assert config.ai_models == [
        DetectModelConfig("車両", "./models/vehicles.onnx", input_size=(1024, 1024), classes=["car", "truck"], score=0.3),
        SamModelConfig("SAM", "https://models.example/sam-encoder.onnx", "https://models.example/sam-decoder.onnx"),
        DetectModelConfig("モデル 4", "./a.onnx"),
        DetectModelConfig("Windows", "C:\\models\\ships.onnx"),
    ]
    assert problems == [
        "aiModels[2] の url に ONNX の URL を記述",
        "aiModels[3] の task は detect・obb・segment・sam のいずれかで指定",
        "aiModels[3] の score は 0 と 1 の間の数で指定",
    ]
    assert ai_model_of({"url": "./m.onnx", "std": [0, 1, 1]}, [], 0) == DetectModelConfig("モデル 1", "./m.onnx")


# Tiles.

def test_overlapping_tiles_the_last_at_the_edge():
    assert tile_starts(500, 640, 0.2) == [0]
    assert tile_starts(1000, 640, 0.2) == [0, 360]
    starts = tile_starts(3000, 640, 0.2)
    assert starts[0] == 0 and starts[-1] == 3000 - 640
    assert all(b - a <= 512 for a, b in zip(starts, starts[1:]))
    assert tiles_of(1000, 500, (640, 640), 0.2) == [(0, 0), (360, 0)]


def test_large_pictures_are_read_in_overlapping_blocks():
    assert blocks_of(4000, 2400, 1024) == [(0, 0, 4000, 2400)]
    blocks = blocks_of(20480, 12800, 1024)
    xs = sorted({b[0] for b in blocks})
    assert xs[0] == 0 and xs[-1] == 20480 - 4096 and xs[1] - xs[0] == 4096 - 1024
    assert all(w == 4096 and h == 4096 and x + w <= 20480 and y + h <= 12800 for x, y, w, h in blocks)


def test_input_tensor_pads_outside_the_picture_and_under_transparent_pixels():
    # 2 × 1 picture: red, then transparent.
    rgba = np.array([[[255, 0, 0, 255], [0, 255, 0, 0]]], np.uint8)
    t = tile_tensor(rgba, 0, 0, (2, 2), (0, 0, 0), (255, 255, 255))
    pad = PAD / 255
    assert np.allclose(t.ravel(), [1, pad, pad, pad, 0, pad, pad, pad, 0, pad, pad, pad], atol=1e-3)
    # B, G, R (YOLOX): red lands in the last plane.
    bgr = tile_tensor(rgba, 0, 0, (2, 2), (0, 0, 0), (1, 1, 1), True)
    assert list(bgr.ravel()[[0, 4, 8]]) == [0, 0, 255]


# Decoding.

def test_yolov8_and_yolov5_outputs():
    s = spec(classes=["a", "b"])
    v8 = np.array([10, 50, 20, 60, 4, 8, 6, 9, 0.9, 0.1, 0.05, 0.2], np.float32)
    assert (layout_of((1, 6, 2), s).format, layout_of((1, 6, 2), s).transposed, layout_of((1, 6, 2), s).classes) == ("yolov8", True, 2)
    [d] = decode_yolo((1, 6, 2), v8, s, 0.25)
    assert (d.cls, round(d.score, 5), d.box) == (0, 0.9, (10, 20, 4, 6))
    v5 = np.array([10, 20, 4, 6, 0.5, 0.2, 0.8, 50, 60, 8, 9, 0.1, 0.9, 0.9], np.float32)
    assert layout_of((1, 2, 7), s).format == "yolov5"
    [d] = decode_yolo((1, 2, 7), v5, s, 0.25)
    assert d.cls == 1 and d.score == pytest.approx(0.4)
    # Without class names, the axes tell them apart.
    lay = layout_of((1, 84, 8400), spec())
    assert (lay.format, lay.classes, lay.count) == ("yolov8", 80, 8400)
    lay = layout_of((1, 25200, 85), spec())
    assert (lay.format, lay.classes, lay.count) == ("yolov5", 80, 25200)


def test_the_raw_grid_output_of_yolox():
    s = detector_spec(DetectModelConfig("", "", format="yolox", input_size=(64, 32), classes=["car", "ship"]), {})
    assert (s.std, s.bgr, s.strides) == ((1, 1, 1), True, [8, 16, 32])
    count = 32 + 8 + 2
    assert list(yolox_grid(s.input_size, s.strides, count)[33]) == [1, 0, 16]
    with pytest.raises(ValueError):
        yolox_grid(s.input_size, s.strides, 40)
    data = np.zeros((count, 7), np.float32)
    data[33] = [0.5, 0.25, math.log(2), 0, 0.9, 0.1, 0.8]
    lay = layout_of((1, count, 7), s)
    assert (lay.format, lay.count, lay.classes) == ("yolox", count, 2)
    [d] = decode_yolo((1, count, 7), data, s, 0.25)
    assert d.cls == 1 and d.score == pytest.approx(0.72) and np.allclose(d.box, (24, 4, 32, 16), atol=1e-3)


def test_end_to_end_exports_and_oriented_boxes():
    e2e = np.zeros((300, 6), np.float32)
    e2e[0] = [10, 20, 30, 60, 0.8, 3]
    [d] = decode_yolo((1, 300, 6), e2e, spec(), 0.25)
    assert d.cls == 3 and d.score == pytest.approx(0.8) and d.box == (20, 40, 20, 40)
    [d] = decode_yolo((1, 6, 1), np.array([100, 100, 40, 10, 0.7, math.pi / 2], np.float32), spec(task="obb", classes=["ship"]), 0.25)
    assert d.angle == pytest.approx(math.pi / 2)
    corners = box_corners(d)
    assert max(c[0] for c in corners) - min(c[0] for c in corners) == pytest.approx(10)
    assert max(c[1] for c in corners) - min(c[1] for c in corners) == pytest.approx(40)


def test_detectron2_outputs_found_by_shape_and_type():
    outputs = [("boxes", "tensor(float)", (2, 4)), ("scores", "tensor(float)", (2,)), ("classes", "tensor(int64)", (2,)), ("size", "tensor(int64)", (2,))]
    assert detectron2_outputs(outputs) == ("boxes", "scores", "classes")
    unnamed = [(str(100 + i), t, d) for i, (_, t, d) in enumerate(outputs)]
    assert detectron2_outputs(unnamed)[2] == "102"
    assert detectron2_outputs([("output0", "tensor(float)", (1, 84, 8400))]) is None
    found = decode_detectron2(np.array([[10, 20, 50, 40], [0, 0, 5, 5]]), np.array([0.9, 0.3]), np.array([3, 1], np.int64), 0.5)
    assert [(d.cls, d.score, d.box) for d in found] == [(3, 0.9, (30, 30, 40, 20))]


def test_nms_keeps_the_best_per_class_and_drops_pieces_inside():
    def d(cls, score, box, angle=None):
        return Detection(cls, score, box, angle)

    kept = nms([d(0, 0.9, (50, 50, 20, 20)), d(0, 0.8, (52, 50, 20, 20)), d(1, 0.7, (52, 50, 20, 20)),
                d(0, 0.6, (56, 50, 8, 20)), d(0, 0.5, (100, 100, 20, 20))], 0.45)
    assert [k.score for k in kept] == [0.9, 0.7, 0.5]
    iou, _ = overlap(d(0, 1, (0, 0, 40, 10), 0), d(0, 1, (0, 0, 40, 10), math.pi / 2))
    assert iou == pytest.approx(100 / 700)


def test_instance_masks_from_prototypes_and_coefficients():
    protos = np.array([1, 1, -1, -1] * 4, np.float32).reshape(1, 1, 4, 4)
    mask, x0, y0 = instance_mask(Detection(0, 1, (4, 4, 8, 8), coeffs=np.array([1], np.float32)), protos, (8, 8))
    assert (x0, y0, mask.shape) == (0, 0, (8, 8))
    assert list(mask[0]) == [1, 1, 1, 1, 0, 0, 0, 0]


# Mask outlines.

def grid(rows):
    return np.array([[1 if c == "#" else 0 for c in row] for row in rows], np.uint8)


def test_a_square_is_one_clockwise_ring():
    assert mask_outline(grid(["....", ".##.", ".##.", "...."]), 0, 1) == [[[(1, 1), (3, 1), (3, 3), (1, 3)]]]


def test_holes_stay_with_their_polygon_and_parts_apart():
    polygons = mask_outline(grid(["#####...", "#...#.##", "#...#.##", "#####..."]), 0, 1)
    assert len(polygons) == 2
    ring = next(p for p in polygons if len(p) == 2)
    assert ring[0] == [(0, 0), (5, 0), (5, 4), (0, 4)] and len(ring[1]) == 4
    # Pixels touching only at a corner are two rings.
    assert len(mask_outline(grid(["##..", "##..", "..##", "..##"]), 0, 1)) == 2


def test_a_staircase_is_simplified_into_a_slanted_edge():
    rows = ["#" * (y + 1) + "." * (20 - y - 1) for y in range(20)]
    [[ring]] = mask_outline(grid(rows), 1)
    assert len(ring) <= 5


def test_a_large_mask_outlines_quickly():
    import time

    yy, xx = np.mgrid[0:1500, 0:1500]
    disc = ((xx - 750) ** 2 + (yy - 750) ** 2 < 600 ** 2).astype(np.uint8)
    started = time.monotonic()
    [[ring]] = mask_outline(disc, 1)
    assert time.monotonic() - started < 5 and len(ring) > 20


# Segment Anything.

def test_sam_scale_prompt_and_tensor():
    assert sam_scale(2000, 1000) == (0.512, 1024, 512)
    coords, labels = sam_prompt([SamPoint(100, 50, True), SamPoint(10, 10, False)], 0.5)
    assert coords.ravel().tolist() == [50, 25, 5, 5, 0, 0] and labels.ravel().tolist() == [1, 0, -1]
    t = sam_tensor(np.array([[[124, 116, 104, 255]]], np.uint8), 4)
    assert t.shape == (3, 4, 4) and np.abs(t).max() < 0.01


def test_sam_takes_the_best_mask_sampling_a_low_resolution_one():
    masks = np.full((1, 2, 4, 4), -1, np.float32)
    masks[0, 1] = [[5 if x < 2 else -5 for x in range(4)] for _ in range(4)]
    mask = sam_mask(masks, np.array([[0.2, 0.9]]), 8, 4, 2, 16)
    assert list(mask[0]) == [1, 1, 1, 1, 0, 0, 0, 0]
    full = sam_mask(np.ones((1, 1, 4, 8), np.float32), None, 8, 4, 2, 16)
    assert full.all()


def test_the_part_under_the_click():
    mask = np.array([[1, 1, 0], [1, 1, 0], [0, 0, 1]], np.uint8)
    assert part_at(mask, 0, 0).tolist() == [[1, 1, 0], [1, 1, 0], [0, 0, 0]]
    assert part_at(mask, 2, 0).tolist() == mask.tolist()


# A tile at the model's limit of objects (ai-split.test.ts).

def capped_model(cap):
    """A stand-in Detectron2 model keeping its best `cap` objects: each bright blob of its [1, 3, 64, 64] input, leftmost first."""
    runs = []

    def run(feed):
        runs.append(1)
        image = feed.reshape(3, 64, 64)[0]
        seen = np.zeros((64, 64), bool)
        blobs = []
        for start in range(64 * 64):
            y, x = divmod(start, 64)
            if seen[y, x] or image[y, x] < 128:
                continue
            box = [64, 64, 0, 0]
            stack = [(y, x)]
            seen[y, x] = True
            while stack:
                py, px = stack.pop()
                box = [min(box[0], px), min(box[1], py), max(box[2], px + 1), max(box[3], py + 1)]
                for qy, qx in ((py, px - 1), (py, px + 1), (py - 1, px), (py + 1, px)):
                    if 0 <= qy < 64 and 0 <= qx < 64 and not seen[qy, qx] and image[qy, qx] >= 128:
                        seen[qy, qx] = True
                        stack.append((qy, qx))
            blobs.append(box)
        kept = sorted(blobs)[:cap]
        n = len(kept)
        return {"boxes": np.array(kept, np.float32).reshape(n, 4), "classes": np.zeros(n, np.int64),
                "scores": np.array([0.5 + (b[2] - b[0]) * (b[3] - b[1]) / 100 for b in kept], np.float32), "image_size": np.array([64, 64], np.int64)}

    return run, runs


def crowded_tile():
    """A 64 × 64 tile with 16 white 6-pixel squares on a black ground."""
    v = np.zeros((64, 64), np.uint8)
    for y in range(64):
        for x in range(64):
            if 5 <= x % 16 < 11 and 5 <= y % 16 < 11:
                v[y, x] = 255
    rgba = np.zeros((64, 64, 4), np.uint8)
    rgba[..., 0] = rgba[..., 1] = rgba[..., 2] = v
    rgba[..., 3] = 255
    return rgba


TYPES = {"boxes": "tensor(float)", "classes": "tensor(int64)", "scores": "tensor(float)", "image_size": "tensor(int64)"}


def split_spec(max_detections):
    s = detector_spec(DetectModelConfig("", "", format="detectron2", input_size=(64, 64)), {})
    s.max_detections = max_detections
    return s


def test_a_tile_at_the_limit_is_analysed_again_in_quarters():
    run, runs = capped_model(8)
    found = nms(detect_tile(run, TYPES, crowded_tile(), 0, 0, split_spec(8)), 0.45)
    assert len(found) == 16 and sorted(d.box for d in found)[0] == (8, 8, 6, 6) and len(runs) > 1


def test_a_tile_is_not_split_without_a_limit_or_under_it():
    run, runs = capped_model(6)
    assert len(detect_tile(run, TYPES, crowded_tile(), 0, 0, split_spec(0))) == 6 and len(runs) == 1
    run, runs = capped_model(100)
    assert len(detect_tile(run, TYPES, crowded_tile(), 0, 0, split_spec(100))) == 16 and len(runs) == 1
