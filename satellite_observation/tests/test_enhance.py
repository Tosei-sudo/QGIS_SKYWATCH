"""
core.enhance without QGIS: steps, the saved form, presets, DRA and the raw
stretch. Pixel-for-pixel agreement with the viewer's library is checked by
scripts/compare_viewer/compare_enhance.py.
"""
import numpy as np
import pytest

from satellite_observation.core.enhance import OP_INFO, PRESETS, Pipeline, normalize_op, process
from satellite_observation.core.enhance.pipeline import enhance_of
from satellite_observation.core.enhance.raster import compute_raster_stretch, sample_raster_histogram, stretch_to_rgba, valid_mask
from satellite_observation.core.enhance.srgb import SRGB_TO_LINEAR, quantize
from satellite_observation.core.enhance.stats import count_pixels, resolve_ops


def image(h=40, w=50, seed=0):
    rng = np.random.default_rng(seed)
    rgb = rng.integers(0, 256, (h, w, 3), dtype=np.uint8)
    return np.dstack([rgb, np.full((h, w), 255, np.uint8)])


def test_quantize_round_trips_every_code():
    assert np.array_equal(quantize(SRGB_TO_LINEAR), np.arange(256))
    assert quantize(np.array([-1.0, np.nan, 2.0])).tolist() == [0, 0, 255]


def test_out_of_range_values_are_clamped_not_refused():
    assert normalize_op({"op": "contrast", "amount": 3}) == {"op": "contrast", "amount": 1.0}
    assert normalize_op({"op": "gamma"}) == {"op": "gamma", "gamma": 1.0}
    lv = normalize_op({"op": "levels", "inBlack": 0.6, "inWhite": 0.2})
    assert lv["inWhite"] == pytest.approx(0.6 + 1 / 255)
    with pytest.raises(ValueError):
        normalize_op({"op": "blur"})


def test_identity_steps_change_nothing():
    img = image()
    p = Pipeline([{"op": name} for name, info in OP_INFO.items() if not info["auto"] and name != "sharpen"])
    assert p.is_identity
    assert np.array_equal(process(img, p), img)


def test_set_get_remove_and_json():
    p = Pipeline().add("exposure", 0).add("sharpen", {"amount": 0})
    p = p.set("sharpen", {"radius": 2}).set("contrast", 0.3)
    assert p.get("sharpen") == {"op": "sharpen", "amount": 0.0, "radius": 2.0, "threshold": 0.0}
    assert [op["op"] for op in p.ops] == ["exposure", "sharpen", "contrast"]
    assert Pipeline.from_json(p.dumps()) == p
    assert p.remove("exposure").get("exposure") is None
    assert p.margin == 0 and p.set("sharpen", 1).margin == 6
    with pytest.raises(ValueError):
        Pipeline.from_json({"ops": []})


def test_image_rules_enhance_as_the_viewer_reads_it():
    problems = []
    p = enhance_of({"contrast": 0.1, "autoStretch": {"lowPercent": 1}, "blur": 2, "levels": 0.2}, PRESETS["satellite"], problems, "imageRules[0]")
    assert p.get("contrast")["amount"] == 0.1
    assert p.get("autoStretch")["lowPercent"] == 1 and p.get("autoStretch")["highPercent"] == 2
    assert problems == ["imageRules[0] の enhance の「blur」は無視（Unknown correction: blur）",
                        "imageRules[0] の enhance の「levels」は無視（levels takes an object of parameters, not a number.）"]
    saved = enhance_of({"version": 1, "ops": [{"op": "gamma", "gamma": 2}]}, PRESETS["auto"], [], "x")
    assert [op["op"] for op in saved.ops] == ["gamma"]


def test_monochrome_stays_monochrome():
    img = image()
    img[..., 1] = img[..., 0]
    img[..., 2] = img[..., 0]
    out = process(img, PRESETS["vivid"].set("temperature", 0.5))
    assert (out[..., 0] == out[..., 1]).all() and (out[..., 1] == out[..., 2]).all()


def test_dra_after_exposure_uses_the_exposed_values():
    img = image()
    img[..., :3] = np.clip(img[..., :3] // 2 + 40, 0, 255)
    ops = resolve_ops(Pipeline().add("exposure", 1).add("autoStretch", {"method": "minMax"}).ops, count_pixels(img, "rgb"))
    out = process(img, ops)
    for c in range(3):
        assert out[..., c].min() == 0 and out[..., c].max() == 255


def test_transparent_pixels_are_left_alone_and_not_counted():
    img = image()
    img[:10, :, :3] = 255
    img[:10, :, 3] = 0
    out = process(img, Pipeline().add("autoStretch", {"method": "minMax"}).add("sharpen", 2))
    assert np.array_equal(out[:10], img[:10])


def test_sharpen_on_tiles_with_margin_equals_the_whole():
    img = image(64, 64)
    p = Pipeline().add("contrast", 0.2).add("sharpen", {"amount": 1, "radius": 1.5})
    whole = process(img, p)
    m = p.margin
    tile = process(img[:32 + m, :32 + m], p, mode="rgb")[:32, :32]
    assert np.array_equal(tile, whole[:32, :32])


def test_raw_stretch_leaves_out_an_untagged_fill_value():
    rng = np.random.default_rng(5)
    band = rng.normal(300, 30, (60, 60))
    band[:, :15] = -9999   # a quarter of fill: more than a 2 % clip
    stats = sample_raster_histogram([band], valid_mask([band]))
    s = compute_raster_stretch(stats, {"lowPercent": 2, "highPercent": 2})
    assert 200 < s["black"][0] < 300 < s["white"][0] < 400
    rgba = stretch_to_rgba([band], valid_mask([band]), s["black"], s["white"])
    assert (rgba[:, :15, :3] == 0).all() and rgba[:, 15:, 0].mean() > 100
