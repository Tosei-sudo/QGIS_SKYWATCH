"""
Runs the same texts and points through the viewer's coordinate notations
(Node: run_coords.mjs bundling coordinate-notation's src/index.ts with its
mgrs) and this plugin's core.coords, and checks they agree: the same text
written, and the same point read (±1e-9°) or both unreadable.

    cd scripts/compare_viewer && npm install
    python3 coords.py /path/to/browser-image-enhancement/packages/coordinate-notation/src/index.ts

Exit status 0 when everything agrees.
"""
from __future__ import annotations

import json
import os
import random
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(os.path.dirname(HERE)))

from satellite_observation.core import coords  # noqa: E402

#: The viewer's own test cases (coordinate-notation test/unit/coordinates.test.ts) and more.
TEXTS = [
    "35.6812, 139.7671", "35.6812 139.7671", "35.6812,139.7671", "N35.6812 E139.7671", "35.6812N 139.7671E",
    "139.7671E, 35.6812N", "E139.7671 N35.6812", "35°40'52.32\"N 139°46'1.56\"E", "35 40 52.32 139 46 1.56",
    "35°40.872′N, 139°46.026′E", "33°27'S 70°36'W", "北緯35度40分52.32秒 東経139度46分1.56秒", "北緯35.6812度、東経139.7671度",
    "３５．６８１２，１３９．７６７１", "35d40m52.32s N 139d46m1.56s E", "35D40M52.32SN, 139D46M1.56SE", "354052.32N 1394601.56E",
    "354052.32 1394601.56", "35°40′52.32″N139°46′01.56″E", "lat 35.6812 lon 139.7671", "Lng: 139.7671, Lat: 35.6812",
    "緯度 35.6812 経度 139.7671", "(35.6812, 139.7671)", "35.6812; 139.7671", "35.6812 / 139.7671", "139.7671, 35.6812",
    "POINT(139.7671 35.6812)", "35°40.872 139°46.026", "南緯33度27分 西経70度36分", "−33.45, −70.6", "lat -33.45 lon -70.6",
    "-33.45, -70.6", "54SUE8843349290", "54s ue 88433 49290", "54SUE884492", "54N 388433 3949290", "54N 388433mE 3949290mN",
    "54N, 388433, 3949290", "", "hello", "95, 100", "354099N 1394601E", "10, 200", "54SUE884", "35.6", "61N 500000 4000000",
    "54SUE", "4QFJ12345678", "4QFJ1234567", "POINT Z (139.7 35.6 10)", "35 40 139 46", "3540N 13946E", "-354052 1394601",
    "東京駅", "千代田区丸の内1丁目", "35.6812，139.7671", "N 35 40 52 E 139 46 1", "35 40 52 N 139 46 1 E", "1N 1E",
    "90, 180", "-90, -180", "91, 0", "0, 181", "33XWG1234512345", "32VNM1234567890", "60X 500000 9000000", "1C 500000 1200000",
    "lat:35.6 long=139.7", "LATITUDE 35 LONGITUDE 139", "35.6812 139.7671 10", "35°N 139°E", "35° 139°", "0 0", "+35.5 +139.5",
    "35.5.5, 1", "354052.32123 1394601.56", "12345678 1", "54S UE 8843 4929", "31U DQ 48251 11932", "18T WL 80628 06830",
    "54SUE88433492901", "54SUE8843349290XX", "54IUE88433", "54SIE88", "54SUW8843349290", "00SUE1234", "61SUE1234",
]


def random_points(n: int, seed: int = 1) -> list[list[float]]:
    rng = random.Random(seed)
    points = [[rng.uniform(-180, 180), rng.uniform(-90, 90)] for _ in range(n)]
    # Edges: zone borders, Norway and Svalbard, bands, the equator, the poles and the antimeridian.
    points += [[180, 0], [-180, 0], [0, 0], [3, 56], [11.9, 63.9], [8.9, 72], [41.9, 83.9], [20, 84], [20, 84.1], [20, -80], [20, -80.1],
               [139.7671, 35.6812], [-70.6, -33.45], [6, 60], [-0.0000001, -0.0000001], [0, 1 - 0.001 / 3600], [12, 90], [12, -90]]
    return points


def texts_of(points: list[list[float]]) -> list[str]:
    """Each point written by the plugin in every notation (and a few variants), to be read back."""
    out = []
    for p in points:
        for write in (coords.format_lat_lon, coords.format_dms, coords.format_mgrs, coords.format_utm):
            text = write(tuple(p))
            if text:
                out += [text, text.replace(" ", ""), text.lower()]
        out.append(f"{p[0]:.4f}, {p[1]:.4f}")
    return out


def main() -> int:
    index = sys.argv[1]
    points = random_points(2000)
    texts = TEXTS + texts_of(points)
    with tempfile.TemporaryDirectory() as tmp:
        cases, out = os.path.join(tmp, "cases.json"), os.path.join(tmp, "out.json")
        with open(cases, "w", encoding="utf-8") as f:
            json.dump({"texts": texts, "points": points}, f, ensure_ascii=False)
        subprocess.run(["node", os.path.join(HERE, "run_coords.mjs"), index, cases, out], check=True)
        with open(out, encoding="utf-8") as f:
            viewer = json.load(f)
    bad = 0
    for text, theirs in zip(texts, viewer["parsed"]):
        ours = coords.parse_coordinate(text)
        same = (ours is None and theirs is None) or (
            ours is not None and theirs is not None and abs(ours[0] - theirs[0]) < 1e-9 and abs(ours[1] - theirs[1]) < 1e-9)
        if not same:
            bad += 1
            print(f"読み取りの差: {text!r} ビューア {theirs} プラグイン {ours}")
    for p, theirs in zip(points, viewer["written"]):
        t = tuple(p)
        ours = [coords.format_lat_lon(t), coords.format_dms(t), coords.format_mgrs(t), coords.format_utm(t), coords.utm_zone(t)]
        if ours != theirs:
            bad += 1
            print(f"書き出しの差: {p} ビューア {theirs} プラグイン {ours}")
    print(f"読み取り {len(texts)} 件・書き出し {len(points)} 地点、差 {bad} 件")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
