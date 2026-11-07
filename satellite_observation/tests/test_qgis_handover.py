"""「分担」 and 「引き継ぎメモ」 in QGIS: the dock's 分担 line, the dialog's tabs, Esri read and written in tasks."""
import json

import pytest

pytest.importorskip("qgis.core")
from qgis.testing import start_app  # noqa: E402

APP = start_app()

from qgis.PyQt.QtWidgets import QApplication  # noqa: E402

from satellite_observation.core.esri import EsriClient  # noqa: E402
from satellite_observation.qgis_io import settings  # noqa: E402
from satellite_observation.tests.fake_esri import FakeEsri, FakeLayer  # noqa: E402
from satellite_observation.tests.qgis_fakes import wait_for  # noqa: E402
from satellite_observation.tests.test_qgis_alerts import plugin  # noqa: E402,F401

WORK = "https://x/arcgis/rest/services/Work/FeatureServer/0"
MEMO = "https://x/arcgis/rest/services/Work/FeatureServer/1"
S, D = "esriFieldTypeString", "esriFieldTypeDate"


def _setup(p, tmp_path, user="田中", memo_table=True):
    fake = FakeEsri()
    work = fake.add_layer(WORK, FakeLayer({"IMAGE_ID": S, "IMAGE_NAME": S, "IMAGE_TIME": D, "ASSIGNEE": S, "STATUS": S, "NOTE": S,
                                           "TAKEN_AT": D, "DONE_AT": D}, ""))
    memo = fake.add_layer(MEMO, FakeLayer({"TEXT": S, "MEMO": S, "PERIOD_START": D, "PERIOD_END": D, "WRITTEN_AT": D, "WRITTEN_BY": S}, ""))
    config = tmp_path / "config.json"
    config.write_text(json.dumps({"assignments": WORK, "editTracking": {"user": user},
                                  "handover": {"table": MEMO, "newline": "lf"} if memo_table else {"newline": "lf"}}), encoding="utf-8")
    settings.set_config_source(str(config))
    p.context.esri = lambda: EsriClient(fake)
    p.context.reload()
    return fake, work, memo


def test_take_an_image_and_someone_else_sees_it_held(plugin):
    p, iface, dock, tmp_path = plugin
    fake, work, _ = _setup(p, tmp_path)
    bar = dock.assignment_bar
    assert bar.isVisible()
    assert wait_for(lambda: bar.read_at > 0)
    assert bar.label.text() == "分担: 担当者なし" and bar.take_button.isVisible()
    bar.take()
    assert wait_for(lambda: bar.label.text().startswith("分担: 自分が担当中"))
    assert work.rows[0]["attributes"]["ASSIGNEE"] == "田中" and work.rows[0]["attributes"]["IMAGE_ID"] == dock.book.current.id
    assert bar.done_button.isVisible() and not bar.take_button.isVisible()
    # Another person (another user name) sees it held, and cannot take it.
    p.context.config.edit_tracking = type(p.context.config.edit_tracking)(user="鈴木")
    from satellite_observation.core import edit_stamp

    edit_stamp.set_tracking(p.context.config.edit_tracking)
    bar.render()
    assert "田中 さんが担当中" in bar.label.text() and not bar.take_button.isVisible()
    bar.take()
    assert wait_for(lambda: "田中 さんが担当中" in dock.status.text())
    assert len(work.rows) == 1


def test_dialog_lists_changes_state_and_hands_over(plugin):
    p, iface, dock, tmp_path = plugin
    fake, work, _ = _setup(p, tmp_path)
    image = dock.book.current
    work.add({"IMAGE_ID": image.id, "IMAGE_NAME": image.name, "ASSIGNEE": "田中", "STATUS": "作業中", "TAKEN_AT": 0})
    work.add({"IMAGE_ID": "OTHER", "IMAGE_NAME": "other.tif", "ASSIGNEE": "鈴木", "STATUS": "完了", "TAKEN_AT": 0})
    dock.book.add(139.7, 35.6, object="船A", kind="船舶", activity="航行")
    next(a for a in p.actions if a.objectName() == "SatObsAssignments").trigger()
    dialog = dock._handover
    assert dialog.isVisible() and dialog.tabs.currentIndex() == 0
    assert wait_for(lambda: dialog.table.rowCount() == 1)
    assert dialog.table.item(0, 2).text() == "田中" and dialog.table.item(0, 4).text() == "記録 1 件"
    dialog.done_check.setChecked(True)
    assert dialog.table.rowCount() == 2
    dialog.done_check.setChecked(False)
    dialog.table.selectRow(0)
    dialog.status_select.setCurrentText("保留")
    dialog.change_status(confirm=False)
    assert wait_for(lambda: work.rows[0]["attributes"]["STATUS"] == "保留")
    assert wait_for(lambda: dialog.table.rowCount() == 1 and dialog.table.item(0, 3).text() == "保留")
    dialog.table.selectRow(0)
    dialog.change_note("北側は雲")
    assert wait_for(lambda: work.rows[0]["attributes"]["NOTE"] == "北側は雲")
    assert wait_for(lambda: dialog.table.item(0, 6) is not None and dialog.table.item(0, 6).text() == "北側は雲")
    dialog.table.selectRow(0)
    dialog.hand_over("佐藤", confirm=False)
    assert wait_for(lambda: work.rows[0]["attributes"]["ASSIGNEE"] == "佐藤")
    assert wait_for(lambda: "佐藤 さんが担当中" in dock.assignment_bar.label.text())


def test_handover_memo_made_kept_and_read_next_time(plugin):
    p, iface, dock, tmp_path = plugin
    fake, work, memo = _setup(p, tmp_path)
    image = dock.book.current
    work.add({"IMAGE_ID": image.id, "IMAGE_NAME": image.name, "ASSIGNEE": "田中", "STATUS": "作業中", "TAKEN_AT": 0})
    dock.book.add(139.7, 35.6, object="船A", kind="船舶", activity="航行")
    next(a for a in p.actions if a.objectName() == "SatObsHandover").trigger()
    dialog = dock._handover
    assert dialog.tabs.currentIndex() == 1
    assert wait_for(lambda: "なし（30 日以内）" in dialog.last_label.text())
    dialog.memo_edit.setPlainText("港の北側は未確認")
    dialog.make()
    assert wait_for(lambda: dialog.text_edit.toPlainText().startswith("引き継ぎメモ"))
    text = dialog.text_edit.toPlainText()
    assert "■ 観測記録: 新規 1 件・更新 0 件・削除 0 件" in text and "  田中: 担当中 1 件・完了 0 件" in text
    assert "■ 申し送り\n  港の北側は未確認" in text and "通報" not in text
    dialog.save()
    assert wait_for(lambda: "テーブルに保存" in dialog.status.text())
    assert memo.rows[0]["attributes"]["TEXT"] == text and memo.rows[0]["attributes"]["WRITTEN_BY"] == "田中"
    assert QApplication.clipboard().text() == text
    # The next shift: the memo kept is shown, and the period starts where it ended.
    assert wait_for(lambda: "前回の引き継ぎメモ（田中" in dialog.last_label.text())
    assert dialog.last_text.toPlainText() == text
    assert dialog.start_edit.dateTime().toMSecsSinceEpoch() == memo.rows[0]["attributes"]["PERIOD_END"]


def test_without_assignments_the_bar_is_hidden_and_the_memo_still_works(plugin):
    p, iface, dock, tmp_path = plugin
    assert not dock.assignment_bar.isVisible()
    dialog = dock.open_handover(0)
    assert dialog.tabs.currentIndex() == 1 and not dialog.tabs.isTabEnabled(0)
    assert "handover.table" in dialog.last_label.text()
    dialog.make()
    assert wait_for(lambda: dialog.text_edit.toPlainText().startswith("引き継ぎメモ"))
    assert "■ 分担" not in dialog.text_edit.toPlainText()
