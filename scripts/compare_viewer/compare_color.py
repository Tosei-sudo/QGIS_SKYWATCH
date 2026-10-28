"""
Colour scales and colour classes: the viewer's library (Node: run_color.mjs
on browser-image-enhancement's committed dist/) and this plugin's
core.enhance.color_scale / color_classes on the same input.

    python3 compare_color.py /path/to/browser-image-enhancement/packages/browser-image-enhancement/dist

Exit status 0 when every palette, coloured image and class list is the same.
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

from satellite_observation.core.enhance.color_classes import class_palette, class_range, class_shares, color_classes  # noqa: E402
from satellite_observation.core.enhance.color_scale import COLOR_SCALES, apply_palette, color_scale_palette  # noqa: E402
from satellite_observation.core.enhance.raster import RasterHistogram, sample_raster_histogram, valid_mask  # noqa: E402


def cases() -> dict:
    rng = np.random.default_rng(11)
    palettes = []
    for name in COLOR_SCALES:
        for opts in ({}, {"reverse": True}, {"fade": True}, {"reverse": True, "fade": True}):
            palettes.append({"name": f"{name}{json.dumps(opts)}", "scale": name, "options": opts})
    custom = [[0, [10, 20, 30]], [0.3, [200, 0, 50, 128]], [0.3, [0, 255, 0]], [1.2, [400, -5, 7.5]]]
    palettes.append({"name": "custom", "scale": custom, "options": {}})
    palettes.append({"name": "one", "scale": [[0.5, [255, 136, 0]]], "options": {"reverse": True}})

    h, w = 40, 50
    color = rng.integers(0, 256, (h, w, 4), dtype=np.uint8)
    color[..., 3] = rng.choice([0, 128, 255], (h, w))
    gray = color.copy()
    gray[..., 1] = gray[..., 0]
    gray[..., 2] = gray[..., 0]
    applied = []
    for img_name, img in (("color", color), ("gray", gray)):
        for scale, opts in (("inferno", {}), ("red", {"fade": True}), ("turbo", {"reverse": True})):
            applied.append({"name": f"{img_name}-{scale}", "image": base64.b64encode(img.tobytes()).decode(),
                            "scale": scale, "options": opts, "shape": [h, w]})

    dem = rng.normal(800, 300, (60, 70)).round(1)
    dem[:3] = -9999
    land = rng.choice([1, 2, 3, 5, 8, 11, 40], (60, 70), p=[0.3, 0.2, 0.2, 0.1, 0.1, 0.05, 0.05]).astype(float)
    ndvi = np.clip(rng.normal(0.3, 0.25, (60, 70)), -1, 1)
    hist8 = np.bincount(rng.integers(20, 200, 5000), minlength=256)
    classes = []
    for name, data, no_data in (("dem", dem, -9999), ("land", land, None), ("ndvi", ndvi, None)):
        for opts in ({"method": "equal", "count": 5}, {"method": "quantile", "count": 7, "scale": "viridis", "reverse": True},
                     {"method": "unique", "scale": "turbo"}, {"method": "equal", "count": 1}):
            classes.append({"name": f"{name}-{json.dumps(opts)}", "raster": {"data": data.ravel().tolist(), "width": 70, "height": 60,
                                                                             "noData": no_data}, "options": opts})
    classes.append({"name": "hist8", "raster": {"data": [0], "width": 1, "height": 1}, "hist8": hist8.tolist(),
                    "options": {"method": "quantile", "count": 4}})
    return {"palettes": palettes, "applied": applied, "classes": classes}


def ours(c: dict) -> dict:
    out = {"palettes": {}, "applied": {}, "classes": {}}
    for p in c["palettes"]:
        out["palettes"][p["name"]] = color_scale_palette(p["scale"], **p["options"]).ravel().tolist()
    for a in c["applied"]:
        img = np.frombuffer(base64.b64decode(a["image"]), np.uint8).reshape(*a["shape"], 4)
        out["applied"][a["name"]] = base64.b64encode(apply_palette(img, color_scale_palette(a["scale"], **a["options"])).tobytes()).decode()
    for k in c["classes"]:
        if "hist8" in k:
            hist = RasterHistogram((0, 255), [np.array(k["hist8"], dtype=np.float64)], int(sum(k["hist8"])))
        else:
            r = k["raster"]
            band = np.array(r["data"], dtype=np.float64).reshape(r["height"], r["width"])
            hist = sample_raster_histogram([band], valid_mask([band], [r["noData"]] if r.get("noData") is not None else []))
        o = dict(k["options"])
        classes = color_classes(hist, **o)
        out["classes"][k["name"]] = classes and {"classes": classes, "range": list(class_range(classes)),
                                                 "palette": class_palette(classes).ravel().tolist(), "shares": class_shares(hist, classes)}
    return out


def same(a, b, path="") -> list:
    """Paths where a and b differ (numbers within 1e-9 relative)."""
    if isinstance(a, dict) and isinstance(b, dict):
        keys = set(a) | set(b)
        return [d for k in sorted(keys) for d in same(a.get(k), b.get(k), f"{path}.{k}")]
    if isinstance(a, (list, tuple)) and isinstance(b, (list, tuple)):
        if len(a) != len(b):
            return [f"{path}: length {len(a)} ≠ {len(b)}"]
        return [d for i, (x, y) in enumerate(zip(a, b)) for d in same(x, y, f"{path}[{i}]")]
    if isinstance(a, (int, float)) and isinstance(b, (int, float)) and not isinstance(a, bool):
        return [] if abs(a - b) <= 1e-9 * max(1.0, abs(a), abs(b)) else [f"{path}: {a} ≠ {b}"]
    return [] if a == b else [f"{path}: {a!r} ≠ {b!r}"]


def main() -> int:
    dist = sys.argv[1]
    c = cases()
    with tempfile.TemporaryDirectory() as tmp:
        cases_path, out_path = os.path.join(tmp, "cases.json"), os.path.join(tmp, "out.json")
        with open(cases_path, "w") as f:
            json.dump(c, f)
        subprocess.run(["node", os.path.join(HERE, "run_color.mjs"), dist, cases_path, out_path], check=True)
        with open(out_path) as f:
            theirs = json.load(f)
    mine = ours(c)
    failed = 0
    for group in ("palettes", "applied", "classes"):
        for name in theirs[group]:
            diffs = same(mine[group][name], theirs[group][name])
            if diffs:
                failed += 1
                print(f"✗ {group} {name}: {len(diffs)} differences, first: {diffs[:3]}")
        print(f"{group}: {len(theirs[group]) - sum(1 for n in theirs[group] if same(mine[group][n], theirs[group][n]))}"
              f"/{len(theirs[group])} agree")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
