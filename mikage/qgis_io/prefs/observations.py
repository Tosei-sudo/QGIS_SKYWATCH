"""
観測記録の個人設定 (QgsSettings, per user profile: docs/architecture.md, 設定を機能ごとに). Read
through qgis_io/settings.py as before (it lists them); a new one of this feature goes here.
"""
from __future__ import annotations

from typing import Optional

from ..qsettings import pref, set_pref


class SettingsPrefs:
    """The observation book's small values (the images listed without their file, per store) in QgsSettings."""

    def get(self, key: str) -> Optional[str]:
        v = pref("observations/" + key)
        return v or None

    def set(self, key: str, value: str) -> None:
        set_pref("observations/" + key, value)


def point_count_setting() -> int:
    """How many clicks a record takes (1 a point; 2 rear and front; 3 and 4 the sides too); 「2 点」 of before as 2."""
    v = pref("observations/points")
    if v in ("1", "2", "3", "4"):
        return int(v)
    return 2 if pref("observations/two_point") == "1" else 1


def set_point_count(n: int) -> None:
    set_pref("observations/points", str(n))


#: How the observations' marks are drawn: as they were, outlines only (no fill), or hidden (the input being made stays).
MARK_STYLES = ("normal", "outline", "hidden")


def mark_style() -> str:
    """The observations' marks on the map ('normal' unless chosen)."""
    v = pref("observations/marks")
    return v if v in MARK_STYLES else "normal"


def set_mark_style(style: str) -> None:
    set_pref("observations/marks", style if style in MARK_STYLES else "normal")
