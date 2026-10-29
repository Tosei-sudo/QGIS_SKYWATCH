"""
Training without QGIS (core/ai/train): the network's gradients, its ONNX
file (run by ONNX Runtime, read back), the targets and the tiles, labels
from the classification, and a few epochs that learn.
"""
import math

import numpy as np
import pytest

from satellite_observation.core.ai.detect import tile_tensor
from satellite_observation.core.ai.model_config import detector_spec
from satellite_observation.core.ai.train import net as N
from satellite_observation.core.ai.train.labels import ObjectSize, class_of, class_order, footprint, kind_path, object_size
from satellite_observation.core.ai.train.loss import STRIDE, batch_targets, detection_loss, tile_targets
from satellite_observation.core.ai.train.onnx_io import read_model, training_state
from satellite_observation.core.ai.train.trainer import (
    InputSpec, TrainError, TrainOptions, Window, add_classes, band_statistics, check, crop, input_of, load_model, new_model, normalize, sample, split,
    train, turn,
)
from satellite_observation.core.obs.model import default_taxonomy
from satellite_observation.core.obs.taxonomy import Taxon


def small_network(classes=2, cin=3, seed=0):
    rng = np.random.default_rng(seed)
    layers = N.architecture(N.WIDTHS["small"], classes)
    return N.Network(layers, N.init_params(layers, cin, rng))


def test_the_gradients_are_those_of_the_heads(monkeypatch):
    monkeypatch.setattr(N, "DTYPE", np.float64)
    rng = np.random.default_rng(1)
    net = small_network(cin=2)
    net.params = {k: v.astype(np.float64) for k, v in net.params.items()}
    x = rng.standard_normal((2, 32, 32, 2))
    heads = net.forward(x, keep=True)
    weights = {k: rng.standard_normal(v.shape) for k, v in heads.items()}
    grads = net.backward(weights)

    def value():
        h = net.forward(x)
        return sum(float((h[k] * weights[k]).sum()) for k in h)

    for name in ("a1.w", "b1.w", "d2.w", "e1.w", "f1.w", "cls.w", "size.b"):
        d = rng.standard_normal(net.params[name].shape)
        kept = net.params[name].copy()
        net.params[name] = kept + 1e-6 * d
        up = value()
        net.params[name] = kept - 1e-6 * d
        down = value()
        net.params[name] = kept
        assert (up - down) / 2e-6 == pytest.approx(float((grads[name] * d).sum()), rel=1e-5, abs=1e-8)


def test_the_onnx_model_gives_what_numpy_gives():
    ort = pytest.importorskip("onnxruntime")
    rng = np.random.default_rng(2)
    model = new_model(["船舶", "航空機", "車両"], InputSpec("bands", [1, 2, 3, 4], [0.0] * 4, [1.0] * 4), 128, "small")
    for k in model.network.params:
        model.network.params[k] += (rng.standard_normal(model.network.params[k].shape) * 0.05).astype(np.float32)
    data = model.onnx()
    session = ort.InferenceSession(data, providers=["CPUExecutionProvider"])
    x = rng.standard_normal((1, 128, 128, 4)).astype(np.float32)
    out = session.run(None, {"images": x.transpose(0, 3, 1, 2)})[0]
    assert out.shape == (1, 4 + 3, 32 * 32)
    assert np.allclose(out, N.yolo_output(model.network.forward(x)), rtol=1e-4, atol=1e-3)
    # Ultralytics' metadata: 物体検出（AI） reads the model as any YOLOv8 one, the band input from `satobs`.
    meta = session.get_modelmeta().custom_metadata_map
    spec = detector_spec(None, meta)
    assert (spec.task, spec.input_size, spec.classes, spec.bands) == ("detect", (128, 128), ["船舶", "航空機", "車両"], [1, 2, 3, 4])


@pytest.mark.parametrize("arch", ["res", "fpn4"])
def test_the_model_reads_back_to_train_further(arch):
    model = new_model(["船舶"], InputSpec(), 256, "medium", depth=1, arch=arch)
    model.history.append({"epochs": 3})
    for k in model.network.buffers:
        model.network.buffers[k] += 0.5
    back = load_model(model.onnx())
    assert (back.arch, back.classes, back.tile, back.widths, back.depth, back.history, back.input.mode) == \
        (arch, ["船舶"], 256, "medium", 1, [{"epochs": 3}], "rgb")
    assert all(np.array_equal(back.network.params[k], v) for k, v in model.network.params.items())
    assert all(np.array_equal(back.network.buffers[k], v) for k, v in model.network.buffers.items())
    assert bool(model.network.buffers) == (arch == "res")
    meta, _ = read_model(model.onnx())
    assert training_state(meta)["arch"]["widths"] == list((N.RES_WIDTHS if arch == "res" else N.WIDTHS)["medium"])


def test_only_models_trained_here_train_further():
    import os

    with open(os.path.join(os.path.dirname(__file__), "data", "ai", "detect.onnx"), "rb") as f:
        data = f.read()
    with pytest.raises(TrainError, match="このプラグインで学習したモデルのみ"):
        load_model(data)


def test_new_classes_leave_the_old_ones_as_they_were():
    rng = np.random.default_rng(3)
    model = new_model(["船舶", "航空機"], InputSpec(), 128, "small")
    x = rng.standard_normal((1, 128, 128, 3)).astype(np.float32)
    before = model.network.forward(x)["cls"]
    assert add_classes(model, ["航空機", "車両", "船舶"]) == ["車両"]
    after = model.network.forward(x)["cls"]
    assert model.classes == ["船舶", "航空機", "車両"] and after.shape[3] == 3
    assert np.allclose(after[..., :2], before)
    # Read back with three classes.
    assert load_model(model.onnx()).network.params["cls.w"].shape[0] == 3


def test_the_cells_near_an_objects_centre_are_its_positives():
    cls, positive, off, size = tile_targets(np.array([[18.0, 10.0, 8.0, 8.0, 1]]), 8, 8, 2)
    ys, xs = np.nonzero(positive)
    # The centre (cell 4, 2) and the cells within a cell of it inside the box.
    assert (4, 2) in zip(xs, ys) and positive.sum() <= 9 and cls[positive, 1].all() and not cls[..., 0].any()
    assert off[2, 4] == pytest.approx((18 / STRIDE - 4.5, 10 / STRIDE - 2.5))
    assert size[2, 4] == pytest.approx((math.log(2), math.log(2)))
    # Overlapping: the smaller box wins.
    cls, positive, _, _ = tile_targets(np.array([[16.0, 16.0, 30.0, 30.0, 0], [16.0, 16.0, 6.0, 6.0, 1]]), 8, 8, 2)
    assert cls[3, 3, 1] == 1 and cls[3, 3, 0] == 0


def test_the_loss_falls_with_the_right_answer():
    net = small_network()
    x = np.zeros((1, 64, 64, 3), np.float32)
    t = batch_targets([np.array([[20.0, 20.0, 8.0, 8.0, 0]])], 16, 16, 2)
    heads = net.forward(x)
    losses, grads = detection_loss(heads, t)
    right = {"cls": np.where(t.cls > 0, 6.0, -6.0).astype(np.float32), "off": t.off, "size": t.size}
    assert detection_loss(right, t)[0]["total"] < losses["total"] / 10
    assert set(grads) == {"cls", "off", "size"}


def test_a_turned_tile_keeps_its_boxes_on_the_objects():
    x = np.zeros((32, 32, 1), np.float32)
    x[4:8, 20:28] = 1  # an object 8 wide, 4 high, centre (24, 6)
    boxes = np.array([[24.0, 6.0, 8.0, 4.0, 0]], np.float32)
    for k in range(4):
        for flip in (False, True):
            y, b = turn(x, boxes, k, flip)
            cx, cy, w, h, _ = b[0]
            assert y[int(cy), int(cx), 0] == 1
            assert (w, h) == ((8, 4) if k % 2 == 0 else (4, 8))
            assert y[int(cy - h / 2):int(cy + h / 2), int(cx - w / 2):int(cx + w / 2)].all()


def test_a_box_cut_by_the_tile_is_kept_when_mostly_inside():
    pixels = np.zeros((64, 64, 4), np.uint8)
    pixels[..., 3] = 255
    window = Window(pixels, np.array([[30.0, 30.0, 10.0, 10.0, 0], [-2.0, 30.0, 10.0, 10.0, 1]], np.float32))
    _, boxes = crop(window, 0, 0, 32)
    assert boxes[:, 4].tolist() == [0]
    # Clipped at the tile's edge.
    assert boxes[0, :4].tolist() == [28.5, 28.5, 7.0, 7.0]
    _, boxes = crop(window, -4, 0, 32)
    assert sorted(boxes[:, 4].tolist()) == [1]


def test_band_values_are_scaled_as_detection_scales_them():
    rng = np.random.default_rng(4)
    values = rng.normal(500, 50, (40, 40, 2)).astype(np.float32)
    valid = np.ones((40, 40, 1), np.float32)
    valid[:5] = 0
    window = Window(np.concatenate([values, valid], axis=2), np.zeros((0, 5), np.float32))
    mean, std = band_statistics([window])
    assert mean[0] == pytest.approx(values[5:, :, 0].mean(), rel=1e-4)
    spec = InputSpec("bands", [3, 4], mean, std)
    pixels, _ = crop(normalize(window, spec), 3, 2, 32)
    # 物体検出（AI）'s tensor of the same pixels: the same numbers (no data at 0).
    expected = tile_tensor(np.concatenate([values, valid], axis=2), 3, 2, (32, 32), mean, std).transpose(1, 2, 0)
    assert np.allclose(input_of(pixels, spec), expected, atol=2e-3) and not input_of(pixels, spec)[:3].any()


def test_rgb_tiles_are_what_detection_feeds():
    rgba = np.zeros((32, 32, 4), np.uint8)
    rgba[..., :3] = 200
    rgba[:16, ..., 3] = 255
    x, _ = sample(Window(rgba, np.zeros((0, 5), np.float32)), InputSpec(), 32, np.random.default_rng(0), augment=False)
    assert np.allclose(x, tile_tensor(rgba, 0, 0, (32, 32), (0, 0, 0), (255, 255, 255)).transpose(1, 2, 0))


def test_windows_are_held_back_only_when_there_are_enough():
    windows = [Window(np.zeros((8, 8, 4), np.uint8), np.zeros((0, 5), np.float32)) for _ in range(20)]
    a, b = split(windows, 0.15, np.random.default_rng(0))
    assert (len(a), len(b)) == (17, 3)
    assert [len(p) for p in split(windows[:5], 0.15, np.random.default_rng(0))] == [5, 0]


def test_classes_from_the_classification():
    taxonomy = default_taxonomy() + [Taxon(["船舶", "支援船"]), Taxon(["船舶", "支援船", "〇〇級"], specs={"別名": "まるまる"})]
    assert class_of("船舶 > 支援船 > 〇〇級", taxonomy) == "船舶 > 支援船 > 〇〇級"
    assert class_of("船舶 > 支援船 > 〇〇級", taxonomy, depth=1) == "船舶"
    assert class_of("船舶 > 支援船 > 〇〇級", taxonomy, depth=2) == "船舶 > 支援船"
    # A class' own name or other name stands for its path.
    assert kind_path("〇〇級", taxonomy) == ["船舶", "支援船", "〇〇級"] and class_of("まるまる", taxonomy, 1) == "船舶"
    # Only below the kinds asked for.
    assert class_of("航空機", taxonomy, within_kinds=["船舶"]) is None
    assert class_of("〇〇級", taxonomy, within_kinds=["船舶 > 支援船"]) == "船舶 > 支援船 > 〇〇級"
    assert class_of("", taxonomy) is None
    # Old classes first, the new in the classification's order, unknown ones last by name.
    assert class_order(["その他", "zz", "船舶", "航空機"], taxonomy, ["航空機"]) == ["航空機", "船舶", "その他", "zz"]


def test_object_sizes_from_records_then_specifications():
    taxonomy = [Taxon(["船舶"]), Taxon(["船舶", "駆逐艦"], specs={"全長": "150", "全幅": "15"})]
    assert object_size(["船舶", "駆逐艦"], taxonomy, 100, 45) == ObjectSize(100, 10, 45, "記録")
    assert object_size(["船舶", "駆逐艦"], taxonomy) == ObjectSize(150, 15, None, "諸元")
    assert object_size(["船舶"], taxonomy, 30) == ObjectSize(30, 30, None, "記録")
    assert object_size(["船舶"], taxonomy, default=12) == ObjectSize(12, 12, None, "既定")


def test_a_footprint_is_the_object_turned_to_its_heading():
    lat = 35.0
    k = math.cos(math.radians(lat)) * 111_320
    corners = footprint(139.0, lat, ObjectSize(100, 20, 90, "記録"))
    xs = [(lon - 139.0) * k for lon, _ in corners]
    ys = [(la - lat) * 111_320 for _, la in corners]
    # Heading east: 100 m east–west, 20 m north–south.
    assert max(xs) - min(xs) == pytest.approx(100, abs=0.01) and max(ys) - min(ys) == pytest.approx(20, abs=0.01)
    # Heading unknown: the square holding it at any heading.
    corners = footprint(139.0, lat, ObjectSize(100, 20, None, "諸元"))
    assert max(la for _, la in corners) - min(la for _, la in corners) == pytest.approx(100 / 111_320)


def synthetic(rng, count=10, size=160):
    """Bright squares on a dark noisy picture."""
    img = rng.normal(50, 10, (size, size, 3)).clip(0, 255)
    boxes = []
    for _ in range(count):
        x, y = rng.integers(10, size - 10, 2)
        img[y - 5:y + 5, x - 5:x + 5] = 230
        boxes.append((x, y, 10, 10, 0))
    rgba = np.concatenate([img.astype(np.uint8), np.full((size, size, 1), 255, np.uint8)], axis=2)
    return Window(rgba, np.array(boxes, np.float32))


@pytest.mark.parametrize("arch", ["res", "fpn4"])
def test_a_few_epochs_learn_the_squares(arch):
    rng = np.random.default_rng(5)
    windows = [synthetic(rng) for _ in range(12)]
    model = new_model(["square"], InputSpec(), 128, "small", arch=arch)
    run = train(model, windows, [], TrainOptions(epochs=8, tile=128, batch=4))
    assert run.epochs == 8 and run.losses[-1] < run.losses[0] / 2
    c = check(model, [synthetic(rng) for _ in range(3)])
    assert c.map50 > 0.7 and c.recall > 0.7


def test_training_stops_when_cancelled_keeping_what_it_learnt():
    rng = np.random.default_rng(6)
    model = new_model(["square"], InputSpec(), 128, "small")
    calls = []
    run = train(model, [synthetic(rng)], [], TrainOptions(epochs=5, tile=128, batch=2),
                cancelled=lambda: calls.append(1) or len(calls) > 25)
    assert run.stopped and run.epochs == 1


# The residual network (高精度).

def test_the_residual_networks_gradients_with_batch_normalization(monkeypatch):
    monkeypatch.setattr(N, "DTYPE", np.float64)
    rng = np.random.default_rng(7)
    layers = N.res_architecture(N.RES_WIDTHS["small"], 2)
    params = {k: v.astype(np.float64) + rng.standard_normal(v.shape) * 0.1 for k, v in N.init_params(layers, 2, rng).items()}
    net = N.Network(layers, params)
    x = rng.standard_normal((2, 64, 64, 2))
    heads = net.forward(x, keep=True)
    assert set(heads) == {"cls", "box"} and heads["box"].shape == (2, 16, 16, 4)
    weights = {k: rng.standard_normal(v.shape) for k, v in heads.items()}
    grads = net.backward(weights)

    def value():
        h = net.forward(x, training=True)
        return sum(float((h[k] * weights[k]).sum()) for k in h)

    for name in ("stem.w", "stem.bn_w", "s2r0a.w", "s3r1b.bn_b", "s5d.w", "p4.w", "hb.bn_w", "box.b", "cls.w"):
        d = rng.standard_normal(net.params[name].shape)
        kept = net.params[name].copy()
        # A small step: after batch normalization many units sit near ReLU's kink.
        net.params[name] = kept + 1e-8 * d
        up = value()
        net.params[name] = kept - 1e-8 * d
        down = value()
        net.params[name] = kept
        assert (up - down) / 2e-8 == pytest.approx(float((grads[name] * d).sum()), rel=1e-4, abs=1e-6)


def test_batch_normalization_learns_running_statistics():
    rng = np.random.default_rng(8)
    net = N.Network(N.res_architecture(N.RES_WIDTHS["small"], 1), N.init_params(N.res_architecture(N.RES_WIDTHS["small"], 1), 3, rng))
    x = (rng.standard_normal((4, 64, 64, 3)) * 3 + 5).astype(np.float32)
    for _ in range(300):
        net.forward(x, keep=True)
    # Once the running statistics are the batch's, detection (running) gives what training (batch) gives.
    trained, running = net.forward(x, training=True), net.forward(x, training=False)
    assert np.allclose(trained["cls"], running["cls"], atol=1e-2)


def test_giou_and_its_gradient():
    from satellite_observation.core.ai.train.loss import giou

    rng = np.random.default_rng(9)
    pred, truth = rng.uniform(0.2, 3, (30, 4)), rng.uniform(0.2, 3, (30, 4))
    iou, g, dg = giou(pred, truth)
    assert np.all(g <= iou + 1e-12) and np.allclose(giou(truth, truth)[1], 1)
    for i in range(4):
        e = np.zeros_like(pred)
        e[:, i] = 1e-6
        assert np.allclose((giou(pred + e, truth)[1] - giou(pred - e, truth)[1]) / 2e-6, dg[:, i], atol=1e-6)


def test_quality_focal_loss_and_its_gradient():
    from satellite_observation.core.ai.train.loss import quality_focal

    rng = np.random.default_rng(10)
    z = rng.normal(0, 2, 40)
    y = rng.uniform(0, 1, 40) * (rng.random(40) < 0.3)
    _, grad = quality_focal(z, y)
    for j in range(40):
        e = np.zeros(40)
        e[j] = 1e-5
        assert (quality_focal(z + e, y)[0] - quality_focal(z - e, y)[0]) / 2e-5 == pytest.approx(grad[j], abs=1e-4)


def test_box_targets_are_distances_from_the_cells_near_the_centre():
    from satellite_observation.core.ai.train.loss import tile_box_targets

    positive, cls, ltrb = tile_box_targets(np.array([[18.0, 10.0, 8.0, 8.0, 1], [40.0, 40.0, 3.0, 2.0, 0]]), 16, 16)
    # The 8 px box (cells 3.5–5.5 across, 1.5–3.5 down): the cell whose centre is inside it, a cell from each side.
    assert sorted(zip(*np.nonzero(positive))) == [(2, 4), (10, 10)]
    assert cls[2, 4] == 1 and ltrb[2, 4] == pytest.approx((1, 1, 1, 1))
    # A box smaller than a cell: its centre cell, the distances kept positive.
    assert cls[10, 10] == 0 and (ltrb[10, 10] >= 0.05).all()


def test_mean_average_precision():
    from satellite_observation.core.ai.train.metrics import evaluate

    truth = [np.array([[10, 10, 4, 4, 0], [30, 30, 6, 6, 1]], float)]
    perfect = [np.array([[10, 10, 4, 4, 0, 0.9], [30, 30, 6, 6, 1, 0.8], [50, 50, 4, 4, 0, 0.1]], float)]
    s = evaluate(perfect, truth, ["a", "b"])
    assert (s.map50, s.map, s.recall, s.precision) == (1.0, 1.0, 1.0, 1.0)
    # Off by half a pixel (IoU 0.78) and the other class missed.
    s = evaluate([np.array([[10.5, 10, 4, 4, 0, 0.9]], float)], truth, ["a", "b"])
    assert s.map50 == 0.5 and s.map == pytest.approx(0.3) and s.per_class == {"a": 1.0, "b": 0.0}


def test_zoomed_tiles_keep_their_boxes_on_the_objects():
    rng = np.random.default_rng(11)
    w = synthetic(rng, count=6, size=192)
    for _ in range(20):
        x, boxes = sample(w, InputSpec(), 128, rng)
        for cx, cy, bw, bh, _ in boxes:
            if 2 <= cx < 126 and 2 <= cy < 126 and bw > 6 and bh > 6:
                assert x[int(cy), int(cx)].mean() > 0.55


def test_the_moving_average_follows_the_weights():
    from satellite_observation.core.ai.train.trainer import Average

    net = small_network()
    average = Average(net, total=100)
    for k in net.params:
        net.params[k] += 1.0
    for _ in range(200):
        average.update(net)
    assert all(np.allclose(average.params[k], v, atol=1e-3) for k, v in net.params.items())
