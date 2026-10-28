"""
Image corrections, ported from browser-image-enhancement (the viewer's 補正):
the same steps, parameters, presets and saved form (`{"version": 1, "ops": [...]}`),
computed with NumPy on 8-bit RGBA arrays, and the raw-value stretch of 16-bit
and float rasters (raster.py). Nothing here imports QGIS.
"""
from .ops import OP_INFO, normalize_op, is_identity, margin_of  # noqa: F401
from .pipeline import Pipeline, PRESETS, PRESET_NAMES  # noqa: F401
from .process import process  # noqa: F401
