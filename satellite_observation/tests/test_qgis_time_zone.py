"""Layers QGIS opens itself get a corrected field per date (the settings file's timeZone)."""
from datetime import datetime, timezone

import pytest

pytest.importorskip("qgis.core")
from qgis.testing import start_app  # noqa: E402

start_app()

from qgis.core import QgsExpression, QgsFeature, QgsField, QgsVectorLayer  # noqa: E402
from qgis.PyQt.QtCore import QDateTime, Qt, QVariant  # noqa: E402

from satellite_observation.core.esri import EsriClient  # noqa: E402
from satellite_observation.core.time_zone import UTC, parse_time_zone, set_rules, source_rule  # noqa: E402
from satellite_observation.qgis_io import time_zone_fields  # noqa: E402
from satellite_observation.tests.fake_esri import FakeEsri, FakeLayer  # noqa: E402
from satellite_observation.tests.qgis_fakes import wait_for  # noqa: E402

URL = "https://gis.example/arcgis/rest/services/Ships/FeatureServer/0"


def ms(*a) -> int:
    return int(datetime(*a, tzinfo=timezone.utc).timestamp() * 1000)


def ships() -> QgsVectorLayer:
    layer = QgsVectorLayer("Point?crs=EPSG:4326", "ships", "memory")
    layer.dataProvider().addAttributes([QgsField("name", QVariant.String), QgsField("seen", QVariant.DateTime)])
    layer.updateFields()
    f = QgsFeature(layer.fields())
    f.setAttributes(["a", QDateTime.fromMSecsSinceEpoch(ms(2026, 10, 28, 9), Qt.TimeSpec.UTC)])
    layer.dataProvider().addFeatures([f])
    return layer


@pytest.fixture(autouse=True)
def registered():
    time_zone_fields.register()
    yield
    time_zone_fields.unregister()


def test_the_function_reads_dates_and_text_in_the_data_zone():
    assert time_zone_fields.corrected(QDateTime.fromMSecsSinceEpoch(ms(2026, 10, 28, 9), Qt.TimeSpec.UTC), "Asia/Tokyo", "UTC").toMSecsSinceEpoch() == ms(2026, 10, 28, 0)
    assert time_zone_fields.corrected("2026-10-28T09:00:00Z", "Asia/Tokyo", "UTC").toMSecsSinceEpoch() == ms(2026, 10, 28, 0)
    assert time_zone_fields.corrected(None, "Asia/Tokyo", "UTC") is None
    assert time_zone_fields.corrected(QDateTime(), "Asia/Tokyo", "UTC") is None
    expression = QgsExpression("satobs_time_zone(to_datetime('2026-10-28T09:00:00Z'), 'Asia/Tokyo', 'UTC')")
    assert not expression.hasParserError(), expression.parserErrorString()
    value = expression.evaluate()
    assert not expression.hasEvalError(), expression.evalErrorString()
    assert value.toMSecsSinceEpoch() == ms(2026, 10, 28, 0)


def test_a_corrected_field_beside_each_date_once():
    layer = ships()
    rule = source_rule(URL, parse_time_zone("Asia/Tokyo"))
    added = time_zone_fields.add_fields(layer, rule, time_zone_fields.TimeShift(rule.zone, UTC))
    assert added == ["seen（Asia/Tokyo）"]
    feature = next(layer.getFeatures())
    assert feature["seen（Asia/Tokyo）"].toMSecsSinceEpoch() == ms(2026, 10, 28, 0)
    assert layer.customProperty(time_zone_fields.PROPERTY) == "Asia/Tokyo（サービス側: UTC）"
    # Not again (a project opened again, the settings file read again).
    assert time_zone_fields.add_fields(layer, rule, time_zone_fields.TimeShift(rule.zone, UTC)) == []
    assert layer.fields().names() == ["name", "seen", "seen（Asia/Tokyo）"]


def test_esri_layers_ask_the_zone_the_service_reads_dates_in(monkeypatch):
    fake = FakeEsri()
    info_layer = fake.add_layer(URL, FakeLayer({"seen": "esriFieldTypeDate"}))
    plain = info_layer.info
    info_layer.info = lambda: {**plain(), "dateFieldsTimeReference": {"timeZoneIANA": "Asia/Tokyo"}}
    set_rules([source_rule(URL, UTC)])
    layer = ships()
    from qgis.core import QgsProject

    QgsProject.instance().addMapLayer(layer, False)
    try:
        monkeypatch.setattr(time_zone_fields, "layer_url", lambda l: URL)
        monkeypatch.setitem(time_zone_fields.PROVIDERS, "memory", True)
        time_zone_fields.TimeZoneFields(lambda: EsriClient(fake)).layers_added([layer])
        wait_for(lambda: "seen（UTC）" in layer.fields().names())
        # The service calls it 09:00 UTC, its 18:00 Tokyo clock: really 18:00 UTC.
        assert next(layer.getFeatures())["seen（UTC）"].toMSecsSinceEpoch() == ms(2026, 10, 28, 18)
        assert layer.customProperty(time_zone_fields.PROPERTY) == "UTC（サービス側: Asia/Tokyo）"
    finally:
        QgsProject.instance().removeMapLayer(layer.id())


def test_layers_without_a_rule_are_left_alone(monkeypatch):
    layer = ships()
    monkeypatch.setattr(time_zone_fields, "layer_url", lambda l: URL)
    time_zone_fields.TimeZoneFields(lambda: None).layers_added([layer])
    assert layer.fields().names() == ["name", "seen"]


def test_the_service_url_of_an_esri_layer():
    layer = QgsVectorLayer(f"crs='EPSG:4326' url='{URL}'", "x", "arcgisfeatureserver")
    assert time_zone_fields.layer_url(layer) == URL
    assert time_zone_fields.layer_url(ships()) is None
