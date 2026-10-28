"""
測地線バッファ・正距方位での重心 in QGIS: distances and centres on the WGS 84
ellipsoid whatever the layer's CRS, and the two Processing algorithms.
"""
import pytest

pytest.importorskip("qgis.core")
from qgis.testing import start_app  # noqa: E402

APP = start_app()

from qgis.core import (  # noqa: E402
    QgsApplication, QgsCoordinateReferenceSystem, QgsCoordinateTransform, QgsCoordinateTransformContext, QgsDistanceArea, QgsFeature,
    QgsGeometry, QgsPointXY, QgsProcessingContext, QgsProcessingFeedback, QgsProject, QgsVectorLayer,
)

from satellite_observation.qgis_io import geodesic_io as io  # noqa: E402

CONTEXT = QgsCoordinateTransformContext()
WGS84 = QgsCoordinateReferenceSystem("EPSG:4326")
MERCATOR = QgsCoordinateReferenceSystem("EPSG:3857")


def measure(crs=WGS84):
    d = QgsDistanceArea()
    d.setSourceCrs(crs, CONTEXT)
    d.setEllipsoid("EPSG:7030")
    return d


def test_a_points_buffer_is_a_geodesic_circle_in_any_crs():
    centre = QgsPointXY(139, 60)
    g = io.geodesic_buffer(QgsGeometry.fromPointXY(centre), WGS84, CONTEXT, 1000, segments=8)
    ring = g.asPolygon()[0]
    assert len(ring) == 33 and all(abs(measure().measureLine(centre, p) - 1000) < 0.01 for p in ring)
    # The same in Web Mercator, where 1000 m is about 2000 map units at 60°N.
    to = QgsCoordinateTransform(WGS84, MERCATOR, CONTEXT)
    m = to.transform(centre)
    g = io.geodesic_buffer(QgsGeometry.fromPointXY(m), MERCATOR, CONTEXT, 1000)
    assert 3900 < g.boundingBox().width() < 4100
    assert all(abs(measure(MERCATOR).measureLine(m, p) - 1000) < 0.01 for p in g.asPolygon()[0])
    assert io.geodesic_buffer(QgsGeometry.fromPointXY(centre), WGS84, CONTEXT, -10) is None


def test_lines_grow_and_polygons_shrink_by_metres():
    line = QgsGeometry.fromPolylineXY([QgsPointXY(139, 60), QgsPointXY(139.1, 60)])
    g = io.geodesic_buffer(line, WGS84, CONTEXT, 500, cap="flat")
    # A band 1 km wide along a line of about 5.58 km.
    assert measure().measureArea(g) == pytest.approx(1000 * measure().measureLength(line), rel=0.01)
    square = io.geodesic_buffer(QgsGeometry.fromPointXY(QgsPointXY(139, 60)), WGS84, CONTEXT, 1000, cap="square")
    shrunk = io.geodesic_buffer(square, WGS84, CONTEXT, -500)
    assert measure().measureArea(shrunk) == pytest.approx(1000 * 1000, rel=0.01)
    assert io.geodesic_buffer(square, WGS84, CONTEXT, -1500) is None


def test_centroids_on_the_ground():
    # A long thin triangle in degrees at 70°N: the planar centroid in degrees is not the one on the ground.
    tri = QgsGeometry.fromPolygonXY([[QgsPointXY(0, 60), QgsPointXY(40, 60), QgsPointXY(20, 80), QgsPointXY(0, 60)]])
    c = io.geodesic_centroid(tri, WGS84, CONTEXT).asPoint()
    planar = tri.centroid().asPoint()
    assert c.x() == pytest.approx(20, abs=1e-6) and abs(c.y() - planar.y()) > 0.5
    # A C shape: the centroid falls outside, and `inside` takes a point within.
    c_shape = QgsGeometry.fromWkt("POLYGON((0 0,3 0,3 1,1 1,1 2,3 2,3 3,0 3,0 0))")
    assert not c_shape.contains(io.geodesic_centroid(c_shape, WGS84, CONTEXT))
    assert c_shape.contains(io.geodesic_centroid(c_shape, WGS84, CONTEXT, inside=True))
    assert len(io.parts_of(QgsGeometry.fromWkt("MULTIPOINT((0 0),(1 1))"))) == 2


def test_the_algorithms():
    from satellite_observation.processing_provider.provider import SatObsProvider

    provider = SatObsProvider()
    QgsApplication.processingRegistry().addProvider(provider)
    try:
        layer = QgsVectorLayer("Point?crs=EPSG:4326&field=名前:string", "点", "memory")
        features = []
        for name, x in (("a", 139.0), ("b", 139.01), ("c", None)):
            f = QgsFeature(layer.fields())
            if x is not None:
                f.setGeometry(QgsGeometry.fromPointXY(QgsPointXY(x, 35)))
            f.setAttributes([name])
            features.append(f)
        layer.dataProvider().addFeatures(features)
        QgsProject.instance().addMapLayer(layer)
        context = QgsProcessingContext()
        context.setProject(QgsProject.instance())
        registry = QgsApplication.processingRegistry()
        results, ok = registry.algorithmById("satobs:geodesic_buffer").run(
            {"INPUT": layer.id(), "DISTANCE": 1000, "OUTPUT": "memory:"}, context, QgsProcessingFeedback())
        out = context.getMapLayer(results["OUTPUT"])
        assert ok and [f["名前"] for f in out.getFeatures()] == ["a", "b"]
        results, ok = registry.algorithmById("satobs:geodesic_buffer").run(
            {"INPUT": layer.id(), "DISTANCE": 1000, "DISSOLVE": True, "OUTPUT": "memory:"}, context, QgsProcessingFeedback())
        merged = context.getMapLayer(results["OUTPUT"])
        assert ok and merged.featureCount() == 1 and len(merged.fields()) == 0
        results, ok = registry.algorithmById("satobs:geodesic_centroids").run(
            {"INPUT": merged, "OUTPUT": "memory:"}, context, QgsProcessingFeedback())
        centres = context.getMapLayer(results["OUTPUT"])
        p = next(centres.getFeatures()).geometry().asPoint()
        assert ok and p.x() == pytest.approx(139.005, abs=1e-5) and p.y() == pytest.approx(35, abs=1e-5)
    finally:
        QgsApplication.processingRegistry().removeProvider(provider)
        QgsProject.instance().removeAllMapLayers()
