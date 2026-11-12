"""一覧の「ビュー」: columns shown / hidden / reordered, the sort and the filters kept under a name, per user and per list;
the order of the columns from the menus (the header's right-click, 「列の表示と順番…」) on every list of the docks."""
import json

import pytest

pytest.importorskip("qgis.core")
from qgis.testing import start_app  # noqa: E402

APP = start_app()

from qgis.core import QgsProject, QgsSettings  # noqa: E402
from qgis.PyQt.QtCore import Qt  # noqa: E402
from qgis.PyQt.QtGui import QStandardItem, QStandardItemModel  # noqa: E402
from qgis.PyQt.QtWidgets import QComboBox, QDialogButtonBox, QLineEdit, QTableView, QTreeWidget, QTreeWidgetItem  # noqa: E402

from mikage.core.config import parse_config  # noqa: E402
from mikage.ui.list_views import ROOT, ColumnsDialog, ListViews, column_drag, column_menus, set_column_drag  # noqa: E402
from mikage.tests.test_qgis_alerts import plugin  # noqa: E402,F401

HEAD = ["名称", "区分", "期間", "状態"]


@pytest.fixture(autouse=True)
def clean():
    QgsSettings().remove(ROOT)
    set_column_drag(True)
    yield
    QgsSettings().remove(ROOT)
    set_column_drag(True)
    QgsProject.instance().removeAllMapLayers()


def fill(model, head=HEAD):
    model.clear()
    model.setHorizontalHeaderLabels(head)
    for r in (["b", "定点", "", "有効"], ["a", "臨時", "", "停止"]):
        model.appendRow([QStandardItem(t) for t in r[:len(head)]])


def settle():
    for _ in range(3):
        APP.processEvents()


def test_columns_sort_and_filters_under_a_name():
    model = QStandardItemModel()
    fill(model)
    view = QTableView()
    view.setModel(model)
    view.setSortingEnabled(True)
    kind, text = QComboBox(), QLineEdit()
    kind.addItem("すべて", "")
    kind.addItem("定点", "fixed")
    applied = []
    views = ListViews("test", view, {"kind": kind, "text": text}, lambda: applied.append(1))
    h = view.horizontalHeader()
    assert h.sectionsMovable()
    assert [c["title"] for c in views.columns()] == HEAD
    # Hidden and moved, as the header's menu and a drag do.
    assert views.set_hidden("期間", True)
    h.moveSection(h.visualIndex(3), 0)
    assert [(c["title"], c["hidden"]) for c in views.columns()] == [("状態", False), ("名称", False), ("区分", False), ("期間", True)]
    view.sortByColumn(0, Qt.SortOrder.AscendingOrder)
    settle()
    kind.setCurrentIndex(1)
    text.setText("港")
    assert views.save("定点の見方") == "定点の見方" and views.names() == ["定点の見方"]

    # Another arrangement, then the view chosen again.
    views.reset()
    kind.setCurrentIndex(0)
    text.setText("")
    view.sortByColumn(1, Qt.SortOrder.DescendingOrder)
    assert [c["hidden"] for c in views.columns()] == [False] * 4 and [c["title"] for c in views.columns()] == HEAD
    assert views.choose("定点の見方") and applied == [1]
    assert [(c["title"], c["hidden"]) for c in views.columns()] == [("状態", False), ("名称", False), ("区分", False), ("期間", True)]
    assert (h.sortIndicatorSection(), h.sortIndicatorOrder()) == (0, Qt.SortOrder.AscendingOrder)
    assert kind.currentData() == "fixed" and text.text() == "港"
    # The list made again (a new search): its arrangement comes back, also a column it did not have before.
    fill(model, HEAD + ["備考"])
    settle()
    assert [(c["title"], c["hidden"]) for c in views.columns()] == [("状態", False), ("名称", False), ("区分", False), ("期間", True),
                                                                    ("備考", False)]
    # Not every column can be hidden.
    for t in ("状態", "名称", "区分", "備考"):
        views.set_hidden(t, True)
    assert sum(1 for c in views.columns() if not c["hidden"]) == 1
    # Kept per user: another list object of the same key reads it.
    again = ListViews("test", QTableView())
    assert again.names() == ["定点の見方"]
    assert views.delete("定点の見方") and views.names() == [] and not views.choose("定点の見方")
    # The menu lists the views and the columns.
    views.save("A")
    views.button.menu().aboutToShow.emit()
    texts = [a.text() for a in views.button.menu().actions()]
    assert texts[0] == "A" and "今の表示を名前を付けて保存…" in texts and "標準の表示に戻す" in texts and "列の表示と順番…" in texts


def titles(views):
    return [c["title"] for c in views.columns() if not c["hidden"]]


def menu_texts(menu):
    return [(a.text(), a.isEnabled()) for a in menu.actions() if not a.isSeparator()]


def test_columns_moved_from_the_header_menu():
    model = QStandardItemModel()
    fill(model)
    view = QTableView()
    view.setModel(model)
    views = ListViews("test", view)
    # The column right-clicked: its moves first, those it cannot do greyed.
    menu = views.columns_menu(column=0)
    texts = menu_texts(menu)
    assert texts[:4] == [("この列を左へ", False), ("この列を右へ", True), ("この列を先頭へ", False), ("この列を末尾へ", True)]
    assert [t for t, _on in texts[4:]] == HEAD + ["列の表示と順番…", "標準の表示に戻す"]
    # Without a column (「ビュー ▸ 列の表示・順番」): no moves.
    assert [t for t, _on in menu_texts(views.columns_menu())][0] == "名称"
    [a for a in menu.actions() if a.text() == "この列を右へ"][0].trigger()
    assert titles(views) == ["区分", "名称", "期間", "状態"]
    assert views.move(0, "last") and titles(views) == ["区分", "期間", "状態", "名称"]
    assert views.move(0, "first") and titles(views) == HEAD[:1] + ["区分", "期間", "状態"]
    assert not views.move(0, "left") and not views.move(3, "right")
    # A hidden column is stepped over.
    views.set_hidden("区分", True)
    assert views.move(2, "left") and titles(views) == ["期間", "名称", "状態"]
    assert not views.can_move(1, "left") and not views.can_move(1, "right")
    # Kept: the list made again, or another one of the same key, is arranged so.
    fill(model)
    settle()
    assert titles(views) == ["期間", "名称", "状態"]
    other = QStandardItemModel()
    fill(other)
    again = QTableView()
    again.setModel(other)
    kept = column_menus("test", again)
    assert kept.button is None and titles(kept) == ["期間", "名称", "状態"]
    assert [(c["title"], c["hidden"]) for c in kept.columns()] == [("期間", False), ("名称", False), ("区分", True), ("状態", False)]


def test_columns_dialog():
    model = QStandardItemModel()
    fill(model)
    view = QTableView()
    view.setModel(model)
    views = ListViews("test", view)
    views.set_hidden("期間", True)
    dialog = ColumnsDialog(views)
    rows = lambda: [(dialog.list.item(r).text(), shown) for r, (_i, shown) in enumerate(dialog.chosen())]  # noqa: E731
    assert rows() == [("名称", True), ("区分", True), ("期間", False), ("状態", True)]
    buttons = dialog.move_buttons
    assert dialog.list.currentRow() == 0 and not buttons["up"].isEnabled() and not buttons["first"].isEnabled() and buttons["down"].isEnabled()
    dialog.list.setCurrentRow(3)
    dialog.move("first")
    dialog.list.setCurrentRow(2)
    dialog.move("down")
    assert [t for t, _s in rows()] == ["状態", "名称", "期間", "区分"] and dialog.list.currentRow() == 3
    assert not buttons["down"].isEnabled() and not buttons["last"].isEnabled()
    dialog.move("up")
    assert [t for t, _s in rows()] == ["状態", "名称", "区分", "期間"]
    dialog.list.item(1).setCheckState(Qt.CheckState.Unchecked)
    # Not every column hidden: OK only while one is ticked.
    ok = dialog.buttons.button(QDialogButtonBox.StandardButton.Ok)
    for r in range(4):
        dialog.list.item(r).setCheckState(Qt.CheckState.Unchecked)
    assert not ok.isEnabled()
    for r in (0, 2):
        dialog.list.item(r).setCheckState(Qt.CheckState.Checked)
    assert ok.isEnabled()
    dialog.accept_into()
    assert [(c["title"], c["hidden"]) for c in views.columns()] == [("状態", False), ("名称", True), ("区分", False), ("期間", True)]
    # 「標準に戻す」 in the dialog: every column in the plugin's order (OK puts it so).
    again = ColumnsDialog(views)
    again.standard()
    assert again.chosen() == [(0, True), (1, True), (2, True), (3, True)]
    again.accept_into()
    assert titles(views) == HEAD


def test_a_tree_list_and_its_arrangement_kept():
    def tree():
        t = QTreeWidget()
        t.setHeaderLabels(["フィード", "種別", "状態"])
        QTreeWidgetItem(t, ["a", "AIS", "接続中"])
        return t

    first = tree()
    views = column_menus("tree", first)
    assert views.header is first.header() and views.header.sectionsMovable()
    assert views.move(2, "first") and titles(views) == ["状態", "フィード", "種別"]
    # The dock made again (QGIS started again): the order is back.
    assert titles(column_menus("tree", tree())) == ["状態", "フィード", "種別"]


def test_every_list_of_the_docks_has_the_menu(plugin):  # noqa: F811
    p, _iface, dock, _ = plugin
    d = p.docks
    lists = {
        "imaging-plan": d["SatObsImagingPlan"].table_columns, "imaging-plan-revisit": d["SatObsImagingPlan"].revisit_columns,
        "satellite-passes": d["SatObsSatellitePass"].table_columns, "schedule": d["SatObsSchedule"].table_columns,
        "plan-files": d["SatObsPlanFiles"].table_columns, "events": d["SatObsEvents"].table_columns,
        "ai-results": d["SatObsAiResults"].list_columns, "notices": d["SatObsNotices"].notices_columns,
        "notice-watches": d["SatObsNotices"].watches_columns, "moving-feeds": d["SatObsMoving"].list_columns,
        "observation-steps": dock.steps_columns, "alert-feed": d["SatObsNotices"].alerts.table_columns,
    }
    lists.update({v.key: v for v in d["SatObsEvents"].item_columns.values()})
    for key, views in lists.items():
        assert views.key == key and views.header.sectionsMovable()
        assert views.header.contextMenuPolicy() == Qt.ContextMenuPolicy.CustomContextMenu
    assert len({v.key for v in lists.values()}) == len(lists) >= 15
    # The events list: moved from the menu, kept for the next start.
    events = d["SatObsEvents"].table_columns
    first = titles(events)[0]
    assert events.move(0, "last") and titles(events)[-1] == first
    kept = json.loads(QgsSettings().value(f"{ROOT}/events"))["arrangement"]["columns"]
    assert kept[-1]["title"] == first


def test_each_list_of_the_observations_dock_keeps_its_own(plugin):  # noqa: F811
    p, _iface, dock, _ = plugin
    book = dock.book
    book.add(139.7, 35.3, object="船A", kind="船舶 > 支援船", activity="航行")
    book.add(139.9, 35.5, object="機B", kind="航空機", activity="飛行")
    dock.render()
    settle()
    views = dock.views
    assert views.key == "observations-0"
    first = [c["title"] for c in views.columns()]
    assert views.set_hidden(first[1], True)
    dock.tabs.setCurrentIndex(1)
    settle()
    assert views.key == "observations-1" and not any(c["hidden"] for c in views.columns())
    dock.tabs.setCurrentIndex(0)
    dock.render()
    settle()
    assert [c["hidden"] for c in views.columns()][:2] == [False, True]
    # With the 大分類 shown.
    dock.filter_root("船舶")
    views.save("船だけ")
    dock.filter_root(None)
    views.reset()
    assert views.choose("船だけ") and dock.class_filter == ["船舶"]
    settle()
    assert [c["hidden"] for c in views.columns()][:2] == [False, True]
    # The other docks have theirs.
    assert p.api.image_catalog.views.key == "catalog" and p.api.requests.views.key == "requests"


def test_header_drag_turned_off_by_the_settings_file(plugin):  # noqa: F811
    p, _iface, dock, _ = plugin
    lists = [dock.views, dock.steps_columns, p.docks["SatObsEvents"].table_columns, p.docks["SatObsAiResults"].list_columns,
             p.api.image_catalog.views, p.api.requests.views]
    assert column_drag() and all(v.header.sectionsMovable() for v in lists)
    # `"columnDrag": false`: the lists open now and those made later are not dragged; the menu still moves the columns.
    p.context.config, _ = parse_config({"columnDrag": False})
    p.context.configChanged.emit()
    assert not column_drag() and not any(v.header.sectionsMovable() for v in lists)
    model = QStandardItemModel()
    fill(model)
    view = QTableView()
    view.setModel(model)
    later = column_menus("test", view)
    assert not view.horizontalHeader().sectionsMovable()
    assert later.move(0, "last") and titles(later) == ["区分", "期間", "状態", "名称"]
    # The file without it again: dragged as before.
    p.context.config, _ = parse_config({})
    p.context.configChanged.emit()
    assert all(v.header.sectionsMovable() for v in lists)
