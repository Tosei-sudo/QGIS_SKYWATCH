"""
The 「衛星パス」 dock, as the viewer's satellite-track-panel.ts: the ground
track of the chosen satellites and what each can image (撮像可能範囲: the
band its off-nadir angles reach, on the sides it can look to), at a moment
(the pass around it, with the satellite where it is then) or over a period.
Several satellites show at once, each in its own colour. The satellites are
the imaging plan's (its catalog, or the samples), and TLEs can be pasted.
"""
from __future__ import annotations

import time
from typing import Optional

from qgis.core import (
    QgsCategorizedSymbolRenderer, QgsFillSymbol, QgsGeometry, QgsLineSymbol, QgsPointXY, QgsProject, QgsRectangle,
    QgsRendererCategory, QgsWkbTypes,
)
from qgis.gui import QgsCollapsibleGroupBox, QgsRubberBand, QgsVertexMarker
from qgis.PyQt.QtCore import QCoreApplication, QDateTime, Qt, QTimer
from qgis.PyQt.QtGui import QColor, QStandardItem, QStandardItemModel
from qgis.PyQt.QtWidgets import (
    QAbstractItemView, QCheckBox, QComboBox, QDateTimeEdit, QDoubleSpinBox, QFormLayout, QHBoxLayout, QHeaderView, QLabel,
    QPlainTextEdit, QPushButton, QTableView, QVBoxLayout, QWidget,
)

from ..core.plan.output import PlanLayer, local_text
from ..core.plan.satellites import BUILT_IN_DEFAULTS, platform_key, satellite_label, satellites_from_text
from ..core.plan.track import (
    MAX_DAYS, TRACK_COLORS, sample_at, satellite_track, track_layers, track_rows, track_shapes, track_ticks,
)
from ..qgis_io.geometry import WGS84, from_wgs84, line_of, polygon_of
from ..qgis_io.plan_layer import memory_layer
from ..qgis_io.tasks import run_task
from .docks import PluginDock
from .plan_overlay import LabelItem
from .satellite_tree import SatelliteTree

#: How often the moment follows the clock, ms.
LIVE_EVERY = 30000
WINDOWS = ((5, "5 分"), (15, "15 分"), (50, "50 分（約半周）"), (100, "100 分（約 1 周）"))


def _now_ms() -> float:
    return time.time() * 1000


class TrackOverlay:
    """The tracks on the map while the dock is open: rubber bands and labels, nothing in the project."""

    def __init__(self, canvas):
        self.canvas = canvas
        self.bands: list[QgsRubberBand] = []
        self.items: list = []
        #: Each satellite's extent (WGS84), by its label; ticks left out.
        self.extents: dict[str, QgsRectangle] = {}

    def clear(self) -> None:
        for item in [*self.bands, *self.items]:
            self.canvas.scene().removeItem(item)
        self.bands, self.items, self.extents = [], [], {}

    def _grow(self, name: str, box: QgsRectangle) -> None:
        if name in self.extents:
            self.extents[name].combineExtentWith(box)
        else:
            self.extents[name] = QgsRectangle(box)

    def _point(self, lon: float, lat: float) -> QgsPointXY:
        return from_wgs84(self.canvas.mapSettings().destinationCrs()).transform(QgsPointXY(lon, lat))

    def draw(self, track, color: str, at: Optional[float], ticks: bool) -> None:
        name = satellite_label(track.sat)
        lines, rings = track_shapes(track)
        c = QColor(color)
        reach = QgsRubberBand(self.canvas, QgsWkbTypes.GeometryType.PolygonGeometry)
        stroke = QColor(c)
        stroke.setAlpha(0x99)
        fill = QColor(c)
        fill.setAlpha(0x2e)
        reach.setStrokeColor(stroke)
        reach.setFillColor(fill)
        reach.setWidth(1)
        reach.setZValue(950)
        for ring in rings:
            g = polygon_of([ring])
            reach.addGeometry(g, WGS84)
            self._grow(name, g.boundingBox())
        line = QgsRubberBand(self.canvas, QgsWkbTypes.GeometryType.LineGeometry)
        line.setStrokeColor(c)
        line.setWidth(2)
        line.setZValue(960)
        for part in lines:
            g = line_of(part)
            line.addGeometry(g, WGS84)
            self._grow(name, g.boundingBox())
        self.bands += [reach, line]
        if ticks:
            long = track.end - track.start > 86400000
            for i, t in track_ticks(track, at):
                lon, lat = track.track[i]
                self.items.append(self._marker(lon, lat, color, 6))
                text = local_text(t)
                self.items.append(LabelItem(self.canvas, self._point(lon, lat), text[5:] if long else text[11:], "#111111", False, 0, True))
        i = 0 if at is None else sample_at(track, at)
        if i >= 0:
            lon, lat = track.track[i]
            self.items.append(self._marker(lon, lat, color, 14))
            label = f"{name}（開始）" if at is None else f"{name} {local_text(track.times[i])[11:]}"
            self.items.append(LabelItem(self.canvas, self._point(lon, lat), label, "#111111", True, -14))
            self._grow(name, QgsRectangle(lon, lat, lon, lat))

    def _marker(self, lon: float, lat: float, color: str, size: int) -> QgsVertexMarker:
        marker = QgsVertexMarker(self.canvas)
        marker.setCenter(self._point(lon, lat))
        marker.setColor(QColor("white"))
        marker.setFillColor(QColor(color))
        marker.setIconType(QgsVertexMarker.IconType.ICON_CIRCLE)
        marker.setIconSize(size)
        marker.setPenWidth(1 if size < 10 else 2)
        marker.setZValue(1500)
        return marker

    def extent(self, name: Optional[str] = None) -> Optional[QgsRectangle]:
        out: Optional[QgsRectangle] = None
        for key, box in self.extents.items():
            if name is not None and key != name:
                continue
            if out is None:
                out = QgsRectangle(box)
            else:
                out.combineExtentWith(box)
        return out


class SatellitePassDock(PluginDock):
    """衛星パス."""

    def __init__(self, iface, context, open_settings, imaging_plan, parent=None):
        super().__init__("SatObsSatellitePass", QCoreApplication.translate("SatellitePassDock", "衛星パス"), context, open_settings, parent)
        self.iface = iface
        self.canvas = iface.mapCanvas()
        #: The imaging plan dock, whose satellites (catalog or samples) are listed here too.
        self.imaging_plan = imaging_plan
        self.overlay = TrackOverlay(self.canvas)
        #: The tracks drawn: [(SatelliteTrack, colour)].
        self.tracks: list[tuple] = []
        #: Each chosen satellite's colour (by its key), kept while it stays chosen.
        self.colors: dict[str, str] = {}
        self.task = None
        self._listed_once = False
        self._build()
        self.timer = QTimer(self)
        self.timer.setSingleShot(True)
        self.timer.setInterval(150)
        self.timer.timeout.connect(self.draw)
        self.live_timer = QTimer(self)
        self.live_timer.setInterval(LIVE_EVERY)
        self.live_timer.timeout.connect(self._tick)
        self.visibilityChanged.connect(self._visibility)
        context.configChanged.connect(self._config_changed)

    # --- The form ---------------------------------------------------------------------------

    def _build(self) -> None:
        self.tree = SatelliteTree()
        self.tree.changed.connect(self._chosen_changed)
        self.body.addWidget(self.tree, 2)
        form = QFormLayout()
        self.mode = QComboBox()
        self.mode.addItem(self.tr("時点"), "at")
        self.mode.addItem(self.tr("期間"), "range")
        self.mode.currentIndexChanged.connect(self._mode_changed)
        form.addRow(self.tr("表示"), self.mode)
        now = QDateTime.currentDateTime()
        self.at = self._time_edit(now)
        now_button = QPushButton(self.tr("今"))
        now_button.setToolTip(self.tr("現在時刻に設定"))
        now_button.clicked.connect(self.set_now)
        at_row = QHBoxLayout()
        at_row.addWidget(self.at, 1)
        at_row.addWidget(now_button)
        self.at_label = QLabel(self.tr("時刻"))
        self.at_row = QWidget()
        self.at_row.setLayout(at_row)
        at_row.setContentsMargins(0, 0, 0, 0)
        form.addRow(self.at_label, self.at_row)
        self.start = self._time_edit(now)
        self.end = self._time_edit(now.addDays(1))
        self.start_label = QLabel(self.tr("開始"))
        self.end_label = QLabel(self.tr("終了"))
        form.addRow(self.start_label, self.start)
        form.addRow(self.end_label, self.end)
        self.body.addLayout(form)

        more = QgsCollapsibleGroupBox(self.tr("詳細"))
        more.setCollapsed(True)
        more.collapsedStateChanged.connect(lambda collapsed: collapsed or self._mode_changed())
        self.more = more
        more_form = QFormLayout(more)
        self.window = QComboBox()
        for minutes, label in WINDOWS:
            self.window.addItem(self.tr(label), minutes)
        self.window.setCurrentIndex(1)
        self.window.currentIndexChanged.connect(self.schedule)
        self.window_label = QLabel(self.tr("前後の時間"))
        more_form.addRow(self.window_label, self.window)
        self.max_off_nadir = QDoubleSpinBox()
        self.max_off_nadir.setRange(-1, 80)
        self.max_off_nadir.setDecimals(1)
        self.max_off_nadir.setSpecialValueText(self.tr("衛星の上限"))
        self.max_off_nadir.setValue(-1)
        self.max_off_nadir.setSuffix("°")
        self.max_off_nadir.valueChanged.connect(self.schedule)
        more_form.addRow(self.tr("オフナディア角（以下）"), self.max_off_nadir)
        self.daylight = QComboBox()
        self.daylight.addItem(self.tr("考慮なし"), None)
        self.daylight.addItem(self.tr("光学衛星は日中のみ（太陽高度 10° 以上）"), 10.0)
        self.daylight.addItem(self.tr("光学衛星は日中のみ（太陽高度 0° 以上）"), 0.0)
        self.daylight.setToolTip(self.tr("光学衛星の撮像可能範囲を、衛星直下の太陽高度がこの値以上の所のみに限定（SAR は常に表示）"))
        self.daylight.currentIndexChanged.connect(self.schedule)
        more_form.addRow(self.tr("日照条件"), self.daylight)
        self.ticks = QCheckBox(self.tr("軌跡に時刻を表示"))
        self.ticks.setChecked(True)
        self.ticks.toggled.connect(self.schedule)
        more_form.addRow(self.ticks)
        self.live = QCheckBox(self.tr("現在時刻に合わせ続ける（30 秒ごと）"))
        self.live.toggled.connect(self.set_live)
        more_form.addRow(self.live)
        self.tle = QPlainTextEdit()
        self.tle.setPlaceholderText("ISS (ZARYA)\n1 25544U 98067A   …\n2 25544  51.6416 …")
        self.tle.setMaximumHeight(70)
        self.tle.textChanged.connect(self.list_sats)
        more_form.addRow(self.tr("TLE を貼り付けて追加"), self.tle)
        self.paste_max = self._angle(BUILT_IN_DEFAULTS["maxOffNadir"])
        self.paste_min = self._angle(0)
        self.paste_side = QComboBox()
        for key, label in (("both", "左右"), ("right", "右のみ"), ("left", "左のみ")):
            self.paste_side.addItem(self.tr(label), key)
        self.paste_side.currentIndexChanged.connect(self.list_sats)
        self.paste_sar = QCheckBox("SAR")
        self.paste_sar.toggled.connect(self.list_sats)
        more_form.addRow(self.tr("最大オフナディア角"), self.paste_max)
        more_form.addRow(self.tr("最小オフナディア角"), self.paste_min)
        more_form.addRow(self.tr("向き"), self.paste_side)
        more_form.addRow(self.paste_sar)
        self.body.addWidget(more)

        self.status = QLabel()
        self.status.setWordWrap(True)
        self.body.addWidget(self.status)
        self.model = QStandardItemModel(0, 5)
        self.model.setHorizontalHeaderLabels([self.tr("衛星"), self.tr("位置"), self.tr("高度"), self.tr("軌道"), self.tr("撮像可能範囲")])
        self.table = QTableView()
        self.table.setModel(self.model)
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.verticalHeader().hide()
        self.table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.ResizeToContents)
        self.table.clicked.connect(lambda index: self.zoom_to(index.row()))
        self.table.setToolTip(self.tr("クリックでこの衛星へ移動"))
        self.body.addWidget(self.table, 1)
        buttons = QHBoxLayout()
        self.layer_button = QPushButton(self.tr("レイヤーとして追加"))
        self.layer_button.setToolTip(self.tr("軌跡と撮像可能範囲をレイヤーとして追加"))
        self.layer_button.clicked.connect(self.add_layer)
        self.zoom_button = QPushButton(self.tr("範囲へ移動"))
        self.zoom_button.clicked.connect(lambda: self.zoom_to())
        buttons.addStretch(1)
        buttons.addWidget(self.layer_button)
        buttons.addWidget(self.zoom_button)
        self.body.addLayout(buttons)
        self._mode_changed()
        self._update_buttons()

    def _time_edit(self, value: QDateTime) -> QDateTimeEdit:
        edit = QDateTimeEdit(value)
        edit.setDisplayFormat("yyyy/MM/dd HH:mm")
        edit.setCalendarPopup(True)
        edit.dateTimeChanged.connect(self.schedule)
        return edit

    def _angle(self, value: float) -> QDoubleSpinBox:
        box = QDoubleSpinBox()
        box.setRange(0, 80)
        box.setDecimals(1)
        box.setSuffix("°")
        box.setValue(value)
        box.valueChanged.connect(self.list_sats)
        return box

    def _mode_changed(self, *_args) -> None:
        moment = self.mode.currentData() == "at"
        for w in (self.at_label, self.at_row, self.window_label, self.window, self.live):
            w.setVisible(moment)
        for w in (self.start_label, self.start, self.end_label, self.end):
            w.setVisible(not moment)
        self.model.setHeaderData(1, Qt.Orientation.Horizontal, self.tr("位置") if moment else self.tr("開始時の位置"))
        if not moment and self.live.isChecked():
            self.live.setChecked(False)
        self.schedule()

    def _visibility(self, visible: bool) -> None:
        if visible:
            self.load()
        else:
            self.set_live(False)
            self.cancel()
            self.overlay.clear()
            self.canvas.refresh()

    def _config_changed(self) -> None:
        if self.isVisible():
            self.load()

    def load(self) -> None:
        """Lists the imaging plan's satellites (reading its catalog first when needed) and draws."""
        self.status.setText(self.tr("衛星を読み込み中…"))
        self.imaging_plan.load_catalog(self._loaded)

    def _loaded(self) -> None:
        self.list_sats()
        self.draw()

    def pasted(self) -> list:
        top = self.paste_max.value() or BUILT_IN_DEFAULTS["maxOffNadir"]
        return satellites_from_text(self.tle.toPlainText(), maxOffNadir=top, minOffNadir=min(top, self.paste_min.value()), swath=BUILT_IN_DEFAULTS["swath"],
                                    lookSide=self.paste_side.currentData(), sar=self.paste_sar.isChecked())

    def list_sats(self, *_args) -> None:
        """Lists the satellites, keeping which were chosen (pasted ones start chosen; at first only a few satellites)."""
        pasted = self.pasted()
        before = {l.key for l in self.tree.items()}
        every = [*self.imaging_plan.catalog_sats, *pasted]
        self.tree.set(every)
        first = not self._listed_once and bool(every)
        if first:
            self._listed_once = True
        # At first only a few satellites (the samples) start chosen, so something shows at once.
        if first and len({platform_key(s) for s in every}) > 2:
            self.tree.choice.check_all(False)
            for l in self.tree.items():
                l.checked = any(l.sat is s for s in pasted)
            self.tree.refresh()
        elif not first:
            # Newly listed catalog satellites start unchosen; pasted ones chosen.
            for l in self.tree.items():
                if l.key not in before and not any(l.sat is s for s in pasted):
                    l.checked = False
            self.tree.refresh()
        self._chosen_changed()

    def _chosen_changed(self) -> None:
        self.paint()
        self.schedule()

    def paint(self) -> None:
        """Colours: kept while chosen, the first free one for a newly chosen satellite."""
        chosen = [l for l in self.tree.items() if l.checked]
        keys = {l.key for l in chosen}
        for key in list(self.colors):
            if key not in keys:
                del self.colors[key]
        for l in chosen:
            if l.key in self.colors:
                continue
            used = set(self.colors.values())
            self.colors[l.key] = next((c for c in TRACK_COLORS if c not in used), TRACK_COLORS[len(self.colors) % len(TRACK_COLORS)])
        self.tree.set_markers(self.colors)

    def schedule(self, *_args) -> None:
        if self.isVisible():
            self.timer.start()

    def set_now(self) -> None:
        self.at.setDateTime(QDateTime.fromMSecsSinceEpoch(int(_now_ms())))
        self.schedule()

    def set_live(self, on: bool) -> None:
        self.live_timer.stop()
        if self.live.isChecked() != on:
            self.live.blockSignals(True)
            self.live.setChecked(on)
            self.live.blockSignals(False)
        if on:
            self._tick()
            self.live_timer.start()

    def _tick(self) -> None:
        self.at.blockSignals(True)
        self.at.setDateTime(QDateTime.fromMSecsSinceEpoch(int(_now_ms())))
        self.at.blockSignals(False)
        self.draw()

    def set_time(self, at=None, start: Optional[float] = None, end: Optional[float] = None) -> None:
        """Shows the satellites at a moment (ms), or over a period."""
        if at is not None:
            self.mode.setCurrentIndex(self.mode.findData("at"))
            self.at.setDateTime(QDateTime.fromMSecsSinceEpoch(int(at)))
        else:
            self.mode.setCurrentIndex(self.mode.findData("range"))
            self.start.setDateTime(QDateTime.fromMSecsSinceEpoch(int(start)))
            self.end.setDateTime(QDateTime.fromMSecsSinceEpoch(int(end)))
        self.draw()

    def choose(self, names: list) -> None:
        """Chooses the satellites named (or with these ids) and draws them."""
        self.tree.check_only(names)
        self.draw()

    # --- Drawing ------------------------------------------------------------------------------

    def period(self) -> dict | str:
        """The period to draw (ms), with a note when it was shortened; a problem when it cannot be read."""
        if self.mode.currentData() == "at":
            at = self.at.dateTime().toMSecsSinceEpoch()
            minutes = self.window.currentData() or 15
            return {"start": at - minutes * 60000, "end": at + minutes * 60000, "note": "", "at": at}
        start = self.start.dateTime().toMSecsSinceEpoch()
        end = self.end.dateTime().toMSecsSinceEpoch()
        if end <= start:
            return self.tr("終了は開始より後で指定")
        note = ""
        if end - start > MAX_DAYS * 86400000:
            end = start + MAX_DAYS * 86400000
            note = self.tr("（期間は最長 {d} 日。{t} までを表示）").format(d=MAX_DAYS, t=local_text(end))
        return {"start": start, "end": end, "note": note, "at": None}

    def cancel(self) -> None:
        if self.task is not None:
            task, self.task = self.task, None
            task.cancel()

    def draw(self) -> None:
        """Works out the chosen satellites' tracks in a task, then draws them."""
        self.timer.stop()
        self.cancel()
        chosen = [(l.key, l.sat) for l in self.tree.items() if l.checked]
        period = self.period()
        if not self.tree.items():
            return self._show([], self.tr("衛星なし。「詳細」で TLE を貼り付けて追加可能"), None)
        if not chosen:
            return self._show([], self.tr("表示する衛星を選択（複数可）"), None)
        if isinstance(period, str):
            return self._show([], period, None)
        top = self.max_off_nadir.value()
        options = {"step": 10 if period["at"] is not None else None, "max_off_nadir": None if top < 0 else top,
                   "min_sun": self.daylight.currentData()}
        colors = dict(self.colors)
        self.status.setText(self.tr("{n} 機の軌跡を計算中…").format(n=len(chosen)))

        def work(task):
            out, failed = [], []
            for i, (key, sat) in enumerate(chosen):
                task.check()
                track = satellite_track(sat, period["start"], period["end"], **options)
                if track and len(track.times) > 1:
                    out.append((track, colors.get(key, TRACK_COLORS[0])))
                else:
                    failed.append(satellite_label(sat))
                task.progress((i + 1) / len(chosen) * 100)
            return out, failed

        def done(result):
            if self.task is not task:
                return
            self.task = None
            tracks, failed = result
            if period["at"] is not None:
                when = self.tr("{t} 前後 {m} 分").format(t=local_text(period["at"]), m=round((period["end"] - period["at"]) / 60000))
            else:
                when = f"{local_text(period['start'])} 〜 {local_text(period['end'])}"
            text = self.tr("{n} 機の軌跡と撮像可能範囲（{when}）").format(n=len(tracks), when=when) + period["note"]
            if failed:
                text += self.tr("。軌道を計算できなかった衛星: {list}").format(list="、".join(failed))
            self._show(tracks, text, period["at"])

        def failed(message):
            if self.task is not task:
                return
            self.task = None
            self.status.setText(self.tr("軌跡の計算に失敗: {m}").format(m=message))

        task = run_task(self.tr("衛星パスの計算"), work, done, failed)
        self.task = task

    def _show(self, tracks: list, text: str, at: Optional[float]) -> None:
        self.tracks = tracks
        self.overlay.clear()
        for track, color in tracks:
            self.overlay.draw(track, color, at, self.ticks.isChecked())
        self.canvas.refresh()
        self.status.setText(text)
        self.model.removeRows(0, self.model.rowCount())
        for (track, color), row in zip(tracks, track_rows([t for t, _ in tracks], at)):
            items = [QStandardItem(v) for v in row]
            items[0].setData(QColor(color), Qt.ItemDataRole.DecorationRole)
            self.model.appendRow(items)
        self._update_buttons()

    def _update_buttons(self) -> None:
        self.layer_button.setEnabled(bool(self.tracks))
        self.zoom_button.setEnabled(bool(self.tracks))

    def zoom_to(self, row: Optional[int] = None) -> None:
        """Fits the map to the tracks drawn (or one satellite's)."""
        name = satellite_label(self.tracks[row][0].sat) if row is not None and 0 <= row < len(self.tracks) else None
        box = self.overlay.extent(name)
        if box is None:
            return
        box = box.buffered(max(box.width(), box.height()) * 0.05 + 0.01)
        self.canvas.setExtent(from_wgs84(self.canvas.mapSettings().destinationCrs()).transformBoundingBox(box))
        self.canvas.refresh()

    def add_layer(self) -> Optional[tuple]:
        """The tracks and reach drawn now as two layers in a group: 地上軌跡 (lines) and 撮像可能範囲 (areas), coloured per satellite."""
        if not self.tracks:
            return None
        fields, lines, areas = track_layers([t for t, _ in self.tracks])
        colors = {satellite_label(t.sat): c for t, c in self.tracks}
        first = self.tracks[0][0]
        title = self.tr("衛星パス {a} 〜 {b}（{n} 機）").format(a=local_text(first.start), b=local_text(first.end), n=len(self.tracks))
        project = QgsProject.instance()
        group = project.layerTreeRoot().insertGroup(0, title)
        made = []
        for name, features, geometry in ((self.tr("撮像可能範囲"), areas, "MultiPolygon"), (self.tr("地上軌跡"), lines, "MultiLineString")):
            layer = memory_layer(PlanLayer(name, fields, features), name, geometry)
            categories = []
            for label, color in colors.items():
                if geometry == "MultiPolygon":
                    stroke = QColor(color)
                    stroke.setAlpha(0x99)
                    symbol = QgsFillSymbol.createSimple({"color": f"{QColor(color).red()},{QColor(color).green()},{QColor(color).blue()},46",
                                                         "outline_color": f"{stroke.red()},{stroke.green()},{stroke.blue()},153", "outline_width": "0.26"})
                else:
                    symbol = QgsLineSymbol.createSimple({"line_color": color, "line_width": "0.6"})
                categories.append(QgsRendererCategory(label, symbol, label))
            layer.setRenderer(QgsCategorizedSymbolRenderer("satellite", categories))
            project.addMapLayer(layer, False)
            group.addLayer(layer)
            made.append(layer)
        return tuple(made)

    def close_down(self) -> None:
        self.timer.stop()
        self.live_timer.stop()
        self.cancel()
        self.overlay.clear()
