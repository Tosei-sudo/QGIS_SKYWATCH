"""
「スペクトル」 (the viewer's spectral.ts): with the dock open, a click on
the map adds the value of every band of the active image there as a line
over the bands (範囲 mode: a drag adds the mean over the box, with its range
behind); up to 8 lines overlay, numbered on the map. The values are read
with GDAL from the file itself, not the corrected display; no data (the
file's and the layer's own) is left out. 「CSV 保存」 writes them.
"""
from __future__ import annotations

import math
import os
from typing import Callable, Optional

from qgis.core import QgsCoordinateTransform, QgsPointXY, QgsProject, QgsProviderRegistry, QgsRasterLayer, QgsRectangle
from qgis.gui import QgsMapCanvas, QgsMapCanvasItem, QgsMapTool, QgsRubberBand
from qgis.PyQt.QtCore import QCoreApplication, QPointF, QRectF, Qt
from qgis.PyQt.QtGui import QColor, QFont, QPainter, QPainterPath, QPen, QPolygonF
from qgis.PyQt.QtWidgets import QComboBox, QFileDialog, QHBoxLayout, QLabel, QPushButton, QSizePolicy, QToolTip, QWidget

from ..core.spectral import (
    COLORS, MAX_SERIES, Spectrum, axis_range, band_labels, band_stats, box_label, format_value, pixel_window, read_size, spectral_csv,
)
from ..qgis_io.geometry import from_wgs84, to_wgs84
from ..qgis_io.obs_images import file_name
from .docks import PluginDock

try:  # Qt5 / Qt6
    from qgis.core import QgsWkbTypes
    POLYGON = QgsWkbTypes.GeometryType.PolygonGeometry
except AttributeError:  # pragma: no cover
    from qgis.core import Qgis
    POLYGON = Qgis.GeometryType.Polygon

#: Padding of the graph (left, right, top, bottom).
PAD = (52, 10, 10, 32)


def _tr(text: str) -> str:
    return QCoreApplication.translate("SpectralDock", text)


class Image:
    """An image layer opened with GDAL to read its pixels: the dataset, its grid and no data."""

    def __init__(self, layer: QgsRasterLayer):
        from osgeo import gdal

        self.layer = layer
        path = QgsProviderRegistry.instance().decodeUri("gdal", layer.source()).get("path") or layer.source()
        gdal.PushErrorHandler("CPLQuietErrorHandler")
        try:
            self.dataset = gdal.Open(path)
        finally:
            gdal.PopErrorHandler()
        if self.dataset is None:
            raise ValueError(_tr("GDAL で開けない画像"))
        self.width, self.height = self.dataset.RasterXSize, self.dataset.RasterYSize
        self.count = self.dataset.RasterCount
        self.inverse = gdal.InvGeoTransform(self.dataset.GetGeoTransform())
        provider = layer.dataProvider()
        self.no_data = []
        for b in range(1, self.count + 1):
            values = set()
            own = self.dataset.GetRasterBand(b).GetNoDataValue()
            if own is not None:
                values.add(own)
            if provider.sourceHasNoDataValue(b) and provider.useSourceNoDataValue(b):
                values.add(provider.sourceNoDataValue(b))
            ranges = [(r.min(), r.max()) for r in provider.userNoDataValues(b)]
            self.no_data.append((values, ranges))
        self.names = [self.dataset.GetRasterBand(b).GetDescription() or None for b in range(1, self.count + 1)]

    def to_pixel(self, x: float, y: float) -> tuple[float, float]:
        """A point in the layer's CRS as fractional pixels (column, row)."""
        g = self.inverse
        return g[0] + g[1] * x + g[2] * y, g[3] + g[4] * x + g[5] * y

    def no_data_test(self, band: int):
        values, ranges = self.no_data[band]

        def test(a):
            import numpy as np

            mask = np.isin(a, list(values)) if values else np.zeros(a.shape, bool)
            for lo, hi in ranges:
                mask |= (a >= lo) & (a <= hi)
            return mask
        return test

    def read(self, c0: int, r0: int, c1: int, r1: int, out_w: Optional[int] = None, out_h: Optional[int] = None) -> list:
        """Every band over a window (at out_w × out_h, nearest neighbour)."""
        from osgeo import gdal

        return [self.dataset.GetRasterBand(b).ReadAsArray(c0, r0, c1 - c0, r1 - r0, buf_xsize=out_w or c1 - c0, buf_ysize=out_h or r1 - r0,
                                                           resample_alg=gdal.GRIORA_NearestNeighbour)
                for b in range(1, self.count + 1)]


class SpectralChart(QWidget):
    """The lines over the bands, a box's range behind its mean; hovering shows the band and every line's value."""

    def __init__(self, parent: Optional[QWidget] = None):
        super().__init__(parent)
        self.series: list[Spectrum] = []
        self.labels: list[str] = []
        self.setMinimumHeight(170)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        self.setMouseTracking(True)

    def set_data(self, series, labels) -> None:
        self.series, self.labels = list(series), list(labels)
        self.update()

    def _plot(self) -> QRectF:
        return QRectF(PAD[0], PAD[2], max(10, self.width() - PAD[0] - PAD[1]), max(10, self.height() - PAD[2] - PAD[3]))

    def _x(self, b: int, plot: QRectF) -> float:
        n = len(self.labels)
        return plot.left() + (plot.width() / 2 if n == 1 else plot.width() * b / (n - 1))

    def paintEvent(self, event) -> None:  # noqa: N802 (Qt's name)
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.fillRect(self.rect(), self.palette().base())
        axis = axis_range(self.series)
        n = len(self.labels)
        if axis is None or not n:
            return
        lo, hi, step = axis
        plot = self._plot()

        def y(v: float) -> float:
            return plot.top() + plot.height() * (hi - v) / (hi - lo)

        font = QFont(self.font())
        font.setPixelSize(10)
        painter.setFont(font)
        text_color = self.palette().text().color()
        grid = QColor(text_color)
        grid.setAlpha(40)
        v = lo
        while v <= hi + step / 2:
            painter.setPen(QPen(grid, 1))
            painter.drawLine(QPointF(plot.left(), y(v)), QPointF(plot.right(), y(v)))
            painter.setPen(text_color)
            painter.drawText(QRectF(0, y(v) - 7, PAD[0] - 4, 14), int(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter), format_value(float(f"{v:.10g}")))
            v += step
        every = math.ceil(n / 12)
        for b in range(0, n, every):
            painter.drawText(QRectF(self._x(b, plot) - 20, plot.bottom() + 3, 40, 14), int(Qt.AlignmentFlag.AlignCenter), f"B{b + 1}")
        painter.drawText(QRectF(0, self.height() - 14, self.width() - PAD[1], 14), int(Qt.AlignmentFlag.AlignRight), _tr("バンド"))
        for s in self.series:
            color = QColor(s.color)
            if s.pixels > 1:
                top = [(i, b.max) for i, b in enumerate(s.bands) if math.isfinite(b.max)]
                bottom = [(i, b.min) for i, b in enumerate(s.bands) if math.isfinite(b.min)][::-1]
                if len(top) > 1:
                    fill = QColor(color)
                    fill.setAlphaF(0.15)
                    painter.setPen(Qt.PenStyle.NoPen)
                    painter.setBrush(fill)
                    painter.drawPolygon(QPolygonF([QPointF(self._x(i, plot), y(v)) for i, v in top + bottom]))
            path = QPainterPath()
            started = False
            for i, b in enumerate(s.bands):
                if not math.isfinite(b.mean):
                    started = False
                    continue
                p = QPointF(self._x(i, plot), y(b.mean))
                path.lineTo(p) if started else path.moveTo(p)
                started = True
            painter.setPen(QPen(color, 2))
            painter.setBrush(Qt.BrushStyle.NoBrush)
            painter.drawPath(path)
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(color)
            for i, b in enumerate(s.bands):
                if math.isfinite(b.mean):
                    painter.drawEllipse(QPointF(self._x(i, plot), y(b.mean)), 2.5, 2.5)

    def band_at(self, x: float) -> Optional[int]:
        n = len(self.labels)
        if not n or not self.series:
            return None
        plot = self._plot()
        return 0 if n == 1 else min(n - 1, max(0, round((x - plot.left()) / plot.width() * (n - 1))))

    def mouseMoveEvent(self, event) -> None:  # noqa: N802
        pos = event.position() if hasattr(event, "position") else event.pos()
        b = self.band_at(pos.x())
        if b is None:
            return
        text = "\n".join([self.labels[b]] + [f"{s.number}: {format_value(s.bands[b].mean if b < len(s.bands) else math.nan)}" for s in self.series])
        QToolTip.showText(self.mapToGlobal(pos.toPoint()), text, self)


class SpectralMarks(QgsMapCanvasItem):
    """The points (numbered circles) and boxes of the lines, in WGS 84."""

    def __init__(self, canvas: QgsMapCanvas):
        super().__init__(canvas)
        self.canvas_ = canvas
        self.series: list[Spectrum] = []
        self.setZValue(1100)

    def changed(self) -> None:
        self.updatePosition()
        self.update()

    def updatePosition(self) -> None:  # noqa: N802 (QGIS's name)
        self.setPos(0, 0)
        self.prepareGeometryChange()

    def boundingRect(self) -> QRectF:  # noqa: N802
        size = self.canvas_.size()
        return QRectF(0, 0, size.width(), size.height())

    def _pixel(self, lon: float, lat: float) -> QPointF:
        try:
            return self.toCanvasCoordinates(from_wgs84(self.canvas_.mapSettings().destinationCrs()).transform(QgsPointXY(lon, lat)))
        except Exception:  # noqa: BLE001
            return QPointF(-1e6, -1e6)

    def paint(self, painter, option=None, widget=None) -> None:
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        font = QFont()
        font.setPixelSize(11)
        font.setBold(True)
        painter.setFont(font)
        for s in self.series:
            color = QColor(s.color)
            if len(s.where) == 4:
                lon0, lat0, lon1, lat1 = s.where
                ring = [self._pixel(lon0, lat0), self._pixel(lon1, lat0), self._pixel(lon1, lat1), self._pixel(lon0, lat1)]
                fill = QColor(color)
                fill.setAlpha(0x22)
                painter.setPen(QPen(color, 2))
                painter.setBrush(fill)
                painter.drawPolygon(QPolygonF(ring))
                c = QPointF(sum(p.x() for p in ring) / 4, sum(p.y() for p in ring) / 4)
                path = QPainterPath()
                path.addText(QPointF(c.x() - 4, c.y() + 4), font, str(s.number))
                painter.setPen(QPen(QColor("white"), 3))
                painter.setBrush(Qt.BrushStyle.NoBrush)
                painter.drawPath(path)
                painter.fillPath(path, color)
            elif len(s.where) == 2:
                p = self._pixel(*s.where)
                painter.setPen(QPen(QColor("white"), 2))
                painter.setBrush(color)
                painter.drawEllipse(p, 8, 8)
                painter.setPen(QColor("white"))
                painter.drawText(QRectF(p.x() - 8, p.y() - 8, 16, 16), int(Qt.AlignmentFlag.AlignCenter), str(s.number))


class SpectralTool(QgsMapTool):
    """A click adds a point; in 範囲 mode a drag adds a box (`box()` says which)."""

    def __init__(self, canvas, point: Callable, box: Callable, boxes: Callable[[], bool]):
        super().__init__(canvas)
        self._point, self._box, self._boxes = point, box, boxes
        self._start = None
        self.band = QgsRubberBand(canvas, POLYGON)
        self.band.setColor(QColor(62, 123, 250, 60))
        self.band.setStrokeColor(QColor("#3e7bfa"))
        self.band.setWidth(1)
        self.setCursor(Qt.CursorShape.CrossCursor)

    def canvasPressEvent(self, event) -> None:  # noqa: N802 (QGIS's name)
        if event.button() == Qt.MouseButton.LeftButton and self._boxes():
            self._start = event.mapPoint()

    def canvasMoveEvent(self, event) -> None:  # noqa: N802
        if self._start is not None:
            self.band.setToGeometry(_rect_geometry(QgsRectangle(self._start, event.mapPoint())), None)

    def canvasReleaseEvent(self, event) -> None:  # noqa: N802
        if event.button() != Qt.MouseButton.LeftButton:
            return
        if self._start is not None:
            rect = QgsRectangle(self._start, event.mapPoint())
            self._start = None
            self.band.reset(POLYGON)
            if rect.width() > 0 and rect.height() > 0:
                self._box(rect)
        elif not self._boxes():
            self._point(event.mapPoint())

    def deactivate(self) -> None:
        self._start = None
        self.band.reset(POLYGON)
        super().deactivate()


def _rect_geometry(rect: QgsRectangle):
    from qgis.core import QgsGeometry

    return QgsGeometry.fromRect(rect)


class SpectralDock(PluginDock):
    """The dock 「スペクトル」: the graph, the mode (点 or 範囲), the lines on the map while it is open."""

    def __init__(self, iface, context, open_settings: Callable[[], None], parent: Optional[QWidget] = None):
        super().__init__("SatObsSpectral", _tr("スペクトル"), context, open_settings, parent)
        self.iface = iface
        self.canvas: QgsMapCanvas = iface.mapCanvas()
        self.series: list[Spectrum] = []
        self.labels: list[str] = []
        self.next_number = 1
        self.image: Optional[Image] = None
        self.marks: Optional[SpectralMarks] = None
        self._previous_tool = None
        self.tool = SpectralTool(self.canvas, self.add_point, self.add_box, lambda: self.mode.currentData() == "box")

        top = QHBoxLayout()
        self.measure_button = QPushButton(_tr("取得"))
        self.measure_button.setCheckable(True)
        self.measure_button.setToolTip(_tr("地図のクリックで取得（ほかの道具に切り替えたあとに戻す）"))
        self.measure_button.toggled.connect(self._toggle_tool)
        self.mode = QComboBox()
        self.mode.addItem(_tr("クリックした点"), "point")
        self.mode.addItem(_tr("ドラッグした範囲の平均"), "box")
        self.mode.currentIndexChanged.connect(lambda _: self._update())
        self.clear_button = QPushButton(_tr("消去"))
        self.clear_button.clicked.connect(self.clear)
        self.save_button = QPushButton(_tr("CSV 保存"))
        self.save_button.clicked.connect(lambda: self.save_csv())
        for w in (self.measure_button, self.mode, self.clear_button, self.save_button):
            top.addWidget(w)
        top.addStretch(1)
        self.body.addLayout(top)
        self.image_label = QLabel()
        self.image_label.setWordWrap(True)
        self.body.addWidget(self.image_label)
        self.chart = SpectralChart()
        self.body.addWidget(self.chart, 1)
        self.legend = QLabel()
        self.legend.setWordWrap(True)
        self.legend.setTextFormat(Qt.TextFormat.RichText)
        self.body.addWidget(self.legend)
        self.note = QLabel()
        self.note.setWordWrap(True)
        self.body.addWidget(self.note)

        self.visibilityChanged.connect(self._shown)
        self.toggleViewAction().triggered.connect(lambda on: on and self.raise_())
        self.canvas.mapToolSet.connect(self._tool_set)
        iface.currentLayerChanged.connect(self._layer_changed)
        self._update()

    # The map tool: on while the dock is open, until another tool is chosen.

    def _shown(self, visible: bool) -> None:
        if visible:
            if self.marks is None:
                self.marks = SpectralMarks(self.canvas)
                self.marks.series = self.series
                self.marks.changed()
            self.measure_button.setChecked(True)
        else:
            self.measure_button.setChecked(False)
            if self.marks is not None:
                self.canvas.scene().removeItem(self.marks)
                self.marks = None

    def _toggle_tool(self, on: bool) -> None:
        if on and self.canvas.mapTool() is not self.tool:
            self._previous_tool = self.canvas.mapTool()
            self.canvas.setMapTool(self.tool)
        elif not on and self.canvas.mapTool() is self.tool:
            try:
                self.canvas.setMapTool(self._previous_tool) if self._previous_tool is not None else self.canvas.unsetMapTool(self.tool)
            except RuntimeError:
                self.canvas.unsetMapTool(self.tool)

    def _tool_set(self, new, old=None) -> None:
        if new is not self.tool and self.measure_button.isChecked():
            self.measure_button.blockSignals(True)
            self.measure_button.setChecked(False)
            self.measure_button.blockSignals(False)

    def _layer_changed(self, layer) -> None:
        """Follows the active layer: another image starts the graph afresh."""
        if self.image is not None and layer is not self.image.layer:
            self.image = None
            self.clear()

    # Reading.

    def _image(self) -> Optional[Image]:
        layer = self.iface.activeLayer()
        if not isinstance(layer, QgsRasterLayer) or not layer.isValid() or layer.providerType() != "gdal":
            return None
        if self.image is None or self.image.layer is not layer:
            self.image = Image(layer)
            self.labels = band_labels(self.image.count, self.image.names)
        return self.image

    def _to_layer(self, layer: QgsRasterLayer) -> QgsCoordinateTransform:
        return QgsCoordinateTransform(self.canvas.mapSettings().destinationCrs(), layer.crs(), QgsProject.instance())

    def add_point(self, point) -> Optional[Spectrum]:
        """Adds the bands' values at a point (the canvas's CRS)."""
        def read(image: Image):
            p = self._to_layer(image.layer).transform(QgsPointXY(point))
            c, r = image.to_pixel(p.x(), p.y())
            col, row = math.floor(c), math.floor(r)
            if not (0 <= col < image.width and 0 <= row < image.height):
                return None, 0, ""
            return image.read(col, row, col + 1, row + 1), 1, _tr("点（列 {c}、行 {r}）").format(c=col, r=row)

        w = to_wgs84(self.canvas.mapSettings().destinationCrs()).transform(QgsPointXY(point))
        return self._add(read, (w.x(), w.y()))

    def add_box(self, rect: QgsRectangle) -> Optional[Spectrum]:
        """Adds the bands' means over a box (the canvas's CRS), read at a lower resolution when large."""
        def read(image: Image):
            to_layer = self._to_layer(image.layer)
            x0, y0, x1, y1 = rect.xMinimum(), rect.yMinimum(), rect.xMaximum(), rect.yMaximum()
            corners = [(x0, y0), (x1, y0), (x1, y1), (x0, y1), ((x0 + x1) / 2, y0), ((x0 + x1) / 2, y1), (x0, (y0 + y1) / 2), (x1, (y0 + y1) / 2)]
            pixels = [image.to_pixel(p.x(), p.y()) for p in (to_layer.transform(QgsPointXY(x, y)) for x, y in corners)]
            window = pixel_window([c for c, _ in pixels], [r for _, r in pixels], image.width, image.height)
            if window is None:
                return None, 0, ""
            c0, r0, c1, r1 = window
            out_w, out_h, thinned = read_size(c1 - c0, r1 - r0)
            return image.read(c0, r0, c1, r1, out_w, out_h), (c1 - c0) * (r1 - r0), box_label(c1 - c0, r1 - r0, thinned)

        t = to_wgs84(self.canvas.mapSettings().destinationCrs())
        a, b = t.transform(QgsPointXY(rect.xMinimum(), rect.yMinimum())), t.transform(QgsPointXY(rect.xMaximum(), rect.yMaximum()))
        return self._add(read, (a.x(), a.y(), b.x(), b.y()))

    def _add(self, read, where) -> Optional[Spectrum]:
        try:
            image = self._image()
        except ValueError as e:
            self.say(str(e))
            return None
        if image is None:
            self.say(_tr("画像のレイヤーを選択"))
            return None
        bands, pixels, label = read(image)
        if bands is None:
            self.say(_tr("その地点に画像なし"))
            return None
        stats = [band_stats([a], image.no_data_test(i))[0] for i, a in enumerate(bands)]
        if all(s.count == 0 for s in stats):
            self.say(_tr("その地点はノーデータ"))
            return None
        number = self.next_number
        self.next_number += 1
        s = Spectrum(number, label, COLORS[(number - 1) % len(COLORS)], stats, pixels, where)
        self.series.append(s)
        while len(self.series) > MAX_SERIES:
            self.series.pop(0)
        self._update()
        return s

    def clear(self) -> None:
        self.series.clear()
        self.next_number = 1
        self._update()

    def csv_text(self) -> str:
        return spectral_csv(self.series, self.labels)

    def save_csv(self, path: Optional[str] = None) -> Optional[str]:
        """Writes the lines as CSV (UTF-8 with BOM, as the viewer); asks where when `path` is not given."""
        if not self.series:
            return None
        if path is None:
            base = os.path.splitext(file_name(self.image.layer))[0] if self.image else "spectrum"
            path, _ = QFileDialog.getSaveFileName(self, _tr("CSV 保存"), f"{base}_spectrum.csv", "CSV (*.csv)")
            if not path:
                return None
        with open(path, "w", encoding="utf-8-sig", newline="") as f:
            f.write(self.csv_text())
        return path

    def _update(self) -> None:
        self.chart.set_data(self.series, self.labels)
        if self.marks is not None:
            self.marks.series = self.series
            self.marks.changed()
        self.clear_button.setEnabled(bool(self.series))
        self.save_button.setEnabled(bool(self.series))
        self.legend.setText("　".join(f'<span style="color:{s.color}">●</span> {s.number}. {s.label}' for s in self.series))
        self.image_label.setText(_tr("画像: {name}").format(name=file_name(self.image.layer)) if self.image else "")
        if self.series:
            self.note.setText("")
        else:
            self.note.setText(_tr("地図上をドラッグすると、その範囲の各バンドの平均（と最小〜最大の幅）を表示") if self.mode.currentData() == "box"
                              else _tr("地図をクリックすると、その点の各バンドの値を表示。複数を重ねて比較可能"))

    def say(self, text: str) -> None:
        self.note.setText(text)

    def close_down(self) -> None:
        self.measure_button.setChecked(False)
        for signal, slot in ((self.canvas.mapToolSet, self._tool_set), (self.iface.currentLayerChanged, self._layer_changed)):
            try:
                signal.disconnect(slot)
            except (TypeError, RuntimeError):
                pass
        if self.marks is not None:
            self.canvas.scene().removeItem(self.marks)
            self.marks = None
        self.tool.band.reset(POLYGON)
