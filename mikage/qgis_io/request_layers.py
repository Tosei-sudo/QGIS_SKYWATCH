"""
Imaging requests to and from QGIS layers: a scratch polygon layer of
requests (レイヤーへ書き出し), and the features of any polygon or point layer
read as rows (レイヤーから取り込み).
"""
from __future__ import annotations

from typing import Sequence

from qgis.core import QgsFeature, QgsField, QgsFields, QgsVectorLayer
from qgis.PyQt.QtCore import QDate, QDateTime, QVariant

from ..core.request.model import ImagingRequest, square_rings
from ..core.request.rows import NUMBERS, WRITTEN, row_of
from ..core.request.store import DEFAULT_REQUEST_FIELDS
from .geometry import rings_geometry, shape_of, shape_rings, to_wgs84


def requests_layer(requests: Sequence[ImagingRequest], name: str) -> QgsVectorLayer:
    """A scratch layer (WGS 84 polygons) of requests, with the ledger's attribute names."""
    layer = QgsVectorLayer("Polygon?crs=EPSG:4326", name, "memory")
    fields = QgsFields()
    for role in WRITTEN:
        kind = QVariant.Double if role in NUMBERS else QVariant.String
        fields.append(QgsField(DEFAULT_REQUEST_FIELDS[role], kind))
    provider = layer.dataProvider()
    provider.addAttributes(fields.toList())
    layer.updateFields()
    features = []
    for r in requests:
        f = QgsFeature(layer.fields())
        row = row_of(r)
        f.setAttributes([row.get(DEFAULT_REQUEST_FIELDS[role]) for role in WRITTEN])
        if r.rings:
            f.setGeometry(rings_geometry(r.rings))
        features.append(f)
    provider.addFeatures(features)
    layer.updateExtents()
    return layer


def _plain(v):
    """A field value as Python (dates as dates; NULL as None)."""
    if v is None or (hasattr(v, "isNull") and v.isNull()):
        return None
    if isinstance(v, QDateTime):
        return v.toPyDateTime().astimezone() if v.isValid() else None
    if isinstance(v, QDate):
        return v.toPyDate() if v.isValid() else None
    return v


def layer_rows(layer: QgsVectorLayer, selected_only: bool, point_km: float) -> list[tuple[dict, list, str]]:
    """
    The features of a layer as (values by field name, WGS 84 rings, 範囲 / 点):
    a polygon its own shape (concave, with its holes and every part of a
    multipart one), a point a square of `point_km`, a line or several points
    the box around them.
    """
    transform = to_wgs84(layer.crs())
    names = [f.name() for f in layer.fields()]
    feats = layer.getSelectedFeatures() if selected_only and layer.selectedFeatureCount() else layer.getFeatures()
    out = []
    for feat in feats:
        values = {n: _plain(feat[n]) for n in names}
        shape = shape_of(feat.geometry(), transform)
        rings, kind = [], "範囲"
        if shape is not None:
            if shape.polygons:
                rings = shape_rings(shape)
            elif len(shape.vertices) == 1:
                rings, kind = square_rings(shape.vertices[0][0], shape.vertices[0][1], point_km), "点"
            else:
                xs = [v[0] for v in shape.vertices]
                ys = [v[1] for v in shape.vertices]
                w, s, e, n = min(xs), min(ys), max(xs), max(ys)
                rings = [[[w, s], [e, s], [e, n], [w, n], [w, s]]]
        out.append((values, rings, kind))
    return out
