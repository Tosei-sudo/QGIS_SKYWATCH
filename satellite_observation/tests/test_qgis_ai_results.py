"""
The dock 「外部AI結果」: an image opened from the catalog brings its results
from two stand-in Esri layers (polygons and points), listed and drawn; the
lowest score hides the weak ones; the list and the map choose together; the
chosen ones and then the rest are recorded as observations once, a polygon at
its point inside with its long side; another image replaces the layers.
"""
import json

import pytest

pytest.importorskip("qgis.core")
from qgis.testing import start_app  # noqa: E402

APP = start_app()

from qgis.core import QgsGeometry, QgsPointXY, QgsProject, QgsRasterLayer, QgsSettings, QgsWkbTypes  # noqa: E402

from satellite_observation.core.esri import EsriClient  # noqa: E402
from satellite_observation.core.obs.flows import living  # noqa: E402
from satellite_observation.qgis_io import settings  # noqa: E402
from satellite_observation.qgis_io.ai_results_io import RESULTS_OF  # noqa: E402
from satellite_observation.qgis_io.catalog_io import CATALOG_URL  # noqa: E402
from satellite_observation.qgis_io.context import PluginContext  # noqa: E402
from satellite_observation.qgis_io.obs_images import IMAGE_ID  # noqa: E402
from satellite_observation.tests.fake_esri import FakeEsri, FakeLayer  # noqa: E402
from satellite_observation.tests.qgis_fakes import FakeIface, wait_for  # noqa: E402
from satellite_observation.tests.test_qgis_observations import geotiff  # noqa: E402

POLYGONS = "https://example.com/arcgis/rest/services/AI/FeatureServer/0"
POINTS = "https://example.com/arcgis/rest/services/AI/FeatureServer/1"
CATALOG = "https://example.com/arcgis/rest/services/Catalog/FeatureServer/0"


def box(lon, lat, dx, dy):
    """An Esri polygon of a box (clockwise)."""
    return {"rings": [[[lon, lat], [lon, lat + dy], [lon + dx, lat + dy], [lon + dx, lat], [lon, lat]]], "spatialReference": {"wkid": 4326}}


@pytest.fixture
def plugin(tmp_path, monkeypatch):
    QgsSettings().remove("satellite_observation")
    settings._set("local_gpkg", str(tmp_path / "local.gpkg"))
    config = {"aiResultSources": [
        {"label": "船舶検出", "url": POLYGONS, "fields": {"imageId": "IMAGE_ID", "class": "CLS", "score": "SCORE", "id": "DET_ID"},
         "kinds": {"ship": "船舶"}, "activity": "停泊", "confidence": "推定"},
        {"label": "車両点", "url": POINTS, "fields": {"imageId": "IMG", "class": "CLS", "name": "NAME"}},
    ]}
    path = tmp_path / "config.json"
    path.write_text(json.dumps(config, ensure_ascii=False), encoding="utf-8")
    settings.set_config_source(str(path))
    fake = FakeEsri()
    polygons = fake.add_layer(POLYGONS, FakeLayer({"IMAGE_ID": "esriFieldTypeString", "CLS": "esriFieldTypeString", "SCORE": "esriFieldTypeDouble",
                                                   "DET_ID": "esriFieldTypeString"}, "esriGeometryPolygon"))
    # A ship about 100 m by 20 m off Tokyo Bay's shore, a weak one, and one in another image.
    polygons.add({"IMAGE_ID": "IMG1", "CLS": "ship", "SCORE": 0.9, "DET_ID": "d-1"}, box(139.70, 35.60, 0.0011, 0.00018))
    polygons.add({"IMAGE_ID": "IMG1", "CLS": "ship", "SCORE": 0.2, "DET_ID": "d-2"}, box(139.65, 35.62, 0.0003, 0.0003))
    polygons.add({"IMAGE_ID": "IMG2", "CLS": "ship", "SCORE": 0.8, "DET_ID": "d-3"}, box(139.66, 35.61, 0.0005, 0.0002))
    # The ship of IMG1 again, about 10 m east.
    polygons.add({"IMAGE_ID": "IMG2", "CLS": "ship", "SCORE": 0.9, "DET_ID": "d-4"}, box(139.7001, 35.60, 0.0011, 0.00018))
    points = fake.add_layer(POINTS, FakeLayer({"IMG": "esriFieldTypeString", "CLS": "esriFieldTypeString", "NAME": "esriFieldTypeString"}))
    points.add({"IMG": "IMG1", "CLS": "car", "NAME": "トラックA"}, {"x": 139.72, "y": 35.65})
    monkeypatch.setattr(PluginContext, "esri", lambda self: EsriClient(fake))
    from satellite_observation import classFactory

    iface = FakeIface()
    p = classFactory(iface)
    p.initGui()
    assert p.api.problems == [], p.api.problems
    obs = p.api.observation_dock
    obs.show()
    yield p, iface, tmp_path
    p.unload()
    iface.setActiveLayer(None)
    QgsProject.instance().removeAllMapLayers()
    QgsSettings().remove("satellite_observation")


def open_catalog_image(tmp_path, image_id: str) -> QgsRasterLayer:
    """An image as the catalog opens it (its id and catalog on the layer)."""
    layer = QgsRasterLayer(geotiff(tmp_path / f"{image_id}.tif", "2026-10-01T01:00:00Z"), image_id)
    assert layer.isValid()
    layer.setCustomProperty(IMAGE_ID, image_id)
    layer.setCustomProperty(CATALOG_URL, CATALOG)
    QgsProject.instance().addMapLayer(layer)
    return layer


def result_layers():
    return sorted((layer for layer in QgsProject.instance().mapLayers().values() if layer.customProperty(RESULTS_OF)), key=lambda x: x.name())


def test_results_of_the_image_are_shown_and_recorded_once(plugin):
    p, iface, tmp_path = plugin
    dock = p.api.ai_results
    assert dock.isHidden()
    image = open_catalog_image(tmp_path, "IMG1")
    iface.setActiveLayer(image)
    assert wait_for(lambda: len(dock.rows) == 3)
    # Shown when results came.
    assert not dock.isHidden() and dock.image_label.text() == "画像: IMG1"
    assert [(s.label, r.cls, r.key) for s, r in dock.rows] == [("船舶検出", "ship", "d-1"), ("船舶検出", "ship", "d-2"), ("車両点", "car", "1")]
    layers = result_layers()
    assert [(layer.name(), layer.geometryType(), layer.featureCount()) for layer in layers] == [
        ("外部AI結果 船舶検出（IMG1）", QgsWkbTypes.GeometryType.PolygonGeometry, 2),
        ("外部AI結果 車両点（IMG1）", QgsWkbTypes.GeometryType.PointGeometry, 1),
    ]

    dock.min_score.setValue(0.5)
    assert [r.key for _s, r in dock.rows] == ["d-1", "1"]
    assert result_layers()[0].featureCount() == 1
    # The list chooses on the map, the map in the list.
    dock.list.topLevelItem(0).setSelected(True)
    polygons, points = result_layers()
    assert polygons.selectedFeatureCount() == 1 and points.selectedFeatureCount() == 0
    points.selectByIds([f.id() for f in points.getFeatures()])
    assert dock.selected_rows() == [0, 1]
    points.removeSelection()
    assert dock.selected_rows() == [0]

    book = p.api.observation_dock.book
    saved = dock.import_rows(dock.selected_rows())
    assert len(saved) == 1
    o = saved[0]
    assert (o.image_id, o.kind, o.activity, o.confidence, o.object_name) == ("IMG1", "船舶", "停泊", "推定", "船舶-1")
    assert o.note == "外部AI 船舶検出: ship 0.90 [AI:船舶検出#d-1]"
    # The point inside the polygon, its long side (about 100 m) as the length.
    assert QgsGeometry.fromPolygonXY([[QgsPointXY(*xy) for xy in box(139.70, 35.60, 0.0011, 0.00018)["rings"][0]]]).contains(
        QgsGeometry.fromPointXY(QgsPointXY(o.lon, o.lat)))
    assert 95 < o.length < 105
    assert dock.list.topLevelItem(0).text(0) == "済"
    assert [f["imported"] for f in result_layers()[0].getFeatures()] == [1]

    # The rest: only the point; then nothing more.
    saved = dock.import_rows(list(range(len(dock.rows))))
    assert [(o.object_name, o.kind, o.lon, o.lat) for o in saved] == [("トラックA", "car", 139.72, 35.65)]
    assert dock.import_rows(list(range(len(dock.rows)))) == []
    assert dock.status.text() == "取り込む結果なし（取込済か、位置なし）"
    assert len(living(book.records)) == 2

    # Shown again later (read from the cache), the recorded ones stay marked.
    dock.min_score.setValue(0)
    assert [dock.list.topLevelItem(i).text(0) for i in range(3)] == ["済", "", "済"]

    # Another image: its results replace the layers.
    other = open_catalog_image(tmp_path, "IMG2")
    assert wait_for(lambda: dock.image_id == "IMG2" and [r.key for _s, r in dock.rows] == ["d-3", "d-4"])
    assert [layer.name() for layer in result_layers()] == ["外部AI結果 船舶検出（IMG2）"]
    # Back to the first: from the cache, without asking again.
    iface.setActiveLayer(image)
    assert dock.image_id == "IMG1" and len(dock.rows) == 3
    assert other.isValid()


def test_an_image_not_from_the_catalog_reads_nothing_and_importing_needs_the_image(plugin):
    p, iface, tmp_path = plugin
    dock = p.api.ai_results
    plain = QgsRasterLayer(geotiff(tmp_path / "plain.tif", "2026-10-01T01:00:00Z"), "plain")
    QgsProject.instance().addMapLayer(plain)
    iface.setActiveLayer(plain)
    assert dock.image_id is None and dock.rows == []
    assert dock.image_label.text() == "画像カタログから画像を開くと、その画像の結果を表示"
    # Results of an image that is not open cannot be recorded.
    dock.load("IMG2")
    assert wait_for(lambda: len(dock.rows) == 2)
    assert dock.import_rows([0]) == []
    assert dock.status.text() == "画像 IMG2 が観測記録の画像一覧になし（画像を開いて）"


def test_a_result_near_a_thing_of_the_image_before_is_recorded_as_it(plugin):
    p, iface, tmp_path = plugin
    dock = p.api.ai_results
    book = p.api.observation_dock.book
    first = open_catalog_image(tmp_path, "IMG1")
    iface.setActiveLayer(first)
    assert wait_for(lambda: len(dock.rows) == 3)
    [ship] = dock.import_rows([0])
    second = open_catalog_image(tmp_path, "IMG2")
    iface.setActiveLayer(second)
    assert wait_for(lambda: dock.image_id == "IMG2" and len(dock.rows) == 2)
    # d-4 is the ship of IMG1 (sure); d-3 is far from it.
    assert list(dock.continued) == [1] and dock.continued[1].tier == "auto"
    box_widget = dock.list.itemWidget(dock.list.topLevelItem(1), 6)
    assert box_widget.currentText().startswith("継続: 船舶-1（")
    assert "継続 1 件" in dock.status.text()
    saved = dock.import_rows([0, 1])
    assert [(o.object_id == ship.object_id, o.object_name) for o in saved] == [(False, "船舶-2"), (True, "船舶-1")]
    assert saved[1].note.endswith("[AI:船舶検出#d-4] [自動継続]")
    assert dock.list.topLevelItem(1).text(6) == "継続: 船舶-1" and dock.status.text() == "2 件を観測記録に取り込み（継続 1 件）"
    assert [o.image_id for o in living(book.records) if o.object_id == ship.object_id] == ["IMG1", "IMG2"]


def test_the_thing_to_continue_can_be_chosen_or_left_new(plugin):
    p, iface, tmp_path = plugin
    dock = p.api.ai_results
    first = open_catalog_image(tmp_path, "IMG1")
    iface.setActiveLayer(first)
    assert wait_for(lambda: len(dock.rows) == 3)
    dock.import_rows([0])
    second = open_catalog_image(tmp_path, "IMG2")
    iface.setActiveLayer(second)
    assert wait_for(lambda: dock.image_id == "IMG2" and len(dock.rows) == 2)
    # Left new: chosen in the 継続 column.
    dock.list.itemWidget(dock.list.topLevelItem(1), 6).setCurrentIndex(0)
    [o] = dock.import_rows([1])
    assert o.object_name == "船舶-2" and "継続" not in o.note
