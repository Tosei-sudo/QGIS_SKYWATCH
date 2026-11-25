"""
画像カタログ・新着画像の個人設定 (QgsSettings, per user profile: docs/architecture.md, 設定を機能ごとに). Read
through qgis_io/settings.py as before (it lists them); a new one of this feature goes here.
"""
from __future__ import annotations

import json
from typing import Optional

from ..qsettings import pref, set_pref


def overview_cache_setting() -> str:
    """The settings file's `overviews.cacheFolder` as last read ("" for the profile's folder), used when QGIS starts."""
    return pref("overview_cache_folder")


def set_overview_cache_setting(folder: str) -> None:
    set_pref("overview_cache_folder", folder)


def overview_temp_setting() -> str:
    """The settings file's `overviews.tempFolder` as last read ("" for the system's), swept when QGIS starts."""
    return pref("overview_temp_folder")


def set_overview_temp_setting(folder: str) -> None:
    set_pref("overview_temp_folder", folder)


def overview_cache_to_clear() -> str:
    """The overview cache folder to clear when QGIS starts next ("" for none)."""
    return pref("overview_cache_clear")


def set_overview_cache_to_clear(folder: str) -> None:
    set_pref("overview_cache_clear", folder)


def central_views() -> list[str]:
    """The plugin's pages beside the map (ui/central_views.py) open when QGIS was last closed, in tab order."""
    return [k for k in pref("centralViews").split(",") if k]


def set_central_views(keys: list[str]) -> None:
    set_pref("centralViews", ",".join(keys))


def feed_close_previous() -> bool:
    """「前の画像を閉じる」 of the 新着画像 page (its own, not the catalog's); off unless turned on."""
    return pref("noticeFeed/closePrevious") == "1"


def set_feed_close_previous(on: bool) -> None:
    set_pref("noticeFeed/closePrevious", "1" if on else "0")


def feed_unread_only() -> bool:
    """「未読のみ」 of the 新着画像 page; off unless turned on."""
    return pref("noticeFeed/unreadOnly") == "1"


def set_feed_unread_only(on: bool) -> None:
    set_pref("noticeFeed/unreadOnly", "1" if on else "0")


#: How the image catalog draws the listed images' footprints: filled, outlines only, or not at all.
FOOTPRINT_STYLES = ("fill", "outline", "hidden")


def footprint_style() -> str:
    """The catalog's footprints on the map (tsudo, 2026-11-09: filled ones hid the images); outlines unless chosen."""
    v = pref("catalog/footprints")
    return v if v in FOOTPRINT_STYLES else "outline"


def set_footprint_style(style: str) -> None:
    set_pref("catalog/footprints", style if style in FOOTPRINT_STYLES else "outline")


def close_previous() -> bool:
    """
    「前の画像を閉じる」 (tsudo, 2026-11-10: looking through images one after
    another): opening images from the catalog closes those it opened before.
    Off unless turned on.
    """
    return pref("catalog/closePrevious") == "1"


def set_close_previous(on: bool) -> None:
    set_pref("catalog/closePrevious", "1" if on else "0")


def zoom_to_opened() -> bool:
    """
    「開いた画像へ移動」 (tsudo, 2026-11-10): opening images from the catalog
    moves the map to them (all of them when several). On unless turned off.
    """
    return pref("catalog/zoomToOpened") != "0"


def set_zoom_to_opened(on: bool) -> None:
    set_pref("catalog/zoomToOpened", "1" if on else "0")


def match_crs() -> bool:
    """
    「投影法を画像に合わせる」 (tsudo, 2026-11-25): opening images from the
    catalog puts the map in the image's CRS (core/map_view.py). Off unless turned on.
    """
    return pref("catalog/matchCrs") == "1"


def set_match_crs(on: bool) -> None:
    set_pref("catalog/matchCrs", "1" if on else "0")


def map_orientation() -> str:
    """「向き」 of the catalog (core/map_view.ORIENTATIONS): the map's way up when images open; north unless chosen."""
    from ...core.map_view import ORIENTATIONS

    v = pref("catalog/orientation")
    return v if v in ORIENTATIONS else "north"


def set_map_orientation(value: str) -> None:
    from ...core.map_view import ORIENTATIONS

    set_pref("catalog/orientation", value if value in ORIENTATIONS else "north")


def saved_areas() -> list:
    """「保存した範囲」 of the image catalog (core/catalog/saved_areas.py), newest first; per user, like the list views."""
    from ...core.catalog.api import read_areas

    return read_areas(pref("catalog/savedAreas"))


def set_saved_areas(areas) -> None:
    from ...core.catalog.api import write_areas

    set_pref("catalog/savedAreas", write_areas(areas))


def path_folders() -> dict[str, str]:
    """The folders chosen on this computer for `pathMappings` prefixes (by core.catalog.paths.folder_key)."""
    try:
        value = json.loads(pref("catalog/path_folders", "{}"))
    except ValueError:
        return {}
    return {k: v for k, v in value.items() if isinstance(k, str) and isinstance(v, str) and v} if isinstance(value, dict) else {}


def path_folder(prefix: str) -> Optional[str]:
    from ...core.catalog.api import folder_key

    return path_folders().get(folder_key(prefix))


def set_path_folder(prefix: str, folder: Optional[str]) -> None:
    from ...core.catalog.api import folder_key

    folders = path_folders()
    if folder:
        folders[folder_key(prefix)] = folder
    else:
        folders.pop(folder_key(prefix), None)
    set_pref("catalog/path_folders", json.dumps(folders, ensure_ascii=False))


def clear_path_folders() -> None:
    """Forgets every folder chosen for the prefixes (「設定…」の「画像のフォルダの対応」)."""
    set_pref("catalog/path_folders", "{}")


def similar_tolerance() -> dict:
    """The tolerances of 「類似画像の検索」 last used (core.catalog.similar.SimilarTolerance JSON)."""
    try:
        value = json.loads(pref("catalog/similar_tolerance", "{}"))
    except ValueError:
        return {}
    return value if isinstance(value, dict) else {}


def set_similar_tolerance(value: dict) -> None:
    set_pref("catalog/similar_tolerance", json.dumps(value))
