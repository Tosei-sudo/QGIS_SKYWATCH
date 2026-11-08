"""SKYWATCH (folder and plugin id `skywatch`): a QGIS plugin.

The `core` package is plain Python (no Qt, no QGIS) and is tested with pytest
alone; `qgis_io` and `ui` hold the QGIS side.
"""


def classFactory(iface):  # noqa: N802 (QGIS's name)
    # First of all, before QGIS opens any image: GDAL reads where to put the .ovr of read-only folders once.
    try:
        from .qgis_io.overviews_io import start_cache

        start_cache()
    except Exception:  # noqa: BLE001 (never keeps the plugin from loading)
        pass
    from .plugin import SkywatchPlugin

    return SkywatchPlugin(iface)
