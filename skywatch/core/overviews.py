"""
The settings file's `overviews`: whether an image opened from the image
catalog without overviews (pyramids) gets them built in the background, as
an external `.ovr` beside it (the image itself is never written). When the
image's folder cannot be written, the `.ovr` goes to a cache folder on this
PC (GDAL's GDAL_PAM_PROXY_DIR, the profile's by default, `cacheFolder` to
choose another), where GDAL finds it again by itself. `"location": "cache"`
puts every image's .ovr there, even when its folder can be written.

    "overviews": false                                  // never build
    "overviews": {"resampling": "AVERAGE", "minSize": 2048, "compress": "DEFLATE",
                  "cacheFolder": "D:/ovr-cache",         // "" (default): the QGIS profile's
                  "location": "cache",                   // "auto" (default): beside the image when it can be written
                  "scope": "catalog"}                    // "all" (default): every local image added to QGIS

Omitted, overviews are built with the defaults below.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

#: GDAL's resampling methods for overviews (BuildOverviews).
RESAMPLINGS = ("NEAREST", "AVERAGE", "BILINEAR", "CUBIC", "CUBICSPLINE", "LANCZOS", "GAUSS", "MODE", "RMS")
#: Compressions of the .ovr (COMPRESS_OVERVIEW).
COMPRESSIONS = ("NONE", "DEFLATE", "LZW", "ZSTD", "JPEG")
#: Where the .ovr goes: beside the image when its folder can be written (else the cache), or always the cache.
LOCATIONS = ("auto", "cache")
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
            problems.append("overviews.location は auto（書き込めれば画像の隣）か cache（常にキャッシュ）で指定")
    scope = DEFAULT_OVERVIEWS.scope
    if "scope" in value:
        text = str(value["scope"]).strip().lower()
        if text in SCOPES:
            scope = text
        else:
            problems.append("overviews.scope は all（QGIS に追加した画像すべて）か catalog（画像カタログから開いた画像だけ）で指定")
    return OverviewOptions(enabled, resampling, min_size, compress, cache_folder, location, scope)


def overview_factors(width: int, height: int, smallest: int = SMALLEST) -> list[int]:
    """The levels to build (2, 4, 8…) until the longer side is at most `smallest` pixels; none for a small image."""
    factors: list[int] = []
    longest = max(width, height)
    factor = 2
    while longest / (factor // 2) > smallest:
        factors.append(factor)
        factor *= 2
    return factors
