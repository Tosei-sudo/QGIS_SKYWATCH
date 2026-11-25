"""
撮像計画's door (窓口) for the rest of the plugin (docs/architecture.md R2): what the other
features, the screens and the plugin import of it. Its modules import each other directly;
everything outside imports through here, so its inside can be rearranged behind it.
"""
from __future__ import annotations

from typing import TYPE_CHECKING

from ..door import lazy

if TYPE_CHECKING:  # the door (core/door.py): each name is imported from its module when first taken
    from .access import (
        best_scene, framed, look_at, Opportunity, plan_access, PlanOptions, PlanResult, PlanTarget, reach_of, satrec_of,
    )
    from .catalogs import load_satellites, load_sensor_modes
    from .cover import cover_plan
    from .modes import sample_sensor_modes, with_modes
    from .moving import (
        DEFAULT_UNCERTAINTY, guess_speed_unit, guess_target_fields, MAX_HORIZON, MAX_SHIPS, places_layers, plan_ships, Report,
        ShipState, SPEED_UNITS, targets_of, UncertaintyModel,
    )
    from .names import best_named, load_satellite_names, names_satellite, NO_NAMES, plain, SatelliteNames
    from .orbit import ecef_of, Frame, frame_of, lon_lat_of, norm, Orbit, point_at, R, sun_angles, track_angles, track_rate, unit
    from .outages import (
        attach, blocking, check_outage, delete_outages, DIRECTIONS, handed_hit, leave_out, load_outages, new_id, on_day, ONGOING,
        OUTAGE, Outage, outage_reason, OUTAGE_REASONS, OUTAGE_WARNING, OVERDUE, PLANNED, RECOVERED, save_outage, stopped,
        summary_text, SYSTEM_OUTAGE, SYSTEM_STOP, SYSTEM_WARNING, system_limits, takes_systems, warning_of, when_text,
    )
    from .output import (
        cover_text, cover_title, describe_problems, first_only, is_old_tle, js_round, LayerField, local_text, mode_facts,
        natural_key, NO_OPPORTUNITY, percent, plan_csv, plan_features, PlanLayer, side_text, sorted_ops, split_antimeridian,
        summary_all, summary_cover,
    )
    from .pairs import repeat_pairs, RepeatOptions, stereo_pairs, StereoOptions
    from .remind import due_reminders, merge_reminders, new_reminders, plan_ics, reminder_of, reminder_text
    from .revisit import box_of, cell_targets, grid_cells, hours_text, revisit_color, revisit_csv, revisit_layer, revisit_stats
    from .satellites import (
        BUILT_IN_DEFAULTS, platform_key, sample_satellites, satellite_label, satellites_from_text, SatelliteSpec, series_of,
    )
    from .scene import pass_shapes, shift_scene, track_place
    from .targets import in_polygon, MAX_POINTS, MAX_TARGETS, name_of, priority_of, spread_out, target_of
    from .timeline import bar_at, HEAD, Layout, layout, ROW
    from .track import MAX_DAYS, sample_at, satellite_track, TRACK_COLORS, track_layers, track_rows, track_shapes, track_ticks
    from .tree import facts_of, Group, is_old, label_of, Listed, SatelliteChoice, source_text, tle_age_days
    from .weather import (
        apply_clouds, cloud_at, CloudForecastConfig, CloudForecasts, CloudSeries, DEFAULT_CLOUD_FORECAST, fetch_clouds,
        FORECAST_DAYS, GRID, key_of, PAST_DAYS, places_for,
    )
    from .weather_file import forecast_file, read_grid
    from .output import OLD_TLE
    from .satellites import SAMPLE_SOURCE

__getattr__, __dir__ = lazy(__name__)

__all__ = [
    "apply_clouds", "attach", "bar_at", "best_named", "best_scene", "blocking", "box_of", "BUILT_IN_DEFAULTS", "cell_targets",
    "check_outage", "cloud_at", "CloudForecastConfig", "CloudForecasts", "CloudSeries", "cover_plan", "cover_text",
    "cover_title", "DEFAULT_CLOUD_FORECAST", "DEFAULT_UNCERTAINTY", "delete_outages", "describe_problems", "DIRECTIONS",
    "due_reminders", "ecef_of", "facts_of", "fetch_clouds", "first_only", "FORECAST_DAYS", "forecast_file", "Frame",
    "frame_of", "framed", "GRID", "grid_cells", "Group", "guess_speed_unit", "guess_target_fields", "handed_hit", "HEAD",
    "hours_text", "in_polygon", "is_old", "is_old_tle", "js_round", "key_of", "label_of", "LayerField", "Layout", "layout",
    "leave_out", "Listed", "load_outages", "load_satellite_names", "load_satellites", "load_sensor_modes", "local_text",
    "lon_lat_of", "look_at", "MAX_DAYS", "MAX_HORIZON", "MAX_POINTS", "MAX_SHIPS", "MAX_TARGETS", "merge_reminders",
    "mode_facts", "name_of", "names_satellite", "natural_key", "new_id", "new_reminders", "NO_NAMES", "NO_OPPORTUNITY", "norm",
    "on_day", "ONGOING", "Opportunity", "Orbit", "OUTAGE", "Outage", "outage_reason", "OUTAGE_REASONS", "OUTAGE_WARNING", "OVERDUE",
    "pass_shapes", "PAST_DAYS", "percent", "places_for", "places_layers", "plain", "plan_access", "plan_csv", "plan_features",
    "plan_ics", "plan_ships", "PlanLayer", "PLANNED", "PlanOptions", "PlanResult", "PlanTarget", "platform_key", "point_at",
    "priority_of", "R", "reach_of", "read_grid", "RECOVERED", "reminder_of", "reminder_text", "repeat_pairs", "RepeatOptions",
    "Report", "revisit_color", "revisit_csv", "revisit_layer", "revisit_stats", "ROW", "sample_at", "sample_satellites",
    "sample_sensor_modes", "satellite_label", "satellite_track", "SatelliteChoice", "SatelliteNames", "satellites_from_text", "series_of",
    "SatelliteSpec", "satrec_of", "save_outage", "shift_scene", "ShipState", "side_text", "sorted_ops", "source_text",
    "SPEED_UNITS", "split_antimeridian", "spread_out", "stereo_pairs", "StereoOptions", "stopped", "summary_all",
    "summary_cover", "summary_text", "sun_angles", "SYSTEM_OUTAGE", "SYSTEM_STOP", "SYSTEM_WARNING", "system_limits",
    "takes_systems", "target_of", "targets_of", "tle_age_days", "track_angles", "TRACK_COLORS",
    "track_layers", "track_place", "track_rate", "track_rows", "track_shapes", "track_ticks", "UncertaintyModel", "unit",
    "warning_of", "when_text", "with_modes",
    "OLD_TLE", "SAMPLE_SOURCE",
]
