"""The local GeoPackage: tables, geometries, dates, and that GDAL reads it as a GeoPackage."""
import sqlite3

import pytest

from satellite_observation.core import gpkg
from satellite_observation.core.gpkg import AREAS, OBSERVATIONS, TAXONOMY, Column, TableSpec, datetime_ms, datetime_text, decode_geometry, encode_point, encode_polygon, open_local


def test_geometries_round_trip():
    assert decode_geometry(encode_point(139.5, 35.25)) == (139.5, 35.25)
    rings = [[[0.0, 0.0], [1.0, 0.0], [1.0, 1.0], [0.0, 0.0]]]
    assert decode_geometry(encode_polygon(rings)) == rings
    assert decode_geometry(None) is None
    assert decode_geometry(b"XX") is None


def test_dates_round_trip():
    assert datetime_text(1759281825123) == "2025-10-01T01:23:45.123Z"
    assert datetime_ms("2025-10-01T01:23:45.123Z") == 1759281825123
    assert datetime_ms(None) is None


def test_tables_rows_and_upserts(tmp_path):
    path = str(tmp_path / "observations.gpkg")
    with open_local(path) as db:
        db.upsert(OBSERVATIONS, [
            {"OBS_ID": "a", "OBJECT_ID": "OBJ-1", "PRESENT": 1, "IMAGE_TIME": datetime_text(0), "geom": encode_point(139.7, 35.6)},
            {"OBS_ID": "b", "OBJECT_ID": "OBJ-1", "PRESENT": 0, "geom": encode_point(139.8, 35.7)},
        ])
        db.upsert(OBSERVATIONS, [{"OBS_ID": "a", "OBJECT_ID": "OBJ-2", "NOTE": "直した", "geom": encode_point(140, 36)}])
        rows = db.rows(OBSERVATIONS)
        assert [(r["OBS_ID"], r["OBJECT_ID"], r["NOTE"], r["geom"]) for r in rows] == [("a", "OBJ-2", "直した", (140.0, 36.0)), ("b", "OBJ-1", None, (139.8, 35.7))]
        assert db.delete(OBSERVATIONS, '"OBS_ID" = ?', ["b"]) == 1
        db.upsert(AREAS, [{"AREA_ID": "r", "KIND": "船舶", "geom": encode_polygon([[[0, 0], [1, 0], [1, 1], [0, 0]]])}])
        db.upsert(TAXONOMY, [{"ROW_KEY": "s\u0000船舶", "STORE": "s", "PATH": "船舶", "SORT_ORDER": 0}])
        assert db.rows(TAXONOMY)[0]["PATH"] == "船舶"
    con = sqlite3.connect(path)
    assert con.execute("PRAGMA application_id").fetchone()[0] == gpkg.APPLICATION_ID
    contents = dict(con.execute("SELECT table_name, data_type FROM gpkg_contents"))
    assert contents == {"observations": "features", "checked_areas": "features", "taxonomy": "attributes", "taxon_photos": "attributes"}
    assert con.execute("SELECT min_x, max_x FROM gpkg_contents WHERE table_name = 'observations'").fetchone() == (140.0, 140.0)


def test_adds_columns_a_newer_plugin_needs(tmp_path):
    path = str(tmp_path / "x.gpkg")
    old = TableSpec("t", None, (Column("K", "TEXT"),), key="K")
    with gpkg.GeoPackage(path) as db:
        db.ensure(old)
        db.upsert(old, [{"K": "1"}])
    new = TableSpec("t", None, (Column("K", "TEXT"), Column("EXTRA", "INTEGER")), key="K")
    with gpkg.GeoPackage(path) as db:
        db.upsert(new, [{"K": "2", "EXTRA": 5}])
        assert [(r["K"], r["EXTRA"]) for r in db.rows(new)] == [("1", None), ("2", 5)]


def test_gdal_reads_it_as_a_geopackage(tmp_path):
    ogr = pytest.importorskip("osgeo.ogr")
    ogr.UseExceptions()
    path = str(tmp_path / "observations.gpkg")
    with open_local(path) as db:
        db.upsert(OBSERVATIONS, [{"OBS_ID": "a", "OBJECT_ID": "OBJ-1", "CREATED_AT": datetime_text(1759281825123), "geom": encode_point(139.7, 35.6)}])
        db.upsert(AREAS, [{"AREA_ID": "r", "geom": encode_polygon([[[0, 0], [1, 0], [1, 1], [0, 1], [0, 0]]])}])
    ds = ogr.Open(path)
    assert ds.GetDriver().GetName() == "GPKG"
    layer = ds.GetLayerByName("observations")
    assert layer.GetGeomType() == ogr.wkbPoint
    assert layer.GetSpatialRef().GetAuthorityCode(None) == "4326"
    f = layer.GetNextFeature()
    assert f.GetField("OBS_ID") == "a"
    assert f.GetGeometryRef().GetX() == pytest.approx(139.7)
    assert f.GetFieldDefnRef("CREATED_AT").GetType() == ogr.OFTDateTime
    area = ds.GetLayerByName("checked_areas").GetNextFeature()
    assert area.GetGeometryRef().GetArea() == pytest.approx(1)
