"""
Default names changed on 2026-11-08 (FILE → OUTPUT_FILE) and 2026-11-10
(the alert rules' AREA → GEOFENCE_NAME): a table with only the old one still works.
"""
from __future__ import annotations

from skywatch.core.esri import EsriClient
from skywatch.core.obs.alerts import DEFAULT_RULE_FIELDS, alert_settings_of, load_rules, rules_of_rows
from skywatch.core.request.store import DEFAULT_HISTORY_FIELDS, HistoryRow, add_history, load_history, request_store_of
from skywatch.tests.fake_esri import FakeEsri, FakeLayer

LEDGER = "https://example.com/arcgis/rest/services/Requests/FeatureServer/0"
HISTORY = "https://example.com/arcgis/rest/services/Requests/FeatureServer/1"
T0 = 1_794_096_000_000
RULES = "https://example.com/arcgis/rest/services/Alerts/FeatureServer/0"
RING = {"rings": [[[139.0, 35.0], [139.0, 35.1], [139.1, 35.1], [139.1, 35.0], [139.0, 35.0]]], "spatialReference": {"wkid": 4326}}


def test_defaults_avoid_reserved_words():
    assert DEFAULT_HISTORY_FIELDS["file"] == "OUTPUT_FILE"
    assert DEFAULT_RULE_FIELDS["area"] == "GEOFENCE_NAME"


def _rules_source(fields=None):
    problems = []
    settings = alert_settings_of({"rules": {"url": RULES, **({"fields": fields} if fields else {})}, "formats": [{"name": "速報"}]}, problems)
    assert problems == []
    return settings.rules


def test_a_rules_table_with_the_old_area_field():
    fake = FakeEsri()
    rules = fake.add_layer(RULES, FakeLayer({n: "esriFieldTypeString" for n in ("KIND", "ACTIVITIES", "AREA", "NOTE")}, ""))
    rules.add({"KIND": "船舶", "ACTIVITIES": "航行", "AREA": "東京湾", "NOTE": "湾内"})
    rules.add({"KIND": "航空機", "AREA": "*"})
    source = _rules_source()
    assert [r.area for r in load_rules(EsriClient(fake), source)] == ["東京湾", "*"]
    assert source.fields["area"] == "GEOFENCE_NAME"  # the settings themselves are left as they are


def test_the_new_name_wins_when_a_table_has_both():
    rows = [{"KIND": "船舶", "GEOFENCE_NAME": "横須賀", "AREA": "東京湾"}, {"KIND": "航空機", "GEOFENCE_NAME": None, "AREA": "東京湾"}]
    assert [r.area for r in rules_of_rows(rows, _rules_source().fields)] == ["横須賀", ""]


def test_a_name_set_in_the_settings_is_used_as_it_is():
    rows = [{"KIND": "船舶", "AREA": "東京湾", "FENCE": "舞鶴"}]
    assert [r.area for r in rules_of_rows(rows, _rules_source({"area": "FENCE"}).fields)] == ["舞鶴"]
    assert [r.area for r in rules_of_rows(rows, _rules_source({"area": "AREA"}).fields)] == ["東京湾"]


def test_a_history_table_with_the_old_file_field():
    fake = FakeEsri()
    fake.add_layer(LEDGER, FakeLayer({"REQ_ID": "esriFieldTypeString"}, "esriGeometryPolygon"))
    table = fake.add_layer(HISTORY, FakeLayer({"RUN_ID": "esriFieldTypeString", "TARGET_DAY": "esriFieldTypeString", "REQ_ID": "esriFieldTypeString",
                                               "FILE": "esriFieldTypeString"}, "esriGeometryNone"))
    store = request_store_of({"url": LEDGER, "history": HISTORY}, [], 0)
    client = EsriClient(fake)
    assert add_history(client, store, None, [HistoryRow("RUN1", T0, "2026-11-08", "R-1", file="SYS-A_20261108.csv")]) == 1
    assert table.rows[0]["attributes"]["FILE"] == "SYS-A_20261108.csv"
    assert [h.file for h in load_history(client, request_store_of({"url": LEDGER, "history": HISTORY}, [], 0), None)] == ["SYS-A_20261108.csv"]
