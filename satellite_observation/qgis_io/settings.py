"""
The plugin's own settings (QgsSettings, per user profile): where the
settings file is, who records, and which QGIS authentication configuration
each service uses. Tokens are never kept here: an authcfg id points into
QGIS's encrypted authentication database.
"""
from __future__ import annotations

import json
import os
from typing import Optional

from qgis.core import QgsApplication, QgsAuthMethodConfig, QgsSettings

PREFIX = "satellite_observation/"


def _get(key: str, default: str = "") -> str:
    value = QgsSettings().value(PREFIX + key, default)
    return value if isinstance(value, str) else default


def _set(key: str, value: str) -> None:
    QgsSettings().setValue(PREFIX + key, value)


def config_source() -> str:
    """The settings file: a path or an http(s) URL ('' for none)."""
    return _get("config_source")


def set_config_source(source: str) -> None:
    _set("config_source", source.strip())


def auth_map() -> dict[str, str]:
    """authcfg ids by service URL."""
    try:
        value = json.loads(_get("auth", "{}"))
    except ValueError:
        return {}
    return {k: v for k, v in value.items() if isinstance(k, str) and isinstance(v, str) and v} if isinstance(value, dict) else {}


def set_auth_map(mapping: dict[str, str]) -> None:
    _set("auth", json.dumps({k: v for k, v in mapping.items() if v}, ensure_ascii=False))


def auth_for(url: str) -> Optional[str]:
    """The authcfg of the service a request URL belongs to: the longest URL in the map that the request starts with."""
    best: Optional[str] = None
    best_len = -1
    for prefix, authcfg in auth_map().items():
        p = prefix.rstrip("/")
        if (url == p or url.startswith(p + "/") or url.startswith(p + "?")) and len(p) > best_len:
            best, best_len = authcfg, len(p)
    return best


def recorder_setting() -> str:
    return _get("recorder")


def set_recorder(name: str) -> None:
    _set("recorder", name.strip())


def auth_user_name(authcfg: Optional[str]) -> str:
    """The user name an authentication configuration holds ('' for none)."""
    if not authcfg:
        return ""
    config = QgsAuthMethodConfig()
    if not QgsApplication.authManager().loadAuthenticationConfig(authcfg, config, True):
        return ""
    for key in ("username", "user"):
        name = config.config(key, "")
        if name:
            return name
    return ""


def recorder_name(store_url: Optional[str] = None) -> str:
    """Who records: the name set in the plugin, else the user name of the store's authentication configuration."""
    return recorder_setting() or auth_user_name(auth_for(store_url) if store_url else None)


def profile_folder() -> str:
    """The plugin's folder in the QGIS user profile (created when missing)."""
    folder = os.path.join(QgsApplication.qgisSettingsDirPath(), "satellite_observation")
    os.makedirs(folder, exist_ok=True)
    return folder


def local_gpkg_path() -> str:
    """The local GeoPackage of observations, checked areas and the classification."""
    return _get("local_gpkg") or os.path.join(profile_folder(), "observations.gpkg")


class SettingsPrefs:
    """The observation book's small values (the images listed without their file, per store) in QgsSettings."""

    def get(self, key: str) -> Optional[str]:
        v = _get("observations/" + key)
        return v or None

    def set(self, key: str, value: str) -> None:
        _set("observations/" + key, value)


def two_point_setting() -> bool:
    return _get("observations/two_point") == "1"


def set_two_point(on: bool) -> None:
    _set("observations/two_point", "1" if on else "0")

def plan_reminders() -> list:
    """The imaging plan's reminders (JSON objects; see core.plan.remind)."""
    try:
        value = json.loads(_get("plan_reminders", "[]"))
    except ValueError:
        return []
    return value if isinstance(value, list) else []


def set_plan_reminders(reminders: list) -> None:
    _set("plan_reminders", json.dumps(reminders, ensure_ascii=False))


def plan_cloud() -> bool:
    """Whether the imaging plan asks for cloud forecasts (the person turned them on)."""
    return _get("plan_cloud") == "1"


def set_plan_cloud(on: bool) -> None:
    _set("plan_cloud", "1" if on else "0")
