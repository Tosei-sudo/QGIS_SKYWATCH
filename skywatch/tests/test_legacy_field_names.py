"""The default names changed on 2026-11-08 (FILE → OUTPUT_FILE): a table with only the old one still works."""
from __future__ import annotations

from skywatch.core.esri import EsriClient
from skywatch.core.request.store import DEFAULT_HISTORY_FIELDS, HistoryRow, add_history, load_history, request_store_of
from skywatch.tests.fake_esri import FakeEsri, FakeLayer

LEDGER = "https://example.com/arcgis/rest/services/Requests/FeatureServer/0"
HISTORY = "https://example.com/arcgis/rest/services/Requests/FeatureServer/1"
T0 = 1_794_096_000_000
RING = {"rings": [[[139.0, 35.0], [139.0, 35.1], [139.1, 35.1], [139.1, 35.0], [139.0, 35.0]]], "spatialReference": {"wkid": 4326}}


def test_defaults_avoid_reserved_words():
    assert DEFAULT_HISTORY_FIELDS["file"] == "OUTPUT_FILE"


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
