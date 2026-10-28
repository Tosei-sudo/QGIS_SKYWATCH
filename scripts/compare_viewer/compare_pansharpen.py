"""
Pan-sharpening: the viewer's library (Node: run_pansharpen.mjs on
browser-image-enhancement's committed dist/) and this plugin's
core.enhance.pansharpen on the same rasters.

    python3 compare_pansharpen.py /path/to/browser-image-enhancement/packages/browser-image-enhancement/dist

Exit status 0 when every case agrees: weights and gains within 1e-9
(relative), values within one code on at most 0.1 % of the integer values
(a value on a rounding boundary can round the other way, as sums are added
in another order) and within 1e-6 (relative) for floats.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile

import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(os.path.dirname(HERE)))

from satellite_observation.core.enhance.pansharpen import PanSharpenModel, pan_sharpen  # noqa: E402

H, W = 90, 110


def cases() -> list:
    rng = np.random.default_rng(5)
    y, x = np.mgrid[0:H, 0:W]
    detail = 300 * np.sin(x / 3) * np.cos(y / 4)
    base = [2000 + 800 * np.sin(x / 17 + k) + 500 * np.cos(y / 13 - k) for k in range(4)]
    ms16 = np.stack([b + rng.normal(0, 30, (H, W)) for b in base]).clip(1, 65535).astype(np.uint16)
    pan16 = (0.25 * sum(base) + detail + rng.normal(0, 20, (H, W))).clip(0, 65535).astype(np.uint16)
    ms16[:, :4, :] = 0
    pan16[50:53, 20:30] = 0
    ms8 = (ms16 / 16).clip(0, 255).astype(np.uint8)
    pan8 = (pan16 / 16).clip(0, 255).astype(np.uint8)
    msf = ms16.astype(np.float32) / 10000
    msf[1, 30:32, 40:45] = np.nan
    panf = pan16.astype(np.float32) / 10000
    alpha = np.concatenate([ms8[:3], np.where(rng.random((1, H, W)) < 0.05, 0, 255).astype(np.uint8)])
    dark = ms8.copy()
    dark[:, 60:70, 60:70] = 0
    out = []

    def add(name, pan, ms, pan_type, ms_type, options, pan_no=None, ms_no=None, alpha_=False):
        out.append({"name": name, "pan": [None if np.isnan(v) else float(v) for v in pan.ravel()] if pan.dtype.kind == "f" else pan.ravel().tolist(),
                    "ms": [None if np.isnan(v) else float(v) for v in np.moveaxis(ms, 0, -1).ravel()] if ms.dtype.kind == "f"
                    else np.moveaxis(ms, 0, -1).ravel().tolist(),
                    "panType": pan_type, "msType": ms_type, "width": W, "height": H, "bands": ms.shape[0],
                    "panNoData": pan_no, "msNoData": ms_no, "alpha": alpha_, "options": options})

    for method in ("gram-schmidt", "ihs", "brovey"):
        add(f"16bit-{method}", pan16, ms16, "uint16", "uint16", {"method": method}, 0, 0)
        add(f"8bit-{method}-equal", pan8, ms8, "uint8", "uint8", {"method": method, "weights": "equal", "strength": 0.6})
    add("float-gs", panf, msf, "float32", "float32", {"method": "gram-schmidt"})
    add("float-brovey", panf, msf, "float32", "float32", {"method": "brovey", "strength": 1.5})
    add("alpha", pan8, alpha, "uint8", "uint8", {"method": "gram-schmidt"}, alpha_=True)
    add("bands-weights", pan16, ms16, "uint16", "uint16", {"method": "gram-schmidt", "bands": [2, 1, 0], "weights": [1, 2, 0]}, 0, 0)
    add("dark-brovey", pan8, dark, "uint8", "uint8", {"method": "brovey"})
    add("model", pan16, ms16, "uint16", "uint16",
        {"method": "ihs", "model": {"weights": [0.1, 0.2, 0.3, 0.4], "gains": [1, 1, 1, 1], "panGain": 0.9, "panOffset": 120}}, 0, 0)
    return out


def ours(c: dict) -> dict:
    t = {"uint8": np.uint8, "uint16": np.uint16, "int16": np.int16, "float32": np.float32}
    pan = np.array([np.nan if v is None else v for v in c["pan"]], dtype=t[c["panType"]]).reshape(H, W)
    ms = np.moveaxis(np.array([np.nan if v is None else v for v in c["ms"]], dtype=t[c["msType"]]).reshape(H, W, c["bands"]), -1, 0)
    o = dict(c["options"])
    model = o.pop("model", None)
    if model:
        model = PanSharpenModel(model["weights"], model["gains"], model["panGain"], model["panOffset"])
    out, no_data, fitted = pan_sharpen(pan, ms, model=model, pan_no_data=c["panNoData"], ms_no_data=c["msNoData"], alpha=c["alpha"], **o)
    return {"data": np.moveaxis(out, 0, -1).ravel(), "noData": no_data, "model": fitted}


def main() -> int:
    dist = sys.argv[1]
    cs = cases()
    with tempfile.TemporaryDirectory() as tmp:
        cases_path, out_path = os.path.join(tmp, "cases.json"), os.path.join(tmp, "out.json")
        with open(cases_path, "w") as f:
            json.dump(cs, f)
        subprocess.run(["node", os.path.join(HERE, "run_pansharpen.mjs"), dist, cases_path, out_path], check=True)
        with open(out_path) as f:
            theirs = json.load(f)
    failed = 0
    for c in cs:
        mine, other = ours(c), theirs[c["name"]]
        m = mine["model"]
        fit = np.allclose(m.weights + m.gains + [m.pan_gain, m.pan_offset],
                          other["model"]["weights"] + other["model"]["gains"] + [other["model"]["panGain"], other["model"]["panOffset"]],
                          rtol=1e-9, atol=1e-9)
        a = np.array(mine["data"], dtype=np.float64)
        b = np.array([np.nan if v is None else v for v in other["data"]], dtype=np.float64)
        same_nan = np.array_equal(np.isnan(a), np.isnan(b))
        diff = np.abs(np.nan_to_num(a) - np.nan_to_num(b))
        if c["msType"] == "float32":
            ok_values = np.allclose(np.nan_to_num(a), np.nan_to_num(b), rtol=1e-6, atol=1e-7)
            note = f"max diff {diff.max():.2e}"
        else:
            off = int((diff > 0).sum())
            ok_values = diff.max() <= 1 and off <= a.size / 1000
            note = f"{off}/{a.size} values off by 1" if off else "all values equal"
        ok = fit and same_nan and ok_values and (mine["noData"] == other["noData"] or (other["noData"] is None and np.isnan(mine["noData"])))
        failed += not ok
        print(f"{'✓' if ok else '✗'} {c['name']}: model {'same' if fit else 'differs'}, {note}")
    print(f"{len(cs) - failed}/{len(cs)} agree")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
