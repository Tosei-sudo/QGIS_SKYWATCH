"""
The base maps (背景地図) of the settings file, as the viewer reads them
(config.ts `baseMapOf`, `baseMapType`): `baseMaps` replaces the built-in
list when written, `defaultBaseMap` is the one shown at start ("" for none).
Each base map is one QGIS layer: raster XYZ tiles, vector tiles by
`{z}/{x}/{y}.pbf`, a Mapbox / MapLibre style JSON, or an ArcGIS
VectorTileServer.
"""
from __future__ import annotations

import posixpath
import re
from dataclasses import dataclass
from html.parser import HTMLParser
from typing import Any, Optional
from urllib.parse import quote, unquote, urlencode, urljoin

TYPES = ("xyz", "mvt", "style", "esri")
_URL = re.compile(r"^(https?:)?//|^\.{0,2}/")


@dataclass(frozen=True)
class BaseMapConfig:
    id: str
    #: The name in the switch.
    label: str
    #: xyz, mvt, style or esri.
    type: str
    url: str
    #: The credit (HTML in the viewer; QGIS shows it as text).
    attributions: str = ""
    #: The deepest zoom with tiles (xyz and mvt); deeper views enlarge those.
    max_zoom: int = 18
    #: esri: another style for the server.
    style: str = ""
    #: esri: the settings file's token (QGIS uses the service's authentication configuration instead).
    token: str = ""


_GSI = '<a href="https://maps.gsi.go.jp/development/ichiran.html" target="_blank" rel="noopener">地理院タイル</a>'
_OSM = '© <a href="https://www.openstreetmap.org/copyright" target="_blank" rel="noopener">OpenStreetMap</a> contributors'

#: The viewer's built-in list (config.ts defaultConfig.baseMaps): used when the settings file has no `baseMaps`.
DEFAULT_BASE_MAPS = (
    BaseMapConfig("gsi-std", "地理院 標準", "xyz", "https://cyberjapandata.gsi.go.jp/xyz/std/{z}/{x}/{y}.png", _GSI, 18),
    BaseMapConfig("gsi-pale", "地理院 淡色", "xyz", "https://cyberjapandata.gsi.go.jp/xyz/pale/{z}/{x}/{y}.png", _GSI, 18),
    BaseMapConfig("gsi-photo", "地理院 写真", "xyz", "https://cyberjapandata.gsi.go.jp/xyz/seamlessphoto/{z}/{x}/{y}.jpg", _GSI, 18),
    BaseMapConfig("gsi-vector", "地理院 ベクトル", "style", "https://gsi-cyberjapan.github.io/optimal_bvmap/style/std.json", _GSI, 16),
    BaseMapConfig("osm", "OpenStreetMap", "xyz", "https://tile.openstreetmap.org/{z}/{x}/{y}.png", _OSM, 19),
    BaseMapConfig("esri-osm", "OpenStreetMap（Esri ベクトル）", "esri",
                  "https://basemaps.arcgis.com/arcgis/rest/services/OpenStreetMap_v2/VectorTileServer", f"Esri, {_OSM}", 22),
)


def base_map_type(url: str) -> str:
    """The kind of base map a URL points to: a VectorTileServer, tiles by {z}/{x}/{y} (vector for .pbf / .mvt), else a style JSON."""
    path = re.split(r"[?#]", url, maxsplit=1)[0]
    if re.search(r"/VectorTileServer/?$", path, re.IGNORECASE):
        return "esri"
    if "{z}" in path:
        return "mvt" if re.search(r"\.(pbf|mvt)$", path, re.IGNORECASE) else "xyz"
    return "style"


def base_map_of(value: Any, problems: list[str], index: int) -> Optional[BaseMapConfig]:
    """One entry of `baseMaps` (the viewer's messages)."""
    if not isinstance(value, dict):
        problems.append(f"baseMaps[{index}] はオブジェクトで指定")
        return None
    id_, label, kind, url = value.get("id"), value.get("label"), value.get("type"), value.get("url")
    if not isinstance(id_, str) or not id_ or not isinstance(url, str) or not _URL.search(url):
        problems.append(f"baseMaps[{index}] に id か url（http(s) か相対パス）なし")
        return None
    if kind is not None and kind not in TYPES:
        problems.append(f"baseMaps[{index}] の type「{kind}」は xyz・mvt・style・esri のいずれかで指定")
        return None
    kind = kind or base_map_type(url)
    style, token, zoom = value.get("style"), value.get("token"), value.get("maxZoom")
    if style is not None and not (isinstance(style, str) and _URL.search(style)):
        problems.append(f"baseMaps[{index}] の style は http(s) か相対パスの URL で指定")
    good_zoom = isinstance(zoom, int) and not isinstance(zoom, bool) and 0 <= zoom <= 30
    attributions = value.get("attributions")
    return BaseMapConfig(
        id_, label if isinstance(label, str) and label else id_, kind, url,
        attributions if isinstance(attributions, str) else "",
        zoom if good_zoom else (14 if kind == "mvt" else 18),
        style if kind == "esri" and isinstance(style, str) and _URL.search(style) else "",
        token if kind == "esri" and isinstance(token, str) else "",
    )


def base_maps_of(value: Any, problems: list[str]) -> list[BaseMapConfig]:
    """`baseMaps`: the entries that can be read, each id once."""
    if not isinstance(value, list):
        problems.append("baseMaps は配列で指定")
        return list(DEFAULT_BASE_MAPS)
    out: list[BaseMapConfig] = []
    for i, v in enumerate(value):
        b = base_map_of(v, problems, i)
        if b is None:
            continue
        if any(x.id == b.id for x in out):
            problems.append(f"baseMaps の id「{b.id}」が重複")
            continue
        out.append(b)
    return out


def default_base_map_of(value: Any, base_maps: list[BaseMapConfig], problems: list[str]) -> str:
    if value == "" or value is None:
        return ""
    if isinstance(value, str) and any(b.id == value for b in base_maps):
        return value
    problems.append(f"defaultBaseMap「{value}」は baseMaps になし")
    return ""


def resolve_url(url: str, config_source: str) -> str:
    """A URL as it is opened: relative ones against the settings file (a URL or a local path); `//host/…` as https."""
    if url.startswith("//"):
        return "https:" + url
    if re.match(r"^https?://", url, re.IGNORECASE):
        return url
    if re.match(r"^https?://", config_source, re.IGNORECASE):
        return urljoin(config_source, url)
    if not config_source:
        return url
    base = posixpath.dirname(config_source.replace("\\", "/"))
    return "file://" + quote(posixpath.normpath(posixpath.join(base, unquote(url))), safe="/:{}")


class _Text(HTMLParser):
    def __init__(self):
        super().__init__()
        self.parts: list[str] = []

    def handle_data(self, data: str) -> None:
        self.parts.append(data)


def attribution_text(html: str) -> str:
    """The credit without its HTML (links as their text)."""
    parser = _Text()
    parser.feed(html)
    return re.sub(r"\s+", " ", "".join(parser.parts)).strip()


def _xyz_url(template: str) -> str:
    return quote(template, safe=":/?=,{}-&.%~_")


def raster_source(base: BaseMapConfig, url: str, authcfg: Optional[str] = None) -> str:
    """The WMS provider's source for raster XYZ tiles."""
    params = {"type": "xyz", "zmin": "0", "zmax": str(base.max_zoom), **({"authcfg": authcfg} if authcfg else {})}
    return "&".join(f"{k}={v}" for k, v in params.items()) + "&url=" + _xyz_url(url)


def vector_tile_source(base: BaseMapConfig, url: str, authcfg: Optional[str] = None, style_url: str = "",
                       zmin: int = 0, zmax: Optional[int] = None) -> str:
    """A vector tile layer's source: {z}/{x}/{y} tiles (mvt, a style's tiles) or an ArcGIS VectorTileServer (esri)."""
    auth = {"authcfg": authcfg} if authcfg else {}
    if base.type == "esri":
        # QGIS reads an ArcGIS VectorTileServer as xyz tiles of the service type arcgis (type=arcgis alone opens nothing).
        params = {"type": "xyz", "serviceType": "arcgis", "url": url.split("?", 1)[0].rstrip("/"), **auth}
        if style_url:
            params["styleUrl"] = style_url
        return urlencode(params, safe=":/{}")
    params = {"type": "xyz", "url": url, "zmin": str(zmin), "zmax": str(base.max_zoom if zmax is None else zmax), **auth}
    if style_url:
        params["styleUrl"] = style_url
    return urlencode(params, safe=":/{}")


_PMTILES = re.compile(r"^pmtiles://(.+?\.pmtiles)(?:/\{z\}/\{x\}/\{y\}.*)?$", re.IGNORECASE)

#: PMTiles archives also served as {z}/{x}/{y} tiles (QGIS 3.34 cannot read PMTiles): 地理院 最適化ベクトルタイル.
PMTILES_AS_XYZ = {
    "https://cyberjapandata.gsi.go.jp/xyz/optimal_bvmap-v1/optimal_bvmap-v1.pmtiles":
        "https://cyberjapandata.gsi.go.jp/xyz/optimal_bvmap-v1/{z}/{x}/{y}.pbf",
}


def pmtiles_archive(url: str) -> Optional[str]:
    """The archive of a `pmtiles://…` tile URL or source URL (None for other URLs)."""
    m = _PMTILES.match(url)
    return m.group(1) if m else None


def style_tiles(style: dict, style_url: str) -> Optional[dict]:
    """
    The first vector source of a Mapbox / MapLibre style: {"tiles": template,
    "minzoom", "maxzoom"}, {"tilejson": url} when the source points to a
    TileJSON to read, or {"pmtiles": archive} for a PMTiles archive with no
    {z}/{x}/{y} tiles known for it; None when the style has none.
    """
    for source in (style.get("sources") or {}).values():
        if not isinstance(source, dict) or source.get("type") != "vector":
            continue
        tiles = source.get("tiles")
        zooms = {"minzoom": int(source.get("minzoom", 0)), "maxzoom": int(source.get("maxzoom", 14))}
        url = tiles[0] if isinstance(tiles, list) and tiles and isinstance(tiles[0], str) else source.get("url")
        if not isinstance(url, str):
            continue
        archive = pmtiles_archive(url)
        if archive is not None:
            archive = urljoin(style_url, archive)
            if archive in PMTILES_AS_XYZ:
                return {"tiles": PMTILES_AS_XYZ[archive], **zooms}
            return {"pmtiles": archive}
        if url is not source.get("url"):
            return {"tiles": urljoin(style_url, url), **zooms}
        return {"tilejson": urljoin(style_url, url)}
    return None
