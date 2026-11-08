"""
「最初の設定」 (tsudo, 2026-11-08): the first time the plugin starts in a QGIS
user profile (not after an update), a page asks for the person's role (担当),
which docks a layout of 「ドックのレイアウト」 opens, and the things each person
may set for themselves:

* how the docks open: joined to the main window (as tabs) or each in its own
  window (floating, tsudo's wish);
* the recorder's name, the clicks a record takes, the cursor position's notation.

The same page is in the menu (「担当と個人設定…」) to choose again.

First start or not: a mark in QgsSettings (`firstSetupDone`). A profile that
already holds any of the plugin's settings (it was used before this page came)
counts as set up, so updating never shows it.
"""
from __future__ import annotations

from typing import Callable, Optional

from qgis.core import QgsSettings
from qgis.PyQt.QtCore import QCoreApplication, Qt
from qgis.PyQt.QtWidgets import (
    QButtonGroup, QComboBox, QDialog, QDialogButtonBox, QDockWidget, QFormLayout, QGroupBox, QLabel, QLineEdit, QMainWindow,
    QPushButton, QRadioButton, QVBoxLayout, QWidget,
)

from ..core.coords import NOTATIONS
from ..qgis_io import settings
from .dock_layouts import PRESETS, DockLayoutStore, apply as apply_layout, float_docks  # noqa: F401 (float_docks also from here)

GROUP = "skywatch"
DONE_KEY = GROUP + "/firstSetupDone"
FLOATING_KEY = GROUP + "/docksFloating"
ROLE_KEY = GROUP + "/role"
#: Off in the tests (conftest), where a page popping up in every plugin test would get in the way.
ENABLED = True

POINT_CHOICES = [(1, "1 点（位置だけ）"), (2, "2 点（後端 → 前端で長さ・向き）"), (3, "3 点（＋片側の側面で幅）"), (4, "4 点（＋両側の側面で幅）")]


def tr(text: str) -> str:
    return QCoreApplication.translate("Skywatch", text)


# ---- what is kept --------------------------------------------------------

def is_first_start() -> bool:
    """
    Whether the page is to be shown now: not yet set up in this profile. A
    profile with any of the plugin's settings already (used before) is marked
    set up instead. Called before the plugin writes anything at start.
    """
    s = QgsSettings()
    if s.value(DONE_KEY, "") == "1":
        return False
    s.beginGroup(GROUP)
    used = bool(s.allKeys())
    s.endGroup()
    if used:
        mark_done()
        return False
    return True


def mark_done() -> None:
    QgsSettings().setValue(DONE_KEY, "1")


def docks_floating() -> bool:
    """「ドックを別ウィンドウで開く」: each dock in a window of its own instead of joined to the main window."""
    return QgsSettings().value(FLOATING_KEY, "") == "1"


def set_docks_floating(on: bool) -> None:
    QgsSettings().setValue(FLOATING_KEY, "1" if on else "0")


def role() -> str:
    """The role (a layout's name) last chosen here ('' for none)."""
    value = QgsSettings().value(ROLE_KEY, "")
    return value if isinstance(value, str) else ""


def set_role(name: str) -> None:
    QgsSettings().setValue(ROLE_KEY, name)


# ---- the docks -----------------------------------------------------------

def use_role(window: QMainWindow, docks: dict[str, QDockWidget], name: str, floating: bool) -> bool:
    """
    Opens the docks of the layout `name` (「ドックのレイアウト」); with `floating`,
    each in a window of its own unless the layout is one saved with its own
    floating windows. Without a layout of that name, only `floating` applies.
    """
    store = DockLayoutStore()
    layout = store.all().get(name) if name else None
    if layout is not None:
        apply_layout(window, docks, layout)
        store.last = name
    if floating and not (layout and layout.get("floating")):
        float_docks(window, docks)
    return layout is not None


# ---- the page ------------------------------------------------------------

class FirstSetupDialog(QDialog):
    """
    「最初の設定」 / 「担当と個人設定」: the role, how the docks open, and the
    person's own settings. `docks` gives the plugin's docks (name → dock).
    """

    def __init__(self, window: QMainWindow, docks: Callable[[], dict], first: bool = False,
                 observation_dock: Callable[[], Optional[QWidget]] = lambda: None,
                 coordinates: Callable[[], Optional[object]] = lambda: None,
                 open_settings: Optional[Callable[[], None]] = None, parent: QWidget | None = None):
        super().__init__(parent or window)
        self.setObjectName("SatObsFirstSetup")
        self.window_ = window
        self.docks = docks
        self.first = first
        self.observation_dock = observation_dock
        self.coordinates = coordinates
        self.setWindowTitle(tr("最初の設定") if first else tr("担当と個人設定"))
        self.setMinimumWidth(580)
        layout = QVBoxLayout(self)
        intro = QLabel(tr("SKYWATCH へようこそ。担当と使い方を選んでください。\n"
                          "あとでメニュー「担当と個人設定…」から変えられます。") if first else
                       tr("担当を選ぶと、そのドックのレイアウトで開き直します。"))
        intro.setWordWrap(True)
        layout.addWidget(intro)

        # 担当: a layout of 「ドックのレイアウト」 (the examples and the person's own), or none.
        roles_box = QGroupBox(tr("担当（開くドック）"))
        roles = QVBoxLayout(roles_box)
        self.role_group = QButtonGroup(self)
        self.role_buttons: dict[str, QRadioButton] = {}
        store = DockLayoutStore()
        titles = {n: d.windowTitle() for n, d in docks().items()}
        # First: the last layout used, else the first example; later: the role chosen here last (none too).
        if first or not QgsSettings().contains(ROLE_KEY):
            current = store.last or ("" if not first else PRESETS[0][0])
        else:
            current = role()
        for name, entry in store.all().items():
            button = QRadioButton(name)
            opened = [titles[e["name"]] for g in entry.get("groups", []) for e in g.get("docks", []) if e.get("name") in titles and e.get("visible", True)]
            opened += [titles[e["name"]] for e in entry.get("floating", []) if e.get("name") in titles and e.get("visible", True)]
            button.setToolTip("、".join(opened))
            roles.addWidget(button)
            hint = QLabel("、".join(opened))
            hint.setWordWrap(False)
            hint.setIndent(22)
            hint.setStyleSheet("color: palette(mid);")
            roles.addWidget(hint)
            self.role_group.addButton(button)
            self.role_buttons[name] = button
        keep = QRadioButton(tr("選ばない（ドックは今のまま）") if not first else tr("選ばない（画像カタログと撮像計画だけ開く）"))
        roles.addWidget(keep)
        self.role_group.addButton(keep)
        self.role_buttons[""] = keep
        (self.role_buttons.get(current) or keep).setChecked(True)
        toolbar_hint = QLabel(tr("ツールバーのボタンも担当に合わせて変わります（メニュー「ツールバーのボタン…」で自分用に選べる）。"))
        toolbar_hint.setWordWrap(True)
        roles.addWidget(toolbar_hint)
        layout.addWidget(roles_box)

        # How the docks open.
        docks_box = QGroupBox(tr("ドックの開き方"))
        docks_layout = QVBoxLayout(docks_box)
        self.joined = QRadioButton(tr("メイン画面に連結する（タブでまとめる）"))
        self.floating = QRadioButton(tr("別ウィンドウで開く（ドックごとに独立したウィンドウ。別の画面にも置ける）"))
        group = QButtonGroup(self)
        group.addButton(self.joined)
        group.addButton(self.floating)
        (self.floating if docks_floating() else self.joined).setChecked(True)
        docks_layout.addWidget(self.joined)
        docks_layout.addWidget(self.floating)
        layout.addWidget(docks_box)

        # The person's own settings.
        own_box = QGroupBox(tr("個人の設定"))
        form = QFormLayout(own_box)
        self.recorder = QLineEdit(settings.recorder_setting())
        self.recorder.setPlaceholderText(tr("空欄なら接続の認証設定のユーザー名"))
        form.addRow(tr("記録者名"), self.recorder)
        self.points = QComboBox()
        for n, label in POINT_CHOICES:
            self.points.addItem(tr(label), n)
        self.points.setCurrentIndex(max(0, settings.point_count_setting() - 1))
        form.addRow(tr("観測記録のクリック数"), self.points)
        self.notation = QComboBox()
        for id_, label, _ in NOTATIONS:
            self.notation.addItem(tr(label), id_)
        from .coordinate_box import cursor_notation

        self.notation.setCurrentIndex(max(0, self.notation.findData(cursor_notation())))
        form.addRow(tr("カーソル位置の表記"), self.notation)
        layout.addWidget(own_box)

        if open_settings is not None:
            source = QPushButton(tr("設定ファイルの指定・接続の認証…"))
            source.setToolTip(tr("メニュー「設定…」と同じ画面"))
            source.clicked.connect(lambda: open_settings())
            layout.addWidget(source, 0, Qt.AlignmentFlag.AlignLeft)

        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        buttons.button(QDialogButtonBox.StandardButton.Ok).setText(tr("この設定で始める") if first else tr("適用"))
        buttons.button(QDialogButtonBox.StandardButton.Cancel).setText(tr("あとで") if first else tr("キャンセル"))
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)
        self.finished.connect(lambda _r: mark_done())

    def chosen_role(self) -> str:
        return next((n for n, b in self.role_buttons.items() if b.isChecked()), "")

    def accept(self) -> None:
        self.save()
        super().accept()

    def save(self) -> None:
        """Keeps the choices and applies them: the docks, the recorder, the clicks and the notation."""
        name = self.chosen_role()
        floating = self.floating.isChecked()
        was_floating = docks_floating()
        set_role(name)
        set_docks_floating(floating)
        docks = self.docks()
        if was_floating and not floating:
            # Back from windows: each joined to the main window again (where it was docked last).
            for dock in docks.values():
                dock.setFloating(False)
        if name:
            use_role(self.window_, docks, name, floating)
        elif floating:
            float_docks(self.window_, docks)
        # The recorder and the clicks, also in an open 「観測記録」.
        obs = self.observation_dock()
        recorder = self.recorder.text().strip()
        if obs is not None and hasattr(obs, "set_observer"):
            obs.set_observer(recorder)
        else:
            settings.set_recorder(recorder)
        n = int(self.points.currentData())
        settings.set_point_count(n)
        if obs is not None and hasattr(obs, "use_points"):
            obs.use_points(n)
        notation = self.notation.currentData()
        coordinates = self.coordinates()
        if coordinates is not None and hasattr(coordinates, "set_notation"):
            coordinates.set_notation(notation)
        else:
            from .coordinate_box import set_cursor_notation

            set_cursor_notation(notation)
        mark_done()
