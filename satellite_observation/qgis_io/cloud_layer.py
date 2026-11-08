"""
「雲量マップ」's layer: a GeoTIFF of cloud cover with one band per forecast
time (docs/cloud-forecast-file.md) shown one band at a time, the band
chosen by the dock's slider or by QGIS's temporal controller.

The service's forecast (Open-Meteo, points only) is asked for a grid over
the map and written as such a file first (`write_service_file`), so both
sources are shown alike.

QGIS 3.34 cannot give a raster's bands times of their own (3.38 can), so
the layer's renderer is switched to the band of the time shown.
"""
from __future__ import annotations

import math
import os
import tempfile
import time
from dataclasses import dataclass, field
from typing import Optional

from qgis.core import (
    Qgis, QgsColorRampShader, QgsDateTimeRange, QgsRasterLayer, QgsRasterShader, QgsSingleBandPseudoColorRenderer,
)
from qgis.PyQt.QtCore import QDateTime, QTimeZone
from qgis.PyQt.QtGui import QColor

from ..core.plan.weather import CloudForecasts, key_of
from ..core.plan.weather_file import read_grid
from ..core.util import iso_text

#: The layer's custom property marking it as the plugin's cloud map.
PROPERTY = "satobs/cloud_map"
NODATA = 255
#: The looks: name → (value, colour, alpha) stops of the colour ramp.
STYLES = {
    "white": [(0, "#ffffff", 0), (20, "#ffffff", 40), (60, "#f8f9fa", 150), (100, "#e9ecef", 235)],
    "color": [(0, "#2b8a3e", 70), (30, "#94d82d", 160), (60, "#fab005", 175), (100, "#e03131", 195)],
}
STYLE_LABELS = {"white": "白（雲の濃さ）", "color": "色分け（緑＝少・赤＝多）"}


@dataclass
class CloudFile:
    """A cloud cover file as shown: its bands in time order."""
    path: str
    #: (time ms, band number from 1) in time order.
    bands: list = field(default_factory=list)
    issued: float = math.nan

    @property
    def times(self) -> list:
        return [t for t, _ in self.bands]

    def nearest(self, ms: float) -> int:
        """The index (in time order) of the band nearest to a time."""
        if not self.bands:
            return 0
        return min(range(len(self.bands)), key=lambda i: abs(self.bands[i][0] - ms))


def cloud_file(path: str) -> CloudFile:
    """The bands of a cloud cover file in time order (bands without a time left out); raises with the reason."""
    grid = read_grid(path)
    bands = sorted((t, i + 1) for i, t in enumerate(grid.times) if math.isfinite(t))
    return CloudFile(path, bands, grid.issued)


def service_file_path(folder: Optional[str] = None) -> str:
    """Where a map of the service's forecast is written: a new file in the temp folder each time (an open one stays locked on Windows)."""
    folder = folder or os.path.join(tempfile.gettempdir(), "satobs_cloud")
    os.makedirs(folder, exist_ok=True)
    return os.path.join(folder, f"service_cloud_{time.strftime('%Y%m%d_%H%M%S')}_{os.getpid()}.tif")


def write_service_file(path: str, step: float, lons: list, lats: list, forecasts: CloudForecasts) -> CloudFile:
    """
    The service's forecasts of a grid of places (lons west to east, lats north
    to south, `step` degrees apart) as a cloud cover file of the spec
    (Byte 0–100, no data 255, a band per hour with VALID_TIME).
    """
    from osgeo import gdal, osr

    times = sorted({t for s in forecasts.values() for t in s.times})
    if not times:
        raise ValueError("雲量予報の値なし")
    lookup = {}
    for key, s in forecasts.items():
        lookup[key] = dict(zip(s.times, s.cover))
    if os.path.exists(path):
        gdal.GetDriverByName("GTiff").Delete(path)
    ds = gdal.GetDriverByName("GTiff").Create(path, len(lons), len(lats), len(times), gdal.GDT_Byte,
                                               ["COMPRESS=DEFLATE", "INTERLEAVE=BAND"])
    if ds is None:
        raise ValueError(f"雲量のファイルを作成できない: {path}")
    ds.SetGeoTransform((lons[0] - step / 2, step, 0, lats[0] + step / 2, 0, -step))
    srs = osr.SpatialReference()
    srs.ImportFromEPSG(4326)
    ds.SetProjection(srs.ExportToWkt())
    if math.isfinite(getattr(forecasts, "issued", math.nan)):
        ds.SetMetadataItem("INIT_TIME", iso_text(forecasts.issued))
    cells = [[lookup.get(key_of((lon, lat))) for lon in lons] for lat in lats]
    for b, t in enumerate(times):
        band = ds.GetRasterBand(b + 1)
        band.SetNoDataValue(NODATA)
        band.SetMetadataItem("VALID_TIME", iso_text(t))
        band.SetDescription(iso_text(t))
        rows = []
        for row in cells:
            for values in row:
                v = values.get(t) if values else None
                rows.append(NODATA if v is None or not math.isfinite(v) else max(0, min(100, int(round(v)))))
        band.WriteRaster(0, 0, len(lons), len(lats), bytes(rows))
    ds.FlushCache()
    ds = None
    return cloud_file(path)


def renderer(layer: QgsRasterLayer, band: int, style: str = "white") -> QgsSingleBandPseudoColorRenderer:
    """One band coloured by its cover (0–100 %)."""
    ramp = QgsColorRampShader(0, 100)
    ramp.setColorRampType(QgsColorRampShader.Type.Interpolated)
    items = []
    for value, color, alpha in STYLES.get(style, STYLES["white"]):
        c = QColor(color)
        c.setAlpha(alpha)
        items.append(QgsColorRampShader.ColorRampItem(value, c, f"{value} %"))
    ramp.setColorRampItemList(items)
    shader = QgsRasterShader(0, 100)
    shader.setRasterShaderFunction(ramp)
    out = QgsSingleBandPseudoColorRenderer(layer.dataProvider(), band, shader)
    out.setClassificationMin(0)
    out.setClassificationMax(100)
    return out


def show_band(layer: QgsRasterLayer, band: int, style: str = "white") -> None:
    """The layer drawn from one band."""
    layer.setRenderer(renderer(layer, band, style))
    layer.triggerRepaint()


def _qtime(ms: float) -> QDateTime:
    return QDateTime.fromMSecsSinceEpoch(int(ms), QTimeZone.utc())


def set_time_range(layer: QgsRasterLayer, file: CloudFile) -> None:
    """
    With QGIS's temporal controller on, the layer shows only within the
    forecast's times (an hour past the last); without it, always.
    """
    if not file.bands:
        return
    props = layer.temporalProperties()
    try:
        props.setMode(Qgis.RasterTemporalMode.FixedTemporalRange)
    except AttributeError:  # older QGIS
        return
    props.setFixedTemporalRange(QgsDateTimeRange(_qtime(file.times[0]), _qtime(file.times[-1] + 3600000)))
    props.setIsActive(True)


def open_layer(file: CloudFile, name: str, style: str = "white", index: int = 0) -> QgsRasterLayer:
    """The cloud cover file as a layer (not yet in the project), drawn from the band at `index` (time order)."""
    layer = QgsRasterLayer(file.path, name, "gdal")
    if not layer.isValid():
        raise ValueError(f"雲量のファイルを開けない: {os.path.basename(file.path)}")
    layer.setCustomProperty(PROPERTY, file.path)
    if file.bands:
        show_band(layer, file.bands[max(0, min(index, len(file.bands) - 1))][1], style)
    set_time_range(layer, file)
    return layer
