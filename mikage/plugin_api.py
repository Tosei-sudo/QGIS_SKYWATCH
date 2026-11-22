"""
The plugin's Python API (`plugin.api`, docs/features/screen.md「Python API」): what scripts and the
Python console reach of the docks and tools. Its names are kept as they are (scripts use them).
"""
from __future__ import annotations

from typing import TYPE_CHECKING, Optional

from .core.obs.api import ObservationApi

if TYPE_CHECKING:  # the docks and tools it hands out (named in its annotations only)
    from .plugin import MikagePlugin
    from .ui.ai_results_dock import AiResultsDock
    from .ui.analysis_dock import AnalysisDock
    from .ui.catalog_dock import CatalogDock
    from .ui.coordinate_box import CoordinateBox
    from .ui.dashboard_dock import DashboardDock
    from .ui.enhance_dock import EnhanceDock
    from .ui.events_dock import EventsDock
    from .ui.imaging_plan_dock import ImagingPlanDock
    from .ui.interests_dock import InterestsDock
    from .ui.moving_dock import MovingDock
    from .ui.moving_target_dialog import MovingTargetDialog
    from .ui.new_layer_dialog import NewLayerDialog
    from .ui.notices_dock import NoticesDock
    from .ui.observation_dock import ObservationDock
    from .ui.place_schedule_dock import PlaceScheduleDock
    from .ui.plan_file_dock import PlanFileDock
    from .ui.request_dock import RequestDock
    from .ui.satellite_pass_dock import SatellitePassDock
    from .ui.schedule_dock import ScheduleDock
    from .ui.segment_tool import SegmentTool
    from .ui.shadow_dock import ShadowHeightDock
    from .ui.shape_receiver import ShapeReceiver
    from .ui.shift import LayerShift
    from .ui.spectral_dock import SpectralDock
    from .ui.swipe import SwipeCompare


class PluginApi:
    """The plugin's Python API (`plugin.api`), for the Python console and Processing scripts; filled stage by stage."""

    def __init__(self, plugin: "MikagePlugin"):
        self._plugin = plugin

    @property
    def config(self):
        """The settings file as read (core.config.PluginConfig)."""
        return self._plugin.context.config

    @property
    def problems(self) -> list[str]:
        """What was wrong in the settings file."""
        return list(self._plugin.context.problems)

    def reload_config(self) -> None:
        """Reads the settings file again."""
        self._plugin.context.reload()

    @property
    def startup_times(self) -> dict:
        """How long the startup took (`steps`, `total`) and each read ahead (`reads`), in seconds."""
        return self._plugin.clock.times()

    def preload(self) -> None:
        """Reads ahead now what the settings file's `preload` lists (in the background)."""
        if self._plugin.preloader is not None:
            self._plugin.preloader.run()

    @property
    def observation_dock(self) -> "ObservationDock":
        """The observations dock (its `book`, `save_csv(path)`, `to_layers()`, `set_observer(name)`…)."""
        return self._plugin.docks["SatObsObservations"]

    def import_taxonomy(self, source, sheet: Optional[str] = None, uses=None, mode: str = "add", save: bool = False,
                        fill_down: bool = True, level_names: bool = True, selected_only: bool = False) -> dict:
        """
        「分類体系の取り込み」 for scripts: a table (a CSV or Excel file's path,
        a layer, or a layer's id or name) into the classification of the
        observations dock's store. Tried only unless `save` (the classification
        as it was kept first in the profile's mikage/taxonomy-backups).
        """
        from .qgis_io import taxonomy_import_io as tio

        columns, rows, name = tio.read_source(source, sheet, selected_only)
        return self._plugin.docks["SatObsObservations"].api().import_taxonomy(
            columns, rows, uses, mode, save, fill_down, level_names, name, tio.write_backup if save else None)

    @property
    def imaging_plan(self) -> "ImagingPlanDock":
        """The imaging plan dock (its `plan`, `shown`, `run()`, `set_point()`, `csv_text()`, `add_layer()`)."""
        return self._plugin.docks["SatObsImagingPlan"]

    def moving_targets(self, layer=None) -> "MovingTargetDialog":
        """Opens 「移動目標撮像計画」 for a point layer (or the active one); its `targets()`, `add_places()`, `plan()`."""
        return self._plugin.open_moving_targets(layer)

    @property
    def satellite_pass(self) -> "SatellitePassDock":
        """The satellite pass dock (its `tracks`, `choose()`, `set_time()`, `draw()`, `add_layer()`)."""
        return self._plugin.docks["SatObsSatellitePass"]

    @property
    def requests(self) -> "RequestDock":
        """The imaging requests dock (its `requests`, `reload()`, `edit()`, `import_layer()`, `export_layer()`)."""
        return self._plugin.docks["SatObsRequests"]

    @property
    def plan_files(self) -> "PlanFileDock":
        """The imaging plan files dock (its `run()`, `plan`, `write()`, `paths`)."""
        return self._plugin.docks["SatObsPlanFiles"]

    @property
    def schedule(self) -> "ScheduleDock":
        """The imaging schedule dock: a day's plan as the systems answered (its `rows`, `set_day()`, `reload()`, `answer()`,
        `import_file()`, `to_layer()`)."""
        return self._plugin.docks["SatObsSchedule"]

    @property
    def place_schedule(self) -> "PlaceScheduleDock":
        """The plans ahead of each place on a timeline (its `lanes`, `add_point()`, `set_layer()`, `clear_points()`, `reload()`)."""
        return self._plugin.docks["SatObsPlaceSchedule"]

    @property
    def image_catalog(self) -> "CatalogDock":
        """The image catalog dock (its `search()`, `records`, `choose()`, `open_records()`, `find_similar()`, `add_layer()`)."""
        return self._plugin.docks["SatObsImageCatalog"]

    @property
    def notices(self) -> "NoticesDock":
        """The notices dock (its `store()`, `check()`, `watch_conditions()`, `watch_point()`, `open_notices()`)."""
        return self._plugin.docks["SatObsNotices"]

    @property
    def image_enhance(self) -> "EnhanceDock":
        """The image correction dock (its `enhance(layer, correction, bands)`, `pipeline()`, `set_pipeline()`, `apply()`)."""
        return self._plugin.docks["SatObsEnhance"]

    @property
    def shadow_height(self) -> "ShadowHeightDock":
        """The dock 「影から高さ」 (its `measurements`, `measure(foot, tip)`, `set_sun()`, `set_time()`, `clear()`, `add_layer()`)."""
        return self._plugin.docks["SatObsShadowHeight"]

    @property
    def coordinates(self) -> "CoordinateBox":
        """The jump field and the cursor position (its `jump(text)`, `search`, `go_to_place()`, `set_notation()`)."""
        return self._plugin.coordinates

    @property
    def shape_receiver(self) -> "ShapeReceiver":
        """「外部から図形を受け取る」 (its `listening`, `url`, `problem`, `show_link(text)`: the shapes of `geom=…&label=…` shown as if sent)."""
        return self._plugin.shape_receiver

    @property
    def spectral(self) -> "SpectralDock":
        """The dock 「スペクトル」 (its `series`, `labels`, `add_point(point)`, `add_box(rect)`, `csv_text()`, `save_csv(path)`, `clear()`)."""
        return self._plugin.docks["SatObsSpectral"]

    @property
    def dashboard(self) -> "DashboardDock":
        """The dock 「ダッシュボード」 (its `figures`, `categories`, `click_value(v)`, `click_bin(i)`, `click_step(i)`, `set_heatmap(on)`, `clear_filter()`)."""
        return self._plugin.docks["SatObsDashboard"]

    @property
    def moving(self) -> "MovingDock":
        """The dock 「移動体（AIS・ADS-B）」 (its `connect_feed(FeedConfig)`, `open_history(path)`, `layers`, `align_to_image()`, `go_live()`)."""
        return self._plugin.docks["SatObsMoving"]

    @property
    def events(self) -> "EventsDock":
        """The dock 「イベント」 (its `events`, `links`, `reload()`, `link_items(event_ids, items)`, `choose(id)`)."""
        return self._plugin.docks["SatObsEvents"]

    @property
    def interests(self) -> "InterestsDock":
        """The dock 「関心地域・地点」 (its `book` (`items`, `load()`, `over(geometry)`), `chosen()`, `choose(id)`, `save()`, `delete()`, `zoom()`)."""
        return self._plugin.docks["SatObsInterests"]

    @property
    def analysis(self) -> "AnalysisDock":
        """The dock 「データ分析」 (its `tables`, `set_sql(text)`, `set_value(name, v)`, `run(then)`, `result`, `save(name)`, `load(name)`, `add_layer()`)."""
        return self._plugin.docks["SatObsAnalysis"]

    @property
    def ai_results(self) -> "AiResultsDock":
        """The dock 「AI結果」 (its `load(image_id)`, `rows`, `selected_rows()`, `import_rows(rows)`, `zoom_to(rows)`, `withhold(why)`)."""
        return self._plugin.docks["SatObsAiResults"]

    @property
    def swipe(self) -> "SwipeCompare":
        """「スワイプ比較」 (its `set_active()`, `set_target(layer or group)`, `set_position(0-1)`, `set_orientation()`)."""
        return self._plugin.swipe

    @property
    def shift(self) -> "LayerShift":
        """「位置ずらし」 (its `set_active()`, `set_shift_metres(layer, east, north)`, `shift_metres(layer)`, `reset()`)."""
        return self._plugin.shift

    @property
    def segment(self) -> "SegmentTool":
        """「クリック抽出（AI）」 (its `set_active()`, `add_model_files([encoder, decoder])`, `click(point)`, `undo()`, `keep()`, `outlines()`)."""
        return self._plugin.segment

    def new_scratch_layer(self, template: Optional[str] = None, source: str = "builtin") -> "NewLayerDialog":
        """Opens 「新規スクラッチレイヤー（テンプレート）」 (with a template: `source` builtin, config or saved); its `create()`."""
        return self._plugin.open_new_layer(source, template)


def _observation_function(name: str):
    """A function of the observations for scripts (core.obs.api.ObservationApi), on the dock's chosen store."""
    def call(self, *args, **kwargs):
        return getattr(self._plugin.docks["SatObsObservations"].api(), name)(*args, **kwargs)

    call.__name__ = name
    call.__doc__ = getattr(ObservationApi, name).__doc__
    # Its arguments as ObservationApi has them (inspect.signature, the API reference).
    call.__wrapped__ = getattr(ObservationApi, name)
    return call


#: As the viewer's scripts have them (viewer.observations → api.observations, viewer.addObservation → api.add_observation…).
OBSERVATION_FUNCTIONS = [
    "observations", "observed_things", "observation_images", "observe_image", "add_observation", "update_observation",
    "rename_observed_thing", "delete_observations", "clear_observations", "observation_candidates", "confidences", "checked_areas", "add_checked_area",
    "delete_checked_areas", "observation_report",
]
for _name in OBSERVATION_FUNCTIONS:
    setattr(PluginApi, _name, _observation_function(_name))
