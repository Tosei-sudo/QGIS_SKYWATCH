"""衛星撮像計画・観測記録 (satellite_observation): a QGIS plugin.

The `core` package is plain Python (no Qt, no QGIS) and is tested with pytest
alone; `qgis_io` and `ui` hold the QGIS side.
"""


def classFactory(iface):  # noqa: N802 (QGIS's name)
    from .plugin import SatelliteObservationPlugin

    return SatelliteObservationPlugin(iface)
