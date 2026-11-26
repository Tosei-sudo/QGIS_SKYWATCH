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

from qgis.core import QgsApplication, QgsAuthMethodConfig

from ..core import edit_stamp, edition
from . import training_io
from . import qsettings
from .qsettings import pref, set_pref  # noqa: F401 (pref and set_pref: read from here too)

# Each feature's own (qgis_io/prefs): read through here as before.
from .prefs.images import (  # noqa: F401
    overview_cache_setting, set_overview_cache_setting, overview_temp_setting, set_overview_temp_setting, overview_cache_to_clear,
    set_overview_cache_to_clear, central_views,
    set_central_views, feed_close_previous, set_feed_close_previous, feed_unread_only, set_feed_unread_only, FOOTPRINT_STYLES,
    footprint_style, set_footprint_style, close_previous, set_close_previous, zoom_to_opened, set_zoom_to_opened, match_crs,
    set_match_crs, map_orientation, set_map_orientation, saved_areas,
    set_saved_areas, path_folders, path_folder, set_path_folder, clear_path_folders, similar_tolerance, set_similar_tolerance,
)
from .prefs.observations import (  # noqa: F401
    SettingsPrefs, point_count_setting, set_point_count, MARK_STYLES, mark_style, set_mark_style,
)
from .prefs.plan import plan_reminders, set_plan_reminders, plan_cloud, set_plan_cloud  # noqa: F401
from .prefs.maps import startup_layers_on, set_startup_layers_on  # noqa: F401
from .prefs.ai import TRAINED_MAX, trained_models, remember_trained_model, text_assist_on, set_text_assist_on  # noqa: F401
from .prefs.requests import (  # noqa: F401
    request_export_folder, set_request_export_folder, plan_file_filter, set_plan_file_filter, group_filter, set_group_filter,
    area_follow_stamp, set_area_follow_stamp,
)

_get, _set = pref, set_pref


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


def production_config_source() -> tuple[str, bool]:
    """
    The settings file of the production (練習モード or not): the one chosen, else the shipped one; the shipped one
    when locked. MIKAGE 練習版 (core/edition.py): only the shipped one, read for what 練習モード takes of it.
    """
    if config_locked():
        return bundled_config(), True
    if edition.training_only():
        shipped = bundled_config()
        return shipped, bool(shipped)
    chosen = config_source()
    if chosen:
        return chosen, False
    shipped = bundled_config()
    return shipped, bool(shipped)


def effective_config_source() -> tuple[str, bool]:
    """
    The settings file to read: 練習モード's while practising (qgis_io/training_io.py), else the production's
    (`production_config_source`); MIKAGE 練習版 none until 練習モード is on (the screens never have the
    production's). (source, shipped)
    """
    session = training_io.active()
    if session is not None:
        return session.config_source, False
    if edition.training_only():
        return "", False
    return production_config_source()


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
    """The 「記録者」 name typed ('' when none is: the user name is used)."""
    return _get("recorder")


def default_recorder() -> str:
    """
    The 「記録者」 when none is typed (tsudo 2026-11-25: 記録者のデフォルト値を
    USERNAME に): the user name the edits are stamped with (editTracking's
    `user`, else its environment variable, USERNAME by default), so the
    records' 記録者 is the name of 作成者・更新者, 分担 and 「自分の記録」.
    """
    return edit_stamp.user_name()


def recorder_shown() -> str:
    """What a 「記録者」 box shows: the name typed, else the user name."""
    return recorder_setting() or default_recorder()


def set_recorder(name: str) -> None:
    """
    Keeps the name typed; the user name itself (or nothing) is kept as none
    typed, so the 記録者 follows the user name (and the settings file's
    editTracking) until another name is typed.
    """
    name = name.strip()
    _set("recorder", "" if name == default_recorder().strip() else name)


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
    """
    Who records: the name typed in the plugin, else the user name
    (default_recorder), else (no user name known) the user name of the
    store's authentication configuration. The Esri sign-in comes last: on
    some networks everyone signs in with one shared account.
    """
    return recorder_shown() or auth_user_name(auth_for(store_url) if store_url else None)


def profile_folder() -> str:
    """The plugin's folder in the QGIS user profile (created when missing)."""
    folder = qsettings.profile_folder()
    os.makedirs(folder, exist_ok=True)
    return folder


def _practice():
    """練習モード's files while practising (each local file below is the practice's own then), else None."""
    session = training_io.active()
    return session.files if session is not None else None


def local_gpkg_path() -> str:
    """The local GeoPackage of observations, checked areas and the classification."""
    practice = _practice()
    if practice is not None:
        return practice.gpkg
    return _get("local_gpkg") or os.path.join(profile_folder(), "observations.gpkg")


def offline_path() -> str:
    """The file of offline work (the cache of reads and the changes waiting to be sent): never the local GeoPackage."""
    practice = _practice()
    if practice is not None:
        return practice.offline
    return _get("offline_file") or os.path.join(profile_folder(), "offline_work.sqlite")


def place_cache_path() -> str:
    """The file of the named areas and countries looked up (地名・国の控え, core/obs/place_cache.py)."""
    practice = _practice()
    return practice.place_cache if practice is not None else os.path.join(profile_folder(), "place_cache.sqlite")


def opened_images_path() -> str:
    """The file of the images opened from a catalog (開いた画像の履歴, core/catalog/opened.py): this computer's paths."""
    practice = _practice()
    return practice.opened_images if practice is not None else os.path.join(profile_folder(), "opened_images.json")


def thumbnail_folder() -> str:
    """Where the 新着画像 cards keep the thumbnails they fetched (qgis_io/thumbnails.py)."""
    practice = _practice()
    return practice.thumbnails if practice is not None else os.path.join(profile_folder(), "thumbnails")
