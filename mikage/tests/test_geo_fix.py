"""クイック幾何補正's arithmetic and settings (core/geo_fix.py): the satellite's direction, the fits, the kept corrections."""
import math

import numpy as np
import pytest

from mikage.core.config import parse_config
from mikage.core.esri import EsriClient
from mikage.core.geo_fix import (
    DEFAULT_FIX_FIELDS, GeoFix, LookGeometry, TiePoint, apply_affine, attributes_of, compass, delete_fix, describe_ground, fit_ground,
    fit_rpc, fix_from_json, fix_of_row, heading_of_pass, kind_name, load_fixes, look_from_text, newest_for, points_of, rpc_pixel,
    save_fix, toward_from_columns,
)
from mikage.tests.fake_esri import FakeEsri, FakeLayer

URL = "https://gis.example/arcgis/rest/services/MIKAGE/FeatureServer/7"
S, D, N = "esriFieldTypeString", "esriFieldTypeDate", "esriFieldTypeDouble"


def test_the_direction_from_a_sicd():
    text = "<SCPCOA><SideOfTrack>R</SideOfTrack><GrazeAng>55.1</GrazeAng><IncidenceAng>34.9</IncidenceAng><AzimAng>262.5</AzimAng></SCPCOA>"
    look = look_from_text(text)
    assert look == LookGeometry(toward=262.5, incidence=34.9, source="file")
    assert look.text() == "入射角 34.9°、衛星は西（262°）"


def test_the_direction_from_a_makers_items():
    # Heading and side: right-looking sees to the right of the track, the satellite on its left.
    look = look_from_text('{"incidence_near": 28.0, "incidence_far": 41.0, "heading": 350.0, "look_side": "right"}')
    assert (look.toward, look.near, look.far, look.incidence) == (260.0, 28.0, 41.0, 34.5)
    assert look_from_text('incidence_center = 30\nplatform_heading = 190\nlook_side = left').toward == 280.0
    # Only the pass: the heading of a sun-synchronous orbit at the latitude.
    asc = look_from_text("ORBIT_DIRECTION = ASCENDING\nINCIDENCE_ANGLE = 39.2", lat=35)
    assert asc.incidence == 39.2 and 255 < asc.toward < 265
    desc = look_from_text("pass: DESCENDING, incidenceAngleDegrees: 25", lat=35)
    assert 95 < desc.toward < 105
    assert 349 < heading_of_pass("A", 35) < 352 and 188 < heading_of_pass("D", 35) < 191
    # Without an angle or without a direction: none.
    assert look_from_text("heading = 350") is None and look_from_text("incidence_angle = 35") is None
    assert look_from_text("") is None and look_from_text("incidence_angle = 135\nheading = 10") is None


def test_the_incidence_across_the_columns_and_the_compass():
    look = LookGeometry(toward=80, incidence=35, near=30, far=40)
    assert look.incidence_at(0) == 30 and look.incidence_at(1) == 40
    assert np.allclose(look.incidence_at(np.array([0.5, 2.0])), [35, 42.5])
    assert LookGeometry(toward=10, incidence=33).incidence_at(np.array([0.2, 0.9])).tolist() == [33, 33]
    assert [compass(a) for a in (0, 44, 46, 91, 180, 225, 270, 359)] == ["北", "北東", "北東", "東", "南", "南西", "西", "北"]
    # The radar's columns go east (range away from it): the satellite is to the west.
    assert toward_from_columns(10, 0) == 270 and toward_from_columns(0, -10) == 0 and toward_from_columns(0, 0) is None


def test_the_tie_points_fit_a_shift_a_similarity_and_an_affine():
    assert fit_ground([], []) == (0.0, 1.0, 0.0, 0.0, 0.0, 1.0)
    assert fit_ground([(10, 20)], [(13, 16)]) == (3.0, 1.0, 0.0, -4.0, 0.0, 1.0)
    # Two points: rotated by 90° and scaled by 2 about the origin, then shifted.
    t = fit_ground([(0, 0), (10, 0)], [(5, 5), (5, 25)])
    assert np.allclose(apply_affine(t, 10, 0), (5, 25)) and np.allclose(apply_affine(t, 0, 10), (-15, 5))
    # An affine through 4 points (least squares), and 3 in a line fall back to a similarity.
    truth = (7, 1.01, 0.02, -3, -0.01, 0.99)
    src = [(0, 0), (1000, 0), (0, 1000), (900, 800)]
    dst = [apply_affine(truth, *p) for p in src]
    assert np.allclose(fit_ground(src, dst), truth)
    line = fit_ground([(0, 0), (10, 0), (20, 0)], [(1, 0), (11, 0), (21, 0)])
    assert np.allclose(line, (1, 1, 0, 0, 0, 1))
    assert kind_name(0) == "なし" and kind_name(1) == "平行移動" and kind_name(2) == "平行移動・回転・縮尺" and kind_name(9) == "アフィン"
    assert describe_ground((3.04, 1, 0, -4.0, 0, 1)) == "東 +3.0 m、北 -4.0 m"
    assert "回転" in describe_ground(t) and "縮尺" in describe_ground(t)


def test_an_rpc_fit_gives_the_pixels_back_as_gdal_counts_them():
    lon, lat, h = np.meshgrid(np.linspace(139.0, 139.2, 9), np.linspace(35.0, 35.1, 9), np.linspace(0, 800, 4), indexing="ij")
    # A placing that leans with the height (as a SAR image's): x east, y south, the height pulling to the west.
    x = (lon - 139.0) * 5000 - h * 0.12 + 0.5
    y = (35.1 - lat) * 5000 + 0.5 + 0.001 * (lon - 139.0) * 5000 * (35.1 - lat) * 5000 / 100
    fit = fit_rpc(lon, lat, h, x, y)
    assert fit.rms < 1e-6 and fit.largest < 1e-5
    px, py = rpc_pixel(fit.items, lon, lat, h)
    assert np.allclose(px, x, atol=1e-5) and np.allclose(py, y, atol=1e-5)
    assert fit.items["LINE_DEN_COEFF"].split()[0] == "1" and len(fit.items["SAMP_NUM_COEFF"].split()) == 20
    with pytest.raises(ValueError):
        fit_rpc([1, 2], [1, 2], [0, 0], [0, 1], [0, 1])


def test_the_correction_as_json_and_as_a_row():
    fix = GeoFix(image_id="IMG-1", image_file="a/b.tif", points=(TiePoint(10.5, 20.25, 139.1234567891, 35.5),), terrain=True,
                 toward=262.0, incidence=34.0, by="tsudo", at=1_700_000_000_000, residual=None)
    back = fix_from_json(fix.to_json())
    # The places are kept to 8 decimals (about a millimetre).
    assert back.points == (TiePoint(10.5, 20.25, 139.12345679, 35.5),) and back.at == fix.at and back.look() == LookGeometry(toward=262.0, incidence=34.0, source="typed")
    assert fix.summary() == "地形（DEM）・目印 1 点（平行移動）" and GeoFix("x", terrain=False).is_empty()
    row = attributes_of(fix, DEFAULT_FIX_FIELDS)
    assert row["USE_DEM"] == 1 and row["TIE_POINTS"] == "[[10.5,20.25,139.12345679,35.5]]"
    assert fix_of_row({**row, "OBJECTID": 4}, DEFAULT_FIX_FIELDS) == GeoFix(**{**fix.__dict__, "points": (TiePoint(10.5, 20.25, 139.12345679, 35.5),),
                                                                                "oid": 4})
    assert points_of("[[1,2,3,4],[1,2,999,4],\"x\",[1,2]]") == (TiePoint(1, 2, 3, 4),) and points_of("nope") == ()
    assert fix_from_json("[]") is None and fix_from_json("{") is None


def test_the_settings():
    config, problems = parse_config({"dem": {"url": "https://gis.example/arcgis/rest/services/DEM/ImageServer/", "geoidHeight": 36.7,
                                             "maxSize": 2048, "renderingRule": {"rasterFunction": "None"}},
                                     "imageCorrections": {"url": URL, "fields": {"points": "POINTS_TEXT"}}})
    assert problems == []
    assert config.dem.url.endswith("/ImageServer") and config.dem.geoid_height == 36.7 and config.dem.max_size == 2048
    assert config.image_corrections.fields["points"] == "POINTS_TEXT" and config.image_corrections.fields["imageId"] == "IMAGE_ID"
    assert parse_config({"dem": "C:/dem/japan.tif"})[0].dem.file == "C:/dem/japan.tif"
    assert parse_config({})[0].dem is None and parse_config({})[0].image_corrections is None
    _, problems = parse_config({"dem": {"url": "https://x/MapServer", "geoidHeight": 500}, "imageCorrections": "https://x/MapServer/0"})
    assert len(problems) == 3
    config, problems = parse_config({"dem": {"file": "dem.tif", "geoidHeight": 500, "maxSize": 10}})
    assert len(problems) == 2 and config.dem.geoid_height == 0 and config.dem.max_size == 4096


def esri():
    fake = FakeEsri()
    table = fake.add_layer(URL, FakeLayer({"IMAGE_ID": S, "IMAGE_FILE": S, "TIE_POINTS": S, "USE_DEM": "esriFieldTypeSmallInteger",
                                           "LOOK_AZIMUTH": N, "INCIDENCE_ANGLE": N, "RESIDUAL_M": N, "CORRECTED_BY": S, "CORRECTED_AT": D}, ""))
    source = parse_config({"imageCorrections": URL})[0].image_corrections
    return EsriClient(fake), table, source


def test_a_correction_is_shared_one_row_per_image_file():
    client, table, source = esri()
    first = GeoFix("IMG-1", "a.tif", points=(TiePoint(1, 2, 139, 35),), by="sato", at=1000)
    saved = save_fix(client, source, first)
    assert saved.oid == 1 and len(table.rows) == 1
    # Saved again (another person, new points): over the same row.
    again = save_fix(client, source, GeoFix("IMG-1", "A.TIF", points=(TiePoint(1, 2, 139, 35), TiePoint(5, 6, 139.1, 35.1)), by="ito", at=2000))
    assert again.oid == 1 and len(table.rows) == 1 and table.rows[0]["attributes"]["CORRECTED_BY"] == "ito"
    save_fix(client, source, GeoFix("IMG-1", "b.tif", terrain=False, at=1500))
    save_fix(client, source, GeoFix("IMG-2", "", at=1))
    read = load_fixes(client, source, ["IMG-1"])
    assert [(x.image_file, len(x.points)) for x in read] == [("A.TIF", 2), ("b.tif", 0)]
    assert newest_for(read, "IMG-1", "a.tif").by == "ito" and newest_for(read, "IMG-1", "c.tif") is None
    assert newest_for(load_fixes(client, source, ["IMG-2"]), "IMG-2", "x.tif").image_id == "IMG-2"
    assert delete_fix(client, source, GeoFix("IMG-1", "a.tif")) == 1 and len(table.rows) == 2
    assert load_fixes(client, source, []) == []


def test_a_table_without_the_needed_attributes_is_said():
    fake = FakeEsri()
    fake.add_layer(URL, FakeLayer({"IMAGE_ID": S}, ""))
    source = parse_config({"imageCorrections": URL})[0].image_corrections
    from mikage.core.esri import EsriError

    with pytest.raises(EsriError, match="TIE_POINTS"):
        load_fixes(EsriClient(fake), source, ["x"])


def test_the_metres_frame_is_consistent():
    from mikage.core.geo_fix import Frame

    f = Frame(139.0, 35.0)
    e, n = f.metres(139.01, 35.01)
    assert math.isclose(float(n), 1105.74) and math.isclose(float(e), 0.01 * 111_320 * math.cos(math.radians(35)))
    lon, lat = f.degrees(e, n)
    assert math.isclose(float(lon), 139.01) and math.isclose(float(lat), 35.01)
