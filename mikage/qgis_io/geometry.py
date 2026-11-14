"""QGIS geometries to and from the core's plain WGS84 shapes."""
from __future__ import annotations

from typing import Optional, Sequence

from qgis.core import (
    QgsCoordinateReferenceSystem, QgsCoordinateTransform, QgsGeometry, QgsPointXY, QgsProject, QgsWkbTypes,
)

from ..core.plan.targets import Shape
from ..core.request.model import polygons_of

WGS84 = QgsCoordinateReferenceSystem("EPSG:4326")


def to_wgs84(crs: QgsCoordinateReferenceSystem) -> QgsCoordinateTransform:
    return QgsCoordinateTransform(crs, WGS84, QgsProject.instance())


def from_wgs84(crs: QgsCoordinateReferenceSystem) -> QgsCoordinateTransform:
    return QgsCoordinateTransform(WGS84, crs, QgsProject.instance())


def shape_of(geometry: QgsGeometry, transform: Optional[QgsCoordinateTransform]) -> Optional[Shape]:
    """A feature's geometry as a Shape in WGS84 (curves made straight); None when empty."""
    if geometry is None or geometry.isNull() or geometry.isEmpty():
        return None
    g = QgsGeometry(geometry)
    if QgsWkbTypes.isCurvedType(g.wkbType()):
        g.convertToStraightSegment()
    if transform is not None and transform.isValid() and not transform.isShortCircuited():
        g.transform(transform)
    vertices = [(v.x(), v.y()) for v in g.vertices()]
    if not vertices:
        return None
    polygons = []
    interior = None
    if g.type() == QgsWkbTypes.GeometryType.PolygonGeometry:
        parts = g.asMultiPolygon() if g.isMultipart() else [g.asPolygon()]
        polygons = [[[(p.x(), p.y()) for p in ring] for ring in rings] for rings in parts if rings]
        if len(polygons) == 1:
            p = g.pointOnSurface()
            if not p.isNull():
                interior = (p.asPoint().x(), p.asPoint().y())
    return Shape(vertices, polygons, interior)


def polygon_of(rings: Sequence[Sequence[tuple]]) -> QgsGeometry:
    """A polygon (WGS84) from its rings."""
    return QgsGeometry.fromPolygonXY([[QgsPointXY(x, y) for x, y in ring] for ring in rings])


def multipolygon_of(polygons: Sequence[Sequence[tuple]]) -> QgsGeometry:
    """A multipolygon (WGS84) of single-ring polygons."""
    return QgsGeometry.fromMultiPolygonXY([[[QgsPointXY(x, y) for x, y in ring]] for ring in polygons])


def rings_geometry(rings: Sequence[Sequence[Sequence[float]]]) -> QgsGeometry:
    """
    A polygon (WGS84) of an area's rings as the core keeps them (a request's:
    outer rings and holes in one list, model.polygons_of): a polygon with its
    holes, or a multipolygon when it has several parts.
    """
    polygons = [[[QgsPointXY(p[0], p[1]) for p in ring] for ring in polygon] for polygon in polygons_of(rings)]
    if len(polygons) == 1:
        return QgsGeometry.fromPolygonXY(polygons[0])
    return QgsGeometry.fromMultiPolygonXY(polygons)


def shape_rings(shape: Optional[Shape]) -> list:
    """Every ring of a Shape's polygons (all parts, holes too) as one list of [longitude, latitude] rings."""
    if shape is None:
        return []
    return [[list(p) for p in ring] for polygon in shape.polygons for ring in polygon]


def line_of(coords: Sequence[tuple]) -> QgsGeometry:
    return QgsGeometry.fromPolylineXY([QgsPointXY(x, y) for x, y in coords])
