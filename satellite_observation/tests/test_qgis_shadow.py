"""
The dock 「影から高さ」 in QGIS: the Sun read from the image under the foot
(its metadata, else its time and the place), clicks on the map, the snap
onto the shadow's direction and the heights as a layer.
"""
import pytest

qgis_core = pytest.importorskip("qgis.core")
from qgis.testing import start_app  # noqa: E402

APP = start_app()

from osgeo import gdal, osr  # noqa: E402
from qgis.core import QgsCoordinateReferenceSystem, QgsCoordinateTransform, QgsPointXY, QgsProject, QgsRasterLayer, QgsRectangle, QgsSettings  # noqa: E402

from satellite_observation.qgis_io import settings  # noqa: E402
from satellite_observation.tests.qgis_fakes import FakeIface  # noqa: E402

WGS84 = QgsCoordinateReferenceSystem("EPSG:4326")


def tiff(path, metadata=None):
    """A small image over Tokyo in WGS 84."""
    ds = gdal.GetDriverByName("GTiff").Create(str(path), 100, 100, 1, gdal.GDT_Byte)
    ds.SetGeoTransform([139.69, 0.0001, 0, 35.70, 0, -0.0001])
    srs = osr.SpatialReference()
    srs.ImportFromEPSG(4326)
    ds.SetProjection(srs.ExportToWkt())
    if metadata:
        ds.SetMetadata(metadata)
    ds.GetRasterBand(1).Fill(100)
    ds = None
    return str(path)


@pytest.fixture
def plugin():
    from satellite_observation import classFactory

    QgsSettings().remove("satellite_observation")
    settings.set_config_source("")
    iface = FakeIface()
    p = classFactory(iface)
    p.initGui()
    iface.canvas.setDestinationCrs(QgsCoordinateReferenceSystem("EPSG:3857"))
    yield p, iface
    p.unload()
    QgsProject.instance().removeAllMapLayers()
    QgsSettings().remove("satellite_observation")


def add(iface, path):
    layer = QgsRasterLayer(path, "image")
    assert layer.isValid()
    QgsProject.instance().addMapLayer(layer)
    iface.canvas.setLayers([layer])
    to_map = QgsCoordinateTransform(WGS84, iface.canvas.mapSettings().destinationCrs(), QgsProject.instance())
    iface.canvas.setExtent(to_map.transformBoundingBox(QgsRectangle(139.69, 35.69, 139.70, 35.70)))
    iface.canvas.refresh()
    return layer, to_map


def test_the_sun_from_the_metadata_snaps_the_tip_and_gives_the_height(plugin, tmp_path):
    p, iface = plugin
    layer, to_map = add(iface, tiff(tmp_path / "a.tif", {"SUN_ELEVATION": "45", "SUN_AZIMUTH": "180"}))
    iface.setActiveLayer(layer)
    dock = p.api.shadow_height
    dock.toggleViewAction().trigger()
    # The map tool is on while the dock is open.
    assert iface.canvas.mapTool() is dock.tool and dock.measure_button.isChecked()
    assert (dock.sun().elevation, dock.sun().azimuth) == (45, 180) and "SUN_ELEVATION" in dock.origin_label.text()
    foot = to_map.transform(QgsPointXY(139.695, 35.695))
    dock.click(foot)
    assert dock.foot is not None and dock.overlay.guide is not None
    # The Sun in the south: the shadow falls north. A tip a little east of north snaps onto north.
    tip = to_map.transform(QgsPointXY(139.6951, 35.6959))
    m = dock.click(tip)
    assert m.tip[0] == pytest.approx(139.695, abs=1e-7)
    assert m.shadow == pytest.approx(99.8, abs=0.3) and m.height == pytest.approx(m.shadow)
    assert dock.list.count() == 1 and dock.overlay.lines and dock.foot is None
    # The heights as a layer.
    out = dock.add_layer()
    [f] = out.getFeatures()
    assert f["height_m"] == pytest.approx(m.height, abs=0.01) and f["sun_elevation"] == 45
    assert out.labelsEnabled()
    # Another tool: the dock lets go; closed, nothing stays on the map.
    dock.measure_button.setChecked(False)
    assert iface.canvas.mapTool() is not dock.tool
    dock.hide()
    assert dock.overlay is None


def test_the_sun_from_the_time_in_the_file_name_and_typed_by_hand(plugin, tmp_path):
    p, iface = plugin
    # 2024-06-21 12:00 local (JST in the container's zone or not): only the time's origin is checked closely.
    layer, to_map = add(iface, tiff(tmp_path / "S2A_20240621T030000_x.tif"))
    dock = p.api.shadow_height
    dock.toggleViewAction().trigger()
    # Not the active layer: the image under the foot.
    iface.setActiveLayer(None)
    dock.click(to_map.transform(QgsPointXY(139.695, 35.695)))
    assert dock.sun_origin == "time" and "ファイル名" in dock.origin_label.text()
    dock.cancel()
    assert dock.foot is None
    # The time typed: the Sun computed again for it.
    dock.set_time(1718938800000)  # 2024-06-21T03:00:00Z
    assert dock.sun().elevation == pytest.approx(77.3, abs=1.5)
    # Typed by hand: no azimuth, no snap.
    dock.set_sun(30)
    assert dock.sun_origin == "manual" and not dock.snap.isEnabled()
    dock.click(to_map.transform(QgsPointXY(139.695, 35.695)))
    m = dock.click(to_map.transform(QgsPointXY(139.696, 35.695)))
    assert m.sun_azimuth is None and m.height == pytest.approx(m.shadow * 0.57735, rel=1e-6)


def test_an_unknown_sun_asks_for_it(plugin, tmp_path):
    p, iface = plugin
    layer, to_map = add(iface, tiff(tmp_path / "plain.tif"))
    dock = p.api.shadow_height
    dock.toggleViewAction().trigger()
    assert dock.click(to_map.transform(QgsPointXY(139.695, 35.695))) is None and dock.foot is None
    assert dock.elevation.text() == "不明"
