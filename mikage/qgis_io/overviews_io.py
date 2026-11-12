"""
Overviews (pyramids) for images opened from the image catalog: an image
without them gets an external `.ovr` built in the background (QGIS's task
manager, where it can be cancelled), so it draws fast when zoomed out. The
image itself is never written: GDAL builds the levels for a VRT of it in a
temporary folder, and the finished `.ovr` is moved beside the image under
its name (`image.tif.ovr`), which GDAL then reads by itself.

A folder that cannot be written gets nothing: its images' .ovr go to a cache
folder on this PC instead, through GDAL's own GDAL_PAM_PROXY_DIR (set when
the plugin is loaded, see start_cache), which GDAL reads again whenever the
image is opened; the layer still reads the image itself (RPC, file name
rules and observations unchanged). With `location` "cache" every image's
.ovr goes there: the plugin builds it into the cache folder and names it in
the image's .aux.xml kept there (OVERVIEW_FILE), registered through GDAL's
own PamAllocateProxy, which GDAL then reads instead of the one beside the
image (whose content is copied into it). The cache is cleared at the next start
when asked (clear_cache_next_start), as its files may be open now.

One image is built at a time; an image asked for again while waiting or
building is not queued twice. When one is done, the project's layers of
that image are read again so the overviews are used at once.

So that QGIS keeps drawing the map smoothly meanwhile, the levels are made
by GDAL's own gdaladdo (QGIS's, found beside its programs or on PATH) in a
process of its own: at a lower priority, on half the cores (`threads`),
with its own GDAL block cache (in QGIS the build's reads pushed out the
blocks the map was drawn from, which then had to be read again) and its
own Python (no wait for the GIL). Without gdaladdo, or when it fails, the
levels are made in QGIS as before (on `threads` cores too). An image in a
folder that cannot be written then gets its .ovr into the cache folder,
named in the .aux.xml GDAL itself keeps there (OVERVIEW_FILE).
"""
from __future__ import annotations

import ctypes
import ctypes.util
import hashlib
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import uuid
from dataclasses import dataclass
from xml.etree import ElementTree
from typing import Callable, Optional, Sequence

from osgeo import gdal
from qgis.core import Qgis, QgsApplication, QgsMapLayer, QgsMessageLog, QgsProject, QgsProviderRegistry, QgsRasterLayer

from ..core.overviews import OverviewOptions, build_threads, overview_factors
from . import settings
from .tasks import LOG_TAG, Cancelled, FunctionTask, run_task

TITLE = "ピラミッド"

#: Outcomes of a build.
BUILT, CACHED, HAS_OVERVIEWS, SMALL, READ_ONLY = "built", "cached", "has", "small", "read-only"
#: location "cache" without a cache folder, or without GDAL's functions for it.
NO_CACHE, UNSUPPORTED = "no-cache", "unsupported"

PROXY_DIR = "GDAL_PAM_PROXY_DIR"
#: The files kept in the cache folder: GDAL's index and `NNNNNN_<path>`, the plugin's `<hash>_<name>.ovr`.
PROXY_FILE = re.compile(r"^(gdal_pam_proxy\.dat|\d{6}_.+|[0-9a-f]{12}_.+\.ovr)$")
#: The folder this plugin gave GDAL_PAM_PROXY_DIR (None: none, or one set outside the plugin is used).
_ours: Optional[str] = None


def default_cache_folder() -> str:
    return os.path.join(settings.profile_folder(), "overview-cache")


def cache_folder() -> Optional[str]:
    """Where GDAL now puts what it cannot write beside an image (None: nowhere)."""
    return gdal.GetConfigOption(PROXY_DIR) or None


def start_cache() -> Optional[str]:
    """
    Points GDAL_PAM_PROXY_DIR at the cache folder, after clearing it when
    that was asked: the settings file's `cacheFolder` as last read; without
    one, the folder already set (QGIS sets its profile's `gdal_pam`, or one
    set in its options or the environment), else the plugin's in the
    profile. GDAL reads this once, the first time it looks for an image's
    .ovr or .aux.xml, so it is done when the plugin is loaded, before any
    image is opened. The folder in use, None when none.
    """
    global _ours
    pending = settings.overview_cache_to_clear()
    if pending:
        clear_folder(pending)
        settings.set_overview_cache_to_clear("")
    chosen = settings.overview_cache_setting()
    now = gdal.GetConfigOption(PROXY_DIR)
    if now and not chosen:
        # QGIS's own (the profile's gdal_pam), or one set in its options or the environment.
        return now
    folder = chosen or default_cache_folder()
    try:
        os.makedirs(folder, exist_ok=True)
    except OSError as error:
        QgsMessageLog.logMessage(f"ピラミッドのキャッシュフォルダを作れず（{folder}: {error}）", LOG_TAG, Qgis.MessageLevel.Warning)
        return None
    gdal.SetConfigOption(PROXY_DIR, folder)
    _ours = folder
    return folder


def configure_cache(options: OverviewOptions) -> Optional[str]:
    """
    Remembers the settings file's `cacheFolder` for the next start (GDAL
    keeps the folder it first read); the folder that will be used then
    when it differs from the one in use now, else None.
    """
    global current
    current = options
    wanted = options.cache_folder
    if wanted != settings.overview_cache_setting():
        settings.set_overview_cache_setting(wanted)
    now = cache_folder()
    if not wanted or not now:
        return None
    if os.path.normcase(os.path.abspath(wanted)) != os.path.normcase(os.path.abspath(now)):
        QgsMessageLog.logMessage(f"ピラミッドのキャッシュフォルダは QGIS の再起動後に {wanted} に変更（今は {now}）", LOG_TAG, Qgis.MessageLevel.Info)
        return wanted
    return None


def cache_size(folder: Optional[str]) -> tuple[int, int]:
    """The files GDAL keeps in a cache folder: how many and their bytes."""
    n = size = 0
    if folder and os.path.isdir(folder):
        for entry in os.scandir(folder):
            if entry.is_file() and PROXY_FILE.match(entry.name) and entry.name != "gdal_pam_proxy.dat":
                n += 1
                size += entry.stat().st_size
    return n, size


def clear_folder(folder: str) -> int:
    """Removes GDAL's files from a cache folder (only those: the folder may hold others); how many."""
    n = 0
    if not os.path.isdir(folder):
        return 0
    for entry in os.scandir(folder):
        if entry.is_file() and PROXY_FILE.match(entry.name):
            try:
                os.remove(entry.path)
                n += 1
            except OSError:
                pass
    return n


def clear_cache_next_start() -> Optional[str]:
    """The cache folder in use is cleared when QGIS starts next (its files may be open now); the folder."""
    folder = cache_folder()
    if folder:
        settings.set_overview_cache_to_clear(folder)
    return folder


@dataclass(frozen=True)
class Outcome:
    kind: str
    path: str
    #: The levels built (BUILT).
    factors: tuple[int, ...] = ()
    #: The .ovr the plugin put in the cache folder (location "cache", or a read-only folder's built by gdaladdo).
    ovr: str = ""
    #: `ovr` is named through GDAL itself (attach_native: a read-only folder), not through PamAllocateProxy.
    native: bool = False


def local_path(layer: QgsMapLayer) -> Optional[str]:
    """The local file a GDAL raster layer reads; None for a URL, a /vsi… path or another provider."""
    if not isinstance(layer, QgsRasterLayer) or layer.providerType() != "gdal":
        return None
    parts = QgsProviderRegistry.instance().decodeUri("gdal", layer.source())
    path = parts.get("path") or ""
    if not path or path.startswith("/vsi") or "://" in path or parts.get("layerName") or parts.get("layerId") is not None:
        return None
    return path if os.path.isfile(path) else None


def has_overviews(ds) -> bool:
    """Whether a dataset already has overviews (internal, an .ovr, or a format's own levels such as JPEG 2000's)."""
    return ds.RasterCount > 0 and ds.GetRasterBand(1).GetOverviewCount() > 0


def writable_folder(folder: str) -> bool:
    """Whether a file can be made in a folder (tried: os.access is not to be trusted on Windows shares)."""
    try:
        fd, probe = tempfile.mkstemp(prefix=".satobs-", suffix=".tmp", dir=folder)
    except OSError:
        return False
    os.close(fd)
    try:
        os.remove(probe)
    except OSError:
        pass
    return True


def _config(ds, options: OverviewOptions) -> dict[str, str]:
    """GDAL's options for the .ovr of a dataset."""
    band = ds.GetRasterBand(1)
    dtype = band.DataType
    compress = options.compress
    if compress == "JPEG" and (dtype != gdal.GDT_Byte or ds.RasterCount not in (1, 3, 4)):
        compress = "DEFLATE"
    # Half the cores by default (`threads`): with all of them QGIS's drawing of the map waited for the build.
    threads = build_threads(options, os.cpu_count())
    config = {"COMPRESS_OVERVIEW": compress, "BIGTIFF_OVERVIEW": "IF_SAFER", "GDAL_NUM_THREADS": str(threads)}
    if compress in ("DEFLATE", "LZW", "ZSTD"):
        if gdal.GetDataTypeName(dtype).startswith("Float"):
            config["PREDICTOR_OVERVIEW"] = "3"
        elif not gdal.GetDataTypeName(dtype).startswith("C"):
            config["PREDICTOR_OVERVIEW"] = "2"
    # YCbCr only for exactly three bands (RGB): GDAL refuses it for four (RGBN, RGBA), which stay plain JPEG (#80).
    if compress == "JPEG" and ds.RasterCount == 3:
        config["PHOTOMETRIC_OVERVIEW"] = "YCBCR"
        config["INTERLEAVE_OVERVIEW"] = "PIXEL"
    return config


def build_external(path: str, options: OverviewOptions, progress: Callable[[float], None] = lambda p: None,
                   cancelled: Callable[[], bool] = lambda: False) -> Outcome:
    """
    Builds the .ovr of an image without overviews that is not small: beside
    it (`path + ".ovr"`) when its folder can be written and `location` is
    "auto", else in the cache folder. The image is only read. Raises
    Cancelled when `cancelled()` turns true, RuntimeError when GDAL fails.
    """
    target = path + ".ovr"
    src = gdal.Open(path, gdal.GA_ReadOnly)
    if src is None:
        raise RuntimeError(gdal.GetLastErrorMsg() or f"{path} を開けず")
    if has_overviews(src) or os.path.exists(target):
        return Outcome(HAS_OVERVIEWS, path)
    if max(src.RasterXSize, src.RasterYSize) <= options.min_size:
        return Outcome(SMALL, path)
    factors = overview_factors(src.RasterXSize, src.RasterYSize)
    if not factors:
        return Outcome(SMALL, path)
    config = _config(src, options)
    if options.location == "cache":
        if not cache_folder():
            return Outcome(NO_CACHE, path)
        if pam_proxy() is None:
            return Outcome(UNSUPPORTED, path)
        return _build_into_cache(src, path, options, factors, config, progress, cancelled)
    folder = os.path.dirname(os.path.abspath(path))
    if not writable_folder(folder):
        if not cache_folder():
            return Outcome(READ_ONLY, path)
        if gdaladdo() is not None:
            # Built in gdaladdo's own process, then named in the .aux.xml GDAL keeps in the cache folder.
            attached = _build_attached(src, path, options, factors, config, progress, cancelled)
            if attached is not None:
                return attached
        return _build_cached(path, options, factors, config, progress, cancelled)
    part = os.path.join(folder, f".{os.path.basename(target)}.{uuid.uuid4().hex[:8]}.part")
    work = tempfile.mkdtemp(prefix="satobs-ovr-")
    try:
        built = _build_in_temp(src, work, options, factors, config, progress, cancelled)
        if os.path.exists(target):
            # Built meanwhile by someone else: theirs is kept.
            return Outcome(HAS_OVERVIEWS, path)
        # Copied beside the image under another name first, so a half-written .ovr is never read.
        shutil.copyfile(built, part)
        progress(99.0)
        os.replace(part, target)
        return Outcome(BUILT, path, tuple(factors))
    finally:
        shutil.rmtree(work, ignore_errors=True)
        if os.path.exists(part):
            try:
                os.remove(part)
            except OSError:
                pass


def _build_in_temp(src, work: str, options: OverviewOptions, factors: list[int], config: dict[str, str],
                   progress: Callable[[float], None], cancelled: Callable[[], bool]) -> str:
    """The .ovr of an image built for a VRT of it in the folder `work` (the image is only read); its path."""
    vrt = os.path.join(work, "image.vrt")
    if gdal.Translate(vrt, src, format="VRT") is None:
        raise RuntimeError(gdal.GetLastErrorMsg() or "VRT を作れず")
    exe = gdaladdo()
    if exe is not None and run_gdaladdo(exe, vrt, options, factors, config, progress, cancelled):
        return vrt + ".ovr"
    ds = gdal.Open(vrt, gdal.GA_ReadOnly)
    if ds is None:
        raise RuntimeError(gdal.GetLastErrorMsg() or "VRT を開けず")

    def callback(complete, message, data):
        progress(complete * 95.0)
        return 0 if cancelled() else 1

    try:
        error = _with_config(config, lambda: ds.BuildOverviews(options.resampling, factors, callback))
    except RuntimeError:
        # Raised when GDAL's exceptions are on.
        if cancelled():
            raise Cancelled() from None
        raise
    ds = None
    if cancelled():
        raise Cancelled()
    built = vrt + ".ovr"
    if error != 0 or not os.path.exists(built):
        raise RuntimeError(gdal.GetLastErrorMsg() or "ピラミッドを作れず")
    return built


#: gdaladdo's niceness below QGIS (Linux, macOS); on Windows its process is BELOW_NORMAL_PRIORITY_CLASS.
NICE = 10
_addo: Optional[str] = None
_addo_tried = False
_addo_lock = threading.Lock()


def gdaladdo() -> Optional[str]:
    """
    GDAL's gdaladdo that comes with this QGIS (beside its programs, else on
    PATH), of the same GDAL major version as QGIS's; None when there is none
    (the levels are then made in QGIS). Looked for once.
    """
    global _addo, _addo_tried
    with _addo_lock:
        if _addo_tried:
            return _addo
        _addo_tried = True
        name = "gdaladdo.exe" if sys.platform == "win32" else "gdaladdo"
        folders = [os.path.dirname(sys.executable)]
        prefix = QgsApplication.prefixPath()
        if prefix:
            # Windows: <QGIS>/apps/qgis(-ltr) → <QGIS>/bin; macOS: QGIS.app/Contents/MacOS/bin; Linux: /usr/bin.
            folders += [os.path.join(prefix, "bin"), os.path.join(prefix, "..", "..", "bin")]
        folders.append(os.path.join(sys.prefix, "..", "..", "bin"))
        candidates = [os.path.normpath(os.path.join(f, name)) for f in folders if f] + [shutil.which("gdaladdo")]
        wanted = gdal.__version__.split(".")[0]
        for exe in dict.fromkeys(c for c in candidates if c):
            if not os.path.isfile(exe):
                continue
            version = _version_of(exe)
            if version and version.split(".")[0] == wanted:
                _addo = exe
                QgsMessageLog.logMessage(f"ピラミッドは別のプロセスで作成（{exe}、GDAL {version}）", LOG_TAG, Qgis.MessageLevel.Info)
                return exe
        QgsMessageLog.logMessage("gdaladdo が見つからず: ピラミッドは QGIS の中で作成", LOG_TAG, Qgis.MessageLevel.Info)
        return None


def _hidden() -> int:
    """The process flags of a GDAL program run by the plugin: no console window and a lower priority (Windows)."""
    if sys.platform != "win32":
        return 0
    return getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000) | getattr(subprocess, "BELOW_NORMAL_PRIORITY_CLASS", 0x00004000)


def _version_of(exe: str) -> Optional[str]:
    try:
        done = subprocess.run([exe, "--version"], stdin=subprocess.DEVNULL, capture_output=True, timeout=20,
                              creationflags=_hidden())
    except (OSError, subprocess.SubprocessError):
        return None
    found = re.search(rb"GDAL (\d+\.\d+(?:\.\d+)?)", done.stdout or b"")
    return found.group(1).decode("ascii") if done.returncode == 0 and found else None


def _read_progress(stream, state: dict) -> None:
    """gdaladdo's progress on its output ("0...10...20…100 - done."): a number every 10 %, a dot every 2.5 %."""
    last, dots, number = 0, 0, b""
    while True:
        c = stream.read(1)
        if not c:
            return
        if c.isdigit():
            number += c
            last, dots = int(number), 0
        else:
            number = b""
            if c == b".":
                dots += 1
        state["percent"] = min(100.0, last + 2.5 * dots)


def run_gdaladdo(exe: str, vrt: str, options: OverviewOptions, factors: Sequence[int], config: dict[str, str],
                 progress: Callable[[float], None] = lambda p: None, cancelled: Callable[[], bool] = lambda: False) -> bool:
    """
    The levels of `vrt` (its `.ovr` beside it) made by gdaladdo in a process
    of its own at a lower priority. It runs in the VRT's folder and is given
    names without a folder, so no path with Japanese or other non-ASCII
    letters goes through the command line (the VRT names the image in
    UTF-8). Raises Cancelled when `cancelled()` turns true (the process is
    stopped); False when gdaladdo failed (logged: the levels are then made
    in QGIS).
    """
    folder, name = os.path.split(vrt)
    args = [exe]
    for key, value in config.items():
        args += ["--config", key, value]
    args += ["-r", options.resampling.lower(), name] + [str(f) for f in factors]
    # GDAL's own cache folder is not given: gdaladdo writes only beside the VRT, and GDAL's index there stays QGIS's.
    env = {k: v for k, v in os.environ.items() if k != PROXY_DIR}
    errors_path = os.path.join(folder, "gdaladdo.log")
    state: dict = {"percent": 0.0}
    with open(errors_path, "wb") as errors:
        try:
            process = subprocess.Popen(args, cwd=folder, env=env, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                       stderr=errors, creationflags=_hidden())
        except OSError as error:
            QgsMessageLog.logMessage(f"gdaladdo を起動できず（{error}）: QGIS の中で作成", LOG_TAG, Qgis.MessageLevel.Info)
            return False
        if sys.platform != "win32":
            try:
                os.setpriority(os.PRIO_PROCESS, process.pid, NICE)
            except (OSError, AttributeError):
                pass
        reader = threading.Thread(target=_read_progress, args=(process.stdout, state), name="satobs-gdaladdo", daemon=True)
        reader.start()
        shown = -1.0
        try:
            while True:
                try:
                    process.wait(timeout=0.1)
                    break
                except subprocess.TimeoutExpired:
                    pass
                if cancelled():
                    raise Cancelled()
                if state["percent"] != shown:
                    shown = state["percent"]
                    progress(shown * 0.95)
        finally:
            if process.poll() is None:
                process.kill()
                process.wait()
            reader.join(2)
            process.stdout.close()
    if cancelled():
        raise Cancelled()
    built = vrt + ".ovr"
    if process.returncode == 0 and os.path.exists(built):
        try:
            ds = gdal.Open(vrt, gdal.GA_ReadOnly)
            ok = ds is not None and has_overviews(ds)
            ds = None
        except RuntimeError:
            ok = False
        if ok:
            return True
    try:
        with open(errors_path, "rb") as f:
            lines = [ln.strip() for ln in f.read().decode("utf-8", "replace").splitlines() if ln.strip()]
    except OSError:
        lines = []
    why = lines[-1] if lines else f"終了コード {process.returncode}"
    QgsMessageLog.logMessage(f"gdaladdo でピラミッドを作れず（{why}）: QGIS の中で作り直し", LOG_TAG, Qgis.MessageLevel.Info)
    for leftover in (built, vrt + ".aux.xml"):
        try:
            os.remove(leftover)
        except OSError:
            pass
    return False


def _build_attached(src, path: str, options: OverviewOptions, factors: list[int], config: dict[str, str],
                    progress: Callable[[float], None], cancelled: Callable[[], bool]) -> Optional[Outcome]:
    """
    The .ovr of an image in a read-only folder, built aside (gdaladdo), moved
    to the cache folder and named in the image's .aux.xml there, which GDAL
    itself writes there when it cannot beside the image (GDAL_PAM_PROXY_DIR).
    None when GDAL does not then find it (the levels are then made the old
    way, by GDAL writing them to the cache folder).
    """
    target = os.path.join(cache_folder(), cached_ovr_name(path))
    part = target + f".{uuid.uuid4().hex[:8]}.part"
    work = tempfile.mkdtemp(prefix="satobs-ovr-")
    try:
        built = _build_in_temp(src, work, options, factors, config, progress, cancelled)
        shutil.copyfile(built, part)
        os.replace(part, target)
    finally:
        shutil.rmtree(work, ignore_errors=True)
        if os.path.exists(part):
            try:
                os.remove(part)
            except OSError:
                pass
    progress(99.0)
    try:
        attach_native(path, target)
        again = gdal.Open(path, gdal.GA_ReadOnly)
        found = again is not None and has_overviews(again)
    except RuntimeError:
        found = False
    if found:
        return Outcome(CACHED, path, tuple(factors), target, native=True)
    QgsMessageLog.logMessage(f"{os.path.basename(path)}: キャッシュの .ovr を GDAL が見つけず、GDAL 自身に作り直させる", LOG_TAG, Qgis.MessageLevel.Info)
    try:
        os.remove(target)
    except OSError:
        pass
    return None


def attach_native(path: str, ovr: str) -> None:
    """
    Names `ovr` as the overviews of an image in a read-only folder
    (OVERVIEW_FILE): GDAL writes it in the image's .aux.xml, which it puts
    in the cache folder when it cannot beside the image.
    """
    ds = gdal.Open(path, gdal.GA_ReadOnly)
    if ds is None:
        raise RuntimeError(gdal.GetLastErrorMsg() or f"{path} を開けず")
    ds.SetMetadataItem("OVERVIEW_FILE", ovr, "OVERVIEWS")
    ds = None


def cached_ovr_name(path: str) -> str:
    """The name of an image's .ovr in the cache folder (location "cache"): a hash of its path and its file name."""
    digest = hashlib.sha1(os.path.normcase(os.path.abspath(path)).encode("utf-8")).hexdigest()[:12]
    return f"{digest}_{os.path.basename(path)}.ovr"


def _build_into_cache(src, path: str, options: OverviewOptions, factors: list[int], config: dict[str, str],
                      progress: Callable[[float], None], cancelled: Callable[[], bool]) -> Outcome:
    """
    The .ovr of an image (whatever its folder) in the cache folder: built in
    a temporary folder, moved there, and named in the image's .aux.xml kept
    in the cache folder (GDAL's proxy, OVERVIEW_FILE), which GDAL reads
    instead of one beside the image. What that one says is copied into it.
    """
    cache = cache_folder()
    target = os.path.join(cache, cached_ovr_name(path))
    part = target + f".{uuid.uuid4().hex[:8]}.part"
    work = tempfile.mkdtemp(prefix="satobs-ovr-")
    try:
        built = _build_in_temp(src, work, options, factors, config, progress, cancelled)
        src = None
        shutil.copyfile(built, part)
        os.replace(part, target)
    finally:
        shutil.rmtree(work, ignore_errors=True)
        if os.path.exists(part):
            try:
                os.remove(part)
            except OSError:
                pass
    progress(99.0)
    point_to_overviews(path, target)
    again = gdal.Open(path, gdal.GA_ReadOnly)
    if again is None or not has_overviews(again):
        raise RuntimeError("キャッシュのピラミッドを GDAL が読めず")
    return Outcome(CACHED, path, tuple(factors), target)


def _spellings(path: str) -> list[str]:
    # GDAL finds a proxy by the file name exactly as the image is opened.
    return list(dict.fromkeys([path, os.path.normpath(path), path.replace("\\", "/")]))


def _read_pam(path: str):
    root = None
    if os.path.exists(path):
        try:
            root = ElementTree.parse(path).getroot()
        except (ElementTree.ParseError, OSError):
            root = None
    return root if root is not None and root.tag == "PAMDataset" else ElementTree.Element("PAMDataset")


def proxy_pams(path: str, made: Optional[list] = None) -> list[str]:
    """
    The image's .aux.xml in the cache folder (one per spelling of its path),
    registered through GDAL (PamAllocateProxy) with what the one beside the
    image says when it has none there yet. From then on GDAL reads and
    writes these instead of the one beside the image.
    """
    alloc, get = pam_proxy()
    out = []
    for key in _spellings(path):
        raw = key.encode("utf-8")
        proxied = get(raw)
        if proxied:
            aux = proxied.decode("utf-8")
            if not os.path.exists(aux):
                ElementTree.ElementTree(_read_pam(key + ".aux.xml")).write(aux, encoding="utf-8", xml_declaration=False)
        else:
            proxied = alloc(raw)
            if not proxied:
                raise RuntimeError("GDAL のキャッシュフォルダ（GDAL_PAM_PROXY_DIR）に登録できず")
            aux = proxied.decode("utf-8")
            ElementTree.ElementTree(_read_pam(key + ".aux.xml")).write(aux, encoding="utf-8", xml_declaration=False)
            if made is not None:
                made.append(aux)
        out.append(aux)
    return out


def point_to_overviews(path: str, ovr: str) -> None:
    """Names `ovr` as the overviews of `path` in its .aux.xml in the cache folder (OVERVIEW_FILE)."""
    for aux in proxy_pams(path):
        root = _read_pam(aux)
        for m in [m for m in root.findall("Metadata") if m.get("domain") == "OVERVIEWS"]:
            root.remove(m)
        meta = ElementTree.SubElement(root, "Metadata", domain="OVERVIEWS")
        ElementTree.SubElement(meta, "MDI", key="OVERVIEW_FILE").text = ovr
        ElementTree.ElementTree(root).write(aux, encoding="utf-8", xml_declaration=False)


#: The settings file's `overviews` now (set by the plugin when it is read).
current = OverviewOptions()


def before_open(path: str) -> bool:
    """
    With `location` "cache", before an image of the catalog is opened: its
    .aux.xml moves to the cache folder (proxy_pams), so what QGIS writes
    there (statistics, histograms) never goes beside the image either.
    Whether it was moved now (not before).
    """
    options = current
    if not options.enabled or options.location != "cache" or not path or path.startswith("/vsi") or "://" in path:
        return False
    if not cache_folder() or not os.path.isfile(path) or pam_proxy() is None:
        return False
    made: list[str] = []
    try:
        proxy_pams(path, made)
    except (RuntimeError, OSError) as error:
        QgsMessageLog.logMessage(f"{path} の .aux.xml をキャッシュに置けず（{error}）", LOG_TAG, Qgis.MessageLevel.Info)
    return bool(made)


def layers_added(layers: Sequence[QgsMapLayer], message_bar=None) -> list[str]:
    """
    With `scope` "all": the local GDAL images added to the project in any
    way (Add Layer, drag and drop, a project read) get overviews like the
    catalog's. With `location` "cache" the image's .aux.xml moves to the
    cache folder too, but the layer already open keeps the one beside the
    image (QGIS reuses its GDAL handle), so QGIS may still write its
    statistics there until the image is opened again. The paths queued.
    """
    options = current
    if not options.enabled or options.scope != "all":
        return []
    rasters = [layer for layer in layers if local_path(layer)]
    for layer in rasters:
        before_open(local_path(layer))
    return queue.request(rasters, options, message_bar)


_pam: Optional[tuple] = None
_pam_tried = False


def pam_proxy() -> Optional[tuple]:
    """
    GDAL's PamAllocateProxy and PamGetProxy (C++ functions of the GDAL
    library, called through ctypes): None when they cannot be found.
    """
    global _pam, _pam_tried
    if _pam_tried:
        return _pam
    _pam_tried = True
    names = {
        "alloc": ("_Z16PamAllocateProxyPKc", "?PamAllocateProxy@@YAPEBDPEBD@Z", "?PamAllocateProxy@@YAPBDPBD@Z"),
        "get": ("_Z11PamGetProxyPKc", "?PamGetProxy@@YAPEBDPEBD@Z", "?PamGetProxy@@YAPBDPBD@Z"),
    }
    for lib in _gdal_libraries():
        found = {}
        for role, candidates in names.items():
            for name in candidates:
                try:
                    f = getattr(lib, name)
                except AttributeError:
                    continue
                f.restype = ctypes.c_char_p
                f.argtypes = [ctypes.c_char_p]
                found[role] = f
                break
        if len(found) == 2:
            _pam = (found["alloc"], found["get"])
            return _pam
    QgsMessageLog.logMessage("GDAL の PamAllocateProxy が見つからず（overviews.location の cache は使えず）", LOG_TAG, Qgis.MessageLevel.Info)
    return None


def _gdal_libraries():
    """The GDAL library loaded in this process (the one osgeo uses), tried by its likely names."""
    seen = set()

    def load(name):
        if not name or name in seen:
            return None
        seen.add(name)
        try:
            return ctypes.CDLL(name)
        except OSError:
            return None

    candidates = []
    if sys.platform == "win32":
        candidates += _loaded_windows_modules(re.compile(r"^gdal\d*\.dll$", re.IGNORECASE))
        version = gdal.VersionInfo("VERSION_NUM")
        major, minor = int(version[0]), int(version[1:3])
        candidates += [f"gdal{major}{minor:02d}.dll", f"gdal{major}0{minor}.dll", "gdal.dll"]
    else:
        candidates += _loaded_posix_libraries()
        candidates += [ctypes.util.find_library("gdal")]
    for name in candidates:
        lib = load(name)
        if lib is not None:
            yield lib


def _loaded_posix_libraries() -> list[str]:
    try:
        with open("/proc/self/maps", encoding="utf-8", errors="replace") as maps:
            return sorted({line.split()[-1] for line in maps if re.search(r"/libgdal[^/]*\.so", line)})
    except OSError:
        pass
    if sys.platform == "darwin":
        try:
            dyld = ctypes.CDLL(None)
            count = dyld._dyld_image_count()
            dyld._dyld_get_image_name.restype = ctypes.c_char_p
            names = [dyld._dyld_get_image_name(i).decode("utf-8", "replace") for i in range(count)]
            return [n for n in names if re.search(r"/(lib)?gdal[^/]*\.dylib$|/gdal\.framework/", n, re.IGNORECASE)]
        except (OSError, AttributeError):
            return []
    return []


def _loaded_windows_modules(pattern) -> list[str]:
    """The paths of the DLLs loaded in this process whose name matches `pattern`."""
    try:
        from ctypes import wintypes

        psapi = ctypes.WinDLL("psapi")
        kernel = ctypes.WinDLL("kernel32")
        process = kernel.GetCurrentProcess()
        modules = (wintypes.HMODULE * 2048)()
        needed = wintypes.DWORD()
        if not psapi.EnumProcessModules(process, modules, ctypes.sizeof(modules), ctypes.byref(needed)):
            return []
        out = []
        for i in range(min(needed.value // ctypes.sizeof(wintypes.HMODULE), 2048)):
            buffer = ctypes.create_unicode_buffer(1024)
            if kernel.GetModuleFileNameW(modules[i], buffer, 1024) and pattern.match(os.path.basename(buffer.value)):
                out.append(buffer.value)
        return out
    except (OSError, AttributeError):
        return []


def _with_config(config: dict[str, str], run: Callable[[], int]) -> int:
    previous = {k: gdal.GetThreadLocalConfigOption(k, None) for k in config}
    for k, v in config.items():
        gdal.SetThreadLocalConfigOption(k, v)
    try:
        return run()
    finally:
        for k, v in previous.items():
            gdal.SetThreadLocalConfigOption(k, v)


def _build_cached(path: str, options: OverviewOptions, factors: list[int], config: dict[str, str],
                  progress: Callable[[float], None], cancelled: Callable[[], bool]) -> Outcome:
    """
    The .ovr of an image in a read-only folder, built by GDAL itself into the
    cache folder (GDAL_PAM_PROXY_DIR), on a dataset of its own (closed
    before the result is read, so the .ovr is complete).
    """
    src = gdal.Open(path, gdal.GA_ReadOnly)
    if src is None:
        raise RuntimeError(gdal.GetLastErrorMsg() or f"{path} を開けず")
    def callback(complete, message, data):
        progress(complete * 99.0)
        return 0 if cancelled() else 1

    def build() -> int:
        try:
            return src.BuildOverviews(options.resampling, factors, callback)
        except RuntimeError:
            # GDAL first fails to write beside the image (raised when exceptions are on), then writes to the cache.
            return 0

    error = _with_config(config, build)
    message = gdal.GetLastErrorMsg()
    src = None
    if cancelled():
        raise Cancelled()
    again = gdal.Open(path, gdal.GA_ReadOnly)
    if error != 0 or again is None or not has_overviews(again):
        raise RuntimeError(message or "ピラミッドを作れず")
    return Outcome(CACHED, path, tuple(factors))


def reload_layers(path: str, project: Optional[QgsProject] = None) -> int:
    """Reads again the project's layers of an image (so its new .ovr is used); how many."""
    project = project or QgsProject.instance()
    n = 0
    target = os.path.normcase(os.path.abspath(path))
    for layer in project.mapLayers().values():
        p = local_path(layer)
        if p and os.path.normcase(os.path.abspath(p)) == target:
            layer.dataProvider().reloadData()
            layer.triggerRepaint()
            n += 1
    return n


class OverviewQueue:
    """The images waiting for overviews, built one at a time."""

    def __init__(self):
        self._waiting: list[tuple[str, OverviewOptions]] = []
        self._task: Optional[FunctionTask] = None
        self._current: Optional[str] = None
        self.message_bar = None
        #: Called with each Outcome (tests).
        self.finished: Optional[Callable[[Optional[Outcome], Optional[str]], None]] = None

    def pending(self) -> list[str]:
        return ([self._current] if self._current else []) + [p for p, _ in self._waiting]

    def request(self, layers: Sequence[QgsMapLayer], options: OverviewOptions, message_bar=None) -> list[str]:
        """Queues the local images of `layers` (unless `options` turns building off); the paths queued."""
        if not options.enabled:
            return []
        if message_bar is not None:
            self.message_bar = message_bar
        known = {os.path.normcase(os.path.abspath(p)) for p in self.pending()}
        queued = []
        for layer in layers:
            path = local_path(layer)
            if not path:
                continue
            key = os.path.normcase(os.path.abspath(path))
            if key in known:
                continue
            known.add(key)
            self._waiting.append((path, options))
            queued.append(path)
        self._next()
        return queued

    def cancel_all(self) -> None:
        """Stops all (the plugin is unloaded): the running task's answer is dropped with the others' (#88)."""
        self._waiting.clear()
        if self._task is not None:
            self._task.cancel()
        self._task, self._current = None, None

    def _next(self) -> None:
        if self._task is not None or not self._waiting:
            return
        path, options = self._waiting.pop(0)
        self._current = path
        name = os.path.basename(path)

        def work(task: FunctionTask) -> Outcome:
            return build_external(path, options, task.progress, task.isCanceled)

        self._task = run_task(f"ピラミッドの作成（{name}）", work,
                              lambda outcome: self._ended(outcome, None),
                              lambda error: self._ended(None, error))

    def _ended(self, outcome: Optional[Outcome], error: Optional[str]) -> None:
        path, self._current, self._task = self._current, None, None
        name = os.path.basename(path or "")
        bar = self.message_bar
        try:
            if outcome is not None and outcome.kind == BUILT:
                reload_layers(outcome.path)
                if bar is not None:
                    bar.pushMessage(TITLE, f"{name}.ovr を作成（画像の隣。画像本体はそのまま）", Qgis.MessageLevel.Info, 5)
            elif outcome is not None and outcome.kind == CACHED:
                reload_layers(outcome.path)
                if outcome.ovr:
                    # The layers opened before kept the .aux.xml as it was, and wrote it back
                    # (with their statistics) when read again: the .ovr is named in it once more.
                    try:
                        (attach_native if outcome.native else point_to_overviews)(outcome.path, outcome.ovr)
                    except (RuntimeError, OSError) as e:
                        QgsMessageLog.logMessage(f"{name}: {e}", LOG_TAG, Qgis.MessageLevel.Warning)
                    reload_layers(outcome.path)
                if bar is not None:
                    bar.pushMessage(TITLE, f"{name} のピラミッドをキャッシュに作成（{cache_folder() or ''}）", Qgis.MessageLevel.Info, 5)
            elif outcome is not None and outcome.kind in (NO_CACHE, UNSUPPORTED):
                if bar is not None:
                    why = "キャッシュフォルダなし" if outcome.kind == NO_CACHE else "この GDAL ではキャッシュに置けず"
                    bar.pushMessage(TITLE, f"{name} のピラミッドは未作成（{why}）", Qgis.MessageLevel.Info, 8)
            elif outcome is not None and outcome.kind == READ_ONLY:
                if bar is not None:
                    bar.pushMessage(TITLE, f"{name} のピラミッドは未作成（フォルダに書き込めず、キャッシュフォルダもなし）", Qgis.MessageLevel.Info, 8)
            elif error is not None and error != "取消" and bar is not None:
                bar.pushMessage(TITLE, f"{name} のピラミッドを作成できず（{error}）", Qgis.MessageLevel.Warning, 10)
            if self.finished is not None:
                self.finished(outcome, error)
        finally:
            self._next()


#: The plugin's queue (shared by the docks that open catalog images).
queue = OverviewQueue()
