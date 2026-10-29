"""
ArcGIS Web Maps in QGIS (the viewer's webmap-dialog.ts without its dialog):
a web map read (core/webmap.py) opened as QGIS layers in a group, with the
web map's visibility, opacity, scale range, filter, renderer, labels and
attribute labels, its base map, view and bookmarks; and the project written
back as a web map, to a .json file or to a portal (a new item, or over the
item opened).

Layers are made in a task (service information is read without holding up
the map) and added to the project afterwards. What a layer of the web map
says that QGIS cannot keep (a map service's per-layer filters, an image
service's rendering rule) is kept on the layer and written back unchanged.

Sign-in: the authentication configuration chosen for a service in the
plugin settings, else the portal's one for the portal's own host (and, from
ArcGIS Online, for its hosted services), never for other hosts.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Callable, Optional

from qgis.core import (
    QgsArcGisRestContext, QgsArcGisRestUtils, QgsBookmark, QgsCoordinateReferenceSystem, QgsCoordinateTransform, QgsDataSourceUri,
    QgsFeature, QgsField, QgsFields, QgsGeometry, QgsLayerTreeGroup, QgsMapLayer, QgsPalLayerSettings, QgsProject, QgsRasterLayer, QgsRectangle,
    QgsReferencedRectangle, QgsVectorLayer, QgsVectorTileLayer, QgsWkbTypes,
)
from qgis.PyQt.QtCore import QCoreApplication, QDate, QDateTime, Qt, QTime, QUrl, QUrlQuery

from ..core.basemaps import BaseMapConfig, raster_source
from ..core.esri import EsriClient, EsriError
from ..core.startup_layers import StartupLayer, layer_source
from ..core.webmap import (
    MAX_COLLECTION_FEATURES, CollectionPart, SaveLayer, SaveResult, ServiceRef, WebMapExtent, WebMapItemRef, WebMapLayer,
    WebMapPlan, build_web_map, geometry_kind, item_data_url, item_url, label_field_of, match_base_map, service_root, template_of,
    token_allowed, user_content_url,
)
from . import settings
from .basemap_layer import PROPERTY as BASE_MAP_PROPERTY, current_base_map, open_base_map
from .network import qgis_transport
from .startup_layers import make_layer
from .webmap_style import esri_labeling, esri_renderer

#: A layer of the web map QGIS cannot make again (tiles, vector tiles): its JSON, written back as it was.
KEPT = "satobs/webmap"
#: An Esri service layer's settings QGIS does not use (per-layer filters, rendering rule…), written back.
SETTINGS = "satobs/webmap_settings"
#: The renderer the web map gave a feature layer, written back when QGIS's style cannot be written.
RENDERER = "satobs/webmap_renderer"
#: On the layers of a web map's base map: that base map's JSON.
BASEMAP = "satobs/webmap_basemap"

WEB_MERCATOR = QgsCoordinateReferenceSystem("EPSG:3857")
WGS84 = QgsCoordinateReferenceSystem("EPSG:4326")


def crs_of_wkid(wkid: int) -> QgsCoordinateReferenceSystem:
    """A web map's spatial reference (102100 and the other Esri ids too)."""
    if wkid in (102100, 102113, 900913):
        return QgsCoordinateReferenceSystem("EPSG:3857")
    crs = QgsArcGisRestUtils.convertSpatialReference({"wkid": wkid})
    return crs if crs.isValid() else QgsCoordinateReferenceSystem(f"EPSG:{wkid}")


def auth_with_portal(portal: Optional[str], portal_auth: Optional[str]) -> Callable[[str], Optional[str]]:
    """The authcfg for a request: the service's own (plugin settings), else the portal's where it may go."""
    def auth(url: str) -> Optional[str]:
        own = settings.auth_for(url)
        if own:
            return own
        return portal_auth if portal and portal_auth and token_allowed(url, portal) else None

    return auth


# ---- Portals ----

def fetch_web_map(ref: WebMapItemRef, auth: Callable[[str], Optional[str]]) -> tuple[dict, dict]:
    """The item and its data (the web map's JSON), in a task."""
    client = EsriClient(qgis_transport(auth))
    item = client.ask(item_url(ref))
    if item.get("type") != "Web Map":
        raise EsriError(f"Web マップ以外のアイテム（種類: {item.get('type') or '不明'}）")
    return item, client.ask(item_data_url(ref))


def portal_user(portal: str, auth: Callable[[str], Optional[str]]) -> str:
    """The user the portal's sign-in is, in a task."""
    me = EsriClient(qgis_transport(auth)).ask(f"{portal}/sharing/rest/community/self")
    user = me.get("username") or (me.get("user") or {}).get("username")
    if not isinstance(user, str) or not user:
        raise EsriError("サインインのユーザーが不明（認証設定を選択するか、ユーザー名を入力）")
    return user


def save_to_portal(portal: str, auth: Callable[[str], Optional[str]], result: SaveResult, title: str, tags: str, user: str = "",
                   folder: str = "", update: Optional[str] = None, extent: Optional[tuple] = None) -> tuple[str, str]:
    """Saves the web map as a new item (or over the item `update`), in a task; (item id, user)."""
    client = EsriClient(qgis_transport(auth))
    user = user or portal_user(portal, auth)
    folder_id = None
    if folder:
        content = client.ask(user_content_url(portal, user))
        folder_id = next((f.get("id") for f in content.get("folders") or [] if folder in (f.get("title"), f.get("id"))), None)
        if not folder_id:
            raise EsriError(f"フォルダー「{folder}」なし")
    params = {"title": title, "tags": tags or "satellite_observation", "text": json.dumps(result.json, ensure_ascii=False)}
    if extent:
        params["extent"] = ",".join(str(round(v, 6)) for v in extent)
    if update:
        answer = client.ask(f"{user_content_url(portal, user)}/items/{update}/update", params)
        if answer.get("success") is False:
            raise EsriError("ポータルが更新を拒否")
        return update, user
    answer = client.ask(f"{user_content_url(portal, user, folder_id)}/addItem",
                        {**params, "type": "Web Map", "typeKeywords": "ArcGIS Online,Explorer Web Map,Map,Online Map,Web Map"})
    item = answer.get("id")
    if answer.get("success") is False or not isinstance(item, str) or not re.fullmatch(r"[0-9a-f]{32}", item, re.IGNORECASE):
        raise EsriError("ポータルでのアイテム作成に失敗")
    return item, user


# ---- Opening ----

@dataclass
class Opened:
    """A layer made from the web map, with how it is shown."""
    layer: QgsMapLayer
    visible: bool
    basemap: bool = False


@dataclass
class OpenedWebMap:
    title: str
    #: Bottom first.
    layers: list = field(default_factory=list)
    #: (title, why) of what could not be opened.
    problems: list = field(default_factory=list)
    #: (title, note) of what QGIS shows differently.
    notes: list = field(default_factory=list)
    #: The settings file's base map the web map's is (its id), shown with the 「背景地図」 switch.
    basemap_id: Optional[str] = None
    basemap_title: str = ""
    extent: Optional[WebMapExtent] = None
    bookmarks: list = field(default_factory=list)


def open_plan(plan: WebMapPlan, title: str, base_maps: list, auth: Callable[[str], Optional[str]],
              extent: Optional[WebMapExtent] = None, check: Optional[Callable[[], None]] = None) -> OpenedWebMap:
    """The layers of a web map (in the calling thread, moved to the main thread), and what could not be opened."""
    out = OpenedWebMap(title, problems=list(plan.unsupported), extent=plan.extent or extent, bookmarks=plan.bookmarks)
    match = match_base_map(plan.basemap, base_maps)
    wanted = [(x, False) for x in plan.layers]
    if match:
        out.basemap_id, out.basemap_title = match.id, match.label
        extra = [x for x in plan.basemap.layers if x.reference]
    else:
        extra = plan.basemap.layers
        if extra:
            out.basemap_title = plan.basemap.title
    wanted = [(x, True) for x in extra if not x.reference] + wanted + [(x, True) for x in extra if x.reference]
    main = QCoreApplication.instance().thread()
    for spec, basemap in wanted:
        if check:
            check()
        name = f"背景: {spec.title}" if basemap else spec.title
        try:
            made = _make(spec, name, auth, out.notes)
        except (ValueError, EsriError) as error:
            out.problems.append((name, str(error)))
            continue
        for layer in made:
            _apply_common(layer, spec)
            if basemap and plan.basemap.source is not None:
                layer.setCustomProperty(BASEMAP, json.dumps(plan.basemap.source, ensure_ascii=False))
            layer.moveToThread(main)
            out.layers.append(Opened(layer, spec.visible, basemap))
    return out


def _invalid(layer: QgsMapLayer) -> str:
    error = layer.error().summary() if layer.error() and not layer.error().isEmpty() else ""
    return "開けず" + (f"：{error}" if error else "")


def _make(spec: WebMapLayer, name: str, auth: Callable[[str], Optional[str]], notes: list) -> list:
    if spec.type == "collection":
        return [_collection_layer(part, f"{name}: {part.name}" if len(spec.parts) > 1 and part.name else name) for part in spec.parts]
    if spec.type == "tiles":
        url = spec.urls[0]
        base = BaseMapConfig("webmap", name, "xyz", url, max_zoom=22)
        layer = QgsRasterLayer(raster_source(base, url, auth(url)), name, "wms")
        if not layer.isValid():
            raise ValueError(_invalid(layer))
        if spec.attributions:
            layer.setAttribution(spec.attributions)
        layer.setCustomProperty(KEPT, json.dumps(spec.source, ensure_ascii=False))
        return [layer]
    if spec.type == "vector-tiles":
        esri = bool(re.search(r"/VectorTileServer/?$", spec.url, re.IGNORECASE))
        base = BaseMapConfig("webmap", name, "esri" if esri else "style", spec.url if esri else spec.style or spec.url, max_zoom=22,
                             style=spec.style if esri else "")
        layer, problems = open_base_map(base, "")
        layer.removeCustomProperty(BASE_MAP_PROPERTY)
        notes += [(name, p) for p in problems]
        layer.setCustomProperty(KEPT, json.dumps(spec.source, ensure_ascii=False))
        return [layer]
    ref = spec.ref
    authcfg = auth(ref.url)
    if ref.kind == "esri":
        return [_esri_layer(spec, name, authcfg, notes)]
    source = layer_source(StartupLayer(ref.kind, ref.url, ref.layer, matrix_set=ref.matrix_set, format=ref.format), "", authcfg)
    layer = make_layer(source)
    if not layer.isValid():
        raise ValueError(_invalid(layer))
    layer.setName(name)
    notes += [(name, n) for n in source.notes]
    return [layer]


def _esri_layer(spec: WebMapLayer, name: str, authcfg: Optional[str], notes: list) -> QgsMapLayer:
    ref = spec.ref
    auth = f" authcfg='{authcfg}'" if authcfg else ""
    s = ref.settings
    if ref.layer in ("map", "image"):
        shown = ",".join(str(i) for i in s.get("layers") or [])
        layer = QgsRasterLayer(f"crs='EPSG:3857' format='PNG32' layer='{shown}' url='{ref.url}'{auth}", name, "arcgismapserver")
        if not layer.isValid():
            raise ValueError(_invalid(layer))
        unused = {k: v for k, v in s.items() if k != "layers"}
        if unused:
            layer.setCustomProperty(SETTINGS, json.dumps(unused, ensure_ascii=False))
            what = {"layerDefs": "レイヤーごとの定義式", "renderingRule": "表示ルール（renderingRule）", "where": "モザイクの条件",
                    "method": "モザイクの方法", "sortField": "モザイクの並び", "bandIds": "バンドの組合せ"}
            notes.append((name, "・".join(dict.fromkeys(what.get(k, k) for k in unused)) + "は QGIS の ArcGIS プロバイダーで未対応（保存時はそのまま書き戻し）"))
        return layer
    layer = QgsVectorLayer(f"crs='EPSG:4326' url='{ref.url}'{auth}", name, "arcgisfeatureserver")
    if not layer.isValid():
        raise ValueError(_invalid(layer))
    if s.get("where") and not layer.setSubsetString(s["where"]):
        notes.append((name, f"定義式を適用できず（{s['where']}）"))
    if spec.renderer:
        renderer = QgsArcGisRestUtils.convertRenderer(spec.renderer)
        if renderer is not None:
            layer.setRenderer(renderer)
        else:
            notes.append((name, "Web マップのシンボルは QGIS で再現できず、サービスの既定で表示"))
        layer.setCustomProperty(RENDERER, json.dumps(spec.renderer, ensure_ascii=False))
    if spec.labeling:
        _labels(layer, spec.labeling)
    elif spec.labels_off:
        layer.setLabelsEnabled(False)
    _alias(layer, spec.aliases)
    return layer


def _labels(layer: QgsVectorLayer, labeling_info: list) -> None:
    """The web map's label classes, as QGIS reads them; an Arcade `$feature.NAME` QGIS leaves empty labels by that attribute."""
    labeling = QgsArcGisRestUtils.convertLabeling(labeling_info)
    if labeling is None:
        return
    name = label_field_of(labeling_info)
    for rule in labeling.rootRule().children():
        label = rule.settings()
        if label is not None and not label.fieldName and name:
            label.fieldName = name
            label.isExpression = False
            rule.setSettings(QgsPalLayerSettings(label))
    layer.setLabeling(labeling)
    layer.setLabelsEnabled(True)


def _alias(layer: QgsVectorLayer, aliases: dict) -> None:
    for field_name, label in aliases.items():
        index = layer.fields().indexOf(field_name)
        if index >= 0:
            layer.setFieldAlias(index, label)


def _collection_layer(part: CollectionPart, name: str) -> QgsVectorLayer:
    """A feature collection's layer as a memory layer (in its own spatial reference)."""
    crs = crs_of_wkid(part.wkid)
    kind = geometry_kind(part.geometry_type)
    wkb = {"point": "MultiPoint" if "multipoint" in part.geometry_type.lower() else "Point", "line": "MultiLineString", "polygon": "MultiPolygon"}[kind]
    layer = QgsVectorLayer(f"{wkb}?crs={crs.authid() or 'EPSG:3857'}", name, "memory")
    fields = QgsFields()
    names = []
    for f in part.fields:
        if f["type"] in ("esriFieldTypeOID", "esriFieldTypeGeometry", "esriFieldTypeBlob", "esriFieldTypeRaster"):
            continue
        variant = QgsArcGisRestUtils.convertFieldType(f["type"])
        fields.append(QgsField(f["name"], variant, len=int(f.get("length") or 0)))
        names.append(f["name"])
    layer.dataProvider().addAttributes(fields.toList())
    layer.updateFields()
    for f in part.fields:
        index = layer.fields().indexOf(f["name"])
        if index >= 0 and f.get("alias") and f["alias"] != f["name"]:
            layer.setFieldAlias(index, f["alias"])
    _alias(layer, part.aliases)
    features = []
    for item in part.features[:50_000]:
        geometry = item.get("geometry")
        made = QgsArcGisRestUtils.convertGeometry(geometry, part.geometry_type, False, False)[0] if isinstance(geometry, dict) else None
        feature = QgsFeature(layer.fields())
        if made is not None:
            g = QgsGeometry(made)
            if wkb.startswith("Multi"):
                g.convertToMultiType()
            feature.setGeometry(g)
        attributes = item.get("attributes") or {}
        for n in names:
            value = attributes.get(n)
            if value is not None and layer.fields().field(n).type() == QgsArcGisRestUtils.convertFieldType("esriFieldTypeDate"):
                value = QgsArcGisRestUtils.convertDateTime(value)
            feature.setAttribute(n, value)
        features.append(feature)
    layer.dataProvider().addFeatures(features)
    layer.updateExtents()
    if part.renderer:
        renderer = QgsArcGisRestUtils.convertRenderer(part.renderer)
        if renderer is not None:
            layer.setRenderer(renderer)
        layer.setCustomProperty(RENDERER, json.dumps(part.renderer, ensure_ascii=False))
    if part.labeling:
        _labels(layer, part.labeling)
    return layer


def _apply_common(layer: QgsMapLayer, spec: WebMapLayer) -> None:
    layer.setOpacity(spec.opacity)
    if spec.min_scale or spec.max_scale:
        # Esri's scales of Web Mercator are QGIS's in a Web Mercator project (96 dpi).
        layer.setScaleBasedVisibility(True)
        layer.setMinimumScale(spec.min_scale or 0)
        layer.setMaximumScale(spec.max_scale or 0)


def add_opened(opened: OpenedWebMap, canvas, project: Optional[QgsProject] = None) -> QgsLayerTreeGroup:
    """Adds the layers made to the project in a group on top (the base map's at its bottom), then shows the view and adds the bookmarks."""
    project = project or QgsProject.instance()
    if not [x for x in project.mapLayers().values() if not x.customProperty(BASE_MAP_PROPERTY)] and opened.extent:
        # An empty project takes the web map's spatial reference, so its scales are the web map's.
        project.setCrs(crs_of_wkid(opened.extent.wkid))
    root = project.layerTreeRoot()
    group = root.insertGroup(0, opened.title)
    for item in opened.layers:
        project.addMapLayer(item.layer, False)
        node = group.insertLayer(0, item.layer)
        node.setItemVisibilityChecked(item.visible)
    if opened.extent and canvas is not None:
        e = opened.extent
        try:
            rect = QgsCoordinateTransform(crs_of_wkid(e.wkid), project.crs(), project).transformBoundingBox(QgsRectangle(e.xmin, e.ymin, e.xmax, e.ymax))
            canvas.setExtent(rect)
            canvas.refresh()
        except Exception:  # noqa: BLE001 (a view that cannot be shown leaves the map where it is)
            pass
    manager = project.bookmarkManager()
    for name, e in opened.bookmarks:
        bookmark = QgsBookmark()
        bookmark.setName(name)
        bookmark.setGroup(opened.title)
        bookmark.setExtent(QgsReferencedRectangle(QgsRectangle(e.xmin, e.ymin, e.xmax, e.ymax), crs_of_wkid(e.wkid)))
        manager.addBookmark(bookmark)
    return group


# ---- Saving ----

def _query(source: str) -> dict:
    query = QUrlQuery(source)
    return {k: v for k, v in query.queryItems(QUrl.ComponentFormattingOption.FullyDecoded)}


def _transform(crs: QgsCoordinateReferenceSystem, project: QgsProject) -> QgsCoordinateTransform:
    return QgsCoordinateTransform(crs, WEB_MERCATOR, project)


def save_layer(layer: QgsMapLayer, visible: bool, project: QgsProject) -> SaveLayer:
    """One layer of the project as what a web map can hold, or why it cannot."""
    common = {"title": layer.name(), "visible": visible, "opacity": layer.opacity()}
    if layer.hasScaleBasedVisibility():
        common["min_scale"] = layer.minimumScale() or None
        common["max_scale"] = layer.maximumScale() or None
    kept = layer.customProperty(KEPT)
    if kept:
        try:
            return SaveLayer(type="json", json=json.loads(kept), **common)
        except ValueError:
            pass
    provider = layer.providerType()
    kept_settings = {}
    try:
        kept_settings = json.loads(layer.customProperty(SETTINGS) or "{}")
    except ValueError:
        pass
    if provider == "arcgismapserver":
        uri = QgsDataSourceUri(layer.source())
        url = uri.param("url").rstrip("/")
        if url.lower().endswith("/imageserver"):
            return SaveLayer(type="ref", ref=ServiceRef("esri", url, "image", kept_settings), **common)
        shown = [int(x) for x in re.findall(r"\d+", uri.param("layer") or "")]
        defs = {k: v for k, v in kept_settings.items() if k == "layerDefs"}
        return SaveLayer(type="ref", ref=ServiceRef("esri", re.sub(r"/\d+$", "", url), "map", {**({"layers": shown} if shown else {}), **defs}), **common)
    if provider == "arcgisfeatureserver":
        uri = QgsDataSourceUri(layer.source())
        url = uri.param("url").rstrip("/")
        where = layer.subsetString() or uri.sql()
        kind = geometry_kind(QgsWkbTypes.displayString(layer.wkbType()))
        renderer = esri_renderer(layer, kind) or _kept_renderer(layer)
        return SaveLayer(type="ref", ref=ServiceRef("esri", url, url.rsplit("/", 1)[-1], {"where": where} if where else {}),
                         renderer=renderer, kind=kind, labeling=esri_labeling(layer), **common)
    if provider == "wms":
        params = _query(layer.source())
        url = params.get("url", "")
        if params.get("type") == "xyz":
            return SaveLayer(type="json", json={"layerType": "WebTiledLayer", "templateUrl": template_of(url),
                                                **({"copyright": layer.attribution()} if layer.attribution() else {})}, **common)
        if params.get("tileMatrixSet"):
            return SaveLayer(type="ref", ref=ServiceRef("wmts", service_root(url), params.get("layers", ""), matrix_set=params["tileMatrixSet"],
                                                        format=params.get("format", "")), **common)
        names = [n for n in params.get("layers", "").split(",") if n]
        if not url or not names:
            return SaveLayer(type="none", reason="WMS のレイヤー名なし", **common)
        return SaveLayer(type="ref", ref=ServiceRef("wms", url, ",".join(names)), **common)
    if provider == "WFS":
        uri = QgsDataSourceUri(layer.source())
        return SaveLayer(type="ref", ref=ServiceRef("wfs", uri.param("url"), uri.param("typename")), **common)
    if isinstance(layer, QgsVectorTileLayer):
        params = _query(layer.source())
        if (params.get("type") == "arcgis" or params.get("serviceType") == "arcgis") and params.get("url"):
            style = params.get("styleUrl") or f"{params['url'].rstrip('/')}/resources/styles/root.json"
            return SaveLayer(type="json", json={"layerType": "VectorTileLayer", "styleUrl": style}, **common)
        if params.get("styleUrl"):
            return SaveLayer(type="json", json={"layerType": "VectorTileLayer", "styleUrl": params["styleUrl"]}, **common)
        return SaveLayer(type="none", reason="スタイルのないベクタータイルは Web マップへの書き込み不可", **common)
    if isinstance(layer, QgsVectorLayer):
        if not layer.isSpatial():
            return SaveLayer(type="none", reason="表（ジオメトリなし）は書き込み不可", **common)
        return _features_layer(layer, project, common)
    if isinstance(layer, QgsRasterLayer):
        return SaveLayer(type="none", reason="画像（ファイル）は Web マップへの書き込み不可", **common)
    return SaveLayer(type="none", reason="Web マップへの書き込み不可の種類", **common)


def _kept_renderer(layer: QgsMapLayer) -> Optional[dict]:
    try:
        value = json.loads(layer.customProperty(RENDERER) or "null")
    except ValueError:
        return None
    return value if isinstance(value, dict) else None


def _attribute(value):
    """An attribute as a web map holds it: dates as ms since 1970 (UTC), other objects as text."""
    if value is None or (hasattr(value, "isNull") and value.isNull()):
        return None
    if isinstance(value, QDateTime):
        return value.toMSecsSinceEpoch()
    if isinstance(value, QDate):
        return QDateTime(value, QTime(0, 0), Qt.TimeSpec.UTC).toMSecsSinceEpoch()
    if isinstance(value, (bool, int, float, str)):
        return value
    return str(value)


def _features_layer(layer: QgsVectorLayer, project: QgsProject, common: dict) -> SaveLayer:
    """A vector layer without a service (file, memory) as a feature collection's features, in Web Mercator."""
    count = layer.featureCount()
    if count > MAX_COLLECTION_FEATURES:
        return SaveLayer(type="none", reason=f"地物が多すぎ（{count:,} 件。フィーチャーコレクションは {MAX_COLLECTION_FEATURES:,} 件まで）", **common)
    transform = _transform(layer.crs(), project)
    context = QgsArcGisRestContext()
    kind = geometry_kind(QgsWkbTypes.displayString(layer.wkbType()))
    fields = [f for f in layer.fields() if f.name().lower() not in ("__objectid",)]
    esri_fields = []
    for f in fields:
        definition = QgsArcGisRestUtils.fieldDefinitionToJson(f)
        if definition.get("type") == "esriFieldTypeOID":
            continue
        if f.alias():
            definition["alias"] = f.alias()
        esri_fields.append(definition)
    names = [d["name"] for d in esri_fields]
    features = []
    for feature in layer.getFeatures():
        geometry = feature.geometry()
        if geometry.isNull() or geometry.isEmpty():
            continue
        geometry = QgsGeometry(geometry)
        geometry.transform(transform)
        own = geometry_kind(QgsWkbTypes.displayString(geometry.wkbType()))
        g = QgsArcGisRestUtils.geometryToJson(geometry, context)
        if not g:
            continue
        features.append((own, g, {n: _attribute(feature[n]) for n in names}))
    return SaveLayer(type="features", features=features, fields=esri_fields, renderer=esri_renderer(layer, kind) or _kept_renderer(layer),
                     kind=kind, labeling=esri_labeling(layer), **common)


def project_web_map(base_maps: list, canvas=None, project: Optional[QgsProject] = None) -> SaveResult:
    """The project as a web map: its layers (in the layer panel's order), the view, the base map and the bookmarks."""
    project = project or QgsProject.instance()
    root = project.layerTreeRoot()
    layers = []
    kept_basemap = None
    for layer in reversed(root.layerOrder()):
        if layer.customProperty(BASE_MAP_PROPERTY):
            continue
        if layer.customProperty(BASEMAP):
            if kept_basemap is None:
                try:
                    kept_basemap = json.loads(layer.customProperty(BASEMAP))
                except ValueError:
                    pass
            continue
        node = root.findLayer(layer.id())
        layers.append(save_layer(layer, bool(node and node.isVisible()), project))
    config = next((b for b in base_maps if b.id == current_base_map(project)), None)
    extent = None
    if canvas is not None:
        try:
            rect = QgsCoordinateTransform(canvas.mapSettings().destinationCrs(), WEB_MERCATOR, project).transformBoundingBox(canvas.extent())
            extent = (rect.xMinimum(), rect.yMinimum(), rect.xMaximum(), rect.yMaximum())
        except Exception:  # noqa: BLE001
            extent = None
    bookmarks = []
    for bookmark in project.bookmarkManager().bookmarks():
        e = bookmark.extent()
        try:
            rect = QgsCoordinateTransform(e.crs(), WEB_MERCATOR, project).transformBoundingBox(QgsRectangle(e))
        except Exception:  # noqa: BLE001
            continue
        bookmarks.append((bookmark.name(), (rect.xMinimum(), rect.yMinimum(), rect.xMaximum(), rect.yMaximum())))
    return build_web_map(layers, extent, config, kept_basemap, bookmarks)


def view_in_degrees(canvas, project: Optional[QgsProject] = None) -> Optional[tuple]:
    """The view in degrees, for the item's extent."""
    project = project or QgsProject.instance()
    try:
        rect = QgsCoordinateTransform(canvas.mapSettings().destinationCrs(), WGS84, project).transformBoundingBox(canvas.extent())
    except Exception:  # noqa: BLE001
        return None
    return (rect.xMinimum(), rect.yMinimum(), rect.xMaximum(), rect.yMaximum())

