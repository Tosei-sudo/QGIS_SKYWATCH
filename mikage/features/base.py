"""
A feature's 登録票 (docs/architecture.md): what it hands the plugin. Its docks,
its entries of the plugin's menu, the map's right-click entries, the reads
ahead, the commands the plugin answers to (`plugin.open_detect()`…) and what
to do when the plugin starts, is wired, and unloads.

A feature is a `Feature` subclass in mikage/features/<name>.py, listed in the
plugin's FEATURES. The plugin calls, for every feature in that order:

1. `start()`: before the docks (objects that listen from the start);
2. `docks()`: its docks, made in the plugin's DOCK_ORDER;
3. `connect()`: once every dock is made (wiring between docks);
4. `add_actions()`: its menu entries (`self.entry(…)`); where each sits is
   the plugin's MENU_GROUPS / MENU_TOP, by its text;
5. `map_menu()`, `preload()`, `qgis_started()`, `roles_changed()`, `after_offline()` when asked;
6. `unload()`: closes what it made (before the menus and docks are removed),
   then `unload_after_docks()`.

`COMMANDS` names its methods the plugin answers to: `plugin.open_quotas()`
calls the requests feature's. Where each dock, entry and right-click entry
sits stays in the plugin's tables, so the screens are the plugin's to lay out.

`self.host` is the plugin (mikage/plugin.py MikagePlugin): its iface, context,
docks, allows() and the objects features made that others use (`host.central`…).
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any, Callable, Optional

from qgis.PyQt.QtCore import QCoreApplication
from qgis.PyQt.QtGui import QIcon

try:  # Qt6
    from qgis.PyQt.QtGui import QAction
except ImportError:  # Qt5
    from qgis.PyQt.QtWidgets import QAction

PLUGIN_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def tr(text: str) -> str:
    return QCoreApplication.translate("Mikage", text)


def icon(name: str) -> QIcon:
    """A feature's own icon (resources/<name>.svg; the plugin's icon.svg stays for the plugin manager)."""
    return QIcon(os.path.join(PLUGIN_DIR, "resources", f"{name}.svg"))


def close(widget) -> None:
    """A dialog or tool a feature made, closed and deleted (None: nothing)."""
    if widget is None:
        return
    widget.close()
    widget.deleteLater()


def disconnect(signal, slot) -> None:
    """A connection undone (gone already, or the object deleted: nothing)."""
    try:
        signal.disconnect(slot)
    except (TypeError, RuntimeError, AttributeError):
        pass


@dataclass(frozen=True)
class DockSpec:
    """A dock of a feature: its title (also its menu entry's text and its 担当 feature), icon and how it is made."""
    title: str
    icon: str
    #: Makes the dock; the docks before it in the plugin's DOCK_ORDER are made already.
    create: Callable[[], Any]


class Feature:
    """A feature of the plugin: every hook does nothing until a feature has it."""

    #: The names of its methods the plugin answers to (`plugin.<name>(…)`).
    COMMANDS: tuple = ()

    def __init__(self, host):
        self.host = host

    @property
    def iface(self):
        return self.host.iface

    @property
    def context(self):
        return self.host.context

    @property
    def docks(self) -> dict:
        return self.host.docks

    def window(self):
        return self.host.iface.mainWindow()

    def entry(self, icon_name: str, text: str, triggered=None, name: Optional[str] = None) -> QAction:
        """
        A menu entry: made, put in the plugin's menu (placed by its text) and in its list of actions.
        `triggered` is a function, or the name of a command of the plugin (`plugin.<name>()`, looked up when chosen).
        """
        action = QAction(icon(icon_name), tr(text), self.window())
        if name:
            action.setObjectName(name)
        if isinstance(triggered, str):
            command = triggered
            action.triggered.connect(lambda _on=False: getattr(self.host, command)())
        elif triggered is not None:
            action.triggered.connect(lambda _on=False: triggered())
        self.host.add_entry(action)
        return action

    # ---- the hooks ---------------------------------------------------------------------------------------

    def start(self) -> None:
        """Before the docks are made."""

    def dock_specs(self) -> dict:
        """{object name: DockSpec}: its docks (made in the plugin's DOCK_ORDER)."""
        return {}

    def connect(self) -> None:
        """Every dock is made: the wiring between docks."""

    def add_actions(self) -> None:
        """Its entries of the plugin's menu (`self.entry`)."""

    def map_menu(self) -> dict:
        """{text: function(x, y)}: the map's right-click entries, at a point in WGS 84 (placed by the plugin's MAP_MENU)."""
        return {}

    def preload(self) -> list:
        """[(key, label, start(then, failed) -> bool, always)]: its reads ahead (placed by the plugin's PRELOAD_ORDER)."""
        return []

    def qgis_started(self) -> None:
        """QGIS has started (its window shown, the other plugins loaded)."""

    def roles_changed(self) -> None:
        """The roles in use (担当) changed: what it opened of a feature now put away closes."""

    def after_offline(self, sent: bool) -> None:
        """
        Work kept offline was sent or thrown away (`sent`), or a server is back
        with nothing sent (not `sent`): what it shows is read again.
        """

    def unload(self) -> None:
        """Closes what it made (the plugin removes the menus and docks afterwards)."""

    def unload_after_docks(self) -> None:
        """Closes what its docks used, once the plugin has removed them."""
