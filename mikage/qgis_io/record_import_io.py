"""観測記録の一括取込み in QGIS (core/obs/record_import.py): a layer's features as rows, their points in WGS 84."""
from __future__ import annotations

from typing import Any

from qgis.core import QgsCoordinateReferenceSystem, QgsCoordinateTransform, QgsFeatureRequest, QgsGeometry, QgsProject, QgsVectorLayer
from qgis.PyQt.QtCore import QDate, QDateTime, QVariant

from ..core.obs.record_import import SourceRow


def _plain(value: Any) -> Any:
    if isinstance(value, QVariant):
        value = None if value.isNull() else value.value()
    if isinstance(value, QDate):
        return QDateTime(value) if value.isValid() else None
    if isinstance(value, QDateTime):
        return value if value.isValid() else None
    return value


def layer_rows(layer: QgsVectorLayer, selected_only: bool = False) -> tuple[list[str], list[SourceRow]]:
    """The layer's attribute names and its features as rows (numbered by feature id); a shape's point on its surface as the point."""
    columns = [f.name() for f in layer.fields()]
    wgs = QgsCoordinateReferenceSystem("EPSG:4326")
    transform = QgsCoordinateTransform(layer.crs(), wgs, QgsProject.instance()) if layer.isSpatial() and layer.crs() != wgs else None
    features = layer.getSelectedFeatures() if selected_only else layer.getFeatures(QgsFeatureRequest())
    out = []
    for f in features:
        row = SourceRow(int(f.id()), {c: _plain(f.attribute(c)) for c in columns})
        g = f.geometry() if layer.isSpatial() else None
        if g is not None and not g.isNull() and not g.isEmpty():
            p = QgsGeometry(g).pointOnSurface()
            if not p.isNull():
                if transform is not None:
                    try:
                        p.transform(transform)
                    except Exception:  # noqa: BLE001 (outside the CRS's area: no point)
                        p = QgsGeometry()
                if not p.isNull():
                    point = p.asPoint()
                    row.lon, row.lat = point.x(), point.y()
        out.append(row)
    return columns, out
