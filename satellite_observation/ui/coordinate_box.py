"""
Coordinates in and out, as the viewer has them (jump.ts, geocode.ts,
cursor-position.ts, coordinate-menu.ts):

- 「座標・地名へ移動」: a field on its own toolbar that reads latitude /
  longitude, MGRS or UTM (core.coords.parse_coordinate), or a place name or
  address (the settings file's `geocoders`), centers the map there and marks
  the spot until the next jump. While a name is typed, the places found show
  in a list under the field (↑↓ and Enter, or a click).
- The cursor position in the status bar, in the notation chosen (10進・
  度分秒・MGRS・UTM), kept for the next time.
- In the map's right-click menu: the point's latitude / longitude, MGRS and
  UTM (and the map's own CRS) to copy, and the notation of the status bar.
"""
from __future__ import annotations

import os
import re
from typing import Optional
from urllib.parse import unquote, urljoin, urlparse

from qgis.core import QgsCoordinateReferenceSystem, QgsCoordinateTransform, QgsPointXY, QgsProject, QgsRectangle, QgsSettings
from qgis.gui import QgsVertexMarker
from qgis.PyQt.QtCore import QEvent, QModelIndex, QObject, Qt, QTimer
from qgis.PyQt.QtGui import QColor, QGuiApplication, QStandardItem, QStandardItemModel
from qgis.PyQt.QtWidgets import QCompleter, QLabel, QLineEdit, QMenu, QToolBar

try:  # Qt6
    from qgis.PyQt.QtGui import QAction, QActionGroup
except ImportError:  # Qt5
    from qgis.PyQt.QtWidgets import QAction, QActionGroup

from ..core.coords import NOTATIONS, LonLat, format_as, format_lat_lon, format_mgrs, format_utm, parse_coordinate, utm_epsg, utm_zone
from ..core.geocode import Place, PlaceSearch
from ..qgis_io import settings
from ..qgis_io.context import PluginContext
from ..qgis_io.network import get_bytes
from ..qgis_io.tasks import run_task

WGS84 = QgsCoordinateReferenceSystem("EPSG:4326")
#: The scale the map goes to at least when jumping: the viewer's zoom 16 in Web Mercator (about 2.4 m per pixel).
JUMP_SCALE = 8530.9
#: The largest scale a place's extent is fitted at: the viewer's zoom 18.
FIT_SCALE = 2132.7
#: Pixels kept around a place's extent.
PADDING = 40
#: How long typing pauses before places are looked up (ms).
PAUSE = 350
NOTATION_KEY = "satellite_observation/cursor_notation"
MARK = QColor("#2563eb")


def cursor_notation() -> str:
    value = QgsSettings().value(NOTATION_KEY, "decimal")
    return value if any(value == n for n, _, _ in NOTATIONS) else "decimal"


def set_cursor_notation(notation: str) -> None:
    QgsSettings().setValue(NOTATION_KEY, notation)


def wrap_lon(lon: float) -> float:
    """The longitude within ±180° (the map repeats the world sideways)."""
    return (((lon + 180) % 360) + 360) % 360 - 180


def coordinate_items(lon_lat: LonLat, crs: Optional[QgsCoordinateReferenceSystem] = None,
                     at: Optional[QgsPointXY] = None) -> list[tuple[str, str]]:
    """The ways to copy a point (label, text): latitude / longitude, MGRS, UTM and the map's own CRS."""
    items = [("緯度, 経度", format_lat_lon(lon_lat))]
    mgrs = format_mgrs(lon_lat)
    if mgrs:
        items.append(("MGRS", mgrs))
    zone, utm = utm_zone(lon_lat), format_utm(lon_lat)
    utm_code = None
    if zone is not None and utm:
        items.append(("UTM", utm))
        utm_code = utm_epsg(zone, lon_lat[1] >= 0)
    # The map's own CRS, unless it is WGS 84 or the UTM zone already listed.
    if crs is not None and at is not None and crs.isValid():
        code = crs.authid()
        if code and code not in ("EPSG:4326", "OGC:CRS84", utm_code):
            digits = 6 if crs.isGeographic() else 2
            items.append((code, f"{at.x():.{digits}f}, {at.y():.{digits}f}"))
    return items


class CoordinateBox(QObject):
    """The jump field, the cursor position and the map's right-click coordinates (one per plugin)."""

    def __init__(self, iface, context: PluginContext, parent: Optional[QObject] = None):
        super().__init__(parent)
        self.iface = iface
        self.context = context
        self.canvas = iface.mapCanvas()
        self.places_found: list[Place] = []
        self._shown_for = ""
        self._task = None
        self._marker: Optional[QgsVertexMarker] = None
        #: The last message shown (for tests and the console).
        self.message = ""
        self.search = self._place_search()

        # The field, on its own toolbar.
        self.toolbar: QToolBar = iface.addToolBar(self.tr("座標・地名へ移動"))
        self.toolbar.setObjectName("SatelliteObservationJumpToolbar")
        self.field = QLineEdit()
        self.field.setObjectName("SatObsJumpField")
        self.field.setClearButtonEnabled(True)
        self.field.setMinimumWidth(260)
        self.field.setMaximumWidth(420)
        self.field.returnPressed.connect(self.submit)
        self.field.textEdited.connect(self._typed)
        self.field.installEventFilter(self)
        self.toolbar.addWidget(self.field)
        self.go = QAction(self.tr("移動"), self.toolbar)
        self.go.triggered.connect(self.submit)
        self.toolbar.addAction(self.go)

        # The places found, in a list under the field.
        self.model = QStandardItemModel(self)
        self.completer = QCompleter(self.model, self)
        self.completer.setWidget(self.field)
        self.completer.setCompletionMode(QCompleter.CompletionMode.UnfilteredPopupCompletion)
        self.completer.setMaxVisibleItems(12)
        self.completer.activated[QModelIndex].connect(self._activated)
        self.timer = QTimer(self)
        self.timer.setSingleShot(True)
        self.timer.setInterval(PAUSE)
        self.timer.timeout.connect(self._look_up)

        # The cursor position in the status bar.
        self.notation = cursor_notation()
        self.position = QLabel()
        self.position.setObjectName("SatObsCursorPosition")
        self.position.setToolTip(self.tr("カーソル位置（WGS 84）。右クリックで表記の切り替え"))
        self.position.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.position.customContextMenuRequested.connect(
            lambda pos: self.notation_menu(QMenu(self.position)).exec(self.position.mapToGlobal(pos)))
        self.position.hide()
        status = iface.statusBarIface()
        status.addPermanentWidget(self.position, 0)
        self.canvas.xyCoordinates.connect(self.show_position)
        self.canvas.viewport().installEventFilter(self)

        self.canvas.contextMenuAboutToShow.connect(self._canvas_menu)
        context.configChanged.connect(self._config_changed)
        self._config_changed()

    # --- Settings ---------------------------------------------------------

    def _resolve(self, url: str) -> str:
        """A geocoder's URL as asked: a relative one against the settings file's place."""
        source = self.context.source or ""
        if re.match(r"^https?://", url, re.IGNORECASE):
            return url
        if url.startswith("//"):
            scheme = urlparse(source).scheme if re.match(r"^https?://", source, re.IGNORECASE) else "https"
            return f"{scheme}:{url}"
        if re.match(r"^https?://", source, re.IGNORECASE):
            return urljoin(source, url)
        base = os.path.dirname(source) if source else settings.PLUGIN_DIR
        return os.path.normpath(url if os.path.isabs(url) else os.path.join(base, url))

    def _get_bytes(self, url: str) -> bytes:
        if re.match(r"^https?://", url, re.IGNORECASE):
            return get_bytes(url, settings.auth_for(url))
        path = unquote(urlparse(url).path) if url.startswith("file:") else url
        try:
            with open(path, "rb") as f:
                return f.read()
        except OSError as error:
            raise OSError(f"{path} の読み込み不可（{error.strerror or error}）") from error

    def _ask_esri(self, url: str, params: dict, token: Optional[str]) -> dict:
        return self.context.esri().ask(url, params, self.context.token_for(url, token))

    def _place_search(self) -> PlaceSearch:
        return PlaceSearch(self.context.config.geocoders, self._get_bytes, self._ask_esri, self._resolve)

    def _config_changed(self) -> None:
        self.search = self._place_search()
        self.hide_places()
        if self.search.enabled:
            self.field.setPlaceholderText(self.tr("緯度経度・MGRS・UTM・地名・住所"))
            self.field.setToolTip(self.tr("座標・地名へ移動"))
        else:
            self.field.setPlaceholderText(self.tr("緯度経度・MGRS・UTM"))
            self.field.setToolTip(self.tr("座標へ移動"))

    # --- Saying -----------------------------------------------------------

    def say(self, message: str) -> None:
        self.message = message
        self.iface.statusBarIface().showMessage(message, 5000)

    def warn(self, message: str) -> None:
        self.message = message
        self.iface.messageBar().pushInfo(self.tr("座標・地名へ移動"), message)

    # --- Jumping ----------------------------------------------------------

    def jump(self, text: str) -> Optional[LonLat]:
        """Goes to the coordinate in `text`; None (with a message) when it cannot be read."""
        lon_lat = parse_coordinate(text)
        if lon_lat is None:
            self.warn(self.tr("座標として読み込み失敗: {text}（緯度経度・MGRS・UTM に対応）").format(text=text))
            return None
        self._go_to(lon_lat)
        self.say(self.tr("移動: {at}").format(at=format_lat_lon(lon_lat)))
        return lon_lat

    def go_to_place(self, place: Place) -> None:
        """Goes to a place found by name: its extent when known, else its point."""
        self.hide_places()
        self._go_to(place.lon_lat, place.extent)
        self.field.setText(place.name)
        self.field.selectAll()
        self.say(self.tr("移動: {name}（{source}）").format(name=place.name, source=place.source))

    @property
    def marked(self) -> Optional[QgsPointXY]:
        """Where the mark is, in the map's CRS (None before the first jump)."""
        return self._marker.center() if self._marker is not None else None

    def _go_to(self, lon_lat: LonLat, extent=None) -> None:
        canvas = self.canvas
        to_map = QgsCoordinateTransform(WGS84, canvas.mapSettings().destinationCrs(), QgsProject.instance())
        center = to_map.transform(QgsPointXY(*lon_lat))
        if self._marker is None:
            self._marker = QgsVertexMarker(canvas)
            self._marker.setIconType(QgsVertexMarker.IconType.ICON_CIRCLE)
            self._marker.setIconSize(18)
            self._marker.setPenWidth(3)
            self._marker.setColor(MARK)
            fill = QColor(MARK)
            fill.setAlpha(0x33)
            self._marker.setFillColor(fill)
            self._marker.setZValue(10_001)
        self._marker.setCenter(center)
        self._marker.show()
        if extent and extent[2] > extent[0] and extent[3] > extent[1]:
            box = to_map.transformBoundingBox(QgsRectangle(*extent))
            pad = PADDING * canvas.mapUnitsPerPixel()
            box.grow(pad)
            canvas.setExtent(box)
            if canvas.scale() < FIT_SCALE:
                canvas.zoomScale(FIT_SCALE)
        else:
            # A map with nothing on it yet has no extent (its scale reads 1:1): zoom there all the same.
            empty = canvas.extent().isEmpty() or canvas.extent().width() == 0
            if empty:
                canvas.setExtent(QgsRectangle(center.x() - 1, center.y() - 1, center.x() + 1, center.y() + 1))
            canvas.setCenter(center)
            if empty or canvas.scale() > JUMP_SCALE:
                canvas.zoomScale(JUMP_SCALE)
        canvas.refresh()

    def submit(self) -> None:
        """Enter: the chosen place, a coordinate, else the first place found for the text."""
        text = self.field.text().strip()
        if not text:
            return
        popup = self.completer.popup()
        if popup.isVisible() and self._shown_for == text and popup.currentIndex().isValid():
            return self._activated(popup.currentIndex())
        if parse_coordinate(text) is not None or not self.search.enabled:
            self.hide_places()
            if self.jump(text):
                self.field.selectAll()
            return
        self.timer.stop()
        if self._shown_for == text and self.places_found:
            return self.go_to_place(self.places_found[0])
        self._look_up(text, go=True)

    # --- Places -----------------------------------------------------------

    def _typed(self, _text: str = "") -> None:
        """While typing a name: look it up once typing pauses."""
        self.timer.stop()
        text = self.field.text().strip()
        if not self.search.enabled or not text or parse_coordinate(text) is not None or len(text) < 2:
            self.hide_places()
            return
        self.timer.start()

    def _look_up(self, text: Optional[str] = None, go: bool = False) -> None:
        text = (text or self.field.text()).strip()
        if not text:
            return
        if self._task is not None:
            try:
                self._task.cancel()
            except RuntimeError:  # already finished and deleted
                pass
        search = self.search

        def work(task):
            return search.search(text, task.isCanceled)

        def done(result):
            places, errors = result
            if self.field.text().strip() != text:
                return
            if go:
                if places:
                    self.go_to_place(places[0])
                elif errors:
                    self.warn(self.tr("地名の検索に失敗（{errors}）").format(errors="、".join(errors)))
                else:
                    self.warn(self.tr("該当なし: {text}（緯度経度・MGRS・UTM・地名・住所に対応）").format(text=text))
                return
            if not places and errors:
                self.warn(self.tr("地名の検索に失敗（{errors}）").format(errors="、".join(errors)))
            self.show_places(text, places)

        self._task = run_task(self.tr("地名の検索"), work, done)

    def show_places(self, text: str, places: list[Place]) -> None:
        self.places_found = places
        self._shown_for = text
        self.model.clear()
        for i, p in enumerate(places):
            item = QStandardItem("　".join(x for x in (p.name, p.source, p.detail) if x))
            item.setData(i, Qt.ItemDataRole.UserRole)
            item.setToolTip("\n".join(x for x in (p.name, p.detail, p.source) if x))
            item.setEditable(False)
            self.model.appendRow(item)
        if not places:
            none = QStandardItem(self.tr("該当なし"))
            none.setEnabled(False)
            none.setSelectable(False)
            self.model.appendRow(none)
        if not self.field.isVisible():
            return
        width = max(self.field.width(), 360)
        self.completer.complete(self.field.rect().adjusted(0, 0, width - self.field.width(), 0))
        self.completer.popup().setCurrentIndex(QModelIndex())

    def hide_places(self) -> None:
        self.timer.stop()
        self.completer.popup().hide()

    def _activated(self, index: QModelIndex) -> None:
        i = index.data(Qt.ItemDataRole.UserRole)
        if isinstance(i, int) and 0 <= i < len(self.places_found):
            self.go_to_place(self.places_found[i])

    def eventFilter(self, watched, event) -> bool:  # noqa: N802 (Qt's name)
        if watched is self.field and event.type() == QEvent.Type.KeyPress and event.key() == Qt.Key.Key_Escape:
            self.hide_places()
        elif watched is self.canvas.viewport() and event.type() == QEvent.Type.Leave:
            self.position.hide()
        return False

    # --- The cursor position ----------------------------------------------

    def text_at(self, point: QgsPointXY) -> Optional[str]:
        """The text shown for a map point (map CRS), or None to show nothing."""
        crs = self.canvas.mapSettings().destinationCrs()
        if not crs.isValid():
            return None
        try:
            p = QgsCoordinateTransform(crs, WGS84, QgsProject.instance()).transform(point)
        except Exception:  # noqa: BLE001 (QgsCsException: outside the CRS)
            return None
        lon, lat = p.x(), p.y()
        if not (abs(lat) <= 90 and lon == lon and abs(lon) != float("inf")):
            return None
        return format_as(self.notation, (wrap_lon(lon), lat))

    def show_position(self, point: QgsPointXY) -> None:
        text = self.text_at(point)
        self.position.setVisible(text is not None)
        if text is not None:
            self.position.setText(text)

    def set_notation(self, notation: str) -> None:
        self.notation = notation
        set_cursor_notation(notation)

    def notation_menu(self, menu: QMenu) -> QMenu:
        """「ステータスバーの表示」: one choice per notation of the cursor position."""
        group = QActionGroup(menu)
        for id_, label, _ in NOTATIONS:
            action = menu.addAction(label)
            action.setCheckable(True)
            action.setChecked(id_ == self.notation)
            action.setToolTip(self.tr("カーソル位置を{label}で表示").format(label=label))
            action.triggered.connect(lambda _=False, n=id_: self.set_notation(n))
            group.addAction(action)
        return menu

    # --- The map's right-click menu ---------------------------------------

    def _canvas_menu(self, menu: QMenu, event) -> None:
        point = event.mapPoint()
        crs = self.canvas.mapSettings().destinationCrs()
        try:
            p = QgsCoordinateTransform(crs, WGS84, QgsProject.instance()).transform(point)
        except Exception:  # noqa: BLE001 (outside the CRS)
            return
        if not abs(p.y()) <= 90:
            return
        lon_lat = (wrap_lon(p.x()), p.y())
        menu.addSeparator()
        copy = menu.addMenu(self.tr("緯度経度・MGRS・UTM のコピー"))
        for label, value in coordinate_items(lon_lat, crs, point):
            action = copy.addAction(f"{label}　{value}")
            action.setToolTip(self.tr("{label} をコピー").format(label=label))
            action.triggered.connect(lambda _=False, lb=label, v=value: self.copy(lb, v))
        self.notation_menu(menu.addMenu(self.tr("ステータスバーの表示")))

    def copy(self, label: str, value: str) -> None:
        QGuiApplication.clipboard().setText(value)
        self.say(self.tr("{label} をコピー: {value}").format(label=label, value=value))

    # --- Closing ----------------------------------------------------------

    def close(self) -> None:
        for signal, slot in ((self.canvas.xyCoordinates, self.show_position), (self.canvas.contextMenuAboutToShow, self._canvas_menu),
                             (self.context.configChanged, self._config_changed)):
            try:
                signal.disconnect(slot)
            except (TypeError, RuntimeError):
                pass
        self.canvas.viewport().removeEventFilter(self)
        self.timer.stop()
        if self._task is not None:
            try:
                self._task.cancel()
            except RuntimeError:
                pass
        self.completer.popup().hide()
        if self._marker is not None:
            self.canvas.scene().removeItem(self._marker)
            self._marker = None
        self.iface.statusBarIface().removeWidget(self.position)
        self.position.deleteLater()
        self.toolbar.deleteLater()
