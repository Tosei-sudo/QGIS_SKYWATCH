"""
地図の個人設定 (QgsSettings, per user profile: docs/architecture.md, 設定を機能ごとに). Read
through qgis_io/settings.py as before (it lists them); a new one of this feature goes here.
"""
from __future__ import annotations

import json

from ..qsettings import pref, set_pref


def startup_layers_on() -> bool:
    """Whether the settings file's `layers` are added to an empty project when QGIS starts (on unless turned off)."""
    return pref("startup_layers", "1") != "0"


def set_startup_layers_on(on: bool) -> None:
    set_pref("startup_layers", "1" if on else "0")


# ---- アペンド（既存レイヤーへ追加）: the field maps kept under a name, and the one last used per target ----------------

#: The most targets whose last field map is kept.
LAST_MAPS = 50


def _json_pref(key: str) -> dict:
    try:
        value = json.loads(pref(key, "{}"))
    except ValueError:
        return {}
    return value if isinstance(value, dict) else {}


def _set_json_pref(key: str, value: dict) -> None:
    set_pref(key, json.dumps(value, ensure_ascii=False))


def append_maps() -> dict:
    """The field maps kept under a name: name → its JSON (core/append.py FieldMap.to_json)."""
    return {k: v for k, v in _json_pref("append/fieldMaps").items() if isinstance(k, str) and isinstance(v, dict)}


def set_append_map(name: str, value: dict) -> None:
    maps = append_maps()
    maps[name] = value
    _set_json_pref("append/fieldMaps", maps)


def delete_append_map(name: str) -> None:
    maps = append_maps()
    maps.pop(name, None)
    _set_json_pref("append/fieldMaps", maps)


def append_last_map(target_key: str) -> dict:
    """The field map last used for a target (its URL, or 「layer:」 and its name), or {}."""
    value = _json_pref("append/lastMaps").get(target_key)
    return value if isinstance(value, dict) else {}


def set_append_last_map(target_key: str, value: dict) -> None:
    maps = _json_pref("append/lastMaps")
    maps.pop(target_key, None)
    maps[target_key] = value
    # The latest LAST_MAPS (a dict keeps its order).
    _set_json_pref("append/lastMaps", dict(list(maps.items())[-LAST_MAPS:]))
