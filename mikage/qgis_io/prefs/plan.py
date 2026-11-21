"""
撮像計画の個人設定 (QgsSettings, per user profile: docs/architecture.md, 設定を機能ごとに). Read
through qgis_io/settings.py as before (it lists them); a new one of this feature goes here.
"""
from __future__ import annotations

import json

from ..qsettings import pref, set_pref


def plan_reminders() -> list:
    """The imaging plan's reminders (JSON objects; see core.plan.remind)."""
    try:
        value = json.loads(pref("plan_reminders", "[]"))
    except ValueError:
        return []
    return value if isinstance(value, list) else []


def set_plan_reminders(reminders: list) -> None:
    set_pref("plan_reminders", json.dumps(reminders, ensure_ascii=False))


def plan_cloud() -> bool:
    """Whether the imaging plan asks for cloud forecasts (the person turned them on)."""
    return pref("plan_cloud") == "1"


def set_plan_cloud(on: bool) -> None:
    set_pref("plan_cloud", "1" if on else "0")
