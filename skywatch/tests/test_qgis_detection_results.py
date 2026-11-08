"""
The plugin's own 「物体検出（AI）」 in the dock 「AI結果」: a detection layer
coming to the project lists its image's results (no aiResultSources needed);
the chosen ones are recorded as observations once (the model's `kinds`, else
the classification, for the 種別; a polygon's point inside, its long and short
sides as length and width); what the person changes in the layer is what is
listed; choosing on the layer chooses in the list; a ship found again on the
next image continues; and a real run of the algorithm can be recorded.
"""
import json
import os

import pytest

pytest.importorskip("qgis.core")
from qgis.testing import start_app  # noqa: E402

APP = start_app()

from qgis.core import (  # noqa: E402
    QgsCoordinateReferenceSystem, QgsCoordinateTransform, QgsFeature, QgsField, QgsGeometry, QgsPointXY, QgsProject,
    QgsRasterLayer, QgsSettings, QgsVectorLayer,
)
from qgis.PyQt.QtCore import QDateTime, QVariant  # noqa: E402

from skywatch.core.obs.flows import living  # noqa: E402
from skywatch.qgis_io import settings  # noqa: E402
from skywatch.tests.qgis_fakes import FakeIface, wait_for  # noqa: E402
from skywatch.tests.test_qgis_observations import geotiff  # noqa: E402

MERCATOR = QgsCoordinateReferenceSystem("EPSG:3857")
WGS84 = QgsCoordinateReferenceSystem("EPSG:4326")


@pytest.fixture
def plugin(tmp_path):
    QgsSettings().remove("skywatch")
    settings._set("local_gpkg", str(tmp_path / "local.gpkg"))
    config = {"aiModels": [{"label": "港湾モデル", "url": "./harbour.onnx", "kinds": {"boat": "船舶"}}]}
    path = tmp_path / "config.json"
    path.write_text(json.dumps(config, ensure_ascii=False), encoding="utf-8")
    settings.set_config_source(str(path))
    from skywatch import classFactory

    iface = FakeIface()
    p = classFactory(iface)
    p.initGui()
    assert p.api.problems == [], p.api.problems
    p.api.observation_dock.show()
    # The list as read (the review order is tested in test_qgis_ai_review).
    p.api.ai_results.order.setCurrentIndex(1)
    yield p, iface, tmp_path
    p.unload()
    iface.setActiveLayer(None)
    QgsProject.instance().removeAllMapLayers()
    QgsSettings().remove("skywatch")


def open_image(tmp_path, name: str, when: str) -> QgsRasterLayer:
    layer = QgsRasterLayer(geotiff(tmp_path / f"{name}.tif", when), name)
    assert layer.isValid()
    QgsProject.instance().addMapLayer(layer)
    return layer


def ship_box(lon, lat, length_deg=0.0011, width_deg=0.00018):
    """A box (WGS 84 corners) about 100 m by 20 m at Tokyo Bay, in Web Mercator."""
    ring = [QgsPointXY(lon, lat), QgsPointXY(lon, lat + width_deg), QgsPointXY(lon + length_deg, lat + width_deg),
            QgsPointXY(lon + length_deg, lat), QgsPointXY(lon, lat)]
    g = QgsGeometry.fromPolygonXY([ring])
    g.transform(QgsCoordinateTransform(WGS84, MERCATOR, QgsProject.instance()))
    return g


def detection_layer(image_id: str, found, model="港湾モデル", at="2026-11-07T01:00:00Z") -> QgsVectorLayer:
    """A layer as 「物体検出（AI）」 makes it (in the image's CRS, here Web Mercator)."""
    layer = QgsVectorLayer("Polygon?crs=EPSG:3857", f"物体検出（{model}）", "memory")
    fields = [QgsField("class", QVariant.String), QgsField("class_id", QVariant.Int), QgsField("score", QVariant.Double),
              QgsField("image", QVariant.String), QgsField("image_id", QVariant.String), QgsField("image_time", QVariant.DateTime),
              QgsField("model", QVariant.String), QgsField("detected_at", QVariant.DateTime), QgsField("note", QVariant.String)]
    layer.dataProvider().addAttributes(fields)
    layer.updateFields()
    when = QDateTime.fromString(at, "yyyy-MM-ddTHH:mm:ssZ")
    features = []
    for cls, score, geometry in found:
        f = QgsFeature(layer.fields())
        f.setGeometry(geometry)
        f.setAttributes([cls, 0, score, image_id, image_id, None, model, when, None])
        features.append(f)
    layer.dataProvider().addFeatures(features)
    layer.updateExtents()
    return layer


def test_a_detection_run_is_listed_and_recorded_once(plugin):
    p, iface, tmp_path = plugin
    dock = p.api.ai_results
    book = p.api.observation_dock.book
    image = open_image(tmp_path, "d1", "2026-10-01T01:00:00Z")
    iface.setActiveLayer(image)
    assert dock.image_id is None and dock.isHidden()
    assert dock.image_label.text().startswith("「物体検出（AI）」を実行すると")
    layer = detection_layer("d1.tif", [("boat", 0.9, ship_box(139.70, 35.60)), ("船舶", 0.3, ship_box(139.65, 35.62)),
                                       ("car", 0.8, QgsGeometry.fromPointXY(QgsPointXY(0, 0)).buffer(5, 4))])
    QgsProject.instance().addMapLayer(layer)
    assert wait_for(lambda: len(dock.rows) == 3)
    assert not dock.isHidden() and dock.image_id == "d1.tif"
    assert [(s.label, r.cls, r.kind) for s, r in dock.rows] == [
        ("物体検出（港湾モデル）", "boat", "船舶"), ("物体検出（港湾モデル）", "船舶", "船舶"), ("物体検出（港湾モデル）", "car", "car")]
    assert dock.list.topLevelItem(0).text(4) == "船舶"
    # The layer itself is the map's: no copy is made.
    assert [x for x in QgsProject.instance().mapLayers().values() if isinstance(x, QgsVectorLayer)] == [layer]

    dock.min_score.setValue(0.5)
    assert [r.cls for _s, r in dock.rows] == ["boat", "car"]
    # Choosing on the layer chooses in the list, and back.
    layer.selectByIds([1])
    assert dock.selected_rows() == [0]
    dock.list.clearSelection()
    dock.list.topLevelItem(1).setSelected(True)
    assert layer.selectedFeatureIds() == [3]

    [o] = dock.import_rows([0])
    assert (o.image_id, o.kind, o.object_name) == ("d1.tif", "船舶", "船舶-1")
    ms = QDateTime.fromString("2026-11-07T01:00:00Z", "yyyy-MM-ddTHH:mm:ssZ").toMSecsSinceEpoch()
    assert o.note == f"AI検出: boat 0.90 港湾モデル [AI:検出@{ms}#1]"
    assert 95 < o.length < 105 and 15 < o.width < 22
    assert QgsGeometry.fromPolygonXY([[QgsPointXY(139.70, 35.60), QgsPointXY(139.70, 35.60018), QgsPointXY(139.7011, 35.60018),
                                       QgsPointXY(139.7011, 35.60), QgsPointXY(139.70, 35.60)]]).contains(QgsGeometry.fromPointXY(QgsPointXY(o.lon, o.lat)))
    assert dock.list.topLevelItem(0).text(0) == "済"
    assert dock.import_rows([0]) == []
    assert len(living(book.records)) == 1

    # What the person changes in the layer: the weak one made a car, the far one deleted.
    dock.min_score.setValue(0)
    layer.startEditing()
    layer.changeAttributeValue(2, layer.fields().indexOf("class"), "boat")
    layer.deleteFeature(3)
    layer.commitChanges()
    assert wait_for(lambda: [r.cls for _s, r in dock.rows] == ["boat", "boat"])
    assert dock.list.topLevelItem(0).text(0) == "済" and dock.list.topLevelItem(1).text(0) == ""
    saved = dock.import_rows(list(range(len(dock.rows))))
    assert [(o.kind, o.object_name) for o in saved] == [("船舶", "船舶-2")]

    # The layer gone: its rows go.
    QgsProject.instance().removeMapLayer(layer.id())
    assert wait_for(lambda: dock.rows == [])


def test_a_ship_detected_again_on_the_next_image_continues(plugin):
    p, iface, tmp_path = plugin
    dock = p.api.ai_results
    book = p.api.observation_dock.book
    open_image(tmp_path, "d1", "2026-10-01T01:00:00Z")
    second = open_image(tmp_path, "d2", "2026-10-02T01:00:00Z")
    QgsProject.instance().addMapLayer(detection_layer("d1.tif", [("boat", 0.9, ship_box(139.70, 35.60))]))
    assert wait_for(lambda: dock.image_id == "d1.tif" and len(dock.rows) == 1)
    [ship] = dock.import_rows([0])
    QgsProject.instance().addMapLayer(detection_layer("d2.tif", [("boat", 0.9, ship_box(139.7001, 35.60)),
                                                                 ("boat", 0.9, ship_box(139.66, 35.61))], at="2026-11-07T02:00:00Z"))
    assert wait_for(lambda: dock.image_id == "d2.tif" and len(dock.rows) == 2)
    assert list(dock.continued) == [0] and dock.continued[0].tier == "auto"
    saved = dock.import_rows([0, 1])
    assert [(o.object_id == ship.object_id, o.object_name) for o in saved] == [(True, "船舶-1"), (False, "船舶-2")]
    assert saved[0].note.endswith("[自動継続]")
    assert [o.image_id for o in living(book.records) if o.object_id == ship.object_id] == ["d1.tif", "d2.tif"]
    # Choosing the image shows its detection again.
    iface.setActiveLayer(QgsProject.instance().mapLayersByName("d1")[0])
    assert dock.image_id == "d1.tif" and [dock.list.topLevelItem(0).text(0)] == ["済"]
    iface.setActiveLayer(second)
    assert dock.image_id == "d2.tif"


def test_a_real_detection_run_is_recorded(plugin):
    pytest.importorskip("onnxruntime")
    from qgis.core import QgsApplication, QgsProcessingContext, QgsProcessingFeedback

    from skywatch.tests.test_qgis_ai import DATA, square_tiff

    p, iface, tmp_path = plugin
    dock = p.api.ai_results
    image = QgsRasterLayer(square_tiff(tmp_path), "square")
    QgsProject.instance().addMapLayer(image)
    algorithm = QgsApplication.processingRegistry().algorithmById("skywatch:detect")
    context = QgsProcessingContext()
    context.setProject(QgsProject.instance())
    results, ok = algorithm.run({"INPUT": image.id(), "MODEL": 1, "MODEL_FILE": os.path.join(DATA, "detect.onnx"), "SCORE": 0.5,
                                 "OUTPUT": "memory:"}, context, QgsProcessingFeedback())
    assert ok
    QgsProject.instance().addMapLayer(context.takeResultLayer(results["OUTPUT"]))
    assert wait_for(lambda: len(dock.rows) == 16)
    assert dock.image_id == "square.tif" and {r.kind for _s, r in dock.rows} == {"bright"}
    saved = dock.import_rows(list(range(16)))
    assert len(saved) == 16 and all(o.image_id == "square.tif" and o.note.startswith("AI検出: bright") for o in saved)
