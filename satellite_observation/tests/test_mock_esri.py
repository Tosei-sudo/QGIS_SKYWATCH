"""
scripts/mock_esri: the Esri stand-in for development, driven over HTTP by the
plugin's own Esri code (EsriClient, catalog search, stores) with the settings
file it writes.
"""
from __future__ import annotations

import importlib.util
import json
import os
import sys
import threading
import time
import urllib.request
from dataclasses import replace
from http.server import ThreadingHTTPServer
from urllib.error import URLError

import pytest

from satellite_observation.core.catalog.query import CatalogSearch
from satellite_observation.core.catalog.search import read_catalog_layer, search_catalog, sensors_of
from satellite_observation.core.config import read_config_file
from satellite_observation.core.esri import EsriClient, EsriError, form_body
from satellite_observation.core.obs.model import Observation
from satellite_observation.core.obs.stores import EsriAreaStore, EsriObservationStore, EsriTaxonomyStore
from satellite_observation.core.plan.satellites import satellite_of

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
MOCK = os.path.join(ROOT, "scripts", "mock_esri")


def _load(name: str):
    if MOCK not in sys.path:
        sys.path.insert(0, MOCK)
    spec = importlib.util.spec_from_file_location(name, os.path.join(MOCK, f"{name}.py"))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


mock_esri = _load("mock_esri")
sample = _load("sample")


def transport(url: str, form: dict[str, str]) -> bytes:
    request = urllib.request.Request(url, data=form_body(form), headers={"Content-Type": "application/x-www-form-urlencoded"})
    try:
        with urllib.request.urlopen(request, timeout=10) as answer:
            return answer.read()
    except URLError as error:
        raise EsriError(str(error)) from None


@pytest.fixture(scope="module")
def files(tmp_path_factory):
    """The folder of the sample images (made once: drawing them takes a few seconds)."""
    return str(tmp_path_factory.mktemp("mock_files"))


@pytest.fixture
def server(tmp_path, files):
    """The mock with its sample data, on a free port; (base URL, store, files folder, settings file)."""
    store = mock_esri.Store(str(tmp_path / "mock.sqlite"))
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), mock_esri.make_handler(mock_esri.Service(store, log=lambda *_: None), files, quiet=True))
    base = f"http://127.0.0.1:{httpd.server_address[1]}"
    mock_esri.load_services(store, sample.services(files, base))
    sample.write_images(files)
    config = str(tmp_path / "config.mock.jsonc")
    sample.write_config(config, base, files)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    yield base, store, files, config
    httpd.shutdown()
    httpd.server_close()


def test_the_settings_file_it_writes_reads_without_problems(server):
    _, _, _, path = server
    config, problems = read_config_file(path)
    assert problems == []
    assert [len(x) for x in (config.image_catalogs, config.satellite_catalogs, config.plan_stores, config.observation_stores,
                             config.ai_result_sources, config.detection_outputs)] == [1, 1, 1, 1, 1, 1]


def test_catalog_search_newest_first_over_an_area_and_by_sensor(server):
    _, _, files, path = server
    config, _ = read_config_file(path)
    catalog = config.image_catalogs[0]
    client = EsriClient(transport)
    layer = read_catalog_layer(client, catalog, None)
    assert sorted(sensors_of(client, catalog, None)) == ["OPT-X1", "OPT-X2", "SAR-Y1", "SAR-Y2"]
    result = search_catalog(client, catalog, layer, CatalogSearch(), [139.70, 35.25, 139.80, 35.35], None)
    assert result.total == 6 and len(result.records) == 6
    times = [r.attributes["ACQ_TIME"] for r in result.records]
    assert times == sorted(times, reverse=True)
    assert all(os.path.exists(r.attributes["FILE_PATH"]) for r in result.records)
    # Far away: nothing. By sensor: only its images.
    assert search_catalog(client, catalog, layer, CatalogSearch(), [130, 30, 131, 31], None).total == 0
    only = search_catalog(client, catalog, layer, CatalogSearch(sensor="OPT-X1"), None, None)
    assert {r.attributes["SENSOR_NAME"] for r in only.records} == {"OPT-X1"}


def test_satellites_have_tles_the_plugin_reads(server):
    _, _, _, path = server
    config, _ = read_config_file(path)
    catalog = config.satellite_catalogs[0]
    rows = EsriClient(transport).query_all(catalog.url, catalog.where, geometry=False)
    sats = [satellite_of(f["attributes"], catalog) for f in rows]
    assert [s.name for s in sats] == ["OPT-X1", "OPT-X2", "SAR-Y1", "SAR-Y2"]
    assert all(s.tle1.startswith("1 ") and s.tle2.startswith("2 ") for s in sats)


def test_observations_areas_and_taxonomy_are_stored_and_read_back(server):
    _, _, _, path = server
    config, _ = read_config_file(path)
    c = config.observation_stores[0]
    client = EsriClient(transport)
    store = EsriObservationStore(c, client)
    seeded = store.load()
    assert len(seeded) == 2 and {o.kind for o in seeded} == {"船舶 > 貨物船", "船舶 > 支援船"}
    now = int(time.time() * 1000)
    new = Observation(id="mock-1", image_id="X", image_name="x.tif", image_time=now, object_id="o-1", object_name="船舶-9",
                      kind="船舶", activity="入港", present=True, note="テスト", lon=139.75, lat=35.3, created_at=now, updated_at=now, length=120.0)
    store.save([new])
    other = EsriObservationStore(c, EsriClient(transport))
    read = next(o for o in other.load() if o.id == "mock-1")
    assert (read.kind, read.activity, read.length, round(read.lon, 6), read.image_time) == ("船舶", "入港", 120.0, 139.75, now)
    [marked] = other.remove([read], now + 1)
    assert marked.deleted_at == now + 1
    taxa = EsriTaxonomyStore(c.taxonomy_table, client, "分類").load()
    ships = next(t for t in taxa if t.path == ["船舶"])
    assert ships.activities == ["入港", "出港", "停泊", "航行", "荷役"] and ships.levels == ["大分類", "種別", "級"]
    assert EsriAreaStore("確認範囲", c.areas, client).load() == []


def test_errors_look_like_arcgis_errors(server):
    base, _, _, _ = server
    client = EsriClient(transport)
    url = f"{base}/arcgis/rest/services/Catalog/FeatureServer/0"
    with pytest.raises(EsriError, match="where に存在しない属性"):
        client.ask(f"{url}/query", {"where": "NOPE = 1"})
    # A text too long for its field fails that feature only.
    result = client.apply_edits(f"{base}/arcgis/rest/services/AI/FeatureServer/0", adds=[{"attributes": {"DET_ID": "x" * 200}}])
    assert result.failures() == ["DET_ID が長すぎる（200 文字、上限 128）"]


def test_paging_ids_and_web_mercator(server):
    base, store, _, _ = server
    layer = store.define("Many", {"id": 0, "geometryType": "point", "maxRecordCount": 7, "fields": {"N": "integer"}})
    for i in range(20):
        store.add(layer, {"attributes": {"N": i}, "geometry": {"x": 139 + i / 100, "y": 35}})
    client = EsriClient(transport)
    url = f"{base}/arcgis/rest/services/Many/FeatureServer/0"
    assert [f["attributes"]["N"] for f in client.query_all(url, "N >= 3")] == list(range(3, 20))
    assert client.ask(f"{url}/query", {"where": "N < 5", "returnIdsOnly": "true"})["objectIds"] == [1, 2, 3, 4, 5]
    merc = client.ask(f"{url}/query", {"where": "N = 0", "outSR": "102100", "outFields": "N"})["features"][0]["geometry"]
    assert round(merc["x"]) == 15473409
    box = json.dumps({"xmin": 15473000, "ymin": 4160000, "xmax": 15474000, "ymax": 4180000, "spatialReference": {"wkid": 102100}})
    assert client.ask(f"{url}/query", {"geometry": box, "returnCountOnly": "true"})["count"] == 1


def test_tokens_are_asked_for_when_set(server, tmp_path):
    base, store, files, _ = server
    secured = ThreadingHTTPServer(("127.0.0.1", 0), mock_esri.make_handler(mock_esri.Service(store, "secret", log=lambda *_: None), files, quiet=True))
    threading.Thread(target=secured.serve_forever, daemon=True).start()
    try:
        url = f"http://127.0.0.1:{secured.server_address[1]}/arcgis/rest/services/Places/FeatureServer/0"
        with pytest.raises(EsriError, match="トークンが必要"):
            EsriClient(transport).layer(url)
        assert EsriClient(transport).layer(url, "secret").geometry_type == "esriGeometryPoint"
    finally:
        secured.shutdown()
        secured.server_close()


def test_new_image_adds_a_catalog_record_and_its_ai_results(server):
    base, store, files, _ = server
    image_id = sample.add_new_image(store, files, base)
    client = EsriClient(transport)
    found = client.ask(f"{base}/arcgis/rest/services/Catalog/FeatureServer/0/query",
                       {"where": f"IMG_ID = '{image_id}' AND REG_TIME >= TIMESTAMP '2000-01-01 00:00:00'", "outFields": "*"})["features"]
    assert len(found) == 1 and os.path.exists(found[0]["attributes"]["FILE_PATH"])
    results = client.query_all(f"{base}/arcgis/rest/services/AI/FeatureServer/0", f"IMAGE_ID = '{image_id}'")
    assert len(results) >= 2


def test_files_are_served_with_ranges(server):
    base, _, files, _ = server
    name = sorted(os.listdir(os.path.join(files, "catalog")))[0]
    request = urllib.request.Request(f"{base}/files/catalog/{name}", headers={"Range": "bytes=0-3"})
    with urllib.request.urlopen(request) as answer:
        assert answer.status == 206 and answer.read() == b"II*\0"


def test_every_layer_written_gets_who_and_when(server, monkeypatch):
    """Plan store, request ledger, observations: CREATED_* / UPDATED_* from USERNAME, not from the sign-in."""
    from satellite_observation.core import edit_stamp

    _, _, _, path = server
    config, _ = read_config_file(path)
    edit_stamp.set_tracking(config.edit_tracking)
    monkeypatch.setenv("USERNAME", "tester-a")
    client = EsriClient(transport)
    c = config.observation_stores[0]
    store = EsriObservationStore(c, client)
    store.load()
    now = int(time.time() * 1000)
    new = Observation(id="stamp-1", image_id="X", image_name="x.tif", image_time=now, object_id="o-9", object_name="船舶-9",
                      kind="船舶", activity="入港", present=True, note="", lon=139.75, lat=35.3, created_at=now, updated_at=now)
    store.save([new])

    def row():
        [f] = client.query_all(c.url, "OBS_ID = 'stamp-1'", geometry=False)
        return f["attributes"]

    first = row()
    assert (first["CREATED_USER"], first["UPDATED_USER"]) == ("tester-a", "tester-a")
    assert abs(first["CREATED_DATE"] - now) < 60_000 and first["UPDATED_DATE"] == first["CREATED_DATE"]
    monkeypatch.setenv("USERNAME", "tester-b")
    store.save([replace(new, note="直した", updated_at=now + 5)])
    second = row()
    assert (second["CREATED_USER"], second["UPDATED_USER"]) == ("tester-a", "tester-b")
    assert second["CREATED_DATE"] == first["CREATED_DATE"] and second["UPDATED_DATE"] >= first["UPDATED_DATE"]
    # Every layer of the sample has the fields.
    for s in sample.services(server[2], server[0], with_features=False)["services"]:
        for layer in s["layers"]:
            assert set(sample.EDIT_FIELDS) <= set(layer["fields"]), (s["name"], layer["id"])


def test_alerts_rules_geofence_and_log_over_http(server):
    from satellite_observation.core.obs.alerts import (
        Moment, build_lines, load_geofences, load_log, load_named_areas, load_rules, reported_ids, select, write_log,
    )
    from satellite_observation.core.obs.model import DEFAULT_CONFIDENCES

    _, _, _, path = server
    config, _ = read_config_file(path)
    s = config.alert_reports
    assert [f.name for f in s.formats] == ["速報", "ファイル（タブ区切り）"] and s.default == "速報"
    assert s.format("ファイル（タブ区切り）").delimiter == "\t"
    client = EsriClient(transport)
    rules, fences = load_rules(client, s.rules), load_geofences(client, s.geofences)
    assert [r.note for r in rules] == ["港内の船舶の動き", "航空機"] and [g.name for g in fences] == ["横須賀"]
    now = int(time.time() * 1000)
    ship = Observation(id="mock-a", image_id="i", image_name="i", image_time=now, object_id="o", object_name="船X", kind="船舶 > 護衛艦",
                       activity="出港", present=True, note="", lon=139.75, lat=35.30, created_at=now, updated_at=now, confidence="確定")
    named = load_named_areas(client, s.named_areas, max_age=0)
    assert [a.name for a in named] == ["東京湾", "横須賀港", "横須賀本港"]
    items = select([ship, replace(ship, id="mock-b", lon=139.9)], s, rules, fences, DEFAULT_CONFIDENCES, named=named)
    assert [i.record.id for i in items] == ["mock-a"] and items[0].areas == ["横須賀"]
    assert [a.name for a in items[0].places] == ["横須賀本港", "横須賀港", "東京湾"]
    at = Moment(now=now, user="tester", count=1)
    assert write_log(client, s, s.format(), items, build_lines(items, s.format(), at), at) == 1
    assert reported_ids(client, s) == {"mock-a"}
    rows = load_log(client, s, start=now - 60_000)
    assert [(r.obs_id, r.format) for r in rows] == [("mock-a", "速報")] and rows[0].text.split("|")[2] == "船X" and rows[0].text.split("|")[5] == "横須賀本港"


def test_assignments_and_handover_memos_over_http(server):
    from satellite_observation.core.obs.assignments import TakenError, holders, take
    from satellite_observation.core.obs.handover import HandoverData, keep_memo, last_memo

    _, _, _, path = server
    config, _ = read_config_file(path)
    client = EsriClient(transport)
    now = int(time.time() * 1000)
    mine = take(client, config.assignments, "IMG-A", "tester", now, "a.tif", now - 3_600_000)
    assert mine.oid is not None
    with pytest.raises(TakenError):
        take(client, config.assignments, "IMG-A", "other", now)
    assert [h.assignee for h in holders(client, config.assignments, "IMG-A")] == ["tester"]
    table = config.handover.table
    keep_memo(client, table, "引き継ぎ本文", HandoverData(start=now - 3_600_000, end=now, now=now, user="tester", memo="申し送り"))
    last = last_memo(client, table, since=now - 86_400_000)
    assert (last.text, last.memo, last.written_by) == ("引き継ぎ本文", "申し送り", "tester")
