"""
The image catalog in QGIS: footprints as geometries, the share of one
footprint another covers, opening an image of the catalog as a raster
layer (a COG through /vsicurl/, or a local path through `pathMappings`),
reading back what a layer opened from the catalog was, and the results as
a memory layer.

A layer opened from the catalog carries (as custom properties, saved in the
project) the catalog's image id `satobs/imageId` (used by the observations
and the imaging plan's follow-up), the catalog `satobs/catalog` and
`satobs/catalogUrl`, the feature `satobs/feature` (attributes and footprint,
for 「類似画像の検索」) and the satellite's view `satobs/sensorAzimuth` /
`satobs/sensorElevation` for other plugins; its metadata has the catalog's
rows. No token is put on a layer.

A record whose path is a folder has its images below it (`imagePatterns`,
core/catalog/images.py): each opens as its own layer with the record's id
(so the observations and AI results of the record are shared by its images;
「観測記録」 lists them as one image) and its path below the folder
`satobs/imageFile`, together in a group. A record opened as one image (its
file, or one of its folder's) is a layer named by the record's id.
"""
from __future__ import annotations

import json
import math
import os
import re
import threading
from dataclasses import dataclass, field
from typing import Any, Callable, Optional, Sequence

from qgis.core import (
    QgsCoordinateReferenceSystem, QgsCoordinateTransform, QgsDistanceArea, QgsFeature, QgsFields, QgsGeometry, QgsLayerMetadata, QgsMapLayer, QgsPointXY,
    QgsProject, QgsRasterLayer, QgsRectangle, QgsVectorLayer, QgsVectorLayerTemporalProperties, QgsWkbTypes,
)

from ..core.catalog.config import CatalogConfig
from ..core.catalog.images import FolderImages, find_images, patterns_for
from ..core.catalog.paths import PathError, PathMapping, PathRewrite, resolve_path, rewrites_of
from ..core.catalog.search import CatalogRecord, catalog_info, record_of, sensor_view_of
from ..core.catalog.watch import areas_of, areas_of_shape, layer_origin
from ..core.util import is_number, ms_of
from . import settings
from .obs_images import IMAGE_FILE, IMAGE_ID, image_layer
from .overviews_io import before_open
from .plan_layer import make_field, qdatetime

CATALOG = "satobs/catalog"
CATALOG_URL = "satobs/catalogUrl"
FEATURE = "satobs/feature"
SENSOR_AZIMUTH = "satobs/sensorAzimuth"
SENSOR_ELEVATION = "satobs/sensorElevation"
#: On a group of a record's images (add_record_layers): the record's id.
RECORD_GROUP = "satobs/recordGroup"

WGS84 = QgsCoordinateReferenceSystem("EPSG:4326")

#: Decimals of the coordinates sent for a layer's geometry (about 1 cm).
_DECIMALS = 7


def _xy(p) -> list[float]:
    return [round(p.x(), _DECIMALS), round(p.y(), _DECIMALS)]


def layer_area(layer: Optional[QgsMapLayer], selected_only: bool = False) -> Optional[dict[str, Any]]:
    """
    A vector layer's geometry (its chosen features only, when asked) in WGS 84
    as an Esri shape to search the catalog over, as it is rather than its
    extent: polygons joined into one polygon `{"rings": …}`, lines into one
    polyline `{"paths": …}`, points as a multipoint `{"points": …}`. None
    without a vector layer or a feature with a geometry.
    """
    if layer is None or layer.type() != QgsMapLayer.LayerType.VectorLayer or not layer.isSpatial():
        return None
    kind = layer.geometryType()
    if kind not in (QgsWkbTypes.GeometryType.PolygonGeometry, QgsWkbTypes.GeometryType.LineGeometry, QgsWkbTypes.GeometryType.PointGeometry):
        return None
    transform = QgsCoordinateTransform(layer.crs(), WGS84, QgsProject.instance())
    features = layer.selectedFeatures() if selected_only else layer.getFeatures()
    geometries: list[QgsGeometry] = []
    for feature in features:
        g = QgsGeometry(feature.geometry())
        if g.isNull() or g.isEmpty():
            continue
        if QgsWkbTypes.isCurvedType(g.wkbType()):
            g.convertToStraightSegment()
        if layer.crs() != WGS84:
            try:
                g.transform(transform)
            except Exception:  # noqa: BLE001 (a feature outside the CRS's area: left out)
                continue
        if kind == QgsWkbTypes.GeometryType.PolygonGeometry and not g.isGeosValid():
            g = g.makeValid()
        geometries.append(g)
    if not geometries:
        return None
    if kind == QgsWkbTypes.GeometryType.PointGeometry:
        points = [_xy(v) for g in geometries for v in g.vertices()]
        return {"points": points} if points else None
    joined = QgsGeometry.unaryUnion(geometries) if len(geometries) > 1 else geometries[0]
    if joined.isNull() or joined.isEmpty():
        return None
    if kind == QgsWkbTypes.GeometryType.LineGeometry:
        lines = joined.asMultiPolyline() if joined.isMultipart() else [joined.asPolyline()]
        paths = [[_xy(p) for p in line] for line in lines if len(line) >= 2]
        return {"paths": paths} if paths else None
    # A union can give a collection (polygons with stray lines): only its polygons count.
    parts = []
    for part in joined.asGeometryCollection() if joined.isMultipart() else [joined]:
        if part.type() == QgsWkbTypes.GeometryType.PolygonGeometry:
            parts += part.asMultiPolygon() if part.isMultipart() else [part.asPolygon()]
    polygons = [[[_xy(p) for p in ring] for ring in polygon] for polygon in parts if polygon]
    areas = areas_of([], polygons)
    return {"rings": areas[0]["rings"]} if areas else None


#: The watch area kind of a layer's geometry type.
_WATCH_KINDS = {QgsWkbTypes.GeometryType.PolygonGeometry: "polygon", QgsWkbTypes.GeometryType.LineGeometry: "lines",
                QgsWkbTypes.GeometryType.PointGeometry: "points"}


def layer_watch_areas(layer: Optional[QgsMapLayer], selected_only: bool = False) -> tuple[list[dict[str, Any]], Optional[dict[str, Any]]]:
    """
    A vector layer's geometry as it is now (see `layer_area`) as the areas of
    a watch, copied, and where they came from (core.catalog.watch.layer_origin);
    ([], None) without a feature with a geometry.
    """
    areas = areas_of_shape(layer_area(layer, selected_only))
    if not areas:
        return [], None
    count = int(layer.selectedFeatureCount() if selected_only else layer.featureCount())
    # A provider that cannot count (-1): no number.
    return areas, layer_origin(layer.name(), layer.id(), _WATCH_KINDS[layer.geometryType()], count if count >= 0 else None, selected_only)


def _signed_area(ring: Sequence[Sequence[float]]) -> float:
    return sum(ring[i][0] * ring[i + 1][1] - ring[i + 1][0] * ring[i][1] for i in range(len(ring) - 1)) / 2


def footprint(geometry: Optional[dict[str, Any]]) -> Optional[QgsGeometry]:
    """An Esri polygon (WGS 84; outer rings clockwise, holes anticlockwise) as a QGIS geometry; None without one."""
    if not isinstance(geometry, dict):
        return None
    rings = geometry.get("rings")
    if not rings:
        if all(is_number(geometry.get(k)) for k in ("xmin", "ymin", "xmax", "ymax")):
            x0, y0, x1, y1 = geometry["xmin"], geometry["ymin"], geometry["xmax"], geometry["ymax"]
            rings = [[[x0, y0], [x0, y1], [x1, y1], [x1, y0], [x0, y0]]]
        else:
            return None
    polygons: list[list[list[QgsPointXY]]] = []
    for ring in rings:
        if len(ring) < 3:
            continue
        points = [QgsPointXY(p[0], p[1]) for p in ring]
        if _signed_area(ring) <= 0 or not polygons:
            polygons.append([points])
        else:
            polygons[-1].append(points)
    if not polygons:
        return None
    g = QgsGeometry.fromMultiPolygonXY(polygons)
    if not g.isGeosValid():
        g = g.makeValid()
    return g


def _distance_area() -> QgsDistanceArea:
    d = QgsDistanceArea()
    d.setSourceCrs(WGS84, QgsProject.instance().transformContext())
    d.setEllipsoid("EPSG:7030")
    return d


def overlap_measure(reference: Optional[QgsGeometry]) -> Callable[[CatalogRecord], Optional[float]]:
    """The share of `reference` (geodesic area) each record's footprint covers; None when it cannot be measured."""
    if reference is None or reference.isEmpty():
        return lambda record: None
    measure = _distance_area()
    own = measure.measureArea(reference)

    def overlap_of(record: CatalogRecord) -> Optional[float]:
        g = footprint(record.geometry)
        if g is None or not own > 0:
            return None
        try:
            return min(1.0, measure.measureArea(reference.intersection(g)) / own)
        except Exception:  # noqa: BLE001 (a footprint GEOS cannot intersect is not measured)
            return None

    return overlap_of


class Cancelled(Exception):
    """The person did not choose the folder of a local path (or none of a folder's images)."""


#: Asked which of a record's images to open (the record, their paths below its folder, whether more were left out): the chosen ones, None to open none.
ChooseImages = Callable[[CatalogRecord, list[str], bool], Optional[list[str]]]


@dataclass(frozen=True)
class RecordImages:
    """
    Where a record's images are: `place` (a URL or a path here) is the image
    itself, or, with `files`, the folder they are in (`files` their paths
    below it, `more` when some were left out).
    """
    place: str
    is_url: bool
    files: list[str] = field(default_factory=list)
    more: bool = False
    #: The path the catalog gives (for messages).
    given: str = ""


def scan_folder(folder: str, patterns: Sequence[str], find: Callable[..., FolderImages] = find_images,
                wait_before_dialog: float = 0.3) -> FolderImages:
    """
    The images of a folder, looked for off the main thread (a share with many
    sub-folders takes long): QGIS keeps answering, and after a moment a
    progress window offers to cancel (Cancelled). The look itself is bounded
    (core/catalog/images.py, #76).
    """
    from qgis.PyQt.QtCore import QCoreApplication, QEventLoop, Qt
    from qgis.PyQt.QtWidgets import QProgressDialog

    result: dict = {}
    stop = threading.Event()

    def run() -> None:
        try:
            result["found"] = find(folder, patterns, cancelled=stop.is_set)
        except Exception as error:  # noqa: BLE001 (raised again on the main thread)
            result["error"] = error

    worker = threading.Thread(target=run, name="satobs-folder-scan", daemon=True)
    worker.start()
    worker.join(wait_before_dialog)
    dialog = None
    try:
        while worker.is_alive():
            if dialog is None:
                dialog = QProgressDialog(f"フォルダ「{folder}」の画像を探しています…", "取消", 0, 0)
                dialog.setWindowTitle("画像の場所")
                dialog.setWindowModality(Qt.WindowModality.ApplicationModal)
                dialog.setMinimumDuration(0)
                dialog.show()
            QCoreApplication.processEvents(QEventLoop.ProcessEventsFlag.AllEvents, 50)
            if dialog.wasCanceled():
                # The look stops at its next folder; nothing waits for it.
                stop.set()
                raise Cancelled()
            worker.join(0.05)
    finally:
        if dialog is not None:
            dialog.close()
            dialog.deleteLater()
    if "error" in result:
        raise result["error"]
    found = result["found"]
    if found.stopped == "取消":
        raise Cancelled()
    return found


def locate_record(record: CatalogRecord, catalog: CatalogConfig, mappings: Sequence[PathMapping | PathRewrite],
                  choose_folder: Callable[[PathMapping], Optional[str]], names=None) -> RecordImages:
    """
    Where a record's images are: a URL, a local path as it is, under a
    mapping's URL, or in the folder chosen for its prefix (asked through
    `choose_folder` the first time). A local folder's images are the files
    below it matching the catalog's `imagePatterns` for the record's sensor
    (or for another name of it in `names`, the satellite name tables).
    Before all that, where the rewrites of `pathMappings` (`pattern` / `to`)
    take the path: the first place that is there (a folder: with images)
    wins; when none is, the path goes on as written.
    Raises PathError with the reason, Cancelled when no folder was chosen.
    """
    source = record.source
    if source is None:
        raise PathError(f"{record.id or '画像'} に画像の場所なし")
    tried: list[str] = []
    for _, target in rewrites_of(mappings, source.value):
        if not os.path.exists(target):
            tried.append(f"{target} なし")
            continue
        try:
            return _local_images(record, catalog, target, target, names)
        except PathError:
            tried.append(f"{target} に画像なし")
    try:
        return _as_written(record, catalog, mappings, choose_folder, names)
    except PathError as e:
        if not tried:
            raise
        raise PathError(f"{e}（読み替え先も: {'、'.join(tried)}）") from None


def _as_written(record: CatalogRecord, catalog: CatalogConfig, mappings: Sequence[PathMapping | PathRewrite],
                choose_folder: Callable[[PathMapping], Optional[str]], names) -> RecordImages:
    """Where a record's images are by its path as the catalog writes it (see locate_record)."""
    source = record.source
    if source.kind == "url":
        url = source.value if not source.value.startswith("//") else "https:" + source.value
    else:
        resolved = resolve_path(source.value, mappings, settings.path_folder)
        if resolved.kind == "folder":
            folder = choose_folder(resolved.mapping)
            if not folder:
                raise Cancelled()
            settings.set_path_folder(resolved.mapping.prefix, folder)
            resolved = resolve_path(source.value, mappings, settings.path_folder)
        url = resolved.value
    is_url = url.lower().startswith(("http://", "https://"))
    if is_url:
        if re.split(r"[?#]", url)[0].endswith("/"):
            raise PathError(f"{record.id or url} の場所はフォルダの URL（一覧できず。フォルダはローカルパスか pathMappings の url なしで指定）")
        return RecordImages(url, True, given=source.value)
    return _local_images(record, catalog, url, source.value, names)


def _local_images(record: CatalogRecord, catalog: CatalogConfig, path: str, given: str, names) -> RecordImages:
    """The image at `path` here, or the images below it when it is a folder (PathError when it has none)."""
    if not os.path.isdir(path):
        return RecordImages(path, False, given=given)
    patterns = patterns_for(catalog.image_patterns, record.sensor, names)
    found = scan_folder(path, patterns)
    if not found.files:
        why = [f"{' / '.join(patterns)} に合うファイルなし。imagePatterns を確認"]
        if found.stopped:
            why.append(found.stopped)
        if found.unreadable:
            why.append(f"読めないフォルダ {found.unreadable} 件")
        raise PathError(f"{record.id or given} のフォルダ「{path}」に画像なし（{'・'.join(why)}）")
    return RecordImages(path, False, found.files, found.more, given=given)


def open_record(record: CatalogRecord, catalog: CatalogConfig, mappings: Sequence[PathMapping | PathRewrite],
                choose_folder: Callable[[PathMapping], Optional[str]],
                choose_images: Optional[ChooseImages] = None,
                problems: Optional[list[str]] = None, names=None) -> list[QgsRasterLayer]:
    """
    The images of a record as raster layers (not yet in the project; see
    add_record_layers): one for an image, or those of a folder's images
    chosen through `choose_images` (all without it). A URL is read through
    /vsicurl/ with the authentication chosen for its server. Raises
    PathError with the reason when none can be opened (the reasons of those
    that cannot, beside others that can, go to `problems`), Cancelled when no
    folder or image was chosen.
    """
    where = locate_record(record, catalog, mappings, choose_folder, names)
    time = ms_of(record.acquired)
    if not where.files:
        url = where.place
        name = record.id or url.replace("\\", "/").rstrip("/").rsplit("/", 1)[-1]
        if not where.is_url:
            before_open(url)
        layer = image_layer(url, name, image_id=record.id or None, time=time,
                            authcfg=settings.auth_for(url) if where.is_url else None)
        if not layer.isValid():
            raise PathError(f"{record.id or url} の画像を開けず（{url if where.is_url else where.given}）")
        remember(layer, record, catalog)
        return [layer]
    files = where.files
    if choose_images is not None and (len(files) > 1 or where.more):
        chosen = choose_images(record, list(files), where.more)
        if not chosen:
            raise Cancelled()
        files = [f for f in files if f in set(chosen)]
    layers, failed = [], []
    for rel in files:
        path = os.path.join(where.place, *rel.split("/"))
        # One image: named as the record (its image id, as 観測記録 calls it); several: by their paths, in the record's group.
        name = rel if len(files) > 1 or not record.id else record.id
        before_open(path)
        layer = image_layer(path, name, image_id=record.id or None, time=time)
        if not layer.isValid():
            failed.append(rel)
            continue
        remember(layer, record, catalog, rel)
        layers.append(layer)
    if failed:
        message = f"{record.id or where.given} の画像を開けず（{where.place}: {'、'.join(failed[:5])}{' ほか' if len(failed) > 5 else ''}）"
        if not layers:
            raise PathError(message)
        if problems is not None:
            problems.append(message)
    return layers


def add_record_layers(layers: Sequence[QgsMapLayer], record: CatalogRecord, project: Optional[QgsProject] = None) -> None:
    """Adds a record's layers to the project: one as it is, several in a group of the record's id at the top of the layer tree."""
    project = project or QgsProject.instance()
    if len(layers) <= 1:
        for layer in layers:
            project.addMapLayer(layer)
        return
    group = project.layerTreeRoot().insertGroup(0, record.id or layers[0].name())
    group.setCustomProperty(RECORD_GROUP, record.id or "")
    for layer in layers:
        project.addMapLayer(layer, False)
        group.addLayer(layer)


def layers_extent(layers: Sequence[QgsMapLayer], crs: QgsCoordinateReferenceSystem,
                  project: Optional[QgsProject] = None) -> Optional[QgsRectangle]:
    """
    The box around the layers' extents in `crs` (the map's, for 「開いた画像へ
    移動」): each from its own CRS (one without a CRS as it is, the way QGIS
    draws it). None when none has an extent that can be shown there.
    """
    project = project or QgsProject.instance()
    box: Optional[QgsRectangle] = None
    for layer in layers:
        extent = QgsRectangle(layer.extent())
        if extent.isNull() or not extent.isFinite():
            continue
        if layer.crs().isValid() and crs.isValid() and layer.crs() != crs:
            try:
                extent = QgsCoordinateTransform(layer.crs(), crs, project).transformBoundingBox(extent)
            except Exception:  # noqa: BLE001 (QgsCsException: outside what the map's CRS can show)
                continue
            if extent.isNull() or not extent.isFinite():
                continue
        if box is None:
            box = extent
        else:
            box.combineExtentWith(extent)
    return box


def close_layers(layer_ids: Sequence[str], project: Optional[QgsProject] = None) -> int:
    """
    Takes images opened from the catalog out of the project (「前の画像を
    閉じる」): the layers still there, and the groups add_record_layers made
    for them once empty. How many layers went.
    """
    project = project or QgsProject.instance()
    root = project.layerTreeRoot()
    there = [i for i in dict.fromkeys(layer_ids) if project.mapLayer(i) is not None]
    groups = []
    for layer_id in there:
        node = root.findLayer(layer_id)
        parent = node.parent() if node is not None else None
        if parent is not None and parent is not root and parent.customProperty(RECORD_GROUP) is not None and parent not in groups:
            groups.append(parent)
    if there:
        project.removeMapLayers(there)
    for group in groups:
        if not group.children() and group.parent() is not None:
            group.parent().removeChildNode(group)
    return len(there)


def remember(layer: QgsMapLayer, record: CatalogRecord, catalog: CatalogConfig, file: Optional[str] = None) -> None:
    """Puts what the catalog says of an image on its layer (custom properties and metadata; `file` the image's path below the record's folder)."""
    if record.id:
        layer.setCustomProperty(IMAGE_ID, record.id)
    if file:
        layer.setCustomProperty(IMAGE_FILE, file)
    layer.setCustomProperty(CATALOG, catalog.label)
    layer.setCustomProperty(CATALOG_URL, catalog.url)
    layer.setCustomProperty(FEATURE, json.dumps(record.feature(), ensure_ascii=False))
    view = sensor_view_of(record, catalog)
    if view:
        layer.setCustomProperty(SENSOR_AZIMUTH, view["azimuth"])
        layer.setCustomProperty(SENSOR_ELEVATION, view["elevation"])
    metadata = layer.metadata() if hasattr(layer, "metadata") else QgsLayerMetadata()
    metadata.setTitle(f"{record.id} {file}".strip() if file else record.id or layer.name())
    metadata.setAbstract("\n".join(f"{k}: {v}" for k, v in catalog_info(record, catalog)))
    metadata.addKeywords("画像カタログ", [catalog.label])
    layer.setMetadata(metadata)


def record_of_layer(layer: QgsMapLayer, catalogs: Sequence[CatalogConfig]) -> Optional[tuple[CatalogRecord, CatalogConfig]]:
    """The catalog record a layer was opened from, with its catalog (the settings file's of the same URL); None for other layers."""
    text = layer.customProperty(FEATURE)
    url = layer.customProperty(CATALOG_URL)
    if not text or not url:
        return None
    catalog = next((c for c in catalogs if c.url == url), None)
    if catalog is None:
        return None
    try:
        feature = json.loads(text)
    except (TypeError, ValueError):
        return None
    return record_of(feature, catalog.fields), catalog


def _kind_of(values: list[Any], field_type: Optional[str]) -> str:
    if field_type in ("esriFieldTypeDate", "esriFieldTypeTimestampOffset"):
        return "date"
    present = [v for v in values if v is not None and v != ""]
    if present and all(isinstance(v, bool) or (isinstance(v, int) and abs(v) < 2 ** 31) for v in present):
        return "integer"
    if present and all(isinstance(v, (int, float)) and not isinstance(v, bool) for v in present):
        return "double"
    return "string"


def results_layer(records: Sequence[CatalogRecord], catalog: CatalogConfig, types: dict[str, str], name: str) -> QgsVectorLayer:
    """
    The listed images as a memory layer (WGS 84): their footprints with every
    attribute, the acquisition time driving the temporal controller.
    """
    layer = QgsVectorLayer("MultiPolygon?crs=EPSG:4326", name, "memory")
    names: list[str] = []
    for r in records:
        for k in r.attributes:
            if k not in names:
                names.append(k)
    kinds = {n: _kind_of([r.attributes.get(n) for r in records], types.get(n)) for n in names}
    acquired = catalog.fields.get("acquired")
    if acquired and acquired in kinds and kinds[acquired] != "date":
        # Text or numbers of times: kept as they are, with the time beside them for the controller.
        names.append("acquired_time")
        kinds["acquired_time"] = "date"
    fields = QgsFields()
    for n in names:
        fields.append(make_field(n, kinds[n]))
    provider = layer.dataProvider()
    provider.addAttributes(fields.toList())
    layer.updateFields()
    features = []
    for r in records:
        feature = QgsFeature(layer.fields())
        g = footprint(r.geometry)
        if g is not None:
            feature.setGeometry(g)
        values = []
        for n in names:
            v = r.acquired if n == "acquired_time" else r.attributes.get(n)
            if kinds[n] == "date":
                t = ms_of(v)
                values.append(qdatetime(t) if t is not None else None)
            elif kinds[n] == "string":
                values.append(None if v is None else str(v))
            else:
                values.append(None if v is None or (isinstance(v, float) and math.isnan(v)) else v)
        feature.setAttributes(values)
        features.append(feature)
    provider.addFeatures(features)
    layer.updateExtents()
    time_field = "acquired_time" if "acquired_time" in kinds else acquired if acquired in kinds else None
    if time_field:
        temporal = layer.temporalProperties()
        temporal.setIsActive(True)
        temporal.setMode(QgsVectorLayerTemporalProperties.TemporalMode.ModeFeatureDateTimeInstantFromField)
        temporal.setStartField(time_field)
    return layer
