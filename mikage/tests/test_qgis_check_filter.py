"""
The filters of check boxes (ui/check_filter.py; tsudo, 2026-11-16: 状態 and 依頼グループ ticked several at once): the
list stays open while items are ticked, 「すべて」 and the presets, the box's words, what is kept and read back
(also what the single-choice boxes kept), and a 「ビュー」's filters.
"""
import json

import pytest

pytest.importorskip("qgis.core")
from qgis.testing import start_app  # noqa: E402

APP = start_app()

from qgis.core import QgsSettings  # noqa: E402
from qgis.PyQt.QtCore import Qt  # noqa: E402
from qgis.PyQt.QtGui import QStandardItem, QStandardItemModel  # noqa: E402
from qgis.PyQt.QtTest import QTest  # noqa: E402
from qgis.PyQt.QtWidgets import QApplication, QTableView  # noqa: E402

from mikage.qgis_io import settings  # noqa: E402
from mikage.ui.check_filter import CheckFilter, keep_values, kept_values  # noqa: E402
from mikage.ui.list_views import ListViews  # noqa: E402

STATES = [("採用", "採用", "", "#16a34a"), ("不明", "不明"), ("不採用", "不採用", "システムが採用しなかった計画"), ("見送り", "見送り")]


@pytest.fixture(autouse=True)
def clean():
    QgsSettings().remove("mikage")
    yield
    QgsSettings().remove("mikage")


def row_of(box, text):
    model = box.model()
    return next(i for i in range(model.rowCount()) if model.item(i).text() == text)


def click(box, text):
    """A click on an entry of the open list."""
    view = box.view()
    rect = view.visualRect(box.model().index(row_of(box, text), 0))
    QTest.mouseClick(view.viewport(), Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier, rect.center())


def ticked(box):
    model = box.model()
    return [model.item(i).text() for i in range(model.rowCount())
            if model.item(i).isCheckable() and model.item(i).checkState() == Qt.CheckState.Checked]


def test_items_ticked_in_the_open_list():
    box = CheckFilter("すべての状態", STATES, presets=[("計画に入ったもの", ("採用",))], key="test/states")
    told = []
    box.changed.connect(lambda: told.append(box.chosen()))
    box.show()
    assert box.is_all() and box.text() == "すべての状態" and ticked(box) == ["すべての状態"]
    assert all(box.passes(v) for _t, v in box.items())
    box.showPopup()
    QApplication.processEvents()
    assert box.view().isVisible()
    # Two clicks: both ticked, the list still open.
    click(box, "採用")
    click(box, "不採用")
    assert box.view().isVisible()
    assert box.chosen() == ["採用", "不採用"] and told == [["採用"], ["採用", "不採用"]]
    assert ticked(box) == ["採用", "不採用"] and box.text() == "採用・不採用"
    assert box.passes("不採用") and not box.passes("不明")
    assert "表示中: 採用・不採用" in box.toolTip()
    # Kept as a list; a click again takes the tick off.
    assert json.loads(settings._get("test/states")) == ["採用", "不採用"]
    click(box, "採用")
    assert box.chosen() == ["不採用"] and settings._get("test/states") == "不採用"
    # Space ticks the entry the keys are on.
    box.view().setCurrentIndex(box.model().index(row_of(box, "見送り"), 0))
    QTest.keyClick(box.view(), Qt.Key.Key_Space)
    assert box.chosen() == ["不採用", "見送り"] and box.view().isVisible()
    # A preset ticks its items only; its name in the box; a click again: all.
    click(box, "計画に入ったもの")
    assert box.chosen() == ["採用"] and box.text() == "計画に入ったもの" and "計画に入ったもの" in ticked(box)
    click(box, "計画に入ったもの")
    assert box.is_all()
    click(box, "不明")
    # 「すべての状態」: the ticks off.
    click(box, "すべての状態")
    assert box.is_all() and settings._get("test/states") == "" and ticked(box) == ["すべての状態"]
    # The line between the parts does nothing.
    model = box.model()
    line = next(i for i in range(model.rowCount()) if not model.item(i).isCheckable())
    view = box.view()
    QTest.mouseClick(view.viewport(), Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier,
                     view.visualRect(model.index(line, 0)).center())
    assert box.is_all()
    # The release of the press that opened the list ticks nothing.
    box.hidePopup()
    box.showPopup()
    QTest.mouseRelease(view.viewport(), Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier,
                       view.visualRect(model.index(row_of(box, "採用"), 0)).center())
    assert box.is_all()
    box.hidePopup()
    # The colour beside a name, a tip under it.
    swatch = model.item(row_of(box, "採用")).data(Qt.ItemDataRole.DecorationRole).pixmap(12, 12).toImage()
    assert swatch.pixelColor(6, 6).name() == "#16a34a" and model.item(row_of(box, "不明")).data(Qt.ItemDataRole.DecorationRole) is None
    assert model.item(row_of(box, "不採用")).toolTip() == "システムが採用しなかった計画"
    box.deleteLater()


def test_kept_values_and_old_single_choices():
    assert kept_values("test/none") is None
    keep_values("test/one", ["採用"])
    assert settings._get("test/one") == "採用" and kept_values("test/one") == ["採用"]
    keep_values("test/two", ["採用", "不採用"])
    assert kept_values("test/two") == ["採用", "不採用"]
    keep_values("test/all", [])
    assert kept_values("test/all") == []
    # A name that only looks like a list is one value.
    settings._set("test/odd", "[A]班")
    assert kept_values("test/odd") == ["[A]班"]
    # Read back when the box is made; values no longer listed go; the default when nothing was kept.
    keep_values("test/states", ["不明", "古い状態"])
    box = CheckFilter("すべての状態", STATES, key="test/states")
    assert box.chosen() == ["不明"]
    other = CheckFilter("すべての状態", STATES, key="test/fresh", default=["採用", "不明"])
    assert other.chosen() == ["採用", "不明"] and kept_values("test/fresh") is None
    # Values set later (as the groups, once read): the ticks kept wait for them, those still unknown listed at the end.
    keep_values("test/later", ["第2班", "第9班"])
    later = CheckFilter("すべてのグループ", key="test/later")
    assert later.chosen() == ["第2班", "第9班"]
    later.set_items([("第1班", "第1班"), ("第2班", "第2班")], keep_unknown=True)
    assert [v for _t, v in later.items()] == ["第1班", "第2班", "第9班"] and later.chosen() == ["第2班", "第9班"]
    for b in (box, other, later):
        b.deleteLater()


def test_a_view_keeps_the_ticks_and_reads_the_old_choices():
    model = QStandardItemModel()
    model.setHorizontalHeaderLabels(["名称", "状態"])
    model.appendRow([QStandardItem("a"), QStandardItem("採用")])
    table = QTableView()
    table.setModel(model)
    box = CheckFilter("すべての状態", STATES)
    views = ListViews("test-check", table, {"state": box})
    box.choose(["採用", "見送り"])
    assert views.state()["filters"]["state"] == {"checked": ["採用", "見送り"]}
    views.apply({"filters": {"state": {"checked": ["不明"]}}})
    assert box.chosen() == ["不明"]
    # What a single-choice box kept in a view: its data, its text (a name, or 「すべて…」).
    views.apply({"filters": {"state": {"data": "不採用"}}})
    assert box.chosen() == ["不採用"]
    views.apply({"filters": {"state": {"text": "見送り"}}})
    assert box.chosen() == ["見送り"]
    views.apply({"filters": {"state": {"text": "すべての状態"}}})
    assert box.is_all()
    views.apply({"filters": {"state": {"data": None}}})
    assert box.is_all()
    box.deleteLater()
    table.deleteLater()
