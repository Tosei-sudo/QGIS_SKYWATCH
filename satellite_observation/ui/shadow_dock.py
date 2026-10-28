"""
「影から高さ」 (the viewer's shadow-height.ts): with the dock open, click
the foot of a building, a tower or a tree, then the tip of its shadow; the
height is the shadow's length times the tangent of the Sun's elevation
(core.shadow).

The Sun is read for the image under the foot (the active raster layer, else
the top visible GDAL image there): its metadata, else when it was taken
(qgis_io.obs_images) and where the foot is, else as typed in the dock. With
an azimuth, a guide shows which way the shadow falls and the second click
snaps onto it.

The heights are drawn over the map (nothing is added to the project) until
「レイヤーとして追加」 makes them a scratch line layer.
"""
from __future__ import annotations

from typing import Callable, Optional

from qgis.core import (
    QgsCoordinateReferenceSystem, QgsDistanceArea, QgsFeature, QgsField, QgsGeometry, QgsPointXY, QgsProject,
    QgsRasterLayer, QgsVectorLayer,
)
from qgis.gui import QgsMapCanvas, QgsMapCanvasItem, QgsMapTool
from qgis.PyQt.QtCore import QCoreApplication, QDateTime, QPointF, QRectF, Qt, QTimeZone, QVariant
from qgis.PyQt.QtGui import QBrush, QColor, QFont, QFontMetricsF, QPainterPath, QPen
from qgis.PyQt.QtWidgets import (
    QCheckBox, QDateTimeEdit, QDoubleSpinBox, QFormLayout, QHBoxLayout, QLabel, QListWidget, QPushButton, QWidget,
)

from ..core.obs.image_time import ORIGINS
from ..core.plan.orbit import sun_angles
from ..core.shadow import (
    ShadowMeasurement, Sun, format_metres, height_from_shadow, shadow_azimuth, snap_to_ray, step_toward, sun_from_metadata,
)
from ..qgis_io.context import PluginContext
from ..qgis_io.geometry import from_wgs84, to_wgs84
from ..qgis_io.obs_images import file_name, gdal_metadata, observed_image
from .docks import PluginDock

WGS84 = QgsCoordinateReferenceSystem("EPSG:4326")
COLOR = QColor("#ff8a00")
HALO = QColor(0, 0, 0, 0xaa)
#: Pixels of the guide line on the screen.
GUIDE = 80


def _utc(ms: int) -> QDateTime:
    return QDateTime.fromMSecsSinceEpoch(int(ms), QTimeZone.utc())


def _utc_text(ms: int) -> str:
    return _utc(ms).toString("yyyy-MM-dd HH:mm:ss")


class ShadowOverlay(QgsMapCanvasItem):
    """The heights measured, the line to the pointer while measuring and the shadow's direction, in WGS 84."""

    def __init__(self, canvas: QgsMapCanvas):
        super().__init__(canvas)
        self.canvas_ = canvas
        #: (foot, tip, label) of the heights measured.
        self.lines: list[tuple] = []
        #: The line to the pointer and its label, and the guide (foot, end) while measuring.
        self.cursor: Optional[tuple] = None
        self.guide: Optional[tuple] = None
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

    def to_pixel(self, lon_lat) -> QPointF:
        try:
            p = from_wgs84(self.canvas_.mapSettings().destinationCrs()).transform(QgsPointXY(lon_lat[0], lon_lat[1]))
        except Exception:  # noqa: BLE001 (a place the CRS cannot show)
            return QPointF(-1e6, -1e6)
        return self.toCanvasCoordinates(p)

    def paint(self, painter, option=None, widget=None) -> None:
        painter.setRenderHint(painter.RenderHint.Antialiasing)
        if self.guide:
            a, b = (self.to_pixel(p) for p in self.guide)
            painter.setPen(QPen(HALO, 3.5))
            painter.drawLine(a, b)
            pen = QPen(QColor("white"), 1.5)
            pen.setDashPattern([5, 5])
            painter.setPen(pen)
            painter.drawLine(a, b)
        for foot, tip, label in self.lines:
            self._line(painter, foot, tip, label, True)
        if self.cursor:
            self._line(painter, *self.cursor, False)

    def _line(self, painter, foot, tip, label: str, done: bool) -> None:
        a, b = self.to_pixel(foot), self.to_pixel(tip)
        painter.setBrush(Qt.BrushStyle.NoBrush)
        painter.setPen(QPen(HALO, 5))
        painter.drawLine(a, b)
        pen = QPen(COLOR, 2.5)
        if not done:
            pen.setDashPattern([3, 2.4])
        painter.setPen(pen)
        painter.drawLine(a, b)
        painter.setPen(QPen(HALO, 1.5))
        painter.setBrush(QBrush(COLOR))
        painter.drawEllipse(a, 4, 4)
        painter.setBrush(QBrush(QColor("white")))
        painter.drawEllipse(b, 3, 3)
        if label:
            font = QFont()
            font.setPixelSize(13)
            font.setBold(True)
            metrics = QFontMetricsF(font)
            w = metrics.horizontalAdvance(label)
            box = QRectF(a.x() - w / 2 - 5, a.y() - 16 - metrics.height() / 2 - 2, w + 10, metrics.height() + 4)
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(QBrush(QColor(0, 0, 0, 0xbb)))
            painter.drawRect(box)
            path = QPainterPath()
            path.addText(QPointF(box.x() + 5, box.y() + 2 + metrics.ascent()), font, label)
            painter.fillPath(path, QColor("white"))


class ShadowTool(QgsMapTool):
    """Clicks on the map for the dock: `click(point)` with the point in the canvas's CRS, `move(point)`, `cancel()` on Esc."""

    def __init__(self, canvas, click: Callable, move: Callable, cancel: Callable):
        super().__init__(canvas)
        self._click, self._move, self._cancel = click, move, cancel
        self.setCursor(Qt.CursorShape.CrossCursor)

    def canvasReleaseEvent(self, event) -> None:  # noqa: N802 (QGIS's name)
        if event.button() == Qt.MouseButton.LeftButton:
            self._click(event.mapPoint())

    def canvasMoveEvent(self, event) -> None:  # noqa: N802
        self._move(event.mapPoint())

    def keyPressEvent(self, event) -> None:  # noqa: N802
        if event.key() == Qt.Key.Key_Escape:
            self._cancel()
            event.accept()
        else:
            event.ignore()


class ShadowHeightDock(PluginDock):
    """The dock 「影から高さ」: the Sun, the heights measured, and the map tool while it is open."""

    def __init__(self, iface, context: PluginContext, open_settings: Callable[[], None], parent: Optional[QWidget] = None):
        super().__init__("SatObsShadowHeight", QCoreApplication.translate("ShadowHeightDock", "影から高さ"), context, open_settings, parent)
        self.iface = iface
        self.canvas: QgsMapCanvas = iface.mapCanvas()
        self.sun_: Optional[Sun] = None
        #: Where the Sun came from: 'metadata', 'time' or 'manual' ('' unknown).
        self.sun_origin = ""
        #: The time the Sun is computed from when not from the metadata: (ms, where it came from).
        self.when: Optional[tuple] = None
        #: The layer the Sun was read for.
        self.read_for: Optional[str] = None
        self.foot: Optional[tuple] = None
        self.measurements: list[ShadowMeasurement] = []
        self.overlay: Optional[ShadowOverlay] = None
        self.tool = ShadowTool(self.canvas, self.click, self.move, self.cancel)
        self._previous_tool = None
        self._setting = False
        self.distance = QgsDistanceArea()
        self.distance.setSourceCrs(WGS84, QgsProject.instance().transformContext())
        self.distance.setEllipsoid("EPSG:7030")

        hint = QLabel(self.tr("建物などの根元、影の先端の順にクリック（Esc で取り消し）"))
        hint.setWordWrap(True)
        self.body.addWidget(hint)
        self.measure_button = QPushButton(self.tr("計測"))
        self.measure_button.setCheckable(True)
        self.measure_button.setToolTip(self.tr("地図のクリックで計測（ほかの道具に切り替えたあとに戻す）"))
        self.measure_button.toggled.connect(self._toggle_tool)
        self.image_label = QLabel()
        self.image_label.setWordWrap(True)
        top = QHBoxLayout()
        top.addWidget(self.measure_button)
        top.addWidget(self.image_label, 1)
        self.body.addLayout(top)

        form = QFormLayout()
        self.elevation = QDoubleSpinBox()
        self.elevation.setRange(0, 90)
        self.elevation.setDecimals(2)
        self.elevation.setSuffix("°")
        self.elevation.setSpecialValueText(self.tr("不明"))
        self.azimuth = QDoubleSpinBox()
        self.azimuth.setRange(-1, 360)
        self.azimuth.setDecimals(2)
        self.azimuth.setSuffix("°")
        self.azimuth.setSpecialValueText(self.tr("不明"))
        self.azimuth.setToolTip(self.tr("太陽の方位（北から時計回り）"))
        self.elevation.editingFinished.connect(self._typed)
        self.azimuth.editingFinished.connect(self._typed)
        self.snap = QCheckBox(self.tr("影の向きに吸着"))
        self.snap.setChecked(True)
        self.snap.toggled.connect(lambda _: self._update_guide())
        form.addRow(self.tr("太陽高度"), self.elevation)
        form.addRow(self.tr("太陽の方位"), self.azimuth)
        form.addRow("", self.snap)
        self.time = QDateTimeEdit()
        self.time.setTimeSpec(Qt.TimeSpec.UTC)
        self.time.setDisplayFormat("yyyy-MM-dd HH:mm:ss")
        self.time.setCalendarPopup(True)
        self.time.setDateTime(QDateTime.currentDateTimeUtc())
        self.time.setToolTip(self.tr("撮影日時（UTC）。変えると太陽高度と方位を計算"))
        self.time.editingFinished.connect(self._time_typed)
        form.addRow(self.tr("撮影日時（UTC）"), self.time)
        self.body.addLayout(form)
        self.origin_label = QLabel()
        self.origin_label.setWordWrap(True)
        self.body.addWidget(self.origin_label)
        self.list = QListWidget()
        self.body.addWidget(self.list, 1)
        buttons = QHBoxLayout()
        self.clear_button = QPushButton(self.tr("消去"))
        self.clear_button.clicked.connect(self.clear)
        self.layer_button = QPushButton(self.tr("レイヤーとして追加"))
        self.layer_button.clicked.connect(self.add_layer)
        buttons.addWidget(self.clear_button)
        buttons.addWidget(self.layer_button)
        buttons.addStretch(1)
        self.body.addLayout(buttons)

        self.visibilityChanged.connect(self._shown)
        # Opened from the menu while tabbed with others: in front, so measuring can start.
        self.toggleViewAction().triggered.connect(lambda on: on and self.raise_())
        self.canvas.mapToolSet.connect(self._tool_set)
        self.canvas.extentsChanged.connect(self._update_guide)
        self._show_sun()
        self._update_list()

    # The map tool: on while the dock is open, until another tool is chosen.

    def _shown(self, visible: bool) -> None:
        if visible:
            if self.overlay is None:
                self.overlay = ShadowOverlay(self.canvas)
                self._redraw()
            self.measure_button.setChecked(True)
            self.prepare()
        else:
            self.measure_button.setChecked(False)
            self._drop_overlay()

    def _toggle_tool(self, on: bool) -> None:
        if on and self.canvas.mapTool() is not self.tool:
            self._previous_tool = self.canvas.mapTool()
            self.canvas.setMapTool(self.tool)
            self.prepare()
        elif not on and self.canvas.mapTool() is self.tool:
            self.drop_foot()
            if self._previous_tool is not None:
                try:
                    self.canvas.setMapTool(self._previous_tool)
                except RuntimeError:  # the tool went with its plugin
                    self.canvas.unsetMapTool(self.tool)
            else:
                self.canvas.unsetMapTool(self.tool)

    def _tool_set(self, new, old=None) -> None:
        if new is not self.tool and self.measure_button.isChecked():
            self.drop_foot()
            self.measure_button.blockSignals(True)
            self.measure_button.setChecked(False)
            self.measure_button.blockSignals(False)

    def _drop_overlay(self) -> None:
        if self.overlay is not None:
            self.canvas.scene().removeItem(self.overlay)
            self.overlay = None

    # The Sun.

    def image_layer(self, lon_lat=None) -> Optional[QgsRasterLayer]:
        """The image the Sun is read for: the active raster layer, else the top visible GDAL image at a place (the map's centre)."""
        active = self.iface.activeLayer()
        if isinstance(active, QgsRasterLayer) and active.isValid():
            return active
        if lon_lat is None:
            c = to_wgs84(self.canvas.mapSettings().destinationCrs()).transform(self.canvas.center())
            lon_lat = (c.x(), c.y())
        for layer in self.canvas.layers():
            if not isinstance(layer, QgsRasterLayer) or layer.providerType() != "gdal" or not layer.crs().isValid():
                continue
            try:
                p = from_wgs84(layer.crs()).transform(QgsPointXY(*lon_lat))
            except Exception:  # noqa: BLE001
                continue
            if layer.extent().contains(p):
                return layer
        return None

    def prepare(self, lon_lat=None) -> None:
        """Reads the Sun for the image (at a place): its metadata, else when it was taken."""
        layer = self.image_layer(lon_lat)
        layer_id = layer.id() if layer is not None else None
        if layer_id == self.read_for and (self.sun_ is not None or self.when is not None):
            return
        self.read_for = layer_id
        if self.sun_origin != "manual":
            self.sun_, self.sun_origin = None, ""
        self.when = None
        self.image_label.setText(self.tr("画像: {name}").format(name=file_name(layer)) if layer is not None else self.tr("画像なし"))
        if layer is not None:
            items = list(gdal_metadata(layer).items()) + list(gdal_metadata(layer, "IMD").items())
            sun = sun_from_metadata(items)
            if sun is not None:
                self.sun_, self.sun_origin = sun, "metadata"
            else:
                image = observed_image(layer)
                if image.time is not None:
                    self.when = (image.time, ORIGINS.get(image.origin, image.origin))
                    self._set_time(image.time)
                if self.sun_origin != "manual":
                    self.from_time(lon_lat)
        self._show_sun()

    def from_time(self, lon_lat=None) -> None:
        """The Sun at a place (the map's centre) at the time known, if any."""
        if self.when is None:
            self._show_sun()
            return
        if lon_lat is None:
            c = to_wgs84(self.canvas.mapSettings().destinationCrs()).transform(self.canvas.center())
            lon_lat = (c.x(), c.y())
        elevation, azimuth = sun_angles(lon_lat, self.when[0])
        self.sun_ = Sun(elevation, azimuth) if elevation > 0 else None
        self.sun_origin = "time"
        self._show_sun()
        if elevation <= 0:
            self.say(self.tr("その日時は太陽が地平線の下（太陽高度 {e}°）").format(e=f"{elevation:.1f}"))

    def sun(self) -> Optional[Sun]:
        """The Sun the heights are worked out with (None: not known yet)."""
        return self.sun_

    def set_sun(self, elevation: Optional[float], azimuth: Optional[float] = None) -> None:
        """Sets the Sun's angles by hand (elevation None: unknown)."""
        self.sun_ = Sun(float(elevation), None if azimuth is None else float(azimuth) % 360) if elevation and 0 < elevation <= 90 else None
        self.sun_origin = "manual" if self.sun_ else ""
        self._show_sun()

    def set_time(self, ms: int) -> None:
        """Computes the Sun from a time (UTC ms) typed for the image."""
        self.when = (int(ms), ORIGINS["manual"])
        self._set_time(ms)
        self.from_time(self.foot)

    def _set_time(self, ms: int) -> None:
        self.time.blockSignals(True)
        self.time.setDateTime(_utc(ms))
        self.time.blockSignals(False)

    def _typed(self) -> None:
        if self._setting:
            return
        az = self.azimuth.value()
        self.set_sun(self.elevation.value() or None, None if az < 0 else az)

    def _time_typed(self) -> None:
        self.set_time(self.time.dateTime().toMSecsSinceEpoch())

    def _show_sun(self) -> None:
        sun = self.sun_
        self._setting = True
        self.elevation.setValue(sun.elevation if sun else 0)
        self.azimuth.setValue(sun.azimuth if sun and sun.azimuth is not None else -1)
        self._setting = False
        if sun is None:
            text = self.tr("太陽高度が不明。入力するか、撮影日時を入力")
        elif self.sun_origin == "metadata":
            text = self.tr("画像のメタデータ（{item}）から").format(item=sun.item)
        elif self.sun_origin == "time" and self.when:
            text = self.tr("撮影日時 {t} UTC（{origin}）と位置から計算").format(t=_utc_text(self.when[0]), origin=self.when[1])
        else:
            text = self.tr("手入力")
        self.origin_label.setText(text)
        self.snap.setEnabled(bool(sun and sun.azimuth is not None))
        self._update_guide()

    # Measuring.

    def _lon_lat(self, point) -> tuple:
        p = to_wgs84(self.canvas.mapSettings().destinationCrs()).transform(QgsPointXY(point))
        return p.x(), p.y()

    def _map_point(self, lon_lat) -> QgsPointXY:
        return from_wgs84(self.canvas.mapSettings().destinationCrs()).transform(QgsPointXY(*lon_lat))

    def measure(self, foot, tip) -> Optional[ShadowMeasurement]:
        """Measures the shadow from `foot` to `tip` (lon, lat); None while the Sun is unknown."""
        if self.sun_ is None:
            return None
        shadow = self.distance.measureLine([QgsPointXY(*foot), QgsPointXY(*tip)])
        return ShadowMeasurement(tuple(foot), tuple(tip), shadow, height_from_shadow(shadow, self.sun_.elevation),
                                 self.sun_.elevation, self.sun_.azimuth)

    def direction(self) -> Optional[tuple]:
        """Which way shadows fall at the foot in the canvas's CRS, None when the azimuth is unknown."""
        if self.foot is None or self.sun_ is None or self.sun_.azimuth is None:
            return None
        a = self._map_point(self.foot)
        b = self._map_point(step_toward(self.foot, shadow_azimuth(self.sun_.azimuth), 10))
        return b.x() - a.x(), b.y() - a.y()

    def tip_for(self, point) -> tuple:
        """Where the second click goes (lon, lat): onto the shadow's direction when snapping."""
        direction = self.direction()
        if self.foot is None or direction is None or not self.snap.isChecked():
            return self._lon_lat(point)
        a = self._map_point(self.foot)
        x, y = snap_to_ray((a.x(), a.y()), direction, (point.x(), point.y()))
        return self._lon_lat(QgsPointXY(x, y))

    def click(self, point) -> Optional[ShadowMeasurement]:
        """A click on the map (canvas CRS): the foot, then the tip; the height measured at the tip."""
        at = self._lon_lat(point)
        if self.foot is None:
            self.prepare(at)
            # The Sun computed from the time at the foot itself.
            if self.sun_origin == "time" or (self.sun_ is None and self.when):
                self.from_time(at)
            if self.sun_ is None:
                self.say(self.tr("太陽高度が不明。ドックに太陽高度か撮影日時を入力"))
                return None
            self.foot = at
            self._update_guide()
            self.move(point)
            self.say(self.tr("影の先端をクリック（Esc で取り消し）"))
            return None
        tip = self.tip_for(point)
        a, b = self._map_point(self.foot), self._map_point(tip)
        if a.distance(b) < self.canvas.mapUnitsPerPixel():
            toward = ""
            if self.sun_ and self.sun_.azimuth is not None and self.snap.isChecked():
                toward = self.tr("（影は方位 {a}° の向き）").format(a=f"{shadow_azimuth(self.sun_.azimuth):.0f}")
            self.say(self.tr("根元と同じ地点。影の先端をクリック") + toward)
            return None
        m = self.measure(self.foot, tip)
        self.drop_foot()
        if m is None:
            return None
        self.measurements.append(m)
        self._redraw()
        self._update_list()
        self.say(self.tr("高さ {h}（影 {s}、太陽高度 {e}°）").format(h=format_metres(m.height), s=format_metres(m.shadow), e=f"{m.sun_elevation:.1f}"))
        return m

    def move(self, point) -> None:
        if self.foot is None or self.overlay is None:
            return
        tip = self.tip_for(point)
        m = self.measure(self.foot, tip)
        label = self.tr("高さ {h}").format(h=format_metres(m.height)) if m and m.shadow > 0 else ""
        self.overlay.cursor = (self.foot, tip, label)
        self.overlay.changed()

    def cancel(self) -> None:
        """Esc: drops the foot clicked, else puts the tool away."""
        if self.foot is not None:
            self.drop_foot()
        else:
            self.measure_button.setChecked(False)

    def drop_foot(self) -> None:
        self.foot = None
        if self.overlay is not None:
            self.overlay.cursor = None
            self.overlay.guide = None
            self.overlay.changed()

    def _update_guide(self, *_) -> None:
        if self.overlay is None:
            return
        direction = self.direction()
        if self.foot is None or direction is None or not self.snap.isChecked():
            self.overlay.guide = None
        else:
            a = self._map_point(self.foot)
            length = (direction[0] ** 2 + direction[1] ** 2) ** 0.5
            reach = GUIDE * self.canvas.mapUnitsPerPixel()
            end = QgsPointXY(a.x() + direction[0] / length * reach, a.y() + direction[1] / length * reach)
            self.overlay.guide = (self.foot, self._lon_lat(end))
        self.overlay.changed()

    def clear(self) -> None:
        """Removes every height measured."""
        self.measurements.clear()
        self.drop_foot()
        self._redraw()
        self._update_list()

    def _redraw(self) -> None:
        if self.overlay is None:
            return
        self.overlay.lines = [(m.foot, m.tip, self.tr("高さ {h}").format(h=format_metres(m.height))) for m in self.measurements]
        self.overlay.changed()

    def _update_list(self) -> None:
        self.list.clear()
        for m in self.measurements:
            self.list.addItem(self.tr("{h}（影 {s}・太陽高度 {e}°）").format(h=format_metres(m.height), s=format_metres(m.shadow), e=f"{m.sun_elevation:.1f}"))
        self.clear_button.setEnabled(bool(self.measurements))
        self.layer_button.setEnabled(bool(self.measurements))

    def add_layer(self) -> Optional[QgsVectorLayer]:
        """The heights measured as a scratch line layer 「影から高さ」 (foot to tip, labelled with the height)."""
        if not self.measurements:
            return None
        layer = QgsVectorLayer("LineString?crs=EPSG:4326", self.tr("影から高さ"), "memory")
        fields = [QgsField("height_m", QVariant.Double), QgsField("shadow_m", QVariant.Double),
                  QgsField("sun_elevation", QVariant.Double), QgsField("sun_azimuth", QVariant.Double)]
        layer.dataProvider().addAttributes(fields)
        layer.updateFields()
        for name, alias in (("height_m", "高さ（m）"), ("shadow_m", "影の長さ（m）"), ("sun_elevation", "太陽高度（°）"), ("sun_azimuth", "太陽の方位（°）")):
            layer.setFieldAlias(layer.fields().indexOf(name), alias)
        features = []
        for m in self.measurements:
            f = QgsFeature(layer.fields())
            f.setGeometry(QgsGeometry.fromPolylineXY([QgsPointXY(*m.foot), QgsPointXY(*m.tip)]))
            f.setAttributes([round(m.height, 2), round(m.shadow, 2), round(m.sun_elevation, 3),
                             None if m.sun_azimuth is None else round(m.sun_azimuth, 3)])
            features.append(f)
        layer.dataProvider().addFeatures(features)
        layer.updateExtents()
        _label_heights(layer)
        QgsProject.instance().addMapLayer(layer)
        return layer

    def say(self, text: str) -> None:
        self.iface.messageBar().pushInfo(self.tr("影から高さ"), text)

    def close_down(self) -> None:
        self.measure_button.setChecked(False)
        try:
            self.canvas.mapToolSet.disconnect(self._tool_set)
            self.canvas.extentsChanged.disconnect(self._update_guide)
        except (TypeError, RuntimeError):
            pass
        self._drop_overlay()


def _label_heights(layer: QgsVectorLayer) -> None:
    """Orange lines labelled 「高さ 12.3 m」, as drawn over the map."""
    from qgis.core import QgsLineSymbol, QgsPalLayerSettings, QgsTextBufferSettings, QgsTextFormat, QgsVectorLayerSimpleLabeling

    layer.renderer().setSymbol(QgsLineSymbol.createSimple({"line_color": COLOR.name(), "line_width": "0.7"}))
    settings = QgsPalLayerSettings()
    settings.isExpression = True
    settings.fieldName = "'高さ ' || format_number(\"height_m\", 1) || ' m'"
    text = QgsTextFormat()
    buffer = QgsTextBufferSettings()
    buffer.setEnabled(True)
    text.setBuffer(buffer)
    settings.setFormat(text)
    layer.setLabeling(QgsVectorLayerSimpleLabeling(settings))
    layer.setLabelsEnabled(True)
