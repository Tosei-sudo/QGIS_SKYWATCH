"""
簡易オルソ補正 in QGIS: an image on a flat ground corrected with a DEM and the
satellite's direction (from the arguments, the metadata or a .IMD file), and
the Processing algorithm.
"""
import math

import numpy as np
import pytest

pytest.importorskip("qgis.core")
from qgis.testing import start_app  # noqa: E402

APP = start_app()

from osgeo import gdal, osr  # noqa: E402
from qgis.core import QgsApplication, QgsProcessingContext, QgsProcessingFeedback, QgsProject, QgsRasterLayer  # noqa: E402

from satellite_observation.core.simple_ortho import SensorView  # noqa: E402
from satellite_observation.qgis_io import ortho_io as io  # noqa: E402


def write(path, array, gt, epsg=32654, no_data=None, kind=gdal.GDT_Byte, metadata=None):
    ds = gdal.GetDriverByName("GTiff").Create(str(path), array.shape[1], array.shape[0], 1, kind)
    ds.SetGeoTransform(gt)
    srs = osr.SpatialReference()
    srs.ImportFromEPSG(epsg)
    ds.SetProjection(srs.ExportToWkt())
    if metadata:
        ds.SetMetadata(metadata)
    band = ds.GetRasterBand(1)
    if no_data is not None:
        band.SetNoDataValue(no_data)
    band.WriteArray(array)
    ds = None
    return str(path)


def scene(tmp_path, dem_height=1000.0, **kw):
    """A 200 × 200 image of 10 m with a bright dot at (100, 100), and a flat DEM of 30 m pixels over it."""
    data = np.zeros((200, 200), np.uint8)
    data[100, 100] = 255
    image = write(tmp_path / "image.tif", data, [500000, 10, 0, 4000000, 0, -10], **kw)
    dem = write(tmp_path / "dem.tif", np.full((80, 80), dem_height, np.float32), [499800, 30, 0, 4000200, 0, -30], kind=gdal.GDT_Float32,
                no_data=-32768)
    return image, dem


def brightest(path):
    ds = gdal.Open(path)
    a = ds.GetRasterBand(1).ReadAsArray()
    y, x = np.unravel_index(int(np.argmax(a)), a.shape)
    return int(x), int(y)


def test_high_ground_moves_toward_the_satellite(tmp_path):
    image, dem = scene(tmp_path)
    # Seen from the east at 45°, a point 1000 m up shows 1000 m west of where it is: put back 100 px east.
    out = str(tmp_path / "o.tif")
    r = io.simple_orthorectify(image, dem, out, SensorView(90, 45), reference=0, resample="nearest")
    assert brightest(out) == (0, 0)  # beyond the east edge: the dot is gone
    assert r.coverage == 1 and r.largest == pytest.approx(1000)
    # From the south at 60°, 400 m above the reference: 231 m (23 px) south.
    out = str(tmp_path / "s.tif")
    io.simple_orthorectify(image, dem, out, SensorView(180, 60), reference=600, resample="nearest")
    x, y = brightest(out)
    assert x == 100 and abs((y - 100) * 10 - 400 / math.tan(math.radians(60))) <= 10
    # The reference left out: the mean DEM height, nothing moves.
    r = io.simple_orthorectify(image, dem, str(tmp_path / "m.tif"), SensorView(90, 45))
    assert r.reference == pytest.approx(1000) and brightest(str(tmp_path / "m.tif")) == (100, 100)


def test_east_shift_and_an_alpha_band_without_no_data(tmp_path):
    image, dem = scene(tmp_path, dem_height=300)
    out = str(tmp_path / "e.tif")
    io.simple_orthorectify(image, dem, out, SensorView(90, 45), reference=0, resample="nearest")
    assert brightest(out) == (130, 100)
    ds = gdal.Open(out)
    assert ds.RasterCount == 2 and ds.GetRasterBand(2).GetColorInterpretation() == gdal.GCI_AlphaBand
    alpha = ds.GetRasterBand(2).ReadAsArray()
    # The westmost 30 columns read beyond the image's west edge: transparent.
    assert (alpha[:, :29] == 0).all() and (alpha[:, 31:] == 255).all()


def test_where_the_dem_has_no_value_nothing_moves(tmp_path):
    image, dem = scene(tmp_path, dem_height=-32768)
    with pytest.raises(io.OrthoError, match="標高データなし"):
        io.simple_orthorectify(image, dem, str(tmp_path / "x.tif"), SensorView(90, 45))
    r = io.simple_orthorectify(image, dem, str(tmp_path / "y.tif"), SensorView(90, 45), reference=0, resample="nearest")
    assert r.coverage == 0 and brightest(str(tmp_path / "y.tif")) == (100, 100)


def test_the_direction_from_metadata_or_an_imd_file(tmp_path):
    image, _ = scene(tmp_path, metadata={"SATELLITE_AZIMUTH": "101.2", "OFF_NADIR": "4.5"})
    assert io.sensor_view_of(image) == SensorView(101.2, 85.5)
    plain, _ = scene(tmp_path / ".." / tmp_path.name)
    (tmp_path / "image.IMD").write_text("BEGIN_GROUP = IMAGE_1\n\tmeanSatAz = 213.4;\n\tmeanSatEl = 71.6;\nEND_GROUP = IMAGE_1\n")
    assert io.sensor_view_of(plain) == SensorView(213.4, 71.6)


def test_the_algorithm(tmp_path):
    from satellite_observation.processing_provider.ortho_algorithm import default_inputs
    from satellite_observation.processing_provider.provider import SatObsProvider

    provider = SatObsProvider()
    QgsApplication.processingRegistry().addProvider(provider)
    try:
        algorithm = QgsApplication.processingRegistry().algorithmById("satobs:simple_ortho")
        image_path, dem_path = scene(tmp_path, dem_height=300)
        image, dem = QgsRasterLayer(image_path, "image"), QgsRasterLayer(dem_path, "dem")
        QgsProject.instance().addMapLayers([image, dem])
        assert default_inputs([dem, image], image) == {"INPUT": image.id(), "DEM": dem.id()}
        out = str(tmp_path / "result.tif")
        context = QgsProcessingContext()
        context.setProject(QgsProject.instance())
        context.addLayerToLoadOnCompletion(out, QgsProcessingContext.LayerDetails("result", QgsProject.instance(), "OUTPUT"))
        feedback = QgsProcessingFeedback()
        results, ok = algorithm.run({"INPUT": image.id(), "DEM": dem.id(), "AZIMUTH": 90, "ELEVATION": 45, "REFERENCE": 0, "RESAMPLE": 1,
                                     "OUTPUT": out}, context, feedback)
        assert ok and brightest(out) == (130, 100)
        layer = QgsRasterLayer(out, "x")
        context.layerToLoadOnCompletionDetails(out).postProcessor().postProcessLayer(layer, context, feedback)
        assert layer.name() == "image_ortho"
        # No angles and none in the metadata: refused. The refused run's exception keeps this frame alive until Python exits, so
        # nothing in it may still hold a layer GDAL would have to close after its own clean-up.
        del layer
        _, ok = algorithm.run({"INPUT": image.id(), "DEM": dem.id(), "OUTPUT": str(tmp_path / "x.tif")}, context, QgsProcessingFeedback())
        assert not ok
    finally:
        QgsApplication.processingRegistry().removeProvider(provider)
        QgsProject.instance().removeAllMapLayers()
