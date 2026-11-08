"""The default names changed on 2026-11-08 (MODE → SENSOR_MODE, FILE → OUTPUT_FILE): a layer with only the old one still works."""
from __future__ import annotations

from skywatch.core.esri import EsriClient
from skywatch.core.plan.store import DEFAULT_PLAN_FIELDS, add_shots, plan_store_of
from skywatch.core.request.store import DEFAULT_HISTORY_FIELDS, HistoryRow, add_history, load_history, request_store_of
from skywatch.tests.fake_esri import FakeEsri, FakeLayer

PLAN_URL = "https://example.com/arcgis/rest/services/Plans/FeatureServer/0"
LEDGER = "https://example.com/arcgis/rest/services/Requests/FeatureServer/0"
HISTORY = "https://example.com/arcgis/rest/services/Requests/FeatureServer/1"
T0 = 1_794_096_000_000
RING = {"rings": [[[139.0, 35.0], [139.0, 35.1], [139.1, 35.1], [139.1, 35.0], [139.0, 35.0]]], "spatialReference": {"wkid": 4326}}


def test_defaults_avoid_reserved_words():
    assert DEFAULT_PLAN_FIELDS["mode"] == "SENSOR_MODE"
    assert DEFAULT_HISTORY_FIELDS["file"] == "OUTPUT_FILE"


def _plan_layer(fake: FakeEsri, mode_field: str) -> FakeLayer:
    fields = {"PLAN_ID": "esriFieldTypeString", "SHOT_TIME": "esriFieldTypeDate", mode_field: "esriFieldTypeString"}
    return fake.add_layer(PLAN_URL, FakeLayer(fields, "esriGeometryPolygon"))


def _shot(store) -> dict:
    f = store.fields
    return {"attributes": {f["planId"]: "P1", f["shotTime"]: T0, f["mode"]: "スポットライト"}, "geometry": RING}


def test_a_plan_layer_with_the_old_mode_field_gets_the_mode():
    fake = FakeEsri()
    layer = _plan_layer(fake, "MODE")
    store = plan_store_of({"url": PLAN_URL}, [], 0)
    # The features are made with the new name before the layer is read (as the dock does).
    assert add_shots(EsriClient(fake), store, None, [_shot(store)]) == (1, [])
    assert layer.rows[0]["attributes"]["MODE"] == "スポットライト"
    assert store.fields["mode"] == "MODE"


def test_the_new_name_wins_and_a_mapped_name_is_kept():
    fake = FakeEsri()
    layer = _plan_layer(fake, "SENSOR_MODE")
    layer.types["MODE"] = "esriFieldTypeString"
    store = plan_store_of({"url": PLAN_URL}, [], 0)
    add_shots(EsriClient(fake), store, None, [_shot(store)])
    assert layer.rows[0]["attributes"].get("SENSOR_MODE") == "スポットライト"
    fake2 = FakeEsri()
    _plan_layer(fake2, "MODE")
    mapped = plan_store_of({"url": PLAN_URL, "fields": {"mode": "BEAM"}}, [], 0)
    add_shots(EsriClient(fake2), mapped, None, [_shot(mapped)])
    assert mapped.fields["mode"] == "BEAM"


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
