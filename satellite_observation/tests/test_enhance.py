"""
core.enhance without QGIS: steps, the saved form, presets, DRA and the raw
stretch. Pixel-for-pixel agreement with the viewer's library is checked by
scripts/compare_viewer/compare_enhance.py.
"""
import re

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


def test_image_rules_by_file_name():
    from satellite_observation.core.enhance.rules import file_name_of, image_rule_for, image_rules_of, resolve_bands

    problems = []
    rules = image_rules_of([
        {"label": "Landsat", "match": r"^LC0[89]_.*\.tif$", "preset": "satellite", "enhance": {"contrast": 0.1}, "bands": [4, 3, 2]},
        {"match": r"_MS\.tif$", "bands": ["NIR", "Red", "Green"]},
        {"match": r"_warp\.tif$", "noData": {"auto": False, "values": [0, "x"]}},
        {"match": r"_PAN\.tif$", "caseSensitive": True, "enhance": {"gamma": 1.2}},
        {"match": "(", "preset": "vivid"},
        {"match": "only_label", "label": "何もなし"},
        {"match": "x", "bands": [1, 2]},
    ], problems)
    assert [r.label for r in rules] == ["Landsat", r"_MS\.tif$", r"_warp\.tif$", r"_PAN\.tif$"]
    assert len(problems) == 4 and problems[0].startswith("imageRules[4] の match は正規表現として読み込み不可")
    landsat = rules[0]
    assert landsat.bands == [4, 3, 2]
    assert landsat.pipeline.get("contrast")["amount"] == 0.1
    assert rules[2].no_data == {"auto": False, "values": [0.0]}

    assert file_name_of("https://h/a/LC08_x.tif?sig=1") == "LC08_x.tif"
    assert file_name_of("C:\\img\\lc09_y.TIF") == "lc09_y.TIF"
    assert file_name_of("/data/a%20b_MS.tif") == "a b_MS.tif"
    assert image_rule_for(rules, "C:\\img\\lc09_y.TIF") is landsat      # case ignored
    assert image_rule_for(rules, "/d/s_pan.tif") is None                # caseSensitive
    assert image_rule_for(rules, "/d/s_PAN.tif") is rules[3]

    assert resolve_bands([4, 3, 2], 4, []) == (4, 3, 2)
    assert resolve_bands(["nir ", "RED", "green"], 4, ["Blue", "Green", "Red", "NIR"]) == (4, 3, 2)
    for bands, count, why in [([4, 3, 2], 3, "バンド 4 なし（3 バンドの画像）"), ([1, 2, 3], 1, "バンドが 1 つのみ"),
                              (["SWIR", "Red", "Green"], 4, "「SWIR」という名前のバンドなし")]:
        with pytest.raises(ValueError, match=re.escape(why)):
            resolve_bands(bands, count, ["Blue", "Green", "Red", "NIR"])


def test_histogram_stats_and_bars_as_the_viewer_s_panel():
    from satellite_observation.core.enhance.histogram import bar_heights, channel_bins, channel_stats

    rgba = np.zeros((2, 4, 4), np.uint8)
    rgba[..., 0] = [[0, 10, 10, 99], [255, 255, 255, 99]]
    rgba[..., 3] = [[255, 255, 255, 0], [255, 255, 255, 0]]   # the last column is transparent
    bins = channel_bins(rgba, gray=False)
    assert len(bins) == 3 and bins[0][10] == 2 and bins[0][255] == 3 and bins[0].sum() == 6
    assert channel_stats(bins[0]) == {"min": 0, "max": 255, "mean": pytest.approx(785 / 6), "median": 10, "count": 6}
    assert channel_stats(np.zeros(256)) is None
    # Linear: the ends do not set the scale (they are cut at 1); logarithmic: they do.
    linear = bar_heights(bins, log=False)
    assert linear[0][10] == 1 and linear[0][0] == 0.5 and linear[0][255] == 1
    log = bar_heights(bins, log=True)
    # (G and B are all 0 here: 6 at 0 is the peak of the three.)
    assert log[1][0] == 1 and log[0][255] == pytest.approx(np.log(4) / np.log(7))
    assert all((h == 0).all() for h in bar_heights([np.zeros(256)], log=False))
