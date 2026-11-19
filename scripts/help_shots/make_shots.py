"""
Takes the screenshots of the help's 「画面つきの手順書」 (mikage/help/index.html,
section #howto) again, in a real QGIS on a virtual screen against the Esri
mock (scripts/mock_esri), and writes them to mikage/help/images/.

    python3.12 scripts/help_shots/make_shots.py [--flows obs,request,plan,check,layer] [--raw フォルダ] [--keep]

Run it with the Python QGIS uses (it needs qgis.PyQt and GDAL's osgeo).

Needs (Ubuntu): qgis, python3-qgis, qgis-providers, xvfb, fonts-noto-cjk,
fonts-open-sans, pngquant (optional: smaller files). The plugin is used from
this repository (a link in a throwaway QGIS profile); nothing is installed.
The pictures show the mock's sample data and the dates of the day they are
taken. Not part of the plugin's zip.
"""
from __future__ import annotations

import argparse
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(os.path.dirname(HERE))
PLUGIN = os.path.join(REPO, "mikage")
IMAGES = os.path.join(PLUGIN, "help", "images")
MOCK = os.path.join(REPO, "scripts", "mock_esri", "mock_esri.py")
#: The user the pictures show (分担・依頼グループ・作成者).
USER = "tanaka"

#: The optical system's 採否 file as the mock's system would answer (for 「結果ファイルの取り込み」).
RESULT_FILE = """
      "resultFile": { "format": "csv", "encoding": "cp932",
        "columns": [ { "key": "依頼ID", "target": "id" },
                     { "key": "判定", "target": "result", "map": { "○": "採用", "×": "不採用" } },
                     { "key": "理由", "target": "reason" },
                     { "key": "撮像予定", "target": "plannedAt", "format": "%Y/%m/%d %H:%M" } ] },"""


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def write_config(source: str, target: str, export_folder: str) -> None:
    """The mock's settings file plus a result file for the optical system and the systems' output folder."""
    with open(source, encoding="utf-8") as f:
        text = f.read()
    folder = export_folder.replace("\\", "/")
    text = text.replace('"system": "MOCK-OPT",', f'"system": "MOCK-OPT", "folder": "{folder}",' + RESULT_FILE, 1)
    text = text.replace('"system": "MOCK-SAR",', f'"system": "MOCK-SAR", "folder": "{folder}",', 1)
    # No external AI results: their layers and dock would cover the steps shown.
    text = text.replace('"aiResultSources":', '"_aiResultSources":', 1)
    with open(target, "w", encoding="utf-8") as f:
        f.write(text)


def profile(root: str, config: str) -> None:
    folder = os.path.join(root, "profiles", "default")
    os.makedirs(os.path.join(folder, "python", "plugins"))
    os.makedirs(os.path.join(folder, "QGIS"))
    os.symlink(PLUGIN, os.path.join(folder, "python", "plugins", "mikage"))
    # QSettings reads an INI file's bytes as Latin-1: anything else is written as \xXXXX (else 田中 shows garbled).
    recorder = "".join(c if ord(c) < 128 else f"\\x{ord(c):04x}" for c in "田中")
    with open(os.path.join(folder, "QGIS", "QGIS3.ini"), "w", encoding="utf-8") as f:
        f.write(f"""[PythonPlugins]
mikage=true

[mikage]
firstSetupDone=1
config_source={config}
recorder={recorder}

[qgis]
showTips340=false
checkVersion=false
""")


def shrink(raw: str, out: str) -> None:
    """The pictures to the help's folder, made smaller by pngquant when it is there."""
    os.makedirs(out, exist_ok=True)
    quant = shutil.which("pngquant")
    for name in sorted(os.listdir(raw)):
        if not name.endswith(".png"):
            continue
        source, target = os.path.join(raw, name), os.path.join(out, name)
        if quant:
            subprocess.run([quant, "--force", "--quality", "70-95", "--skip-if-larger", "--output", target, source], check=False)
        if not os.path.exists(target) or not quant:
            shutil.copyfile(source, target)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--flows", default="obs,request,plan,check,layer", help="撮る手順（obs・request・plan・check・layer のカンマ区切り、この順で）")
    parser.add_argument("--raw", help="加工前の画像を置くフォルダ（既定は一時フォルダ）")
    parser.add_argument("--out", default=IMAGES, help=f"画像の出力先（既定 {IMAGES}）")
    parser.add_argument("--keep", action="store_true", help="一時フォルダを消さない")
    args = parser.parse_args()

    work = tempfile.mkdtemp(prefix="mikage-")
    raw = args.raw or os.path.join(work, "raw")
    os.makedirs(raw, exist_ok=True)
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
        # Images that look like images (the mock's are 512 px cartoons).
        made = subprocess.run([sys.executable, os.path.join(HERE, "scenes.py"), os.path.join(work, "mock")],
                              env=dict(env, QT_QPA_PLATFORM="offscreen"), capture_output=True, text=True)
        if made.returncode:
            sys.exit("見本の画像を作れませんでした（QGIS の Python で実行してください）:\n" + made.stderr[-2000:])
        config = os.path.join(work, "config.jsonc")
        exports = os.path.join(work, "撮像計画ファイル")
        os.makedirs(exports)
        write_config(os.path.join(work, "mock", "config.mock.jsonc"), config, exports)
        profiles = os.path.join(work, "profiles")
        profile(profiles, config)
        env.update(SHOTS=raw, SHOTS_CODE=HERE, WORK=work, FLOWS=args.flows, EXPORTS=exports, BASEMAP=os.path.join(work, "mock", "basemap.tif"))
        cmd = ["xvfb-run", "-a", "-s", "-screen 0 1700x1060x24 -dpi 96", "qgis", "--profiles-path", profiles, "--nologo", "--noversioncheck",
               "--code", os.path.join(HERE, "start.py")]
        with open(os.path.join(work, "qgis.log"), "w") as log:
            subprocess.run(cmd, env=env, stdout=log, stderr=subprocess.STDOUT, timeout=900, check=False)
        with open(os.path.join(raw, "scenario.log"), encoding="utf-8") as f:
            print(f.read())
        shrink(raw, args.out)
    finally:
        mock.terminate()
        if not args.keep:
            shutil.rmtree(work, ignore_errors=True)
        else:
            print("一時フォルダ:", work)
    return 0


if __name__ == "__main__":
    sys.exit(main())
