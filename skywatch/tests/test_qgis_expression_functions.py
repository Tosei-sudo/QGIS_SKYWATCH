"""QGIS の式の SKYWATCH 関数: the observation dock's records and the named areas in expressions (field calculator, labels…)."""
import json

import pytest

pytest.importorskip("qgis.core")
from qgis.testing import start_app  # noqa: E402

APP = start_app()

from qgis.core import (  # noqa: E402
    QgsExpression, QgsExpressionContext, QgsExpressionContextUtils, QgsFeature, QgsGeometry, QgsProject, QgsVectorLayer,
)
from qgis.PyQt.QtCore import QDateTime  # noqa: E402

from skywatch.core.esri import EsriClient  # noqa: E402
from skywatch.core.obs.alerts import forget_named_areas  # noqa: E402
from skywatch.qgis_io import expression_functions, settings  # noqa: E402
from skywatch.tests.fake_esri import FakeEsri, FakeLayer  # noqa: E402
from skywatch.tests.qgis_fakes import wait_for  # noqa: E402
from skywatch.tests.test_qgis_alerts import plugin  # noqa: E402,F401

PLACES = "https://x/arcgis/rest/services/Alerts/FeatureServer/3"


@pytest.fixture(autouse=True)
def empty_project():
    yield
    QgsProject.instance().removeAllMapLayers()


def value(text, layer=None, feature=None):
    e = QgsExpression(text)
    assert not e.hasParserError(), e.parserErrorString()
    context = QgsExpressionContext()
    if layer is not None:
        context.appendScopes(QgsExpressionContextUtils.globalProjectLayerScopes(layer))
    if feature is not None:
        context.setFeature(feature)
    out = e.evaluate(context)
    assert not e.hasEvalError(), e.evalErrorString()
    return out


def test_records_and_places_in_expressions(plugin, tmp_path):  # noqa: F811
    p, _iface, dock, _ = plugin
    assert all(QgsExpression.isFunctionName(n) for n in expression_functions.NAMES)
    forget_named_areas()
    fake = FakeEsri()
    places = fake.add_layer(PLACES, FakeLayer({"NAME": "esriFieldTypeString", "KIND": "esriFieldTypeString"}, "esriGeometryPolygon"))
    places.add({"NAME": "東京湾", "KIND": "海域"}, {"rings": [[[139.5, 35.0], [140.1, 35.0], [140.1, 35.7], [139.5, 35.7], [139.5, 35.0]]]})
    places.add({"NAME": "横須賀港", "KIND": "港"}, {"rings": [[[139.65, 35.25], [139.75, 35.25], [139.75, 35.35], [139.65, 35.35], [139.65, 35.25]]]})
    config = tmp_path / "config.json"
    config.write_text(json.dumps({"observationExports": {"namedAreas": PLACES}}), encoding="utf-8")
    settings.set_config_source(str(config))
    p.context.esri = lambda: EsriClient(fake)
    p.context.reload()

    book = dock.book
    image = book.current.id
    book.add(139.7, 35.3, object="船A", kind="船舶 > 支援船", activity="航行")
    book.add(139.9, 35.5, object="機B", kind="航空機", activity="飛行")
    book.add(139.8, 35.4, object="船C", kind="船舶", activity="停泊", present=False)
    assert value(f"skywatch_record_count('{image}')") == 2
    assert value(f"skywatch_record_count('{image}', '船舶')") == 1
    assert value("skywatch_record_count('ほかの画像')") == 0
    assert value(f"array_to_string(skywatch_object_names('{image}'), '、')") == "機B、船A"
    seen = value("skywatch_last_seen('船A')")
    assert isinstance(seen, QDateTime) and seen.toUTC().toString("yyyy-MM-ddTHH:mm") == "2026-10-01T01:00"
    assert value("skywatch_last_activity('船A')") == "航行"
    assert value("skywatch_last_seen('いない')") is None
    assert value("skywatch_kind_level('船舶 > 支援船 > 〇〇級', 1)") == "船舶"
    assert value("skywatch_kind_level('船舶 > 支援船 > 〇〇級', -1)") == "〇〇級"
    assert value("skywatch_kind_level('船舶', 3)") is None

    # The named areas: a square not kept is NULL at first, looked up in the background (its square only, not the
    # whole layer), and the map drawn again; a layer in another CRS.
    drawn = []
    p.place_reader.refresh = lambda: drawn.append(1)
    layer = QgsVectorLayer("Point?crs=EPSG:3857", "点", "memory")
    QgsProject.instance().addMapLayer(layer)
    f = QgsFeature(layer.fields())
    g = QgsGeometry.fromWkt("POINT(139.7 35.3)")
    from qgis.core import QgsCoordinateReferenceSystem, QgsCoordinateTransform

    g.transform(QgsCoordinateTransform(QgsCoordinateReferenceSystem("EPSG:4326"), layer.crs(), QgsProject.instance()))
    f.setGeometry(g)
    asked = len(fake.asked)
    assert value("skywatch_place($geometry)", layer, f) is None
    assert value("skywatch_place($geometry)", layer, f) is None  # asked once
    assert wait_for(lambda: drawn == [1])
    assert value("skywatch_place($geometry)", layer, f) == "横須賀港"
    assert value("array_to_string(skywatch_places($geometry), '・')", layer, f) == "横須賀港・東京湾"
    queries = [form for url, form in fake.asked[asked:] if url.endswith("/query")]
    assert queries and all("geometry" in form or "objectIds" in form for form in queries)
    # Kept: no request for another point of the same square.
    asked = len(fake.asked)
    assert value("skywatch_place(transform(make_point(139.9, 35.5), 'EPSG:4326', 'EPSG:3857'))", layer) == "東京湾"
    assert len(fake.asked) == asked
    far = QgsFeature(layer.fields())
    far.setGeometry(QgsGeometry.fromWkt("POINT(0 0)"))
    assert value("skywatch_place($geometry)", layer, far) is None
    assert wait_for(lambda: len(drawn) == 2)
    assert value("skywatch_place($geometry)", layer, far) is None and len(drawn) == 2  # in none: no lookup again

    # Gone with the plugin.
    expression_functions.unregister()
    assert not any(QgsExpression.isFunctionName(n) for n in expression_functions.NAMES)
    expression_functions.register()
