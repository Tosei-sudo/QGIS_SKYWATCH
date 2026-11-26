"""
画像ファイルから読むもの (qgis_io/raster_facts.py, 2026-11-27): a raster
layer's file opened once for what the plugin reads of it (its metadata,
whether it is complex SAR), not again by each dock and check.
"""
import pytest

pytest.importorskip("qgis.core")
from qgis.testing import start_app  # noqa: E402

APP = start_app()

from osgeo import gdal, osr  # noqa: E402
from qgis.core import QgsProject, QgsRasterLayer  # noqa: E402

from mikage.qgis_io import complex_sar, obs_images, raster_facts  # noqa: E402


def image(path, data_type=gdal.GDT_Byte, **items) -> str:
    ds = gdal.GetDriverByName("GTiff").Create(str(path), 16, 16, 1, data_type)
    ds.SetGeoTransform([139.6, 0.01, 0, 35.7, 0, -0.01])
    srs = osr.SpatialReference()
    srs.ImportFromEPSG(4326)
    ds.SetProjection(srs.ExportToWkt())
    for k, v in items.items():
        ds.SetMetadataItem(k, v)
    ds = None
    return str(path)


@pytest.fixture(autouse=True)
def fresh():
    obs_images.forget()
    yield
    obs_images.forget()
    QgsProject.instance().removeAllMapLayers()


def test_an_image_is_opened_once_for_its_time_and_metadata(tmp_path):
    path = image(tmp_path / "scene.tif", ACQUISITION_START_TIME="2026-10-01T01:00:00Z")
    layer = QgsRasterLayer(path, "scene")
    before = raster_facts.opened
    for _ in range(4):
        # As the observation dock, the image check and the complex SAR check read it.
        assert obs_images.observed_image(layer).time is not None
        assert obs_images.is_image_layer(layer)
        assert obs_images.gdal_metadata(layer)["ACQUISITION_START_TIME"] == "2026-10-01T01:00:00Z"
    assert not complex_sar.convert_layer(layer, "dB", str(tmp_path / "cache"))
    assert raster_facts.opened - before == 1
    # Another domain: one open more, once.
    obs_images.gdal_metadata(layer, "IMD")
    obs_images.gdal_metadata(layer, "IMD")
    assert raster_facts.opened - before == 2
    # What a caller does with what it got is its own.
    obs_images.gdal_metadata(layer)["ACQUISITION_START_TIME"] = "changed"
    assert obs_images.gdal_metadata(layer)["ACQUISITION_START_TIME"] == "2026-10-01T01:00:00Z"


def test_a_layer_added_again_reads_its_file_again(tmp_path):
    path = image(tmp_path / "scene.tif", ACQUISITION_START_TIME="2026-10-01T01:00:00Z")
    first = QgsRasterLayer(path, "scene")
    assert obs_images.gdal_metadata(first)["ACQUISITION_START_TIME"] == "2026-10-01T01:00:00Z"
    # The file written again (another time), and opened again as a new layer: its new metadata.
    image(tmp_path / "scene.tif", ACQUISITION_START_TIME="2026-10-02T01:00:00Z")
    again = QgsRasterLayer(path, "scene")
    assert obs_images.gdal_metadata(again)["ACQUISITION_START_TIME"] == "2026-10-02T01:00:00Z"


def test_a_complex_image_is_converted_with_its_one_read(tmp_path):
    path = image(tmp_path / "slc.tif", gdal.GDT_CInt16)
    layer = QgsRasterLayer(path, "slc")
    before = raster_facts.opened
    obs_images.observed_image(layer)
    assert complex_sar.convert_layer(layer, "dB", str(tmp_path / "cache"))
    # The check used what the observation dock had read; the VRT shows the amplitude.
    assert raster_facts.opened - before == 1
    assert layer.source().endswith(".vrt") and obs_images.source_of(layer) == path


def test_unreadable_files_are_not_kept_and_the_most_recent_are(tmp_path, monkeypatch):
    missing = str(tmp_path / "missing.tif")
    assert raster_facts.metadata("a", missing) == {}
    assert raster_facts.metadata("a", missing) == {}
    assert ("a", missing) not in raster_facts._kept
    monkeypatch.setattr(raster_facts, "KEPT", 2)
    paths = [image(tmp_path / f"i{n}.tif", N=str(n)) for n in range(3)]
    for n, p in enumerate(paths):
        assert raster_facts.metadata(f"layer{n}", p)["N"] == str(n)
    assert len(raster_facts._kept) == 2 and ("layer0", paths[0]) not in raster_facts._kept
