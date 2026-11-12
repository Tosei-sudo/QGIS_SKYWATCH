"""
「AI結果」 reads the `detectionOutputs` table too (tsudo, 2026-11-09): what
one person detected and sent with 「検出結果を DB へ転記」 is reviewed by
others. The person who sent it sees each result once (their layer's row);
recorded or rejected from the layer (before or after it was sent) or from
the table, a result is 取込済 (誤) in both.
"""
import json

import pytest

pytest.importorskip("qgis.core")
from qgis.testing import start_app  # noqa: E402

APP = start_app()

from qgis.core import QgsProject, QgsSettings  # noqa: E402
from qgis.PyQt.QtCore import QDateTime  # noqa: E402

from mikage.core.ai.review import ACCEPTED, REJECTED_STATE  # noqa: E402
from mikage.core.esri import EsriClient  # noqa: E402
from mikage.core.obs.flows import living  # noqa: E402
from mikage.qgis_io import ai_transfer_io, settings  # noqa: E402
from mikage.qgis_io.context import PluginContext  # noqa: E402
from mikage.tests.fake_esri import FakeEsri, FakeLayer  # noqa: E402
from mikage.tests.qgis_fakes import FakeIface, wait_for  # noqa: E402
from mikage.tests.test_qgis_detection_results import detection_layer, open_image, ship_box  # noqa: E402

URL = "https://example.com/arcgis/rest/services/Detections/FeatureServer/0"
TYPES = {"IMAGE_ID": "esriFieldTypeString", "DETECTED_AT": "esriFieldTypeDate", "CLASS": "esriFieldTypeString",
         "CONFIDENCE": "esriFieldTypeDouble", "MODEL": "esriFieldTypeString", "STATUS": "esriFieldTypeString", "LONG_M": "esriFieldTypeDouble"}
AT = "2026-11-07T01:00:00Z"


@pytest.fixture
def plugin(tmp_path, monkeypatch):
    QgsSettings().remove("mikage")
    settings._set("local_gpkg", str(tmp_path / "local.gpkg"))
    config = {
        "aiModels": [{"label": "港湾モデル", "url": "./harbour.onnx", "kinds": {"boat": "船舶"}}],
        "detectionOutputs": [{"label": "検出DB", "url": URL, "fields": {
            "imageId": "IMAGE_ID", "detectedAt": "DETECTED_AT", "class": "CLASS", "score": "CONFIDENCE", "model": "MODEL",
            "status": "STATUS", "length": "LONG_M"}}],
    }
    path = tmp_path / "config.json"
    path.write_text(json.dumps(config, ensure_ascii=False), encoding="utf-8")
    settings.set_config_source(str(path))
    fake = FakeEsri()
    table = fake.add_layer(URL, FakeLayer(TYPES, "esriGeometryPolygon"))
    table.name = "TBL_DETECTION"
    monkeypatch.setattr(PluginContext, "esri", lambda self: EsriClient(fake))
    from mikage import classFactory

    iface = FakeIface()
    p = classFactory(iface)
    p.initGui()
    assert p.api.problems == [], p.api.problems
    p.api.observation_dock.show()
    p.api.ai_results.order.setCurrentIndex(1)
    yield p, iface, tmp_path, table
    p.unload()
    iface.setActiveLayer(None)
    QgsProject.instance().removeAllMapLayers()
    QgsSettings().remove("mikage")


def send(p, iface, layer) -> None:
    """「検出結果を DB へ転記…」 of the whole layer (nothing chosen: the review chose the next one)."""
    layer.removeSelection()
    iface.setActiveLayer(layer)
    p.open_transfer()
    dialog = p.transfer_dialog
    assert wait_for(lambda: "新規" in dialog.plan_label.text()), dialog.plan_label.text()
    dialog.run()
    assert wait_for(lambda: not dialog.running)
    dialog.close()


def test_a_detection_sent_to_the_table_is_reviewed_by_others_once(plugin):
    p, iface, tmp_path, table = plugin
    dock = p.api.ai_results
    book = p.api.observation_dock.book
    assert [s.label for s in dock.sources()] == ["検出DB"] and dock.sources()[0].detections
    open_image(tmp_path, "d1", "2026-10-01T01:00:00Z")
    layer = detection_layer("d1.tif", [("boat", 0.9, ship_box(139.70, 35.60)), ("boat", 0.8, ship_box(139.65, 35.62)),
                                       ("boat", 0.7, ship_box(139.68, 35.61)), ("boat", 0.6, ship_box(139.66, 35.63))], at=AT)
    ai_transfer_io.mark_origin(layer)
    QgsProject.instance().addMapLayer(layer)
    assert wait_for(lambda: len(dock.rows) == 4)
    ms = QDateTime.fromString(AT, "yyyy-MM-ddTHH:mm:ssZ").toMSecsSinceEpoch()
    # Before sending: one recorded, one recorded as wrong (from the layer).
    [first] = dock.import_rows([0])
    assert first.note == f"AI検出: boat 0.90 港湾モデル [AI:検出@{ms}#1]"
    assert len(dock.reject_rows([3])) == 1

    send(p, iface, layer)
    assert len(table.rows) == 4
    oid = {r["attributes"]["CONFIDENCE"]: str(r["attributes"][table.oid]) for r in table.rows}
    # Read again: the table's rows are the person's own layer's, listed once (as the layer's).
    dock.load("d1.tif", again=True)
    assert wait_for(lambda: dock.hidden == 4)
    assert [s.local for s, _r in dock.rows] == [True] * 4 and "転記先の 4 件は自分の検出レイヤーで表示" in dock.status.text()
    # Recorded from the layer after it was sent: the note has the table's tag too.
    [second] = dock.import_rows([1])
    assert second.note == f"AI検出: boat 0.80 港湾モデル [AI:検出@{ms}#2] [AI:検出DB#{oid[0.8]}]"

    # Someone else (no detection layer): the table's rows, the classes as the model's kinds.
    QgsProject.instance().takeMapLayer(layer)  # kept, to come back
    assert wait_for(lambda: len(dock.rows) == 4 and all(s.detections for s, _r in dock.rows))
    by_score = {r.score: row for row, (_s, r) in enumerate(dock.rows)}
    assert {r.kind for _s, r in dock.rows} == {"船舶"} and dock.rows[0][0].label == "検出DB"
    # Recorded before it was sent (its run and place), after (its tag), wrong before it was sent: known.
    assert [dock.state(by_score[s]) for s in (0.9, 0.8, 0.6)] == [ACCEPTED, ACCEPTED, REJECTED_STATE]
    assert dock.state(by_score[0.7]) not in (ACCEPTED, REJECTED_STATE)
    assert dock.import_rows([by_score[0.9], by_score[0.8], by_score[0.6]]) == []
    [third] = dock.import_rows([by_score[0.7]])
    assert third.note == f"AI検出（検出DB）: boat 0.70 港湾モデル [AI:検出DB#{oid[0.7]}]" and third.kind == "船舶"
    assert len(living(book.records)) == 3

    # The one who detected it, with the layer again: recorded from the table, 取込済 in the layer's row too.
    QgsProject.instance().addMapLayer(layer)
    assert wait_for(lambda: len(dock.rows) == 4 and all(s.local for s, _r in dock.rows))
    assert [dock.state(i) for i in range(4)] == [ACCEPTED, ACCEPTED, ACCEPTED, REJECTED_STATE]
    assert dock.import_all(confirm=False) == [] and len(living(book.records)) == 3


def test_a_table_can_be_left_out_of_the_ai_results(plugin, tmp_path):
    p, iface, _tmp, _table = plugin
    path = tmp_path / "config.json"
    config = json.loads(path.read_text(encoding="utf-8"))
    config["detectionOutputs"][0]["aiResults"] = False
    path.write_text(json.dumps(config, ensure_ascii=False), encoding="utf-8")
    p.context.reload()
    assert wait_for(lambda: p.api.ai_results.sources() == [])
