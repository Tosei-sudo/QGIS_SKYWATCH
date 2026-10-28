"""影から高さ: the viewer's shadow-height.test.ts cases on core.shadow."""
from datetime import datetime, timezone

import pytest

from satellite_observation.core.plan.orbit import sun_angles
from satellite_observation.core.shadow import (
    Sun, format_metres, height_from_shadow, shadow_azimuth, snap_to_ray, step_toward, sun_from_metadata,
)


def test_reads_the_sun_from_the_items_satellite_products_write():
    assert sun_from_metadata({"SUN_ELEVATION": "54.3", "SUN_AZIMUTH": "141.2"}.items()) == Sun(54.3, 141.2, "SUN_ELEVATION")
    s = sun_from_metadata({"MEANSUNEL": "60.1", "MEANSUNAZ": "170.5"}.items())
    assert (s.elevation, s.azimuth) == (60.1, 170.5)
    s = sun_from_metadata({"sun_elevation": "33", "sun_azimuth": "200"}.items())
    assert (s.elevation, s.azimuth) == (33, 200)
    s = sun_from_metadata({"NITF_USE00A_SUN_EL": "45.5", "NITF_USE00A_SUN_AZ": "123.0"}.items())
    assert (s.elevation, s.azimuth) == (45.5, 123)
    # A zenith angle, and no azimuth.
    s = sun_from_metadata({"MEAN_SOLAR_ZENITH_ANGLE": "30"}.items())
    assert (s.elevation, s.azimuth) == (60, None)
    # A Maxar .IMD as GDAL's IMD domain has it.
    s = sun_from_metadata({"IMAGE_1.meanSunEl": "41.2", "IMAGE_1.meanSunAz": "160.4"}.items())
    assert (s.elevation, s.azimuth) == (41.2, 160.4)
    # NITF's "unknown", and nothing at all.
    assert sun_from_metadata({"NITF_USE00A_SUN_EL": "999.9"}.items()) is None
    assert sun_from_metadata({"AREA_OR_POINT": "Area"}.items()) is None


def test_works_out_the_height_from_the_length_and_the_elevation():
    assert height_from_shadow(10, 45) == pytest.approx(10)
    assert height_from_shadow(30, 30) == pytest.approx(17.32, abs=0.005)
    assert shadow_azimuth(150) == 330
    assert shadow_azimuth(300) == 120


def test_snaps_the_tip_onto_the_direction_of_the_shadow_never_behind_the_foot():
    assert snap_to_ray((0, 0), (0, 1), (3, 5)) == (0, 5)
    assert snap_to_ray((10, 10), (1, 1), (12, 10)) == pytest.approx((11, 11))
    assert snap_to_ray((0, 0), (0, 1), (3, -5)) == (0, 0)


def test_steps_toward_an_azimuth():
    lon, lat = step_toward((139.7, 35.7), 90, 1000)
    assert lat == pytest.approx(35.7, abs=1e-6) and lon > 139.7
    assert step_toward((0, 0), 0, 111_320)[1] == pytest.approx(1, abs=1e-6)


def ms(*args):
    return datetime(*args, tzinfo=timezone.utc).timestamp() * 1000


def test_computes_where_the_sun_is_from_a_time_and_a_place():
    # Tokyo at noon on the June solstice: nearly overhead, a little west of south.
    elevation, azimuth = sun_angles((139.69, 35.69), ms(2024, 6, 21, 3, 0))
    assert 76 < elevation < 78.5 and 175 < azimuth < 215
    # Greenwich at noon at the March equinox: about 90° − 51.5°, due south.
    elevation, azimuth = sun_angles((0, 51.48), ms(2024, 3, 20, 12, 0))
    assert elevation == pytest.approx(38.5, abs=0.5) and abs(azimuth - 180) < 5
    # Morning in Tokyo: in the east.
    assert sun_angles((139.69, 35.69), ms(2024, 6, 20, 22, 0))[1] < 100


def test_formats_lengths():
    assert format_metres(12.34) == "12.3 m"
    assert format_metres(1234.5) == "1.234 km"
