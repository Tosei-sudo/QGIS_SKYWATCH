"""
Complex SAR images (SICD's I/Q bands, complex pixels) shown as amplitude or
dB through a GDAL VRT (qgis_io/complex_sar), and the settings file's
`complexImages`.
"""
import os

import numpy as np
import pytest

pytest.importorskip("qgis.core")
from qgis.testing import start_app  # noqa: E402

APP = start_app()

from osgeo import gdal, osr  # noqa: E402
from qgis.core import QgsProject, QgsRasterLayer  # noqa: E402

from satellite_observation.core.complex_images import complex_images_of  # noqa: E402
from satellite_observation.core.config import parse_config  # noqa: E402
from satellite_observation.qgis_io import complex_sar  # noqa: E402
from satellite_observation.qgis_io.complex_sar import COMPLEX_SOURCE, Output, build_vrt, convert_layer, outputs_of  # noqa: E402
from satellite_observation.qgis_io.obs_images import file_name, gdal_metadata  # noqa: E402
from satellite_observation.ui.enhance_dock import source_name  # noqa: E402

W, H = 64, 48


def _georef(ds):
    ds.SetGeoTransform([139.0, 0.001, 0, 36.0, 0, -0.001])
    srs = osr.SpatialReference()
    srs.ImportFromEPSG(4326)
    ds.SetProjection(srs.ExportToWkt())


def sicd(path, subcats="I,Q", seed=1):
    """A NITF like a SICD: SAR, two Float32 bands I and Q (the corner row zero, as padding)."""
    rng = np.random.default_rng(seed)
    i = rng.normal(size=(H, W)).astype("f4")
    q = rng.normal(size=(H, W)).astype("f4")
    i[0, :] = 0
    q[0, :] = 0
    mem = gdal.GetDriverByName("MEM").Create("", W, H, 2, gdal.GDT_Float32)
    _georef(mem)
    mem.GetRasterBand(1).WriteArray(i)
    mem.GetRasterBand(2).WriteArray(q)
    gdal.GetDriverByName("NITF").CreateCopy(str(path), mem, options=[
        "IREPBAND=,", f"ISUBCAT={subcats}", "ICAT=SAR", "IDATIM=20251231235959"])
    return str(path), i, q


def tiff(path, bands=1, dtype=gdal.GDT_CInt16, descriptions=()):
    ds = gdal.GetDriverByName("GTiff").Create(str(path), W, H, bands, dtype)
    _georef(ds)
    rng = np.random.default_rng(2)
    values = []
    for b in range(bands):
        if gdal.DataTypeIsComplex(dtype):
            v = (rng.integers(-500, 500, (H, W)) + 1j * rng.integers(-500, 500, (H, W))).astype("c8")
        else:
            v = rng.normal(size=(H, W)).astype("f4")
        ds.GetRasterBand(b + 1).WriteArray(v)
        if b < len(descriptions):
            ds.GetRasterBand(b + 1).SetDescription(descriptions[b])
        values.append(v)
    ds = None
    return str(path), values


def test_which_images_are_complex(tmp_path):
    path, _, _ = sicd(tmp_path / "a.ntf")
    assert outputs_of(gdal.Open(path)) == [Output((1, 2))]
    path, _ = tiff(tmp_path / "c.tif", bands=2)
    assert outputs_of(gdal.Open(path)) == [Output((1,)), Output((2,))]
    path, _ = tiff(tmp_path / "d.tif", bands=4, dtype=gdal.GDT_Float32, descriptions=("I", "Q", "real", "imag"))
    assert outputs_of(gdal.Open(path)) == [Output((1, 2)), Output((3, 4))]
    path, _, _ = sicd(tmp_path / "mp.ntf", subcats="M,P")
    assert outputs_of(gdal.Open(path)) == [Output((1,), amplitude=True)]
    # Two ordinary bands, or Q before I: not complex.
    path, _ = tiff(tmp_path / "plain.tif", bands=2, dtype=gdal.GDT_Float32)
    assert outputs_of(gdal.Open(path)) == []
    path, _ = tiff(tmp_path / "qi.tif", bands=2, dtype=gdal.GDT_Float32, descriptions=("Q", "I"))
    assert outputs_of(gdal.Open(path)) == []


def test_db_vrt_of_a_sicd(tmp_path):
    path, i, q = sicd(tmp_path / "a.ntf")
    vrt = build_vrt(path, "dB", str(tmp_path / "cache"))
    ds = gdal.Open(vrt)
    assert ds.RasterCount == 1 and ds.GetRasterBand(1).DataType == gdal.GDT_Float32
    values = ds.ReadAsArray()
    expected = 20 * np.log10(np.hypot(i, q)[1:])
    assert np.allclose(values[1:], expected, atol=1e-4)
    # I = Q = 0 (padding) is no data.
    assert np.all(np.isneginf(values[0])) and np.isneginf(ds.GetRasterBand(1).GetNoDataValue())
    # The image's metadata and georeferencing come along.
    assert ds.GetMetadataItem("NITF_IDATIM") == "20251231235959"
    assert ds.GetGeoTransform() == pytest.approx(gdal.Open(path).GetGeoTransform())
    assert ds.GetSpatialRef() is not None
    # Reused when asked again; the image is never written.
    before = os.path.getmtime(vrt)
    assert build_vrt(path, "dB", str(tmp_path / "cache")) == vrt and os.path.getmtime(vrt) == before
    assert sorted(os.listdir(tmp_path)) == ["a.ntf", "cache"]


def test_amplitude_vrt_of_complex_pixels(tmp_path):
    path, (values,) = tiff(tmp_path / "slc.tif")
    vrt = build_vrt(path, "amplitude", str(tmp_path / "cache"))
    assert np.allclose(gdal.Open(vrt).ReadAsArray(), np.abs(values), atol=1e-3)
    assert build_vrt(str(tiff(tmp_path / "plain.tif", dtype=gdal.GDT_Byte)[0]), "dB", str(tmp_path / "cache")) is None


def test_amplitude_phase_bands(tmp_path):
    path, m, _ = sicd(tmp_path / "mp.ntf", subcats="M,P")
    values = gdal.Open(build_vrt(path, "amplitude", str(tmp_path / "cache"))).ReadAsArray()
    assert np.allclose(values, np.abs(m), atol=1e-5)


def test_layer_converted_keeps_the_image(tmp_path):
    path, _, _ = sicd(tmp_path / "a.ntf")
    layer = QgsRasterLayer(path, "a", "gdal")
    layer.setCustomProperty("satobs/imageId", "IMG-1")
    assert layer.bandCount() == 2
    assert convert_layer(layer, "dB", str(tmp_path / "cache"))
    assert layer.isValid() and layer.bandCount() == 1
    assert layer.source().endswith(".dB.vrt")
    assert layer.customProperty(COMPLEX_SOURCE) == path
    assert layer.customProperty("satobs/imageId") == "IMG-1"
    renderer = layer.renderer()
    assert renderer.type() == "singlebandgray" and renderer.grayBand() == 1
    lo, hi = renderer.contrastEnhancement().minimumValue(), renderer.contrastEnhancement().maximumValue()
    assert -40 < lo < hi < 20
    # The image's name and metadata, not the VRT's.
    assert file_name(layer) == "a.ntf"
    assert source_name(layer) == path
    assert gdal_metadata(layer).get("NITF_ICAT") == "SAR"
    # Once only.
    assert not convert_layer(layer, "dB", str(tmp_path / "cache"))


def test_layers_added_converts_only_complex_images(tmp_path, monkeypatch):
    monkeypatch.setattr(complex_sar, "default_folder", lambda: str(tmp_path / "cache"))
    path, _, _ = sicd(tmp_path / "a.ntf")
    plain = QgsRasterLayer(tiff(tmp_path / "plain.tif", bands=2, dtype=gdal.GDT_Float32)[0], "plain", "gdal")
    layer = QgsRasterLayer(path, "a", "gdal")
    assert complex_sar.layers_added([plain, layer], None) == []
    assert layer.bandCount() == 2

    class Bar:
        def __init__(self):
            self.infos = []

        def pushInfo(self, title, text):  # noqa: N802 (Qt's name)
            self.infos.append(text)

    bar = Bar()
    assert complex_sar.layers_added([plain, layer], "dB", bar) == [layer]
    assert plain.bandCount() == 2 and layer.bandCount() == 1
    assert bar.infos and "dB" in bar.infos[0]


def test_saved_project_reads_the_vrt_again(tmp_path):
    path, _, _ = sicd(tmp_path / "a.ntf")
    layer = QgsRasterLayer(path, "a", "gdal")
    convert_layer(layer, "dB", str(tmp_path / "cache"))
    project = QgsProject()
    project.addMapLayer(layer)
    assert project.write(str(tmp_path / "p.qgz"))
    again = QgsProject()
    assert again.read(str(tmp_path / "p.qgz"))
    (read,) = again.mapLayers().values()
    assert read.isValid() and read.bandCount() == 1 and read.customProperty(COMPLEX_SOURCE) == path
    assert read.renderer().type() == "singlebandgray"


def test_settings_file_entry():
    problems = []
    assert complex_images_of("dB", problems) == "dB"
    assert complex_images_of("Amplitude", problems) == "amplitude"
    assert complex_images_of(False, problems) is None
    assert complex_images_of(True, problems) == "dB"
    assert not problems
    assert complex_images_of("phase", problems) == "dB" and problems
    assert parse_config({})[0].complex_images == "dB"
    assert parse_config({"complexImages": False})[0].complex_images is None


def test_the_plugin_shows_a_sicd_added_in_any_way_as_db(tmp_path, monkeypatch):
    """Added like any layer (Add Layer, drag and drop): converted before the docks see it, its overviews from the dB."""
    from satellite_observation import classFactory
    from satellite_observation.qgis_io import overviews_io, settings
    from satellite_observation.tests.qgis_fakes import FakeIface

    monkeypatch.setattr(complex_sar, "default_folder", lambda: str(tmp_path / "cache"))
    asked = []
    monkeypatch.setattr(overviews_io, "layers_added", lambda layers, bar=None: asked.extend(layer.source() for layer in layers))
    config = tmp_path / "config.json"
    config.write_text("{}", encoding="utf-8")
    settings.set_config_source(str(config))
    settings.set_startup_layers_on(False)
    plugin = classFactory(FakeIface())
    plugin.initGui()
    try:
        path, _, _ = sicd(tmp_path / "a.ntf")
        layer = QgsRasterLayer(path, "a", "gdal")
        QgsProject.instance().addMapLayer(layer)
        assert layer.bandCount() == 1 and layer.customProperty(COMPLEX_SOURCE) == path
        assert asked == [layer.source()] and asked[0].endswith(".dB.vrt")
    finally:
        QgsProject.instance().removeAllMapLayers()
        plugin.unload()
        settings.set_config_source("")


def test_an_alpha_band_after_i_and_q_is_left_out(tmp_path):
    """I, Q and a third band (alpha): one dB band from I and Q, the alpha ignored (tsudo, 2026-11-08)."""
    mem = gdal.GetDriverByName("MEM").Create("", W, H, 3, gdal.GDT_Float32)
    _georef(mem)
    rng = np.random.default_rng(3)
    i, q = rng.normal(size=(H, W)).astype("f4"), rng.normal(size=(H, W)).astype("f4")
    mem.GetRasterBand(1).WriteArray(i)
    mem.GetRasterBand(2).WriteArray(q)
    mem.GetRasterBand(3).WriteArray(np.zeros((H, W), "f4"))
    path = str(tmp_path / "iqa.ntf")
    gdal.GetDriverByName("NITF").CreateCopy(path, mem, options=["ISUBCAT=I,Q,A", "ICAT=SAR"])
    assert outputs_of(gdal.Open(path)) == [Output((1, 2))]
    ds = gdal.Open(build_vrt(path, "dB", str(tmp_path / "cache")))
    assert ds.RasterCount == 1 and ds.GetRasterBand(1).GetMaskFlags() & gdal.GMF_ALPHA == 0
    assert np.allclose(ds.ReadAsArray(), 20 * np.log10(np.hypot(i, q)), atol=1e-4)
    # A GeoTIFF whose third band is an alpha band.
    tif, _ = tiff(tmp_path / "iqa.tif", bands=3, dtype=gdal.GDT_Float32, descriptions=("I", "Q"))
    assert outputs_of(gdal.Open(tif)) == [Output((1, 2))]
