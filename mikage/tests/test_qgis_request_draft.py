"""
依頼文から下書き on the request form (ui/request_draft.py) with a model of
the test's own: the row only while the task has a model, the proposal from
a pasted text (the named area as the area, a place looked up in the
gazetteer), 「欄に入れる」 tinting the fields, two places made into the
batch form, and editing a request changing only what the text names.
"""
import json
import os
from datetime import date

import pytest

pytest.importorskip("qgis.core")

from qgis.PyQt.QtCore import QDate, QEvent  # noqa: E402
from qgis.PyQt.QtGui import QFocusEvent  # noqa: E402
from qgis.PyQt.QtWidgets import QApplication  # noqa: E402

from mikage.core.obs.alerts import forget_named_areas  # noqa: E402
from mikage.core.request.model import jst_day  # noqa: E402
from mikage.qgis_io import settings  # noqa: E402
from mikage.tests.fake_esri import FakeLayer  # noqa: E402
from mikage.tests.qgis_fakes import wait_for  # noqa: E402
from mikage.tests.test_qgis_requests import CONFIG, plugin  # noqa: E402,F401 (the fixture)
from mikage.tests.test_qgis_text_assist import FakeWriter  # noqa: E402

PLACES = "https://example.com/arcgis/rest/services/Places/FeatureServer/0"
YOKOSUKA = [[[139.65, 35.25], [139.75, 35.25], [139.75, 35.35], [139.65, 35.35], [139.65, 35.25]]]
TODAY = date(2026, 11, 21)  # a Saturday
MAIL = "お疲れさまです。来週月曜から2週間、横須賀港と呉を光学で毎日撮ってください。雲は30%以下、至急でお願いします。依頼者: 情報課 山田"
REPLY = "\n".join([
    "名称: 横須賀港 光学 監視", "地名: 横須賀港、呉", "期間: 2026-11-23 〜 2026-12-06", "頻度: 毎日", "優先度: 至急", "雲量: 30%以下",
    "分解能: なし", "オフナディア角: なし", "入射角: なし", "時間帯: なし", "1日の回数: なし", "依頼者: 情報課 山田"])


def _setup(p, fake, tmp_path, replies, task=True):
    """The settings with a textAssist (requestDraft given the model when `task`), the named areas and a gazetteer."""
    import threading

    forget_named_areas()
    places = fake.add_layer(PLACES, FakeLayer({"NAME": "esriFieldTypeString", "KIND": "esriFieldTypeString"}, "esriGeometryPolygon"))
    places.add({"NAME": "横須賀港", "KIND": "港"}, {"rings": YOKOSUKA})
    places.add({"NAME": "東京湾", "KIND": "海域"}, {"rings": [[[139.5, 35.0], [140.1, 35.0], [140.1, 35.7], [139.5, 35.0]]]})
    (tmp_path / "places.csv").write_text("名称,緯度,経度\n呉市,34.249,132.566\n", encoding="utf-8")
    gate = threading.Event()
    gate.set()
    loaded = []
    a = p.context.text_assist

    def writer_of(folder, threads):
        w = FakeWriter(folder, threads, replies, gate, 3.0)
        loaded.append(w)
        return w

    a._writer_of = writer_of
    a._folder_of = lambda source, base, cancelled=lambda: False: os.path.join(os.path.dirname(base), source)
    text_assist = {"models": {"large": "models/sarashina-3b"}, "tasks": {"requestDraft": "large"} if task else {"tidyNote": "large"}}
    config = {**CONFIG, "textAssist": text_assist, "observationExports": {"namedAreas": PLACES},
              "geocoders": [{"type": "csv", "url": "./places.csv", "label": "地名表"}]}
    (tmp_path / "config.json").write_text(json.dumps(config, ensure_ascii=False), encoding="utf-8")
    settings.set_config_source(str(tmp_path / "config.json"))
    p.context.reload()
    assert wait_for(lambda: a.available)
    dock = p.api.requests
    dock.show()
    assert wait_for(lambda: dock._loaded and dock.task is None)
    dock.draft_host.today = lambda: TODAY
    return dock, loaded


def _new_form(dock):
    dock.edit(new=True)
    form = dock.form
    assert wait_for(lambda: len(form.tree.items()) == 2)
    return form


def test_the_row_shows_only_while_the_task_has_a_model(plugin):
    p, iface, fake, tmp_path = plugin
    dock, _loaded = _setup(p, fake, tmp_path, [], task=False)
    form = _new_form(dock)
    row = form.draft_row
    assert row is not None and not row.widget.isVisibleTo(form) and not row.label.isVisibleTo(form)
    form.close()


def test_a_mail_made_into_the_fields_of_a_new_request(plugin):
    p, iface, fake, tmp_path = plugin
    dock, loaded = _setup(p, fake, tmp_path, [REPLY])
    form = _new_form(dock)
    row = form.draft_row
    assert row.widget.isVisibleTo(form) and not row.button.isEnabled()
    # At the top of the form.
    assert form._fields.itemAt(0, form._fields.ItemRole.LabelRole).widget() is row.label
    row.edit.setPlainText(MAIL)
    assert row.button.isEnabled()
    row.button.click()
    assert wait_for(lambda: row.panel.draft_value is not None and row.panel.replace_button.isVisible())
    asked = loaded[0].asked[0][0][-1]["content"].split("\n")
    # Only the name and the places: the words say the rest (the requester after 「依頼者:」).
    assert asked[0] == "今日: 2026-11-21（土）" and f"依頼の文: {MAIL}" in asked and asked[-3:] == ["出力は次の 2 行だけ:", "名称: ", "地名: "]
    assert loaded[0].until is not None
    panel = row.panel
    items = {key: check.text() for key, check in panel.checks.items()}
    assert items["name"] == "名称: 横須賀港 光学 監視"
    assert items["period"] == "期間: 2026-11-23（月） 〜 2026-12-06（日）（「来週月曜から2週間」から）"
    assert items["category"] == "区分: 臨時（「毎日」から）" and items["achieve"] == "達成条件: 毎回（「毎日」から）"
    assert items["priority"] == "優先度: 最優先（100）（「至急」から）"
    assert items["cloud_no_go"].startswith("実施しない雲量: 31%") and items["kind"] == "種別: 光学"
    assert items["requester"] == "依頼者: 情報課 山田"
    # The places: the named area, and the gazetteer's place for the one the table lacks.
    assert [(r.place, r.combo.currentText(), r.check.isChecked()) for r in panel.rows] == [
        ("横須賀港", "横須賀港（地名テーブル・港）", True), ("呉", "呉市（地名・住所検索・地名表・一辺 10 km）", True)]
    assert panel.replace_button.text() == "2 件の依頼にする"
    # One place: into the fields.
    panel.rows[1].check.setChecked(False)
    assert panel.replace_button.text() == "欄に入れる"
    panel.checks["requester"].setChecked(False)
    # Nothing changed until 「欄に入れる」.
    assert form.name.text() == "" and form.rings == []
    panel.replace_button.click()
    assert not panel.isVisible() and row.taken.isVisible() and "8 項目" in row.taken.text()
    assert form.name.text() == "横須賀港 光学 監視"
    assert (form.start.date(), form.end.date()) == (QDate(2026, 11, 23), QDate(2026, 12, 6))
    assert form.category.currentText() == "臨時" and form.achieve.currentText() == "毎回" and form.kind.currentText() == "光学"
    assert form.cloud.value() == 31 and form.level.currentText().startswith("最優先") and form.requester.text() == ""
    assert form.rings == YOKOSUKA and form.shape_kind == "範囲"
    assert form.note.toPlainText().startswith("依頼文（") and MAIL in form.note.toPlainText()
    marked = form.marked()
    assert {form.name, form.start, form.end, form.cloud, form.kind, form.area_label, form.note} <= set(marked)
    assert form.requester not in marked and "fef9c3" in form.name.styleSheet()
    # Touched: the tint goes.
    QApplication.sendEvent(form.name, QFocusEvent(QEvent.Type.FocusIn))
    assert form.name not in form.marked() and "fef9c3" not in form.name.styleSheet()
    form._save_clicked()
    assert wait_for(lambda: dock.task is None and len(dock.requests) == 1)
    saved = dock.requests[0]
    assert saved.name == "横須賀港 光学 監視" and saved.cloud_no_go == 31 and jst_day(saved.end) == date(2026, 12, 6)


def test_two_places_made_into_two_requests(plugin):
    p, iface, fake, tmp_path = plugin
    dock, loaded = _setup(p, fake, tmp_path, [REPLY])
    form = _new_form(dock)
    row = form.draft_row
    row.edit.setPlainText(MAIL)
    row.button.click()
    assert wait_for(lambda: row.panel.draft_value is not None and row.panel.replace_button.isVisible())
    assert row.panel.replace_button.text() == "2 件の依頼にする"
    row.panel.replace_button.click()
    batch = dock.form
    assert batch is not form and batch.batch is not None and batch.windowTitle() == "依頼文から 2 件"
    assert batch.draft_row is None and not batch.name.isEnabled() and batch.name.text() == "横須賀港 光学 監視、呉 光学 監視"
    assert batch.kind.currentText() == "光学" and batch.cloud.value() == 31
    batch._save_clicked()
    assert wait_for(lambda: dock.task is None and len(dock.requests) == 2)
    by_name = {r.name: r for r in dock.requests}
    assert set(by_name) == {"横須賀港 光学 監視", "呉 光学 監視"}
    assert by_name["横須賀港 光学 監視"].shape_kind == "範囲" and by_name["呉 光学 監視"].shape_kind == "点"
    assert all(r.requester == "情報課 山田" and r.cloud_no_go == 31 for r in dock.requests)
    xs = [x for x, _y in by_name["呉 光学 監視"].rings[0]]
    assert min(xs) < 132.566 < max(xs)


def test_editing_changes_only_what_the_text_names(plugin):
    p, iface, fake, tmp_path = plugin
    replies = [REPLY, "名称: 横須賀港 光学 定点"]
    dock, loaded = _setup(p, fake, tmp_path, replies)
    form = _new_form(dock)
    row = form.draft_row
    row.edit.setPlainText(MAIL)
    row.button.click()
    assert wait_for(lambda: row.panel.draft_value is not None and row.panel.replace_button.isVisible())
    row.panel.rows[1].check.setChecked(False)
    row.panel.replace_button.click()
    form._save_clicked()
    assert wait_for(lambda: dock.task is None and len(dock.requests) == 1)

    dock.chosen = dock.requests[0]
    dock.edit()
    form = dock.form
    assert form.windowTitle() == "撮像依頼の編集"
    row = form.draft_row
    assert row.widget.isVisibleTo(form)
    asked_before = sum(len(w.asked) for w in loaded)
    row.edit.setPlainText("例の横須賀港の件、12月末まで延長でお願いします")
    row.button.click()
    assert wait_for(lambda: row.panel.draft_value is not None and row.panel.replace_button.isVisible())
    # The words say it all: the model is not asked.
    assert sum(len(w.asked) for w in loaded) == asked_before
    # Only the end of the period; the area stays (the text does not ask to change it).
    assert list(row.panel.checks) == ["period"] and row.panel.rows == []
    assert row.panel.checks["period"].text().startswith("期間: 〜 2026-12-31（木）")
    row.panel.replace_button.click()
    assert (form.start.date(), form.end.date()) == (QDate(2026, 11, 23), QDate(2026, 12, 31))
    assert form.end in form.marked() and form.start not in form.marked() and form.name.text() == "横須賀港 光学 監視"
    form._save_clicked()
    assert wait_for(lambda: dock.task is None and jst_day(dock.requests[0].end) == date(2026, 12, 31))
    assert len(dock.requests) == 1 and sorted(map(tuple, dock.requests[0].rings[0])) == sorted(map(tuple, YOKOSUKA[0]))

    # A new name asked for: only that line asked of the model (and the request being changed said).
    dock.chosen = dock.requests[0]
    dock.edit()
    form = dock.form
    row = form.draft_row
    row.edit.setPlainText("名称を横須賀港の定点観測に変えてください")
    row.button.click()
    assert wait_for(lambda: row.panel.draft_value is not None and row.panel.replace_button.isVisible())
    asked = loaded[-1].asked[-1][0][-1]["content"]
    assert "直す依頼: 横須賀港 光学 監視（期間 2026-11-23 〜 2026-12-31）" in asked and asked.endswith("出力は次の 2 行だけ:\n名称: \n頻度: ")
    assert list(row.panel.checks) == ["name", "category"] and row.panel.checks["name"].text() == "名称: 横須賀港 光学 定点"
    row.panel.checks["category"].setChecked(False)
    row.panel.replace_button.click()
    assert form.name.text() == "横須賀港 光学 定点" and form.marked() == [form.name, form.note]
    form.close()

    # A text that names no change: said so.
    dock.chosen = dock.requests[0]
    dock.edit()
    row = dock.form.draft_row
    row.edit.setPlainText("よろしくお願いします")
    row.button.click()
    assert wait_for(lambda: row.panel.isVisible() and not row.panel.cancel_button.isVisible())
    assert row.panel.label.text().startswith("直すところなし") and not row.panel.replace_button.isVisible()
    dock.form.close()
