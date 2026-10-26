"""
Runs the same cases through the viewer (Node: run_viewer.mjs bundling its
imaging-plan.ts) and this plugin's core, and checks they agree:
time ±1 s, angles ±0.05°, coverage ±1 %, and the same passes in each
combination.

    cd scripts/compare_viewer && npm install
    python3 compare.py /path/to/browser-image-enhancement/usecase/image-viewer/src

Exit status 0 when every case agrees.
"""
from __future__ import annotations

import json
import math
import os
import subprocess
import sys
import tempfile
import time
from dataclasses import replace
from datetime import datetime, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(os.path.dirname(HERE)))

from satellite_observation.core.plan.access import PlanOptions, PlanTarget, plan_access  # noqa: E402
from satellite_observation.core.plan.cover import cover_plan  # noqa: E402
from satellite_observation.core.plan.satellites import GroundStation, satellites_from_text  # noqa: E402

ISS = "ISS\n1 25544U 98067A   26280.50000000  .00016717  00000-0  10270-3 0  9994\n2 25544  51.6416 247.4627 0006703 130.5360 325.0288 15.49815399432717"
ISS2 = ISS.replace("325.0288", "145.0288").replace("ISS\n", "ISS-2\n")
OPT = "サンプル光学衛星\n1 90001U 26901A   26280.50000000  .00000100  00000-0  40000-4 0  9990\n2 90001  98.5700 350.5000 0001100  90.0000 270.0000 14.30820000    13"
SAR = "サンプルSAR衛星\n1 90002U 26902A   26280.50000000  .00000100  00000-0  40000-4 0  9992\n2 90002  98.1800 283.0000 0001300  90.0000 270.0000 14.59200000    16"
EPOCH = datetime(2026, 1, 1, tzinfo=timezone.utc).timestamp() * 1000 + 279.5 * 86400000
DAY = 86400000


def box(label, lon, lat, w, h, n=20):
    d_lon = w / (111.32 * math.cos(lat * math.pi / 180))
    d_lat = h / 111.32
    cells = [[lon - d_lon / 2 + d_lon * (i + 0.5) / n, lat - d_lat / 2 + d_lat * (j + 0.5) / n] for i in range(n) for j in range(n)]
    points = [[lon - d_lon / 2, lat - d_lat / 2], [lon + d_lon / 2, lat - d_lat / 2], [lon + d_lon / 2, lat + d_lat / 2], [lon - d_lon / 2, lat + d_lat / 2]]
    return {"label": label, "center": [lon, lat], "points": points, "cells": cells}


def point(label, lon, lat, **kw):
    return {"label": label, "center": [lon, lat], "points": [], **kw}


def walker(n):
    """`n` made-up satellites on spread orbits (different planes and phases)."""
    out = []
    for i in range(n):
        raan = (i * 360 / n) % 360
        ma = (i * 137.5) % 360
        line2 = f"2 {90100 + i:05d}  97.4000 {raan:8.4f} 0001200  90.0000 {ma:8.4f} 15.20000000    10"
        line1 = f"1 {90100 + i:05d}U 26900A   26280.50000000  .00000100  00000-0  40000-4 0  9990"
        out.append({"tle": f"SAT-{i + 1}\n{line1}\n{line2}", "spec": {"maxOffNadir": 30, "swath": 15, "sar": i % 3 == 0}})
    return out


CASES = [
    {"name": "iss-points-week", "satellites": [{"tle": ISS, "spec": {"maxOffNadir": 45}}],
     "targets": [point("tokyo", 139.7, 35.7), point("ny", -74, 40.7), point("paris", 2.35, 48.85), point("sydney", 151.2, -33.9), point("dateline", 179.9, 0), point("north", 0, 89)],
     "options": {"start": EPOCH, "end": EPOCH + 7 * DAY, "maxOffNadir": None, "daylight": "none", "minSunElevation": 10}},
    {"name": "samples-tokyo-optical", "satellites": [{"tle": OPT, "spec": {"maxOffNadir": 20, "swath": 30, "length": 30}}, {"tle": SAR, "spec": {"maxOffNadir": 45, "minOffNadir": 20, "swath": 50, "length": 50, "lookSide": "right", "sar": True}}],
     "targets": [point("tokyo", 139.7, 35.68)],
     "options": {"start": EPOCH, "end": EPOCH + 14 * DAY, "maxOffNadir": None, "daylight": "optical", "minSunElevation": 10}},
    {"name": "conditions", "satellites": [{"tle": ISS, "spec": {"maxOffNadir": 45, "leadTime": 24}}, {"tle": SAR, "spec": {"maxOffNadir": 45, "minOffNadir": 20, "lookSide": "both", "sar": True}}],
     "targets": [point("osaka", 135.5, 34.7), point("sapporo", 141.35, 43.06)],
     "options": {"start": EPOCH, "end": EPOCH + 10 * DAY, "maxOffNadir": 35, "daylight": "optical", "minSunElevation": 5, "now": EPOCH + 2 * DAY,
                 "minIncidence": 10, "maxIncidence": 40, "pass": "descending", "side": "left",
                 "stations": [{"name": "勝浦", "lon": 140.3, "lat": 35.15, "minElevation": 5}]}},
    {"name": "area-cover", "satellites": [{"tle": ISS, "spec": {"maxOffNadir": 45, "swath": 30, "length": 30, "scenesPerPass": 4}}, {"tle": ISS2, "spec": {"maxOffNadir": 45, "swath": 30, "length": 30, "scenesPerPass": 4}}],
     "targets": [box("wide", 139.7, 35.7, 150, 60)],
     "options": {"start": EPOCH, "end": EPOCH + 14 * DAY, "maxOffNadir": None, "daylight": "none", "minSunElevation": 10},
     "cover": {"goal": 0.9, "minGain": 0.02, "share": True}},
    {"name": "points-shared-priority", "satellites": [{"tle": ISS, "spec": {"maxOffNadir": 45, "swath": 40, "length": 40, "minInterval": 10, "maxPerDay": 3}}],
     "targets": [point("a", 139.70, 35.70, priority=1), point("b", 139.75, 35.72, priority=2), point("c", 139.9, 35.6), point("d", 135.5, 34.7, priority=1)],
     "options": {"start": EPOCH, "end": EPOCH + 7 * DAY, "maxOffNadir": None, "daylight": "none", "minSunElevation": 10},
     "cover": {"goal": 0.95, "minGain": 0.02, "share": True}},
    {"name": "modes", "satellites": [
        {"tle": SAR, "spec": {"id": "S", "mode": "strip", "minOffNadir": 20, "maxOffNadir": 45, "swath": 50, "length": 50, "resolution": 5, "sar": True, "lookSide": "right", "minInterval": 5}},
        {"tle": SAR, "spec": {"id": "S", "mode": "spot", "minOffNadir": 15, "maxOffNadir": 35, "swath": 10, "length": 10, "resolution": 1, "sar": True, "lookSide": "right", "minInterval": 5}},
        {"tle": SAR, "spec": {"id": "S", "mode": "scan", "minOffNadir": 20, "maxOffNadir": 45, "swath": 250, "length": 250, "resolution": 20, "sar": True, "lookSide": "right", "minInterval": 5}}],
     "targets": [box("bay", 139.8, 35.4, 80, 80, 25), point("p", 140.1, 36.0)],
     "options": {"start": EPOCH, "end": EPOCH + 14 * DAY, "maxOffNadir": None, "daylight": "none", "minSunElevation": 10},
     "cover": {"goal": 0.95, "minGain": 0.02, "share": True}},
    {"name": "speed-20-sats-week", "satellites": walker(20), "targets": [point("tokyo", 139.7, 35.7)],
     "options": {"start": EPOCH, "end": EPOCH + 7 * DAY, "maxOffNadir": None, "daylight": "optical", "minSunElevation": 10}},
]


def python_run(case):
    sats = []
    for s in case["satellites"]:
        sat = satellites_from_text(s["tle"], 30, 0, 10)[0]
        sats.append(replace(sat, **s["spec"]))
    targets = [PlanTarget(t["label"], tuple(t["center"]), [tuple(p) for p in t["points"]], [tuple(c) for c in t["cells"]] if t.get("cells") else None, priority=t.get("priority")) for t in case["targets"]]
    o = case["options"]
    options = PlanOptions(o["start"], o["end"], o.get("maxOffNadir"), o["daylight"], o["minSunElevation"], now=o.get("now"),
                          minIncidence=o.get("minIncidence"), maxIncidence=o.get("maxIncidence"), pass_=o.get("pass"), side=o.get("side"),
                          stations=[GroundStation(g["name"], g["lon"], g["lat"], g["minElevation"]) for g in o.get("stations", [])])
    t0 = time.perf_counter()
    result = plan_access(sats, targets, options)
    out = {"opportunities": result.opportunities, "problems": result.problems, "tooSoon": result.tooSoon, "ms": (time.perf_counter() - t0) * 1000}
    if case.get("cover"):
        c = case["cover"]
        out["cover"] = cover_plan(result.opportunities, targets, sats, c["goal"], c["minGain"], c["share"])
    return out


def node_case(case):
    c = dict(case)
    c["options"] = {k: v for k, v in case["options"].items() if v is not None}
    return c


def compare_ops(label, theirs, ours, errors):
    if len(theirs) != len(ours):
        errors.append(f"{label}: 件数 ビューア {len(theirs)}・プラグイン {len(ours)}")
    worst = {"time": 0.0, "angle": 0.0, "coverage": 0.0}
    for a, b in zip(sorted(theirs, key=lambda o: (o["time"], o["satellite"], o["target"])), sorted(ours, key=lambda o: (o.time, o.satellite, o.target))):
        if (a["satellite"], a["target"], a["side"], a["ascending"]) != (b.satellite, b.target, b.side, b.ascending):
            errors.append(f"{label}: 別の機会 {a['time']} / {b.time}")
            continue
        dt = abs(a["time"] - b.time) / 1000
        da = max(abs(a["offNadir"] - b.offNadir), abs(a["incidence"] - b.incidence), abs(a["sunElevation"] - b.sunElevation), abs(a["elevation"] - b.elevation))
        dc = abs(a["coverage"] - b.coverage)
        worst = {"time": max(worst["time"], dt), "angle": max(worst["angle"], da), "coverage": max(worst["coverage"], dc)}
        if dt > 1 or da > 0.05 or dc > 0.01:
            errors.append(f"{label}: {a['time']} の差 時刻 {dt:.3f} s・角度 {da:.4f}°・被覆 {dc * 100:.2f}%")
        if ("downlink" in a) != (b.downlink is not None) or ("downlink" in a and (abs(a["downlink"][0] - b.downlink[0]) > 1000 or a["downlink"][1] != b.downlink[1])):
            errors.append(f"{label}: {a['time']} の受信見込みの差")
        if "shot" in a and (a["shot"], a["shotTargets"]) != (b.shot, b.shotTargets):
            errors.append(f"{label}: {a['time']} の撮像番号の差 {a['shot']}/{b.shot}")
    return worst


def main(src: str) -> int:
    with tempfile.TemporaryDirectory() as tmp:
        cases_path = os.path.join(tmp, "cases.json")
        out_path = os.path.join(tmp, "out.json")
        with open(cases_path, "w", encoding="utf-8") as f:
            json.dump([node_case(c) for c in CASES], f)
        subprocess.run(["node", os.path.join(HERE, "run_viewer.mjs"), src, cases_path, out_path], check=True)
        with open(out_path, encoding="utf-8") as f:
            viewer = json.load(f)
    failed = 0
    for case in CASES:
        theirs = viewer[case["name"]]
        ours = python_run(case)
        errors: list[str] = []
        if theirs["problems"] != ours["problems"] or theirs["tooSoon"] != ours["tooSoon"]:
            errors.append(f"problems・tooSoon の差 {theirs['problems']}/{ours['problems']} {theirs['tooSoon']}/{ours['tooSoon']}")
        worst = compare_ops("機会", theirs["opportunities"], ours["opportunities"], errors)
        if case.get("cover"):
            for p, q in zip(theirs["cover"], ours["cover"]):
                compare_ops(f"組み合わせ（対象 {p['target']}）", p["steps"], q.steps, errors)
                if (p["done"] is None) != (q.done is None) or abs(p["coverage"] - q.coverage) > 0.01 or p["blocked"] != q.blocked:
                    errors.append(f"組み合わせ（対象 {p['target']}）: 完了 {p['done']}/{q.done}・被覆 {p['coverage']:.3f}/{q.coverage:.3f}・見送り {p['blocked']}/{q.blocked}")
        status = "一致" if not errors else "不一致"
        print(f"{case['name']}: {status}（機会 {len(ours['opportunities'])} 件、最大差 時刻 {worst['time']:.3f} s・角度 {worst['angle']:.5f}°・被覆 {worst['coverage'] * 100:.3f}%、"
              f"計算 ビューア {theirs['ms']:.0f} ms・プラグイン {ours['ms']:.0f} ms）")
        for e in errors[:10]:
            print("  " + e)
        failed += bool(errors)
    return 1 if failed else 0


if __name__ == "__main__":
    if len(sys.argv) != 2:
        print(__doc__)
        sys.exit(2)
    sys.exit(main(sys.argv[1]))
