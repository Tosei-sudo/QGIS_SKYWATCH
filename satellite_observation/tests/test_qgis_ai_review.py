"""
Active learning in the dock 「AI結果」: the results the AI is least sure of
come first (要確認); 「誤検出」 records a checked area around the result
(nothing there), 「正」 records it as an observation, each going to the next
one to look at; deleting the area takes a rejection back; training reads a
rejection as a window of background around it; 「確認結果で追加学習」 opens
the training on the image and the trained model that found the results.
"""
import sys
import types

import numpy as np
import pytest

pytest.importorskip("qgis.core")
from qgis.testing import start_app  # noqa: E402

APP = start_app()

from qgis.core import QgsCoordinateReferenceSystem, QgsCoordinateTransform, QgsGeometry, QgsPointXY, QgsProject, QgsRectangle  # noqa: E402

from satellite_observation.core.ai.review import is_rejection  # noqa: E402
from satellite_observation.core.obs.areas import CheckedArea  # noqa: E402
from satellite_observation.core.obs.flows import living  # noqa: E402
from satellite_observation.core.obs.model import Observation, default_taxonomy  # noqa: E402
from satellite_observation.qgis_io import settings  # noqa: E402
from satellite_observation.qgis_io.ai_io import Grid  # noqa: E402
from satellite_observation.qgis_io.train_io import area_geometries, labels_from_book, windows_of  # noqa: E402
from satellite_observation.tests.qgis_fakes import wait_for  # noqa: E402
from satellite_observation.tests.test_qgis_detection_results import (  # noqa: E402,F401 (the fixture)
    WGS84, detection_layer, open_image, plugin, ship_box,
)


def found(dock):
    return [(r.score, dock.list.topLevelItem(i).text(7), dock.list.topLevelItem(i).text(0)) for i, (_s, r) in enumerate(dock.rows)]


def test_the_least_sure_first_and_the_judgements_kept(plugin, monkeypatch):
    p, iface, tmp_path = plugin
    dock = p.api.ai_results
    book = p.api.observation_dock.book
    image = open_image(tmp_path, "d1", "2026-10-01T01:00:00Z")
    iface.setActiveLayer(image)
    dock.order.setCurrentIndex(0)
    assert dock.sure.value() == 0.8
    layer = detection_layer("d1.tif", [("boat", 0.9, ship_box(139.70, 35.60)), ("boat", 0.3, ship_box(139.65, 35.62)),
                                       ("boat", 0.6, ship_box(139.66, 35.63)), ("boat", 0.85, ship_box(139.67, 35.64))])
    QgsProject.instance().addMapLayer(layer)
    assert wait_for(lambda: len(dock.rows) == 4)
    assert found(dock) == [(0.3, "要確認", ""), (0.6, "要確認", ""), (0.85, "", ""), (0.9, "", "")]
    assert "要確認 2 件" in dock.status.text()
    # Choosing in the list still chooses the feature it is.
    assert dock.next_to_review() == 0 and layer.selectedFeatureIds() == [2]

    dock.reject_rows([0])
    [area] = [a for a in book.areas if a.deleted_at is None]
    assert is_rejection(area.note) and area.note.startswith("AI誤検出: boat 0.30 [AI:検出@") and area.kind == "" and area.image_id == "d1.tif"
    ring = QgsGeometry.fromPolygonXY([[QgsPointXY(x, y) for x, y in area.ring]])
    wrong = ship_box(139.65, 35.62)
    wrong.transform(QgsCoordinateTransform(QgsCoordinateReferenceSystem("EPSG:3857"), WGS84, QgsProject.instance()))
    assert ring.contains(wrong) and ring.area() < wrong.area() * 5
    assert found(dock)[0] == (0.3, "", "誤") and dock.selected_rows() == [1]
    assert "要確認 1 件・誤検出 1 件" in dock.status.text()
    # Not twice.
    assert dock.reject_rows([0]) == [] and len(book.areas) == 1

    [o] = dock.accept_rows([1])
    assert o.kind == "船舶" and len(living(book.records)) == 1
    assert found(dock)[1] == (0.6, "", "済")
    assert dock.selected_rows() == [] and dock.next_to_review() is None and dock.status.text().startswith("要確認の結果なし")

    # Read again: the judgements are the book's; the decided go last.
    dock.load("d1.tif", again=True)
    assert found(dock) == [(0.85, "", ""), (0.9, "", ""), (0.6, "", "済"), (0.3, "", "誤")]
    dock.sure.setValue(1.01)
    assert [s for s, *_ in found(dock)] == [0.85, 0.9, 0.6, 0.3] and found(dock)[0][1] == "要確認"
    # Deleting the checked area takes the rejection back.
    book.remove_areas([area])
    dock.show_results()
    assert found(dock)[0] == (0.3, "要確認", "")
    # Read as loaded, the list keeps its order.
    dock.order.setCurrentIndex(1)
    assert [s for s, *_ in found(dock)] == [0.9, 0.3, 0.6, 0.85]
    # 「未取込をすべて取り込み」 leaves the wrong one out (and asks first only when there are many).
    from satellite_observation.ui import ai_results_dock

    assert dock.reject_rows([1])
    asked = []
    monkeypatch.setattr(ai_results_dock.QMessageBox, "exec", lambda self: asked.append(self.text()) or ai_results_dock.QMessageBox.StandardButton.No)
    monkeypatch.setattr(ai_results_dock, "ASK_OVER", 1)
    assert dock.import_all() == [] and asked == ["未取込 2 件を観測記録に取り込みますか？（誤検出は除外）"]
    assert dock.import_rows([1]) == [] and "誤検出" in dock.status.text()
    saved = dock.import_all(confirm=False)
    assert len(saved) == 2 and len(living(book.records)) == 3
    assert all("0.30" not in o.note for o in saved)


def test_training_opens_on_the_image_and_the_trained_model(plugin, monkeypatch):
    p, iface, tmp_path = plugin
    dock = p.api.ai_results
    model = tmp_path / "harbour2.onnx"
    model.write_bytes(b"onnx")
    settings.remember_trained_model("harbour2", str(model))
    image = open_image(tmp_path, "d1", "2026-10-01T01:00:00Z")
    QgsProject.instance().addMapLayer(detection_layer("d1.tif", [("boat", 0.4, ship_box(139.70, 35.60))], model="学習済み: harbour2"))
    assert wait_for(lambda: len(dock.rows) == 1)
    assert dock.training_inputs() == {"SOURCE": 0, "IMAGES": [image.id()], "BASE_MODEL": str(model)}
    opened = []
    # QGIS's processing module (only in the application): its dialog.
    fake = types.ModuleType("processing")
    fake.execAlgorithmDialog = lambda name, inputs: opened.append((name, inputs))
    monkeypatch.setitem(sys.modules, "processing", fake)
    dock.train_button.click()
    assert opened == [("satobs:train_detector", dock.training_inputs())]


def test_a_rejection_is_a_window_of_background_in_training():
    ring = [[139.0, 35.0], [139.001, 35.0], [139.001, 35.001], [139.0, 35.001], [139.0, 35.0]]
    far = [[139.01, 35.01], [139.011, 35.01], [139.011, 35.011], [139.01, 35.011], [139.01, 35.01]]
    areas = [CheckedArea("a1", "img", "img", None, ring, "", "AI誤検出: boat 0.30 [AI:検出@1#2]", 0, 0),
             CheckedArea("a2", "img", "img", None, far, "", "港の確認", 0, 0)]
    record = Observation("r1", "img", "img", None, "o1", "", "船舶", "", True, "", 139.0005, 35.0105, 0, 0, length=20.0)
    gathered = labels_from_book([record], areas, default_taxonomy())
    assert [a.hard for a in gathered.areas] == [True, False]
    grid = Grid(139.0, 35.02, 0.0001, 0.0001, 200, 300)
    hard = area_geometries(gathered.areas, grid, WGS84, QgsProject.instance().transformContext(), "img", hard=True)
    others = area_geometries(gathered.areas, grid, WGS84, QgsProject.instance().transformContext(), "img", hard=False)
    assert len(hard) == 1 and len(others) == 1
    centre = hard[0].boundingBox().center()
    assert abs(centre.x() - 5) < 1 and abs(centre.y() - 195) < 1

    def read(extent: QgsRectangle, w, h):
        return np.full((h, w, 4), 255, np.uint8)

    boxes = np.array([[5.0, 95.0, 4.0, 4.0, 0]], np.float32)
    part = windows_of(read, grid, boxes, others, 32, np.random.default_rng(0), "img", hard=hard)
    assert part.hard == 1
    w = part.windows[-1]
    # The window is around the wrong result, and holds no object.
    assert len(w.boxes) == 0
