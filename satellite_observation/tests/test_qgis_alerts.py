"""「通報」 in QGIS: from the plugin menu, rules / geofences / log read and written in tasks, the clipboard and the file."""
import json

import pytest

pytest.importorskip("qgis.core")
from qgis.testing import start_app  # noqa: E402

APP = start_app()

from qgis.core import QgsProject, QgsRasterLayer, QgsSettings  # noqa: E402
from qgis.PyQt.QtCore import Qt  # noqa: E402
from qgis.PyQt.QtWidgets import QApplication  # noqa: E402

from satellite_observation.core.esri import EsriClient  # noqa: E402
from satellite_observation.qgis_io import settings  # noqa: E402
from satellite_observation.tests.fake_esri import FakeEsri, FakeLayer  # noqa: E402
from satellite_observation.tests.qgis_fakes import FakeIface, wait_for  # noqa: E402
from satellite_observation.tests.test_qgis_observations import geotiff  # noqa: E402

RULES = "https://x/arcgis/rest/services/Alerts/FeatureServer/0"
FENCES = "https://x/arcgis/rest/services/Alerts/FeatureServer/1"
LOG = "https://x/arcgis/rest/services/Alerts/FeatureServer/2"


@pytest.fixture
def plugin(tmp_path):
    QgsSettings().remove("satellite_observation")
    settings._set("local_gpkg", str(tmp_path / "local.gpkg"))
    settings.set_recorder("田中")
    from satellite_observation import classFactory

    iface = FakeIface()
    p = classFactory(iface)
    p.initGui()
    layer = QgsRasterLayer(geotiff(tmp_path / "d1.tif", "2026-10-01T01:00:00Z"), "d1")
    QgsProject.instance().addMapLayers([layer])
    iface.canvas.setDestinationCrs(layer.crs())
    iface.canvas.setLayers([layer])
    iface.canvas.setExtent(layer.extent())
    dock = p.api.observation_dock
    dock.show()
    yield p, iface, dock, tmp_path
    p.unload()


def test_alerts_from_rules_to_the_log_clipboard_and_file(plugin):
    p, iface, dock, tmp_path = plugin
    fake = FakeEsri()
    rules = fake.add_layer(RULES, FakeLayer({n: "esriFieldTypeString" for n in ("KIND", "ACTIVITIES", "MIN_CONFIDENCE", "AREA", "NOTE")}, ""))
    rules.add({"KIND": "船舶", "ACTIVITIES": "航行", "AREA": "東京湾", "NOTE": "湾内航行"})
    fences = fake.add_layer(FENCES, FakeLayer({"NAME": "esriFieldTypeString"}, "esriGeometryPolygon"))
    fences.add({"NAME": "東京湾"}, {"rings": [[[139.6, 35.5], [139.8, 35.5], [139.8, 35.7], [139.6, 35.7], [139.6, 35.5]]]})
    log = fake.add_layer(LOG, FakeLayer({"OBS_ID": "esriFieldTypeString", "REPORT_NAME": "esriFieldTypeString", "TEXT": "esriFieldTypeString",
                                         "REPORTED_AT": "esriFieldTypeDate", "REPORTED_BY": "esriFieldTypeString"}, ""))
    out = tmp_path / "out" / "alert_{now:%Y%m%d}.txt"
    config = tmp_path / "config.json"
    config.write_text(json.dumps({"alertReports": {
        "default": "速報", "rules": RULES, "geofences": FENCES, "table": LOG,
        "formats": [
            {"name": "詳細", "delimiter": ","},
            {"name": "速報", "fields": ["objectName", "activity", "area", "rule"], "header": "【注目】{count}件", "footer": "以上",
             "newline": "lf", "file": {"path": str(out), "encoding": "cp932"}},
        ]}}), encoding="utf-8")
    settings.set_config_source(str(config))
    p.context.esri = lambda: EsriClient(fake)
    p.context.reload()
    book = dock.book
    book.add(139.7, 35.6, object="船A", kind="船舶 > 支援船", activity="航行")
    book.add(139.7, 35.6, object="船B", kind="船舶", activity="停泊")
    book.add(140.5, 35.6, object="船C", kind="船舶", activity="航行")
    # From the plugin menu: 観測記録 ▸ 通報文の作成…
    action = next(a for a in p.actions if a.objectName() == "SatObsAlert")
    action.trigger()
    dialog = dock._alerts
    assert dialog.isVisible() and dialog.tabs.currentIndex() == 0
    assert dialog.format_select.currentData() == "速報" and dialog.format_select.count() == 2
    dialog.pick()
    assert wait_for(lambda: dialog.status.text().startswith("1 件を抽出"))
    assert dialog.lines_table.item(0, 1).text() == "船A|航行|東京湾|湾内航行"
    assert dialog.preview.toPlainText() == "【注目】1件\n船A|航行|東京湾|湾内航行\n以上\n"
    # Edited by hand, then sent: the log row, the clipboard and the file.
    dialog.lines_table.item(0, 1).setText("船A|航行|東京湾|至急")
    dialog.send()
    assert wait_for(lambda: "件を通報" in dialog.status.text())
    assert [r["attributes"]["TEXT"] for r in log.rows] == ["船A|航行|東京湾|至急"]
    assert log.rows[0]["attributes"]["REPORT_NAME"] == "速報"
    assert QApplication.clipboard().text() == "【注目】1件\n船A|航行|東京湾|至急\n以上\n"
    written = list((tmp_path / "out").glob("alert_*.txt"))
    assert len(written) == 1 and written[0].read_bytes().decode("cp932") == "【注目】1件\n船A|航行|東京湾|至急\n以上\n"
    assert dialog.lines_table.rowCount() == 0
    # Reported once: not picked again unless asked.
    dialog.pick()
    assert wait_for(lambda: dialog.status.text().startswith("0 件を抽出"))
    dialog.again_check.setChecked(True)
    dialog.pick()
    assert wait_for(lambda: dialog.status.text().startswith("1 件を抽出"))
    # Unticked: nothing to send.
    dialog.lines_table.item(0, 0).setCheckState(Qt.CheckState.Unchecked)
    assert dialog.chosen() == ([], [])
    # 通報記録の確認… : the log, copied and deleted (so the record can be reported again).
    next(a for a in p.actions if a.objectName() == "SatObsAlertLog").trigger()
    assert dialog.tabs.currentIndex() == 1
    dialog.read_log()
    assert wait_for(lambda: dialog.status.text() == "通報記録 1 件")
    assert dialog.log_table.item(0, 3).text() == "船A|航行|東京湾|至急" and dialog.log_table.item(0, 1).text() == "速報"
    dialog.log_table.selectRow(0)
    dialog.copy_log()
    assert QApplication.clipboard().text() == "船A|航行|東京湾|至急\r\n"
    dialog.remove_log(confirm=False)
    assert wait_for(lambda: dialog.status.text() == "通報記録 1 行を削除")
    assert log.rows == [] and dialog.log_table.rowCount() == 0


def test_without_settings_the_dialog_says_so(plugin):
    p, iface, dock, _ = plugin
    dialog = dock.open_alerts()
    assert "alertReports" in dialog.note.text()
    assert not dialog.pick_button.isEnabled() and not dialog.log_button.isEnabled()


def _setup(p, tmp_path, extra=None):
    fake = FakeEsri()
    rules = fake.add_layer(RULES, FakeLayer({n: "esriFieldTypeString" for n in ("KIND", "ACTIVITIES", "MIN_CONFIDENCE", "AREA", "NOTE")}, ""))
    rules.add({"KIND": "船舶", "ACTIVITIES": "航行", "NOTE": "航行中"})
    log = fake.add_layer(LOG, FakeLayer({"OBS_ID": "esriFieldTypeString", "REPORT_NAME": "esriFieldTypeString", "TEXT": "esriFieldTypeString",
                                         "MANUAL": "esriFieldTypeSmallInteger"}, ""))
    config = tmp_path / "config.json"
    config.write_text(json.dumps({"alertReports": {"rules": RULES, "table": LOG, **(extra or {}),
                                                   "formats": [{"name": "速報", "fields": ["objectName", "activity", "rule", "manual"]}]}}),
                      encoding="utf-8")
    settings.set_config_source(str(config))
    p.context.esri = lambda: EsriClient(fake)
    p.context.reload()
    return fake, log


def test_records_added_by_hand_whatever_the_conditions(plugin):
    p, iface, dock, tmp_path = plugin
    _, log = _setup(p, tmp_path, {"notifyOnSave": False})
    book = dock.book
    a = book.add(139.7, 35.6, object="船A", kind="船舶", activity="停泊")   # outside the conditions
    b = book.add(139.7, 35.6, object="船B", kind="船舶", activity="航行")   # inside
    # From the form of a record: 「通報」.
    dock.open_form(a.lon, a.lat, editing=a)
    assert not dock.alert_button.isHidden()
    dock.alert_button.click()
    dialog = dock._alerts
    assert wait_for(lambda: dialog.status.text().startswith("手動で 1 件を追加"))
    assert dialog.lines_table.item(0, 1).text() == "船A|停泊|手動|手動" and dialog.lines_table.item(0, 0).text().startswith("手動 ")
    # Picking by the conditions keeps it, above what the conditions find.
    dialog.pick()
    assert wait_for(lambda: "件を抽出" in dialog.status.text())
    assert [dialog.lines_table.item(r, 1).text() for r in range(2)] == ["船A|停泊|手動|手動", "船B|航行|航行中|"]
    # Again from the table's chosen rows: one already listed is not added twice.
    dock.add_to_alerts([a, b])
    assert wait_for(lambda: dialog.status.text().startswith("追加する記録なし"))
    dialog.send()
    assert wait_for(lambda: "件を通報" in dialog.status.text())
    assert sorted((r["attributes"]["OBS_ID"] == a.id, r["attributes"]["MANUAL"]) for r in log.rows) == [(False, 0), (True, 1)]


def test_a_saved_record_meeting_the_conditions_is_said_in_the_message_bar(plugin):
    p, iface, dock, tmp_path = plugin
    _setup(p, tmp_path)
    dock.set_active(True)
    dock.map_clicked(139.7, 35.6, None)
    dock.object_input.setText("船C")
    dock.kind_input.setText("船舶")
    dock.activity_input.setText("停泊")
    dock.submit()
    APP.processEvents()
    # Not notable: nothing said.
    assert not wait_for(lambda: dock._alert_widget is not None, 800)
    dock.map_clicked(139.71, 35.61, None)
    dock.object_input.setText("船D")
    dock.kind_input.setText("船舶")
    dock.activity_input.setText("航行")
    dock.submit()
    assert wait_for(lambda: dock._alert_widget is not None)
    assert "船D（航行）" in dock._alert_widget.text()
    # The next notable save joins the notice shown (one notice, both records).
    first = dock._alert_widget
    dock.map_clicked(139.72, 35.62, None)
    dock.object_input.setText("船E")
    dock.kind_input.setText("船舶")
    dock.activity_input.setText("航行")
    dock.submit()
    assert wait_for(lambda: dock._alert_widget is not first)
    assert "船D（航行）・船E（航行）" in dock._alert_widget.text()
    assert [w for w in iface.messageBar().items() if w.title() == "注目の記録"] == [dock._alert_widget]
    from qgis.PyQt.QtWidgets import QPushButton
    button = next(b for b in dock._alert_widget.findChildren(QPushButton) if b.text() == "通報文を作成")
    button.click()
    dialog = dock._alerts
    assert dialog.isVisible() and dialog.lines_table.rowCount() == 2
    assert {dialog.lines_table.item(r, 1).text().split("|")[0] for r in range(2)} == {"船D", "船E"}
