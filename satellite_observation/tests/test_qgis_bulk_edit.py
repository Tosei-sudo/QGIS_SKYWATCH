"""
一括編集 in QGIS: several requests chosen in the 「撮像依頼」 dock, several
records (or things) in the 「観測記録」 dock, changed together in the dialog.
"""
import pytest

pytest.importorskip("qgis.core")
from qgis.testing import start_app  # noqa: E402

APP = start_app()

from qgis.PyQt.QtCore import QItemSelectionModel  # noqa: E402

from satellite_observation.tests.qgis_fakes import wait_for  # noqa: E402
from satellite_observation.tests.test_qgis_observations import plugin as obs_plugin  # noqa: E402,F401
from satellite_observation.tests.test_qgis_requests import LEDGER  # noqa: E402
from satellite_observation.tests.test_qgis_requests import plugin as request_plugin  # noqa: E402,F401

RING = {"rings": [[[139.6, 35.0], [139.7, 35.0], [139.7, 35.1], [139.6, 35.1], [139.6, 35.0]]], "spatialReference": {"wkid": 4326}}


def _choose(table, rows):
    sel = table.selectionModel()
    sel.clearSelection()
    for r in rows:
        sel.select(table.model().index(r, 0), QItemSelectionModel.SelectionFlag.Select | QItemSelectionModel.SelectionFlag.Rows)


def test_requests_chosen_together_get_a_priority_and_status(request_plugin):
    p, iface, fake, tmp_path = request_plugin
    ledger = fake.layers[LEDGER]
    for i, name in enumerate(("横須賀", "呉", "佐世保")):
        ledger.add({"REQ_ID": f"R-{i}", "REQ_NAME": name, "CATEGORY": "臨時", "STATUS": "有効", "PRIORITY": 500, "BOOST": 100,
                    "KIND": "光学", "UPDATED_AT": 1000}, RING)
    dock = p.api.requests
    dock.show()
    assert wait_for(lambda: dock._loaded and dock.task is None)
    assert not dock.bulk_button.isEnabled()
    names = [r.name for r in dock.shown]
    _choose(dock.table, [names.index("横須賀"), names.index("佐世保")])
    assert dock.bulk_button.isEnabled() and {r.name for r in dock.selected()} == {"横須賀", "佐世保"}
    dialog = dock.bulk_edit()
    dialog.tick("priority", 120)
    dialog.extras["reset_boost"].setChecked(True)
    dialog.tick("status", "停止")
    assert dialog.values() == {"priority": 120, "status": "停止", "reset_boost": True}
    dialog._ok()
    assert wait_for(lambda: dock.task is None and dock.status.text().startswith("2 件を変更")), dock.status.text()
    rows = {r["attributes"]["REQ_NAME"]: r["attributes"] for r in ledger.rows}
    assert [(rows[n]["PRIORITY"], rows[n]["STATUS"], rows[n]["BOOST"]) for n in ("横須賀", "呉", "佐世保")] == [
        (120, "停止", 0), (500, "有効", 100), (120, "停止", 0)]
    # The same ones are still chosen, for the next change.
    assert {r.name for r in dock.selected()} == {"横須賀", "佐世保"}


def test_a_request_changed_by_someone_else_is_named_and_the_rest_saved(request_plugin):
    p, iface, fake, tmp_path = request_plugin
    ledger = fake.layers[LEDGER]
    for i in range(2):
        ledger.add({"REQ_ID": f"R-{i}", "REQ_NAME": f"依頼{i}", "CATEGORY": "臨時", "STATUS": "有効", "PRIORITY": 500,
                    "UPDATED_AT": 1000}, RING)
    dock = p.api.requests
    dock.show()
    assert wait_for(lambda: dock._loaded and dock.task is None)
    ledger.rows[1]["attributes"].update(PRIORITY=50, UPDATED_AT=999_000)
    from satellite_observation.core.request.bulk import RequestChange
    assert dock.apply_bulk(list(dock.shown), RequestChange(kind="SAR"))
    assert wait_for(lambda: dock.task is None and "保存できず" in dock.status.text()), dock.status.text()
    assert "1 件を変更" in dock.status.text() and "依頼1" in dock.status.text()
    rows = {r["attributes"]["REQ_NAME"]: r["attributes"] for r in ledger.rows}
    assert (rows["依頼0"]["KIND"], rows["依頼1"].get("KIND"), rows["依頼1"]["PRIORITY"]) == ("SAR", None, 50)


def test_records_and_things_chosen_together(obs_plugin):
    p, iface, dock, layers, tmp_path = obs_plugin
    book = dock.book
    for name, lon in (("あおば", 139.65), ("いぶき", 139.7), ("うらが", 139.75)):
        book.add(lon, 35.6, object=name, kind="船舶", activity="停泊")
    dock.render()
    assert dock.tab == "image" and dock.table.rowCount() == 3
    assert not dock.bulk_button.isVisible()
    _choose(dock.table, [0, 2])
    assert dock.bulk_button.isVisible()
    assert sorted(o.object_name for o in dock.selected_records()) == ["あおば", "うらが"]
    dialog = dock.bulk_edit()
    dialog.tick("kind", "船舶 > 護衛艦")
    dialog.tick("confidence", "推定")
    dialog._ok()
    got = {o.object_name: o for o in book.living()}
    assert [(got[n].kind, got[n].confidence) for n in ("あおば", "いぶき", "うらが")] == [
        ("船舶 > 護衛艦", "推定"), ("船舶", ""), ("船舶 > 護衛艦", "推定")]
    assert dock.status.text().startswith("2 件の記録を一括変更")
    # On 「モノ」, a thing's row stands for all its records.
    dock.step(1)
    book.add(139.7, 35.6, object="いぶき", kind="船舶", activity="航行")
    dock.tabs.setCurrentIndex(1)
    names = [dock.table.item(r, 0).text() for r in range(dock.table.rowCount())]
    _choose(dock.table, [names.index("いぶき"), names.index("あおば")])
    assert len(dock.selected_records()) == 3
    dock.apply_bulk([o.id for o in dock.selected_records()], {"activity": "航行"})
    assert sorted(o.activity for o in book.living() if o.object_name in ("いぶき", "あおば")) == ["航行"] * 3


def test_records_and_things_chosen_together_are_deleted_after_asking(obs_plugin, monkeypatch):
    p, iface, dock, layers, tmp_path = obs_plugin
    book = dock.book
    for name, lon in (("あおば", 139.65), ("いぶき", 139.7), ("うらが", 139.75)):
        book.add(lon, 35.6, object=name, kind="船舶", activity="停泊")
    dock.render()
    assert not dock.remove_chosen_button.isVisible()
    _choose(dock.table, [0, 2])
    assert dock.remove_chosen_button.isVisible()
    asked = []
    monkeypatch.setattr(dock, "ask_remove", lambda records: asked.append(len(records)) or False)
    dock.remove_selected()
    assert asked == [2] and len(book.living()) == 3  # いいえ: nothing deleted
    monkeypatch.setattr(dock, "ask_remove", lambda records: asked.append(len(records)) or True)
    dock.remove_selected()
    assert sorted(o.object_name for o in book.living()) == ["いぶき"]
    assert dock.status.text() == "2 件の記録を削除"
    assert dock.table.rowCount() == 1 and not dock.remove_chosen_button.isVisible()
    # Restorable from 「削除済みも表示」.
    dock.deleted_check.setChecked(True)
    assert dock.table.rowCount() == 3
    dock.deleted_check.setChecked(False)
    # On 「モノ」, a thing's row stands for all its records.
    dock.step(1)
    book.add(139.7, 35.6, object="いぶき", kind="船舶", activity="航行")
    book.add(139.8, 35.6, object="えとろふ", kind="船舶", activity="航行")
    dock.tabs.setCurrentIndex(1)
    names = [dock.table.item(r, 0).text() for r in range(dock.table.rowCount())]
    _choose(dock.table, [names.index("いぶき"), names.index("えとろふ")])
    assert len(dock.selected_records()) == 3
    dock.remove_selected()
    assert book.living() == []


def test_the_question_says_whether_deleted_records_can_be_restored(obs_plugin, monkeypatch):
    p, iface, dock, layers, tmp_path = obs_plugin
    book = dock.book
    o = book.add(139.65, 35.6, object="あおば", kind="船舶", activity="停泊")
    seen = []
    monkeypatch.setattr("satellite_observation.ui.observation_dock.ask_no", lambda parent, title, text: seen.append(text) or False)
    assert dock.ask_remove([o]) is False
    assert "1 件（モノ 1 件）" in seen[0] and "復元できます" in seen[0]
    monkeypatch.setattr(book, "removes_for_good", lambda: True)
    dock.ask_remove([o])
    assert "元に戻せません" in seen[1]
