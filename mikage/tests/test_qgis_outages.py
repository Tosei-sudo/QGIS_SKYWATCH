"""
衛星の障害情報 in QGIS (tsudo, 2026-11-16): the menu's 「衛星の障害情報…」
lists and enters outages in the table, the day's calculation holds back what
only the stopped satellite could take, 「復旧」 brings it back, and those who
only ask for pictures see the list without its buttons.
"""
import json

import pytest

pytest.importorskip("qgis.core")

from qgis.PyQt.QtWidgets import QApplication  # noqa: E402

from mikage.core.plan.outages import OUTAGE, ONGOING, RECOVERED  # noqa: E402
from mikage.core.request.daily import TAKEN  # noqa: E402
from mikage.tests import test_qgis_request_shots as shots_test  # noqa: E402
from mikage.tests.fake_esri import FakeLayer  # noqa: E402
from mikage.tests.qgis_fakes import wait_for  # noqa: E402
from mikage.tests.test_plan import T  # noqa: E402
from mikage.tests.test_satellite_outages import TYPES  # noqa: E402
from mikage.ui.outage_dialog import COLUMNS, OutageDialog  # noqa: E402

plugin = shots_test.plugin
OUTAGES = "https://example.com/arcgis/rest/services/Req/FeatureServer/27"


def _with_outages(plugin):
    p, fake, tmp_path = shots_test._setup(plugin)
    fake.add_layer(OUTAGES, FakeLayer(dict(TYPES), "esriGeometryNone"))
    config = json.loads((tmp_path / "config.json").read_text(encoding="utf-8"))
    config["satelliteOutages"] = OUTAGES
    (tmp_path / "config.json").write_text(json.dumps(config, ensure_ascii=False), encoding="utf-8")
    p.api.reload_config()
    assert p.api.problems == [], p.api.problems
    return p, fake, tmp_path


def _open(p):
    next(a for a in p.actions if a.objectName() == "SatObsOutages").trigger()
    dialog = p.outage_dialog
    assert dialog is not None and dialog.isVisible()
    assert wait_for(lambda: dialog.task is None), dialog.status.text()
    return dialog


def test_entered_held_back_and_recovered(plugin, monkeypatch):
    monkeypatch.setenv("USERNAME", "tsudo")
    p, fake, tmp_path = _with_outages(plugin)
    dialog = _open(p)
    assert dialog.model.rowCount() == 0 and dialog.status.text().startswith("復旧していない障害 0 件")
    assert dialog.add_button.isVisible() and not dialog.edit_button.isEnabled()
    # 新規…: the satellites of the catalog to choose from; the form says what is missing.
    plan = p.docks["SatObsImagingPlan"]
    plan.load_catalog()
    assert wait_for(lambda: plan.catalog_sats), plan.status.text()
    form = dialog.add()
    assert form.satellite.findText("OPT 1") >= 0
    form.satellite.setCurrentText("")
    form._accept()
    assert form.problem.text() == "衛星を選ぶ" and form.isVisible()
    form.satellite.setCurrentText("OPT 1")
    form.reason.setText("姿勢制御の不具合")
    form.stop.setChecked(True)
    form.start.setDateTime(form.start.dateTime().addDays(-60))
    assert form.no_expected.isChecked() and not form.expected.isEnabled()
    form._accept()
    assert wait_for(lambda: dialog.task is None and len(fake.layers[OUTAGES].rows) == 1), dialog.status.text()
    row = fake.layers[OUTAGES].rows[0]["attributes"]
    assert (row["SAT_NAME"], row["STOP_IMAGING"], row["OUTAGE_REASON"], row["CREATED_USER"]) == ("OPT 1", 1, "姿勢制御の不具合", "tsudo")
    assert row["EXPECTED_END"] is None and row["RECOVERED_AT"] is None
    assert wait_for(lambda: dialog.model.rowCount() == 1)
    assert dialog.model.item(0, COLUMNS.index("状態")).text() == ONGOING
    assert dialog.model.item(0, COLUMNS.index("影響")).text() == "撮像停止"
    assert dialog.status.text().startswith("障害を登録。復旧していない障害 1 件")
    # Handed to the others at once: the satellite marked in the plan's list.
    assert len(p.context.known_outages()) == 1 and plan.catalog_sats[0].outages
    assert any("（障害）" in item.text(0) for item, _node in plan.tree._items)

    # The day's file: only OPT 1 could take them, so both wait for it.
    daily = shots_test._calculate(p, tmp_path)
    got = {c.request.name: (c.result, c.reason) for c in daily.plan.candidates}
    assert all(r != TAKEN and why.startswith(OUTAGE + "（OPT 1: 姿勢制御の不具合、撮像停止") for r, why in got.values()), got
    assert "。障害中の衛星: OPT 1（撮像停止）" in daily.status.text(), daily.status.text()

    # 復旧: the row keeps it as history, hidden from the list until 「復旧済みも表示」.
    dialog.table.selectRow(0)
    assert dialog.recover_button.isEnabled()
    dialog.recover()
    assert wait_for(lambda: dialog.task is None and fake.layers[OUTAGES].rows[0]["attributes"]["RECOVERED_AT"] is not None)
    assert wait_for(lambda: dialog.model.rowCount() == 0)
    dialog.show_recovered.setChecked(True)
    assert dialog.model.rowCount() == 1 and dialog.model.item(0, 0).text() == RECOVERED
    assert not any("（障害）" in item.text(0) for item, _node in plan.tree._items)
    # Recovered now is after the day calculated (in the past): still held back that day.
    daily = shots_test._calculate(p, tmp_path)
    assert all(c.result != TAKEN for c in daily.plan.candidates)
    # 編集…: recovered a day after it started, long before the day: OPT 1 is used again.
    dialog.table.selectRow(0)
    form = dialog.edit()
    assert form.has_recovered.isChecked() and form.stop.isChecked() and form.reason.text() == "姿勢制御の不具合"
    form.recovered.setDateTime(form.start.dateTime().addDays(1))
    form._accept()
    assert wait_for(lambda: dialog.task is None and dialog.status.text().startswith("障害を更新")), dialog.status.text()
    assert len(fake.layers[OUTAGES].rows) == 1
    daily = shots_test._calculate(p, tmp_path)
    assert any(c.result == TAKEN for c in daily.plan.candidates), [(c.result, c.reason) for c in daily.plan.candidates]
    assert "障害中の衛星" not in daily.status.text()
    QApplication.processEvents()


def test_those_who_ask_only_look(plugin):
    p, fake, _tmp_path = _with_outages(plugin)
    fake.layers[OUTAGES].add({"OUTAGE_ID": "OUT-1", "SAT_NAME": "OPT 1", "START_TIME": int(T), "NO_ASC_LEFT": 1,
                              "OUTAGE_REASON": "片側のみ"})
    dialog = OutageDialog(p.context, lambda: [], lambda feature: feature != "outages.edit")
    dialog.show()
    dialog.run()
    assert wait_for(lambda: dialog.task is None and dialog.model.rowCount() == 1), dialog.status.text()
    assert dialog.model.item(0, COLUMNS.index("影響")).text() == "使えない方向: 昇交左"
    assert not any(b.isVisible() for b in (dialog.add_button, dialog.edit_button, dialog.recover_button, dialog.delete_button))
    dialog.table.selectRow(0)
    assert dialog.add() is None and dialog.edit() is None
    dialog.delete(ask=False)
    assert len(fake.layers[OUTAGES].rows) == 1
    dialog.close()


def test_without_the_table_the_menu_says_so(plugin):
    p, _fake, _tmp_path = shots_test._setup(plugin)
    pushed = []
    p.iface.messageBar().pushInfo = lambda title, text: pushed.append(text)
    next(a for a in p.actions if a.objectName() == "SatObsOutages").trigger()
    assert p.outage_dialog is None and pushed == ["衛星の障害情報の表が未設定（管理者に連絡: satelliteOutages）"]


def test_shots_handed_out_are_marked_in_the_schedule_and_not_changed(plugin):
    from qgis.PyQt.QtCore import QDate

    from mikage.core.request.model import jst_day
    from mikage.tests import test_qgis_requests as base
    from mikage.ui.schedule_dock import COLUMNS as SCHEDULE_COLUMNS

    p, fake, tmp_path = _with_outages(plugin)
    daily = shots_test._calculate(p, tmp_path)
    daily.write(ask=False)
    assert wait_for(lambda: daily.task is None and len(fake.layers[shots_test.SHOTS].rows) == 1), daily.status.text()
    shot = fake.layers[shots_test.SHOTS].rows[0]["attributes"]
    # Entered after the file was handed out: the direction of its shot closed.
    side = {("昇交", "右"): "NO_ASC_RIGHT", ("昇交", "左"): "NO_ASC_LEFT", ("降交", "右"): "NO_DESC_RIGHT",
            ("降交", "左"): "NO_DESC_LEFT"}[(shot["ORBIT_DIR"], shot["LOOK_SIDE"])]
    fake.layers[OUTAGES].add({"OUTAGE_ID": "OUT-1", "SAT_NAME": "OPT 1", "START_TIME": int(T) - 86_400_000, side: 1,
                              "OUTAGE_REASON": "片側のみ"})
    before = [dict(r["attributes"]) for r in fake.layers[base.HISTORY].rows]
    dock = p.api.schedule
    day = jst_day(T)
    dock.calendar.setSelectedDate(QDate(day.year, day.month, day.day))
    dock.show()
    dock.reload(refresh=True)
    assert wait_for(lambda: dock._loaded and dock.task is None and dock.day == day.isoformat() and dock.outages), dock.status.text()
    notes = [dock.model.item(i, SCHEDULE_COLUMNS.index("理由・メモ")).text() for i in range(dock.model.rowCount())]
    assert notes and all(n.startswith("衛星の障害の影響あり（使えない方向）") for n in notes), notes
    assert "衛星の障害の影響あり 2 件（予定は変えない" in dock.status.text()
    assert "片側のみ" in dock.model.item(0, SCHEDULE_COLUMNS.index("衛星")).toolTip()
    # Nothing written back.
    assert [dict(r["attributes"]) for r in fake.layers[base.HISTORY].rows] == before
    # Read again without it (deleted as entered by mistake): no longer marked.
    p.context.set_outages([])
    assert wait_for(lambda: not dock.model.item(0, SCHEDULE_COLUMNS.index("理由・メモ")).text().startswith("衛星の障害"))
