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
from typing import Any, Callable, Optional

from . import errors
from .ai.model_config import AiModelConfig, ai_model_of
from .ai.transfer import DetectionOutput, detection_output_of
from .ai_results import AiResultSource, ai_result_source_of
from .auth import ArcgisPortalConfig, arcgis_portal_of
from .audit import DEFAULT_AUDIT, AuditSettings, audit_log_of
from .training.api import DEFAULT_TRAINING, TrainingSettings, training_settings_of
from .edit_stamp import DEFAULT_EDIT_TRACKING, EditTracking, edit_tracking_of
from .basemaps import DEFAULT_BASE_MAPS, BaseMapConfig, base_maps_of, default_base_map_of
from .catalog import CatalogConfig, catalog_of
from .catalog.config import DEFAULT_WATCH_INTERVAL, watch_interval_of
from .catalog.paths import PathMapping, PathRewrite, path_mappings_of
from .enhance.rules import ImageRule, image_rules_of
from .geocode import GeocoderConfig, geocoder_of
from .layer_templates import LayerTemplate, templates_of
from .overviews import DEFAULT_OVERVIEWS, OverviewOptions, overviews_of
from .raster_speed import DEFAULT_RASTER_SPEED, RasterSpeedOptions, raster_speed_of
from .complex_images import DEFAULT_MODE as DEFAULT_COMPLEX_MODE, complex_images_of
from .obs.alerts import AlertSettings, EsriSource, alert_settings_of, level_key_problems
from .obs.assignments import assignment_source_of
from .obs.glossary import glossary_source_of
from .obs.handover import HandoverSettings, handover_settings_of
from .obs.model import ObservationStoreConfig, ObservationVocabulary, observation_store_of, vocabulary_of
from .plan.names import AliasTableConfig, alias_tables_of
from .plan.outages import satellite_outages_of
from .plan.modes import SensorCatalogConfig, sensor_catalog_of
from .plan.satellites import GroundStation, SatelliteCatalogConfig, ground_stations_of, satellite_catalog_of
from .plan.weather import DEFAULT_CLOUD_FORECAST, CloudForecastConfig, cloud_forecast_of
from .request.store import RequestStoreConfig, request_store_of
from .request.systems import ExportSystem, export_systems_of
from .obs.export import ExportSettings, export_settings_of
from .gp import GpServiceConfig, gp_service_of
from .moving import FeedConfig, moving_feeds_of
from .events import EventSettings, events_settings_of
from .interests import InterestSettings, interests_settings_of
from .roles import user_roles_settings_of
from .startup_layers import StartupLayer, startup_layers_of
from .time_zone import TimeZoneRule, time_zones_of
from .preload import ITEMS as PRELOAD_ITEMS, preload_of
from .shape_receiver import DEFAULT_RECEIVER, ReceiverSettings, receiver_settings_of
from .text_assist import TextAssistSettings, text_assist_settings_of
from .analysis_cache import DEFAULT_MINUTES as ANALYSIS_CACHE_MINUTES
from .util import is_number


@dataclass
class PluginConfig:
    image_catalogs: list[CatalogConfig] = field(default_factory=list)
    #: Minutes between checks for new images.
    watch_interval: float = DEFAULT_WATCH_INTERVAL
    #: `pathMappings`: prefixes and what they stand for, and rewrites (`pattern` / `to`) tried first.
    path_mappings: list[PathMapping | PathRewrite] = field(default_factory=list)
    #: `overviews`: an image opened from the catalog without overviews gets an external .ovr built in the background.
    overviews: OverviewOptions = DEFAULT_OVERVIEWS
    #: `rasterSpeed`: GDAL's threads and block cache in this QGIS, QGIS's slower options put right once (core/raster_speed.py).
    raster_speed: RasterSpeedOptions = DEFAULT_RASTER_SPEED
    #: `complexImages`: complex SAR images (SICD's I/Q bands, complex pixels) shown as "dB" or "amplitude"; None as QGIS opens them.
    complex_images: Optional[str] = DEFAULT_COMPLEX_MODE
    satellite_catalogs: list[SatelliteCatalogConfig] = field(default_factory=list)
    sensor_catalogs: list[SensorCatalogConfig] = field(default_factory=list)
    #: `satelliteAliases`: Esri tables of satellites' other names (the image catalog's 「WV03」 for 「WorldView-3」).
    satellite_aliases: list[AliasTableConfig] = field(default_factory=list)
    #: `satelliteOutages`: the Esri table of satellite outages (衛星の障害情報, core/plan/outages.py); None for none.
    satellite_outages: Optional[EsriSource] = None
    #: None hides the choice.
    cloud_forecast: Optional[CloudForecastConfig] = DEFAULT_CLOUD_FORECAST
    ground_stations: list[GroundStation] = field(default_factory=list)
    observation_stores: list[ObservationStoreConfig] = field(default_factory=list)
    #: `localObservations`: whether the local GeoPackage is offered as a store of records (false: only `observationStores`).
    local_observations: bool = True
    #: `preload`: what is read from Esri in the background right after QGIS has started (core/preload.ITEMS; all by default).
    preload: tuple = tuple(PRELOAD_ITEMS)
    observation_vocabulary: ObservationVocabulary = field(default_factory=ObservationVocabulary)
    #: `glossary`: the Esri table of the words the 備考 should use (備考の単語集, core/obs/glossary.py); None for none.
    glossary: Optional[EsriSource] = None
    #: `textAssist`: the small generative model of 文章の手伝い (備考を整える・申し送りの下書き, core/text_assist.py); None hides it.
    text_assist: Optional[TextAssistSettings] = None
    arcgis_portals: list[ArcgisPortalConfig] = field(default_factory=list)
    #: `layers`: added to an empty project when QGIS starts (the first entry at the bottom).
    startup_layers: list[StartupLayer] = field(default_factory=list)
    #: The background maps to switch between (the viewer's list when the file has none).
    base_maps: list[BaseMapConfig] = field(default_factory=lambda: list(DEFAULT_BASE_MAPS))
    #: The one shown at start ("" for none).
    default_base_map: str = ""
    #: The jump field's place search; none (the default) looks nothing up.
    geocoders: list[GeocoderConfig] = field(default_factory=list)
    #: The organisation's templates for a new scratch layer (after the built-in ones).
    layer_templates: list[LayerTemplate] = field(default_factory=list)
    #: The correction, bands and no data an image gets by its file name.
    image_rules: list[ImageRule] = field(default_factory=list)
    #: The moving-object feeds (AIS, ADS-B) offered in 「移動体（AIS・ADS-B）」.
    moving_feeds: list[FeedConfig] = field(default_factory=list)
    gp_services: list[GpServiceConfig] = field(default_factory=list)
    #: The AI models offered in 「物体検出（AI）」 and 「クリック抽出（AI）」.
    ai_models: list[AiModelConfig] = field(default_factory=list)
    detection_outputs: list[DetectionOutput] = field(default_factory=list)
    #: External AI results shown for the image opened from the catalog (「外部AI結果」).
    ai_result_sources: list[AiResultSource] = field(default_factory=list)
    #: The zone each source's dates are really in, when the service says otherwise:
    #: the `timeZone` of a source entry (its URLs and the layers under them), then `timeZones` rules.
    time_zones: list[TimeZoneRule] = field(default_factory=list)
    #: The imaging request ledgers (撮像依頼) and their history tables.
    request_stores: list[RequestStoreConfig] = field(default_factory=list)
    #: The satellite systems the day's requests are written for (in the order they are tried).
    request_exports: list[ExportSystem] = field(default_factory=list)
    #: `editTracking`: the fields every Esri layer written gets (when and who added / updated), and where the user name comes from.
    edit_tracking: EditTracking = DEFAULT_EDIT_TRACKING
    #: `auditLog`: the audit log of operations (監査ログ) and the folder it is sent to.
    audit_log: AuditSettings = DEFAULT_AUDIT
    #: `alertReports`: notable records as delimited text (通報); None hides it.
    alert_reports: Optional[AlertSettings] = None
    #: `observationExports`: the named formats (CSV / JSON) of 観測記録のエクスポート, besides the built-in ones.
    observation_exports: Optional[ExportSettings] = None
    #: `assignments`: the Esri table of who holds which image (分担); None hides it.
    assignments: Optional[EsriSource] = None
    #: `handover`: how the shift handover memo (引き継ぎメモ) is written and kept.
    handover: HandoverSettings = field(default_factory=HandoverSettings)
    #: `events`: the event layer and its link table (イベント); None hides the dock's work.
    events: Optional[EventSettings] = None
    #: `interests`: each person's own points and areas of interest (関心地域・地点), on the cards and in their dock.
    interests: Optional[InterestSettings] = None
    #: `userRoles`: the table of each person's roles (担当), which the menus and buttons shown follow (core/roles.py).
    user_roles: Optional[EsriSource] = None
    #: `offlineWork`: working on through a server outage (cache of reads, changes kept to send later).
    offline_work: OfflineSettings = field(default_factory=lambda: OfflineSettings())
    #: `columnDrag`: whether a list's columns may be dragged by their header (false: only from the header's menu).
    column_drag: bool = True
    #: `shapeReceiver`: shapes taken from AutoMap, links and programs on this computer (127.0.0.1) and shown on the map.
    shape_receiver: ReceiverSettings = DEFAULT_RECEIVER
    #: `training`: 練習モード (false hides it; `config`: the organisation's own practice settings file).
    training: TrainingSettings = DEFAULT_TRAINING
    #: `analysisCacheMinutes`: how long 「データ分析」 answers the same Esri query from memory (core/analysis_cache.py; 0: never).
    analysis_cache_minutes: float = ANALYSIS_CACHE_MINUTES


@dataclass
class OfflineSettings:
    """`offlineWork`: true / false, or {enabled, cacheMB, probeSeconds}."""
    enabled: bool = True
    #: The most the cache of reads may take (MB).
    cache_mb: float = 500
    #: How often a server down is asked whether it answers again (s).
    probe_seconds: float = 30


def offline_settings_of(value: Any, problems: list[str]) -> OfflineSettings:
    if isinstance(value, bool):
        return OfflineSettings(enabled=value)
    if not isinstance(value, dict):
        problems.append("offlineWork は true / false か {enabled, cacheMB, probeSeconds} で指定")
        return OfflineSettings()
    out = OfflineSettings()
    enabled = value.get("enabled", True)
    if isinstance(enabled, bool):
        out.enabled = enabled
    else:
        problems.append("offlineWork.enabled は true か false で指定")
    for key, attr, low in (("cacheMB", "cache_mb", 0), ("probeSeconds", "probe_seconds", 5)):
        v = value.get(key)
        if v is None:
            continue
        if isinstance(v, (int, float)) and not isinstance(v, bool) and v >= low:
            setattr(out, attr, float(v))
        else:
            problems.append(f"offlineWork.{key} は {low} 以上の数で指定")
    return out


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
        raise JsoncError(f"JSON として読めない（{error.lineno} 行 {error.colno} 列: {error.msg}）") from error


def _flag(key: str, default: bool) -> Callable[[Any, list[str]], bool]:
    """A true / false key (its default when it is neither)."""
    def read(value: Any, problems: list[str]) -> bool:
        if isinstance(value, bool):
            return value
        problems.append(f"{key} は true か false で指定")
        return default
    return read


def _analysis_cache_minutes(minutes: Any, problems: list[str]) -> float:
    if is_number(minutes) and 0 <= minutes <= 24 * 60:
        return minutes
    problems.append("analysisCacheMinutes はデータ分析の読み込みを覚えておく分数（0〜1440、0 で覚えない）で指定")
    return ANALYSIS_CACHE_MINUTES


def _listed(key: str, value: Any, read, problems: list[str]) -> list:
    """The items of a list key, each read on its own (those that cannot be left out, and why)."""
    if not isinstance(value, list):
        problems.append(f"{key} は配列で指定")
        return []
    return [c for c in (read(v, problems, i) for i, v in enumerate(value)) if c is not None]


def _arcgis_portals(value: Any, problems: list[str]) -> list[ArcgisPortalConfig]:
    seen: set[str] = set()
    portals = []
    for p in _listed("arcgisPortals", value, arcgis_portal_of, problems):
        if p.url in seen:
            problems.append(f"arcgisPortals の url「{p.url}」が重複")
            continue
        seen.add(p.url)
        portals.append(p)
    return portals


@dataclass(frozen=True)
class Section:
    """
    A key of the settings file (設定セクション, docs/architecture.md): the PluginConfig attribute it
    fills and how its value is read (`read(value, problems)`; a `listed` key's items one by one,
    `read(item, problems, index)`, those it cannot read left out; `whole`: from the whole file;
    `after`: also given the settings read before it).
    """
    key: str
    attr: str
    read: Callable
    listed: bool = False
    whole: bool = False
    after: bool = False


#: The keys this plugin reads, in the order they are read (the order of the problems told).
#: A new key: its PluginConfig attribute above and its line here (the config builder's page too).
SECTIONS = (
    Section("imageCatalogs", "image_catalogs", catalog_of, listed=True),
    Section("layers", "startup_layers", startup_layers_of),
    Section("watchInterval", "watch_interval", watch_interval_of),
    Section("pathMappings", "path_mappings", path_mappings_of),
    Section("overviews", "overviews", overviews_of),
    Section("rasterSpeed", "raster_speed", raster_speed_of),
    Section("complexImages", "complex_images", complex_images_of),
    Section("satelliteCatalogs", "satellite_catalogs", satellite_catalog_of, listed=True),
    Section("sensorCatalogs", "sensor_catalogs", sensor_catalog_of, listed=True),
    Section("satelliteAliases", "satellite_aliases", alias_tables_of),
    Section("satelliteOutages", "satellite_outages", satellite_outages_of),
    Section("cloudForecast", "cloud_forecast", cloud_forecast_of),
    Section("groundStations", "ground_stations", ground_stations_of),
    Section("observationStores", "observation_stores", observation_store_of, listed=True),
    Section("localObservations", "local_observations", _flag("localObservations", True)),
    Section("offlineWork", "offline_work", offline_settings_of),
    Section("columnDrag", "column_drag", _flag("columnDrag", True)),
    Section("analysisCacheMinutes", "analysis_cache_minutes", _analysis_cache_minutes),
    Section("preload", "preload", preload_of),
    Section("shapeReceiver", "shape_receiver", receiver_settings_of),
    Section("observationVocabulary", "observation_vocabulary", vocabulary_of),
    Section("glossary", "glossary", glossary_source_of),
    Section("textAssist", "text_assist", text_assist_settings_of),
    Section("arcgisPortals", "arcgis_portals", _arcgis_portals),
    Section("baseMaps", "base_maps", base_maps_of),
    Section("defaultBaseMap", "default_base_map", lambda v, problems, config: default_base_map_of(v, config.base_maps, problems),
            after=True),
    Section("geocoders", "geocoders", geocoder_of, listed=True),
    Section("layerTemplates", "layer_templates", templates_of),
    Section("imageRules", "image_rules", image_rules_of),
    Section("movingFeeds", "moving_feeds", moving_feeds_of),
    Section("gpServices", "gp_services", gp_service_of, listed=True),
    Section("aiModels", "ai_models", ai_model_of, listed=True),
    Section("detectionOutputs", "detection_outputs", detection_output_of, listed=True),
    Section("aiResultSources", "ai_result_sources", ai_result_source_of, listed=True),
    Section("timeZones", "time_zones", time_zones_of, whole=True),
    Section("requestStores", "request_stores", request_store_of, listed=True),
    Section("requestExports", "request_exports", export_systems_of),
    Section("editTracking", "edit_tracking", edit_tracking_of),
    Section("auditLog", "audit_log", audit_log_of),
    Section("alertReports", "alert_reports", alert_settings_of),
    Section("observationExports", "observation_exports", export_settings_of),
    Section("assignments", "assignments", assignment_source_of),
    Section("handover", "handover", handover_settings_of),
    Section("events", "events", events_settings_of),
    Section("interests", "interests", interests_settings_of),
    Section("userRoles", "user_roles", user_roles_settings_of),
    Section("training", "training", training_settings_of),
)
#: The keys this plugin reads.
KEYS = tuple(s.key for s in SECTIONS)


def parse_config(value: Any) -> tuple[PluginConfig, list[str]]:
    """The settings of a parsed file, and what was wrong in it."""
    problems: list[str] = []
    config = PluginConfig()
    if not isinstance(value, dict):
        return config, ["設定はオブジェクトで指定"]
    for s in SECTIONS:
        if s.whole:
            setattr(config, s.attr, s.read(value, problems))
        elif s.key not in value:
            continue
        elif s.listed:
            setattr(config, s.attr, _listed(s.key, value[s.key], s.read, problems))
        elif s.after:
            setattr(config, s.attr, s.read(value[s.key], problems, config))
        else:
            setattr(config, s.attr, s.read(value[s.key], problems))
    if config.alert_reports is not None:
        # confidence.<key>: a key some 確度 level has (the vocabulary's or a store's own).
        levels = [*config.observation_vocabulary.confidences, *(c for st in config.observation_stores for c in st.confidences or [])]
        problems.extend(level_key_problems(config.alert_reports, levels))
    return config, problems


def parse_config_text(text: str) -> tuple[PluginConfig, list[str]]:
    """The settings of a file's text; the defaults (and why) when it cannot be read."""
    try:
        value = load_jsonc(text)
    except JsoncError as error:
        return PluginConfig(), [errors.tagged(f"設定ファイルを {error}", "E702")]
    return parse_config(value)


def read_config_file(path: str) -> tuple[PluginConfig, list[str]]:
    """The settings of a local file (UTF-8)."""
    try:
        with open(path, encoding="utf-8-sig") as f:
            text = f.read()
    except OSError as error:
        return PluginConfig(), [errors.tagged(f"設定ファイルを読めない（{error.strerror or error}）", errors.os_code(error))]
    except UnicodeDecodeError:
        return PluginConfig(), [errors.tagged("設定ファイルの文字コードを読めない（UTF-8 で保存）", "E702")]
    return parse_config_text(text)
