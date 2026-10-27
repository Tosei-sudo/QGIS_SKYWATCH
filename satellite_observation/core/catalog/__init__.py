"""
Image catalogs (`imageCatalogs`): the settings (config.py), the search
conditions (query.py), searching and listing (search.py), 「類似画像の検索」
(similar.py), local paths (paths.py) and the watches for new images
(watch.py, watch_store.py). Free of Qt and QGIS.
"""
from .config import ANGLE_KINDS, ROLE_LABELS, CatalogConfig, catalog_of

__all__ = ["ANGLE_KINDS", "ROLE_LABELS", "CatalogConfig", "catalog_of"]
