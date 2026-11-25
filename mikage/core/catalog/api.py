"""
画像カタログ's door (窓口) for the rest of the plugin (docs/architecture.md R2): what the other
features, the screens and the plugin import of it. Its modules import each other directly;
everything outside imports through here, so its inside can be rearranged behind it.
"""
from __future__ import annotations

from typing import TYPE_CHECKING

from ..door import lazy

if TYPE_CHECKING:  # the door (core/door.py): each name is imported from its module when first taken
    from . import saved_areas
    from .config import catalog_of, CatalogConfig, MODES
    from .images import find_images, FolderImages, patterns_for
    from .opened import OpenedImage, OpenedImages
    from .paths import folder_key, path_mappings_of, PathError, PathMapping, PathRewrite, resolve_path, rewrites_of
    from .query import CatalogSearch, time_text
    from .saved_areas import read_areas, write_areas
    from .sensors import (
        KINDS, kind_word, kinds_in, OPTICAL, pick_sensors, SAR, SensorPick, sensor_kind, sensor_list, sensors_named,
        SEPARATOR as SENSOR_SEPARATOR, words_of as sensor_words_of,
    )
    from .search import (
        catalog_info, CatalogLayerInfo, CatalogRecord, cell_text, Column, list_columns, listed_image, place_text,
        read_catalog_layer, record_of, records_by_ids, search_catalog, sensor_view_of, sensors_of, sort_records,
    )
    from .similar import rank_similar, similar_search, SimilarTolerance
    from .thumbnail import (
        Contents, contents_lines, contents_of, DEFAULT_HEIGHT, DEFAULT_WIDTH, footprint_of, is_address, thumbnail_address,
    )
    from .watch import (
        add_areas, areas_of, areas_of_shape, CatalogWatch, conditions_text, layer_origin, MAX_SEEN, new_watch, replace_areas,
        WatchNotice,
    )
    from .watch_store import check_due, check_one, Checked, WatchStore

__getattr__, __dir__ = lazy(__name__)

__all__ = [
    "folder_key", "read_areas", "write_areas",
    "add_areas", "areas_of", "areas_of_shape", "catalog_info", "catalog_of", "CatalogConfig", "CatalogLayerInfo", "CatalogRecord",
    "CatalogSearch", "CatalogWatch", "cell_text", "check_due", "check_one", "Checked", "Column", "conditions_text", "Contents",
    "contents_lines", "contents_of", "DEFAULT_HEIGHT", "DEFAULT_WIDTH", "find_images", "FolderImages", "footprint_of",
    "is_address", "layer_origin", "list_columns", "listed_image", "MAX_SEEN", "MODES", "new_watch", "OpenedImage", "OpenedImages",
    "path_mappings_of", "PathError", "PathMapping", "PathRewrite", "patterns_for", "place_text", "rank_similar", "read_catalog_layer", "record_of",
    "records_by_ids", "replace_areas", "resolve_path", "rewrites_of", "saved_areas", "search_catalog", "sensor_view_of",
    "sensors_of", "similar_search", "SimilarTolerance", "sort_records", "thumbnail_address", "time_text", "WatchNotice",
    "WatchStore",
    "KINDS", "kind_word", "kinds_in", "OPTICAL", "pick_sensors", "SAR", "SensorPick", "sensor_kind", "sensor_list", "sensors_named", "SENSOR_SEPARATOR",
    "sensor_words_of",
]
