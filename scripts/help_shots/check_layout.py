"""
Checks the plugin's docks and windows for what is drawn over something else
or cut off, in a real QGIS on a virtual screen against the Esri mock
(scripts/mock_esri), as make_shots.py does for the help's pictures.

    python3.12 scripts/help_shots/check_layout.py --out フォルダ [--widths 440,300] [--parts docks,dialogs,forms,feed] [--only 名前,…]

Writes a picture of each dock (each tab, at each width) and window, and
problems.json (what mikage/tests/layout_probe.py found) to the folder.
Needs what make_shots.py needs. Not part of the plugin's zip.
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from make_shots import MOCK, USER, free_port, profile, write_config  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--out", required=True, help="画像と problems.json の出力先")
    parser.add_argument("--widths", default="440,300", help="ドックの幅（px、カンマ区切り）")
    parser.add_argument("--parts", default="docks,dialogs,forms,feed,filled", help="見るもの（docks・dialogs・forms・feed・filled）")
    parser.add_argument("--only", default="", help="このドック名・メニュー項目だけ（カンマ区切り）")
    parser.add_argument("--font", type=float, default=0, help="QGIS の文字の大きさ（pt、0 は既定）")
    parser.add_argument("--keep", action="store_true", help="一時フォルダを消さない")
    args = parser.parse_args()

    out = os.path.abspath(args.out)
    os.makedirs(out, exist_ok=True)
    work = tempfile.mkdtemp(prefix="mikage-layout-")
    env = dict(os.environ, LOGNAME=USER, USER=USER, USERNAME=USER, LANG="ja_JP.UTF-8", TZ="Asia/Tokyo")
    port = free_port()
    mock = subprocess.Popen([sys.executable, MOCK, "--data", os.path.join(work, "mock"), "--port", str(port), "--reset", "--quiet"],
                            env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    try:
        base = f"http://127.0.0.1:{port}/arcgis/rest/services"
        for _ in range(100):
            try:
                urllib.request.urlopen(base + "?f=json", timeout=1)
                break
            except OSError:
                time.sleep(0.2)
        config = os.path.join(work, "config.jsonc")
        exports = os.path.join(work, "exports")
        os.makedirs(exports)
        write_config(os.path.join(work, "mock", "config.mock.jsonc"), config, exports)
        with open(config, encoding="utf-8") as f:
            text = f.read()
        with open(config, "w", encoding="utf-8") as f:  # the AI results too (make_shots leaves them out)
            f.write(text.replace('"_aiResultSources":', '"aiResultSources":', 1))
        profiles = os.path.join(work, "profiles")
        profile(profiles, config)
        if args.font:
            ini = os.path.join(profiles, "profiles", "default", "QGIS", "QGIS3.ini")
            with open(ini, encoding="utf-8") as f:
                text = f.read()
            with open(ini, "w", encoding="utf-8") as f:
                f.write(text.replace("[qgis]\n", f"[qgis]\nfontPointSize={args.font}\n", 1))
        env.update(SHOTS=out, WORK=work, WIDTHS=args.widths, PARTS=args.parts, ONLY=args.only)
        start = os.path.join(work, "start.py")
        with open(start, "w", encoding="utf-8") as f:
            f.write(f"import sys, os, traceback\nsys.path.insert(0, {HERE!r})\n"
                    "try:\n    import layout_scenario  # noqa: F401\nexcept Exception:\n"
                    "    open(os.path.join(os.environ['SHOTS'], 'scenario.log'), 'a').write(traceback.format_exc())\n    os._exit(1)\n")
        cmd = ["xvfb-run", "-a", "-s", "-screen 0 1700x1060x24 -dpi 96", "qgis", "--profiles-path", profiles, "--nologo", "--noversioncheck",
               "--code", start]
        with open(os.path.join(out, "qgis.log"), "w") as log:
            subprocess.run(cmd, env=env, stdout=log, stderr=subprocess.STDOUT, timeout=1800, check=False)
        with open(os.path.join(out, "scenario.log"), encoding="utf-8") as f:
            print(f.read()[-6000:])
        found = os.path.join(out, "problems.json")
        if os.path.exists(found):
            with open(found, encoding="utf-8") as f:
                data = json.load(f)
            bad = {k: v for k, v in data.items() if v["problems"]}
            print(f"{len(data)} 画面中 {len(bad)} 画面に問題")
    finally:
        mock.terminate()
        if not args.keep:
            shutil.rmtree(work, ignore_errors=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
