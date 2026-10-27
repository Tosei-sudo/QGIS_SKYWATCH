"""
The image catalog's core, with the inputs and expectations of the viewer's
catalog.test.ts, catalog-similar.test.ts, catalog-watch.test.ts and
local-paths.test.ts, and searches against the fake Esri server.
"""
import json
import math
import os
from datetime import datetime, timezone

import pytest

from satellite_observation.core.catalog import catalog_of
from satellite_observation.core.catalog.paths import (
    PathError, PathMapping, find_file, mapping_for, path_mapping_of, path_parts, resolve_path, url_for,
)
from satellite_observation.core.catalog.query import EMPTY_SEARCH, day_range, local_day, where_of
from satellite_observation.core.catalog.search import (
    catalog_info, list_columns, read_catalog_layer, record_of, search_catalog, sensor_view_of, sensors_of, sort_records,
    source_of,
)
from satellite_observation.core.catalog.similar import DEFAULT_TOLERANCE, SimilarTolerance, azimuth_difference, rank_similar, similar_search
from satellite_observation.core.catalog.watch import add_areas, areas_of, check_watch, conditions_text, join_areas, new_watch, since_condition, watch_query
from satellite_observation.core.catalog.watch_store import WatchStore, check_due
from satellite_observation.core.config import parse_config
from satellite_observation.core.esri import EsriClient
from satellite_observation.core.gpkg import GeoPackage
from satellite_observation.core.util import utc_text
from satellite_observation.tests.fake_esri import FakeEsri, FakeLayer

ENTRY = {
    "label": "衛星画像",
    "url": "https://gis.example.com/arcgis/rest/services/Catalog/FeatureServer/0",
    "fields": {"id": "IMG_ID", "acquired": "ACQ_TIME", "registered": "REG_TIME", "systemRegistered": "SYS_TIME", "sensor": "SENSOR",
               "angle": "OFF_NADIR", "azimuth": "AZIMUTH", "source": ["COG_URL", "FILE_PATH"]},
    "labels": {"angle": "オフナディア角"},
    "columns": [{"field": "CLOUD", "label": "雲量"}, "ORBIT"],
    "where": "STATUS = 'OK'",
}
CATALOG = catalog_of(ENTRY, [], 0)


def utc(*args) -> int:
    return int(datetime(*args, tzinfo=timezone.utc).timestamp() * 1000)


def record(attrs, catalog=CATALOG):
    return record_of({"attributes": attrs}, catalog.fields)


# --------------------------------------------------------------------------- catalog.test.ts


def test_catalog_of_reads_the_attribute_names_by_role():
    problems = []
    c = catalog_of(ENTRY, problems, 0)
    assert problems == []
    assert (c.label, c.url, c.fields, c.labels, c.columns, c.where, c.max_results, c.angle_kind, c.orbit_height) == (
        "衛星画像", ENTRY["url"], ENTRY["fields"], {"angle": "オフナディア角"},
        [{"field": "CLOUD", "label": "雲量"}, {"field": "ORBIT", "label": "ORBIT"}], "STATUS = 'OK'", 500, "offNadir", 600)


def test_catalog_needs_a_layer_url_and_a_source_field():
    problems = []
    assert catalog_of({**ENTRY, "url": "https://gis.example.com/arcgis/rest/services/Catalog/FeatureServer"}, problems, 0) is None
    assert catalog_of({"url": ENTRY["url"], "fields": {"id": "ID"}}, problems, 1) is None
    c = catalog_of({"url": ENTRY["url"], "fields": {"source": "PATH", "sensor": "bad name;"}}, problems, 2)
    assert c.label == "カタログ 3" and c.fields["source"] == ["PATH"]
    assert len(problems) == 3


def test_catalog_keys_in_the_settings_file():
    config, problems = parse_config({"imageCatalogs": [ENTRY, {"url": "x"}], "watchInterval": 5,
                                     "pathMappings": [{"prefix": "Z:", "label": "Z", "url": "./z/"}]})
    assert [c.label for c in config.image_catalogs] == ["衛星画像"]
    assert len(problems) == 1
    assert config.watch_interval == 5
    assert config.path_mappings == [PathMapping("Z:", "Z", "./z/")]
    _, problems = parse_config({"watchInterval": 0, "pathMappings": {}})
    assert problems == ["watchInterval は新着画像を確認する間隔（1〜1440 分）で指定", "pathMappings は配列で指定"]


def test_source_of_tells_urls_from_local_paths():
    assert source_of("https://bucket.example.com/a.tif") == source_of(" https://bucket.example.com/a.tif ")
    assert source_of("https://bucket.example.com/a.tif").kind == "url"
    for path in ("\\\\nas\\img\\a.tif", "C:\\data\\a.tif", "file:///mnt/img/a.tif"):
        assert source_of(path).kind == "path" and source_of(path).value == path
    assert source_of("") is None and source_of(None) is None


def test_records_take_the_first_source_with_a_value_and_sort_with_empty_last():
    r = record({"IMG_ID": "A1", "ACQ_TIME": 1700000000000, "SENSOR": "S1", "OFF_NADIR": "12.5", "COG_URL": None, "FILE_PATH": "\\\\nas\\a.tif"})
    assert (r.id, r.sensor, r.angle, r.azimuth, r.source.kind, r.source.value) == ("A1", "S1", 12.5, None, "path", "\\\\nas\\a.tif")
    records = [record({"IMG_ID": "old", "ACQ_TIME": "2026-01-01T00:00:00Z"}), record({"IMG_ID": "none"}), record({"IMG_ID": "new", "ACQ_TIME": utc(2026, 6, 1)})]
    assert [r.id for r in sort_records(records, "acquired", True)] == ["new", "old", "none"]
    assert [r.id for r in sort_records(records, "acquired", False)] == ["old", "new", "none"]
    # Other columns: numbers as numbers, text in natural order.
    clouds = [record({"IMG_ID": f"IMG-{n}", "CLOUD": c}) for n, c in ((10, 5), (2, 40), (1, None))]
    assert [r.id for r in sort_records(clouds, "id", False)] == ["IMG-1", "IMG-2", "IMG-10"]
    assert [r.id for r in sort_records(clouds, "column:CLOUD", True)] == ["IMG-2", "IMG-10", "IMG-1"]


def test_text_times_without_a_zone_are_utc():
    from satellite_observation.core.util import time_of

    assert time_of("2026-10-01 01:02:03") == utc(2026, 10, 1, 1, 2, 3)
    assert math.isnan(time_of("unknown"))


def test_days_are_written_for_the_field_type():
    start = local_day("2026-10-01")
    first, last = day_range("T", "esriFieldTypeDate", "2026-10-01", "2026-10-31")
    assert first == f"T >= TIMESTAMP '{utc_text(start)}'"
    assert last == f"T < TIMESTAMP '{utc_text(local_day('2026-11-01'))}'"
    assert day_range("T", "esriFieldTypeString", "2026-10-01", "2026-10-31") == ["T >= '2026-10-01'", "T < '2026-11-01'"]
    assert day_range("T", "esriFieldTypeDateOnly", "", "2026-12-31") == ["T < DATE '2027-01-01'"]
    assert day_range("T", "esriFieldTypeDouble", "2026-10-01", "") == [f"T >= {start}"]


def test_the_conditions_are_joined():
    s = EMPTY_SEARCH.with_(acquired_from="2026-10-01", sensor="O'Neil", max_angle=20, where="CLOUD < 10")
    assert where_of(CATALOG, {"ACQ_TIME": "esriFieldTypeString"}, s) == \
        "(STATUS = 'OK') AND ACQ_TIME >= '2026-10-01' AND SENSOR = 'O''Neil' AND OFF_NADIR <= 20 AND (CLOUD < 10)"
    plain = catalog_of({**ENTRY, "where": ""}, [], 0)
    assert where_of(plain, {}, EMPTY_SEARCH) == "1=1"
    assert where_of(plain, {"SYS_TIME": "esriFieldTypeString"}, EMPTY_SEARCH.with_(system_registered_to="2026-10-31", azimuth_from=90, azimuth_to=180)) == \
        "SYS_TIME < '2026-11-01' AND AZIMUTH >= 90 AND AZIMUTH <= 180"
    # A range through north.
    assert where_of(plain, {}, EMPTY_SEARCH.with_(azimuth_from=350, azimuth_to=10)) == "(AZIMUTH >= 350 OR AZIMUTH <= 10)"


def test_sensor_view_turns_the_angle_into_an_elevation():
    def r(angle, azimuth):
        return record({"OFF_NADIR": angle, "AZIMUTH": azimuth, "COG_URL": "https://x/a.tif"})

    assert sensor_view_of(r(10, 120.5), CATALOG) == {"azimuth": 120.5, "elevation": 79.05}
    assert sensor_view_of(r(0, -10), CATALOG) == {"azimuth": 350, "elevation": 90}
    assert sensor_view_of(r(10, 0), catalog_of({**ENTRY, "orbitHeight": 500}, [], 0))["elevation"] == pytest.approx(79.21, abs=0.05)
    assert sensor_view_of(r(30, 90), catalog_of({**ENTRY, "angleKind": "incidence"}, [], 0)) == {"azimuth": 90, "elevation": 60}
    assert sensor_view_of(r(70, 90), catalog_of({**ENTRY, "angleKind": "elevation"}, [], 0)) == {"azimuth": 90, "elevation": 70}
    assert sensor_view_of(r(10, None), CATALOG) is None
    assert sensor_view_of(r(None, 90), CATALOG) is None
    assert sensor_view_of(r(80, 90), CATALOG) is None
    problems = []
    assert catalog_of({**ENTRY, "angleKind": "incidence", "orbitHeight": 520}, problems, 0).orbit_height == 520
    catalog_of({**ENTRY, "angleKind": "nadir", "orbitHeight": -1}, problems, 0)
    assert len(problems) == 2


def test_the_list_columns_and_the_information_rows():
    assert [c.label for c in list_columns(CATALOG)] == ["画像ID", "撮像日時", "登録日時", "システム登録日時", "センサー", "オフナディア角", "方位角", "雲量", "ORBIT"]
    r = record({"IMG_ID": "A", "SENSOR": "S1", "OFF_NADIR": 10, "AZIMUTH": 120.5, "CLOUD": 5})
    assert catalog_info(r, CATALOG) == [("カタログ", "衛星画像"), ("画像ID", "A"), ("センサー", "S1"), ("オフナディア角", "10°"), ("方位角", "120.5°"), ("雲量", "5")]


# --------------------------------------------------------------------------- searching the fake service

URL = "https://example.com/arcgis/rest/services/Images/FeatureServer/0"
TYPES = {"IMG_ID": "esriFieldTypeString", "ACQ": "esriFieldTypeDate", "SENSOR": "esriFieldTypeString", "URL": "esriFieldTypeString"}


def ring(x0, y0, x1, y1):
    return {"rings": [[[x0, y0], [x0, y1], [x1, y1], [x1, y0], [x0, y0]]]}


def fake_catalog(pagination=True):
    fake = FakeEsri()
    layer = fake.add_layer(URL, FakeLayer(TYPES, "esriGeometryPolygon", pagination=pagination))
    layer.add({"IMG_ID": "OLD", "ACQ": utc(2026, 8, 1), "SENSOR": "S1", "URL": "https://x/old.tif"}, ring(139.6, 35.6, 139.9, 35.75))
    layer.add({"IMG_ID": "NEW", "ACQ": utc(2026, 10, 1), "SENSOR": "S2", "URL": "\\\\nas\\img\\new.tif"}, ring(139.6, 35.6, 139.9, 35.75))
    layer.add({"IMG_ID": "FAR", "ACQ": utc(2026, 9, 1), "SENSOR": "S1", "URL": "https://x/far.tif"}, ring(10, 10, 11, 11))
    catalog = catalog_of({"label": "画像", "url": URL, "fields": {"id": "IMG_ID", "acquired": "ACQ", "sensor": "SENSOR", "source": "URL"}}, [], 0)
    return fake, catalog


def test_search_lists_newest_first_in_the_view_and_counts_all():
    fake, catalog = fake_catalog()
    client = EsriClient(fake)
    layer = read_catalog_layer(client, catalog, None)
    assert layer.types["ACQ"] == "esriFieldTypeDate" and layer.paging
    result = search_catalog(client, catalog, layer, EMPTY_SEARCH, [139, 35, 140, 36], None)
    # The service orders them (the fake keeps its own order); FAR is out of view.
    assert [r.id for r in result.records] == ["OLD", "NEW"] and result.total == 2
    asked = [form for url, form in fake.asked if url.endswith("/query")]
    assert asked[0]["returnCountOnly"] == "true" and json.loads(asked[0]["geometry"])["xmin"] == 139
    assert asked[1]["orderByFields"] == "ACQ DESC" and asked[1]["resultRecordCount"] == "500" and asked[1]["outSR"] == "4326"
    # A sensor narrows it; the record knows where the image is.
    result = search_catalog(client, catalog, layer, EMPTY_SEARCH.with_(sensor="S2"), None, None)
    assert [(r.id, r.source.kind) for r in result.records] == [("NEW", "path")] and result.total == 1


def test_a_service_that_cannot_page_is_asked_plainly_and_sorted_here():
    fake, catalog = fake_catalog(pagination=False)
    client = EsriClient(fake)
    layer = read_catalog_layer(client, catalog, None)
    result = search_catalog(client, catalog, layer, EMPTY_SEARCH, None, None)
    assert [r.id for r in result.records] == ["NEW", "FAR", "OLD"]
    assert not any("orderByFields" in form for _, form in fake.asked)


def test_sensors_come_from_distinct_values_or_none():
    fake, catalog = fake_catalog()
    client = EsriClient(fake)
    assert sensors_of(client, catalog, None) == ["S1", "S2"]
    fake.fail_next = {"error": {"code": 400, "message": "no distinct"}}
    assert sensors_of(client, catalog, None) == []


# --------------------------------------------------------------------------- catalog-similar.test.ts

SIMILAR = catalog_of({"url": "https://gis.example.com/arcgis/rest/services/Catalog/FeatureServer/0",
                      "fields": {"id": "IMG_ID", "acquired": "ACQ", "sensor": "SENSOR", "angle": "OFF_NADIR", "azimuth": "AZIMUTH", "source": "URL"}}, [], 0)


def srecord(attrs):
    return record(attrs, SIMILAR)


def test_similar_search_asks_for_the_same_sensor_and_angles_within_the_tolerances():
    reference = srecord({"IMG_ID": "A", "SENSOR": "S1", "OFF_NADIR": 12, "AZIMUTH": 100, "URL": "https://x/a.tif"})
    search = similar_search(SIMILAR, reference, DEFAULT_TOLERANCE)
    assert where_of(SIMILAR, {}, search) == "SENSOR = 'S1' AND AZIMUTH >= 80 AND AZIMUTH <= 120 AND (OFF_NADIR >= 7 AND OFF_NADIR <= 17)"


def test_similar_search_goes_through_north_and_keeps_the_dates_and_sql():
    reference = srecord({"IMG_ID": "A", "SENSOR": "S1", "AZIMUTH": 350, "URL": "https://x/a.tif"})
    search = similar_search(SIMILAR, reference, SimilarTolerance(same_sensor=False), EMPTY_SEARCH.with_(sensor="S9", max_angle=3, where="CLOUD < 20"))
    assert search.sensor == "" and search.max_angle is None
    assert where_of(SIMILAR, {}, search) == "(AZIMUTH >= 330 OR AZIMUTH <= 10) AND ((CLOUD < 20))"
    assert similar_search(SIMILAR, reference, SimilarTolerance(azimuth=180)).azimuth_from is None
    assert similar_search(SIMILAR, reference, SimilarTolerance(azimuth=None)).azimuth_from is None


def test_rank_similar_orders_the_most_alike_first():
    reference = srecord({"IMG_ID": "A", "SENSOR": "S1", "OFF_NADIR": 10, "AZIMUTH": 100, "URL": "https://x/a.tif"})
    near = srecord({"IMG_ID": "B", "SENSOR": "S1", "OFF_NADIR": 11, "AZIMUTH": 102, "URL": "https://x/b.tif"})
    far = srecord({"IMG_ID": "C", "SENSOR": "S1", "OFF_NADIR": 14, "AZIMUTH": 115, "URL": "https://x/c.tif"})
    half = srecord({"IMG_ID": "D", "SENSOR": "S1", "OFF_NADIR": 10, "AZIMUTH": 100, "URL": "https://x/d.tif"})
    apart = srecord({"IMG_ID": "E", "SENSOR": "S1", "OFF_NADIR": 10, "AZIMUTH": 100, "URL": "https://x/e.tif"})
    itself = srecord({"IMG_ID": "A", "SENSOR": "S1", "OFF_NADIR": 10, "AZIMUTH": 100, "URL": "https://x/a.tif"})
    overlaps = {id(near): 1, id(far): 1, id(half): 0.4, id(apart): 0, id(itself): 1}
    ranked = rank_similar(reference, [far, half, apart, itself, near], DEFAULT_TOLERANCE, lambda r: overlaps.get(id(r)))
    assert [s.record.id for s in ranked] == ["B", "D", "C"]
    assert ranked[1].overlap == 0.4
    assert [s.record.id for s in rank_similar(reference, [near, half], SimilarTolerance(min_overlap=0.5), lambda r: overlaps.get(id(r)))] == ["B"]


def test_azimuths_are_measured_through_north():
    assert azimuth_difference(355, 5) == 10
    assert azimuth_difference(10, 190) == 180
    assert azimuth_difference(-20, 20) == 40


def test_tolerances_survive_a_round_trip():
    t = SimilarTolerance(same_sensor=False, angle=None, azimuth=7.5, min_overlap=0.3)
    assert SimilarTolerance.from_json(json.loads(json.dumps(t.to_json()))) == t
    assert SimilarTolerance.from_json(None) == DEFAULT_TOLERANCE


# --------------------------------------------------------------------------- catalog-watch.test.ts

WATCHED = catalog_of({"label": "カタログ", "url": "https://svc/FeatureServer/0",
                      "fields": {"id": "IMG_ID", "acquired": "ACQ", "registered": "REG", "sensor": "SENSOR", "angle": "ANGLE", "source": "URL"},
                      "where": "STATUS = 'OK'"}, [], 0)
CREATED = utc(2026, 10, 10)


def watch(**over):
    w = new_watch("東京", WATCHED, {"ACQ": "esriFieldTypeDate", "REG": "esriFieldTypeDate"}, EMPTY_SEARCH.with_(sensor="S1", max_angle=20),
                  [{"kind": "points", "points": [[139.7, 35.6]]}], now=CREATED)
    w.id = "w1"
    for k, v in over.items():
        setattr(w, k, v)
    return w


def feature(image_id, acq=0):
    return {"attributes": {"OBJECTID": len(image_id), "IMG_ID": image_id, "ACQ": acq, "SENSOR": "S1", "URL": f"https://x/{image_id}.tif"},
            "geometry": {"rings": [[[0, 0], [0, 1], [1, 1], [0, 0]]]}}


def answering(ids, calls=None):
    def ask(url, params):
        if calls is not None:
            calls.append(params)
        return {"objectIdFieldName": "OBJECTID", "features": [feature(i, n) for n, i in enumerate(ids)]}

    return ask


def test_since_condition_writes_the_time_for_the_field_type():
    t = utc(2026, 10, 1, 2, 3, 4)
    assert since_condition("F", "esriFieldTypeDate", t) == "F >= TIMESTAMP '2026-10-01 02:03:04'"
    assert since_condition("F", "esriFieldTypeDateOnly", t) == "F >= DATE '2026-10-01'"
    assert since_condition("F", "esriFieldTypeDouble", t) == f"F >= {t}"
    assert since_condition("F", "esriFieldTypeString", t) == "F >= '2026-10-01'"


def test_watch_query_ands_the_catalog_the_conditions_and_the_newness_over_the_point():
    q = watch_query(watch(), CREATED)
    assert q["where"] == "(STATUS = 'OK') AND SENSOR = 'S1' AND ANGLE <= 20 AND REG >= TIMESTAMP '2026-10-07 00:00:00'"
    assert q["geometryType"] == "esriGeometryPoint"
    assert json.loads(q["geometry"]) == {"x": 139.7, "y": 35.6, "spatialReference": {"wkid": 4326}}
    assert q["spatialRel"] == "esriSpatialRelIntersects"
    many = watch_query(watch(areas=[{"kind": "points", "points": [[139.7, 35.6], [135.5, 34.7]]}]), None)
    assert many["geometryType"] == "esriGeometryMultipoint" and json.loads(many["geometry"])["points"] == [[139.7, 35.6], [135.5, 34.7]]
    assert watch_query(watch(areas=[{"kind": "polygon", "rings": [[[0, 0], [1, 0], [1, 1], [0, 0]]]}]), None)["geometryType"] == "esriGeometryPolygon"
    assert "geometry" not in watch_query(watch(areas=[]), None)


def test_check_watch_first_notes_what_is_there_then_notices_only_new_images():
    first = check_watch(watch(), answering(["A", "B"]), CREATED + 1000)
    assert first.notices == [] and first.watch.seen == ["A", "B"] and first.watch.last_checked == CREATED + 1000
    calls = []
    second = check_watch(first.watch, answering(["A", "B", "C", "D"], calls), CREATED + 2000)
    assert [n.image_id for n in second.notices] == ["D", "C"]
    n = second.notices[0]
    assert (n.id, n.watch_name, n.catalog_label, n.read, n.sensor) == ("w1|D", "東京", "カタログ", False, "S1")
    assert second.watch.seen == ["A", "B", "C", "D"]
    assert calls[0]["orderByFields"] == "REG DESC"


def test_a_failed_check_keeps_the_error_and_the_last_check():
    def failing(url, params):
        raise RuntimeError("つながりません")

    before = watch(last_checked=CREATED, seen=["A"])
    result = check_watch(before, failing, CREATED + 5000)
    assert result.notices == [] and result.watch.last_error == "つながりません"
    assert result.watch.last_checked == CREATED and result.watch.last_tried == CREATED + 5000
    fixed = check_watch(result.watch, answering(["A", "B"]), CREATED + 9000)
    assert fixed.watch.last_error is None and [n.image_id for n in fixed.notices] == ["B"]


def test_ordering_refused_asks_again_plainly():
    calls = []

    def old(url, params):
        calls.append(params)
        if params.get("orderByFields"):
            raise RuntimeError("not supported")
        return {"features": [feature("A")]}

    result = check_watch(watch(last_checked=CREATED), old, CREATED + 1)
    assert len(calls) == 2 and [n.image_id for n in result.notices] == ["A"]


def test_each_area_is_asked_and_an_image_noticed_once():
    calls = []
    areas = join_areas([{"kind": "points", "points": [[1, 1]]}], [{"kind": "polygon", "rings": [[[0, 0], [0, 2], [2, 2], [0, 0]]]}, {"kind": "points", "points": [[2, 2]]}])
    assert [a["kind"] for a in areas] == ["points", "polygon"]
    result = check_watch(watch(areas=areas, last_checked=CREATED, seen=[]), answering(["A", "B"], calls), CREATED + 1)
    assert [c["geometryType"] for c in calls] == ["esriGeometryMultipoint", "esriGeometryPolygon"]
    assert sorted(n.image_id for n in result.notices) == ["A", "B"]


def test_a_point_joins_a_watch_noting_the_images_already_there():
    before = watch(last_checked=CREATED, seen=["A"])
    after = add_areas(before, [{"kind": "points", "points": [[135.5, 34.7]]}], answering(["A", "X"]), CREATED + 1)
    assert after.areas == [{"kind": "points", "points": [[139.7, 35.6], [135.5, 34.7]]}]
    assert after.seen == ["A", "X"] and after.last_checked == CREATED
    following = check_watch(after, answering(["A", "X", "Y"]), CREATED + 2)
    assert [n.image_id for n in following.notices] == ["Y"]


def test_areas_join_points_and_polygons_outer_rings_clockwise():
    square = [[[0, 0], [1, 0], [1, 1], [0, 1], [0, 0]]]  # anticlockwise
    holed = [[[0, 0], [0, 4], [4, 4], [4, 0], [0, 0]], [[1, 1], [1, 2], [2, 2], [2, 1], [1, 1]]]
    areas = areas_of([[139.7, 35.6], [135.5, 34.7]], [square, holed])
    assert areas[0] == {"kind": "points", "points": [[139.7, 35.6], [135.5, 34.7]]}
    rings = areas[1]["rings"]

    def signed(r):
        return sum(r[i][0] * r[i + 1][1] - r[i + 1][0] * r[i][1] for i in range(len(r) - 1))

    assert len(rings) == 3 and signed(rings[0]) < 0 and signed(rings[1]) < 0 and signed(rings[2]) > 0


def test_conditions_text():
    w = watch(areas=[{"kind": "points", "points": [[139.7, 35.6], [135.5, 34.7]]}])
    assert conditions_text(w.catalog, w.conditions, w.areas) == "カタログ ・ 地点 2 か所 ・ センサー S1 ・ 角度 20° 以下"
    assert conditions_text(w.catalog, w.conditions, []) == "カタログ ・ 範囲なし ・ センサー S1 ・ 角度 20° 以下"


def test_the_store_keeps_watches_and_notices_and_checks_what_is_due(tmp_path):
    store = WatchStore(GeoPackage(str(tmp_path / "w.gpkg")))
    w = watch()
    store.put_watch(w)
    images = ["A"]

    def ask_for(watch):
        return watch, answering(images)

    # The first check notes; a later one notices.
    assert check_due(store, ask_for, 600000, now=CREATED + 1) == 0
    assert store.watch("w1").seen == ["A"] and store.watch("w1").catalog.token is None
    images.append("B")
    # Not due yet.
    assert check_due(store, ask_for, 600000, now=CREATED + 2) == 0
    assert check_due(store, ask_for, 600000, force=True, now=CREATED + 3) == 1
    assert [(n.image_id, n.read) for n in store.notices()] == [("B", False)] and store.unread() == 1
    # Renamed meanwhile: the notice takes the new name.
    renamed = store.watch("w1")
    renamed.name = "東京湾"
    store.put_watch(renamed)
    images.append("C")
    assert check_due(store, ask_for, 600000, now=CREATED + 700000) == 1
    assert store.notices()[0].watch_name == "東京湾" and store.watch("w1").name == "東京湾"
    store.mark_read([store.notices()[0].id])
    assert store.unread() == 1
    store.mark_read()
    assert store.unread() == 0
    store.delete_watch("w1")
    assert store.watches() == [] and store.notices() == []


# --------------------------------------------------------------------------- local-paths.test.ts


def test_path_parts_read_unc_drive_posix_and_file_urls():
    assert path_parts("\\\\NAS\\img\\a.tif") == ["//nas", "img", "a.tif"]
    assert path_parts("//nas/img//./a.tif") == ["//nas", "img", "a.tif"]
    assert path_parts("C:\\Data\\a.tif") == ["c:", "Data", "a.tif"]
    assert path_parts("/mnt/img/a.tif") == ["/", "mnt", "img", "a.tif"]
    assert path_parts("file:///C:/Data/a%20b.tif") == ["c:", "Data", "a b.tif"]
    assert path_parts("file://nas/img/a.tif") == ["//nas", "img", "a.tif"]
    assert path_parts("file:///mnt/a.tif") == ["/", "mnt", "a.tif"]


MAPPINGS = [PathMapping("\\\\nas\\img", "NAS"), PathMapping("\\\\nas\\img\\cog", "COG", "https://nas-web/cog/"), PathMapping("Z:\\", "Z")]


def test_mapping_for_takes_the_longest_prefix_ignoring_case_and_slashes():
    assert mapping_for(MAPPINGS, "\\\\NAS\\IMG\\2026\\a.tif") == (MAPPINGS[0], ["2026", "a.tif"])
    assert mapping_for(MAPPINGS, "//nas/img/cog/a b.tif") == (MAPPINGS[1], ["a b.tif"])
    assert mapping_for(MAPPINGS, "z:/x/a.tif") == (MAPPINGS[2], ["x", "a.tif"])
    assert mapping_for(MAPPINGS, "\\\\nas\\imgs\\a.tif") is None
    assert mapping_for(MAPPINGS, "\\\\nas\\img") is None


def test_urls_are_built_from_the_rest_of_the_path():
    assert url_for(PathMapping("x", "x", "https://nas-web/cog/"), ["2026", "a b#.tif"]) == "https://nas-web/cog/2026/a%20b%23.tif"


def test_path_mapping_needs_a_prefix_and_an_http_url_when_one_is_given():
    problems = []
    assert path_mapping_of({"prefix": "\\\\nas\\img"}, problems, 0) == PathMapping("\\\\nas\\img", "\\\\nas\\img")
    assert path_mapping_of({"prefix": ""}, problems, 1) is None
    assert path_mapping_of({"prefix": "Z:", "url": "ftp://x"}, problems, 2) is None
    assert len(problems) == 2


def test_local_paths_open_as_they_are_through_a_url_or_in_the_folder_chosen(tmp_path):
    folder = tmp_path / "img"
    (folder / "2026").mkdir(parents=True)
    (folder / "2026" / "scene.tif").write_bytes(b"x")
    chosen: dict = {}

    def folder_for(prefix):
        return chosen.get(prefix)

    # A URL mapping opens as a COG.
    assert resolve_path("\\\\nas\\img\\cog\\a.tif", MAPPINGS, folder_for).value == "https://nas-web/cog/a.tif"
    # A path that is here opens as it is (a file: URL as its path).
    here = str(folder / "2026" / "scene.tif")
    assert resolve_path(here, MAPPINGS, folder_for) .value == here
    assert resolve_path("file://" + here.replace(os.sep, "/"), [], folder_for).value == here.replace(os.sep, "/")
    # Under the NAS: the folder is asked for once, then followed regardless of case.
    asked = resolve_path("\\\\NAS\\img\\2026\\Scene.TIF", MAPPINGS, folder_for)
    assert asked.kind == "folder" and asked.mapping == MAPPINGS[0]
    chosen["\\\\nas\\img"] = str(folder)
    assert resolve_path("\\\\NAS\\img\\2026\\Scene.TIF", MAPPINGS, folder_for).value == here
    with pytest.raises(PathError, match="2026/other.tif なし"):
        resolve_path("\\\\nas\\img\\2026\\other.tif", MAPPINGS, folder_for)
    with pytest.raises(PathError, match="pathMappings"):
        resolve_path("\\\\elsewhere\\a.tif", MAPPINGS, folder_for)
    assert find_file(str(folder), ["2026", "SCENE.tif"]) == here
