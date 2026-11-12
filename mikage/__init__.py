"""MIKAGE (folder and plugin id `mikage`): a QGIS plugin.

The `core` package is plain Python (no Qt, no QGIS) and is tested with pytest
alone; `qgis_io` and `ui` hold the QGIS side.
"""


def classFactory(iface):  # noqa: N802 (QGIS's name)
    from .core.preload import StartupClock

    clock = StartupClock()
    # First of all, before QGIS opens any image: GDAL reads where to put the .ovr of read-only folders once.
    try:
        from .qgis_io.overviews_io import start_cache

        start_cache()
    except Exception:  # noqa: BLE001 (never keeps the plugin from loading)
        pass
    from .plugin import MikagePlugin

    clock.step("プラグインの読み込み")
    plugin = MikagePlugin(iface)
    plugin.clock = clock
    return plugin
