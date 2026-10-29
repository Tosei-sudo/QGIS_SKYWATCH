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


#: The plugin's folder, where a settings file shipped in the zip sits.
PLUGIN_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
#: The names a shipped settings file may have, first found wins.
BUNDLED_NAMES = ("config.json", "config.jsonc")


def config_source() -> str:
    """The settings file chosen in the settings dialog: a path or an http(s) URL ('' for none)."""
    return _get("config_source")


def bundled_config() -> str:
    """The settings file shipped in the plugin's folder (config.json or config.jsonc); '' when there is none."""
    for name in BUNDLED_NAMES:
        path = os.path.join(PLUGIN_DIR, name)
        if os.path.isfile(path):
            return path
    return ""


def effective_config_source() -> tuple[str, bool]:
    """The settings file to read: the one chosen, else the shipped one. (source, shipped)"""
    chosen = config_source()
    if chosen:
        return chosen, False
    shipped = bundled_config()
    return shipped, bool(shipped)


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


def path_folders() -> dict[str, str]:
    """The folders chosen on this computer for `pathMappings` prefixes (by core.catalog.paths.folder_key)."""
    try:
        value = json.loads(_get("catalog/path_folders", "{}"))
    except ValueError:
        return {}
    return {k: v for k, v in value.items() if isinstance(k, str) and isinstance(v, str) and v} if isinstance(value, dict) else {}


def path_folder(prefix: str) -> Optional[str]:
    from ..core.catalog.paths import folder_key

    return path_folders().get(folder_key(prefix))


def set_path_folder(prefix: str, folder: Optional[str]) -> None:
    from ..core.catalog.paths import folder_key

    folders = path_folders()
    if folder:
        folders[folder_key(prefix)] = folder
    else:
        folders.pop(folder_key(prefix), None)
    _set("catalog/path_folders", json.dumps(folders, ensure_ascii=False))


def clear_path_folders() -> None:
    """Forgets every folder chosen for the prefixes (「フォルダの対応を消去」)."""
    _set("catalog/path_folders", "{}")


def similar_tolerance() -> dict:
    """The tolerances of 「類似画像の検索」 last used (core.catalog.similar.SimilarTolerance JSON)."""
    try:
        value = json.loads(_get("catalog/similar_tolerance", "{}"))
    except ValueError:
        return {}
    return value if isinstance(value, dict) else {}


def set_similar_tolerance(value: dict) -> None:
    _set("catalog/similar_tolerance", json.dumps(value))


def plan_cloud() -> bool:
    """Whether the imaging plan asks for cloud forecasts (the person turned them on)."""
    return _get("plan_cloud") == "1"


def set_plan_cloud(on: bool) -> None:
    _set("plan_cloud", "1" if on else "0")


def startup_layers_on() -> bool:
    """Whether the settings file's `layers` are added to an empty project when QGIS starts (on unless turned off)."""
    return _get("startup_layers", "1") != "0"


def set_startup_layers_on(on: bool) -> None:
    _set("startup_layers", "1" if on else "0")


#: The most trained models remembered for 物体検出（AI）.
TRAINED_MAX = 20


def trained_models() -> list[tuple[str, str]]:
    """Models trained in this profile (newest first), as (label, path), those whose file is still there."""
    try:
        value = json.loads(_get("trained_models", "[]"))
    except ValueError:
        return []
    out = []
    for item in value if isinstance(value, list) else []:
        if isinstance(item, dict) and isinstance(item.get("path"), str) and os.path.isfile(item["path"]):
            out.append((str(item.get("label") or os.path.basename(item["path"])), item["path"]))
    return out


def remember_trained_model(label: str, path: str) -> None:
    """Lists a trained model first among 物体検出（AI）'s models (the same file once)."""
    path = os.path.abspath(path)
    kept = [(l, p) for l, p in trained_models() if os.path.abspath(p) != path]
    items = [{"label": label, "path": path}] + [{"label": l, "path": p} for l, p in kept]
    _set("trained_models", json.dumps(items[:TRAINED_MAX], ensure_ascii=False))
