"""
Builds the plugin's zip for QGIS (Plugins → Install from ZIP): dist/satellite_observation-<version>.zip, tests left out.

    python3 scripts/build_zip.py                      # the plugin alone (or with satellite_observation/config.json if present)
    python3 scripts/build_zip.py --config my.json     # with a settings file shipped as satellite_observation/config.json
    python3 scripts/build_zip.py --win64              # the Windows zip: -win64.zip, with onnxruntime, sgp4 and tzdata inside

The Windows zip carries ONNX Runtime (for the AI tools, built for QGIS's
Python 3.12), sgp4 (the C++ accelerated orbits, any Python from 3.11) and
tzdata (the time zone database Windows lacks, for the settings file's
timeZone) in satellite_observation/ext/win64, used when QGIS has none of its
own. Their wheels are downloaded with pip (from PyPI, or the index pip is set
to) unless given with --onnxruntime-wheel / --sgp4-wheel / --tzdata-wheel.

A shipped settings file is read when the person has not chosen one in the
settings dialog. It is checked first: the zip is not made when it cannot be
read. Tokens in it travel with the zip, so prefer QGIS authentication
configurations for services that need a sign-in.
"""
import argparse
import configparser
import glob
import os
import subprocess
import sys
import zipfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PLUGIN = os.path.join(ROOT, "satellite_observation")
SKIP_DIRS = {"__pycache__", "tests", ".pytest_cache"}
#: Shipped settings files in the plugin folder (left out when --config gives another).
BUNDLED = ("config.json", "config.jsonc")
#: Where the Windows zip keeps its packages.
WIN64 = "satellite_observation/ext/win64"
#: What of ONNX Runtime's package is carried: its core (the converters, quantization and tools are left out).
ONNXRUNTIME_KEEP = {"__init__.py", "capi", "LICENSE", "ThirdPartyNotices.txt", "Privacy.md"}
#: QGIS's Python on Windows, which the carried ONNX Runtime is built for.
WIN_PYTHON = "3.12"


def check_config(path: str) -> list[str]:
    """What is wrong in a settings file (as the plugin reads it), and a note when it holds tokens."""
    sys.path.insert(0, ROOT)
    from satellite_observation.core.config import read_config_file

    _, problems = read_config_file(path)
    with open(path, encoding="utf-8-sig") as f:
        if '"token"' in f.read():
            print(f"注意: {os.path.basename(path)} に token あり。zip を受け取った人すべてに渡る", file=sys.stderr)
    return problems


def wheel(package: str, given: str, cache: str, tag: str = "win_amd64") -> str:
    """A Windows (win_amd64, or `tag`: `none-any` for pure Python) wheel of `package`: the one given, else one downloaded with pip into `cache`."""
    if given:
        return given
    os.makedirs(cache, exist_ok=True)
    found = sorted(glob.glob(os.path.join(cache, f"{package}-*{tag}.whl")))
    if found:
        return found[-1]
    subprocess.run([sys.executable, "-m", "pip", "download", package, "--only-binary=:all:", "--platform", "win_amd64",
                    "--python-version", WIN_PYTHON, "--no-deps", "-d", cache], check=True)
    found = sorted(glob.glob(os.path.join(cache, f"{package}-*{tag}.whl")))
    if not found:
        raise SystemExit(f"{package} の Windows 用 wheel を取得できず")
    return found[-1]


def wheel_files(path: str, package: str, keep=None, skip=()) -> list[tuple[str, bytes]]:
    """The files of `package` in a wheel (top-level names in `keep` when given, `skip` left out), with its licence."""
    out = []
    with zipfile.ZipFile(path) as w:
        for info in w.infolist():
            parts = info.filename.split("/")
            if info.is_dir():
                continue
            if parts[0] == package and len(parts) > 1:
                if (keep is not None and parts[1] not in keep) or parts[-1] in skip or "__pycache__" in parts:
                    continue
                out.append((info.filename, w.read(info)))
            elif parts[0].endswith(".dist-info") and parts[-1].startswith("LICENSE"):
                out.append((f"{package}/{parts[-1]}", w.read(info)))
    if not out:
        raise SystemExit(f"{os.path.basename(path)} に {package} なし")
    return out


def main(config: str = "", out_dir: str = "", win64: bool = False, onnxruntime_wheel: str = "", sgp4_wheel: str = "",
         tzdata_wheel: str = "") -> str:
    meta = configparser.ConfigParser()
    meta.read(os.path.join(PLUGIN, "metadata.txt"), encoding="utf-8")
    version = meta["general"]["version"]
    shipped = config or next((os.path.join(PLUGIN, n) for n in BUNDLED if os.path.isfile(os.path.join(PLUGIN, n))), "")
    if shipped:
        problems = check_config(shipped)
        if problems:
            raise SystemExit("設定ファイルの問題:\n" + "\n".join(problems))
    out_dir = out_dir or os.path.join(ROOT, "dist")
    os.makedirs(out_dir, exist_ok=True)
    carried: list[tuple[str, bytes]] = []
    if win64:
        cache = os.path.join(ROOT, "dist", "wheels")
        carried += wheel_files(wheel("onnxruntime", onnxruntime_wheel, cache), "onnxruntime", ONNXRUNTIME_KEEP)
        carried += wheel_files(wheel("sgp4", sgp4_wheel, cache), "sgp4", skip=("tests.py", "SGP4-VER.TLE", "tcppver.out"))
        carried += wheel_files(wheel("tzdata", tzdata_wheel, cache, "none-any"), "tzdata")
    target = os.path.join(out_dir, f"satellite_observation-{version}{'-win64' if win64 else ''}.zip")
    with zipfile.ZipFile(target, "w", zipfile.ZIP_DEFLATED) as z:
        for folder, dirs, files in os.walk(PLUGIN):
            dirs[:] = sorted(d for d in dirs if d not in SKIP_DIRS)
            for name in sorted(files):
                if name.endswith(".pyc") or (config and folder == PLUGIN and name in BUNDLED):
                    continue
                path = os.path.join(folder, name)
                z.write(path, os.path.relpath(path, ROOT))
        if config:
            z.write(config, os.path.join("satellite_observation", "config.jsonc" if config.endswith(".jsonc") else "config.json"))
        for name, data in carried:
            z.writestr(f"{WIN64}/{name}", data)
    return target


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--config", default="", help="同梱する設定ファイル（config.json）")
    parser.add_argument("--win64", action="store_true", help="Windows 版（onnxruntime・sgp4・tzdata を同梱、-win64.zip）")
    parser.add_argument("--onnxruntime-wheel", default="", help="同梱する onnxruntime の wheel（cp312 win_amd64、指定なしで pip で取得）")
    parser.add_argument("--tzdata-wheel", default="", help="同梱する tzdata の wheel（指定なしで pip で取得）")
    parser.add_argument("--sgp4-wheel", default="", help="同梱する sgp4 の wheel（win_amd64、指定なしで pip で取得）")
    args = parser.parse_args()
    print(main(args.config, win64=args.win64, onnxruntime_wheel=args.onnxruntime_wheel, sgp4_wheel=args.sgp4_wheel,
               tzdata_wheel=args.tzdata_wheel))
