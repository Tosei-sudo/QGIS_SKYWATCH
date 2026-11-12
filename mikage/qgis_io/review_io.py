"""
The QGIS side of the AI results' review (core/ai/review): the checked area
(確認範囲) that records a wrong result, a rectangle around it a little
larger than it, in longitude / latitude.
"""
from __future__ import annotations

from typing import Optional

from qgis.core import QgsCoordinateTransform, QgsGeometry, QgsProject, QgsWkbTypes

from ..core.ai.review import REJECT_POINT_M, reject_margin
from ..core.ai_results import AiResult
from .ai_results_io import WGS84, _local_crs, geometry_of


def rejection_ring(result: AiResult) -> Optional[list[list[float]]]:
    """The outline (longitude / latitude, closed) of the checked area recording a result as wrong; None without a place."""
    g = geometry_of(result.geometry)
    if g is None or g.isEmpty():
        return None
    centre = g.centroid().asPoint()
    local_crs = _local_crs(centre)
    project = QgsProject.instance()
    local = QgsGeometry(g)
    local.transform(QgsCoordinateTransform(WGS84, local_crs, project))
    box = local.boundingBox()
    if g.type() == QgsWkbTypes.GeometryType.PointGeometry or (box.width() < 1e-6 and box.height() < 1e-6):
        side = result.length if result.length and result.length > 0 else REJECT_POINT_M
        c = box.center()
        box.setXMinimum(c.x() - side / 2)
        box.setXMaximum(c.x() + side / 2)
        box.setYMinimum(c.y() - side / 2)
        box.setYMaximum(c.y() + side / 2)
    margin = reject_margin(max(box.width(), box.height()))
    box.grow(margin)
    ring = QgsGeometry.fromRect(box)
    ring.transform(QgsCoordinateTransform(local_crs, WGS84, project))
    points = ring.asPolygon()[0]
    return [[p.x(), p.y()] for p in points]

