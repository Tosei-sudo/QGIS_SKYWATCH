"""
エラーコードから対処法へ: the help's search finds a code however typed and shows
its row; the message bar's 「対処法」 and a status line's click open it there.
"""
import pytest

pytest.importorskip("qgis.core")
from qgis.testing import start_app  # noqa: E402

APP = start_app()

from qgis.core import Qgis  # noqa: E402
from qgis.PyQt.QtCore import QEvent, QPoint, QPointF, Qt  # noqa: E402
from qgis.PyQt.QtGui import QMouseEvent  # noqa: E402
from qgis.PyQt.QtWidgets import QApplication, QLabel, QPushButton  # noqa: E402

from mikage.core import errors  # noqa: E402
from mikage.ui import error_help, help_dialog  # noqa: E402
from mikage.ui.docks import plain_label  # noqa: E402
from mikage.tests.test_qgis_alerts import plugin  # noqa: E402,F401


def _selected_row(dialog) -> str:
    return dialog.page.textCursor().block().text().strip()


def test_the_help_search_goes_to_a_code_however_typed():
    dialog = help_dialog.HelpDialog()
    assert "エラーコード" in dialog.search.placeholderText()
    for typed in ("E101", "e-101", "Ｅ１０１", "［E101］"):
        dialog.search.setText(typed)
        # Its own cell first (the row marked whole), its text beside the box.
        assert _selected_row(dialog) == "E101" and dialog.page.textCursor().currentTable() is not None, typed
        assert dialog.hits_label.text().startswith("E101 サーバーに接続できない"), typed
    # The section and its kind stay in the contents, the others hidden.
    shown = [dialog.tree.topLevelItem(i) for i in range(dialog.tree.topLevelItemCount()) if not dialog.tree.topLevelItem(i).isHidden()]
    errors_item = next(i for i in shown if i.data(0, Qt.ItemDataRole.UserRole) == "errors")
    kinds = [errors_item.child(j) for j in range(errors_item.childCount())]
    assert [k.data(0, Qt.ItemDataRole.UserRole) for k in kinds if not k.isHidden()] == ["errors-net"]
    # Marked: the row's cells and the hits.
    assert len(dialog.page.extraSelections()) >= 4 + 1
    # Every code of the list is found at its row.
    for code in errors.CODES:
        dialog.search.setText(code.lower())
        assert _selected_row(dialog) == code, code
    # One not in the list says so; ordinary words search as before.
    dialog.search.setText("E199")
    assert dialog.hits_label.text() == "E199 は一覧にない"
    assert dialog.find("控え") > 0 and dialog.code == ""
    # Going to a code (the message bar's button, a status line) is the same search.
    dialog.go("E304")
    assert dialog.search.text() == "E304" and _selected_row(dialog) == "E304"
    dialog.go("E304")
    assert _selected_row(dialog) == "E304"
    dialog.close()


def test_the_message_bar_and_a_status_line_open_the_help_at_the_code(plugin):
    p, iface, dock, _tmp = plugin
    bar = iface.messageBar()
    bar.pushMessage("観測記録", errors.tagged("記録の取得に失敗: サーバーに接続できない", "E101"), Qgis.MessageLevel.Warning, 0)
    item = bar.currentItem()
    buttons = [b for b in item.findChildren(QPushButton) if b.objectName() == "mikageErrorHelp"]
    assert len(buttons) == 1 and buttons[0].text() == "対処法"
    buttons[0].click()
    dialog = p.help_dialog
    assert dialog is not None and dialog.isVisible() and dialog.search.text() == "E101"
    assert _selected_row(dialog) == "E101"
    # A message without a code (or another plugin's 「[E101]」) has no button.
    bar.pushMessage("観測記録", "保存した", Qgis.MessageLevel.Success, 0)
    bar.pushMessage("他のプラグイン", "failed [E101]", Qgis.MessageLevel.Warning, 0)
    assert not [b for b in bar.currentItem().findChildren(QPushButton) if b.objectName() == "mikageErrorHelp"]
    bar.clearWidgets()

    # A dock's status line: the hand and a click while its text has a code; plain text all the while.
    label = plain_label(QLabel())
    label.setText(errors.tagged("保存に失敗: ほかの人が先に変更", "E301"))
    label.show()
    QApplication.sendEvent(label, QEvent(QEvent.Type.Enter))
    assert label.cursor().shape() == Qt.CursorShape.PointingHandCursor and label.textFormat() == Qt.TextFormat.PlainText
    for kind in (QEvent.Type.MouseButtonPress, QEvent.Type.MouseButtonRelease):
        QApplication.sendEvent(label, QMouseEvent(kind, QPointF(5, 5), QPointF(label.mapToGlobal(QPoint(5, 5))),
                                                  Qt.MouseButton.LeftButton, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier))
    assert dialog.search.text() == "E301" and _selected_row(dialog) == "E301"
    # Without a code the line is as before.
    label.setText("3 件")
    QApplication.sendEvent(label, QEvent(QEvent.Type.Enter))
    assert label.cursor().shape() != Qt.CursorShape.PointingHandCursor
    assert error_help.shown_code("3 件") is None and error_help.shown_code("x［E999］") is None
    label.close()
    dialog.close()


def test_nothing_opens_once_the_plugin_is_gone():
    error_help.set_opener(None)
    assert not error_help.open_code("E101")
