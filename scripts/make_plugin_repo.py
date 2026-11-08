"""
Makes (or updates) an internal QGIS plugin repository: a folder holding the plugin's zips and the plugins.xml that
QGIS's plugin manager reads, for closed networks where plugins.qgis.org cannot be reached.

    python3 scripts/make_plugin_repo.py \\\\server\\share\\qgis-plugins            # builds both zips with a new version, lists them
    python3 scripts/make_plugin_repo.py /srv/www/qgis-plugins --url http://intra.example/qgis-plugins
    python3 scripts/make_plugin_repo.py OUT --config my.json --lock-config     # build_zip.py's options pass through
    python3 scripts/make_plugin_repo.py OUT --zip dist/a.zip dist/a-win64.zip  # zips already built (their own versions)

The folder then holds:

    plugins.xml            the repository for the regular zip (each PC adds this URL in QGIS)
    plugins-win64.xml      the repository for the Windows zip (onnxruntime, sgp4 and tzdata inside)
    satellite_observation.<version>.zip / satellite_observation.<version>.win64.zip
    icon.svg

QGIS offers an update when the listed version is greater than the installed one, so each build gets a new version:
metadata.txt's version followed by the UTC time, e.g. 0.4.0.20261108123000 (or --version). A zip whose version is not
greater than the one already listed is refused unless --allow-older (going back to an older release).

The download address of each zip is `--url` + its file name. Without --url it is the folder's file:// address
(file://server/share/... for a UNC path), which QGIS reads like an http:// one.
"""
import argparse
import configparser
import datetime
import os
import pathlib
import shutil
import sys
import urllib.parse
import xml.etree.ElementTree as ET
import zipfile

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
PLUGIN_ID = "satellite_observation"
#: Repository files, by kind of zip.
XML_NAMES = {"regular": "plugins.xml", "win64": "plugins-win64.xml"}
WIN64_DIR = f"{PLUGIN_ID}/ext/win64/"


def build_module():
    sys.path.insert(0, HERE)
    import build_zip

    return build_zip


def base_version() -> str:
    meta = configparser.ConfigParser()
    meta.read(os.path.join(ROOT, PLUGIN_ID, "metadata.txt"), encoding="utf-8")
    return meta["general"]["version"]


def release_version(now: datetime.datetime | None = None) -> str:
    """metadata.txt's version with the UTC time to the second after it: 0.4.0 → 0.4.0.20261108123000 (always greater than the last build's)."""
    now = now or datetime.datetime.now(datetime.timezone.utc)
    return f"{base_version()}.{now:%Y%m%d%H%M%S}"


# --- QGIS's version comparison (python/pyplugin_installer/version_compare.py), so the check here agrees with it ---
def _chop(s: str) -> list[str]:
    def kind(c):
        return 0 if c in ".-_ " else 1 if c.isdigit() else 2

    parts = [s[0]]
    for prev, c in zip(s, s[1:]):
        if kind(c) == 0:
            continue
        if kind(c) == kind(prev):
            parts[-1] += c
        else:
            parts.append(c)
    return parts


def _element(a: str, b: str) -> int:
    if a == b:
        return 0
    if a.isnumeric() and b.isnumeric() and a[0] != "0" and b[0] != "0":
        return 0 if float(a) == float(b) else 1 if float(a) > float(b) else 2
    words = ["ALPHA", "BETA", "PREVIEW", "RC", "TRUNK"]
    a = a if a in words else "Z" + a
    b = b if b in words else "Z" + b
    return 1 if a > b else 2


def compare_versions(a: str, b: str) -> int:
    """0 when equal, 1 when a is greater, 2 when b is greater (as QGIS's plugin manager decides)."""
    if not a or not b:
        return 0
    a, b = a.strip().upper(), b.strip().upper()
    if a == b:
        return 0
    v1, v2 = _chop(a), _chop(b)
    for x, y in zip(v1, v2):
        if _element(x, y):
            return _element(x, y)
    if len(v1) > len(v2):
        return _element(v1[len(v2)], " ")
    if len(v2) > len(v1):
        return _element(" ", v2[len(v1)])
    return 0


# --- zips ---
def read_zip(path: str) -> dict:
    """The plugin's metadata in a zip, and whether it is the Windows one."""
    with zipfile.ZipFile(path) as z:
        names = z.namelist()
        if f"{PLUGIN_ID}/metadata.txt" not in names:
            raise SystemExit(f"{os.path.basename(path)} はこのプラグインの zip ではない（{PLUGIN_ID}/metadata.txt なし）")
        meta = configparser.ConfigParser(interpolation=None)
        meta.read_string(z.read(f"{PLUGIN_ID}/metadata.txt").decode("utf-8"))
    info = dict(meta["general"])
    info["kind"] = "win64" if any(n.startswith(WIN64_DIR) for n in names) else "regular"
    return info


def file_name(version: str, kind: str) -> str:
    """The zip's name in the repository. QGIS takes the plugin's id from the part before the first dot, so it must start with `satellite_observation.`."""
    return f"{PLUGIN_ID}.{version}{'.win64' if kind == 'win64' else ''}.zip"


def folder_url(folder: str) -> str:
    """The folder's file:// address (file://server/share/... for \\\\server\\share\\...)."""
    if folder.startswith("\\\\") or folder.startswith("//"):
        parts = folder.replace("\\", "/").lstrip("/").split("/")
        return "file://" + parts[0] + "/" + "/".join(urllib.parse.quote(p) for p in parts[1:] if p)
    return pathlib.Path(os.path.abspath(folder)).as_uri()


def listed_version(xml_path: str) -> str:
    """The version of this plugin already listed in a repository file ("" when none)."""
    if not os.path.isfile(xml_path):
        return ""
    try:
        root = ET.parse(xml_path).getroot()
    except ET.ParseError:
        return ""
    for node in root.iter("pyqgis_plugin"):
        name = (node.findtext("file_name") or "").strip()
        if name.partition(".")[0] == PLUGIN_ID:
            return node.get("version", "")
    return ""


def plugin_xml(info: dict, download_url: str, name: str, icon_url: str, changelog: str, experimental: bool,
               today: str) -> bytes:
    """A repository file listing one release, in the format of plugins.qgis.org's plugins.xml."""
    root = ET.Element("plugins")
    p = ET.SubElement(root, "pyqgis_plugin", name=info.get("name", PLUGIN_ID), version=info["version"])

    def put(tag, text):
        ET.SubElement(p, tag).text = text

    put("description", info.get("description", ""))
    put("about", info.get("about", ""))
    put("version", info["version"])
    put("qgis_minimum_version", info.get("qgisminimumversion", "3.0"))
    put("qgis_maximum_version", info.get("qgismaximumversion", "3.99"))
    put("homepage", info.get("homepage", info.get("repository", "")))
    put("file_name", name)
    put("icon", icon_url)
    put("author_name", info.get("author", ""))
    put("download_url", download_url)
    put("uploaded_by", info.get("author", ""))
    put("create_date", today)
    put("update_date", today)
    put("experimental", "True" if experimental else "False")
    put("deprecated", "False")
    put("tracker", info.get("tracker", ""))
    put("repository", info.get("repository", ""))
    put("tags", info.get("tags", ""))
    put("trusted", "True")
    if changelog:
        put("changelog", changelog)
    ET.indent(root)
    return b'<?xml version="1.0" encoding="UTF-8"?>\n' + ET.tostring(root, encoding="utf-8") + b"\n"


def write_atomically(path: str, data: bytes) -> None:
    tmp = path + ".tmp"
    with open(tmp, "wb") as f:
        f.write(data)
    os.replace(tmp, path)


def publish(out: str, zips: list[str], url: str = "", changelog: str = "", experimental: bool = False,
            allow_older: bool = False, today: str = "") -> list[str]:
    """Copies the zips into `out` and writes the repository file of each kind. Returns the repository files' addresses."""
    os.makedirs(out, exist_ok=True)
    base = (url or folder_url(out)).rstrip("/")
    today = today or datetime.date.today().isoformat()
    infos = [(z, read_zip(z)) for z in zips]
    kinds = [i["kind"] for _, i in infos]
    if len(set(kinds)) != len(kinds):
        raise SystemExit("同じ種類（通常版・Windows 版）の zip が 2 つある")
    # Check every zip before changing anything.
    for z, info in infos:
        listed = listed_version(os.path.join(out, XML_NAMES[info["kind"]]))
        if listed and compare_versions(info["version"], listed) != 1 and not allow_older:
            raise SystemExit(f"{os.path.basename(z)} の版 {info['version']} は掲載中の {listed} より新しくない。"
                             "QGIS は更新と見なさない（戻すときは --allow-older）")
    icon = os.path.join(ROOT, PLUGIN_ID, "resources", "icon.svg")
    if os.path.isfile(icon):
        shutil.copyfile(icon, os.path.join(out, "icon.svg"))
    addresses = []
    for z, info in infos:
        name = file_name(info["version"], info["kind"])
        target = os.path.join(out, name)
        # The zip first, then the list: a PC reading the list never finds a zip missing.
        shutil.copyfile(z, target + ".tmp")
        os.replace(target + ".tmp", target)
        xml = os.path.join(out, XML_NAMES[info["kind"]])
        write_atomically(xml, plugin_xml(info, f"{base}/{urllib.parse.quote(name)}", name, f"{base}/icon.svg",
                                         changelog, experimental, today))
        addresses.append(f"{base}/{XML_NAMES[info['kind']]}")
    return addresses


def main(argv=None) -> list[str]:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("out", help="リポジトリのフォルダー（共有フォルダーか Web サーバーの公開フォルダー）")
    parser.add_argument("--url", default="", help="そのフォルダーのアドレス（http://… か file://…、指定なしでフォルダーの file:// アドレス）")
    parser.add_argument("--zip", nargs="+", default=[], help="作ってある zip を載せる（指定なしで通常版と Windows 版を新しい版で作る）")
    parser.add_argument("--version", default="", help="作る zip の版（指定なしで metadata.txt の版 + UTC の日時）")
    parser.add_argument("--no-win64", action="store_true", help="Windows 版を作らない")
    parser.add_argument("--no-regular", action="store_true", help="通常版を作らない")
    parser.add_argument("--config", default="", help="同梱する設定ファイル（build_zip.py --config）")
    parser.add_argument("--lock-config", action="store_true", help="同梱の設定ファイルに固定（build_zip.py --lock-config）")
    parser.add_argument("--changelog", default="", help="プラグインの管理に出す変更点")
    parser.add_argument("--experimental", action="store_true", help="実験版として載せる（各 PC で「実験的なプラグインも表示する」が要る）")
    parser.add_argument("--allow-older", action="store_true", help="掲載中より古い版でも載せる（前の版に戻すとき）")
    args = parser.parse_args(argv)
    zips = list(args.zip)
    if not zips:
        if args.no_win64 and args.no_regular:
            raise SystemExit("--no-win64 と --no-regular を両方付けると作るものがない")
        b = build_module()
        version = args.version or release_version()
        work = os.path.join(ROOT, "dist", "repo-build")
        if not args.no_regular:
            zips.append(b.main(args.config, work, lock_config=args.lock_config, version=version))
        if not args.no_win64:
            zips.append(b.main(args.config, work, win64=True, lock_config=args.lock_config, version=version))
    addresses = publish(args.out, zips, args.url, args.changelog, args.experimental, args.allow_older)
    for a in addresses:
        print(a)
    return addresses


if __name__ == "__main__":
    main()
