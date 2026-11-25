"""
地図の投影法と向きを開いた画像に合わせる (tsudo, 2026-11-25; core/map_view.py):
the map put in an opened image's CRS and turned so that the way it was
taken is up, and put back when those images are closed.

`MapOrienter.orient` (the catalog dock, after images open): the first
image's CRS (「投影法を画像に合わせる」) and the rotation of 「向き」. The
CRS and rotation the map had before are kept the first time, and put back
once every image it was done for has left the project; a CRS or rotation
the person changes meanwhile (QGIS's own boxes, the north arrow) is theirs
and is not put back. A project opened or cleared forgets it all.

`MapOrienter.turn` (画像補正ドック「地図の向き」, tsudo 2026-11-26): the map
turned for one image as a button asks, its CRS changed only when it is in
degrees; the map left as it is (and why) when the way is not known.

One MapOrienter for a map (`shared_orienter`): the docks share it, so there
is one north arrow and one map to put back.

`NorthArrow`: while the map is turned (by anyone), an arrow in its top
right corner shows where north is; a click turns the map back north up.
"""
from __future__ import annotations

import math
from typing import Callable, Optional

from qgis.core import QgsCoordinateReferenceSystem, QgsCoordinateTransform, QgsPointXY, QgsProject
from qgis.PyQt.QtCore import QEvent, QObject, QPointF, QSize, Qt, pyqtSignal
from qgis.PyQt.QtGui import QColor, QPainter, QPen, QPolygonF
from qgis.PyQt.QtWidgets import QToolButton

from ..core.map_view import ORIENTATION_LABELS, bearing_up, rotation_for, turned_text
from ..qgis_io.geometry import WGS84
from ..qgis_io.map_view_io import centre_of, crs_for, directions_of, fitted_extent, grid_bearing

#: Rotations closer to 0 than this are north up.
_STILL = 0.05
#: The maps' orienters (by the canvas), while a dock holds them (`shared_orienter`).
_SHARED: dict = {}


class NorthArrow(QToolButton):
    """The arrow to north over a turned map (top right); a click puts north up."""

    SIZE = 44

    def __init__(self, canvas):
        super().__init__(canvas)
        self.canvas_ = canvas
        self.setObjectName("MikageNorthArrow")
        self.setFixedSize(QSize(self.SIZE, self.SIZE))
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setAutoRaise(True)
        self.clicked.connect(self.north_up)
        canvas.installEventFilter(self)
        canvas.rotationChanged.connect(self.update_state)
        canvas.destinationCrsChanged.connect(self.update_state)
        canvas.extentsChanged.connect(self.update)
        self.update_state()

    def eventFilter(self, watched, event) -> bool:  # noqa: N802 (Qt's name)
        if watched is self.canvas_ and event.type() == QEvent.Type.Resize:
            self._place()
        return False

    def _place(self) -> None:
        self.move(max(0, self.canvas_.width() - self.SIZE - 10), 10)

    def update_state(self, *_args) -> None:
        turned = abs(self.canvas_.rotation()) > _STILL
        self.setToolTip(self.tr("北はこの向き（地図の回転 {r}）。クリックで北が上に戻る").format(r=turned_text(self.canvas_.rotation())))
        self._place()
        self.setVisible(turned)
        self.raise_()
        self.update()

    def north_up(self) -> None:
        self.canvas_.setRotation(0)
        self.canvas_.refresh()

    def north_angle(self) -> float:
        """Where north is on the screen (degrees clockwise from the top) at the map's middle."""
        s = self.canvas_.mapSettings()
        try:
            c = s.visibleExtent().center()
            to_wgs = QgsCoordinateTransform(s.destinationCrs(), WGS84, QgsProject.instance())
            back = QgsCoordinateTransform(WGS84, s.destinationCrs(), QgsProject.instance())
            g = to_wgs.transform(c)
            n = back.transform(QgsPointXY(g.x(), min(89.9, g.y() + 0.001)))
            m = s.mapToPixel()
            a, b = m.transform(c), m.transform(n)
            return math.degrees(math.atan2(b.x() - a.x(), -(b.y() - a.y())))
        except Exception:  # noqa: BLE001 (outside the CRS: the rotation alone)
            return self.canvas_.rotation()

    def paintEvent(self, event) -> None:  # noqa: N802 (Qt's name)
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        r = self.SIZE / 2
        painter.translate(r, r)
        painter.setPen(QPen(QColor("#64748b"), 1))
        painter.setBrush(QColor(255, 255, 255, 220))
        painter.drawEllipse(QPointF(0, 0), r - 2, r - 2)
        painter.rotate(self.north_angle())
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QColor("#dc2626"))
        painter.drawPolygon(QPolygonF([QPointF(0, -r + 6), QPointF(6, 2), QPointF(-6, 2)]))
        painter.setBrush(QColor("#94a3b8"))
        painter.drawPolygon(QPolygonF([QPointF(0, r - 10), QPointF(6, 2), QPointF(-6, 2)]))
        painter.end()


class MapOrienter(QObject):
    """
    Puts the map in an opened image's CRS and turns it (`orient`), and puts
    it back when those images are closed. `said(text)`: what it did.
    """

    said = pyqtSignal(str)
    #: The map was turned or put back by it (the docks show the way up).
    turned = pyqtSignal()

    def __init__(self, canvas, project: Optional[QgsProject] = None, parent: Optional[QObject] = None):
        super().__init__(parent)
        self.canvas = canvas
        self.project = project or QgsProject.instance()
        #: The map as it was before images turned it: {"crs": …, "rotation": …} (a value None: the person's since); None for none kept.
        self.before: Optional[dict] = None
        #: The layers it was done for (their ids): when the last of them goes, the map is put back.
        self.layer_ids: set[str] = set()
        self._busy = False
        #: The way up it last put the map (ORIENTATIONS' key) and the rotation for it; None when not, or turned since.
        self.way: Optional[tuple[str, float]] = None
        #: The docks holding it (`shared_orienter`).
        self.users: set[int] = set()
        self.arrow = NorthArrow(canvas)
        self.project.layersWillBeRemoved.connect(self._removed)
        self.project.removeAll.connect(self.forget)
        self.project.readProject.connect(self.forget)
        canvas.destinationCrsChanged.connect(self._crs_changed)
        canvas.rotationChanged.connect(self._rotation_changed)

    @property
    def busy(self) -> bool:
        """Whether it is changing the map now (a CRS or rotation change seen meanwhile is its own)."""
        return self._busy

    def current_way(self) -> Optional[str]:
        """The way up the map shows now: "north" for none turned, the one it put otherwise, None when turned otherwise."""
        r = self.canvas.rotation()
        if abs(r) <= _STILL:
            return "north"
        if self.way is not None and abs(self.way[1] - r) < 0.01:
            return self.way[0]
        return None

    def unload(self) -> None:
        for signal, slot in ((self.project.layersWillBeRemoved, self._removed), (self.project.removeAll, self.forget),
                             (self.project.readProject, self.forget), (self.canvas.destinationCrsChanged, self._crs_changed),
                             (self.canvas.rotationChanged, self._rotation_changed)):
            try:
                signal.disconnect(slot)
            except (TypeError, RuntimeError):
                pass
        try:
            self.canvas.removeEventFilter(self.arrow)
            self.arrow.deleteLater()
        except RuntimeError:
            pass

    # ---- the person's own changes, and the images going ----

    def forget(self, *_args) -> None:
        self.before = None
        self.layer_ids.clear()

    def _crs_changed(self) -> None:
        if not self._busy and self.before is not None:
            self.before["crs"] = None
            self._forget_if_nothing()

    def _rotation_changed(self, *_args) -> None:
        if not self._busy:
            self.way = None
        if not self._busy and self.before is not None:
            self.before["rotation"] = None
            self._forget_if_nothing()

    def _forget_if_nothing(self) -> None:
        if self.before is not None and self.before.get("crs") is None and self.before.get("rotation") is None:
            self.forget()

    def _removed(self, ids) -> None:
        if not self.layer_ids:
            return
        self.layer_ids -= set(ids)
        if not self.layer_ids and self.before is not None:
            self.put_back()

    def put_back(self) -> None:
        """The CRS and rotation the map had before images changed them (those the person did not change since)."""
        before, self.before = self.before, None
        self.layer_ids.clear()
        if not before:
            return
        crs, rotation = before.get("crs"), before.get("rotation")
        self.way = None
        self._set(crs if crs is not None and crs.isValid() else None, rotation)
        self.turned.emit()
        if crs is not None or rotation is not None:
            self.said.emit(self.tr("開いた画像を閉じたので、地図の投影法と向きを元に戻した"))

    def _set(self, crs: Optional[QgsCoordinateReferenceSystem], rotation: Optional[float]) -> None:
        self._busy = True
        try:
            if crs is not None and crs != self.canvas.mapSettings().destinationCrs():
                self.project.setCrs(crs)
                if self.canvas.mapSettings().destinationCrs() != crs:
                    self.canvas.setDestinationCrs(crs)
            if rotation is not None and abs(self.canvas.rotation() - rotation) > 1e-9:
                self.canvas.setRotation(rotation)
        finally:
            self._busy = False
        self.canvas.refresh()

    # ---- an image opened ----

    def _bearing(self, layer, orientation: str, found_of, typed_of) -> tuple[Optional[float], str]:
        """The true bearing to put up for `layer` (core/map_view.bearing_up) and where it came from, or None and why."""
        return bearing_up(orientation, directions_of(layer, found_of(layer), typed_of(layer), orientation))

    def _keep_before(self) -> None:
        """The map as it is kept to put back (what the person changed since it was kept: as it is now)."""
        now = {"crs": QgsCoordinateReferenceSystem(self.canvas.mapSettings().destinationCrs()), "rotation": self.canvas.rotation()}
        if self.before is None:
            self.before = now
        else:
            for key, value in now.items():
                if self.before.get(key) is None:
                    self.before[key] = value

    def _turn_to(self, centre, crs, bearing: float, orientation: str) -> None:
        """The map in `crs` (None: as it is) turned so the true `bearing` is up at `centre`."""
        shown_in = crs if crs is not None else self.canvas.mapSettings().destinationCrs()
        grid = grid_bearing(shown_in, centre[0], centre[1], bearing, self.project)
        rotation = rotation_for(grid if grid is not None else bearing)
        self._keep_before()
        self._set(crs, rotation)
        self.way = (orientation, self.canvas.rotation())

    def orient(self, layers, match: bool, orientation: str,
               found_of: Callable[[object], Optional[tuple]] = lambda layer: None,
               typed_of: Callable[[object], Optional[float]] = lambda layer: None) -> str:
        """
        The map put in the first image's CRS (`match`) and turned as
        `orientation` says (core/map_view.ORIENTATIONS) for it; what was done
        (or why the map was not turned), "" when nothing was asked.
        `found_of(layer)`: its catalog record and catalog; `typed_of(layer)`:
        the direction typed in クイック幾何補正.
        """
        if not layers or (not match and orientation == "north"):
            return ""
        layer = next((x for x in layers if centre_of(x, self.project) is not None), None)
        if layer is None:
            return ""
        centre = centre_of(layer, self.project)
        current = self.canvas.mapSettings().destinationCrs()
        why = source = ""
        bearing = None
        if orientation != "north":
            bearing, source = self._bearing(layer, orientation, found_of, typed_of)
            if bearing is None:
                why, source = source, ""
        crs = crs_for(layer, centre, match, bearing is not None, current)
        if bearing is not None:
            self._turn_to(centre, crs, bearing, orientation)
        else:
            # North up (asked, or the way not known): the rotation an image put on before taken off.
            rotation = self.before.get("rotation") if self.before is not None else None
            self._keep_before()
            self.way = None
            self._set(crs, rotation)
        self.layer_ids |= {x.id() for x in layers}
        self.turned.emit()
        parts = []
        if crs is not None:
            parts.append(crs.description() or crs.authid())
        if bearing is not None:
            parts.append(self.tr("{way}（回転 {r}、{source}から）").format(
                way=ORIENTATION_LABELS[orientation].split("（")[0], r=turned_text(self.canvas.rotation()), source=source))
        elif why:
            parts.append(self.tr("北が上のまま（{why}）").format(why=why))
        text = self.tr("地図: ") + "、".join(parts) if parts else ""
        if text:
            self.said.emit(text)
        return text

    def turn(self, layer, orientation: str,
             found_of: Callable[[object], Optional[tuple]] = lambda layer: None,
             typed_of: Callable[[object], Optional[float]] = lambda layer: None) -> tuple[bool, str]:
        """
        画像補正「地図の向き」: the map turned so `orientation`'s way of
        `layer` is up (a map in degrees put in the image's UTM zone first), or
        north up for "north" (the CRS as it is); whether it turned, and what
        was done or why not (the map then left as it is).
        """
        if orientation == "north":
            self.way = None
            self.canvas.setRotation(0)
            self.canvas.refresh()
            self.turned.emit()
            return True, self.tr("北が上")
        centre = centre_of(layer, self.project) if layer is not None else None
        if centre is None:
            return False, self.tr("回さなかった: 画像の位置がわからない")
        bearing, source = self._bearing(layer, orientation, found_of, typed_of)
        if bearing is None:
            return False, self.tr("回さなかった: {why}").format(why=source)
        crs = crs_for(layer, centre, False, True, self.canvas.mapSettings().destinationCrs())
        self._turn_to(centre, crs, bearing, orientation)
        self.layer_ids.add(layer.id())
        self.turned.emit()
        text = self.tr("回転 {r}（{way}、{source}から）").format(
            r=turned_text(self.canvas.rotation()), way=ORIENTATION_LABELS[orientation].split("（")[0], source=source)
        if crs is not None:
            text += self.tr("。地図を {crs} に").format(crs=crs.description() or crs.authid())
        return True, text

    def fitted(self, layers) -> Optional[object]:
        """The extent that shows the layers whole on the map as it is turned now (None when it can not)."""
        s = self.canvas.mapSettings()
        return fitted_extent(layers, s.destinationCrs(), self.canvas.rotation(), project=self.project)


def shared_orienter(canvas, user) -> MapOrienter:
    """The map's MapOrienter, held by `user` (a dock) until `release_orienter`: one north arrow, one map to put back."""
    o = _SHARED.get(id(canvas))
    if o is None or o.canvas is not canvas or not o.users:
        o = MapOrienter(canvas)
        _SHARED[id(canvas)] = o
    o.users.add(id(user))
    return o


def release_orienter(orienter: MapOrienter, user) -> None:
    """`user` no longer needs the orienter: the last one unloads it."""
    orienter.users.discard(id(user))
    if not orienter.users:
        if _SHARED.get(id(orienter.canvas)) is orienter:
            del _SHARED[id(orienter.canvas)]
        orienter.unload()
