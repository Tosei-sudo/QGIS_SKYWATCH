"""
観測記録の個人設定 (QgsSettings, per user profile: docs/architecture.md, 設定を機能ごとに). Read
through qgis_io/settings.py as before (it lists them); a new one of this feature goes here.
"""
from __future__ import annotations

import os
import uuid
from typing import Optional

from ..qsettings import pref, set_pref

#: The values kept only while this QGIS runs: the images listed without their files (their footprints' layers
#: follow the list). tsudo 2026-11-26: 画像なしの画像カタログから観測結果を入れたあと、QGIS を落として、また
#: 立ち上げると、レイヤーが残っている. As the images opened (their layers go when QGIS closes): the next QGIS
#: starts with none listed, their records stay. A new project, another store, the plugin reloaded keep them.
RUN_KEYS = ("listed_images/",)
#: This QGIS's mark, in its environment (a reloaded plugin keeps it): `<process id>-<8 letters>`.
RUN_ENV = "MIKAGE_QGIS_RUN"


def qgis_run() -> str:
    """
    This run of QGIS: the same until it closes (the plugin reloaded too), another at its next start (the 8
    letters: the process id may come again), and in a QGIS started from this one (it has the environment).
    """
    pid = str(os.getpid())
    known = os.environ.get(RUN_ENV, "")
    if known.split("-")[0] == pid:
        return known
    run = f"{pid}-{uuid.uuid4().hex[:8]}"
    os.environ[RUN_ENV] = run
    return run


class SettingsPrefs:
    """
    The observation book's small values in QgsSettings: what the last record said (per store), the records
    waiting to be sent, and the images listed without their files (per store, only while this QGIS runs: RUN_KEYS).
    """

    def get(self, key: str) -> Optional[str]:
        v = pref("observations/" + key)
        if v and key.startswith(RUN_KEYS) and pref("observations/run/" + key) != qgis_run():
            return None  # kept by an earlier run of QGIS
        return v or None

    def set(self, key: str, value: str) -> None:
        set_pref("observations/" + key, value)
        if key.startswith(RUN_KEYS):
            set_pref("observations/run/" + key, qgis_run())


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
