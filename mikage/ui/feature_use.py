"""
機能の使われ方 (core/feature_use.py) in QGIS: what is used is counted where
it is chosen, and the counts are kept and sent in the background.

- The plugin's menu entries (the toolbar's buttons are the same actions): each
  entry's trigger is counted a moment later (a zero timer), from the place
  that claimed it meanwhile: the toolbar (its buttons' clicked, by the person
  only), a help link or 文で操作 (`claim` before they trigger it), else the
  plugin's menus when one of them closed just before (a menu closes as its
  entry is chosen, before the entry triggers; QMenu.triggered and the
  toolbar's actionTriggered come for an action triggered from anywhere, so
  they cannot tell). Unclaimed (QGIS's own panel menu, another program's
  call): 「そのほか」. A choice in an entry's popup (背景地図) counts only when
  the popup closed just before: from the menu, else from the toolbar. A
  dock's entry counts when it opens the dock, not when it closes it.
- The plugin's keys (ui/shortcuts.py), the map's right-click entries.
- Inside each dock: its buttons (check boxes, tool buttons and their menus),
  tabs, folded groups opened, lists chosen from (QComboBox.activated: by
  the person only, never its value) and keys, by their label. A dock's
  controls are looked for when it is shown and at each save (new ones made
  meanwhile); a control in a dialog of the dock is not counted.
- How long each dock is shown (in front), and the plugin running.
- 迷い (tsudo, 2026-11-28): how long until an entry was chosen in the plugin's
  menu (or the menu closed with nothing chosen), until a dock opened by the
  person was first used (or closed unused), until a window of the plugin was
  closed with OK (or otherwise); a clock that stops while QGIS is not in
  front (QGuiApplication.applicationStateChanged), in bands (core BANDS).
  The help: the screen in front when it was opened, the sections read (one
  at the top of the page for READ seconds), the searches and the words of
  those that found nothing. The error codes shown (ui/error_help.set_seen).
  No filter on every event of QGIS: the signals above, and focusChanged to
  find the plugin's windows as they open.

On the main thread only `Counter.add`. The file and the table: a hidden task
every SAVE_MINUTES (sending every `sendMinutes`, the first FIRST_SEND seconds
after the start); the last counts written as the plugin unloads.
"""
from __future__ import annotations

import time
from typing import Callable, Iterable, Optional

from qgis.core import Qgis, QgsMessageLog
from qgis.PyQt import sip
from qgis.PyQt.QtCore import QCoreApplication, QObject, Qt, QTimer
from qgis.PyQt.QtWidgets import (
    QAbstractButton, QApplication, QComboBox, QDialog, QDockWidget, QFormLayout, QLabel, QTabBar, QToolButton, QWidget,
)

try:  # Qt 6
    from qgis.PyQt.QtGui import QShortcut
except ImportError:  # Qt 5
    from qgis.PyQt.QtWidgets import QShortcut

from ..core import feature_use as fu
from ..qgis_io import qsettings
from ..qgis_io.tasks import run_task
from . import error_help
from .audit_status import LOG_TAG, plugin_version

#: The profile's id in the table (QgsSettings, made once).
USER_KEY = "featureUseId"
#: A control already listened to.
MARK = "mikageFeatureUse"
#: A popup of an entry opened from a help link: what is chosen in it is not counted again (s).
POPUP_CLAIM = 120
#: An entry triggered within this many seconds after a menu of the plugin closed was chosen in it.
MENU_CLOSED = 1.0
#: Qt's own buttons (the others named Q… are its parts: a dock's close and float, a table's corner, a box's clear…).
QT_BUTTONS = frozenset({"QPushButton", "QToolButton", "QCheckBox", "QRadioButton", "QCommandLinkButton"})
#: The plugin's own package (its windows are those of classes of it).
ROOT = __name__.split(".")[0]
#: Windows of the plugin not timed: the help (reading, not hesitating).
NOT_TIMED = frozenset({"HelpDialog"})
#: A dock used this long ago (s) is still the one in front when the help is opened from the menu.
RECENT = 120


def plugin_window(window) -> bool:
    """A window of the plugin's own (a QDialog of a class of it), not the help."""
    cls = type(window)
    return isinstance(window, QDialog) and cls.__module__.split(".")[0] == ROOT and cls.__name__ not in NOT_TIMED


def qt_part(button) -> bool:
    """A button that is part of a Qt control, not one of the dock's: a dock's title bar's, a tab bar's arrows, a table's corner."""
    name = button.metaObject().className()
    if name.startswith("Q") and not name.startswith("Qgs") and name not in QT_BUTTONS:
        return True
    return isinstance(button.parentWidget(), QTabBar)


def tr(text: str) -> str:
    return QCoreApplication.translate("SatelliteObservation", text)


def default_folder() -> str:
    """Where this profile's counts are kept: the profile's mikage/feature-use."""
    return qsettings.profile_folder("feature-use")


def user_key() -> str:
    """This profile's random id (made the first time)."""
    key = qsettings.value(USER_KEY, "")
    if not isinstance(key, str) or len(key) < 8:
        key = fu.new_user_key()
        qsettings.set_value(USER_KEY, key)
    return key


def plugin_menu(iface, title: str):
    """The plugin's own menu under QGIS's Plugins menu (its submenus' choices come through it too), None when not found."""
    menu = iface.pluginMenu() if hasattr(iface, "pluginMenu") else None
    if menu is None:
        return None
    for action in menu.actions():
        if action.menu() is not None and action.text().replace("&", "") == title.replace("&", ""):
            return action.menu()
    return None


def _collapse_classes() -> tuple:
    try:
        from qgis.gui import QgsCollapsibleGroupBoxBasic, QgsGroupBoxCollapseButton
        return QgsCollapsibleGroupBoxBasic, QgsGroupBoxCollapseButton
    except ImportError:  # pragma: no cover (every QGIS 3 has them)
        return (), ()


def button_label(button) -> str:
    """A button's name as counted: its text (unless only a mark such as ＋ or ▼), else the start of its tooltip, else its accessible name."""
    text = fu.label_of(button.text())
    if text and any(ch.isalnum() for ch in text):
        return text
    for label in (fu.tip_label(button.toolTip()), fu.label_of(button.accessibleName())):
        if label:
            return label
    return text


def combo_label(combo: QComboBox, buddies: dict) -> str:
    """A list's name: its accessible name, the label beside it in a form or whose buddy it is, else its tooltip."""
    if combo.accessibleName():
        return fu.label_of(combo.accessibleName())
    parent = combo.parentWidget()
    layout = parent.layout() if parent is not None else None
    if isinstance(layout, QFormLayout):
        label = layout.labelForField(combo)
        if isinstance(label, QLabel) and label.text():
            return fu.label_of(label.text()).rstrip("：:")
    if combo in buddies:
        return fu.label_of(buddies[combo]).rstrip("：:")
    return fu.tip_label(combo.toolTip())


class FeatureUseKeeper(QObject):
    """The counts of this QGIS run (one per plugin load), with this profile's file and the settings file's table."""

    def __init__(self, context, folder: Optional[str] = None, parent: Optional[QObject] = None):
        super().__init__(parent)
        self.context = context
        self.settings = fu.DEFAULT_FEATURE_USE
        self.counter = fu.Counter(version=plugin_version())
        self.store = fu.UseStore(folder or default_folder())
        self.user_key = user_key()
        #: What happened to the last send (for 「機能の使われ方…」).
        self.last_sent: Optional[float] = None
        self.sent_rows = 0
        self.last_error = ""
        self._task = None
        self._closed = False
        self._run_mark = time.monotonic()
        self._next_send = time.monotonic() + fu.FIRST_SEND
        self._claims: dict = {}
        self._popup_claims: dict[str, float] = {}
        #: When a menu of the plugin, and each entry's popup, last closed (monotonic s).
        self._menu_closed = -1e9
        self._popup_closed: dict[str, float] = {}
        self._entries: dict = {}
        self._dock_actions: set = set()
        self._docks: dict[str, tuple] = {}
        self._shown: dict[str, float] = {}
        #: Every control seen in a dock (dock title, label): listed with 0 when not used.
        self.controls: dict[tuple[str, str], None] = {}
        self._connections: list = []
        self._group_box, self._collapse_button = _collapse_classes()
        # 迷い: the clock of QGIS in front, the menu, the docks opened, the windows of the plugin open.
        self._away_total = 0.0
        self._away_since: Optional[float] = None
        self._menu_session = 0
        self._menu_opened: Optional[float] = None
        self._menu_last = ""
        self._menu_title = ""
        self._dock_names: dict[str, str] = {}
        self._dock_wait: dict[str, float] = {}
        self._dialogs: dict[int, tuple] = {}
        self._last_dock: Optional[tuple[str, float]] = None
        self._connect(context.configChanged, self._config_read, context)
        app = QApplication.instance()
        if app is not None:
            self._connect(app.applicationStateChanged, self._app_state, app)
            self._connect(app.focusChanged, self._focus, app)
        error_help.set_seen(self.error_seen)
        self.timer = QTimer(self)
        self.timer.setInterval(fu.SAVE_MINUTES * 60_000)
        self.timer.timeout.connect(lambda: self.tick())
        self.timer.start()
        self._first = QTimer(self)
        self._first.setSingleShot(True)
        self._first.setInterval(fu.FIRST_SEND * 1000)
        self._first.timeout.connect(lambda: self.tick())
        self._first.start()
        self.counter.add(fu.START_FEATURE, fu.START)
        self._config_read()

    # ---- the settings ----------------------------------------------------------------------------------

    def _connect(self, signal, slot, owner=None) -> None:
        """A connection undone at unload (not when `owner`, the signal's object, is deleted by then)."""
        signal.connect(slot)
        self._connections.append((signal, slot, owner))

    def _config_read(self) -> None:
        c = self.context
        if c is None or getattr(c, "loading", False) or self.counter.paused:
            return
        self.settings = getattr(c.config, "feature_use", fu.DEFAULT_FEATURE_USE)
        if not self.settings.enabled:
            self.counter.take()

    @property
    def enabled(self) -> bool:
        return self.settings.enabled

    def set_roles(self, codes: Iterable[str]) -> None:
        """The roles in use (counted with what is used from now)."""
        self.counter.roles = fu.roles_text(codes)

    def practise(self, on: bool) -> None:
        """練習モード: nothing counted meanwhile (the practice's settings file not applied either)."""
        if on:
            self._mark_minutes()
            self.counter.paused = True
        else:
            self.counter.paused = False
            self._run_mark = time.monotonic()
            for name in self._shown:
                self._shown[name] = time.monotonic()

    # ---- what is used ----------------------------------------------------------------------------------

    def used(self, feature: str, place: str, dock: str = "") -> None:
        if not self.settings.enabled or self._closed:
            return
        self.counter.add(feature, place, dock)
        if dock and place in (fu.DOCK, "KEY"):
            name = self._dock_names.get(dock)
            start = self._dock_wait.pop(name, None) if name else None
            if start is not None:
                self._waited(fu.DOCK_FIRST, dock, start)
            self._last_dock = (dock, time.monotonic())

    # ---- 迷い ------------------------------------------------------------------------------------------

    def _app_state(self, state) -> None:
        now = time.monotonic()
        if state == Qt.ApplicationState.ApplicationActive:
            if self._away_since is not None:
                self._away_total += now - self._away_since
                self._away_since = None
        elif self._away_since is None:
            self._away_since = now

    def clock(self) -> float:
        """Seconds of QGIS in front (stopped while another program is)."""
        now = time.monotonic()
        away = self._away_total + (now - self._away_since if self._away_since is not None else 0.0)
        return now - away

    def _waited(self, kind: str, feature: str, start: float, dock: str = "") -> None:
        if not self.settings.enabled or self._closed or not feature:
            return
        seconds = max(0.0, self.clock() - start)
        place = fu.wait_place(kind, seconds)
        self.counter.add(feature, place, dock, minutes=0.0 if place.endswith(fu.LONG) else seconds / 60)

    def watch_menu(self, root, submenus: Iterable) -> None:
        """The plugin's menu (under QGIS's プラグイン): opened until an entry is chosen, or closed with nothing chosen."""
        if root is None:
            return
        self._menu_title = fu.label_of(root.title())
        self._connect(root.aboutToShow, self._menu_shown, root)
        self._connect(root.aboutToHide, self._menu_hidden, root)
        for menu in submenus:
            if menu is not None and menu is not root:
                self._connect(menu.aboutToShow, lambda menu=menu: self._submenu_shown(menu), menu)

    def _menu_shown(self) -> None:
        self._menu_session += 1
        self._menu_opened = self.clock()
        self._menu_last = ""

    def _submenu_shown(self, menu) -> None:
        try:
            self._menu_last = fu.label_of(menu.title())
        except RuntimeError:
            pass

    def _menu_hidden(self) -> None:
        # The entry chosen triggers just after the menu closes (and is counted a moment later).
        session = self._menu_session
        QTimer.singleShot(int(MENU_CLOSED * 1000), lambda: self._menu_gone(session))

    def _menu_gone(self, session: int) -> None:
        if self._closed or session != self._menu_session or self._menu_opened is None:
            return
        self._waited(fu.MENU_NONE, tr("メニュー: {m}").format(m=self._menu_last or self._menu_title), self._menu_opened)
        self._menu_opened = None

    def dock_opened(self, name: str) -> None:
        """A dock opened by the person (its entry, its key): until a control in it is used, or it is closed."""
        try:
            if name in self._docks and self._docks[name][0].isVisible():
                self._dock_wait[name] = self.clock()
        except RuntimeError:
            pass

    def _focus(self, _old, new) -> None:
        """A window of the plugin's taking the focus: timed from now until it is closed (once while it is open)."""
        if new is None or self._closed or not self.settings.enabled:
            return
        try:
            window = QWidget.window(new)
            if id(window) in self._dialogs or not plugin_window(window):
                return
            if not window.property(MARK):
                window.setProperty(MARK, True)
                window.finished.connect(lambda result, window=window: self._dialog_done(window, result))
            dock, feature = self._dialog_feature(window)
        except RuntimeError:
            return
        self._dialogs[id(window)] = (window, self.clock(), dock, feature)

    def _dialog_done(self, window, result: int) -> None:
        kept = self._dialogs.pop(id(window), None)
        if kept is None or kept[0] is not window:
            return
        _w, start, dock, feature = kept
        accepted = result == QDialog.DialogCode.Accepted or result == 1
        self._waited(fu.DLG_OK if accepted else fu.DLG_CANCEL, feature, start, dock)

    def _dialog_feature(self, window) -> tuple[str, str]:
        """(dock, feature) of a window of the plugin: its menu entry when it has one (「監査ログ…」), else its title in its dock."""
        title = fu.label_of(window.windowTitle())
        texts = set(self._entries.values())
        for text in (f"{title}…", title):
            if text in texts:
                return "", text
        return self.dock_of(window.parentWidget()), title

    def dock_of(self, widget) -> str:
        """The title of the plugin's dock a widget is in, "" when in none."""
        docks = {id(dock): title for dock, title in self._docks.values()}
        try:
            while widget is not None:
                if isinstance(widget, QDockWidget) and id(widget) in docks:
                    return docks[id(widget)]
                widget = widget.parentWidget()
        except RuntimeError:
            pass
        return ""

    def screen_of(self, widget) -> str:
        """The screen a widget is on: a dock's title, a window of the plugin's (its entry), else 「地図・そのほか」."""
        if widget is None:
            return fu.ELSEWHERE
        try:
            window = QWidget.window(widget)
            if plugin_window(window):
                return self._dialog_feature(window)[1]
        except RuntimeError:
            return fu.ELSEWHERE
        return self.dock_of(widget) or fu.ELSEWHERE

    def help_opened(self) -> None:
        """The help opened: from the screen in front (a window of the plugin's, the dock with the focus or last used)."""
        if not self.settings.enabled or self._closed:
            return
        try:
            active = QApplication.activeWindow()
            screen = self.screen_of(active) if plugin_window(active) else self.screen_of(QApplication.focusWidget())
        except RuntimeError:
            screen = fu.ELSEWHERE
        if screen == fu.ELSEWHERE and self._last_dock is not None and time.monotonic() - self._last_dock[1] <= RECENT:
            screen = self._last_dock[0]
        self.counter.add(screen, fu.HELP_FROM)

    def watch_help(self, dialog) -> None:
        """The help window's sections read and searches (its signals)."""
        self._connect(dialog.sectionRead, lambda title: self.used(title, fu.HELP_PAGE), dialog)
        self._connect(dialog.searched, self._searched, dialog)

    def _searched(self, text: str, hits: int) -> None:
        self.used(tr("検索"), fu.HELP_FIND)
        if hits == 0 and fu.search_label(text):
            self.used(fu.search_label(text), fu.HELP_MISS)

    def error_seen(self, code: str, widget) -> None:
        """An error code shown: on a status line (`widget`) or the message bar (None)."""
        if not self.settings.enabled or self._closed:
            return
        self.counter.add(code, fu.ERROR, self.screen_of(widget) if widget is not None else fu.MESSAGE_BAR)

    def claim(self, action, place: str) -> None:
        """The next trigger of `action` (just after) is from `place` (a help link, 文で操作)."""
        self._claims[action] = place

    def popup_claimed(self, text: str) -> None:
        """An entry's popup menu opened from a help link (counted there): what is chosen in it is not counted again."""
        self._popup_claims[text] = time.monotonic() + POPUP_CLAIM

    def watch_entries(self, entries: dict, menus: Iterable, toolbar, dock_actions: Iterable) -> None:
        """
        The plugin's menu entries (text → action), its menus (each claims what is
        chosen in it) and its toolbar; `dock_actions`: the docks' entries (counted when they open the dock).
        """
        self._toolbar = toolbar
        self._dock_actions = set(dock_actions)
        for text, action in entries.items():
            self._entries[action] = text
            menu = action.menu()
            if menu is not None:
                self._connect(menu.triggered, lambda child, text=text: self._defer(text, child, popup=True), menu)
                self._connect(menu.aboutToHide, lambda text=text: self._popup_closed.__setitem__(text, time.monotonic()), menu)
            else:
                self._connect(action.triggered, lambda _on=False, action=action, text=text: self._defer(text, action), action)
        for menu in menus:
            if menu is not None:
                self._connect(menu.aboutToHide, self._a_menu_closed, menu)
        if toolbar is not None:
            self.watch_toolbar(toolbar)

    def _a_menu_closed(self) -> None:
        self._menu_closed = time.monotonic()

    def watch_toolbar(self, toolbar) -> None:
        """The toolbar's buttons (made anew each time it is filled): a click claims its action."""
        for button in toolbar.findChildren(QToolButton):
            if button.property(MARK) or button.defaultAction() is None:
                continue
            button.setProperty(MARK, True)
            button.clicked.connect(lambda _on=False, button=button: self._toolbar_clicked(button))

    def _toolbar_clicked(self, button) -> None:
        try:
            action = button.defaultAction()
        except RuntimeError:
            return
        if action is not None:
            self._claims[action] = "TOOLBAR"
    def _defer(self, text: str, action, popup: bool = False) -> None:
        # After the menu's and the toolbar's own signals (they come after the action's).
        QTimer.singleShot(0, lambda: self._entry_used(text, action, popup))

    def _entry_used(self, text: str, action, popup: bool) -> None:
        if self._closed:
            return
        now = time.monotonic()
        place = self._claims.pop(action, None)
        self._claims.clear()
        from_menu = now - self._menu_closed <= MENU_CLOSED
        chosen_at = self._menu_opened
        if popup:
            if self._popup_claims.pop(text, 0) > now:
                return  # opened from a help link: counted there
            if now - self._popup_closed.get(text, -1e9) > MENU_CLOSED:
                return  # not chosen by the person (a program switched it)
            place = place or ("MENU" if from_menu else "TOOLBAR" if self._on_toolbar(text) else fu.OTHER)
        elif place is None:
            place = "MENU" if from_menu else fu.OTHER
        if place == "MENU" and chosen_at is not None:
            self._menu_opened = None
            self._waited(fu.MENU_PICK, text, chosen_at)
        try:
            if action in self._dock_actions and not action.isChecked():
                return  # a dock closed from its entry
        except RuntimeError:
            return
        self.used(text, place)
        if action in self._dock_actions:
            name = next((n for n, (dock, _t) in self._docks.items() if dock.toggleViewAction() is action), None)
            if name is not None:
                self.dock_opened(name)

    def _on_toolbar(self, text: str) -> bool:
        bar = getattr(self, "_toolbar", None)
        action = next((a for a, t in self._entries.items() if t == text), None)
        try:
            return bar is not None and action is not None and action in bar.actions()
        except RuntimeError:
            return False

    def watch_shortcuts(self, actions: dict, dock_titles: dict, opens: dict) -> None:
        """The plugin's keys (name → action); a key that opens a dock counts as that dock's entry."""
        for name, action in actions.items():
            dock = opens.get(name, "")
            feature = dock_titles.get(dock, "") or fu.label_of(action.text())
            self._connect(action.triggered, lambda _on=False, feature=feature, dock=dock: self._key_used(feature, dock), action)

    def _key_used(self, feature: str, dock: str) -> None:
        self.used(feature, "KEY")
        if dock:
            QTimer.singleShot(0, lambda: None if self._closed else self.dock_opened(dock))

    def watch_docks(self, docks: dict, titles: dict) -> None:
        """The docks (object name → dock) with their titles: shown when and for how long, and their controls."""
        now = time.monotonic()
        for name, dock in docks.items():
            title = titles.get(name) or dock.windowTitle()
            self._docks[name] = (dock, title)
            self._dock_names[title] = name
            self._connect(dock.visibilityChanged, lambda on, name=name: self._visibility(name, on), dock)
            if dock.isVisible() and name not in self._shown:
                self._shown[name] = now
                self.used(title, fu.SHOWN)
            self.walk(name)

    def _visibility(self, name: str, on: bool) -> None:
        if self._closed or name not in self._docks:
            return
        title = self._docks[name][1]
        if on:
            if name not in self._shown:
                self._shown[name] = time.monotonic()
                self.used(title, fu.SHOWN)
            # Its controls made since (a dock fills itself when first shown).
            QTimer.singleShot(0, lambda: None if self._closed else self.walk(name))
        else:
            start = self._shown.pop(name, None)
            if start is not None and self.settings.enabled:
                self.counter.add(title, fu.SHOWN, count=0, minutes=fu.elapsed_minutes(start))
            opened = self._dock_wait.pop(name, None)
            if opened is not None:
                self._waited(fu.DOCK_NONE, title, opened)

    def walk(self, name: str) -> int:
        """Listens to the dock's controls not listened to yet; how many it found."""
        dock, title = self._docks[name]
        try:
            # (QWidget's: a dock may have an attribute of that name.)
            window = QWidget.window(dock)
            buttons = dock.findChildren(QAbstractButton)
        except RuntimeError:  # the dock deleted
            return 0
        found = 0
        for b in buttons:
            if b.property(MARK) or QWidget.window(b) is not window:
                continue
            b.setProperty(MARK, True)
            if self._collapse_button and isinstance(b, self._collapse_button):
                continue  # its group's opening is counted (below)
            if qt_part(b):
                continue  # e.g. the dock's own close and float: its showing is counted
            label = button_label(b)
            if not label:
                continue
            found += 1
            self.controls[(title, label)] = None
            b.clicked.connect(lambda _on=False, b=b, title=title, label=label: self._clicked(title, b, label))
            menu = b.menu() if hasattr(b, "menu") else None
            if menu is not None and not menu.property(MARK):
                menu.setProperty(MARK, True)
                menu.triggered.connect(lambda action, title=title, label=label:
                                       self.used(f"{label} ▸ {fu.label_of(action.text())}", fu.DOCK, title))
        for bar in dock.findChildren(QTabBar):
            if bar.property(MARK) or QWidget.window(bar) is not window:
                continue
            bar.setProperty(MARK, True)
            found += 1
            for i in range(bar.count()):
                self.controls[(title, f"タブ: {fu.label_of(bar.tabText(i))}")] = None
            bar.tabBarClicked.connect(lambda i, bar=bar, title=title:
                                      self.used(f"タブ: {fu.label_of(bar.tabText(i))}", fu.DOCK, title) if i >= 0 else None)
        if self._group_box:
            for box in dock.findChildren(self._group_box):
                if box.property(MARK) or QWidget.window(box) is not window or not fu.label_of(box.title()):
                    continue
                box.setProperty(MARK, True)
                found += 1
                self.controls[(title, f"開く: {fu.label_of(box.title())}")] = None
                box.collapsedStateChanged.connect(lambda collapsed, box=box, title=title: self._group_opened(title, box, collapsed))
        combos = [c for c in dock.findChildren(QComboBox) if not c.property(MARK) and QWidget.window(c) is window]
        if combos:
            buddies = {label.buddy(): label.text() for label in dock.findChildren(QLabel) if label.buddy() is not None}
            for combo in combos:
                combo.setProperty(MARK, True)
                label = combo_label(combo, buddies)
                if not label:
                    continue
                found += 1
                self.controls[(title, f"選択: {label}")] = None
                combo.activated.connect(lambda _i, title=title, label=label: self.used(f"選択: {label}", fu.DOCK, title))
        for shortcut in dock.findChildren(QShortcut):
            if shortcut.property(MARK):
                continue
            shortcut.setProperty(MARK, True)
            label = fu.label_of(shortcut.whatsThis())
            if label:
                found += 1
                shortcut.activated.connect(lambda title=title, label=label: self.used(f"キー: {label}", "KEY", title))
        return found

    def _clicked(self, title: str, button, label: str) -> None:
        try:
            now = button_label(button)
        except RuntimeError:
            now = ""
        self.used(now or label, fu.DOCK, title)

    def _group_opened(self, title: str, box, collapsed: bool) -> None:
        """A folded group opened by the person (not its state put back as it is shown: the mouse is over it, or its button has the key focus)."""
        if collapsed:
            return
        try:
            by_person = box.underMouse() or (box.collapseButton() is not None and box.collapseButton().hasFocus())
        except RuntimeError:
            return
        if by_person:
            self.used(f"開く: {fu.label_of(box.title())}", fu.DOCK, title)

    def known(self, allows: Callable[[str], bool], entries: Iterable[str]) -> list[tuple[str, str]]:
        """The features listed with 0 when not used: the entries `allows` lets through, and their docks' controls."""
        shown = [("", text) for text in entries if allows(text)]
        return shown + [(dock, label) for dock, label in self.controls if allows(dock)]

    # ---- keeping and sending ---------------------------------------------------------------------------

    def _mark_minutes(self) -> None:
        """The minutes of the plugin running and of the docks shown, up to now."""
        if not self.settings.enabled or self.counter.paused:
            return
        now = time.monotonic()
        self.counter.add(fu.START_FEATURE, fu.START, count=0, minutes=fu.elapsed_minutes(self._run_mark, now))
        self._run_mark = now
        for name, start in list(self._shown.items()):
            self.counter.add(self._docks[name][1], fu.SHOWN, count=0, minutes=fu.elapsed_minutes(start, now))
            self._shown[name] = now

    def tick(self, send: bool = False, then: Optional[Callable[[], None]] = None) -> bool:
        """
        What was counted into this PC's file, and sent when it is time
        (`send`: now); False when a save is still running (counted on).
        """
        if self._closed:
            return False
        if not self.settings.enabled:
            self.counter.take()
            return False
        if self._task is not None:
            return False
        self._mark_minutes()
        for name, (dock, _title) in list(self._docks.items()):
            try:
                if dock.isVisible():
                    self.walk(name)
            except RuntimeError:
                pass
        counts = self.counter.take()
        source = self.settings.source
        due = source is not None and (send or time.monotonic() >= self._next_send)
        if not counts and not due:
            if then is not None:
                then()
            return True
        store, user = self.store, self.user_key
        client = self.context.esri() if due else None
        merged = [False]

        def work(task):
            if counts:
                store.merge(counts)
            merged[0] = True
            if not due:
                return None
            return fu.send(client, source, store, user, cancelled=lambda: task.isCanceled())

        def done(added) -> None:
            self._task = None
            if due:
                self._sent(added or 0, "")
            if then is not None and not self._closed:
                then()

        def failed(message: str) -> None:
            self._task = None
            if not merged[0]:
                self._put_back(counts)
            if due:
                self._sent(0, message or tr("送信に失敗"))
            if then is not None and not self._closed:
                then()

        self._task = run_task(tr("機能の使われ方の保存"), work, done, failed, hidden=True)
        return True

    def _put_back(self, counts: dict) -> None:
        self.counter.put_back(counts)

    def _sent(self, added: int, error: str) -> None:
        if self._closed:
            return
        self._next_send = time.monotonic() + self.settings.send_minutes * 60
        if error:
            if error != self.last_error:
                QgsMessageLog.logMessage(tr("機能の使われ方: 送信に失敗（この PC に残し、次に送る）: {e}").format(e=error), LOG_TAG,
                                         Qgis.MessageLevel.Info, notifyUser=False)
            self.last_error = error
            return
        self.last_error = ""
        self.last_sent = time.time()
        self.sent_rows += added

    def snapshot(self) -> list:
        """What is counted in memory now, as rows (not taken: the file gets it at the next save)."""
        return self.counter.rows()

    def unload(self) -> None:
        """The last counts written to this PC's file (not sent: the next start sends them)."""
        if self._closed:
            return
        self.timer.stop()
        self._first.stop()
        if self.settings.enabled:
            self._mark_minutes()
            for name in list(self._shown):
                self._shown.pop(name, None)
            counts = self.counter.take()
            if counts:
                try:
                    self.store.merge(counts)
                except OSError:
                    pass
        self._closed = True
        error_help.set_seen(None)
        self._dialogs.clear()
        for signal, slot, owner in self._connections:
            try:
                if owner is None or not sip.isdeleted(owner):
                    signal.disconnect(slot)
            except (TypeError, RuntimeError):
                pass
        self._connections.clear()
