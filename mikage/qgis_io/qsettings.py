"""
The plugin's keys in QGIS's settings (QgsSettings, per user profile): `<plugin id>/<key>` (`mikage/…`, or
`mikage_training/…` in MIKAGE 練習版: core/edition.py), and its folder in the profile (`profile_folder`).
qgis_io/settings.py (the plugin's own), each feature's qgis_io/prefs module and the screens that keep
something of their own read and write them through here, with the key without the root.
"""
from __future__ import annotations

import os

from qgis.core import QgsApplication, QgsSettings

from ..core import edition


def root() -> str:
    """The group of the plugin's keys: its plugin id."""
    return edition.plugin_id()


def key(name: str) -> str:
    """The whole key of one of the plugin's (`name` without the root)."""
    return f"{root()}/{name}"


def pref(name: str, default: str = "") -> str:
    """The text kept under `<root>/<name>` (`default` when there is none, or it is not text)."""
    value = QgsSettings().value(key(name), default)
    return value if isinstance(value, str) else default


def set_pref(name: str, value: str) -> None:
    QgsSettings().setValue(key(name), value)


def value(name: str, default=None, type=None):
    """Any value kept under `<root>/<name>` (QgsSettings.value, `type` as it takes it)."""
    if type is None:
        return QgsSettings().value(key(name), default)
    return QgsSettings().value(key(name), default, type=type)


def set_value(name: str, value) -> None:
    QgsSettings().setValue(key(name), value)


def remove(name: str) -> None:
    QgsSettings().remove(key(name))


def contains(name: str) -> bool:
    return QgsSettings().contains(key(name))


def any_kept() -> bool:
    """Whether the profile has any of the plugin's keys (used before)."""
    s = QgsSettings()
    s.beginGroup(root())
    found = bool(s.allKeys())
    s.endGroup()
    return found


def sync() -> None:
    QgsSettings().sync()


def profile_folder(*parts: str) -> str:
    """The plugin's folder in the QGIS user profile (`<profile>/<plugin id>`), or a folder or file in it."""
    return os.path.join(QgsApplication.qgisSettingsDirPath(), root(), *parts)
