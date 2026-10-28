"""
The dock 「スペクトル」 in QGIS: points and boxes read from the file with
GDAL (no data left out), the lines and marks, CSV, following the active layer.
"""
import numpy as np
import pytest

qgis_core = pytest.importorskip("qgis.core")
from qgis.testing import start_app  # noqa: E402

APP = start_app()

from osgeo import gdal, osr  # noqa: E402
from qgis.core import QgsPointXY, QgsProject, QgsRasterLayer, QgsRectangle, QgsSettings  # noqa: E402

from satellite_observation.qgis_io import settings  # noqa: E402
from satellite_observation.tests.qgis_fakes import FakeIface  # noqa: E402


def tiff(path, nodata=None):
    """4 bands of 10 × 10 pixels in EPSG:3857 (1 m each): band b holds 10·b + column; the top-left pixel no data."""
    ds = gdal.GetDriverByName("GTiff").Create(str(path), 10, 10, 4, gdal.GDT_UInt16)
    ds.SetGeoTransform([0, 1, 0, 10, 0, -1])
    srs = osr.SpatialReference()
    srs.ImportFromEPSG(3857)
    ds.SetProjection(srs.ExportToWkt())
    cols = np.tile(np.arange(10, dtype=np.uint16), (10, 1))
    for b in range(1, 5):
        band = ds.GetRasterBand(b)
        a = cols + 10 * b
        if nodata is not None:
            a[0, 0] = nodata
            band.SetNoDataValue(nodata)
        band.WriteArray(a)
        band.SetDescription(["Blue", "Green", "Red", "NIR"][b - 1])
    ds = None
    layer = QgsRasterLayer(str(path), path.stem)
    assert layer.isValid()
    return layer


@pytest.fixture
def plugin(tmp_path):
    from satellite_observation import classFactory

    QgsSettings().remove("satellite_observation")
    settings.set_config_source("")
    iface = FakeIface()
    p = classFactory(iface)
    p.initGui()
    layer = tiff(tmp_path / "four.tif", nodata=999)
    QgsProject.instance().addMapLayer(layer)
    iface.canvas.setDestinationCrs(layer.crs())
    iface.canvas.setLayers([layer])
    iface.canvas.setExtent(QgsRectangle(0, 0, 10, 10))
    yield p, iface, layer, tmp_path
    p.unload()
    QgsProject.instance().removeAllMapLayers()
    QgsSettings().remove("satellite_observation")


def test_points_and_boxes_give_the_bands_values(plugin):
    p, iface, layer, tmp_path = plugin
    dock = p.api.spectral
    dock.toggleViewAction().trigger()
    assert iface.canvas.mapTool() is dock.tool
    # No image chosen yet.
    assert dock.add_point(QgsPointXY(3.5, 5.5)) is None and dock.note.text() == "画像のレイヤーを選択"
    iface.setActiveLayer(layer)
    s = dock.add_point(QgsPointXY(3.5, 5.5))
    assert s.label == "点（列 3、行 4）" and [b.mean for b in s.bands] == [13, 23, 33, 43]
    assert dock.labels == ["B1 Blue", "B2 Green", "B3 Red", "B4 NIR"] and s.where == pytest.approx((3.5 * 360 / 40075016.68557849, s.where[1]))
    # The top-left pixel is no data; off the image there is nothing.
    assert dock.add_point(QgsPointXY(0.5, 9.5)) is None and dock.note.text() == "その地点はノーデータ"
    assert dock.add_point(QgsPointXY(20, 5)) is None and dock.note.text() == "その地点に画像なし"
    # A box over the top two rows: no data left out of the mean.
    box = dock.add_box(QgsRectangle(0, 8, 10, 10))
    assert box.label == "範囲 10 × 2 画素" and box.pixels == 20
    assert box.bands[0].count == 19 and box.bands[0].min == 10 and box.bands[0].max == 19
    assert box.bands[0].mean == pytest.approx((sum(range(10, 20)) * 2 - 10) / 19)
    assert dock.marks.series == dock.series and len(dock.series) == 2
    path = dock.save_csv(str(tmp_path / "s.csv"))
    text = open(path, encoding="utf-8-sig").read()
    assert text.splitlines()[0] == "band,1 点（列 3、行 4）,2 範囲 10 × 2 画素,2 範囲 10 × 2 画素 min,2 範囲 10 × 2 画素 max,2 範囲 10 × 2 画素 std"
    assert text.splitlines()[1].startswith("B1 Blue,13,")
    # At most 8 lines: the oldest goes.
    for x in range(8):
        dock.add_point(QgsPointXY(x + 0.5, 5.5))
    assert len(dock.series) == 8 and dock.series[0].number == 3
    # Another active layer: the graph starts afresh.
    other = tiff(tmp_path / "other.tif")
    QgsProject.instance().addMapLayer(other)
    iface.setActiveLayer(other)
    assert dock.series == [] and dock.next_number == 1
    dock.hide()
    assert dock.marks is None and iface.canvas.mapTool() is not dock.tool
