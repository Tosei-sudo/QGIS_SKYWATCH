"""
画像補正's door (窓口) for the rest of the plugin (docs/architecture.md R2): what the other
features, the screens and the plugin import of it. Its modules import each other directly;
everything outside imports through here, so its inside can be rearranged behind it.
"""
from __future__ import annotations

from .color_classes import (
    auto_label, class_palette, class_range, class_shares, color_classes, format_class_value, recolor, resize_classes,
    set_bound,
)
from .color_scale import apply_palette, color_scale_palette, COLOR_SCALES
from .histogram import bar_heights, channel_bins, channel_stats
from .ops import curve_function, is_identity_curve, normalize_curve, OP_INFO
from .pansharpen import fit_model, METHODS, pan_sharpen, PanSharpenModel, valid_pixels
from .pipeline import Pipeline, PRESET_NAMES, PRESETS
from .process import channel_tables, process
from .raster import (
    code_table, compute_raster_stretch, look_up, RasterHistogram, sample_raster_histogram, stretch_to_rgba, to_codes,
    valid_mask,
)
from .rules import image_rule_for, resolve_bands
from .stats import count_pixels, resolve_ops

__all__ = [
    "apply_palette", "auto_label", "bar_heights", "channel_bins", "channel_stats", "channel_tables", "class_palette",
    "class_range", "class_shares", "code_table", "color_classes", "color_scale_palette", "COLOR_SCALES",
    "compute_raster_stretch", "count_pixels", "curve_function", "fit_model", "format_class_value", "image_rule_for",
    "is_identity_curve", "look_up", "METHODS", "normalize_curve", "OP_INFO", "pan_sharpen", "PanSharpenModel", "Pipeline",
    "PRESET_NAMES", "PRESETS", "process", "RasterHistogram", "recolor", "resize_classes", "resolve_bands", "resolve_ops",
    "sample_raster_histogram", "set_bound", "stretch_to_rgba", "to_codes", "valid_mask", "valid_pixels",
]
