"""
The plugin's own settings (QgsSettings, per user profile): where the
settings file is, who records, and which QGIS authentication configuration
each service uses. Tokens are never kept here: an authcfg id points into
QGIS's encrypted authentication database.
"""
from __future__ import annotations

import json
import re
import os
from typing import Optional

from qgis.core import QgsApplication, QgsAuthMethodConfig, QgsSettings

PREFIX = "mikage/"


def _get(key: str, default: str = "") -> str:
    value = QgsSettings().value(PREFIX + key, default)
    return value if isinstance(value, str) else default


def _set(key: str, value: str) -> None:
    QgsSettings().setValue(PREFIX + key, value)


#: The plugin's folder, where a settings file shipped in the zip sits.
PLUGIN_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
#: The names a shipped settings file may have, first found wins.
BUNDLED_NAMES = ("config.json", "config.jsonc")
#: The marker beside the shipped settings file that locks the plugin to it (scripts/build_zip.py --lock-config).
LOCK_NAME = "config.lock"


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


def config_locked() -> bool:
    """Whether the plugin reads only the shipped settings file: a config.lock beside it (no effect without one)."""
    return bool(bundled_config()) and os.path.isfile(os.path.join(PLUGIN_DIR, LOCK_NAME))


def effective_config_source() -> tuple[str, bool]:
    """The settings file to read: the one chosen, else the shipped one; always the shipped one when locked. (source, shipped)"""
    if config_locked():
        return bundled_config(), True
    chosen = config_source()
    if chosen:
        return chosen, False
    shipped = bundled_config()
    return shipped, bool(shipped)


def set_config_source(source: str) -> None:
    _set("config_source", source.strip())


#: A URL's query parameters that carry a secret (`?token=…` of a moving feed, an API key).
_SECRET_PARAM = re.compile(r"([?&])(token|access_token|apikey|api_key|key|password|secret)=[^&#]*&?", re.IGNORECASE)


def _without_tokens(value):
    """
    A settings file's value with every `token` left out, and the secret
    parameters of its URLs too (tokens are never kept in the profile).
    """
    if isinstance(value, dict):
        return {k: _without_tokens(v) for k, v in value.items() if k != "token"}
    if isinstance(value, list):
        return [_without_tokens(v) for v in value]
    if isinstance(value, str) and "=" in value and re.match(r"^[a-z][a-z0-9+.-]*://", value, re.IGNORECASE):
        return re.sub(r"[?&]$", "", _SECRET_PARAM.sub(lambda m: m.group(1), value))
    return value


def cached_config(source: str) -> Optional[tuple[object, str]]:
    """The settings file last read from `source` (a URL), without its tokens, and when: None when there is none (#85)."""
    try:
        value = json.loads(_get("config_cache", "{}"))
    except ValueError:
        return None
    if not isinstance(value, dict) or value.get("source") != source or "config" not in value:
        return None
    return value["config"], str(value.get("when") or "")


def set_cached_config(source: str, config: object, when: str) -> None:
    """Keeps the settings file read from `source` (tokens left out) for when it cannot be read next time."""
    _set("config_cache", json.dumps({"source": source, "when": when, "config": _without_tokens(config)}, ensure_ascii=False))


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
    folder = os.path.join(QgsApplication.qgisSettingsDirPath(), "mikage")
    os.makedirs(folder, exist_ok=True)
    return folder


def overview_cache_setting() -> str:
    """The settings file's `overviews.cacheFolder` as last read ("" for the profile's folder), used when QGIS starts."""
    return _get("overview_cache_folder")


def set_overview_cache_setting(folder: str) -> None:
    _set("overview_cache_folder", folder)


def overview_cache_to_clear() -> str:
    """The overview cache folder to clear when QGIS starts next ("" for none)."""
    return _get("overview_cache_clear")


def set_overview_cache_to_clear(folder: str) -> None:
    _set("overview_cache_clear", folder)


def local_gpkg_path() -> str:
    """The local GeoPackage of observations, checked areas and the classification."""
    return _get("local_gpkg") or os.path.join(profile_folder(), "observations.gpkg")


def offline_path() -> str:
    """The file of offline work (the cache of reads and the changes waiting to be sent): never the local GeoPackage."""
    return _get("offline_file") or os.path.join(profile_folder(), "offline_work.sqlite")


def place_cache_path() -> str:
    """The file of the named areas and countries looked up (地名・国の控え, core/obs/place_cache.py)."""
    return os.path.join(profile_folder(), "place_cache.sqlite")


class SettingsPrefs:
    """The observation book's small values (the images listed without their file, per store) in QgsSettings."""

    def get(self, key: str) -> Optional[str]:
        v = _get("observations/" + key)
        return v or None

    def set(self, key: str, value: str) -> None:
        _set("observations/" + key, value)


def point_count_setting() -> int:
    """How many clicks a record takes (1 a point; 2 rear and front; 3 and 4 the sides too); 「2 点」 of before as 2."""
    v = _get("observations/points")
    if v in ("1", "2", "3", "4"):
        return int(v)
    return 2 if _get("observations/two_point") == "1" else 1


def set_point_count(n: int) -> None:
    _set("observations/points", str(n))


#: How the image catalog draws the listed images' footprints: filled, outlines only, or not at all.
FOOTPRINT_STYLES = ("fill", "outline", "hidden")
#: How the observations' marks are drawn: as they were, outlines only (no fill), or hidden (the input being made stays).
MARK_STYLES = ("normal", "outline", "hidden")


def footprint_style() -> str:
    """The catalog's footprints on the map (tsudo, 2026-11-09: filled ones hid the images); outlines unless chosen."""
    v = _get("catalog/footprints")
    return v if v in FOOTPRINT_STYLES else "outline"


def set_footprint_style(style: str) -> None:
    _set("catalog/footprints", style if style in FOOTPRINT_STYLES else "outline")


def close_previous() -> bool:
    """
    「前の画像を閉じる」 (tsudo, 2026-11-10: looking through images one after
    another): opening images from the catalog closes those it opened before.
    Off unless turned on.
    """
    return _get("catalog/closePrevious") == "1"


def set_close_previous(on: bool) -> None:
    _set("catalog/closePrevious", "1" if on else "0")


def zoom_to_opened() -> bool:
    """
    「開いた画像へ移動」 (tsudo, 2026-11-10): opening images from the catalog
    moves the map to them (all of them when several). On unless turned off.
    """
    return _get("catalog/zoomToOpened") != "0"


def set_zoom_to_opened(on: bool) -> None:
    _set("catalog/zoomToOpened", "1" if on else "0")


def mark_style() -> str:
    """The observations' marks on the map ('normal' unless chosen)."""
    v = _get("observations/marks")
    return v if v in MARK_STYLES else "normal"


def set_mark_style(style: str) -> None:
    _set("observations/marks", style if style in MARK_STYLES else "normal")


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


def request_export_folder() -> str:
    """Where the day's request files go for systems whose settings name no folder (the last one chosen)."""
    return _get("request_export_folder")


def set_request_export_folder(folder: str) -> None:
    _set("request_export_folder", folder.strip())


def plan_file_filter() -> dict:
    """The 「撮像計画ファイル出力」 table's system and series filters last chosen (each person writes their own systems)."""
    return {"system": _get("plan_files/system"), "series": _get("plan_files/series")}


def set_plan_file_filter(system: str, series: str) -> None:
    _set("plan_files/system", system)
    _set("plan_files/series", series)


def group_filter(where: str) -> str:
    """The 依頼グループ filter last chosen in a dock (`requests`, `plan_files`, `schedule`): a group's name,
    *mine* (自分のグループ), *none* (グループなし), or "" for all (core/request/groups.py)."""
    return _get(f"request_groups/{where}")


def set_group_filter(where: str, value: str) -> None:
    _set(f"request_groups/{where}", value or "")
