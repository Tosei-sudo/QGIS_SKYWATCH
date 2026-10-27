"""Reading satellite and sensor catalogs from Esri layers (the viewer's `loadSatellites` and `loadSensorModes`)."""
from __future__ import annotations

from typing import Callable, Optional, Sequence

from ..esri import EsriClient
from .modes import SensorCatalogConfig, SensorMode, sensor_mode_of
from .satellites import SatelliteCatalogConfig, SatelliteSpec, satellite_of

TokenFor = Callable[[str, Optional[str]], Optional[str]]


def load_satellites(client: EsriClient, catalog: SatelliteCatalogConfig, token_for: TokenFor,
                    cancelled: Optional[Callable[[], bool]] = None) -> tuple[list[SatelliteSpec], list[str]]:
    """The satellites of a catalog, and the records that could not be read (every page, without geometry)."""
    rows = client.query_all(catalog.url, catalog.where or "1=1", token_for(catalog.url, catalog.token), geometry=False, cancelled=cancelled)
    satellites: list[SatelliteSpec] = []
    problems: list[str] = []
    for row in rows:
        sat = satellite_of(row.get("attributes") or {}, catalog)
        if isinstance(sat, str):
            problems.append(sat)
        else:
            satellites.append(sat)
    return satellites, problems


def load_sensor_modes(client: EsriClient, catalogs: Sequence[SensorCatalogConfig], token_for: TokenFor,
                      cancelled: Optional[Callable[[], bool]] = None) -> tuple[list[SensorMode], list[str]]:
    """The modes of the sensor catalogs: those written in the settings file, and those of tables."""
    modes: list[SensorMode] = []
    problems: list[str] = []
    for catalog in catalogs:
        modes.extend(catalog.modes)
        if not catalog.url:
            continue
        try:
            rows = client.query_all(catalog.url, catalog.where or "1=1", token_for(catalog.url, catalog.token), geometry=False, cancelled=cancelled)
        except Exception as error:  # noqa: BLE001 (reported, the others still read)
            problems.append(f"{catalog.label} の読み込みに失敗（{error}）")
            continue
        for row in rows:
            mode = sensor_mode_of(row.get("attributes") or {}, catalog)
            if mode:
                modes.append(mode)
    return modes, problems
