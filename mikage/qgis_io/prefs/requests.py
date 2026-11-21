"""
撮像依頼・撮像予定の個人設定 (QgsSettings, per user profile: docs/architecture.md, 設定を機能ごとに). Read
through qgis_io/settings.py as before (it lists them); a new one of this feature goes here.
"""
from __future__ import annotations

from ..qsettings import pref, set_pref


def request_export_folder() -> str:
    """Where the day's request files go for systems whose settings name no folder (the last one chosen)."""
    return pref("request_export_folder")


def set_request_export_folder(folder: str) -> None:
    set_pref("request_export_folder", folder.strip())


def plan_file_filter() -> dict:
    """The 「撮像計画ファイル出力」 table's system and series filters last chosen (each person writes their own systems)."""
    return {"system": pref("plan_files/system"), "series": pref("plan_files/series")}


def set_plan_file_filter(system: str, series: str) -> None:
    set_pref("plan_files/system", system)
    set_pref("plan_files/series", series)


def group_filter(where: str) -> str:
    """The 依頼グループ filter last chosen in a dock (`requests`, `plan_files`, `schedule`, `place_schedule`): a
    group's name, *mine* (自分のグループ), *none* (グループなし), several of them as a JSON list (ticked together,
    2026-11-16; ui/check_filter.py), or "" for all (core/request/groups.py)."""
    return pref(f"request_groups/{where}")


def set_group_filter(where: str, value: str) -> None:
    set_pref(f"request_groups/{where}", value or "")
