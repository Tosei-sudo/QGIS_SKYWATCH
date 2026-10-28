"""Data time zones (timeZone / timeZones), as the viewer's test/time-zone.test.ts, plus the plugin's own requests."""
import json
from datetime import datetime, timezone

from satellite_observation.core import time_zone as tz
from satellite_observation.core.config import parse_config
from satellite_observation.core.esri import EsriClient
from satellite_observation.core.gp import GpTask, esri_field, esri_params, ogc_field, ogc_inputs
from satellite_observation.core.moving import read_record, time_of
from satellite_observation.core.time_zone import (
    UTC, TimeShift, clock_of, instant_of, parse_time_zone, read_instant, read_text_instant, rule_for, set_rules,
    source_rule, write_instant, write_text,
)

from .fake_esri import FakeEsri, FakeLayer

H = 3_600_000
tokyo = parse_time_zone("Asia/Tokyo")


def utc(*a) -> float:
    return datetime(*a, tzinfo=timezone.utc).timestamp() * 1000


def test_reads_iana_names_offsets_and_windows_names():
    assert tokyo.name == "Asia/Tokyo"
    assert tokyo.offset(utc(2026, 1, 1)) == 9 * H
    assert parse_time_zone("+09:00").offset(0) == 9 * H
    assert parse_time_zone("UTC+9").name == "UTC+09:00"
    assert parse_time_zone("-0530").offset(0) == -5.5 * H
    assert parse_time_zone("UTC") is UTC
    assert parse_time_zone("Tokyo Standard Time").offset(0) == 9 * H
    assert parse_time_zone("Mars/Olympus") is None
    assert parse_time_zone("+15:00") is None
    assert parse_time_zone(9) is None


def test_follows_daylight_saving():
    ny = parse_time_zone("America/New_York")
    assert ny.offset(utc(2026, 1, 15)) == -5 * H
    assert ny.offset(utc(2026, 7, 15)) == -4 * H
    assert instant_of(utc(2026, 7, 15, 12), ny) == utc(2026, 7, 15, 16)
    assert clock_of(utc(2026, 7, 15, 16), ny) == utc(2026, 7, 15, 12)


def test_steady_zones_read_without_a_zone_database(monkeypatch):
    import zoneinfo

    def missing(name):
        raise zoneinfo.ZoneInfoNotFoundError(name)

    monkeypatch.setattr(zoneinfo, "ZoneInfo", missing)
    monkeypatch.setattr(tz, "_iana_cache", {})
    seoul = parse_time_zone("asia/seoul")
    assert seoul.name == "Asia/Seoul" and seoul.offset(utc(2026, 7, 1)) == 9 * H
    assert parse_time_zone("Tokyo Standard Time").offset(0) == 9 * H
    # Zones with daylight saving need the database.
    assert parse_time_zone("America/New_York") is None


def test_reads_a_service_value_as_clock_time_in_the_data_zone_and_writes_it_back():
    raw = utc(2026, 10, 28, 9)
    shift = TimeShift(tokyo, UTC)
    assert read_instant(raw, shift) == utc(2026, 10, 28, 0)
    assert write_instant(utc(2026, 10, 28, 0), shift) == raw
    # The service says Tokyo, the data is UTC.
    assert read_instant(raw, TimeShift(UTC, tokyo)) == utc(2026, 10, 28, 18)


def test_reads_text_ignoring_its_own_suffix_and_writes_it_in_the_same_form():
    assert read_text_instant("2026-10-28T09:00:00Z", tokyo) == utc(2026, 10, 28, 0)
    assert read_text_instant("2026-10-28 09:00", tokyo) == utc(2026, 10, 28, 0)
    assert read_text_instant("2026-10-28", tokyo) is None
    t = utc(2026, 10, 28, 0)
    assert write_text(t, tokyo, "2026-01-01T00:00:00Z") == "2026-10-28T09:00:00Z"
    assert write_text(t, tokyo, "2026-01-01 00:00:00") == "2026-10-28 09:00:00"
    assert write_text(t, tokyo, "2026-01-01T00:00:00+00:00") == "2026-10-28T09:00:00+09:00"


def test_the_settings_file_as_tsudo_wrote_it():
    config, problems = parse_config({
        "imageCatalogs": [{"label": "衛星画像", "url": "https://gis.example/arcgis/rest/services/Catalog/FeatureServer/0",
                           "timeZone": "Asia/Tokyo", "fields": {"id": "IMAGE_ID", "source": "PATH"}}],
        "observationStores": [{"label": "観測記録", "url": "https://gis.example/arcgis/rest/services/Obs/FeatureServer/0", "timeZone": "+09:00"}],
        "layers": [{"type": "esri", "url": "https://gis.example/arcgis/rest/services/Ships/FeatureServer", "layer": "0", "timeZone": "Asia/Tokyo"}],
        "timeZones": [{"match": "gis\\.example/.*/Legacy/", "timeZone": "Asia/Tokyo", "serviceTimeZone": "UTC"}],
    })
    assert not [p for p in problems if "timeZone" in p or "timeZones" in p]
    set_rules(config.time_zones)
    assert rule_for("https://gis.example/arcgis/rest/services/Catalog/FeatureServer/0").zone.name == "Asia/Tokyo"
    assert rule_for("https://gis.example/arcgis/rest/services/Obs/FeatureServer/0").zone.name == "UTC+09:00"
    # A service's entry covers its layers.
    assert rule_for("https://gis.example/arcgis/rest/services/Ships/FeatureServer/3").zone.name == "Asia/Tokyo"
    legacy = rule_for("https://gis.example/arcgis/rest/services/Legacy/Old/FeatureServer/0")
    assert legacy.zone.name == "Asia/Tokyo" and legacy.service is UTC
    assert rule_for("https://gis.example/arcgis/rest/services/Other/FeatureServer/0") is None


def test_entries_first_then_rules_and_what_is_wrong():
    config, problems = parse_config({
        "imageCatalogs": [{"label": "c", "url": "https://gis.example/arcgis/rest/services/Cat/FeatureServer/0", "timeZone": "Asia/Tokyo", "fields": {"id": "ID", "source": "PATH"}}],
        "layers": [{"type": "esri", "url": "https://gis.example/arcgis/rest/services/Ships/FeatureServer", "layer": "0", "timeZone": "+09:00", "serviceTimeZone": "UTC"}],
        "timeZones": [{"match": "gis\\.example", "timeZone": "Asia/Seoul"}, {"match": "x"}, {"match": "y", "timeZone": "Nowhere/Land"}, {"match": "("}],
        "observationStores": [{"label": "o", "url": "https://gis.example/arcgis/rest/services/Obs/FeatureServer/0", "timeZone": "bad zone"}],
    })
    assert len([p for p in problems if "timeZone" in p or "timeZones" in p]) == 4
    set_rules(config.time_zones)
    assert rule_for("https://gis.example/arcgis/rest/services/Cat/FeatureServer/0").zone.name == "Asia/Tokyo"
    # Not the catalog's layer 01: the timeZones rule instead.
    assert rule_for("https://gis.example/arcgis/rest/services/Cat/FeatureServer/01").zone.name == "Asia/Seoul"
    assert rule_for("https://gis.example/arcgis/rest/services/Ships/FeatureServer/3").service is UTC
    assert rule_for("https://gis.example/arcgis/rest/services/Obs/FeatureServer/0").zone.name == "Asia/Seoul"


def test_tables_under_an_entry_and_tokens_left_out():
    config, _ = parse_config({"observationStores": [{
        "label": "o", "url": "https://h/arcgis/rest/services/Obs/FeatureServer/0", "timeZone": "Asia/Tokyo", "token": "https://not/a/source",
        "areas": {"url": "https://h/arcgis/rest/services/Obs/FeatureServer/1"}, "taxonomyTable": {"url": "https://other/T/FeatureServer/2"}}]})
    set_rules(config.time_zones)
    assert rule_for("https://other/T/FeatureServer/2").zone.name == "Asia/Tokyo"
    assert rule_for("https://not/a/source") is None


LAYER = "https://gis.example/arcgis/rest/services/Obs/FeatureServer/0"


def obs_server(**info):
    fake = FakeEsri()
    layer = fake.add_layer(LAYER, FakeLayer({"seen": "esriFieldTypeDate", "n": "esriFieldTypeInteger"}))
    if info:
        plain = layer.info
        layer.info = lambda: {**plain(), **info}
    return fake, layer


def test_esri_dates_read_in_the_data_zone_and_where_and_edits_written_back():
    set_rules([source_rule(LAYER, tokyo)])
    fake, layer = obs_server()
    raw = utc(2026, 10, 28, 9)
    layer.add({"seen": raw, "n": 5})
    client = EsriClient(fake)
    features = client.query_all(LAYER, where="seen >= TIMESTAMP '2026-10-28 00:00:00'", geometry=False)
    assert features[0]["attributes"]["seen"] == utc(2026, 10, 28, 0) and features[0]["attributes"]["n"] == 5
    query = next(form for url, form in fake.asked if url.endswith("/query"))
    assert query["where"] == "seen >= TIMESTAMP '2026-10-28 09:00:00'"
    client.apply_edits(LAYER, adds=[{"attributes": {"seen": utc(2026, 10, 28, 0), "n": 1}}],
                       updates=[{"attributes": {"OBJECTID": 1, "seen": "2026-10-28T01:00:00.000Z"}}])
    edit = next(form for url, form in fake.asked if url.endswith("/applyEdits"))
    assert json.loads(edit["adds"])[0]["attributes"] == {"seen": raw, "n": 1}
    assert json.loads(edit["updates"])[0]["attributes"]["seen"] == utc(2026, 10, 28, 10)
    # The layer's information is asked once per client.
    assert len([u for u, _ in fake.asked if u == LAYER]) == 1


def test_the_layers_own_date_reference_is_the_service_zone():
    set_rules([source_rule(LAYER, UTC)])
    fake, layer = obs_server(dateFieldsTimeReference={"timeZone": "Tokyo Standard Time", "respectsDaylightSaving": False})
    layer.add({"seen": utc(2026, 10, 28, 0)})
    assert EsriClient(fake).query_all(LAYER, geometry=False)[0]["attributes"]["seen"] == utc(2026, 10, 28, 9)
    # serviceTimeZone wins over what the layer says.
    set_rules([source_rule(LAYER, UTC, UTC)])
    assert EsriClient(fake).query_all(LAYER, geometry=False)[0]["attributes"]["seen"] == utc(2026, 10, 28, 0)


def test_layers_without_a_zone_are_left_alone():
    fake, layer = obs_server()
    layer.add({"seen": 1})
    client = EsriClient(fake)
    assert client.ask(f"{LAYER}/query", {"where": "1=1", "outFields": "*"})["features"][0]["attributes"]["seen"] == 1
    assert [u for u, _ in fake.asked] == [f"{LAYER}/query"]


def test_feed_times_read_in_the_zone():
    assert time_of("2026-10-28 09:00:00 +0000 UTC", tokyo) == utc(2026, 10, 28, 0)
    assert time_of(utc(2026, 10, 28, 9), tokyo) == utc(2026, 10, 28, 0)
    assert time_of("2026-10-28T09:00:00Z") == utc(2026, 10, 28, 9)
    record = read_record({"id": "a", "lat": 35, "lon": 139, "t": "2026-10-28T09:00:00"}, {"id": "id", "lat": "lat", "lon": "lon", "time": "t"}, 0, tokyo)
    assert record.t == utc(2026, 10, 28, 0)


def test_processing_dates_written_in_the_services_clock():
    p = lambda **kw: {"direction": "esriGPParameterDirectionInput", "parameterType": "esriGPParameterTypeRequired", "displayName": kw["name"], **kw}  # noqa: E731
    task = GpTask("esri", "https://h/arcgis/rest/services/Tools/GPServer/T", "T", "T", [esri_field(p(name="When", dataType="GPDate"))], [])
    assert esri_params(task, {"When": "2026-10-28T00:00:00Z"})["When"] == str(int(utc(2026, 10, 28)))
    set_rules([source_rule("https://h/arcgis/rest/services/Tools/GPServer", tokyo)])
    assert esri_params(task, {"When": "2026-10-28T00:00:00Z"})["When"] == str(int(utc(2026, 10, 28, 9)))
    ogc = GpTask("ogc", "https://h/processes/p", "p", "p", [ogc_field("when", {"schema": {"type": "string", "format": "date-time"}})], [])
    assert ogc_inputs(ogc, {"when": "2026-10-28T00:00:00Z"}, tokyo)["when"] == "2026-10-28T09:00:00Z"
    assert ogc_inputs(ogc, {"when": "2026-10-28T00:00:00Z"})["when"] == "2026-10-28T00:00:00Z"
