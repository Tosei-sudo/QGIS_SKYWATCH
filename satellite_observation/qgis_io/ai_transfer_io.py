"""
「検出結果を DB へ転記」 in QGIS: what the model made and what was sent where
are kept on the layer (custom properties, saved with the project), the
features measured on the ellipsoid and sent as Esri JSON in WGS 84.
"""
from __future__ import annotations

import json
import math
from typing import Any, Callable, Iterable, Optional

from qgis.core import (
    QgsCoordinateReferenceSystem, QgsCoordinateTransform, QgsDistanceArea, QgsFeature, QgsGeometry, QgsProject, QgsVectorLayer,
    QgsWkbTypes,
)
from qgis.PyQt.QtCore import QDate, QDateTime

from ..core.ai.transfer import (
    BATCH, DetectionOutput, Plan, Target, action_of, bearing_text, signature_of, status_of, transfer_attributes,
)
from ..core.esri import EsriClient, EsriError

#: What the model made, by feature id: `{fid: {"class", "geometry"}}`.
ORIGIN = "satellite_observation/ai_origin"
#: What was sent where: `{url: {fid: {"id", "signature"}}}`.
SENT = "satellite_observation/ai_sent"

WGS84 = QgsCoordinateReferenceSystem("EPSG:4326")
MERCATOR = QgsCoordinateReferenceSystem("EPSG:3857")


def _property(layer: QgsVectorLayer, key: str) -> dict:
    try:
        value = json.loads(layer.customProperty(key) or "{}")
    except (TypeError, ValueError):
        return {}
    return value if isinstance(value, dict) else {}


def plain(value: Any) -> Any:
    """An attribute as plain Python: NULL as None, times as ISO 8601 (UTC)."""
    if value is None or (hasattr(value, "isNull") and not isinstance(value, (QDateTime, QDate)) and value.isNull()):
        return None
    if isinstance(value, QDateTime):
        return value.toUTC().toString("yyyy-MM-ddTHH:mm:ss.zzzZ") if value.isValid() else None
    if isinstance(value, QDate):
        return value.toString("yyyy-MM-dd") if value.isValid() else None
    return value


def geometry_text(geometry: QgsGeometry, crs: QgsCoordinateReferenceSystem) -> str:
    """The geometry as text, to the centimetre (Web Mercator), for comparing."""
    if geometry is None or geometry.isEmpty():
        return ""
    g = QgsGeometry(geometry)
    if crs.isValid() and crs != MERCATOR:
        g.transform(QgsCoordinateTransform(crs, MERCATOR, QgsProject.instance()))
    return ",".join(f"{v.x():.2f},{v.y():.2f}" for v in g.vertices())


def _class(feature: QgsFeature) -> Any:
    return plain(feature["class"]) if feature.fields().indexOf("class") >= 0 else None


def mark_origin(layer: QgsVectorLayer, features: Optional[Iterable[QgsFeature]] = None) -> None:
    """Marks a layer's features as the model made them (called when an AI layer is made)."""
    origin = _property(layer, ORIGIN)
    for f in features if features is not None else layer.getFeatures():
        origin[str(f.id())] = {"class": _class(f), "geometry": geometry_text(f.geometry(), layer.crs())}
    layer.setCustomProperty(ORIGIN, json.dumps(origin, ensure_ascii=False))


def status(layer: QgsVectorLayer, feature: QgsFeature, origin: Optional[dict] = None) -> str:
    """Whether a feature is the model's own, corrected, or added by a person."""
    origin = _property(layer, ORIGIN) if origin is None else origin
    return status_of(origin.get(str(feature.id())), _class(feature), geometry_text(feature.geometry(), layer.crs()))


def mark_sent(layer: QgsVectorLayer, url: str, sent: Iterable[tuple[int, int, str]]) -> None:
    """Remembers features written to `url`: (feature id, object id, signature)."""
    record = _property(layer, SENT)
    target = record.setdefault(url, {})
    for fid, oid, signature in sent:
        target[str(fid)] = {"id": oid, "signature": signature}
    layer.setCustomProperty(SENT, json.dumps(record, ensure_ascii=False))


def is_ai_layer(layer) -> bool:
    """Whether a layer is one the AI tools made (or looks like one: polygons or points with a class)."""
    return (isinstance(layer, QgsVectorLayer) and layer.isValid() and layer.fields().indexOf("class") >= 0
            and layer.geometryType() in (QgsWkbTypes.GeometryType.PolygonGeometry, QgsWkbTypes.GeometryType.PointGeometry))


class Measure:
    """Sizes on the ground (WGS 84 ellipsoid) and WGS 84 coordinates of a layer's geometries."""

    def __init__(self, crs: QgsCoordinateReferenceSystem):
        self.crs = crs
        self.area = QgsDistanceArea()
        self.area.setSourceCrs(crs, QgsProject.instance().transformContext())
        self.area.setEllipsoid("EPSG:7030")
        self.to_wgs84 = QgsCoordinateTransform(crs, WGS84, QgsProject.instance())

    def wgs84(self, geometry: QgsGeometry) -> QgsGeometry:
        g = QgsGeometry(geometry)
        if self.crs != WGS84:
            g.transform(self.to_wgs84)
        return g

    def size(self, geometry: QgsGeometry) -> dict[str, Optional[float]]:
        """Area, and for a box (one ring of four corners) its long and short sides and the bearing of the long side."""
        out: dict[str, Optional[float]] = {"area": None, "length": None, "width": None, "angle": None}
        if geometry.type() != QgsWkbTypes.GeometryType.PolygonGeometry:
            return out
        out["area"] = self.area.measureArea(geometry)
        polygons = geometry.asMultiPolygon() if geometry.isMultipart() else [geometry.asPolygon()]
        if len(polygons) != 1 or len(polygons[0]) != 1 or len(polygons[0][0]) != 5:
            return out
        ring = polygons[0][0]
        a = self.area.measureLine(ring[0], ring[1])
        b = self.area.measureLine(ring[1], ring[2])
        start, end = (ring[0], ring[1]) if a >= b else (ring[1], ring[2])
        out["length"], out["width"] = max(a, b), min(a, b)
        out["angle"] = bearing_text(math.degrees(self.area.bearing(start, end)))
        return out


def _round(value: Optional[float], digits: int) -> Optional[float]:
    return None if value is None else round(value, digits)


def role_values(feature: QgsFeature, measure: Measure, state: str, now_ms: int) -> dict[str, Any]:
    """The value of each role for a feature (before it is fitted to the target's field types)."""
    def attr(name: str) -> Any:
        return plain(feature[name]) if feature.fields().indexOf(name) >= 0 else None

    geometry = feature.geometry()
    point = measure.wgs84(geometry.pointOnSurface()).asPoint() if not geometry.isEmpty() else None
    size = measure.size(geometry)
    angle = attr("angle")
    return {
        "imageId": attr("image_id"),
        "fileName": attr("image"),
        "imageTime": attr("image_time"),
        "detectedAt": attr("detected_at"),
        "sentAt": now_ms,
        "class": attr("class"),
        "classId": attr("class_id"),
        "score": attr("score"),
        "model": attr("model"),
        "status": state,
        "lon": _round(point.x(), 7) if point else None,
        "lat": _round(point.y(), 7) if point else None,
        "area": _round(size["area"], 2),
        "length": _round(size["length"], 2),
        "width": _round(size["width"], 2),
        "angle": angle if angle is not None else size["angle"],
        "note": attr("note"),
    }


def _clockwise(ring: list) -> bool:
    return sum((b.x() - a.x()) * (b.y() + a.y()) for a, b in zip(ring, ring[1:])) > 0


def esri_geometry(geometry: QgsGeometry, geometry_type: str) -> Optional[dict[str, Any]]:
    """
    A WGS 84 geometry as Esri JSON for a layer of `geometry_type`: polygons
    as they are (outer rings clockwise, holes anticlockwise, as Esri has
    them), a point layer gets a polygon's inner point; None when it does not fit.
    """
    kind = geometry.type()
    if geometry_type == "esriGeometryPolygon" and kind == QgsWkbTypes.GeometryType.PolygonGeometry:
        rings = []
        for polygon in geometry.asMultiPolygon() if geometry.isMultipart() else [geometry.asPolygon()]:
            for i, ring in enumerate(polygon):
                if (i == 0) != _clockwise(ring):
                    ring = list(reversed(ring))
                rings.append([[round(p.x(), 9), round(p.y(), 9)] for p in ring])
        return {"rings": rings, "spatialReference": {"wkid": 4326}} if rings else None
    if geometry_type == "esriGeometryPoint" and kind in (QgsWkbTypes.GeometryType.PolygonGeometry, QgsWkbTypes.GeometryType.PointGeometry):
        p = geometry.pointOnSurface().asPoint() if kind == QgsWkbTypes.GeometryType.PolygonGeometry else geometry.centroid().asPoint()
        return {"x": round(p.x(), 9), "y": round(p.y(), 9), "spatialReference": {"wkid": 4326}}
    return None


def plan(layer: QgsVectorLayer, features: Iterable[QgsFeature], output: DetectionOutput, target: Target, now_ms: int) -> Plan:
    """What would be sent: new features to add, changed ones to update (each `(fid, attributes, geometry, signature, id)`)."""
    out = Plan()
    measure = Measure(layer.crs())
    origin = _property(layer, ORIGIN)
    sent = _property(layer, SENT).get(output.url, {})
    sent_at = output.fields.get("sentAt")
    sent_at = target.fields[sent_at.lower()].name if sent_at and sent_at.lower() in target.fields else None
    for f in features:
        geometry = f.geometry()
        shape = esri_geometry(measure.wgs84(geometry), target.geometry_type) if not geometry.isEmpty() else None
        if shape is None:
            out.skipped += 1
            continue
        state = status(layer, f, origin)
        out.statuses[state] += 1
        attributes = transfer_attributes(role_values(f, measure, state, now_ms), output, target.fields)
        signature = signature_of(attributes, shape, sent_at)
        kind, oid = action_of(sent.get(str(f.id())), signature)
        if kind == "add":
            out.adds.append((f.id(), attributes, shape, signature, None))
        elif kind == "update":
            out.updates.append((f.id(), attributes, shape, signature, oid))
        else:
            out.same += 1
    return out


def send(client: EsriClient, output: DetectionOutput, token: Optional[str], target: Target, todo: Plan,
         progress: Callable[[int, int], None] = lambda done, total: None) -> tuple[list[tuple[int, int, str]], int, int, list[str]]:
    """
    Sends the new and changed features, BATCH at a time (off the main
    thread): what was sent (feature id, object id, signature), how many were
    added and updated, and why the others failed.
    """
    sent: list[tuple[int, int, str]] = []
    added = updated = 0
    failures: list[str] = []
    updates = todo.updates if target.can_update else []
    if todo.updates and not target.can_update:
        failures.append(f"更新不可のレイヤーのため、変更した {len(todo.updates)} 件は送信対象外")
    total = max(len(todo.adds), len(updates))
    try:
        for i in range(0, total, BATCH):
            progress(min(i + BATCH, total), total)
            adds, ups = todo.adds[i:i + BATCH], updates[i:i + BATCH]
            result = client.apply_edits(
                output.url,
                adds=[{"attributes": a[1], "geometry": a[2]} for a in adds],
                updates=[{"attributes": {**u[1], target.object_id_field: u[4]}, "geometry": u[2]} for u in ups],
                token=token,
            )
            for a, r in zip(adds, result.add_results):
                if r.get("success") and r.get("objectId") is not None:
                    sent.append((a[0], int(r["objectId"]), a[3]))
                    added += 1
                else:
                    failures.append((r.get("error") or {}).get("description") or "追加失敗")
            for u, r in zip(ups, result.update_results):
                if r.get("success"):
                    sent.append((u[0], u[4], u[3]))
                    updated += 1
                else:
                    failures.append((r.get("error") or {}).get("description") or "更新失敗")
    except EsriError as error:
        failures.append(str(error))
    return sent, added, updated, failures
