"""
画像カタログ's door (窓口) for the rest of the plugin (docs/architecture.md R2): what the other
features, the screens and the plugin import of it. Its modules import each other directly;
everything outside imports through here, so its inside can be rearranged behind it.
"""
from __future__ import annotations

from . import saved_areas
from .config import CatalogConfig
from .images import find_images, FolderImages, patterns_for
from .opened import OpenedImage, OpenedImages
from .paths import PathError, PathMapping, PathRewrite, resolve_path, rewrites_of
from .query import CatalogSearch, time_text
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

__all__ = [
    "add_areas", "areas_of", "areas_of_shape", "catalog_info", "CatalogConfig", "CatalogLayerInfo", "CatalogRecord",
    "CatalogSearch", "CatalogWatch", "cell_text", "check_due", "check_one", "Checked", "Column", "conditions_text", "Contents",
    "contents_lines", "contents_of", "DEFAULT_HEIGHT", "DEFAULT_WIDTH", "find_images", "FolderImages", "footprint_of",
    "is_address", "layer_origin", "list_columns", "listed_image", "MAX_SEEN", "new_watch", "OpenedImage", "OpenedImages",
    "PathError", "PathMapping", "PathRewrite", "patterns_for", "place_text", "rank_similar", "read_catalog_layer", "record_of",
    "records_by_ids", "replace_areas", "resolve_path", "rewrites_of", "saved_areas", "search_catalog", "sensor_view_of",
    "sensors_of", "similar_search", "SimilarTolerance", "sort_records", "thumbnail_address", "time_text", "WatchNotice",
    "WatchStore",
]
