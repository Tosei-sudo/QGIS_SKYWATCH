"""「観測記録のエクスポート」 in QGIS: from the plugin menu, the period / place / 大分類 chosen, named areas read in a task, the file."""
import json

import pytest

pytest.importorskip("qgis.core")
from qgis.testing import start_app  # noqa: E402

APP = start_app()

from qgis.core import QgsFeature, QgsGeometry, QgsPointXY, QgsProject, QgsVectorLayer  # noqa: E402
from qgis.PyQt.QtCore import QDateTime, Qt  # noqa: E402

from skywatch.core.esri import EsriClient  # noqa: E402
from skywatch.core.obs.alerts import forget_named_areas  # noqa: E402
from skywatch.qgis_io import settings  # noqa: E402
from skywatch.tests.fake_esri import FakeEsri, FakeLayer  # noqa: E402
from skywatch.tests.qgis_fakes import wait_for  # noqa: E402
from skywatch.tests.test_qgis_alerts import plugin  # noqa: E402,F401

PLACES = "https://x/arcgis/rest/services/Alerts/FeatureServer/3"


@pytest.fixture(autouse=True)
def empty_project():
    """The layers added here (the image, a polygon layer) go with the test: others expect an empty project."""
    yield
    QgsProject.instance().removeAllMapLayers()


def test_exports_by_period_place_and_class_with_place_names(plugin, tmp_path):  # noqa: F811
    p, iface, dock, _ = plugin
    forget_named_areas()
    fake = FakeEsri()
    places = fake.add_layer(PLACES, FakeLayer({"NAME": "esriFieldTypeString", "KIND": "esriFieldTypeString"}, "esriGeometryPolygon"))
    places.add({"NAME": "東京湾", "KIND": "海域"}, {"rings": [[[139.5, 35.0], [140.1, 35.0], [140.1, 35.7], [139.5, 35.7], [139.5, 35.0]]]})
    places.add({"NAME": "横須賀港", "KIND": "港"}, {"rings": [[[139.65, 35.25], [139.75, 35.25], [139.75, 35.35], [139.65, 35.35], [139.65, 35.25]]]})
    config = tmp_path / "config.json"
    config.write_text(json.dumps({"observationExports": {
        "namedAreas": PLACES, "default": "集計",
        "formats": [{"name": "集計", "columns": ["objectName", {"key": "TOP", "source": "kind", "level": 1},
                                                 {"key": "PLACE", "source": "place", "empty": "（なし）"}, {"key": "AREA", "source": "areaName"}]},
                    {"name": "連携", "format": "json", "columns": [{"key": "NAME", "source": "objectName"}, {"key": "PLACES", "source": "places"}]}],
    }}), encoding="utf-8")
    settings.set_config_source(str(config))
    p.context.esri = lambda: EsriClient(fake)
    p.context.reload()
    book = dock.book
    book.add(139.7, 35.3, object="船A", kind="船舶 > 支援船", activity="航行")
    book.add(139.9, 35.5, object="機B", kind="航空機", activity="飛行")
    book.add(141.0, 35.5, object="船C", kind="船舶", activity="航行")
    # From the plugin menu: 観測記録 ▸ 観測記録のエクスポート…
    action = next(a for a in p.actions if a.objectName() == "SatObsObservationExport")
    action.trigger()
    dialog = dock._export
    assert dialog.isVisible() and dialog.format_select.currentData() == "集計"
    assert [dialog.format_select.itemData(i) for i in range(dialog.format_select.count())] == ["標準（CSV）", "標準（JSON）", "集計", "連携"]
    assert "地名" in dialog.format_note.text()
    assert dialog.count() == 3 and dialog.count_label.text() == "該当する記録 3 件"
    # 大分類: only 船舶.
    dialog.all_roots.setChecked(False)
    roots = {dialog.root_list.item(i).text(): dialog.root_list.item(i) for i in range(dialog.root_list.count())}
    assert {"船舶", "航空機"} <= set(roots)
    for name, item in roots.items():
        item.setCheckState(Qt.CheckState.Checked if name == "船舶" else Qt.CheckState.Unchecked)
    assert dialog.count() == 2
    # A period after now: none.
    dialog.start_check.setChecked(True)
    dialog.start_edit.setDateTime(QDateTime.currentDateTime().addDays(1))
    assert dialog.count() == 0 and not dialog.save_button.isEnabled()
    dialog.start_check.setChecked(False)
    # A polygon drawn on the map: the tool, then its corners.
    dialog.place_select.setCurrentIndex(dialog.place_select.findData("drawn"))
    assert dialog.count_label.text() == "範囲が未描画"
    was = iface.canvas.mapTool()
    dialog.start_drawing()
    assert iface.canvas.mapTool() is dialog._tool
    dialog.set_drawn([[139.6, 35.2], [139.8, 35.2], [139.8, 35.4]])
    assert iface.canvas.mapTool() is was and dialog.band is not None and dialog.count() == 1
    # A layer's polygons (in another CRS), and only those selected.
    layer = QgsVectorLayer("Polygon?crs=EPSG:4326", "範囲", "memory")
    feats = []
    for ring in ([(140.9, 35.4), (141.1, 35.4), (141.1, 35.6), (140.9, 35.4)], [(139.6, 35.2), (139.8, 35.2), (139.8, 35.4), (139.6, 35.2)]):
        f = QgsFeature()
        f.setGeometry(QgsGeometry.fromPolygonXY([[QgsPointXY(x, y) for x, y in ring]]))
        feats.append(f)
    layer.dataProvider().addFeatures(feats)
    QgsProject.instance().addMapLayer(layer)
    dialog.place_select.setCurrentIndex(dialog.place_select.findData("layer"))
    dialog.layer_select.setLayer(layer)
    assert dialog.band is None and dialog.count() == 2
    layer.selectByIds([next(layer.getFeatures()).id()])
    dialog.selected_check.setChecked(True)
    assert dialog.count() == 1 and dialog.area()[1] == "範囲（選択）"
    # A named area (read in a task).
    dialog.all_roots.setChecked(True)
    dialog.place_select.setCurrentIndex(dialog.place_select.findData("named"))
    assert wait_for(lambda: dialog.named_select.count() == 2)
    dialog.named_select.setCurrentIndex(dialog.named_select.findData("東京湾"))
    assert dialog.count() == 2
    path = tmp_path / "out.csv"
    dialog.save(str(path))
    assert wait_for(lambda: dialog.status.text().startswith("2 件を保存"))
    lines = path.read_text(encoding="utf-8-sig").splitlines()
    assert lines[0] == "objectName,TOP,PLACE,AREA" and sorted(lines[1:]) == ["機B,航空機,東京湾,東京湾", "船A,船舶,横須賀港,東京湾"]
    # JSON of columns, everywhere.
    dialog.place_select.setCurrentIndex(0)
    dialog.format_select.setCurrentIndex(dialog.format_select.findData("連携"))
    path = tmp_path / "out.json"
    dialog.save(str(path))
    assert wait_for(lambda: dialog.status.text().startswith("3 件を保存"))
    assert {r["NAME"]: r["PLACES"] for r in json.loads(path.read_text(encoding="utf-8"))} == {"機B": "東京湾", "船A": "横須賀港・東京湾", "船C": ""}
    dialog.close()
    assert dialog.band is None


def test_the_built_in_csv_without_settings_and_the_dock_button(plugin, tmp_path):  # noqa: F811
    p, iface, dock, _ = plugin
    book = dock.book
    book.add(139.7, 35.3, object="船A", kind="船舶", activity="航行")
    dialog = dock.open_export()
    assert dialog.format_select.currentData() == "標準（CSV）"
    i = dialog.place_select.findData("named")
    assert not dialog.place_select.model().item(i).isEnabled()
    path = tmp_path / "o.csv"
    dialog.save(str(path))
    assert wait_for(lambda: dialog.status.text().startswith("1 件を保存"))
    data = path.read_bytes()
    assert data.startswith(b"\xef\xbb\xbf") and not data.startswith(b"\xef\xbb\xbf" * 2)
    assert data.decode("utf-8-sig").splitlines()[0].endswith("削除日時")
    assert dock.open_export() is dialog
