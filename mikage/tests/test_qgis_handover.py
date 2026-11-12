"""「分担」 and 「引き継ぎメモ」 in QGIS: the dock's 分担 line, the dialog's tabs, Esri read and written in tasks."""
import json

import pytest

pytest.importorskip("qgis.core")
from qgis.testing import start_app  # noqa: E402

APP = start_app()

from qgis.PyQt.QtWidgets import QApplication  # noqa: E402

from mikage.core.esri import EsriClient  # noqa: E402
from mikage.qgis_io import settings  # noqa: E402
from mikage.tests.fake_esri import FakeEsri, FakeLayer  # noqa: E402
from mikage.tests.qgis_fakes import wait_for  # noqa: E402
from mikage.tests.test_qgis_alerts import plugin  # noqa: E402,F401

WORK = "https://x/arcgis/rest/services/Work/FeatureServer/0"
MEMO = "https://x/arcgis/rest/services/Work/FeatureServer/1"
S, D = "esriFieldTypeString", "esriFieldTypeDate"


def _setup(p, tmp_path, user="田中", memo_table=True, mode=None):
    fake = FakeEsri()
    work = fake.add_layer(WORK, FakeLayer({"IMAGE_ID": S, "IMAGE_NAME": S, "IMAGE_TIME": D, "ASSIGNEE": S, "STATUS": S, "NOTE": S,
                                           "TAKEN_AT": D, "DONE_AT": D}, ""))
    memo = fake.add_layer(MEMO, FakeLayer({"TEXT": S, "MEMO": S, "PERIOD_START": D, "PERIOD_END": D, "WRITTEN_AT": D, "WRITTEN_BY": S}, ""))
    config = tmp_path / "config.json"
    config.write_text(json.dumps({"assignments": WORK if mode is None else {"url": WORK, "mode": mode}, "editTracking": {"user": user},
                                  "handover": {"table": MEMO, "newline": "lf"} if memo_table else {"newline": "lf"}}), encoding="utf-8")
    settings.set_config_source(str(config))
    p.context.esri = lambda: EsriClient(fake)
    p.context.reload()
    return fake, work, memo


def test_take_an_image_and_someone_else_sees_it_held(plugin):
    p, iface, dock, tmp_path = plugin
    fake, work, _ = _setup(p, tmp_path, mode="exclusive")
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
    from mikage.core import edit_stamp

    edit_stamp.set_tracking(p.context.config.edit_tracking)
    bar.render()
    assert "田中 さんが担当中" in bar.label.text() and not bar.take_button.isVisible()
    bar.take()
    assert wait_for(lambda: "田中 さんが担当中" in dock.status.text())
    assert len(work.rows) == 1


def test_dialog_lists_changes_state_and_hands_over(plugin):
    p, iface, dock, tmp_path = plugin
    fake, work, _ = _setup(p, tmp_path, mode="exclusive")
    image = dock.book.current
    work.add({"IMAGE_ID": image.id, "IMAGE_NAME": image.name, "ASSIGNEE": "田中", "STATUS": "作業中", "TAKEN_AT": 0})
    work.add({"IMAGE_ID": "OTHER", "IMAGE_NAME": "other.tif", "ASSIGNEE": "鈴木", "STATUS": "完了", "TAKEN_AT": 0})
    dock.book.add(139.7, 35.6, object="船A", kind="船舶", activity="航行")
    next(a for a in p.actions if a.objectName() == "SatObsAssignments").trigger()
    dialog = dock._handover
    assert dialog.isVisible() and dialog.tabs.currentIndex() == 0
    assert wait_for(lambda: dialog.table.rowCount() == 1)
    assert dialog.table.item(0, 2).text() == "田中" and dialog.table.item(0, 4).text() == "記録 1 件（船舶 1）"
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


def test_the_buttons_ask_as_when_clicked(plugin, monkeypatch):
    """The buttons' clicked(bool) does not reach the slots' own arguments (a crash on メモ…, asking skipped)."""
    from mikage.ui import handover_dialog

    p, iface, dock, tmp_path = plugin
    fake, work, _ = _setup(p, tmp_path)
    work.add({"IMAGE_ID": "IMG9", "IMAGE_NAME": "img9.tif", "ASSIGNEE": "鈴木", "STATUS": "作業中", "TAKEN_AT": 0})
    dialog = dock.open_handover(0)
    assert wait_for(lambda: dialog.table.rowCount() == 1)
    dialog.table.selectRow(0)
    monkeypatch.setattr(handover_dialog.QInputDialog, "getMultiLineText", staticmethod(lambda *a: ("北側から", True)))
    asked = []
    monkeypatch.setattr(handover_dialog.QMessageBox, "question",
                        staticmethod(lambda *a: asked.append(a[1]) or handover_dialog.QMessageBox.StandardButton.No))
    dialog.note_button.click()
    assert wait_for(lambda: work.rows[0]["attributes"]["NOTE"] == "北側から")
    # Someone else's row: asked first, and left as it is on いいえ.
    dialog.table.selectRow(0)
    dialog.status_select.setCurrentText("完了")
    dialog.status_button.click()
    monkeypatch.setattr(handover_dialog.QInputDialog, "getText", staticmethod(lambda *a: ("佐藤", True)))
    dialog.hand_button.click()
    assert asked == ["状態の変更", "担当の変更"]
    assert work.rows[0]["attributes"]["STATUS"] == "作業中" and work.rows[0]["attributes"]["ASSIGNEE"] == "鈴木"


# ---- 「クリック記録」 takes the image -------------------------------------------------


def _record_config(p, tmp_path, fake, claim=None, user="田中"):
    value = WORK if claim is None else {"url": WORK, "claimOnRecord": claim}
    (tmp_path / "config.json").write_text(json.dumps({"assignments": value, "editTracking": {"user": user}}), encoding="utf-8")
    p.context.esri = lambda: EsriClient(fake)
    p.context.reload()


def test_recording_starts_by_taking_the_image(plugin):
    p, iface, dock, tmp_path = plugin
    fake, work, _ = _setup(p, tmp_path)
    assert wait_for(lambda: dock.assignment_bar.read_at > 0)
    dock.set_active(True)
    assert not dock.active
    assert wait_for(lambda: dock.active)
    assert len(work.rows) == 1 and work.rows[0]["attributes"]["ASSIGNEE"] == "田中"
    assert work.rows[0]["attributes"]["IMAGE_ID"] == dock.book.current.id
    assert dock.record_button.isChecked() and dock.record_button.isEnabled() and dock.record_button.text() == "クリック記録中"
    assert "を担当に" in dock.status.text()
    assert wait_for(lambda: dock.assignment_bar.label.text().startswith("分担: 自分が担当中"))
    # Again (one's own already): at once, nothing more written.
    dock.set_active(False)
    dock.set_active(True)
    assert dock.active and len(work.rows) == 1


def test_recording_is_refused_on_an_image_someone_else_holds(plugin):
    p, iface, dock, tmp_path = plugin
    fake, work, _ = _setup(p, tmp_path, mode="exclusive")
    work.add({"IMAGE_ID": dock.book.current.id, "ASSIGNEE": "鈴木", "STATUS": "作業中", "TAKEN_AT": 0})
    dock.set_active(True)
    assert wait_for(lambda: "クリック記録できません" in dock.status.text())
    assert "鈴木 さんが担当中" in dock.status.text()
    assert not dock.active and not dock.record_button.isChecked() and dock.record_button.isEnabled()
    assert dock.record_button.text() == "クリック記録"
    assert len(work.rows) == 1
    assert any("クリック記録できません" in w.text() for w in iface.bar.items())


def test_recording_is_refused_when_the_table_cannot_be_reached(plugin):
    p, iface, dock, tmp_path = plugin
    fake, work, _ = _setup(p, tmp_path, mode="exclusive")
    from mikage.core.esri import EsriError

    def offline(_url, _form):
        raise EsriError("接続できません")

    p.context.esri = lambda: EsriClient(offline)
    dock.set_active(True)
    assert wait_for(lambda: "クリック記録できません" in dock.status.text())
    assert not dock.active and not work.rows


def test_recording_takes_each_image_it_goes_to(plugin):
    p, iface, dock, tmp_path = plugin
    fake, work, _ = _setup(p, tmp_path, mode="exclusive")
    first = dock.book.current
    dock.set_active(True)
    assert wait_for(lambda: dock.active)
    work.add({"IMAGE_ID": "B", "ASSIGNEE": "鈴木", "STATUS": "作業中", "TAKEN_AT": 0})
    dock.book.list_image("A", "a.tif", None, None)
    dock._went()
    assert wait_for(lambda: dock.active and dock.book.current.id == "A")
    assert {r["attributes"]["IMAGE_ID"] for r in work.rows} == {first.id, "A", "B"}
    dock.book.list_image("B", "b.tif", None, None)
    dock._went()
    assert wait_for(lambda: "クリック記録できません" in dock.status.text())
    assert not dock.active and len(work.rows) == 3


def test_shared_recording_starts_beside_someone_else(plugin):
    """協業 (the default): someone else holding the image does not stop 「クリック記録」; both are named, with their 大分類."""
    p, iface, dock, tmp_path = plugin
    fake, work, _ = _setup(p, tmp_path)
    assert p.context.config.assignments.shared is True
    image = dock.book.current
    work.add({"IMAGE_ID": image.id, "ASSIGNEE": "鈴木", "STATUS": "作業中", "TAKEN_AT": 0})
    dock.book.add(139.7, 35.6, object="機A", kind="航空機 > 戦闘機", activity="駐機")
    dock.book.records[-1].recorded_by = "鈴木"
    bar = dock.assignment_bar
    assert wait_for(lambda: bar.read_at > 0)
    dock.render()
    assert bar.label.text() == "分担: 鈴木 さん（航空機 1）が担当中。一緒に担当できます"
    assert bar.take_button.isVisible() and bar.take_button.text() == "一緒に担当" and not bar.done_button.isVisible()
    dock.set_active(True)
    assert wait_for(lambda: dock.active)
    assert sorted(r["attributes"]["ASSIGNEE"] for r in work.rows) == ["田中", "鈴木"]
    dock.book.add(139.7, 35.6, object="船A", kind="船舶", activity="航行")
    dock.book.records[-1].recorded_by = "田中"
    assert wait_for(lambda: bar.read_at > 0 and len(bar.items) == 2)
    dock.render()
    assert bar.label.text() == "分担: 自分（船舶 1）・鈴木 さん（航空機 1）で担当中（協業）"
    assert bar.done_button.isVisible() and bar.release_button.isVisible() and not bar.take_button.isVisible()
    # 完了 marks one's own row only.
    bar.set_status("完了")
    assert wait_for(lambda: any(r["attributes"]["ASSIGNEE"] == "田中" and r["attributes"]["STATUS"] == "完了" for r in work.rows))
    assert next(r for r in work.rows if r["attributes"]["ASSIGNEE"] == "鈴木")["attributes"]["STATUS"] == "作業中"


def test_shared_recording_goes_on_when_the_table_cannot_be_reached(plugin):
    """協業: nobody is kept out, so a table that cannot be written only warns."""
    p, iface, dock, tmp_path = plugin
    fake, work, _ = _setup(p, tmp_path)
    from mikage.core.esri import EsriError

    def offline(_url, _form):
        raise EsriError("接続できません")

    p.context.esri = lambda: EsriClient(offline)
    dock.set_active(True)
    assert wait_for(lambda: dock.active)
    assert "分担を記録できませんでした" in dock.status.text() and dock.record_button.isChecked()
    assert any("分担を記録できませんでした" in w.text() for w in iface.bar.items())
    assert not work.rows


def test_recording_does_not_take_when_switched_off_or_without_the_table(plugin):
    p, iface, dock, tmp_path = plugin
    dock.set_active(True)
    assert dock.active
    dock.set_active(False)
    fake, work, _ = _setup(p, tmp_path)
    _record_config(p, tmp_path, fake, claim=False)
    assert p.context.config.assignments.claim_on_record is False
    dock.set_active(True)
    assert dock.active and not work.rows


def test_the_connection_check_dialog_lists_and_copies(plugin):
    """「接続と表の点検…」: the services of the settings asked in a task, problems first, copied as text."""
    p, iface, dock, tmp_path = plugin
    fake, work, memo = _setup(p, tmp_path)
    work.types.pop("ASSIGNEE")  # a needed attribute gone
    dialog = p.open_health_check()
    assert wait_for(lambda: dialog.results)
    top = [dialog.tree.topLevelItem(i) for i in range(dialog.tree.topLevelItemCount())]
    assert top[0].text(0) == "問題" and top[0].text(1) == "assignments"
    assert any(t.text(1) == "handover.table" and t.text(0) in ("OK", "注意") for t in top)
    text = dialog.copy()
    assert text.startswith("接続と表の点検（") and "属性 ASSIGNEE がない" in text
    assert top[0].child(0).text(0).startswith("問題: 属性 ASSIGNEE がない")
    assert QApplication.clipboard().text() == text
    # Asked again: the same dialog.
    assert p.open_health_check() is dialog
    assert wait_for(lambda: dialog.again_button.isEnabled())
