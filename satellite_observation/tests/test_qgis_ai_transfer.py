"""
「検出結果を DB へ転記…」, as the viewer's test/ai.spec.ts (transfer): the
detections of the white square, one class corrected and one deleted, sent to
a stand-in Esri layer with the attributes mapped; sent again, only a box
moved since is updated.
"""
import json
import os

import pytest

pytest.importorskip("qgis.core")
pytest.importorskip("onnxruntime")
from qgis.testing import start_app  # noqa: E402

APP = start_app()

from qgis.core import (  # noqa: E402
    QgsApplication, QgsProcessingContext, QgsProcessingFeedback, QgsProject, QgsRasterLayer, QgsSettings, QgsVectorLayer,
)

from satellite_observation.core.esri import EsriClient  # noqa: E402
from satellite_observation.processing_provider.detect_algorithm import AiLook  # noqa: E402
from satellite_observation.qgis_io import ai_transfer_io, settings  # noqa: E402
from satellite_observation.qgis_io.context import PluginContext  # noqa: E402
from satellite_observation.tests.fake_esri import FakeEsri, FakeLayer  # noqa: E402
from satellite_observation.tests.qgis_fakes import FakeIface, wait_for  # noqa: E402
from satellite_observation.tests.test_qgis_ai import DATA, square_tiff  # noqa: E402

URL = "https://example.com/arcgis/rest/services/Detections/FeatureServer/0"
TYPES = {"FILE_NAME": "esriFieldTypeString", "IMAGE_ID": "esriFieldTypeString", "DETECTED_AT": "esriFieldTypeDate",
         "SENT_AT": "esriFieldTypeDate", "CLASS": "esriFieldTypeString", "CONFIDENCE": "esriFieldTypeDouble", "MODEL": "esriFieldTypeString",
         "STATUS": "esriFieldTypeInteger", "LON": "esriFieldTypeDouble", "LAT": "esriFieldTypeDouble", "AREA_M2": "esriFieldTypeDouble",
         "LONG_M": "esriFieldTypeDouble", "SOURCE": "esriFieldTypeString"}


@pytest.fixture
def plugin(tmp_path, monkeypatch):
    QgsSettings().remove("satellite_observation")
    config = {"detectionOutputs": [{
        "label": "検出DB", "url": URL,
        "fields": {"fileName": "FILE_NAME", "imageId": "IMAGE_ID", "detectedAt": "DETECTED_AT", "sentAt": "SENT_AT", "class": "CLASS",
                   "score": "CONFIDENCE", "model": "MODEL", "status": "STATUS", "lon": "LON", "lat": "LAT", "area": "AREA_M2",
                   "length": "LONG_M", "note": "NOTE"},
        "statusValues": {"ai": 1, "corrected": 2, "manual": 3},
        "constants": {"SOURCE": "qgis"},
    }]}
    path = tmp_path / "config.json"
    path.write_text(json.dumps(config, ensure_ascii=False), encoding="utf-8")
    settings.set_config_source(str(path))
    fake = FakeEsri()
    target = fake.add_layer(URL, FakeLayer(TYPES, "esriGeometryPolygon"))
    target.name = "検出結果"
    monkeypatch.setattr(PluginContext, "esri", lambda self: EsriClient(fake))
    from satellite_observation import classFactory

    iface = FakeIface()
    p = classFactory(iface)
    p.initGui()
    assert p.api.problems == [], p.api.problems
    yield p, iface, target
    p.unload()
    iface.setActiveLayer(None)
    QgsProject.instance().clear()
    QgsSettings().remove("satellite_observation")


def detect(tmp_path) -> QgsVectorLayer:
    """The square's detections as 物体検出（AI） makes them: the layer opened as QGIS opens it (named, drawn, marked)."""
    image = QgsRasterLayer(square_tiff(tmp_path), "square")
    QgsProject.instance().addMapLayer(image)
    algorithm = QgsApplication.processingRegistry().algorithmById("satobs:detect")
    context = QgsProcessingContext()
    context.setProject(QgsProject.instance())
    feedback = QgsProcessingFeedback()
    results, ok = algorithm.run({"INPUT": image.id(), "MODEL_FILE": os.path.join(DATA, "detect.onnx"), "SCORE": 0.5, "OUTPUT": "memory:"},
                                context, feedback)
    assert ok
    layer = context.takeResultLayer(results["OUTPUT"])
    QgsProject.instance().addMapLayer(layer)
    # What QGIS does when it opens the output (the post-processor set when the algorithm runs from its dialog).
    AiLook("物体検出（detect.onnx）").postProcessLayer(layer, context, feedback)
    return layer


def test_detections_are_sent_once_and_a_moved_box_is_updated(plugin, tmp_path):
    p, iface, target = plugin
    layer = detect(tmp_path)
    assert layer.name() == "物体検出（detect.onnx）" and layer.customProperty(ai_transfer_io.ORIGIN)
    count = layer.featureCount()
    assert count == 16
    # A person checks the result: one class corrected, one false detection deleted.
    first, second = list(layer.getFeatures())[:2]
    layer.startEditing()
    layer.changeAttributeValue(first.id(), layer.fields().indexOf("class"), "roof")
    layer.deleteFeature(second.id())
    assert layer.commitChanges()
    count -= 1
    iface.setActiveLayer(layer)

    p.open_transfer()
    dialog = p.transfer_dialog
    assert dialog.source.currentText() == layer.name() and dialog.target.currentText() == "検出DB"
    assert wait_for(lambda: "新規" in dialog.plan_label.text()), dialog.plan_label.text()
    assert dialog.plan_label.text() == \
        f"検出結果（ポリゴン）へ新規 {count} 件・更新 0 件・転記済みで変更なし 0 件（AI {count - 1}・修正 1）。転記先にない属性 1 個は書き込み対象外"
    # NOTE is mapped but the layer has no such attribute: shown, and left out.
    rows = {dialog.mapping.topLevelItem(i).text(0): dialog.mapping.topLevelItem(i).text(1) for i in range(dialog.mapping.topLevelItemCount())}
    assert rows["備考"] == "NOTE（転記先にない・書けない属性）" and rows["固定値 「qgis」"] == "SOURCE"
    assert dialog.run_button.isEnabled()
    dialog.run()
    assert wait_for(lambda: not dialog.running) and dialog.note.text() == f"検出DB へ {count} 件を追加、0 件を更新"

    sent = target.rows
    assert len(sent) == count
    assert [r["attributes"]["CLASS"] for r in sent if r["attributes"]["STATUS"] == 2] == ["roof"]
    assert all(r["attributes"]["CLASS"] == "bright" for r in sent if r["attributes"]["STATUS"] == 1)
    for r in sent:
        a = r["attributes"]
        assert a["MODEL"] == "detect.onnx" and a["SOURCE"] == "qgis" and a["FILE_NAME"] == "square.tif" and a["IMAGE_ID"] == "square.tif"
        assert a["CONFIDENCE"] >= 0.5 and isinstance(a["DETECTED_AT"], int) and isinstance(a["SENT_AT"], int)
        # Near 35.68° N, 139.69° E (UTM zone 54N, 380 km E); the 160 m cells measured on the ellipsoid.
        assert 35.6 < a["LAT"] < 35.75 and 139.6 < a["LON"] < 139.8
        assert 25000 < a["AREA_M2"] < 26200 and 159 < a["LONG_M"] < 161
        # One ring of four corners, clockwise as Esri has its outer rings.
        ring = r["geometry"]["rings"][0]
        assert len(ring) == 5 and sum((b[0] - a_[0]) * (b[1] + a_[1]) for a_, b in zip(ring, ring[1:])) > 0
    assert set(json.loads(layer.customProperty(ai_transfer_io.SENT))[URL]) == {str(f.id()) for f in layer.getFeatures()}

    # Sent once: nothing new. A box moved afterwards is sent again as an update of its row.
    dialog.close()
    third = list(layer.getFeatures())[2]
    layer.startEditing()
    layer.translateFeature(third.id(), 10, 0)
    assert layer.commitChanges()
    p.open_transfer()
    assert dialog.plan_label.text() == \
        f"検出結果（ポリゴン）へ新規 0 件・更新 1 件・転記済みで変更なし {count - 1} 件（AI {count - 2}・修正 2）。転記先にない属性 1 個は書き込み対象外"
    dialog.run()
    assert wait_for(lambda: not dialog.running) and dialog.note.text() == "検出DB へ 0 件を追加、1 件を更新"
    assert len(target.rows) == count and sum(r["attributes"]["STATUS"] == 2 for r in target.rows) == 2
    assert "新規 0 件・更新 0 件" in dialog.plan_label.text() and not dialog.run_button.isEnabled()


def test_selected_features_only_and_a_point_layer(plugin, tmp_path):
    p, iface, target = plugin
    target.geometry = "esriGeometryPoint"
    layer = detect(tmp_path)
    layer.selectByIds([f.id() for f in list(layer.getFeatures())[:3]])
    p.open_transfer()
    dialog = p.transfer_dialog
    assert dialog.selected_only.isChecked() and dialog.selected_only.text() == "選択中の地物だけ（3 件）"
    assert wait_for(lambda: "新規" in dialog.plan_label.text()), dialog.plan_label.text()
    assert dialog.plan_label.text().startswith("検出結果（ポイント：中心点を転記）へ新規 3 件・更新 0 件・転記済みで変更なし 0 件（AI 3）")
    dialog.run()
    assert wait_for(lambda: not dialog.running)
    assert len(target.rows) == 3 and all(set(r["geometry"]) == {"x", "y", "spatialReference"} for r in target.rows)


def test_without_outputs_or_ai_layers_the_dialog_says_what_to_do(plugin):
    p, iface, target = plugin
    p.open_transfer()
    assert p.transfer_dialog.plan_label.text() == "転記できるレイヤーなし（先に「物体検出（AI）」か「クリック抽出（AI）」で結果を作成）"
    assert not p.transfer_dialog.run_button.isEnabled()
    p.context.config.detection_outputs = []
    p.open_transfer()
    assert p.transfer_dialog.plan_label.text().startswith("転記先なし。設定ファイルの detectionOutputs に")
