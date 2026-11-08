"""一覧の「ビュー」: columns shown / hidden / reordered, the sort and the filters kept under a name, per user and per list."""
import pytest

pytest.importorskip("qgis.core")
from qgis.testing import start_app  # noqa: E402

APP = start_app()

from qgis.core import QgsProject, QgsSettings  # noqa: E402
from qgis.PyQt.QtCore import Qt  # noqa: E402
from qgis.PyQt.QtGui import QStandardItem, QStandardItemModel  # noqa: E402
from qgis.PyQt.QtWidgets import QComboBox, QLineEdit, QTableView  # noqa: E402

from skywatch.ui.list_views import ROOT, ListViews  # noqa: E402
from skywatch.tests.test_qgis_alerts import plugin  # noqa: E402,F401

HEAD = ["名称", "区分", "期間", "状態"]


@pytest.fixture(autouse=True)
def clean():
    QgsSettings().remove(ROOT)
    yield
    QgsSettings().remove(ROOT)
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
    assert texts[0] == "A" and "今の表示を名前を付けて保存…" in texts and "標準の表示に戻す" in texts


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
