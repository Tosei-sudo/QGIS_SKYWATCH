"""
Builds the plugin's zip for QGIS (Plugins → Install from ZIP): dist/satellite_observation-<version>.zip, tests left out.

    python3 scripts/build_zip.py                      # the plugin alone (or with satellite_observation/config.json if present)
    python3 scripts/build_zip.py --config my.json     # with a settings file shipped as satellite_observation/config.json

A shipped settings file is read when the person has not chosen one in the
settings dialog. It is checked first: the zip is not made when it cannot be
read. Tokens in it travel with the zip, so prefer QGIS authentication
configurations for services that need a sign-in.
"""
import argparse
import configparser
import os
import sys
import zipfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PLUGIN = os.path.join(ROOT, "satellite_observation")
SKIP_DIRS = {"__pycache__", "tests", ".pytest_cache"}
#: Shipped settings files in the plugin folder (left out when --config gives another).
BUNDLED = ("config.json", "config.jsonc")


def check_config(path: str) -> list[str]:
    """What is wrong in a settings file (as the plugin reads it), and a note when it holds tokens."""
    sys.path.insert(0, ROOT)
    from satellite_observation.core.config import read_config_file

    _, problems = read_config_file(path)
    with open(path, encoding="utf-8-sig") as f:
        if '"token"' in f.read():
            print(f"注意: {os.path.basename(path)} に token あり。zip を受け取った人すべてに渡る", file=sys.stderr)
    return problems


def main(config: str = "", out_dir: str = "") -> str:
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
    target = os.path.join(out_dir, f"satellite_observation-{version}.zip")
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
    return target


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--config", default="", help="同梱する設定ファイル（config.json）")
    print(main(parser.parse_args().config))
