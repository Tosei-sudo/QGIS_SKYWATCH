"""
The settings file's `overviews`: whether an image opened from the image
catalog without overviews (pyramids) gets them built in the background, as
an external `.ovr` beside it (the image itself is never written). When the
image's folder cannot be written, the `.ovr` goes to a cache folder on this
PC (GDAL's GDAL_PAM_PROXY_DIR, the profile's by default, `cacheFolder` to
choose another), where GDAL finds it again by itself. `"location": "cache"`
puts every image's .ovr there, even when its folder can be written.
`"location": "temp"` puts every image's .ovr in a temporary folder on this
PC instead (`tempFolder`, the system's temporary folder by default), deleted
when QGIS is closed (tsudo, 2026-11-22). Both folders may name environment
variables as `%TEMP%`, `%LOCALAPPDATA%` (expand_folder).

    "overviews": false                                  // never build
    "overviews": {"resampling": "AVERAGE", "minSize": 2048, "compress": "DEFLATE",
                  "cacheFolder": "D:/ovr-cache",         // "" (default): the QGIS profile's
                  "location": "cache",                   // "auto" (default): beside the image when it can be written; "temp"
                  "tempFolder": "%TEMP%/MIKAGE",         // "" (default): the system's temporary folder (location "temp")
                  "scope": "catalog",                    // "all" (default): every local image added to QGIS
                  "threads": 2}                          // 0 (default): half the PC's cores

Omitted, overviews are built with the defaults below. The levels are made
by GDAL's own gdaladdo in a process of its own, at a lower priority and on
`threads` cores, so QGIS keeps drawing the map smoothly meanwhile
(qgis_io/overviews_io.py).
"""
from __future__ import annotations

import os
import re
import tempfile
from dataclasses import dataclass
from typing import Any

#: GDAL's resampling methods for overviews (BuildOverviews).
RESAMPLINGS = ("NEAREST", "AVERAGE", "BILINEAR", "CUBIC", "CUBICSPLINE", "LANCZOS", "GAUSS", "MODE", "RMS")
#: Compressions of the .ovr (COMPRESS_OVERVIEW).
COMPRESSIONS = ("NONE", "DEFLATE", "LZW", "ZSTD", "JPEG")
#: Where the .ovr goes: beside the image when its folder can be written (else the cache), always the cache, or a
#: temporary folder deleted when QGIS is closed.
LOCATIONS = ("auto", "cache", "temp")
#: Which images: every local image added to QGIS, or only those opened from the image catalog.
SCOPES = ("all", "catalog")
#: The smallest overview: levels are added (2, 4, 8…) until the image's longer side fits in this.
SMALLEST = 256


@dataclass(frozen=True)
class OverviewOptions:
    #: Whether overviews are built at all.
    enabled: bool = True
    resampling: str = "AVERAGE"
    #: Images whose longer side is at most this many pixels are left as they are (QGIS reads them fast enough).
    min_size: int = 2048
    compress: str = "DEFLATE"
    #: Where the .ovr of an image in a read-only folder goes ("" for the QGIS profile's folder).
    cache_folder: str = ""
    #: "auto" or "cache" (LOCATIONS).
    location: str = "auto"
    #: "all" or "catalog" (SCOPES).
    scope: str = "all"
    #: Cores GDAL builds on (0: half the PC's, so the others stay for QGIS drawing the map).
    threads: int = 0
    #: Where the .ovr go with `location` "temp" ("" for the system's temporary folder).
    temp_folder: str = ""


DEFAULT_OVERVIEWS = OverviewOptions()


def overviews_of(value: Any, problems: list[str]) -> OverviewOptions:
    """The `overviews` entry (true / false / an object); the defaults with the reason in `problems` for what cannot be read."""
    if isinstance(value, bool):
        return OverviewOptions(enabled=value)
    if not isinstance(value, dict):
        problems.append("overviews は true・false かオブジェクトで指定")
        return DEFAULT_OVERVIEWS
    enabled = DEFAULT_OVERVIEWS.enabled
    if "enabled" in value:
        if isinstance(value["enabled"], bool):
            enabled = value["enabled"]
        else:
            problems.append("overviews.enabled は true か false で指定")
    resampling = DEFAULT_OVERVIEWS.resampling
    if "resampling" in value:
        text = str(value["resampling"]).strip().upper()
        if text in RESAMPLINGS:
            resampling = text
        else:
            problems.append(f"overviews.resampling は {'・'.join(RESAMPLINGS)} のどれかで指定")
    min_size = DEFAULT_OVERVIEWS.min_size
    if "minSize" in value:
        v = value["minSize"]
        if isinstance(v, (int, float)) and not isinstance(v, bool) and v >= 0:
            min_size = int(v)
        else:
            problems.append("overviews.minSize は 0 以上の数（ピクセル）で指定")
    compress = DEFAULT_OVERVIEWS.compress
    if "compress" in value:
        text = str(value["compress"]).strip().upper()
        if text in COMPRESSIONS:
            compress = text
        else:
            problems.append(f"overviews.compress は {'・'.join(COMPRESSIONS)} のどれかで指定")
    cache_folder = DEFAULT_OVERVIEWS.cache_folder
    if "cacheFolder" in value:
        if isinstance(value["cacheFolder"], str):
            cache_folder = value["cacheFolder"].strip()
        else:
            problems.append("overviews.cacheFolder はフォルダのパス（文字列）で指定")
    location = DEFAULT_OVERVIEWS.location
    if "location" in value:
        text = str(value["location"]).strip().lower()
        if text in LOCATIONS:
            location = text
        else:
            problems.append("overviews.location は auto（書き込めれば画像の隣）・cache（常にキャッシュ）・temp（一時フォルダ。QGIS を閉じたら消す）のどれかで指定")
    scope = DEFAULT_OVERVIEWS.scope
    if "scope" in value:
        text = str(value["scope"]).strip().lower()
        if text in SCOPES:
            scope = text
        else:
            problems.append("overviews.scope は all（QGIS に追加した画像すべて）か catalog（画像カタログから開いた画像だけ）で指定")
    threads = DEFAULT_OVERVIEWS.threads
    if "threads" in value:
        v = value["threads"]
        if isinstance(v, (int, float)) and not isinstance(v, bool) and v >= 0 and v == int(v):
            threads = int(v)
        else:
            problems.append("overviews.threads は 0 以上の整数（0 で PC のコアの半分）で指定")
    temp_folder = DEFAULT_OVERVIEWS.temp_folder
    if "tempFolder" in value:
        if isinstance(value["tempFolder"], str):
            temp_folder = value["tempFolder"].strip()
        else:
            problems.append("overviews.tempFolder はフォルダのパス（文字列）で指定")
    return OverviewOptions(enabled, resampling, min_size, compress, cache_folder, location, scope, threads, temp_folder)


#: The folder under the system's temporary folder where `location` "temp" puts the .ovr.
TEMP_SUBFOLDER = os.path.join("MIKAGE", "overviews")


def expand_folder(text: str, environ=None) -> str:
    """
    A folder of the settings file with its environment variables filled in:
    `%NAME%` (as Windows writes them, on every system; `%TEMP%` and `%TMP%`
    are the system's temporary folder where they are not set) and a leading
    `~`. A name that is not set stays as written.
    """
    environ = os.environ if environ is None else environ
    text = (text or "").strip()
    if not text:
        return ""

    def value(match: "re.Match[str]") -> str:
        name = match.group(1)
        found = environ.get(name)
        if found is None:
            found = next((v for k, v in environ.items() if k.upper() == name.upper()), None)
        if found is None and name.upper() in ("TEMP", "TMP"):
            found = tempfile.gettempdir()
        return found if found is not None else match.group(0)

    text = re.sub(r"%([A-Za-z_][A-Za-z0-9_()]*)%", value, text)
    return os.path.expanduser(text)


def temp_root(options: OverviewOptions) -> str:
    """Where `location` "temp" puts the .ovr: `tempFolder` (its variables filled in), else MIKAGE's in the system's."""
    return expand_folder(options.temp_folder) or os.path.join(tempfile.gettempdir(), TEMP_SUBFOLDER)


def build_threads(options: OverviewOptions, cores: int | None = None) -> int:
    """The cores one build uses: `threads`, or half the PC's (at least one), never more than it has."""
    cores = max(1, cores or 1)
    return max(1, min(cores, options.threads)) if options.threads else max(1, cores // 2)


def overview_factors(width: int, height: int, smallest: int = SMALLEST) -> list[int]:
    """The levels to build (2, 4, 8…) until the longer side is at most `smallest` pixels; none for a small image."""
    factors: list[int] = []
    longest = max(width, height)
    factor = 2
    while longest / (factor // 2) > smallest:
        factors.append(factor)
        factor *= 2
    return factors
