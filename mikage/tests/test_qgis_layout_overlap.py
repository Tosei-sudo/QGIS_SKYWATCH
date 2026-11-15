"""
Nothing drawn over anything else, nothing cut off (tsudo, 2026-11-15:
「表示が重なる不具合が検証しきれていなそう」). Every dock (each tab) at a
usual and a narrow width, the windows that had rows drawn over one another,
and the fixes found by scripts/help_shots/check_layout.py in a real QGIS:
dialogs keep the height their wrapped text needs (ui/fit.py), the 「ビュー」
button has room for its arrow, the 新着画像 page is not under QGIS's message
bar, the dashboard's notes wrap.
"""
import ast
import glob
import os
from types import SimpleNamespace

import pytest

pytest.importorskip("qgis.core")
from qgis.testing import start_app  # noqa: E402

APP = start_app()

from qgis.core import QgsSettings  # noqa: E402
from qgis.gui import QgsMessageBar  # noqa: E402
from qgis.PyQt.QtCore import Qt  # noqa: E402
from qgis.PyQt.QtWidgets import (  # noqa: E402
    QDialog, QDialogButtonBox, QDockWidget, QGridLayout, QHBoxLayout, QLabel, QLineEdit, QPushButton, QStackedWidget, QTabWidget,
    QToolButton, QVBoxLayout, QWidget,
)

from mikage.tests.layout_probe import problems, summary  # noqa: E402
from mikage.tests.qgis_fakes import FakeIface  # noqa: E402
from mikage.ui.fit import VerticalScroll, fit_text  # noqa: E402

UI = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "ui")
LONG = ("設定ファイルにある ArcGIS のサービスに 1 回ずつ問い合わせ、応答・サインイン・属性・編集の可否を確かめます"
        "（書き込みはしません）。結果は一覧に出て、問題のある行は赤で示します。")


@pytest.fixture
def plugin():
    from mikage import classFactory

    QgsSettings().remove("mikage")
    iface = FakeIface()
    plugin = classFactory(iface)
    plugin.initGui()
    APP.processEvents()
    yield plugin
    plugin.unload()
    QgsSettings().remove("mikage")


def settle(rounds=5):
    for _ in range(rounds):
        APP.processEvents()


def alone(window, dock: QDockWidget, width: int) -> None:
    for other in window.findChildren(QDockWidget):
        if other is not dock:
            other.hide()
    area = Qt.DockWidgetArea.BottomDockWidgetArea if dock.objectName() == "SatObsPlaceSchedule" else Qt.DockWidgetArea.RightDockWidgetArea
    dock.setFloating(False)
    window.addDockWidget(area, dock)
    dock.show()
    if area == Qt.DockWidgetArea.RightDockWidgetArea:
        window.resizeDocks([dock], [width], Qt.Orientation.Horizontal)
    settle()


def each_tab(root):
    yield ""
    for tabs in root.findChildren(QTabWidget):
        before = tabs.currentIndex()
        for i in range(tabs.count()):
            if i != before and tabs.isTabEnabled(i):
                tabs.setCurrentIndex(i)
                settle(2)
                if tabs.isVisible():
                    yield f" tab {i}"
        tabs.setCurrentIndex(before)


def test_no_dock_draws_over_itself(plugin):
    window = plugin.iface.window
    window.resize(1400, 900)
    found = {}
    for name, dock in plugin.docks.items():
        for width in (440, 300):
            alone(window, dock, width)
            for tab in each_tab(dock):
                bad = problems(dock)
                if bad:
                    found[f"{name} {width}{tab}"] = summary(bad)
        dock.hide()
    assert found == {}


def test_the_probe_sees_rows_drawn_over_one_another():
    # A window with wrapped text and no fit: Qt keeps it only as high as its rows at their widest, so narrow it
    # is lower than its rows, which then overlap.
    dialog = QDialog()
    layout = QVBoxLayout(dialog)
    label = QLabel(LONG)
    label.setWordWrap(True)
    layout.addWidget(label)
    layout.addWidget(QLineEdit())
    layout.addWidget(QDialogButtonBox(QDialogButtonBox.StandardButton.Ok))
    dialog.show()
    dialog.resize(220, 60)
    settle()
    assert dialog.height() < layout.minimumHeightForWidth(dialog.width())
    assert problems(dialog) != []
    dialog.close()


def test_a_window_with_wrapped_text_keeps_the_height_its_rows_need():
    dialog = QDialog()
    fit_text(dialog)
    layout = QVBoxLayout(dialog)
    label = QLabel(LONG)
    label.setWordWrap(True)
    layout.addWidget(label)
    row = QHBoxLayout()
    row.addWidget(QLineEdit())
    row.addWidget(QPushButton("参照…"))
    layout.addLayout(row)
    layout.addWidget(QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel))
    dialog.resize(600, 80)  # as a window that sets its own size, too low for its text
    dialog.show()
    settle()
    assert problems(dialog) == [], summary(problems(dialog))
    assert dialog.height() >= layout.minimumHeightForWidth(dialog.width())
    # Dragged narrower and lower: the text takes more lines, the window stays tall enough for them.
    dialog.resize(240, 50)
    settle()
    assert dialog.width() >= layout.totalMinimumSize().width()
    assert problems(dialog) == [], summary(problems(dialog))
    # Wider again: it may be lower again (no taller than it was made).
    dialog.resize(700, 50)
    settle()
    assert dialog.minimumHeight() == layout.minimumHeightForWidth(dialog.width())
    dialog.close()


def test_every_dialog_of_the_plugin_keeps_its_rows():
    # Each QDialog of ui/ installs the fit (it does nothing in a dialog without word-wrapped text).
    missing = []
    for path in sorted(glob.glob(os.path.join(UI, "*.py"))):
        with open(path, encoding="utf-8") as f:
            tree = ast.parse(f.read())
        classes = {c.name: c for c in tree.body if isinstance(c, ast.ClassDef)}

        def bases(c):
            return [b.id for b in c.bases if isinstance(b, ast.Name)]

        def is_dialog(c) -> bool:
            return any(b == "QDialog" or b in classes and is_dialog(classes[b]) for b in bases(c))

        def fits(c) -> bool:
            calls = any(isinstance(n, ast.Call) and getattr(n.func, "id", None) == "fit_text" for n in ast.walk(c))
            return calls or any(b in classes and fits(classes[b]) for b in bases(c))

        def own_init(c) -> bool:
            return any(isinstance(n, ast.FunctionDef) and n.name == "__init__" for n in c.body)

        # (a base without __init__ of its own, such as watch_dialog's _AreaDialog, is fitted by its subclasses)
        missing += [f"{os.path.basename(path)}:{c.name}" for c in classes.values() if is_dialog(c) and own_init(c) and not fits(c)]
    assert missing == []


def test_a_form_in_a_vertical_scroll_is_never_cut_at_the_right():
    # 撮像依頼 opened narrower than its rows (the satellite rows widen them after it opens): the right was cut off.
    dialog = QDialog()
    outer = QVBoxLayout(dialog)
    body = QWidget()
    rows = QVBoxLayout(body)
    rows.addWidget(QLineEdit())
    scroll = VerticalScroll()
    scroll.setWidget(body)
    outer.addWidget(scroll)
    dialog.resize(260, 300)
    dialog.show()
    settle()
    wide = QHBoxLayout()
    for text in ("ストリップマップ", "スポットライト", "スキャンSAR", "すべて", "解除"):
        wide.addWidget(QPushButton(text))
    rows.addLayout(wide)
    settle(10)
    assert scroll.viewport().width() >= body.minimumSizeHint().width()
    assert problems(dialog) == [], summary(problems(dialog))
    dialog.close()


def test_the_settings_dialog_shows_each_connections_choice_whole(plugin, monkeypatch):
    from mikage.ui import fit

    monkeypatch.setattr(fit, "SCREEN_SHARE", 10)  # the test's screen (800 × 600) is lower than the dialog
    services = [SimpleNamespace(role="画像カタログ", label="モック画像カタログ", url=f"https://example.com/arcgis/rest/services/S{i}/FeatureServer/0",
                                has_token=False) for i in range(3)]
    plugin.context.services = lambda: services
    from mikage.ui.settings_dialog import SettingsDialog

    dialog = SettingsDialog(plugin.context, plugin.iface.window)
    dialog.show()
    settle()
    select = dialog.services.cellWidget(0, 3)
    assert dialog.services.columnWidth(3) >= select.sizeHint().width()
    assert problems(dialog) == [], summary(problems(dialog))
    dialog.reject()


def test_the_view_button_has_room_for_its_arrow():
    from mikage.ui.list_views import MenuToolButton

    plain, ours = QToolButton(), MenuToolButton()
    for b in (plain, ours):
        b.setText("ビュー")
        b.setPopupMode(QToolButton.ToolButtonPopupMode.InstantPopup)
    assert ours.sizeHint().width() > plain.sizeHint().width()


def test_a_page_beside_the_map_is_not_under_the_message_bar():
    from mikage.ui.central_views import CentralViews

    # As QGIS builds its map area: the message bar in the grid's first row, over the stack of pages.
    window = QWidget()
    grid = QGridLayout(window)
    stack = QStackedWidget()
    canvas = QWidget()
    stack.addWidget(canvas)
    bar = QgsMessageBar(window)
    grid.addWidget(stack, 0, 0, 2, 1)
    grid.addWidget(bar, 0, 0, 1, 1)
    iface = SimpleNamespace(mapCanvas=lambda: canvas, messageBar=lambda: bar)
    views = CentralViews(iface)
    page = QWidget()
    top = QPushButton("すべて既読", page)
    QVBoxLayout(page).addWidget(top)
    views.add_view("notices", "新着画像", top.icon(), page)
    window.resize(900, 600)
    window.show()
    views.open("notices")
    settle()
    assert views.bar_room() == 0 and page.contentsMargins().top() == 0
    bar.pushWarning("テスト", "メッセージ")
    settle(10)
    room = views.bar_room()
    assert room > 0 and page.contentsMargins().top() == room
    below = top.mapToGlobal(top.rect().topLeft()).y()
    assert below >= bar.mapToGlobal(bar.rect().bottomLeft()).y()
    bar.clearWidgets()
    settle(10)
    assert page.contentsMargins().top() == 0
    views.unload()
    window.close()


def test_a_chart_note_wraps_in_a_narrow_dock():
    from mikage.ui.dashboard_dock import BarChart, TrendChart

    for chart in (BarChart(), TrendChart()):
        chart.show()
        chart.resize(400, 10)
        settle()
        chart.set_rows([], "時間の属性なし（レイヤーのプロパティの「時系列」で設定）")
        wide = chart.height()
        chart.resize(150, wide)
        settle()
        assert chart.height() > wide  # more lines instead of the end cut off
        chart.close()
