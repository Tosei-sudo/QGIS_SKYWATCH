"""
Geodesic buffers and centroids (the viewer's processing/buffer.ts and
centroid.ts), worked out on the WGS 84 ellipsoid rather than in the layer's
CRS: each feature is projected onto an azimuthal equidistant projection
centred on it (PROJ's `aeqd`, exact distances from its centre on the
ellipsoid), buffered or measured there in metres, and projected back. A
point's round buffer is so a true geodesic circle.

Edges are kept as the layer draws them: they are densified in the layer's
CRS before being projected, and the result densified before it comes back.
"""
from __future__ import annotations

from typing import Optional

from qgis.core import (
    Qgis, QgsCoordinateReferenceSystem, QgsCoordinateTransform, QgsCoordinateTransformContext, QgsGeometry, QgsPointXY, QgsRectangle,
    QgsWkbTypes,
)

CAPS = {"round": Qgis.EndCapStyle.Round, "flat": Qgis.EndCapStyle.Flat, "square": Qgis.EndCapStyle.Square}
WGS84 = QgsCoordinateReferenceSystem("EPSG:4326")
#: Pieces each edge is cut into at least, before and after the projection.
DENSIFY = 100


def aeqd(lon: float, lat: float) -> QgsCoordinateReferenceSystem:
    """The azimuthal equidistant projection centred on lon, lat (metres, WGS 84)."""
    return QgsCoordinateReferenceSystem.fromProj(f"+proj=aeqd +lat_0={lat!r} +lon_0={lon!r} +x_0=0 +y_0=0 +datum=WGS84 +units=m +no_defs")


def centre_of(extent: QgsRectangle, crs: QgsCoordinateReferenceSystem, context: QgsCoordinateTransformContext) -> QgsPointXY:
    """The middle of an extent, in longitude and latitude."""
    to = QgsCoordinateTransform(crs, WGS84, context)
    return to.transform(extent.center())


def _densified(geometry: QgsGeometry, pieces: int = DENSIFY) -> QgsGeometry:
    """Edges cut so none is longer than 1/pieces of the geometry's extent (points as they are)."""
    if geometry.type() == QgsWkbTypes.GeometryType.PointGeometry:
        return QgsGeometry(geometry)
    box = geometry.boundingBox()
    step = max(box.width(), box.height()) / pieces
    return geometry.densifyByDistance(step) if step > 0 else QgsGeometry(geometry)


def to_local(geometry: QgsGeometry, crs: QgsCoordinateReferenceSystem, context: QgsCoordinateTransformContext,
             centre: Optional[QgsPointXY] = None) -> tuple[QgsGeometry, QgsCoordinateTransform]:
    """The geometry in the aeqd projection centred on it (or on `centre`), and the transform back."""
    centre = centre or centre_of(geometry.boundingBox(), crs, context)
    local = aeqd(centre.x(), centre.y())
    to = QgsCoordinateTransform(crs, local, context)
    g = _densified(geometry)
    g.transform(to)
    return g, to


def from_local(geometry: QgsGeometry, to: QgsCoordinateTransform) -> QgsGeometry:
    g = _densified(geometry)
    g.transform(to, QgsCoordinateTransform.TransformDirection.ReverseTransform)
    return g


def geodesic_buffer(geometry: QgsGeometry, crs: QgsCoordinateReferenceSystem, context: QgsCoordinateTransformContext,
                    distance: float, segments: int = 16, cap: str = "round") -> Optional[QgsGeometry]:
    """The buffer of `distance` metres (negative shrinks polygons), in the layer's CRS; None when empty."""
    if geometry is None or geometry.isEmpty():
        return None
    local, to = to_local(geometry, crs, context)
    grown = local.buffer(distance, segments, CAPS[cap], Qgis.JoinStyle.Round, 2)  # round joins, as the viewer's
    if grown is None or grown.isEmpty():
        return None
    if geometry.type() == QgsWkbTypes.GeometryType.PointGeometry:
        # Circles already have their vertices on the geodesic circle.
        grown.transform(to, QgsCoordinateTransform.TransformDirection.ReverseTransform)
        return grown
    return from_local(grown, to)


def dissolve(geometries: list[QgsGeometry], crs: QgsCoordinateReferenceSystem, context: QgsCoordinateTransformContext) -> Optional[QgsGeometry]:
    """The union of geometries, in an aeqd projection centred on them all."""
    if not geometries:
        return None
    box = QgsRectangle(geometries[0].boundingBox())
    for g in geometries[1:]:
        box.combineExtentWith(g.boundingBox())
    centre = centre_of(box, crs, context)
    local = aeqd(centre.x(), centre.y())
    to = QgsCoordinateTransform(crs, local, context)
    parts = []
    for g in geometries:
        g = QgsGeometry(g)
        g.transform(to)
        parts.append(g)
    merged = QgsGeometry.unaryUnion(parts)
    if merged is None or merged.isEmpty():
        return None
    merged.transform(to, QgsCoordinateTransform.TransformDirection.ReverseTransform)
    return merged


def parts_of(geometry: QgsGeometry) -> list[QgsGeometry]:
    """The parts of a multi geometry (itself when single)."""
    return [QgsGeometry(p.clone()) for p in geometry.constParts()] if geometry.isMultipart() else [geometry]


def geodesic_centroid(geometry: QgsGeometry, crs: QgsCoordinateReferenceSystem, context: QgsCoordinateTransformContext,
                      inside: bool = False) -> Optional[QgsGeometry]:
    """
    The centre of mass on the ground (polygons by area, lines by length,
    points equally), in the layer's CRS; with `inside`, a point inside a
    polygon whose centroid falls outside it. None when empty.
    """
    if geometry is None or geometry.isEmpty():
        return None
    local, to = to_local(geometry, crs, context)
    point = local.centroid()
    if inside and local.type() == QgsWkbTypes.GeometryType.PolygonGeometry and not local.contains(point):
        point = local.pointOnSurface()
    if point is None or point.isEmpty():
        return None
    point.transform(to, QgsCoordinateTransform.TransformDirection.ReverseTransform)
    return point
