"""
The settings file's `layers`: the layers opened at start, as the viewer
reads them (config.ts `layerOf`), and what each is in QGIS (its provider and
data source). The first entry is the bottom layer.
"""
from __future__ import annotations

import posixpath
import re
from dataclasses import dataclass, field
from typing import Any, Optional
from urllib.parse import quote, unquote, urlencode, urljoin, urlsplit

TYPES = ("cog", "file", "wms", "wmts", "wfs", "esri", "ogc-features", "ogc-tiles", "wcs", "ogc-coverage", "xyz")
_SERVICES = ("wms", "wmts", "wfs", "esri", "ogc-features", "ogc-tiles", "wcs", "ogc-coverage")
_URL = re.compile(r"^(https?:)?//|^\.{0,2}/")


@dataclass
class StartupLayer:
    type: str
    url: str
    #: The layer name (WMS, WMTS, WFS, OGC API), id (Esri) or collection ("*": the whole dataset).
    layer: str = ""
    matrix_set: str = ""
    format: str = ""
    #: OGC API – Features, WCS, OGC API – Coverages: west, south, east, north (degrees).
    bbox: Optional[list[float]] = None
    #: OGC API – Features: the condition.
    filter: str = ""
    #: Esri image and map services: the display rule (as serviceRules), kept as written.
    settings: dict[str, Any] = field(default_factory=dict)


def startup_layer_of(value: Any, problems: list[str], index: int) -> Optional[StartupLayer]:
    """One entry of `layers` (the viewer's messages)."""
    if not isinstance(value, dict):
        problems.append(f"layers[{index}] はオブジェクトで指定")
        return None
    kind, url, layer = value.get("type"), value.get("url"), value.get("layer")
    if not isinstance(url, str) or not _URL.search(url):
        problems.append(f"layers[{index}] に url（http(s) か相対パス）なし")
        return None
    if kind in ("cog", "file"):
        return StartupLayer(kind, url)
    if kind == "xyz":
        return StartupLayer(kind, url, "tiles")
    if isinstance(kind, str) and kind in _SERVICES:
        bbox = value.get("bbox")
        box = None
        if (isinstance(bbox, list) and len(bbox) == 4 and all(isinstance(n, (int, float)) and not isinstance(n, bool) for n in bbox)
                and bbox[0] <= bbox[2] and bbox[1] <= bbox[3]):
            box = [float(n) for n in bbox]
        if bbox is not None and box is None:
            problems.append(f"layers[{index}].bbox は [西, 南, 東, 北]（度）の 4 つの数で指定")
        if not isinstance(layer, str) or not layer:
            problems.append(f"layers[{index}] に layer（レイヤー名）なし")
            return None
        settings = value.get("settings")
        return StartupLayer(
            kind, url, layer,
            matrix_set=value["matrixSet"] if isinstance(value.get("matrixSet"), str) else "",
            format=value["format"] if isinstance(value.get("format"), str) else "",
            bbox=box,
            filter=value["filter"].strip() if isinstance(value.get("filter"), str) else "",
            settings=settings if kind == "esri" and isinstance(settings, dict) else {},
        )
    problems.append(f"layers[{index}] の type「{kind}」は cog・file・wms・wmts・wfs・esri・ogc-features・ogc-tiles・wcs・ogc-coverage・xyz のいずれかで指定")
    return None


def startup_layers_of(value: Any, problems: list[str]) -> list[StartupLayer]:
    if not isinstance(value, list):
        problems.append("layers は配列で指定")
        return []
    return [x for x in (startup_layer_of(v, problems, i) for i, v in enumerate(value)) if x is not None]


def resolve_url(url: str, config_source: str) -> str:
    """
    A URL of the settings file as it is to be opened: relative ones (`./data/x.geojson`)
    against the settings file (a URL or a local path, as the viewer resolves them
    against its page); `//host/…` as https.
    """
    if url.startswith("//"):
        return "https:" + url
    if re.match(r"^https?://", url, re.IGNORECASE):
        return url
    if re.match(r"^https?://", config_source, re.IGNORECASE):
        return urljoin(config_source, url)
    if not config_source:
        return url
    base = posixpath.dirname(config_source.replace("\\", "/"))
    return posixpath.normpath(posixpath.join(base, unquote(url)))


@dataclass
class LayerSource:
    """What QGIS opens: the kind of layer (raster, vector, vectortile), its provider and source, and its name."""
    kind: str
    provider: str
    source: str
    name: str
    #: OGC API – Features: the condition, set as the layer's filter.
    subset: str = ""
    #: What QGIS cannot do of the entry (shown with the layer's name).
    notes: list[str] = field(default_factory=list)


def _name_of(url: str) -> str:
    path = urlsplit(url).path if re.match(r"^https?://", url, re.IGNORECASE) else url
    parts = [p for p in path.replace("\\", "/").split("/") if p]
    return unquote(parts[-1]) if parts else url


def _esri_name(url: str) -> str:
    """An Esri service's name: the part before FeatureServer / MapServer / ImageServer."""
    parts = [p for p in urlsplit(url).path.split("/") if p]
    for i, p in enumerate(parts):
        if p.lower() in ("featureserver", "mapserver", "imageserver") and i > 0:
            return unquote(parts[i - 1])
    return _name_of(url)


def _vsi(url: str) -> str:
    return "/vsicurl/" + url if re.match(r"^https?://", url, re.IGNORECASE) else url


def _ows_uri(params: dict[str, str]) -> str:
    """A WMS / WMTS / XYZ provider source (key=value pairs, the URL last as QGIS writes it)."""
    return "&".join(f"{k}={v}" for k, v in params.items())


def _service_base(url: str, kind: str) -> str:
    """An Esri service URL without the layer id, query or trailing slash."""
    u = url.split("?", 1)[0].rstrip("/")
    return re.sub(r"/\d+$", "", u) if kind != "feature" else u


def layer_source(layer: StartupLayer, config_source: str, authcfg: Optional[str] = None) -> LayerSource:
    """The QGIS layer for an entry of `layers` (its URL resolved against the settings file)."""
    url = resolve_url(layer.url, config_source)
    auth = {"authcfg": authcfg} if authcfg else {}
    notes: list[str] = []
    t = layer.type
    if t == "cog":
        return LayerSource("raster", "gdal", _vsi(url), _name_of(url))
    if t == "file":
        name = _name_of(url)
        lower = name.lower()
        if lower.endswith(".zip"):
            return LayerSource("vector", "ogr", "/vsizip/" + _vsi(url), name)
        if re.search(r"\.(tiff?|jpe?g|png|jp2|ntf|nitf|img|vrt)$", lower):
            return LayerSource("raster", "gdal", _vsi(url), name)
        return LayerSource("vector", "ogr", _vsi(url), name)
    if t == "xyz":
        template, _, fragment = url.partition("#")
        options = dict(p.split("=", 1) for p in fragment.split("&") if "=" in p)
        params = {"type": "xyz", **auth}
        if options.get("zmin"):
            params["zmin"] = options["zmin"]
        if options.get("zmax"):
            params["zmax"] = options["zmax"]
        params["url"] = quote(template, safe=":/?=,{}-")
        return LayerSource("raster", "wms", _ows_uri(params), unquote(options.get("name", "")) or _name_of(template.split("{", 1)[0]) or "XYZ")
    if t == "wms":
        params = {"crs": "EPSG:3857", "format": layer.format or "image/png", "layers": layer.layer, "styles": "", **auth, "url": url}
        return LayerSource("raster", "wms", _ows_uri(params), layer.layer)
    if t == "wmts":
        capabilities = url if "request=getcapabilities" in url.lower() else url + ("&" if "?" in url else "?") + "SERVICE=WMTS&REQUEST=GetCapabilities"
        params = {"contextualWMSLegend": "0", "crs": "EPSG:3857", "dpiMode": "7", "format": layer.format or "image/png",
                  "layers": layer.layer, "styles": "default", "tileMatrixSet": layer.matrix_set or "GoogleMapsCompatible", **auth,
                  "url": quote(capabilities, safe=":/?=&,")}
        if not layer.matrix_set:
            notes.append("matrixSet なし（GoogleMapsCompatible を使用）")
        return LayerSource("raster", "wms", _ows_uri(params), layer.layer, notes=notes)
    if t == "wfs":
        source = f"url='{url}' typename='{layer.layer}' version='auto'" + (f" authcfg='{authcfg}'" if authcfg else "")
        return LayerSource("vector", "WFS", source, layer.layer)
    if t == "ogc-features":
        source = f"url='{url.rstrip('/')}' typename='{layer.layer}'" + (f" authcfg='{authcfg}'" if authcfg else "")
        if layer.bbox:
            source += " restrictToRequestBBOX='1'"
            notes.append("bbox は表示範囲での読み込みに置き換え")
        return LayerSource("vector", "OAPIF", source, layer.layer, subset=layer.filter, notes=notes)
    if t == "ogc-tiles":
        base = url.rstrip("/")
        collection = "" if layer.layer == "*" else f"/collections/{layer.layer}"
        fmt = layer.format or "application/vnd.mapbox-vector-tile"
        template = f"{base}{collection}/tiles/WebMercatorQuad/{{z}}/{{y}}/{{x}}"
        if fmt == "application/vnd.mapbox-vector-tile":
            params = {"type": "xyz", "url": template + "?f=mvt", **auth}
            return LayerSource("vectortile", "", urlencode(params, safe=":/{}?="), layer.layer if layer.layer != "*" else _name_of(base))
        f = "jpg" if "jpeg" in fmt else "png"
        params = {"type": "xyz", **auth, "url": quote(f"{template}?f={f}", safe=":/?=,{}")}
        return LayerSource("raster", "wms", _ows_uri(params), layer.layer if layer.layer != "*" else _name_of(base))
    if t == "wcs":
        params = {"identifier": layer.layer, **auth, "url": url}
        if layer.bbox:
            notes.append("bbox は無視（全体を読む）")
        return LayerSource("raster", "wcs", urlencode(params, safe=":/"), layer.layer, notes=notes)
    if t == "ogc-coverage":
        return LayerSource("raster", "gdal", f"OGCAPI:{url.rstrip('/')}/collections/{layer.layer}", layer.layer)
    if t == "esri":
        lower = url.lower()
        if "/featureserver" in lower or ("/mapserver" in lower and re.fullmatch(r"\d+", layer.layer)):
            target = f"{_service_base(url, 'service')}/{layer.layer}" if re.fullmatch(r"\d+", layer.layer) else url
            source = f"crs='EPSG:4326' url='{target}'" + (f" authcfg='{authcfg}'" if authcfg else "")
            return LayerSource("vector", "arcgisfeatureserver", source, f"{_esri_name(url)} {layer.layer}")
        if layer.settings:
            notes.append("表示ルール（settings）は QGIS の ArcGIS プロバイダーで未対応")
        base = _service_base(url, "service")
        source = f"crs='EPSG:3857' format='PNG32' layer='' url='{base}'" + (f" authcfg='{authcfg}'" if authcfg else "")
        return LayerSource("raster", "arcgismapserver", source, _esri_name(base), notes=notes)
    raise ValueError(t)
