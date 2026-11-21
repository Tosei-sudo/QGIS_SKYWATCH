"""
地図の個人設定 (QgsSettings, per user profile: docs/architecture.md, 設定を機能ごとに). Read
through qgis_io/settings.py as before (it lists them); a new one of this feature goes here.
"""
from __future__ import annotations

from ..qsettings import pref, set_pref


def startup_layers_on() -> bool:
    """Whether the settings file's `layers` are added to an empty project when QGIS starts (on unless turned off)."""
    return pref("startup_layers", "1") != "0"


def set_startup_layers_on(on: bool) -> None:
    set_pref("startup_layers", "1" if on else "0")
