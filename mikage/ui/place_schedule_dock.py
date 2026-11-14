"""
The 「地点ごとの撮像予定」 dock (tsudo, 2026-11-14: 「撮像計画カレンダーとは別に、
この地点を次いつ取る計画があるかを可視化したい。レイヤー（ポリゴン、ポイント
等）もしくはクリックした地点の、この先の撮像計画をタイムラインで。読み専用。
複数地点は縦に並べ、横に時間が伸びる」).

The places are a layer's features (all, or those selected; named by an
attribute) and points clicked on the map (or 「この地点の撮像予定」 of the
map's right-click menu). For each, from today for the days chosen, the
plans of the history that take it and the days requests in force will be
planned on (core/request/place_plans.py), on a timeline under the map
(ui/place_timeline.py): a click on a plan draws its area and shot on the
map, one on a place's name goes to the place. Nothing is written: the
answers and plans are entered in 「撮像予定」 and 「撮像計画ファイル出力」.
"""
from __future__ import annotations

import time
from datetime import timedelta
from typing import Optional

from qgis.core import NULL, QgsFeatureRequest, QgsGeometry, QgsMapLayerProxyModel, QgsPointXY, QgsRectangle, QgsVectorLayer, QgsWkbTypes
from qgis.gui import QgsFieldComboBox, QgsMapLayerComboBox, QgsMapToolEmitPoint
from qgis.PyQt.QtCore import QCoreApplication, Qt, QTimer
from qgis.PyQt.QtWidgets import QCheckBox, QComboBox, QFrame, QLabel, QPushButton, QScrollArea

from ..core.esri import EsriError
from ..core.request.model import bbox_of, jst_day
from ..core.request.place_plans import (
    DAY_CHOICES, DAYS, MOST_PLACES, REQUEST, Lane, Mark, Place, lanes_of, mark_name, next_of, sort_by_next, span_of,
    tally_text,
)
from ..core.request.store import RequestStoreConfig, load_days, load_requests, load_shots_where, shot_days_where
from ..qgis_io.geometry import from_wgs84, rings_geometry, to_wgs84
from ..qgis_io.tasks import run_task
from ..qgis_io import settings
from .docks import PluginDock, plain_label
from .flow_layout import FlowLayout
from .place_timeline import COLORS, PlaceTimeline
from .plan_overlay import PlanOverlay
from .schedule_dock import SHOT_COLOR

try:
    _GEOMETRY_LAYERS = QgsMapLayerProxyModel.Filter.HasGeometry
except AttributeError:  # older bindings: the enum unscoped
    _GEOMETRY_LAYERS = QgsMapLayerProxyModel.HasGeometry

#: The clicked points' colour on the map.
POINT_COLOR = "#0f766e"
#: How often 「次」 and the line of now move on (ms).
TICK_MS = 60000
#: The height the dock asks for under the map (pixels): low, so that the map keeps most of the window.
HEIGHT = 300


def _now_ms() -> float:
    return time.time() * 1000


def _text(value) -> str:
    return "" if value is None or value == NULL else str(value).strip()


def place_of(geometry: QgsGeometry, transform, name: str, key: str) -> Optional[Place]:
    """A feature's shape as a place (WGS84; curves made straight); None when it has none."""
    if geometry is None or geometry.isNull() or geometry.isEmpty():
        return None
    g = QgsGeometry(geometry)
    if QgsWkbTypes.isCurvedType(g.wkbType()):
        g.convertToStraightSegment()
    if transform is not None and transform.isValid() and not transform.isShortCircuited():
        g.transform(transform)
    kind = g.type()
    if kind == QgsWkbTypes.GeometryType.PolygonGeometry:
        parts = g.asMultiPolygon() if g.isMultipart() else [g.asPolygon()]
        rings = [[[p.x(), p.y()] for p in ring] for polygon in parts for ring in polygon if ring]
        return Place(name, rings=rings, key=key) if rings else None
    if kind == QgsWkbTypes.GeometryType.LineGeometry:
        parts = g.asMultiPolyline() if g.isMultipart() else [g.asPolyline()]
        lines = [[[p.x(), p.y()] for p in line] for line in parts if len(line) >= 2]
        return Place(name, lines=lines, key=key) if lines else None
    if kind == QgsWkbTypes.GeometryType.PointGeometry:
        points = g.asMultiPoint() if g.isMultipart() else [g.asPoint()]
        return Place(name, points=[[p.x(), p.y()] for p in points], key=key) if points else None
    return None


def _valid(g: QgsGeometry) -> QgsGeometry:
    if g.isEmpty() or g.isGeosValid():
        return g
    fixed = g.makeValid()
    return fixed if not fixed.isEmpty() else g


class GeometryShares:
    """
    place_plans.share_of with QGIS's geometry (a layer's shapes may have
    thousands of vertices): each place's shape prepared once, each area made
    once, for one working out of the lines.
    """

    def __init__(self):
        self._places: dict[int, tuple] = {}
        self._areas: dict[int, Optional[tuple]] = {}

    def _place(self, place: Place) -> tuple:
        got = self._places.get(id(place))
        if got is None:
            if place.rings:
                g = _valid(rings_geometry(place.rings))
            elif place.lines:
                g = QgsGeometry.fromMultiPolylineXY([[QgsPointXY(p[0], p[1]) for p in line] for line in place.lines])
            else:
                g = QgsGeometry.fromMultiPointXY([QgsPointXY(p[0], p[1]) for p in place.points])
            engine = QgsGeometry.createGeometryEngine(g.constGet())
            engine.prepareGeometry()
            got = (g, engine, g.area() if place.rings else 0.0, place.box)
            self._places[id(place)] = got
        return got

    def _area(self, rings) -> Optional[tuple]:
        key = id(rings)
        if key not in self._areas:
            g = _valid(rings_geometry(rings)) if rings else None
            self._areas[key] = (g, bbox_of(rings)) if g is not None and not g.isEmpty() else None
        return self._areas[key]

    def __call__(self, place: Place, rings) -> Optional[float]:
        area = self._area(rings)
        if area is None:
            return None
        a, abox = area
        g, engine, size, box = self._place(place)
        if box is None or abox is None or box[0] > abox[2] or abox[0] > box[2] or box[1] > abox[3] or abox[1] > box[3]:
            return None
        if not engine.intersects(a.constGet()):
            return None
        if place.rings:
            return min(1.0, g.intersection(a).area() / size) if size > 0 else 1.0
        if place.points and len(place.points) > 1:
            inside = sum(1 for p in place.points if a.contains(QgsGeometry.fromPointXY(QgsPointXY(p[0], p[1]))))
            return inside / len(place.points)
        return 1.0


class PlaceScheduleDock(PluginDock):
    """The plans ahead of each place (a layer's features, points clicked), on a timeline. Read only."""

    def __init__(self, iface, context, open_settings, parent=None):
        super().__init__("SatObsPlaceSchedule", QCoreApplication.translate("PlaceScheduleDock", "地点ごとの撮像予定"), context,
                         open_settings, parent)
        self.iface = iface
        self.canvas = iface.mapCanvas()
        self.overlay = PlanOverlay(self.canvas)
        self.points_overlay = PlanOverlay(self.canvas)
        self.store: Optional[RequestStoreConfig] = None
        #: What was read (the days from `first`): the history's rows, the shots by SHOT_ID, the ledger's requests.
        self.rows: list = []
        self.shots: dict = {}
        self.requests: list = []
        self.first = jst_day(_now_ms())
        self.read_days = 0
        self.shot_problem = ""
        #: The points clicked on the map (WGS84), in order.
        self.clicked: list[Place] = []
        self._clicks = 0
        #: The lines shown (in the order shown), and whether the layer had more features than are shown.
        self.lanes: list[Lane] = []
        self.cut = False
        self.task = None
        self._loaded = False
        self._again = False
        self._watched: Optional[QgsVectorLayer] = None
        self._previous_tool = None
        self.point_tool = QgsMapToolEmitPoint(self.canvas)
        self.point_tool.canvasClicked.connect(self._map_clicked)
        self.point_tool.deactivated.connect(lambda: self.pick.setChecked(False))
        self._tick = QTimer(self)
        self._tick.setInterval(TICK_MS)
        self._tick.timeout.connect(self._moved_on)
        self._build()
        context.configChanged.connect(self._config_changed)
        self.visibilityChanged.connect(self._visibility)
        self._config_changed()

    # --- Building -------------------------------------------------------------------------------

    def _build(self) -> None:
        layout = self.body
        self.store_choice = QComboBox()
        self.store_choice.setToolTip(self.tr("依頼の保存先"))
        self.store_choice.currentIndexChanged.connect(self._store_chosen)
        layout.addWidget(self.store_choice)
        places = FlowLayout()
        places.addWidget(QLabel(self.tr("地点")))
        self.layer_choice = QgsMapLayerComboBox()
        self.layer_choice.setFilters(_GEOMETRY_LAYERS)
        try:
            self.layer_choice.setAllowEmptyLayer(True, self.tr("（レイヤーなし）"))
        except TypeError:  # older bindings: no text for the empty entry
            self.layer_choice.setAllowEmptyLayer(True)
        self.layer_choice.setLayer(None)
        self.layer_choice.setToolTip(self.tr("このレイヤーの地物（ポリゴン・点・線）ごとに 1 行。空ならクリックした地点だけ"))
        self.layer_choice.setMinimumWidth(180)
        self.layer_choice.layerChanged.connect(self._layer_changed)
        places.addWidget(self.layer_choice)
        self.name_field = QgsFieldComboBox()
        self.name_field.setAllowEmptyFieldName(True)
        self.name_field.setToolTip(self.tr("地点の名前にする属性（空なら「レイヤー名 地物ID」）"))
        self.name_field.setMinimumWidth(120)
        self.name_field.fieldChanged.connect(lambda _f: self.refit())
        places.addWidget(self.name_field)
        self.selected_only = QCheckBox(self.tr("選択中の地物だけ"))
        self.selected_only.toggled.connect(lambda _on: self.refit())
        places.addWidget(self.selected_only)
        self.pick = QPushButton(self.tr("地図で地点を追加"))
        self.pick.setCheckable(True)
        self.pick.setToolTip(self.tr("地図をクリックした地点を行に足す（もう一度押すと終わる）。地図の右クリック「この地点の撮像予定」でも足せる"))
        self.pick.toggled.connect(self._pick_toggled)
        places.addWidget(self.pick)
        self.clear_button = QPushButton(self.tr("地点を消す"))
        self.clear_button.setToolTip(self.tr("クリックで足した地点をすべて消す（レイヤーの地物はそのまま）"))
        self.clear_button.clicked.connect(lambda: self.clear_points())
        places.addWidget(self.clear_button)
        # One row with the period and the options when the dock is wide (under the map), wrapped when narrow.
        options = places
        options.addWidget(QLabel(self.tr("期間")))
        self.days_choice = QComboBox()
        for n in DAY_CHOICES:
            self.days_choice.addItem(self.tr("今日から {n} 日").format(n=n), n)
        saved = settings._get("placeSchedule/days", str(DAYS))
        index = self.days_choice.findData(int(saved) if saved.isdigit() else DAYS)
        self.days_choice.setCurrentIndex(index if index >= 0 else self.days_choice.findData(DAYS))
        self.days_choice.currentIndexChanged.connect(self._days_chosen)
        options.addWidget(self.days_choice)
        self.not_taken = QCheckBox(self.tr("不採用・見送りも"))
        self.not_taken.setToolTip(self.tr("システムが採用しなかった計画と、出さなかった（見送った）依頼も出す"))
        self.not_taken.setChecked(settings._get("placeSchedule/notTaken") == "1")
        self.not_taken.toggled.connect(lambda on: (settings._set("placeSchedule/notTaken", "1" if on else "0"), self.refit()))
        options.addWidget(self.not_taken)
        self.by_next = QCheckBox(self.tr("次が早い順"))
        self.by_next.setToolTip(self.tr("次の計画が早い地点を上に（計画がなく依頼だけの地点、予定なしの地点はその下）"))
        self.by_next.setChecked(settings._get("placeSchedule/byNext") == "1")
        self.by_next.toggled.connect(lambda on: (settings._set("placeSchedule/byNext", "1" if on else "0"), self.refit()))
        options.addWidget(self.by_next)
        self.reload_button = QPushButton(self.tr("再読み込み"))
        self.reload_button.setToolTip(self.tr("出力履歴・撮像の範囲・依頼台帳を読み直す"))
        self.reload_button.clicked.connect(lambda: self.reload())
        options.addWidget(self.reload_button)
        layout.addLayout(places)
        # Names from the ledger and the layers: shown as written, never as HTML.
        self.status = plain_label(QLabel())
        self.status.setWordWrap(True)
        self.status.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        layout.addWidget(self.status)
        self.timeline = PlaceTimeline()
        self.timeline.picked.connect(self._picked)
        self.timeline.place_clicked.connect(self.go_to)
        self.area = QScrollArea()
        self.area.setObjectName("SatObsPlaceScheduleTimeline")
        self.area.setWidgetResizable(True)
        self.area.setFrameShape(QFrame.Shape.StyledPanel)
        self.area.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.area.setWidget(self.timeline)
        self.area.setMinimumHeight(90)
        layout.addWidget(self.area, 1)
        legend = QLabel(self._legend())
        legend.setTextFormat(Qt.TextFormat.RichText)
        legend.setWordWrap(True)
        legend.setToolTip(self.tr("縦棒は時刻の分かる計画、破線の枠は時刻未定の計画（その日）。薄い色はその依頼の範囲の外で、"
                                  "撮像の範囲（衛星が撮る範囲）に入るもの。下の帯は依頼の日（まだ計画前）。赤い線は今"))
        layout.addWidget(legend)
        self.chosen_label = plain_label(QLabel())
        self.chosen_label.setWordWrap(True)
        layout.addWidget(self.chosen_label)

    def _legend(self) -> str:
        names = [("採用", "採用"), ("要求外", "要求外（システムの定常）"), ("不明", "採否不明"), ("不採用", "不採用"), ("見送り", "見送り"),
                 (REQUEST, "依頼の日（計画前）")]
        return "　".join(f"<span style='color:{COLORS[k]}'>■</span> {self.tr(text)}" for k, text in names)

    # --- Settings -------------------------------------------------------------------------------

    def _config_changed(self) -> None:
        stores = self.context.config.request_stores
        self.store_choice.blockSignals(True)
        self.store_choice.clear()
        for s in stores:
            self.store_choice.addItem(s.label)
        self.store_choice.blockSignals(False)
        self.store_choice.setVisible(len(stores) > 1)
        self.store = stores[0] if stores else None
        self._forget()
        if self.store is None:
            self.status.setText(self.tr("設定ファイルに撮像依頼の保存先（requestStores）なし"))
        elif self.isVisible():
            self.reload()

    def _store_chosen(self, index: int) -> None:
        stores = self.context.config.request_stores
        self.store = stores[index] if 0 <= index < len(stores) else None
        self._forget()
        self.reload()

    def _forget(self) -> None:
        self.rows, self.shots, self.requests, self.read_days, self.shot_problem = [], {}, [], 0, ""
        self._loaded = False
        self.refit()

    def days(self) -> int:
        return int(self.days_choice.currentData() or DAYS)

    def _days_chosen(self, _index: int) -> None:
        settings._set("placeSchedule/days", str(self.days()))
        if self._loaded and self.days() <= self.read_days and self.first == jst_day(_now_ms()):
            self.refit()
        else:
            self.reload()

    def _first_height(self) -> None:
        """
        The first time it is opened under the map: low (HEIGHT), so that the
        map keeps most of the window. After that QGIS keeps the size it is
        given, as for any dock.
        """
        if settings._get("placeSchedule/sized") == "1" or self.isFloating():
            return
        window = self.iface.mainWindow()
        if window.dockWidgetArea(self) == Qt.DockWidgetArea.BottomDockWidgetArea:
            window.resizeDocks([self], [HEIGHT], Qt.Orientation.Vertical)
            settings._set("placeSchedule/sized", "1")

    def _visibility(self, visible: bool) -> None:
        if visible:
            QTimer.singleShot(0, self._first_height)
            self._tick.start()
            if self.store is not None and (not self._loaded or self.first != jst_day(_now_ms())):
                self.reload()
            self._draw_points()
        else:
            self._tick.stop()
            self.overlay.unpaint()
            self.points_overlay.clear()
            if self.pick.isChecked():
                self.pick.setChecked(False)
            self.canvas.refresh()

    def token(self) -> Optional[str]:
        return self.context.token_for(self.store.url, self.store.token) if self.store else None

    # --- Reading --------------------------------------------------------------------------------

    def reload(self, then=None) -> None:
        """Reads the history of the days ahead, their shots and the ledger (in the background), then works the lines out."""
        if self.store is None:
            return
        if self.task is not None:
            self._again = True
            return
        store, client, token = self.store, self.context.esri(), self.token()
        first = jst_day(_now_ms())
        n = self.days()
        last = (first + timedelta(days=n - 1)).isoformat()
        self.status.setText(self.tr("{a}〜{b} の計画と依頼を読み込み中…").format(a=first.isoformat(), b=last))

        def work(task):
            rows = load_days(client, store, token, first.isoformat(), last, task.isCanceled)
            shots, problem = [], ""
            if store.shots:
                try:
                    shots = load_shots_where(client, store, token, shot_days_where(store, first.isoformat(), last), geometry=True,
                                             cancelled=task.isCanceled)
                except EsriError as error:
                    # The requests' rows matter most: shown without the shots.
                    problem = str(error)
            requests = load_requests(client, store, token, task.isCanceled)
            return rows, shots, requests, problem

        def done(result):
            self.task = None
            rows, shots, requests, problem = result
            self.rows, self.requests, self.shot_problem = rows, requests, problem
            self.shots = {s.shot_id: s for s in shots if s.shot_id}
            self.first, self.read_days, self._loaded = first, n, True
            self._update_buttons()
            if self._again:
                self._again = False
                self.reload(then)
                return
            self.refit()
            if then:
                then()

        def failed(message):
            self.task = None
            self._update_buttons()
            self.status.setText(message)
            if self._again:
                self._again = False
                self.reload(then)

        self.task = run_task(self.tr("地点ごとの撮像予定の読み込み"), work, done, failed)
        self._update_buttons()

    def _update_buttons(self) -> None:
        self.reload_button.setEnabled(self.store is not None and self.task is None)
        self.clear_button.setEnabled(bool(self.clicked))

    # --- The places -----------------------------------------------------------------------------

    def _layer_changed(self, layer) -> None:
        if self._watched is not None:
            try:
                self._watched.selectionChanged.disconnect(self._selection_changed)
            except (TypeError, RuntimeError):
                pass
        self._watched = layer if isinstance(layer, QgsVectorLayer) else None
        self.name_field.setLayer(layer)
        if self._watched is not None:
            names = [f.name() for f in self._watched.fields()]
            hints = ("name", "名称", "名前", "地名", "施設名", "title")
            self.name_field.blockSignals(True)
            self.name_field.setField(next((n for hint in hints for n in names if n.lower() == hint), "")
                                     or next((n for n in names if "名" in n or "name" in n.lower()), ""))
            self.name_field.blockSignals(False)
            self._watched.selectionChanged.connect(self._selection_changed)
        self.refit()

    def _selection_changed(self, *_args) -> None:
        if self.selected_only.isChecked():
            self.refit()

    def set_layer(self, layer: Optional[QgsVectorLayer], name_field: Optional[str] = None, selected_only: bool = False) -> None:
        """Shows a layer's features (a line each; `name_field` names them, `selected_only` the selected ones)."""
        self.layer_choice.setLayer(layer)
        if name_field is not None:
            self.name_field.setField(name_field)
        self.selected_only.setChecked(selected_only)
        self.refit()

    def layer_places(self) -> list[Place]:
        """The chosen layer's features as places (at most MOST_PLACES with the points clicked; `cut` when more)."""
        layer = self.layer_choice.currentLayer()
        if not isinstance(layer, QgsVectorLayer) or not layer.isValid():
            return []
        room = MOST_PLACES - len(self.clicked)
        if room <= 0:
            self.cut = True
            return []
        transform = to_wgs84(layer.crs())
        field = self.name_field.currentField()
        request = QgsFeatureRequest()
        request.setLimit(room + 1)
        if not field:
            request.setNoAttributes()
        if self.selected_only.isChecked():
            request.setFilterFids(layer.selectedFeatureIds())
        out = []
        for f in layer.getFeatures(request):
            if len(out) >= room:
                self.cut = True
                break
            name = _text(f.attribute(field)) if field else ""
            place = place_of(f.geometry(), transform, name or f"{layer.name()} {f.id()}", f"{layer.id()}:{f.id()}")
            if place is not None:
                out.append(place)
        return out

    def places(self) -> list[Place]:
        """The places shown: those clicked first, then the layer's features."""
        self.cut = False
        return [*self.clicked, *self.layer_places()]

    def add_point(self, lon: float, lat: float, name: str = "") -> Place:
        """Adds a point (WGS84) as a line of its own (shown at the top, after those clicked before)."""
        self._clicks += 1
        place = Place(name or self.tr("地点 {n}").format(n=self._clicks), points=[[lon, lat]], key=f"click-{self._clicks}",
                      note=self.tr("緯度 {y:.5f}・経度 {x:.5f}").format(y=lat, x=lon))
        self.clicked.append(place)
        self._draw_points()
        self._update_buttons()
        self.refit()
        return place

    def clear_points(self) -> None:
        """Takes the points clicked off (the layer's features stay)."""
        self.clicked = []
        self._clicks = 0
        self._draw_points()
        self._update_buttons()
        self.refit()

    def _draw_points(self) -> None:
        if not self.isVisible():
            return
        self.points_overlay.numbered_points([(p.points[0][0], p.points[0][1], str(i + 1)) for i, p in enumerate(self.clicked)],
                                            POINT_COLOR)
        self.canvas.refresh()

    def _pick_toggled(self, on: bool) -> None:
        if on:
            self._previous_tool = self.canvas.mapTool()
            self.canvas.setMapTool(self.point_tool)
        elif self.canvas.mapTool() is self.point_tool:
            self.canvas.unsetMapTool(self.point_tool)
            if self._previous_tool is not None:
                self.canvas.setMapTool(self._previous_tool)

    def _map_clicked(self, point: QgsPointXY, _button=None) -> None:
        p = to_wgs84(self.canvas.mapSettings().destinationCrs()).transform(point)
        self.add_point(p.x(), p.y())

    # --- The lines ------------------------------------------------------------------------------

    def refit(self) -> None:
        """Works out each place's line again from what was read (no reading)."""
        places = self.places()
        now = _now_ms()
        n = min(self.days(), self.read_days) if self._loaded else self.days()
        if self._loaded and places:
            areas = {q.id: q.rings for q in self.requests}
            lanes = lanes_of(places, self.rows, self.shots, self.requests, self.first, n, now, areas=areas,
                             not_taken=self.not_taken.isChecked(), share=GeometryShares())
        else:
            lanes = [Lane(p) for p in places] if self._loaded else []
        self.lanes = sort_by_next(lanes) if self.by_next.isChecked() else lanes
        start, end = span_of(self.first, n)
        self.timeline.set(self.lanes, start, end, now, self._empty_text())
        self._status()
        if self.timeline.chosen is None:
            self.overlay.unpaint()
            self.chosen_label.clear()
            if self.isVisible():
                self.canvas.refresh()

    def _empty_text(self) -> str:
        if self.store is None:
            return self.tr("設定ファイルに撮像依頼の保存先（requestStores）なし")
        if not self.clicked and self.layer_choice.currentLayer() is None:
            return self.tr("上でレイヤーを選ぶか、「地図で地点を追加」で地図をクリックすると、その地点のこの先の撮像計画が並ぶ")
        if not self._loaded:
            return self.tr("読み込み中…")
        return self.tr("地点なし（選択中の地物がない、または形のない地物だけ）")

    def _status(self) -> None:
        if self.store is None or not self._loaded:
            return
        n = min(self.days(), self.read_days)
        last = self.first + timedelta(days=n - 1)
        text = f"{self.first.month}/{self.first.day}〜{last.month}/{last.day}: " + tally_text(self.lanes)
        if self.cut:
            text += self.tr("（地点は最初の {n} 件まで）").format(n=MOST_PLACES)
        if not self.store.history:
            text += self.tr("。出力履歴（history）がないため依頼の日だけ")
        if self.shot_problem:
            text += self.tr("。撮像の範囲を読めず（{m}）").format(m=self.shot_problem)
        self.status.setText(text)

    def _moved_on(self) -> None:
        """Every minute: 「次」 and the line of now move on (a new day: read again)."""
        if not self.isVisible():
            return
        if self._loaded and self.first != jst_day(_now_ms()):
            self.reload()
            return
        now = _now_ms()
        for lane in self.lanes:
            lane.next = next_of(lane.marks, now)
        if self.by_next.isChecked():
            self.lanes = sort_by_next(self.lanes)
        start, end = span_of(self.first, min(self.days(), self.read_days) if self._loaded else self.days())
        self.timeline.set(self.lanes, start, end, now, self._empty_text())
        self._status()

    # --- The map --------------------------------------------------------------------------------

    def _picked(self, mark: Mark, index: int) -> None:
        if not 0 <= index < len(self.lanes):
            return
        place = self.lanes[index].place
        self.chosen_label.setText(f"{place.name}: {mark_name(mark)}（{mark.state}）")
        self.draw(mark, place)

    def draw(self, mark: Mark, place: Place) -> None:
        """Draws a plan's area (and its shot, dashed), or a request day's areas, over the place; goes there when out of view."""
        polygons = []
        if mark.row is not None:
            rings = mark.row.rings or next((q.rings for q in self.requests if q.id == mark.row.request_id), [])
            if rings:
                polygons.append((rings, COLORS.get(mark.state, "#64748b"), 2, mark_name(mark)))
            if mark.shot is not None and mark.shot.rings:
                polygons.append((mark.shot.rings, SHOT_COLOR, 2, "", True))
        else:
            for q in mark.requests:
                polygons.append((q.rings, COLORS[REQUEST], 2, q.name or q.id))
        if place.rings:
            polygons.append((place.rings, "#111827", 2, "", True))
        self.overlay.paint(polygons)
        box = self._box([ring for p in polygons for ring in p[0]] + [place.coords()])
        if box is not None:
            view = to_wgs84(self.canvas.mapSettings().destinationCrs()).transformBoundingBox(self.canvas.extent())
            if not view.contains(box):
                self._zoom(box)
        self.canvas.refresh()

    @staticmethod
    def _box(rings) -> Optional[QgsRectangle]:
        b = bbox_of(rings)
        return QgsRectangle(*b) if b is not None else None

    def _zoom(self, box: QgsRectangle) -> None:
        box = box.buffered(max(box.width(), box.height()) * 0.2 + 0.01)
        self.canvas.setExtent(from_wgs84(self.canvas.mapSettings().destinationCrs()).transformBoundingBox(box))

    def go_to(self, index: int) -> None:
        """Goes to a line's place on the map (a point: the map moved to it at its scale)."""
        if not 0 <= index < len(self.lanes):
            return
        place = self.lanes[index].place
        box = place.box
        if box is None:
            return
        if place.rings or place.lines or len(place.points) > 1:
            self._zoom(QgsRectangle(*box))
        else:
            p = from_wgs84(self.canvas.mapSettings().destinationCrs()).transform(QgsPointXY(box[0], box[1]))
            self.canvas.setCenter(p)
        self.canvas.refresh()

    def close_all(self) -> None:
        """Takes everything off the map (when the plugin is unloaded)."""
        self._tick.stop()
        if self.pick.isChecked():
            self.pick.setChecked(False)
        if self._watched is not None:
            try:
                self._watched.selectionChanged.disconnect(self._selection_changed)
            except (TypeError, RuntimeError):
                pass
            self._watched = None
        self.overlay.remove()
        self.points_overlay.remove()
