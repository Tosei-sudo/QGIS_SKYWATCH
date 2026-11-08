"""
The settings file's `complexImages`: how a complex SAR image (SICD, whose
NITF holds the real part I and the imaginary part Q as two bands, or a
raster of complex pixels such as CInt16 / CFloat32) is shown. QGIS alone
draws I or Q as they are, which says nothing; the plugin shows the
amplitude sqrt(I² + Q²) instead, in decibels by default.

    "complexImages": "dB"          // 20·log10(amplitude) (default)
    "complexImages": "amplitude"   // sqrt(I² + Q²)
    "complexImages": false         // left as QGIS opens it (I and Q as bands)
"""
from __future__ import annotations

from typing import Any, Optional

#: What a complex image is shown as.
MODES = ("dB", "amplitude")
DEFAULT_MODE = "dB"


def complex_images_of(value: Any, problems: list[str]) -> Optional[str]:
    """The `complexImages` entry: a mode of MODES, None for false (left as it is); the default with the reason in `problems` otherwise."""
    if value is False:
        return None
    if value is True:
        return DEFAULT_MODE
    if isinstance(value, str):
        for mode in MODES:
            if value.strip().lower() == mode.lower():
                return mode
    problems.append(f"complexImages は {'・'.join(repr(m) for m in MODES)} か false で指定")
    return DEFAULT_MODE
