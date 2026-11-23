"""
「自分の記録」 (tsudo 2026-11-18: 過去の自分がどの画像のどの観測結果を作ったのかが
わかりにくいから、確認や修正が容易になる方法を): the window of one's records across
the images, a record's image opened by a double click (even when not open),
and 「この画像」's 記録者 column and 「自分の記録のみ」.
"""
import json
import time

import pytest

pytest.importorskip("qgis.core")
from qgis.testing import start_app  # noqa: E402

APP = start_app()

from qgis.core import QgsProject  # noqa: E402

from mikage.core import edit_stamp  # noqa: E402
from mikage.core.esri import EsriClient  # noqa: E402
from mikage.qgis_io import settings  # noqa: E402
from mikage.qgis_io.obs_images import image_layer  # noqa: E402
from mikage.tests.fake_esri import FakeEsri, FakeLayer  # noqa: E402
from mikage.tests.qgis_fakes import wait_for  # noqa: E402
from mikage.features.images import Images  # noqa: E402
from mikage.features.observations import Observations  # noqa: E402
from mikage.tests.test_qgis_observations import FIELDS, TYPES, URL, _listed, bare, geotiff, ms, plugin  # noqa: E402,F401

#: The features the plugin fixture starts with here (tests/feature_harness.py): the records' images opened from the catalog.
PLUGIN_FEATURES = (Images, Observations)


@pytest.fixture(autouse=True)
def user_name(monkeypatch):
    """The user name the edits are stamped with (USERNAME): oneself too."""
    monkeypatch.setattr(edit_stamp, "user_name", lambda *_a, **_k: "tanaka")


def _texts(table, column: int) -> list[str]:
    return [table.item(r, column).text() if table.item(r, column) else "" for r in range(table.rowCount())]


def _choose(table, rows) -> None:
    from qgis.PyQt.QtCore import QItemSelectionModel

    table.clearSelection()
    model, sel = table.model(), table.selectionModel()
    for r in rows:
        sel.select(model.index(r, 0), QItemSelectionModel.SelectionFlag.Select | QItemSelectionModel.SelectionFlag.Rows)
    sel.setCurrentIndex(model.index(rows[0], 0), QItemSelectionModel.SelectionFlag.NoUpdate)


def _record(dock, lon, observer="田中", **values):
    """A record on the image shown, by `observer`."""
    dock.set_observer(observer)
    o = dock.book.add(lon, 35.6, **values)
    dock.set_observer("田中")
    return o


def test_lists_ones_records_across_the_images_and_opens_them_by_a_double_click(plugin):
    p, iface, dock, layers, tmp_path = plugin
    book = dock.book
    first = _record(dock, 139.65, object="あおば", kind="船舶 > 護衛艦", activity="停泊")
    _record(dock, 139.7, observer="鈴木", object="鈴木さんのモノ", kind="船舶", activity="航行")
    dock.step(1)
    _record(dock, 139.72, observer="TANAKA", object="ユーザー名のモノ", kind="航空機", activity="駐機")
    dock.step(1)
    assert book.current.name == "d3.tif"
    # Opened from the menu (観測記録 ▸ 自分の記録…), a window of its own.
    [action] = [a for a in p.actions if a.objectName() == "SatObsMyRecords"]
    action.trigger()
    dialog = dock._my_records
    assert dialog is not None and dialog.isVisible() and dialog.isWindow() and not dialog.isModal()
    table = dialog.table
    assert dialog.who.currentText() == "自分（田中・tanaka）" and dialog.period.currentText() == "30 日"
    assert dialog.summary.text() == "自分の記録 2 件・画像 2 枚（直近 30 日）"
    assert [table.horizontalHeaderItem(c).text() for c in range(table.columnCount())] == \
        ["撮像日時", "画像", "モノ", "種別", "活動", "備考", "記録日時", "記録者", "更新者"]
    # The newest made first.
    assert _texts(table, 2) == ["ユーザー名のモノ", "あおば"] and _texts(table, 1) == ["d2.tif", "d1.tif"]
    # Everyone; another person by name; 大分類; a text.
    dialog.who.setCurrentIndex(dialog.who.findData("\x00all"))
    dialog.who.activated.emit(dialog.who.currentIndex())
    assert len(_texts(table, 2)) == 3 and dialog.summary.text().startswith("全員の記録 3 件")
    dialog.who.setCurrentIndex(dialog.who.findData("鈴木"))
    dialog.who.activated.emit(dialog.who.currentIndex())
    assert _texts(table, 2) == ["鈴木さんのモノ"]
    dialog.who.setCurrentIndex(0)
    dialog.who.activated.emit(0)
    dialog.root.setCurrentIndex(dialog.root.findData("航空機"))
    dialog.root.activated.emit(dialog.root.currentIndex())
    assert _texts(table, 2) == ["ユーザー名のモノ"]
    dialog.root.setCurrentIndex(0)
    dialog.root.activated.emit(0)
    dialog.text.setText("護衛艦")
    assert _texts(table, 2) == ["あおば"]
    dialog.text.clear()
    # A click only chooses: the image and the form stay.
    _choose(table, [1])
    assert book.current.name == "d3.tif" and not dock.form.isVisible() and dialog.open_button.isEnabled()
    # A double click: its image, the map on it, the record in the form.
    table.cellDoubleClicked.emit(1, 0)
    assert book.current.name == "d1.tif" and dock.form.isVisible() and dock.pending[2] is not None and dock.pending[2].id == first.id
    dock.close_form()
    dock.set_active(False)
    # 「画像ごと」: one row per image, its records per 大分類, which is open here.
    dialog.modes.setCurrentIndex(1)
    assert _texts(table, 1) == ["d2.tif", "d1.tif"] and _texts(table, 2) == ["1 件", "1 件"] and _texts(table, 3) == ["航空機 1", "船舶 1"]
    assert _texts(table, 5) == ["開いている", "開いている"] and dialog.image_button.isVisible() and not dialog.open_button.isVisible()
    table.cellDoubleClicked.emit(0, 0)
    assert book.current.name == "d2.tif" and not dock.form.isVisible()
    dialog.close()


def test_a_records_image_not_open_is_opened_before_its_record(plugin, monkeypatch):
    """tsudo 2026-11-18: 「画像（…）が開かれておらず移動不可」 no more: the image is opened (history, catalog), else listed without its file."""
    p, iface, dock, layers, tmp_path = plugin
    book = dock.book
    # A record on an image listed without its file, then taken off the list: its image is not open.
    dock.list_catalog_images([_listed("IMG9", 4)])
    gone_image = _record(dock, 139.7, object="一覧外のモノ", kind="船舶")
    dock._unlist()
    assert "IMG9" not in [i.id for i in book.images]
    # No catalog has it: listed without its file again (its name and time from the record), the record in the form.
    dock.open_record(gone_image)
    assert book.current.id == "IMG9" and not book.current.file and book.current.time == ms(4)
    assert dock.form.isVisible() and dock.pending[2].id == gone_image.id
    assert dock.status.text() == "IMG9 は画像カタログにないので、ファイルのない画像として一覧に追加"
    dock.close_form()
    dock.set_active(False)
    dock._unlist()
    # A catalog opens it (開いた画像の履歴 or the catalog by its id): the record opens once its layer is there.
    catalog = p.docks["SatObsImageCatalog"]
    asked = []

    def open_image_id(image_id, then=None, missing=None):
        asked.append(image_id)
        layer = image_layer(geotiff(tmp_path / "img9.tif", west=139.6), "img9.tif", image_id="IMG9", time=ms(4))
        QgsProject.instance().addMapLayer(layer)
        then([layer])

    monkeypatch.setattr(catalog, "open_image_id", open_image_id)
    dock.step(-5)
    dock.open_record(gone_image)
    assert asked == ["IMG9"] and book.current.id == "IMG9" and book.current.file
    assert dock.form.isVisible() and dock.pending[2].id == gone_image.id
    dock.close_form()
    dock.set_active(False)
    # A flow row clicked (an image not open) only says so; its double click opens it.
    from dataclasses import replace

    assert not dock.go_to_record(replace(gone_image, image_id="IMG10", image_name="IMG10"))
    assert dock.status.text() == "画像 IMG10 はまだ開いていない。ダブルクリックで開いて記録を編集"


def test_this_images_list_shows_who_recorded_and_only_ones_own(plugin):
    p, iface, dock, layers, tmp_path = plugin
    _record(dock, 139.65, object="自分のモノ", kind="船舶")
    _record(dock, 139.7, observer="鈴木", object="鈴木さんのモノ", kind="船舶")
    other = dock.book.living()[1]
    dock.book.update(other.id, note="直した")  # 田中 changes 鈴木's record
    dock.render()
    table = dock.table
    heads = [table.horizontalHeaderItem(c).text() for c in range(table.columnCount())]
    assert heads[5:7] == ["記録者", "更新"]
    assert sorted(_texts(table, 5)) == ["田中", "鈴木（更新 田中）"]
    assert dock.mine_only.isVisible() and dock.mine_only.text() == "自分の記録のみ"
    dock.mine_only.setChecked(True)
    assert _texts(table, 0) == ["自分のモノ"]
    # Carried over: only one's own things on the next image too.
    dock.step(1)
    assert _texts(table, 0) == ["自分のモノ"] and dock.carry_label.text() == "前後の画像のモノ 1 件"
    dock.set_observer("佐藤")
    dock.render()
    assert _texts(table, 0) == ["自分の記録なし。「自分の記録のみ」を外すと全員の記録を表示"]
    dock.set_observer("田中")
    dock.mine_only.setChecked(False)
    assert len(_texts(table, 0)) == 2


def test_chosen_records_are_changed_without_opening_their_images(plugin, monkeypatch):
    p, iface, dock, layers, tmp_path = plugin
    book = dock.book
    a = _record(dock, 139.65, object="A", kind="船舶", activity="停泊")
    dock.step(1)
    b = _record(dock, 139.7, object="B", kind="船舶", activity="停泊")
    dock.step(1)
    dialog = dock.open_my_records()
    table = dialog.table
    _choose(table, [0, 1])
    assert {o.id for o in dialog.chosen_records()} == {a.id, b.id}
    assert dialog.bulk_button.isEnabled() and dialog.remove_button.isEnabled() and not dialog.open_button.isEnabled()
    # 一括編集: the dock's dialog, on the dock's own records.
    dialog.bulk_edit()
    [edit] = [w for w in APP.topLevelWidgets() if w.windowTitle() == "観測記録の一括編集" and w.isVisible()]
    edit.reject()
    dock.apply_bulk([a.id, b.id], {"activity": "航行"})
    assert {o.activity for o in book.living()} == {"航行"}
    assert wait_for(lambda: set(_texts(table, 4)) == {"航行"})
    assert book.current.name == "d3.tif"  # no image opened
    # 削除 (after asking), then 「削除済みも表示」 and 「復元」.
    monkeypatch.setattr(dock, "ask_remove", lambda _r: True)
    _choose(table, [0])
    dialog.remove()
    assert len(book.living()) == 1
    assert wait_for(lambda: len(_texts(table, 2)) == 1)
    dialog.deleted.setChecked(True)
    assert len(_texts(table, 2)) == 2
    gone_row = [r for r in range(table.rowCount()) if table.item(r, 2).font().strikeOut()][0]
    _choose(table, [gone_row])
    assert dialog.restore_button.isVisible()
    dialog.restore()
    assert len(book.living()) == 2
    # 「画像カタログで表示」: the images of the chosen records in the catalog's list.
    shown = []
    catalog = p.docks["SatObsImageCatalog"]
    monkeypatch.setattr(catalog, "show_image_ids", lambda ids, title="": shown.append((ids, title)))
    _choose(table, [0, 1])
    dialog.show_in_catalog()
    assert shown and sorted(shown[0][0]) == sorted([a.image_id, b.image_id]) and shown[0][1] == "自分の記録の画像"
    dialog.close()


def test_an_esri_stores_own_records_are_asked_without_reading_them_all(bare):
    p, iface, dock, tmp_path = bare
    fake = FakeEsri()
    layer = fake.add_layer(URL, FakeLayer({n: TYPES.get(n, "esriFieldTypeString") for n in FIELDS}))
    now = int(time.time() * 1000)
    for i, (who, made) in enumerate((("田中", now - 3600_000), ("tanaka", now - 86_400_000), ("鈴木", now - 3600_000), ("田中", now - 40 * 86_400_000))):
        layer.rows.append({"attributes": {
            "OBJECTID": i + 1, "OBS_ID": f"r{i}", "IMAGE_ID": f"IMG{i}", "IMAGE_NAME": f"IMG{i}", "IMAGE_TIME": ms(1), "OBJECT_ID": f"t{i}",
            "OBJECT_NAME": f"モノ{i}", "KIND": "船舶", "ACTIVITY": "停泊", "PRESENT": 1, "NOTE": "", "OBSERVER": who, "UPDATED_BY": who,
            "CREATED_AT": made, "UPDATED_AT": made, "DELETED_AT": None}, "geometry": {"x": 139.7, "y": 35.6}})
    config = tmp_path / "config.json"
    config.write_text(json.dumps({"observationStores": [{"label": "共有", "url": URL}]}), encoding="utf-8")
    settings.set_config_source(str(config))
    p.context.esri = lambda: EsriClient(fake)
    p.context.reload()
    assert dock.store_set.remote and not dock.book.loaded
    dialog = dock.open_my_records()
    # One's own of the last 30 days, asked of the layer; the dock's records not read.
    assert wait_for(lambda: dialog.source == "made")
    assert _texts(dialog.table, 2) == ["モノ0", "モノ1"] and not dock.book.loaded
    asked = [f["where"] for u, f in fake.asked if u == URL + "/query" and "where" in f]
    assert any("UPPER(OBSERVER) IN ('田中', 'TANAKA')" in w for w in asked)
    # Everyone: everyone's of the period, asked of the layer too (never every record).
    fake.asked.clear()
    dialog.who.setCurrentIndex(dialog.who.findData("\x00all"))
    dialog.who.activated.emit(dialog.who.currentIndex())
    assert wait_for(lambda: len(_texts(dialog.table, 2)) == 3) and not dock.book.loaded
    asked = [f["where"] for u, f in fake.asked if u == URL + "/query" and "where" in f]
    assert asked and all("CREATED_AT >= TIMESTAMP" in w and "OBSERVER" not in w for w in asked)
    # A double click on an image not open: no catalog has it, listed without its file and the record opened.
    row = _texts(dialog.table, 2).index("モノ2")
    dialog.table.cellDoubleClicked.emit(row, 0)
    # (The record as it is on the server now, asked first.)
    assert wait_for(lambda: dock.book.current is not None and dock.book.current.id == "IMG2" and dock.form.isVisible())
    assert dock.pending[2].id == "r2"
    dialog.close()
