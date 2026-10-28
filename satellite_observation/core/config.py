"""
The settings file: the viewer's `config.json`, read as it is. Only the keys
this plugin uses are read; others are left alone, so the viewer's file can
be shared. Comments (`//`, `/* */`) and trailing commas are allowed (JSONC).

As in the viewer (config.ts `parseConfig`), a missing or invalid entry falls
back to its default and the reason goes to `problems`, so the plugin always
starts.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Optional

from .auth import ArcgisPortalConfig, arcgis_portal_of
from .catalog import CatalogConfig, catalog_of
from .catalog.config import DEFAULT_WATCH_INTERVAL, watch_interval_of
from .catalog.paths import PathMapping, path_mappings_of
from .obs.model import ObservationStoreConfig, ObservationVocabulary, observation_store_of, vocabulary_of
from .plan.modes import SensorCatalogConfig, sensor_catalog_of
from .plan.satellites import GroundStation, SatelliteCatalogConfig, ground_stations_of, satellite_catalog_of
from .plan.store import PlanStoreConfig, plan_store_of
from .plan.weather import DEFAULT_CLOUD_FORECAST, CloudForecastConfig, cloud_forecast_of
from .startup_layers import StartupLayer, startup_layers_of

#: The keys this plugin reads.
KEYS = (
    "satelliteCatalogs", "sensorCatalogs", "planStores", "groundStations", "cloudForecast",
    "imageCatalogs", "watchInterval", "pathMappings", "observationStores", "observationVocabulary", "arcgisPortals", "layers",
)


@dataclass
class PluginConfig:
    image_catalogs: list[CatalogConfig] = field(default_factory=list)
    #: Minutes between checks for new images.
    watch_interval: float = DEFAULT_WATCH_INTERVAL
    path_mappings: list[PathMapping] = field(default_factory=list)
    satellite_catalogs: list[SatelliteCatalogConfig] = field(default_factory=list)
    sensor_catalogs: list[SensorCatalogConfig] = field(default_factory=list)
    #: None hides the choice.
    cloud_forecast: Optional[CloudForecastConfig] = DEFAULT_CLOUD_FORECAST
    ground_stations: list[GroundStation] = field(default_factory=list)
    plan_stores: list[PlanStoreConfig] = field(default_factory=list)
    observation_stores: list[ObservationStoreConfig] = field(default_factory=list)
    observation_vocabulary: ObservationVocabulary = field(default_factory=ObservationVocabulary)
    arcgis_portals: list[ArcgisPortalConfig] = field(default_factory=list)
    #: `layers`: added to an empty project when QGIS starts (the first entry at the bottom).
    startup_layers: list[StartupLayer] = field(default_factory=list)


class JsoncError(ValueError):
    """The text is not JSON (with comments)."""


def strip_jsonc(text: str) -> str:
    """JSON of a JSONC text: comments and trailing commas removed (strings untouched)."""
    out: list[str] = []
    i, n = 0, len(text)
    in_string = False
    while i < n:
        c = text[i]
        if in_string:
            out.append(c)
            if c == "\\" and i + 1 < n:
                out.append(text[i + 1])
                i += 2
                continue
            if c == '"':
                in_string = False
            i += 1
            continue
        if c == '"':
            in_string = True
            out.append(c)
            i += 1
        elif text.startswith("//", i):
            end = text.find("\n", i)
            i = n if end < 0 else end
        elif text.startswith("/*", i):
            end = text.find("*/", i + 2)
            if end < 0:
                raise JsoncError("閉じていないコメント")
            # Keep line numbers for error messages.
            out.append("\n" * text.count("\n", i, end))
            i = end + 2
        else:
            out.append(c)
            i += 1
    # Trailing commas: a comma followed (after blanks) by } or ], outside strings.
    s = "".join(out)
    result: list[str] = []
    in_string = False
    i = 0
    while i < len(s):
        c = s[i]
        if in_string:
            result.append(c)
            if c == "\\" and i + 1 < len(s):
                result.append(s[i + 1])
                i += 2
                continue
            if c == '"':
                in_string = False
        elif c == '"':
            in_string = True
            result.append(c)
        elif c == ",":
            j = i + 1
            while j < len(s) and s[j] in " \t\r\n":
                j += 1
            if j < len(s) and s[j] in "}]":
                i += 1
                continue
            result.append(c)
        else:
            result.append(c)
        i += 1
    return "".join(result)


def load_jsonc(text: str) -> Any:
    """The value of a JSON (or JSONC) text; raises JsoncError with the place when it cannot be read."""
    if text.startswith("﻿"):
        text = text[1:]
    try:
        return json.loads(strip_jsonc(text))
    except json.JSONDecodeError as error:
        raise JsoncError(f"JSON として読み込み不可（{error.lineno} 行 {error.colno} 列: {error.msg}）") from error


def parse_config(value: Any) -> tuple[PluginConfig, list[str]]:
    """The settings of a parsed file, and what was wrong in it."""
    problems: list[str] = []
    config = PluginConfig()
    if not isinstance(value, dict):
        return config, ["設定はオブジェクトで指定"]

    def listed(key: str, read) -> list:
        if key not in value:
            return []
        if not isinstance(value[key], list):
            problems.append(f"{key} は配列で指定")
            return []
        return [c for c in (read(v, problems, i) for i, v in enumerate(value[key])) if c is not None]

    config.image_catalogs = listed("imageCatalogs", catalog_of)
    if "layers" in value:
        config.startup_layers = startup_layers_of(value["layers"], problems)
    if "watchInterval" in value:
        config.watch_interval = watch_interval_of(value["watchInterval"], problems)
    if "pathMappings" in value:
        config.path_mappings = path_mappings_of(value["pathMappings"], problems)
    config.satellite_catalogs = listed("satelliteCatalogs", satellite_catalog_of)
    config.sensor_catalogs = listed("sensorCatalogs", sensor_catalog_of)
    if "cloudForecast" in value:
        config.cloud_forecast = cloud_forecast_of(value["cloudForecast"], problems)
    if "groundStations" in value:
        config.ground_stations = ground_stations_of(value["groundStations"], problems)
    config.plan_stores = listed("planStores", plan_store_of)
    config.observation_stores = listed("observationStores", observation_store_of)
    if "observationVocabulary" in value:
        config.observation_vocabulary = vocabulary_of(value["observationVocabulary"], problems)
    if "arcgisPortals" in value:
        seen: set[str] = set()
        portals = []
        for p in listed("arcgisPortals", arcgis_portal_of):
            if p.url in seen:
                problems.append(f"arcgisPortals の url「{p.url}」が重複")
                continue
            seen.add(p.url)
            portals.append(p)
        config.arcgis_portals = portals
    return config, problems


def parse_config_text(text: str) -> tuple[PluginConfig, list[str]]:
    """The settings of a file's text; the defaults (and why) when it cannot be read."""
    try:
        value = load_jsonc(text)
    except JsoncError as error:
        return PluginConfig(), [f"設定ファイルを {error}"]
    return parse_config(value)


def read_config_file(path: str) -> tuple[PluginConfig, list[str]]:
    """The settings of a local file (UTF-8)."""
    try:
        with open(path, encoding="utf-8-sig") as f:
            text = f.read()
    except OSError as error:
        return PluginConfig(), [f"設定ファイルの読み込み不可（{error.strerror or error}）"]
    except UnicodeDecodeError:
        return PluginConfig(), ["設定ファイルは UTF-8 で保存"]
    return parse_config_text(text)
