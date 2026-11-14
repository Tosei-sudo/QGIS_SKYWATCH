"""
Keyboard shortcuts (tsudo, 2026-11-07): every key of the plugin is registered with
QGIS's shortcut manager, so it is listed (names starting 「観測記録」「撮像計画」…) in
Settings ▸ Keyboard Shortcuts and can be changed or cleared there; QGIS keeps the change.

Two kinds:

* the plugin's own actions on the main window (`install`): work wherever the focus is,
  also on the map while points are placed. Their default keys are Ctrl+Alt+… combinations
  QGIS itself (with Processing and the Python console) does not use (`GLOBAL`);
* a dock's keys (`dock_key`): work only while that dock (or the widget) has the focus,
  such as 1–9 in 「観測記録」; letters and digits there would otherwise get in the way of typing elsewhere.
"""
from __future__ import annotations

from typing import Callable, Optional

from qgis.PyQt.QtCore import QCoreApplication, Qt
from qgis.PyQt.QtGui import QKeySequence

try:  # Qt 6
    from qgis.PyQt.QtGui import QAction, QShortcut
except ImportError:  # Qt 5
    from qgis.PyQt.QtWidgets import QAction, QShortcut

#: Every object name (QGIS's settings key for the key chosen) starts with this.
PREFIX = "SatObsKey"

#: The main-window actions: (name, label, default key). Checked against QGIS 3.34's own keys (tests/test_qgis_shortcuts.py).
GLOBAL = [
    ("ShowObservations", "観測記録ドックを開く", "Ctrl+Alt+O"),
    ("ShowCatalog", "画像カタログを開く", "Ctrl+Alt+I"),
    ("ShowImagingPlan", "撮像計画ドックを開く", "Ctrl+Alt+K"),
    ("ShowRequests", "撮像依頼ドックを開く", "Ctrl+Alt+Q"),
    ("ToggleNoticeFeed", "新着画像（大きな画面）と地図の切り替え", "Ctrl+Alt+N"),
    ("NextImage", "観測記録: 次の画像", "Ctrl+Alt+PgDown"),
    ("PreviousImage", "観測記録: 前の画像", "Ctrl+Alt+PgUp"),
    ("ToggleRecording", "観測記録: 記録の開始・終了", "Ctrl+Alt+E"),
    ("ToggleMarks", "観測記録: 地図の印の表示・非表示", "Ctrl+Alt+D"),
]

#: Every shortcut registered (to be taken off QGIS's list when the plugin unloads).
_registered: list = []


def tr(text: str) -> str:
    return QCoreApplication.translate("Mikage", text)


def _manager():
    try:
        from qgis.gui import QgsGui
        return QgsGui.shortcutsManager()
    except Exception:  # noqa: BLE001 (no QGIS GUI: the keys just keep their defaults)
        return None


def dock_key(parent, name: str, label: str, default: str, slot: Callable, widget_only: bool = False) -> QShortcut:
    """
    A key of a dock: active while `parent` (with its children, unless `widget_only`) has the
    focus, listed as 「label」 in QGIS's shortcut settings, where the user may change it.
    """
    shortcut = QShortcut(QKeySequence(default), parent)
    shortcut.setContext(Qt.ShortcutContext.WidgetShortcut if widget_only else Qt.ShortcutContext.WidgetWithChildrenShortcut)
    shortcut.setObjectName(PREFIX + name)
    shortcut.setWhatsThis(tr(label))
    shortcut.activated.connect(slot)
    manager = _manager()
    if manager is not None:
        # Sets the key the user chose, if any.
        manager.registerShortcut(shortcut, default)
        _registered.append(shortcut)
    return shortcut


class Shortcuts:
    """The plugin's main-window actions with keys (`plugin.shortcuts`)."""

    def __init__(self, iface, docks: dict, toggle_feed: Optional[Callable[[], None]] = None):
        self.iface = iface
        self.docks = docks
        self.actions: dict[str, QAction] = {}
        slots = {
            "ShowObservations": lambda: self.show_dock("SatObsObservations"),
            "ShowCatalog": lambda: self.show_dock("SatObsImageCatalog"),
            "ShowImagingPlan": lambda: self.show_dock("SatObsImagingPlan"),
            "ShowRequests": lambda: self.show_dock("SatObsRequests"),
            "ToggleNoticeFeed": lambda: toggle_feed() if toggle_feed else self.show_dock("SatObsNotices"),
            "NextImage": lambda: self._observations("step", 1),
            "PreviousImage": lambda: self._observations("step", -1),
            "ToggleRecording": lambda: self._observations("toggle_recording"),
            "ToggleMarks": self._toggle_marks,
        }
        window = iface.mainWindow()
        for name, label, default in GLOBAL:
            action = QAction(tr(label), window)
            action.setObjectName(PREFIX + name)
            action.setShortcut(QKeySequence(default))
            action.setShortcutContext(Qt.ShortcutContext.WindowShortcut)
            action.triggered.connect(lambda _on=False, run=slots[name]: run())
            # The main window holds it, so its key works without a menu entry.
            window.addAction(action)
            manager = _manager()
            if manager is not None:
                # As iface.registerMainWindowAction does; sets the key the user chose, if any.
                manager.registerAction(action, default)
            self.actions[name] = action

    def show_dock(self, name: str) -> None:
        """Shows the dock in front and gives it the focus."""
        dock = self.docks.get(name)
        if dock is None:
            return
        dock.show()
        dock.raise_()
        dock.activateWindow()
        widget = dock.widget()
        (widget or dock).setFocus(Qt.FocusReason.ShortcutFocusReason)

    def _observations(self, method: str, *args) -> None:
        dock = self.docks.get("SatObsObservations")
        if dock is None:
            return
        if not dock.isVisible():
            self.show_dock("SatObsObservations")
        getattr(dock, method)(*args)

    def _toggle_marks(self) -> None:
        """The marks hidden or shown again; the dock not open: opened with its marks shown."""
        dock = self.docks.get("SatObsObservations")
        if dock is None:
            return
        if dock.isVisible():
            dock.toggle_marks()
            return
        self.show_dock("SatObsObservations")
        if dock.mark_style_select.currentData() == "hidden":
            dock.toggle_marks()

    def unload(self) -> None:
        window = self.iface.mainWindow()
        manager = _manager()
        for action in self.actions.values():
            if manager is not None:
                try:
                    manager.unregisterAction(action)
                except (RuntimeError, TypeError):
                    pass
            window.removeAction(action)
            action.deleteLater()
        self.actions.clear()
        unload_dock_keys()


def unload_dock_keys() -> None:
    """Takes the docks' keys off QGIS's list (the docks go with the plugin)."""
    manager = _manager()
    for shortcut in _registered:
        if manager is not None:
            try:
                manager.unregisterShortcut(shortcut)
            except (RuntimeError, TypeError):
                pass
    _registered.clear()
