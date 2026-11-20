"""
The plugin's keys in QGIS's settings (QgsSettings, per user profile): `mikage/<key>`, kept as text.
qgis_io/settings.py (the plugin's own) and each feature's qgis_io/prefs module read and write them
through here.
"""
from __future__ import annotations

from qgis.core import QgsSettings

PREFIX = "mikage/"


def pref(key: str, default: str = "") -> str:
    """The text kept under `mikage/<key>` (`default` when there is none, or it is not text)."""
    value = QgsSettings().value(PREFIX + key, default)
    return value if isinstance(value, str) else default


def set_pref(key: str, value: str) -> None:
    QgsSettings().setValue(PREFIX + key, value)
