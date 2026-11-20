"""
The plugin's screens as a person meets them, written down whole: the menu
(entries, icons, names), the toolbar, every dock (where, title, the widgets
in it with their words), the map's right-click menu, plugin.api's names,
the settings file's keys and the profile's settings keys. The refactoring
of 2026-11-20 (docs/architecture.md) changes none of it: this test compares
with snapshots/screen.json.

`MIKAGE_UPDATE_SNAPSHOT=1` writes the snapshot again (only for a change to
the screens that was asked for).
"""
import ast
import hashlib
import json
import os
import re

import pytest

qgis_core = pytest.importorskip("qgis.core")
from qgis.testing import start_app  # noqa: E402

APP = start_app()

from qgis.core import QgsPointXY, QgsSettings  # noqa: E402
from qgis.PyQt.QtCore import QBuffer, QByteArray, QIODevice  # noqa: E402
from qgis.PyQt.QtWidgets import (QAbstractButton, QComboBox, QGroupBox, QLabel, QLineEdit, QMenu,  # noqa: E402
                                 QTabWidget, QWidget)

from mikage.qgis_io import settings  # noqa: E402
from mikage.tests import architecture  # noqa: E402
from mikage.tests.qgis_fakes import FakeIface  # noqa: E402

HERE = os.path.dirname(__file__)
SNAPSHOT = os.path.join(HERE, "snapshots", "screen.json")


@pytest.fixture(autouse=True)
def clean_settings():
    QgsSettings().remove("mikage")
    yield
    QgsSettings().remove("mikage")


def _words(text: str) -> str:
    """Words as shown, numbers masked (counts, dates and times change from run to run)."""
    return re.sub(r"\d+", "#", text or "")


def _icon(icon) -> str:
    if icon is None or icon.isNull():
        return ""
    data = QByteArray()
    buffer = QBuffer(data)
    buffer.open(QIODevice.OpenModeFlag.WriteOnly)
    icon.pixmap(16, 16).toImage().save(buffer, "PNG")
    return hashlib.sha1(bytes(data)).hexdigest()[:12]


def _action(action, icons=True) -> object:
    if action.isSeparator():
        return "-"
    entry = {"text": _words(action.text()), "name": action.objectName(), "checkable": action.isCheckable()}
    if icons and not action.text().startswith("通知"):  # the bell carries the unread count
        entry["icon"] = _icon(action.icon())
    if action.menu() is not None and action.menu().objectName().startswith("SatObsMenu"):
        entry["entries"] = [_action(a, icons) for a in action.menu().actions()]
    return entry


def _outline(widget, depth=0) -> list:
    """The widgets inside, in order: class, name and words (buttons, labels, group titles, tabs, placeholders)."""
    out = []
    for child in widget.children():
        if not isinstance(child, QWidget) or isinstance(child, QMenu):
            continue
        words = ""
        if isinstance(child, QAbstractButton):
            words = child.text()
        elif isinstance(child, QLabel):
            words = child.text()
        elif isinstance(child, QGroupBox):
            words = child.title()
        elif isinstance(child, QTabWidget):
            words = " | ".join(child.tabText(i) for i in range(child.count()))
        elif isinstance(child, QLineEdit):
            words = child.placeholderText()
        elif isinstance(child, QComboBox):
            words = f"{child.count()} items"
        line = "  " * depth + type(child).__name__
        if child.objectName() and not child.objectName().startswith("qt_"):
            line += f"#{child.objectName()}"
        if words:
            line += f" 「{_words(words)}」"
        if child.isHidden():
            line += " (hidden)"
        out.append(line)
        out += _outline(child, depth + 1)
    return out


def _settings_keys() -> list:
    """The profile's keys the plugin reads and writes: `pref` / `set_pref` (qgis_io/qsettings; `_get` / `_set` in qgis_io/settings) and the modules' *KEY constants."""
    keys = set()
    for m in architecture.modules().values():
        for node in ast.walk(m.tree):
            if isinstance(node, ast.Call) and isinstance(node.func, (ast.Name, ast.Attribute)) and node.args:
                name = node.func.id if isinstance(node.func, ast.Name) else node.func.attr
                if name in ("_get", "_set", "pref", "set_pref") and isinstance(node.args[0], (ast.Constant, ast.JoinedStr)):
                    keys.add(ast.unparse(node.args[0]))
            elif isinstance(node, ast.Assign) and len(node.targets) == 1 and isinstance(node.targets[0], ast.Name):
                target = node.targets[0].id
                if (target.endswith("KEY") or target in ("PREFIX", "ROOT")) and isinstance(node.value, (ast.Constant, ast.JoinedStr)):
                    if isinstance(node.value, ast.JoinedStr) or isinstance(node.value.value, str):
                        keys.add(f"{target}={ast.unparse(node.value)}")
    return sorted(keys)


def screens(plugin, iface) -> dict:
    from mikage.core.config import KEYS
    from mikage.plugin import PluginApi

    window = iface.mainWindow()
    docks = {}
    for dock in iface.docks:
        docks[dock.objectName()] = {
            "title": dock.windowTitle(),
            "area": int(window.dockWidgetArea(dock).value if hasattr(window.dockWidgetArea(dock), "value") else window.dockWidgetArea(dock)),
            "visible": dock.isVisible(),
            "tabs": sorted(d.objectName() for d in window.tabifiedDockWidgets(dock)),
            "toggle": _action(dock.toggleViewAction()),
            "widgets": _outline(dock),
        }

    class Event:
        def mapPoint(self):  # noqa: N802
            return QgsPointXY(139.7, 35.6)

    menu = QMenu()
    plugin._canvas_menu(menu, Event())
    toolbar = [_action(a, icons=False) if not a.isSeparator() else "-" for a in plugin.toolbar.actions()]
    return {
        "menu": [_action(a) for a in iface._menu_actions],
        # Which entries the plugin has (their order is the menu's, above: the features add them in turn).
        "actions": sorted(_words(a.text()) for a in plugin.actions),
        "toolbar": toolbar,
        "toolbar_visible": [_words(a.text()) for a in plugin.toolbar.actions() if a.isVisible() and not a.isSeparator()],
        "dock_order": [d.objectName() for d in iface.docks],
        "docks": docks,
        "map_menu": [a.text() for a in menu.actions()],
        "layer_actions": [a.text() for a, _t in iface.layer_actions],
        "new_layer_menu": [a.objectName() or a.text() for a in iface.new_layer_menu.actions()],
        "api": sorted(n for n in dir(PluginApi) if not n.startswith("_")),
        "config_keys": sorted(KEYS),
        "services": [[s.role, s.label] for s in plugin.context.services()],
        "settings_keys": _settings_keys(),
    }


def test_the_screens_are_as_they_were():
    from mikage import classFactory

    settings.set_config_source(os.path.join(HERE, "data", "viewer-config.jsonc"))
    settings.set_startup_layers_on(False)
    iface = FakeIface()
    plugin = classFactory(iface)
    plugin.initGui()
    try:
        found = screens(plugin, iface)
    finally:
        plugin.unload()
    if os.environ.get("MIKAGE_UPDATE_SNAPSHOT") == "1" or not os.path.exists(SNAPSHOT):
        os.makedirs(os.path.dirname(SNAPSHOT), exist_ok=True)
        with open(SNAPSHOT, "w", encoding="utf-8") as f:
            json.dump(found, f, ensure_ascii=False, indent=1)
            f.write("\n")
    with open(SNAPSHOT, encoding="utf-8") as f:
        expected = json.load(f)
    for key in expected:
        assert found.get(key) == expected[key], f"画面が変わった: {key}"
    assert set(found) == set(expected)
