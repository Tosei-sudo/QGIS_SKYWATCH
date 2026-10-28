"""
ArcGIS Web Maps (the JSON of a portal's 「Web Map」 item, version 2.x), as
the viewer reads and writes them (webmap.ts). Only data in and data out
here; qgis_io/webmap_io.py opens the layers in QGIS and writes the project.

Reading: the operational layers (feature, map, image, tiled map, vector
tile, WMS, WMTS, WFS and web tiled layers; group layers flattened; feature
collections stored in the map) with their visibility, opacity, scale range,
definition expression, renderer and labels; the base map; the initial
extent; the bookmarks. What cannot be shown is listed, with the reason.

Writing: layers with a service URL as service layers, vector layers
without one (files, memory layers) as feature collections (capped in size),
the view, the base map and the bookmarks. The renderers and label classes
are made by the caller (from QGIS's styles) and written as given.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any, Optional
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from .basemaps import BaseMapConfig, attribution_text
from .util import is_record

#: The web map version written.
WEBMAP_VERSION = "2.31"
#: The most features one feature collection of a saved web map holds.
MAX_COLLECTION_FEATURES = 5_000
#: The largest web map written (JSON characters): portals refuse much larger item data.
MAX_WEBMAP_CHARS = 10_000_000
#: The largest web map read (bytes).
MAX_WEBMAP_READ = 50_000_000
#: ArcGIS Online, for an item id typed without a portal.
DEFAULT_PORTAL = "https://www.arcgis.com"
#: Web Mercator as web maps write it.
WEB_MERCATOR = {"wkid": 102100, "latestWkid": 3857}
#: The color of layers written without a style of their own (the viewer's default).
DEFAULT_COLOR = (0x43, 0x63, 0xD8)

_ITEM_ID = re.compile(r"^[0-9a-f]{32}$", re.IGNORECASE)


def _str(v: Any) -> Optional[str]:
    return v.strip() if isinstance(v, str) and v.strip() else None


def _num(v: Any) -> Optional[float]:
    return v if isinstance(v, (int, float)) and not isinstance(v, bool) and v == v and abs(v) != float("inf") else None


# ---- Where a web map is ----

@dataclass(frozen=True)
class WebMapItemRef:
    """A web map item of a portal."""
    #: The portal's root (`https://www.arcgis.com`, `https://gis.example.com/portal`), without a trailing slash.
    portal: str
    id: str


def portal_root(text: str) -> Optional[str]:
    """A portal URL typed: its root without a trailing slash (and without `/home…`, `/apps…` or `/sharing…`), or None."""
    try:
        u = urlsplit(text.strip())
    except ValueError:
        return None
    if u.scheme not in ("http", "https") or not u.netloc:
        return None
    path = re.sub(r"/(?:home|apps|sharing)(?:/.*)?$", "", u.path, flags=re.IGNORECASE).rstrip("/")
    return f"{u.scheme}://{u.netloc.lower()}{path}"


def parse_item_input(text: str, portal: str = DEFAULT_PORTAL) -> Optional[WebMapItemRef]:
    """
    The item a typed text names: an item page (`…/home/item.html?id=…`), a
    Map Viewer link (`…/apps/mapviewer/index.html?webmap=…`,
    `…/home/webmap/viewer.html?webmap=…`), a REST URL of the item
    (`…/sharing/rest/content/items/…`), or a bare item id on `portal`.
    """
    text = text.strip()
    if _ITEM_ID.match(text):
        root = portal_root(portal)
        return WebMapItemRef(root, text.lower()) if root else None
    try:
        u = urlsplit(text)
    except ValueError:
        return None
    if u.scheme not in ("http", "https") or not u.netloc:
        return None
    params = dict(parse_qsl(u.query))
    rest = re.match(r"^(.*?)/sharing/rest/content/items/([0-9a-f]{32})(?:/|$)", u.path, re.IGNORECASE)
    item = rest.group(2) if rest else next((v for v in (params.get("webmap"), params.get("id"), params.get("itemId")) if v and _ITEM_ID.match(v)), None)
    if not item:
        return None
    if rest:
        path = rest.group(1)
    else:
        m = re.match(r"^(.*?)/(?:home|apps)/", u.path, re.IGNORECASE)
        if not m:
            return None
        path = m.group(1)
    return WebMapItemRef(f"{u.scheme}://{u.netloc.lower()}{path.rstrip('/')}", item.lower())


def item_url(ref: WebMapItemRef) -> str:
    return f"{ref.portal}/sharing/rest/content/items/{ref.id}"


def item_data_url(ref: WebMapItemRef) -> str:
    return f"{item_url(ref)}/data"


def item_page(ref: WebMapItemRef) -> str:
    return f"{ref.portal}/home/item.html?id={ref.id}"


def user_content_url(portal: str, user: str, folder: Optional[str] = None) -> str:
    from urllib.parse import quote

    return f"{portal}/sharing/rest/content/users/{quote(user, safe='')}" + (f"/{quote(folder, safe='')}" if folder else "")


def token_allowed(url: str, portal: str) -> bool:
    """
    Whether the sign-in of `portal` may go with requests to `url`: to the
    portal's own host, and from ArcGIS Online to its hosted services
    (`*.arcgis.com`). Never to other hosts, and never from https down to http.
    """
    try:
        target, home = urlsplit(url), urlsplit(portal)
    except ValueError:
        return False
    if not target.netloc or not home.netloc:
        return False
    if target.scheme != "https" and not (target.scheme == "http" and home.scheme == "http" and target.netloc == home.netloc):
        return False
    if target.netloc.lower() == home.netloc.lower():
        return True

    def online(host: str) -> bool:
        return host == "arcgis.com" or host.endswith(".arcgis.com")

    return online((home.hostname or "").lower()) and online((target.hostname or "").lower())


# ---- Reading ----

@dataclass
class ServiceRef:
    """A service layer: Esri (`layer` a feature layer's id, "map" or "image"), WMS, WMTS or WFS."""
    kind: str
    url: str
    layer: str
    #: Esri: where (feature), layers / layerDefs (map), renderingRule / where / method / sortField / bandIds (image).
    settings: dict = field(default_factory=dict)
    matrix_set: str = ""
    format: str = ""


@dataclass
class CollectionPart:
    """One layer of a feature collection stored in a web map."""
    name: str
    geometry_type: str
    #: Esri field definitions ({name, type, alias?, length?}).
    fields: list
    #: Esri JSON features ({geometry, attributes}).
    features: list
    wkid: int
    renderer: Optional[dict] = None
    #: The label classes (`labelingInfo`), when it has labels.
    labeling: Optional[list] = None
    #: The popup labels of the attributes (name → label).
    aliases: dict = field(default_factory=dict)


@dataclass
class WebMapLayer:
    """A layer of a web map, as it will be opened."""
    title: str
    visible: bool
    #: 0–1.
    opacity: float
    #: Hidden when zoomed out beyond 1:min_scale (Esri scale); None: no limit.
    min_scale: Optional[float]
    #: Hidden when zoomed in beyond 1:max_scale; None: no limit.
    max_scale: Optional[float]
    #: The layer as the web map has it, to write it back unchanged when it cannot be made again.
    source: dict
    #: service, tiles, vector-tiles or collection.
    type: str = "service"
    id: str = ""
    ref: Optional[ServiceRef] = None
    #: Esri: the portal's sign-in may go with it.
    esri: bool = False
    #: A feature layer's renderer in the web map (overriding the service's).
    renderer: Optional[dict] = None
    #: A feature layer's label classes when shown; None: the service's own (or none).
    labeling: Optional[list] = None
    #: The web map turns the labels off.
    labels_off: bool = False
    aliases: dict = field(default_factory=dict)
    #: tiles: XYZ URLs ({z}/{x}/{y}).
    urls: list = field(default_factory=list)
    attributions: str = ""
    #: vector-tiles: the VectorTileServer (or style) URL, and the style.
    url: str = ""
    style: str = ""
    parts: list = field(default_factory=list)
    #: A base map's reference layer (labels drawn over the operational layers).
    reference: bool = False


@dataclass
class WebMapExtent:
    xmin: float
    ymin: float
    xmax: float
    ymax: float
    wkid: int


@dataclass
class WebMapBaseMap:
    title: str
    layers: list
    #: The web map's base map as it was, to write it back.
    source: Optional[dict]


@dataclass
class WebMapPlan:
    """A web map, read."""
    #: Operational layers, bottom first.
    layers: list
    basemap: WebMapBaseMap
    #: (title, why) of what cannot be shown.
    unsupported: list
    extent: Optional[WebMapExtent]
    #: (name, extent).
    bookmarks: list


UNSUPPORTED_REASONS = {
    "CSV": "CSV レイヤーには非対応",
    "GeoJSON": "GeoJSON レイヤーには非対応（ファイルとして開く）",
    "KML": "KML レイヤーには非対応",
    "GeoRSS": "GeoRSS レイヤーには非対応",
    "OGCFeatureLayer": "OGC API - Features には非対応",
    "ArcGISSceneServiceLayer": "シーンレイヤーには非対応",
    "IntegratedMeshLayer": "統合メッシュには非対応",
    "PointCloudLayer": "点群には非対応",
    "BuildingSceneLayer": "ビルディングシーンレイヤーには非対応",
    "ArcGISStreamLayer": "ストリームレイヤーには非対応（「移動体」ドックで接続）",
    "SubtypeGroupLayer": "サブタイプグループレイヤーには非対応",
    "BingMapsAerial": "Bing Maps には非対応",
    "BingMapsRoad": "Bing Maps には非対応",
    "BingMapsHybrid": "Bing Maps には非対応",
    "MediaLayer": "メディアレイヤーには非対応",
    "ArcGISImageServiceVectorLayer": "ベクターフィールドの表示には非対応",
    "KnowledgeGraphLayer": "ナレッジグラフには非対応",
}

_TOP = {"visible": True, "opacity": 1.0, "min_scale": None, "max_scale": None}


def plan_web_map(value: Any) -> WebMapPlan:
    """Reads a web map's JSON; anything malformed is skipped and listed, never raised, except a document that is not a web map."""
    if not is_record(value) or (not isinstance(value.get("operationalLayers"), list) and not is_record(value.get("baseMap"))):
        raise ValueError("Web マップの JSON 以外（operationalLayers・baseMap なし）")
    unsupported: list = []
    layers: list = []

    def walk(items: Any, parent: dict) -> None:
        if not isinstance(items, list):
            return
        for item in items:
            if not is_record(item):
                continue
            base = _layer_base(item, parent)
            if item.get("layerType") == "GroupLayer":
                walk(item.get("layers"), base)
                continue
            read = _read_layer(item, base)
            if isinstance(read, str):
                unsupported.append((base["title"], read))
            else:
                layers.extend(read)

    walk(value.get("operationalLayers"), _TOP)

    base_map = value.get("baseMap") if is_record(value.get("baseMap")) else None
    basemap_layers: list = []
    for item in (base_map or {}).get("baseMapLayers") or []:
        if not is_record(item):
            continue
        base = _layer_base(item, _TOP)
        read = _read_layer(item, base)
        if isinstance(read, str):
            unsupported.append((f"背景地図: {base['title']}", read))
        else:
            for layer in read:
                layer.reference = item.get("isReference") is True
                basemap_layers.append(layer)

    wkid = _wkid_of(value.get("spatialReference")) or 102100
    state = value.get("initialState")
    initial = state["viewpoint"].get("targetGeometry") if is_record(state) and is_record(state.get("viewpoint")) else None
    options = value.get("mapOptions")
    extent = _extent_of(initial, wkid) or _extent_of(options.get("extent") if is_record(options) else None, wkid)
    bookmarks = []
    for b in value.get("bookmarks") or []:
        if not is_record(b):
            continue
        target = b["viewpoint"].get("targetGeometry") if is_record(b.get("viewpoint")) else None
        e = _extent_of(b.get("extent"), wkid) or _extent_of(target, wkid)
        if e:
            bookmarks.append((_str(b.get("name")) or "ブックマーク", e))
    return WebMapPlan(layers, WebMapBaseMap(_str((base_map or {}).get("title")) or "背景地図", basemap_layers, base_map), unsupported, extent, bookmarks)


def item_extent(item: Any) -> Optional[WebMapExtent]:
    """The extent of an item (`[[xmin, ymin], [xmax, ymax]]` in degrees), for maps that have none of their own."""
    e = item.get("extent") if is_record(item) else None
    if not isinstance(e, list) or len(e) != 2 or not all(isinstance(p, list) and len(p) == 2 for p in e):
        return None
    (xmin, ymin), (xmax, ymax) = e
    if any(_num(v) is None for v in (xmin, ymin, xmax, ymax)) or not (xmax > xmin and ymax > ymin):
        return None
    return WebMapExtent(xmin, ymin, xmax, ymax, 4326)


def _wkid_of(sr: Any) -> Optional[int]:
    if not is_record(sr):
        return None
    v = _num(sr.get("latestWkid")) or _num(sr.get("wkid"))
    return int(v) if v else None


def _extent_of(e: Any, fallback: int) -> Optional[WebMapExtent]:
    if not is_record(e):
        return None
    values = [_num(e.get(k)) for k in ("xmin", "ymin", "xmax", "ymax")]
    if any(v is None for v in values) or not (values[2] > values[0] and values[3] > values[1]):
        return None
    return WebMapExtent(*values, _wkid_of(e.get("spatialReference")) or fallback)


def _layer_base(item: dict, parent: dict) -> dict:
    def scale(v: Any) -> Optional[float]:
        n = _num(v)
        return n if n is not None and n > 0 else None

    definition = item.get("layerDefinition") if is_record(item.get("layerDefinition")) else {}
    min_scale = scale(item.get("minScale")) or scale(definition.get("minScale"))
    max_scale = scale(item.get("maxScale")) or scale(definition.get("maxScale"))

    # A group's range narrows its layers': the smaller "zoomed out" limit and the larger "zoomed in" one.
    def narrow(a, b, pick):
        return b if a is None else a if b is None else pick(a, b)

    drawing = definition.get("drawingInfo") if is_record(definition.get("drawingInfo")) else {}
    transparency = _num(drawing.get("transparency"))
    opacity = max(0.0, min(1.0, _num(item.get("opacity")) if _num(item.get("opacity")) is not None else 1.0))
    if transparency is not None:
        opacity *= 1 - max(0.0, min(100.0, transparency)) / 100
    url = _str(item.get("url"))
    return {
        "id": _str(item.get("id")) or "",
        "title": _str(item.get("title")) or _str(item.get("name")) or ("/".join(url.split("/")[-2:]) if url else "レイヤー"),
        "visible": parent["visible"] and item.get("visibility") is not False,
        "opacity": parent["opacity"] * opacity,
        "min_scale": narrow(parent["min_scale"], min_scale, min),
        "max_scale": narrow(parent["max_scale"], max_scale, max),
        "source": item,
    }


def _layer(base: dict, **kw) -> WebMapLayer:
    return WebMapLayer(**base, **kw)


def _read_layer(item: dict, base: dict) -> "list[WebMapLayer] | str":
    """One layer of a web map (not a group) as layers to open (one, or a WMS layer's several), or why it cannot be shown."""
    kind = _str(item.get("layerType")) or ("ArcGISFeatureLayer" if is_record(item.get("featureCollection")) else "")
    url = _str(item.get("url"))
    definition = item.get("layerDefinition") if is_record(item.get("layerDefinition")) else {}
    drawing = definition.get("drawingInfo") if is_record(definition.get("drawingInfo")) else {}
    if url and not re.match(r"^https?://", url, re.IGNORECASE):
        return "URL が http(s) 以外"
    bare = re.sub(r"\?.*$", "", url) if url else ""
    if kind == "ArcGISFeatureLayer":
        if is_record(item.get("featureCollection")):
            parts = _collection_parts(item["featureCollection"])
            return [_layer(base, type="collection", parts=parts)] if parts else "フィーチャーコレクションに地物なし"
        if not url:
            return "URL なし（アイテムの地物は読み込み不可）"
        m = re.match(r"^(.*/(?:FeatureServer|MapServer))(?:/(\d+))?/?$", bare, re.IGNORECASE)
        if not m:
            return "FeatureServer・MapServer のレイヤーの URL 以外"
        layer_id = m.group(2) or str(int(_num(item.get("layerId")) or 0))
        where = _str(definition.get("definitionExpression"))
        off = item.get("showLabels") is False or definition.get("showLabels") is False
        labeling = drawing.get("labelingInfo") if isinstance(drawing.get("labelingInfo"), list) and drawing["labelingInfo"] and not off else None
        return [_layer(base, type="service", esri=True,
                       ref=ServiceRef("esri", f"{m.group(1)}/{layer_id}", layer_id, {"where": where} if where else {}),
                       renderer=drawing["renderer"] if is_record(drawing.get("renderer")) else None,
                       labeling=labeling, labels_off=off, aliases=popup_aliases(item.get("popupInfo")))]
    if kind in ("ArcGISMapServiceLayer", "ArcGISTiledMapServiceLayer"):
        m = re.match(r"^(.*/MapServer)/?$", bare, re.IGNORECASE)
        if not m:
            return "MapServer の URL 以外"
        settings: dict = {}
        if isinstance(item.get("visibleLayers"), list):
            settings["layers"] = [v for v in item["visibleLayers"] if isinstance(v, int) and not isinstance(v, bool)]
        defs = {}
        for sub in item.get("layers") or []:
            where = _str(sub["layerDefinition"].get("definitionExpression")) if is_record(sub) and is_record(sub.get("layerDefinition")) else None
            if where and isinstance(sub.get("id"), int):
                defs[str(sub["id"])] = where
        if defs:
            settings["layerDefs"] = defs
        return [_layer(base, type="service", esri=True, ref=ServiceRef("esri", m.group(1), "map", settings))]
    if kind in ("ArcGISImageServiceLayer", "ArcGISTiledImageServiceLayer"):
        m = re.match(r"^(.*/ImageServer)/?$", bare, re.IGNORECASE)
        if not m:
            return "ImageServer の URL 以外"
        settings = {}
        if is_record(item.get("renderingRule")):
            settings["renderingRule"] = json.dumps(item["renderingRule"], ensure_ascii=False, separators=(",", ":"))
        mosaic = item.get("mosaicRule") if is_record(item.get("mosaicRule")) else {}
        for key, name in (("where", "where"), ("mosaicMethod", "method"), ("sortField", "sortField")):
            if _str(mosaic.get(key)):
                settings[name] = _str(mosaic.get(key))
        if isinstance(item.get("bandIds"), list) and all(isinstance(b, int) for b in item["bandIds"]):
            settings["bandIds"] = item["bandIds"]
        return [_layer(base, type="service", esri=True, ref=ServiceRef("esri", m.group(1), "image", settings))]
    if kind == "WMS":
        if not url:
            return "URL なし"
        names = [v for v in item.get("visibleLayers") or [] if isinstance(v, str) and v]
        listed = [_str(sub.get("name")) for sub in item.get("layers") or [] if is_record(sub) and _str(sub.get("name"))]
        shown = names or listed[:1]
        if not shown:
            return "表示する WMS レイヤーなし"
        service = _str(item.get("mapUrl")) or url
        return [_layer({**base, "title": f"{base['title']}: {name}" if len(shown) > 1 else base["title"]}, type="service",
                       ref=ServiceRef("wms", service, name)) for name in shown]
    if kind == "WMTS":
        info = item.get("wmtsInfo") if is_record(item.get("wmtsInfo")) else {}
        service = _str(info.get("url")) or url
        identifier = _str(info.get("layerIdentifier"))
        if not service or not identifier:
            return "WMTS のレイヤー（wmtsInfo）なし"
        return [_layer(base, type="service", ref=ServiceRef("wmts", service, identifier, matrix_set=_str(info.get("tileMatrixSet")) or "",
                                                           format=_str(item.get("format")) or ""))]
    if kind == "WFS":
        info = item.get("wfsInfo") if is_record(item.get("wfsInfo")) else {}
        name = _str(info.get("name"))
        if not url or not name:
            return "WFS の地物型（wfsInfo.name）なし"
        return [_layer(base, type="service", ref=ServiceRef("wfs", url, name))]
    if kind == "WebTiledLayer":
        template = _str(item.get("templateUrl"))
        if not template or not re.match(r"^https?://", template, re.IGNORECASE):
            return "タイルの URL（templateUrl）なし"
        return [_layer(base, type="tiles", urls=tile_urls(template, item.get("subDomains")), attributions=_str(item.get("copyright")) or "")]
    if kind == "OpenStreetMap":
        return [_layer(base, type="tiles", urls=["https://tile.openstreetmap.org/{z}/{x}/{y}.png"], attributions="© OpenStreetMap contributors")]
    if kind == "VectorTileLayer":
        style = _str(item.get("styleUrl"))
        if style and not re.match(r"^https?://", style, re.IGNORECASE):
            return "スタイルの URL が http(s) 以外"
        m = re.match(r"^(.*/VectorTileServer)(?:/|$)", style, re.IGNORECASE) if style else None
        service = url or (m.group(1) if m else None)
        if not service and not style:
            return "URL なし"
        return [_layer(base, type="vector-tiles", url=service or style, style=style or "")]
    if not kind:
        return "種類（layerType）なし"
    return UNSUPPORTED_REASONS.get(kind, f"{kind} には非対応")


def tile_urls(template: str, sub_domains: Any) -> list:
    """A web tiled layer's template (`{level}`, `{col}`, `{row}`, `{subDomain}`) as XYZ URLs."""
    xyz = template.replace("{level}", "{z}").replace("{col}", "{x}").replace("{row}", "{y}")
    subs = [s for s in sub_domains if isinstance(s, str) and re.fullmatch(r"[\w-]+", s)] if isinstance(sub_domains, list) else []
    if "{subDomain}" not in xyz:
        return [xyz]
    return [xyz.replace("{subDomain}", s) for s in (subs or ["a"])]


def popup_aliases(popup_info: Any) -> dict:
    """The labels a web map's popup gives the attributes (`popupInfo.fieldInfos`), where they differ from the names."""
    out = {}
    infos = popup_info.get("fieldInfos") if is_record(popup_info) else None
    for info in infos if isinstance(infos, list) else []:
        if not is_record(info):
            continue
        name, label = _str(info.get("fieldName")), _str(info.get("label"))
        if name and label and label != name and not re.match(r"^(relationships|expression)/", name):
            out[name] = label
    return out


def _collection_parts(collection: dict) -> list:
    parts = []
    for sub in collection.get("layers") or []:
        if not is_record(sub):
            continue
        definition = sub.get("layerDefinition") if is_record(sub.get("layerDefinition")) else {}
        feature_set = sub.get("featureSet") if is_record(sub.get("featureSet")) else {}
        features = [f for f in feature_set.get("features") or [] if is_record(f)]
        if not features:
            continue
        drawing = definition.get("drawingInfo") if is_record(definition.get("drawingInfo")) else {}
        first = features[0].get("geometry")
        parts.append(CollectionPart(
            name=_str(definition.get("name")) or "",
            geometry_type=_str(feature_set.get("geometryType")) or _str(definition.get("geometryType")) or "",
            fields=[f for f in definition.get("fields") or [] if is_record(f) and isinstance(f.get("name"), str) and isinstance(f.get("type"), str)],
            features=features,
            wkid=_wkid_of(feature_set.get("spatialReference")) or _wkid_of(definition.get("spatialReference"))
            or _wkid_of(first.get("spatialReference") if is_record(first) else None) or 102100,
            renderer=drawing["renderer"] if is_record(drawing.get("renderer")) else None,
            labeling=drawing["labelingInfo"] if isinstance(drawing.get("labelingInfo"), list) and drawing["labelingInfo"] else None,
            aliases=popup_aliases(sub.get("popupInfo")),
        ))
    return parts


def label_field_of(labeling: Any) -> Optional[str]:
    """The attribute named by a layer's first label class (`$feature.NAME`, `$feature["NAME"]`, `[NAME]`), or None."""
    first = next((x for x in labeling if is_record(x)), None) if isinstance(labeling, list) else None
    if not first:
        return None
    info = first.get("labelExpressionInfo")
    arcade = _str(info.get("expression")) if is_record(info) else None
    m = re.match(r"^\$feature(?:\.(\w+)|\[\s*[\"'](.+?)[\"']\s*\])$", arcade) if arcade else None
    if m:
        return m.group(1) or m.group(2)
    old = _str(first.get("labelExpression"))
    m = re.match(r"^\[(\w+)\]$", old) if old else None
    return m.group(1) if m else None


# ---- Writing ----

def geometry_kind(esri_type: Optional[str]) -> str:
    """point, line or polygon, for an Esri or OGC geometry type name."""
    if not esri_type:
        return "point"
    if re.search(r"polyline|linestring|line", esri_type, re.IGNORECASE):
        return "line"
    if re.search(r"polygon|envelope", esri_type, re.IGNORECASE):
        return "polygon"
    return "point"


GEOMETRY_TYPES = {"point": "esriGeometryPoint", "line": "esriGeometryPolyline", "polygon": "esriGeometryPolygon"}


def default_symbol(kind: str) -> dict:
    """The symbol of a layer written without a style of its own."""
    r, g, b = DEFAULT_COLOR
    outline = {"type": "esriSLS", "style": "esriSLSSolid", "color": [255, 255, 255, 255], "width": 0.75}
    if kind == "line":
        return {"type": "esriSLS", "style": "esriSLSSolid", "color": [r, g, b, 255], "width": 1.5}
    if kind == "polygon":
        return {"type": "esriSFS", "style": "esriSFSSolid", "color": [r, g, b, 64], "outline": {**outline, "color": [r, g, b, 255], "width": 1.5}}
    return {"type": "esriSMS", "style": "esriSMSCircle", "color": [r, g, b, 255], "size": 6, "angle": 0, "xoffset": 0, "yoffset": 0, "outline": outline}


@dataclass
class SaveLayer:
    """One layer to write, bottom first: a service (`ref`), JSON kept from a web map, features, or why it cannot be written."""
    title: str
    visible: bool
    opacity: float
    #: ref, json, features or none.
    type: str
    min_scale: Optional[float] = None
    max_scale: Optional[float] = None
    ref: Optional[ServiceRef] = None
    #: The renderer (made from the layer's style) and its geometry kind; label classes.
    renderer: Optional[dict] = None
    kind: str = "point"
    labeling: Optional[list] = None
    json: Optional[dict] = None
    #: features: (kind, Esri geometry in Web Mercator, attributes).
    features: list = field(default_factory=list)
    #: features: Esri field definitions (no object id).
    fields: list = field(default_factory=list)
    reason: str = ""


@dataclass
class SaveResult:
    json: dict
    included: list
    #: (title, why).
    skipped: list


def build_web_map(layers: list, extent: Optional[tuple] = None, basemap: Optional[BaseMapConfig] = None,
                  basemap_json: Optional[dict] = None, bookmarks: Optional[list] = None) -> SaveResult:
    """
    The JSON of a web map (version 2.x): `layers` bottom first, `extent` the
    view in Web Mercator, the base map (one of the settings file's, else the
    one a web map came with, written back as it was), bookmarks as (name,
    Web Mercator extent).
    """
    included: list = []
    skipped: list = []
    operational: list = []
    size = 0
    for i, layer in enumerate(layers):
        common: dict = {"id": f"layer-{i + 1}", "title": layer.title, "visibility": layer.visible, "opacity": round(layer.opacity, 3)}
        if layer.min_scale:
            common["minScale"] = layer.min_scale
        if layer.max_scale:
            common["maxScale"] = layer.max_scale
        if layer.type == "none":
            out: "dict | str" = layer.reason
        elif layer.type == "json":
            out = {**layer.json, **common, "id": _str(layer.json.get("id")) or common["id"]}
        elif layer.type == "ref":
            out = _ref_layer(layer, common)
        else:
            out = _collection_layer(layer, common)
        if isinstance(out, str):
            skipped.append((layer.title, out))
            continue
        chars = len(json.dumps(out, ensure_ascii=False))
        if size + chars > MAX_WEBMAP_CHARS:
            skipped.append((layer.title, f"大きすぎ（Web マップ全体で {MAX_WEBMAP_CHARS // 1_000_000} MB まで）"))
            continue
        size += chars
        operational.append(out)
        included.append(layer.title)
    base_json, base_skipped = _basemap_json(basemap, basemap_json)
    if base_skipped:
        skipped.append(base_skipped)
    out_json: dict = {
        "operationalLayers": operational,
        "baseMap": base_json,
        "spatialReference": WEB_MERCATOR,
        "authoringApp": "satellite_observation",
        "authoringAppVersion": "1",
        "version": WEBMAP_VERSION,
    }
    if extent:
        out_json["initialState"] = {"viewpoint": {"targetGeometry": _extent_json(extent)}}
    marks = [{"name": name, "extent": _extent_json(e), "viewpoint": {"targetGeometry": _extent_json(e)}} for name, e in bookmarks or []]
    if marks:
        out_json["bookmarks"] = marks
    return SaveResult(out_json, included, skipped)


def _extent_json(e: tuple) -> dict:
    return {"xmin": round(e[0], 2), "ymin": round(e[1], 2), "xmax": round(e[2], 2), "ymax": round(e[3], 2), "spatialReference": WEB_MERCATOR}


def service_root(url: str) -> str:
    """A service URL without the request parameters added to it (SERVICE, REQUEST, VERSION)."""
    try:
        u = urlsplit(url)
    except ValueError:
        return url
    query = [(k, v) for k, v in parse_qsl(u.query, keep_blank_values=True) if k.lower() not in ("service", "request", "version")]
    return urlunsplit((u.scheme, u.netloc, u.path, urlencode(query, safe=":/,"), u.fragment))


def _ref_layer(layer: SaveLayer, common: dict) -> "dict | str":
    ref = layer.ref
    settings = ref.settings or {}
    if ref.kind == "esri":
        if ref.layer == "map":
            subs = [{"id": int(k), "layerDefinition": {"definitionExpression": v}} for k, v in (settings.get("layerDefs") or {}).items()]
            return {**common, "layerType": "ArcGISMapServiceLayer", "url": ref.url,
                    **({"visibleLayers": settings["layers"]} if settings.get("layers") else {}), **({"layers": subs} if subs else {})}
        if ref.layer == "image":
            try:
                rule = json.loads(settings["renderingRule"]) if settings.get("renderingRule") else None
            except ValueError:
                rule = None
            mosaic = None
            if settings.get("where") or settings.get("method"):
                mosaic = {"mosaicMethod": settings.get("method") or "esriMosaicNone", **({"where": settings["where"]} if settings.get("where") else {}),
                          **({"sortField": settings["sortField"]} if settings.get("sortField") else {})}
            return {**common, "layerType": "ArcGISImageServiceLayer", "url": ref.url, **({"renderingRule": rule} if rule else {}),
                    **({"mosaicRule": mosaic} if mosaic else {}), **({"bandIds": settings["bandIds"]} if settings.get("bandIds") else {})}
        definition: dict = {}
        if settings.get("where"):
            definition["definitionExpression"] = settings["where"]
        drawing = {**({"renderer": layer.renderer} if layer.renderer else {}), **({"labelingInfo": layer.labeling} if layer.labeling else {})}
        if drawing:
            definition["drawingInfo"] = drawing
        return {**common, "layerType": "ArcGISFeatureLayer", "url": ref.url, **({"showLabels": True} if layer.labeling else {}),
                **({"layerDefinition": definition} if definition else {})}
    if ref.kind == "wms":
        return {**common, "layerType": "WMS", "url": service_root(ref.url), "visibleLayers": [ref.layer], "layers": [{"name": ref.layer, "title": layer.title}]}
    if ref.kind == "wmts":
        info = {"url": service_root(ref.url), "layerIdentifier": ref.layer, **({"tileMatrixSet": ref.matrix_set} if ref.matrix_set else {})}
        return {**common, "layerType": "WMTS", "url": service_root(ref.url), **({"format": ref.format} if ref.format else {}), "wmtsInfo": info}
    if ref.kind == "wfs":
        return {**common, "layerType": "WFS", "url": service_root(ref.url), "wfsInfo": {"name": ref.layer}}
    return "書き出し不可の種類"


def _collection_layer(layer: SaveLayer, common: dict) -> "dict | str":
    features = [f for f in layer.features if f[1]]
    if not features:
        return "地物なし"
    if len(features) > MAX_COLLECTION_FEATURES:
        return f"地物が多すぎ（{len(features):,} 件。フィーチャーコレクションは {MAX_COLLECTION_FEATURES:,} 件まで）"
    # One part per kind of geometry: a collection layer holds one.
    by_kind: dict = {}
    for kind, geometry, attributes in features:
        by_kind.setdefault(kind, []).append((geometry, attributes))
    fields = [{"name": "__OBJECTID", "type": "esriFieldTypeOID", "alias": "OBJECTID"}, *layer.fields]
    names = [f["name"] for f in layer.fields]
    parts = []
    for part, (kind, items) in enumerate(by_kind.items()):
        renderer = layer.renderer if layer.renderer and kind == layer.kind else {"type": "simple", "symbol": default_symbol(kind)}
        rows = []
        for n, (geometry, attributes) in enumerate(items, 1):
            row = {"__OBJECTID": n + part * MAX_COLLECTION_FEATURES}
            row.update({name: attributes.get(name) for name in names})
            rows.append({"geometry": round_geometry(geometry), "attributes": row})
        parts.append({
            "layerDefinition": {
                "name": f"{layer.title} ({kind})" if len(by_kind) > 1 else layer.title,
                "type": "Feature Layer",
                "geometryType": GEOMETRY_TYPES[kind],
                "objectIdField": "__OBJECTID",
                "fields": fields,
                "drawingInfo": {"renderer": renderer, **({"labelingInfo": layer.labeling} if layer.labeling else {})},
            },
            "featureSet": {"geometryType": GEOMETRY_TYPES[kind], "spatialReference": WEB_MERCATOR, "features": rows},
        })
    return {**common, "layerType": "ArcGISFeatureLayer", "featureCollection": {"layers": parts, "showLegend": True}}


def round_geometry(geometry: dict) -> dict:
    """Coordinates to centimetres, which is all a web map needs (and keeps it small); no spatial reference."""
    def r(v):
        return round(v, 2) if isinstance(v, float) else [r(x) for x in v] if isinstance(v, list) else v

    return {k: r(v) for k, v in geometry.items() if k != "spatialReference"}


def template_of(url: str) -> str:
    """An XYZ URL template as a web tiled layer's (`{level}`, `{col}`, `{row}`, `{subDomain}`)."""
    url = url.replace("{z}", "{level}").replace("{x}", "{col}")
    url = re.sub(r"\{-?y\}", "{row}", url)
    return re.sub(r"\{[a-c]-[a-c]\}", "{subDomain}", url)


def _basemap_json(config: Optional[BaseMapConfig], kept: Optional[dict]) -> tuple:
    """The base map: one of the settings file's as a web map's, else the web map's own kept, else none; and what was left out."""
    if config:
        title = config.label
        layer = {"id": f"basemap-{config.id}", "title": title, "visibility": True, "opacity": 1}
        if config.type == "xyz":
            credit = attribution_text(config.attributions) if config.attributions else ""
            return {"title": title, "baseMapLayers": [{**layer, "layerType": "WebTiledLayer", "templateUrl": template_of(config.url),
                                                        **({"copyright": credit} if credit else {})}]}, None
        if config.type == "esri":
            url = config.url.rstrip("/")
            return {"title": title, "baseMapLayers": [{**layer, "layerType": "VectorTileLayer", "styleUrl": config.style or f"{url}/resources/styles/root.json"}]}, None
        if config.type == "style":
            return {"title": title, "baseMapLayers": [{**layer, "layerType": "VectorTileLayer", "styleUrl": config.url}]}, None
        # Plain {z}/{x}/{y}.pbf tiles have no style a web map could name.
        return {"title": "背景なし", "baseMapLayers": []}, (f"背景地図: {title}", "スタイルのないベクタータイルは Web マップへの書き込み不可")
    if kept and isinstance(kept.get("baseMapLayers"), list):
        return kept, None
    return {"title": "背景なし", "baseMapLayers": []}, None


# ---- Base maps ----

def _loose(url: str) -> str:
    """A URL compared loosely: no scheme, query, trailing slash or case."""
    url = re.sub(r"^https?://", "", url, flags=re.IGNORECASE)
    return re.sub(r"[?#].*$", "", url).rstrip("/").lower()


def match_base_map(basemap: WebMapBaseMap, configs: list) -> Optional[BaseMapConfig]:
    """The settings file's base map a web map's base map is, matched by its layers' URLs (tile template, VectorTileServer, style), else by title."""
    for layer in (x for x in basemap.layers if not x.reference):
        if layer.type == "tiles":
            urls = [_loose(u) for u in layer.urls]
        elif layer.type == "vector-tiles":
            urls = [_loose(u) for u in (layer.url, layer.style) if u]
        elif layer.type == "service" and layer.ref:
            urls = [_loose(layer.ref.url)]
        else:
            urls = []
        for config in configs:
            own = [_loose(config.url), *([_loose(config.style)] if config.style else [])]
            if any(u == o or (config.type == "esri" and u.startswith(o + "/")) for u in urls for o in own):
                return config
    title = basemap.title.strip().lower()
    return next((c for c in configs if c.label.strip().lower() == title or c.id.lower() == title), None)
