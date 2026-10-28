"""
External AI results in QGIS: one image's results read from a source
(`aiResultSources`), their Esri geometries (points, multipoints, lines or
polygons) as QGIS geometries, drawn as a memory layer per source, and the
point an observation is recorded at (a polygon's point inside it, with its
long side as the length).
"""
from __future__ import annotations

from typing import Any, Callable, Optional, Sequence

from qgis.core import (
    QgsCoordinateReferenceSystem, QgsCoordinateTransform, QgsFeature, QgsFields, QgsGeometry, QgsLineSymbol, QgsMarkerSymbol,
    QgsPalLayerSettings, QgsPointXY, QgsProject, QgsSingleSymbolRenderer, QgsFillSymbol, QgsTextFormat, QgsVectorLayer,
    QgsVectorLayerSimpleLabeling, QgsWkbTypes,
)
from qgis.PyQt.QtGui import QColor

from ..core.ai_results import AiResult, AiResultSource, image_where, kind_for, result_of
from ..core.esri import EsriClient
from ..core.util import is_number
from .catalog_io import footprint
from .plan_layer import make_field, qdatetime

#: On the layers this makes: the image id they show (so they are replaced when another image is shown).
RESULTS_OF = "satobs/aiResultsOf"
#: Each feature's row in the dock's list.
ROW = "row"
COLOR = "#f97316"

WGS84 = QgsCoordinateReferenceSystem("EPSG:4326")


def fetch(client: EsriClient, source: AiResultSource, image_id: str, token: Optional[str] = None,
          cancelled: Optional[Callable[[], bool]] = None) -> list[AiResult]:
    """The results of one image in a source (use inside a task)."""
    info = client.layer(source.url, token)
    where = image_where(source, image_id, info.field_types.get(source.fields["imageId"]))
    features = client.query_all(source.url, where, token, limit=source.max_results, cancelled=cancelled)
    return [result_of(source, f, info.object_id_field) for f in features]


def geometry_of(esri: Optional[dict[str, Any]]) -> Optional[QgsGeometry]:
    """An Esri geometry (WGS 84) as a QGIS one; None without one."""
    if not isinstance(esri, dict):
        return None
    if is_number(esri.get("x")) and is_number(esri.get("y")):
        return QgsGeometry.fromPointXY(QgsPointXY(esri["x"], esri["y"]))
    points = esri.get("points")
    if isinstance(points, list) and points:
        return QgsGeometry.fromMultiPointXY([QgsPointXY(p[0], p[1]) for p in points if len(p) >= 2])
    paths = esri.get("paths")
    if isinstance(paths, list) and paths:
        return QgsGeometry.fromMultiPolylineXY([[QgsPointXY(p[0], p[1]) for p in path] for path in paths if len(path) >= 2])
    return footprint(esri)


def family_of(results: Sequence[AiResult]) -> str:
    """The layer type that holds a source's results: polygons, lines or points (the most common geometry)."""
    counts = {"MultiPolygon": 0, "MultiLineString": 0, "MultiPoint": 0}
    for r in results:
        g = r.geometry if isinstance(r.geometry, dict) else {}
        if "rings" in g or "xmin" in g:
            counts["MultiPolygon"] += 1
        elif "paths" in g:
            counts["MultiLineString"] += 1
        elif g:
            counts["MultiPoint"] += 1
    return max(counts, key=lambda k: counts[k]) if any(counts.values()) else "MultiPoint"


def _local_crs(point: QgsPointXY) -> QgsCoordinateReferenceSystem:
    return QgsCoordinateReferenceSystem.fromProj(f"+proj=aeqd +lat_0={point.y()} +lon_0={point.x()} +datum=WGS84 +units=m +no_defs")


def place_of(result: AiResult) -> Optional[tuple[float, float, Optional[float]]]:
    """
    Where a result is recorded: a point as it is, a polygon's point inside it
    (and the long side of its smallest rotated rectangle, m), a line's middle; None without a geometry.
    """
    g = geometry_of(result.geometry)
    if g is None or g.isEmpty():
        return None
    kind = g.type()
    if kind == QgsWkbTypes.GeometryType.PointGeometry:
        p = g.asPoint() if not g.isMultipart() else g.asMultiPoint()[0]
        return p.x(), p.y(), None
    if kind == QgsWkbTypes.GeometryType.LineGeometry:
        p = g.interpolate(g.length() / 2).asPoint()
        return p.x(), p.y(), None
    inside = g.pointOnSurface()
    if inside.isNull():
        inside = g.centroid()
    p = inside.asPoint()
    length = None
    try:
        local = QgsGeometry(g)
        local.transform(QgsCoordinateTransform(WGS84, _local_crs(p), QgsProject.instance()))
        box = local.orientedMinimumBoundingBox()
        # (geometry, area, angle, width, height)
        if box and len(box) >= 5 and box[3] and box[4]:
            length = round(max(box[3], box[4]), 1)
    except Exception:  # noqa: BLE001 (a polygon that cannot be measured is recorded without a length)
        length = None
    return p.x(), p.y(), length


def results_layer(results: Sequence[AiResult], rows: Sequence[int], source: AiResultSource, image_id: str,
                  imported: set[str]) -> QgsVectorLayer:
    """One source's results as a memory layer (WGS 84), labelled with their class and score; `row` is the list's row."""
    family = family_of(results)
    layer = QgsVectorLayer(f"{family}?crs=EPSG:4326", f"外部AI結果 {source.label}（{image_id}）", "memory")
    fields = QgsFields()
    for name, kind in ((ROW, "integer"), ("source", "string"), ("result_id", "string"), ("class", "string"), ("score", "double"),
                       ("kind", "string"), ("name", "string"), ("model", "string"), ("detected_at", "date"), ("imported", "integer")):
        fields.append(make_field(name, kind))
    provider = layer.dataProvider()
    provider.addAttributes(fields.toList())
    layer.updateFields()
    features = []
    for r, row in zip(results, rows):
        f = QgsFeature(layer.fields())
        g = geometry_of(r.geometry)
        if g is not None and not g.isEmpty():
            g.convertToMultiType()
            if QgsWkbTypes.geometryType(g.wkbType()) == QgsWkbTypes.geometryType(layer.wkbType()):
                f.setGeometry(g)
        f.setAttributes([row, r.source, r.key, r.cls or None, r.score, kind_for(r, source) or None, r.name or None, r.model or None,
                         qdatetime(r.detected_at) if r.detected_at is not None else None, 1 if r.tag in imported else 0])
        features.append(f)
    provider.addFeatures(features)
    layer.updateExtents()
    layer.setCustomProperty(RESULTS_OF, image_id)
    _style(layer, family)
    return layer


def _style(layer: QgsVectorLayer, family: str) -> None:
    if family == "MultiPolygon":
        symbol = QgsFillSymbol.createSimple({"color": "0,0,0,0", "outline_color": COLOR, "outline_width": "0.6"})
    elif family == "MultiLineString":
        symbol = QgsLineSymbol.createSimple({"line_color": COLOR, "line_width": "0.6"})
    else:
        symbol = QgsMarkerSymbol.createSimple({"name": "circle", "color": "0,0,0,0", "outline_color": COLOR, "outline_width": "0.6", "size": "4"})
    layer.setRenderer(QgsSingleSymbolRenderer(symbol))
    label = QgsPalLayerSettings()
    label.fieldName = "concat(coalesce(\"class\", ''), ' ', coalesce(format_number(\"score\", 2), ''))"
    label.isExpression = True
    text = QgsTextFormat()
    text.setColor(QColor(COLOR))
    text.setSize(8)
    buffer = text.buffer()
    buffer.setEnabled(True)
    buffer.setSize(0.8)
    buffer.setColor(QColor("white"))
    text.setBuffer(buffer)
    label.setFormat(text)
    layer.setLabeling(QgsVectorLayerSimpleLabeling(label))
    layer.setLabelsEnabled(True)


def mark_imported(layer: QgsVectorLayer, rows: set[int]) -> None:
    """Marks features (by list row) recorded as observations."""
    index = layer.fields().indexOf("imported")
    row_index = layer.fields().indexOf(ROW)
    if index < 0 or row_index < 0:
        return
    changes = {f.id(): {index: 1} for f in layer.getFeatures() if f[row_index] in rows and f[index] != 1}
    if changes:
        layer.dataProvider().changeAttributeValues(changes)
        layer.triggerRepaint()

