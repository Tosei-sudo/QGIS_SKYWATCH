"""
Runs the same images and corrections through the viewer's library
(Node: run_enhance.mjs on browser-image-enhancement's committed dist/) and
this plugin's core.enhance, and reports how many pixel values differ.

    python3 compare_enhance.py /path/to/browser-image-enhancement/packages/browser-image-enhancement/dist

Exit status 0 when every case agrees within one code on at most 1 % of the values (an input code that lands on a rounding boundary can round the other way).
"""
from __future__ import annotations

import base64
import json
import os
import subprocess
import sys
import tempfile

import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(os.path.dirname(HERE)))

from satellite_observation.core.enhance import PRESETS, Pipeline, process  # noqa: E402
from satellite_observation.core.enhance.raster import compute_raster_stretch, sample_raster_histogram, valid_mask  # noqa: E402

W, H = 97, 83


def images() -> dict:
    rng = np.random.default_rng(7)
    y, x = np.mgrid[0:H, 0:W]
    base = np.stack([x * 255 / W, y * 255 / H, (x + y) * 128 / (W + H) + 60], axis=-1)
    color = np.clip(base + rng.normal(0, 25, (H, W, 3)), 0, 255).astype(np.uint8)
    rgba = np.dstack([color, np.full((H, W), 255, np.uint8)])
    holes = rgba.copy()
    holes[10:30, 20:45, 3] = 0
    holes[:, :5, 3] = 0
    holes[40:50, 60:70, 3] = 128
    gray = rgba.copy()
    gray[..., 1] = gray[..., 0]
    gray[..., 2] = gray[..., 0]
    hazy = np.dstack([(color * 0.3 + 90).astype(np.uint8), rgba[..., 3]])
    return {"color": rgba, "holes": holes, "gray": gray, "hazy": hazy}


OPS = [
    ("brightness+", [{"op": "brightness", "amount": 0.3}]),
    ("brightness-", [{"op": "brightness", "amount": -0.45}]),
    ("contrast+", [{"op": "contrast", "amount": 0.4}]),
    ("contrast-", [{"op": "contrast", "amount": -0.6}]),
    ("exposure", [{"op": "exposure", "ev": 1.3}]),
    ("gamma", [{"op": "gamma", "gamma": 1.8}]),
    ("saturation", [{"op": "saturation", "amount": 0.5}]),
    ("desaturate", [{"op": "saturation", "amount": -1}]),
    ("temperature", [{"op": "temperature", "amount": -0.4}]),
    ("tint", [{"op": "tint", "amount": 0.5}]),
    ("whiteBalance", [{"op": "whiteBalance", "r": 0.6, "g": 0.5, "b": 0.4}]),
    ("shadows", [{"op": "shadows", "amount": 0.7}]),
    ("highlights", [{"op": "highlights", "amount": -0.6}]),
    ("curve", [{"op": "curve", "points": [[0, 0.05], [0.3, 0.42], [0.7, 0.8], [1, 0.97]], "red": [[0, 0], [0.5, 0.6], [1, 1]]}]),
    ("curve-falls", [{"op": "curve", "points": [[0, 1], [1, 0]]}]),
    ("levels", [{"op": "levels", "inBlack": 0.1, "inWhite": 0.85, "gamma": 1.4, "outBlack": 0.02, "outWhite": 0.95}]),
    ("stretch", [{"op": "stretch", "black": [0.1, 0.05, 0.2], "white": [0.9, 0.8, 0.95]}]),
    ("dra-clip", [{"op": "autoStretch", "lowPercent": 2, "highPercent": 1}]),
    ("dra-minmax-linked", [{"op": "autoStretch", "method": "minMax", "linked": True}]),
    ("dra-sd", [{"op": "autoStretch", "method": "standardDeviation", "stdDevs": 1.5}]),
    ("exposure-then-dra", [{"op": "exposure", "ev": 0.5}, {"op": "autoStretch"}, {"op": "contrast", "amount": 0.2}]),
    ("chain", [{"op": "exposure", "ev": 0.4}, {"op": "contrast", "amount": 0.25}, {"op": "saturation", "amount": 0.3},
               {"op": "gamma", "gamma": 1.2}, {"op": "levels", "inBlack": 0.05}]),
    ("sharpen", [{"op": "sharpen", "amount": 1.2, "radius": 1.5}]),
    ("sharpen-threshold", [{"op": "sharpen", "amount": 0.8, "radius": 0.7, "threshold": 0.03}]),
    ("chain-sharpen", [{"op": "exposure", "ev": 0.3}, {"op": "saturation", "amount": 0.2}, {"op": "sharpen", "amount": 0.9},
                       {"op": "contrast", "amount": 0.2}, {"op": "saturation", "amount": -0.3}]),
    ("two-sharpens", [{"op": "sharpen", "amount": 0.5}, {"op": "gamma", "gamma": 1.3}, {"op": "sharpen", "amount": 0.4, "radius": 2.5}]),
] + [(f"preset-{name}", list(p.to_json()["ops"])) for name, p in PRESETS.items()]


def cases() -> list:
    out = []
    for img_name, img in images().items():
        for name, ops in OPS:
            out.append({"name": f"{img_name}/{name}", "image": base64.b64encode(img.tobytes()).decode(), "width": W, "ops": ops})
        out.append({"name": f"{img_name}/forced-gray", "image": base64.b64encode(img.tobytes()).decode(), "width": W,
                    "colorMode": "gray", "ops": [{"op": "contrast", "amount": 0.3}, {"op": "sharpen", "amount": 0.6}]})
    rng = np.random.default_rng(3)
    for name, data, no_data, options in [
        ("raster/landsat-like", rng.normal([8000, 9000, 7500], [600, 700, 500], (60, 50, 3)), None, {}),
        ("raster/fill-value", np.where(rng.random((60, 50, 1)) < 0.2, -9999, rng.normal(300, 40, (60, 50, 3))), None, {"lowPercent": 2, "highPercent": 2}),
        ("raster/outliers", np.where(rng.random((60, 50, 3)) < 0.003, 60000, rng.normal(1000, 30, (60, 50, 3))), None, {"linked": True}),
        ("raster/nodata", np.where(rng.random((60, 50, 1)) < 0.3, 0, rng.normal(500, 90, (60, 50, 3))), 0, {"method": "standardDeviation"}),
    ]:
        out.append({"name": name, "raster": {"data": data.ravel().tolist(), "width": 50, "height": 60, "bands": 3, "noData": no_data},
                    "options": options, "array": data, "noData": no_data})
    return out


def main(dist: str) -> int:
    all_cases = cases()
    with tempfile.TemporaryDirectory() as tmp:
        cpath, opath = os.path.join(tmp, "cases.json"), os.path.join(tmp, "out.json")
        with open(cpath, "w") as f:
            json.dump([{k: v for k, v in c.items() if k not in ("array",)} for c in all_cases], f)
        subprocess.run(["node", os.path.join(HERE, "run_enhance.mjs"), dist, cpath, opath], check=True)
        with open(opath) as f:
            viewer = json.load(f)
    bad = 0
    for c in all_cases:
        v = viewer[c["name"]]
        if "raster" in c:
            data = c["array"]
            bands = [data[..., b].astype(np.float64) for b in range(3)]
            stats = sample_raster_histogram(bands, valid_mask(bands, [] if c["noData"] is None else [c["noData"]]))
            mine = compute_raster_stretch(stats, c["options"])
            diff = max(abs(a - b) for k in ("black", "white") for a, b in zip(mine[k], v["stretch"][k]))
            scale = max(1e-9, v["range"][1] - v["range"][0])
            ok = diff <= scale * 1e-9
            print(f"{'OK ' if ok else 'NG '} {c['name']:<34} 黒・白の差 {diff:.3g}（範囲 {v['range'][0]:.6g}〜{v['range'][1]:.6g}）")
            bad += not ok
            continue
        img = np.frombuffer(base64.b64decode(c["image"]), np.uint8).reshape(H, W, 4)
        theirs = np.frombuffer(base64.b64decode(v["image"]), np.uint8).reshape(H, W, 4)
        mine = process(img, Pipeline(c["ops"]), c.get("colorMode", "auto"))
        d = np.abs(mine.astype(int) - theirs.astype(int))
        share = np.count_nonzero(d) / d.size
        ok = d.max() <= 1 and share <= 0.01
        print(f"{'OK ' if ok else 'NG '} {c['name']:<34} 最大差 {d.max()}  異なる値 {np.count_nonzero(d)}/{d.size}")
        bad += not ok
    print(f"{len(all_cases) - bad}/{len(all_cases)} 一致")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1]))
