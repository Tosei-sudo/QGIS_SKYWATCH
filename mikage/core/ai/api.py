"""
AI's door (窓口) for the rest of the plugin (docs/architecture.md R2): what the other
features, the screens and the plugin import of it. Its modules import each other directly;
everything outside imports through here, so its inside can be rearranged behind it.
"""
from __future__ import annotations

from typing import TYPE_CHECKING

from ..door import lazy

if TYPE_CHECKING:  # the door (core/door.py): each name is imported from its module when first taken
    from .annotation_boxes import (
        Box, convert, ConvertError, dota_row, is_point_box, mask_shape, parse_lengths, Rules, Shape, threshold_mask, UNKNOWN_MODES,
        WIDTH_MODES, window_at, window_side,
    )
    from .detect import (
        band_picture, blocks_of, box_corners, class_filter, class_name, crop_of, detect_tile, Detection, is_empty, nms, offset,
        tiles_of, wanted,
    )
    from .mask_outline import mask_outline, ring_area
    from .pixel_boxes import box_properties, collection, feature, METRE_DIGITS, PixelBox as PixelRect, turned_box, upright_box
    from .model_config import DetectModelConfig, detector_spec, DetectorSpec, OnnxSignature, OnnxValue, SamModelConfig
    from .review import (
        ACCEPTED, is_rejection, queue, reject_margin, REJECT_POINT_M, REJECTED_STATE, rejected_tags, review_order,
        state_of, SURE_SCORE, TO_REVIEW,
    )
    from .sam import part_at, sam_hwc, sam_mask, SAM_MEAN, sam_prompt, sam_scale, SAM_STD, sam_tensor, SamPoint
    from .train.dataset import (
        add_group, count_classes, Dataset, DatasetError, Group, Item, load_dataset, make_group, missing_files, parse_class_map,
        PixelBox, Reader, save_dataset,
    )
    from .train.labels import class_of, class_order, footprint, kind_path, METRES_PER_DEGREE, object_size
    from .train.trainer import (
        add_classes, band_statistics, history_entry, InputSpec, load_model, new_model, normalize, split, TILE_SIZES, train,
        TrainError, TrainOptions, Window,
    )
    from .transfer import (
        action_of, BATCH, bearing_text, DetectionOutput, missing_fields, Plan, signature_of, status_of, Target, target_of,
        transfer_attributes, TRANSFER_ROLES,
    )
    from .annotation_boxes import length_for, _positive as positive_or_none

__getattr__, __dir__ = lazy(__name__)

__all__ = [
    "ACCEPTED", "action_of", "add_classes", "add_group", "band_picture", "band_statistics", "BATCH", "bearing_text",
    "blocks_of", "Box", "box_corners", "class_filter", "class_name", "class_of", "class_order", "convert", "ConvertError",
    "count_classes", "crop_of", "Dataset", "DatasetError", "detect_tile", "Detection", "DetectionOutput", "DetectModelConfig",
    "detector_spec", "DetectorSpec", "dota_row", "footprint", "Group", "history_entry", "InputSpec", "is_empty",
    "is_point_box", "is_rejection", "Item", "kind_path", "load_dataset", "load_model", "make_group", "mask_outline",
    "mask_shape", "METRES_PER_DEGREE", "missing_fields", "missing_files", "new_model", "nms", "normalize", "object_size",
    "offset", "OnnxSignature", "OnnxValue", "parse_class_map", "parse_lengths", "part_at", "PixelBox", "Plan", "queue",
    "Reader", "reject_margin", "REJECT_POINT_M", "REJECTED_STATE", "rejected_tags", "review_order",
    "ring_area", "Rules", "sam_hwc", "sam_mask", "SAM_MEAN", "sam_prompt", "sam_scale", "SAM_STD", "sam_tensor",
    "SamModelConfig", "SamPoint", "save_dataset", "Shape", "signature_of", "split", "state_of", "status_of", "SURE_SCORE",
    "Target", "target_of", "threshold_mask", "TILE_SIZES", "tiles_of", "TO_REVIEW", "train", "TrainError", "TrainOptions",
    "transfer_attributes", "TRANSFER_ROLES", "UNKNOWN_MODES", "wanted", "WIDTH_MODES", "Window", "window_at", "window_side",
    "length_for", "positive_or_none",
    "box_properties", "collection", "feature", "METRE_DIGITS", "PixelRect", "turned_box", "upright_box",
]
