"""
「最初の設定」 (tsudo, 2026-11-08): the first time the plugin starts in a QGIS
user profile (not after an update), a page asks for the person's roles (担当,
as many as they like: 2026-11-15), which decide the features shown in the
menus, the toolbar and the docks (core/roles.py) and which docks open (the
roles' examples of 「ドックのレイアウト」 together), and the things each person
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
    QButtonGroup, QCheckBox, QComboBox, QDialog, QDialogButtonBox, QDockWidget, QFormLayout, QGridLayout, QGroupBox, QLabel, QLineEdit,
    QMainWindow, QPushButton, QRadioButton, QVBoxLayout, QWidget,
)

from ..core import roles as role_list
from ..core.coords import NOTATIONS
from ..qgis_io import roles_io, settings
from .dock_layouts import DockLayoutStore, apply as apply_layout, float_docks, merged  # noqa: F401 (float_docks also from here)
from .fit import fit_text

GROUP = "mikage"
DONE_KEY = GROUP + "/firstSetupDone"
FLOATING_KEY = GROUP + "/docksFloating"
#: Off in the tests (conftest), where a page popping up in every plugin test would get in the way.
ENABLED = True

POINT_CHOICES = [(1, "1 点（位置だけ）"), (2, "2 点（後端 → 前端で長さ・向き）"), (3, "3 点（＋片側の側面で幅）"), (4, "4 点（＋両側の側面で幅）")]


def tr(text: str) -> str:
    return QCoreApplication.translate("Mikage", text)


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


def roles() -> list[str]:
    """The roles (担当) ticked here last, by name ([] for none)."""
    return role_list.names_of(roles_io.stored_codes())


def set_roles(names: list[str]) -> None:
    """Keeps roles in this profile only (scripts and tests; the page sends them to the table too)."""
    roles_io.store_codes(role_list.codes_of(names))


# ---- the docks -----------------------------------------------------------

def use_roles(window: QMainWindow, docks: dict[str, QDockWidget], names: list[str], floating: bool) -> bool:
    """
    Opens the docks of the roles' layouts (「ドックのレイアウト」 of their names,
    together: dock_layouts.merged); with `floating`, each in a window of its
    own unless the layout is one saved with its own floating windows. Without
    any layout (管理者 only, or none), only `floating` applies.
    """
    store = DockLayoutStore()
    layouts = store.all()
    found = [n for n in names if n in layouts]
    layout = merged([layouts[n] for n in found]) if found else None
    if layout is not None:
        apply_layout(window, docks, layout)
        store.last = found[0] if len(found) == 1 else ""
    if floating and not (layout and layout.get("floating")):
        float_docks(window, docks)
    return layout is not None


def _saved_floating(names: list[str]) -> bool:
    """Whether a layout of the roles is one saved with its own floating windows (kept as saved)."""
    layouts = DockLayoutStore().all()
    return any((layouts.get(n) or {}).get("floating") for n in names)


# ---- the page ------------------------------------------------------------

class FirstSetupDialog(QDialog):
    """
    「最初の設定」 / 「担当と個人設定」: the role, how the docks open, and the
    person's own settings. `docks` gives the plugin's docks (name → dock).
    """

    def __init__(self, window: QMainWindow, docks: Callable[[], dict], first: bool = False,
                 observation_dock: Callable[[], Optional[QWidget]] = lambda: None,
                 coordinates: Callable[[], Optional[object]] = lambda: None,
                 open_settings: Optional[Callable[[], None]] = None, keeper: Optional[roles_io.RoleKeeper] = None,
                 all_docks: Optional[Callable[[], dict]] = None, text_assist: Optional[object] = None,
                 parent: QWidget | None = None):
        super().__init__(parent or window)
        fit_text(self)
        self.setObjectName("SatObsFirstSetup")
        self.window_ = window
        self.docks = docks
        #: Every dock, the ones of features put away too (`docks`: those the roles have; the same by default).
        self.all_docks = all_docks or docks
        self.first = first
        self.observation_dock = observation_dock
        self.coordinates = coordinates
        #: 生成 AI (qgis_io/text_assist_io.TextAssistant), for its switch (opt-in).
        self.text_assist = text_assist
        #: The roles in use and their table (None: kept in this profile only).
        self.keeper = keeper
        self.setWindowTitle(tr("最初の設定") if first else tr("担当と個人設定"))
        layout = QVBoxLayout(self)
        intro = QLabel(tr("MIKAGE へようこそ。担当と使い方を選んでください。\n"
                          "あとでメニュー「担当と個人設定…」から変えられます。") if first else
                       tr("担当を変えると、メニュー・ツールバー・ドックに出る機能が変わります。"))
        # (Its lines broken by hand: see the roles below.)
        layout.addWidget(intro)

        # 担当: as many as the person works in (core/roles.ROLES).
        roles_box = QGroupBox(tr("担当（いくつでも）"))
        box = QVBoxLayout(roles_box)
        # One row a role: the box, and what the role is for beside it. No text here wraps: a dialog
        # (a window of its own) is not made taller for wrapped lines, which then draw over one another.
        grid = QGridLayout()
        grid.setHorizontalSpacing(16)
        grid.setVerticalSpacing(4)
        box.addLayout(grid)
        self.role_checks: dict[str, QCheckBox] = {}
        layouts = DockLayoutStore().all()
        titles = {n: d.windowTitle() for n, d in self.all_docks().items()}
        for role in role_list.ROLES:
            check = QCheckBox(role.name)
            entry = layouts.get(role.name) or {}
            opened = [titles[e["name"]] for g in entry.get("groups", []) for e in g.get("docks", []) if e.get("name") in titles and e.get("visible", True)]
            opened += [titles[e["name"]] for e in entry.get("floating", []) if e.get("name") in titles and e.get("visible", True)]
            check.setToolTip(tr("開くドック: {0}").format("、".join(opened)) if opened else "")
            check.toggled.connect(self._touched)
            hint = QLabel(tr(role.hint))
            hint.setToolTip(check.toolTip())
            row = len(self.role_checks)
            grid.addWidget(check, row, 0)
            grid.addWidget(hint, row, 1)
            self.role_checks[role.name] = check
        self._show_roles(self._current_names())
        self.touched = False
        rule = QLabel(tr("チェックした担当で使う機能だけを、メニュー・ツールバー・ドックのボタンに出します。\n"
                         "どれも選ばないときは、管理者の機能のほかはすべて出します。"))
        box.addSpacing(6)
        box.addWidget(rule)
        self.where = QLabel()
        box.addWidget(self.where)
        self.relayout = QCheckBox(tr("チェックした担当のドックで開き直す（ツールバーのボタンは担当に合わせて変わる）"))
        self.relayout.setChecked(True)
        box.addWidget(self.relayout)
        layout.addWidget(roles_box)
        if keeper is not None:
            # Read from the table while the page is open (another PC's choice): shown unless already changed here.
            keeper.changed.connect(self._roles_read)
        self._show_where()

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
        # 生成 AI (tsudo 2026-11-21: 「生成AIの機能はオプトインにしよう」): off until turned on here.
        self.ai = QCheckBox(tr("使う（文章の手伝い・文で操作。この PC で動く生成 AI）"))
        self.ai.setChecked(text_assist is not None and text_assist.enabled)
        self._ai_offered()
        if text_assist is not None:
            # The settings file read while the page is open (the first start): offered once it has a model.
            text_assist.changed.connect(self._ai_offered)
        form.addRow(tr("生成 AI の機能"), self.ai)
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

    def _ai_offered(self) -> None:
        """「生成 AI の機能」 can be turned on only when the settings give a task a model."""
        configured = self.text_assist is not None and self.text_assist.configured
        self.ai.setEnabled(configured)
        self.ai.setToolTip(tr("使うと、備考を整える・依頼文から下書き・文で操作などのボタンが出る（モデルは使うときに読み込む）")
                           if configured else tr("設定ファイルに生成 AI のモデルの割り当てなし（textAssist）"))

    def _current_names(self) -> list[str]:
        return self.keeper.names if self.keeper is not None else roles()

    def _show_roles(self, names: list[str]) -> None:
        for name, check in self.role_checks.items():
            check.blockSignals(True)
            check.setChecked(name in names)
            check.blockSignals(False)

    def _touched(self, *_args) -> None:
        self.touched = True

    def _roles_read(self) -> None:
        if not self.touched:
            self._show_roles(self._current_names())
        self._show_where()

    def _show_where(self) -> None:
        if self.keeper is not None:
            self.where.setText(tr(self.keeper.where_kept()))
            # Why it could not be sent (a server's message can be long): on the mouse.
            self.where.setToolTip(tr("前回送れなかった理由: {0}").format(self.keeper.failure) if self.keeper.failure else "")
            self.where.setVisible(True)
        else:
            self.where.setVisible(False)

    def chosen_roles(self) -> list[str]:
        """The roles ticked, by name, in the order offered."""
        return [n for n, b in self.role_checks.items() if b.isChecked()]

    def accept(self) -> None:
        self.save()
        super().accept()

    def save(self) -> None:
        """Keeps the choices and applies them: the docks, the recorder, the clicks and the notation."""
        names = self.chosen_roles()
        floating = self.floating.isChecked()
        was_floating = docks_floating()
        # The roles first: the docks they do not use are put away before the others are laid out.
        if self.keeper is not None:
            self.keeper.choose(role_list.codes_of(names))
        else:
            set_roles(names)
        set_docks_floating(floating)
        every = self.all_docks()
        if was_floating and not floating:
            # Back from windows: each joined to the main window again (where it was docked last).
            for dock in every.values():
                dock.setFloating(False)
        laid_out = self.relayout.isChecked() and names and use_roles(self.window_, self.docks(), names, False)
        if floating and not (laid_out and _saved_floating(names)):
            # Every dock (the ones put away too, so they open as windows when the roles have them again).
            float_docks(self.window_, every)
        # The recorder and the clicks, also in an open 「観測記録」.
        obs = self.observation_dock()
        recorder = self.recorder.text().strip()
        if obs is not None and hasattr(obs, "set_observer"):
            obs.set_observer(recorder)
        else:
            settings.set_recorder(recorder)
        if self.text_assist is not None and self.ai.isEnabled():
            self.text_assist.set_enabled(self.ai.isChecked())
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
