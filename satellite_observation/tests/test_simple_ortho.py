"""簡易オルソ補正 (core.simple_ortho): the viewer's simple-ortho.test.ts (the parts without its grids)."""
import math

import numpy as np
import pytest

from satellite_observation.core.simple_ortho import SensorView, displacement, is_imd_name, sample, sensor_view_from_text


def test_points_the_displacement_away_from_the_satellite():
    e, n = displacement(SensorView(90, 45))
    assert e == pytest.approx(-1) and n == pytest.approx(0, abs=1e-9)
    assert displacement(SensorView(0, 90))[1] == pytest.approx(0, abs=1e-9)
    # Seen from the south at 60°: high ground shows to the north, 1 / tan 60° m per metre.
    assert displacement(SensorView(180, 60))[1] == pytest.approx(1 / math.tan(math.radians(60)))


def test_reads_a_digitalglobe_imd_file():
    imd = "BEGIN_GROUP = IMAGE_1\n\tmeanSunAz = 150.2;\n\tmeanSatAz = 213.4;\n\tmeanSatEl = 71.6;\n\tmeanOffNadirViewAngle = 16.9;\nEND_GROUP = IMAGE_1\n"
    assert sensor_view_from_text(imd) == SensorView(213.4, 71.6)
    assert is_imd_name("16FEB01-P1BS.IMD") and not is_imd_name("a.tif")


def test_reads_gdal_metadata_and_off_nadir_angles():
    xml = '<GDALMetadata><Item name="IMAGE_1.meanSatAz" domain="IMD">45.5</Item><Item name="IMAGE_1.meanSatEl" domain="IMD">80</Item></GDALMetadata>'
    assert sensor_view_from_text(xml) == SensorView(45.5, 80)
    assert sensor_view_from_text('{"satellite_azimuth": 101.2, "view_angle": 4.5}') == SensorView(101.2, 85.5)
    assert sensor_view_from_text('{"satellite_azimuth": -90, "off_nadir": -10}') == SensorView(270, 80)


def test_gives_none_without_both_angles():
    assert sensor_view_from_text("meanSunAz = 150;") is None
    assert sensor_view_from_text("meanSatAz = 150;") is None


def test_samples_bilinear_and_nearest():
    v = np.array([[0.0, 10], [20, 30]])
    x, y = np.array([0.5, 1.0, 1.5, 3.0]), np.array([0.5, 1.0, 1.5, 0.5])
    assert sample(v, x, y)[:3].tolist() == [0, 15, 30] and np.isnan(sample(v, x, y)[3])
    assert sample(v, np.array([0.9, 1.1]), np.array([0.2, 1.9]), "nearest").tolist() == [0, 30]
    assert sample(np.array([[np.nan, 1.0]]), np.array([1.0]), np.array([0.5]), fill=-1).tolist() == [-1]
