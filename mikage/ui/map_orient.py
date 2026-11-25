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

    def __init__(self, canvas, project: Optional[QgsProject] = None, parent: Optional[QObject] = None):
        super().__init__(parent)
        self.canvas = canvas
        self.project = project or QgsProject.instance()
        #: The map as it was before images turned it: {"crs": …, "rotation": …} (a value None: the person's since); None for none kept.
        self.before: Optional[dict] = None
        #: The layers it was done for (their ids): when the last of them goes, the map is put back.
        self.layer_ids: set[str] = set()
        self._busy = False
        self.arrow = NorthArrow(canvas)
        self.project.layersWillBeRemoved.connect(self._removed)
        self.project.removeAll.connect(self.forget)
        self.project.readProject.connect(self.forget)
        canvas.destinationCrsChanged.connect(self._crs_changed)
        canvas.rotationChanged.connect(self._rotation_changed)

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
        self._set(crs if crs is not None and crs.isValid() else None, rotation)
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
        turning = orientation != "north"
        why = ""
        rotation = None
        bearing = None
        if turning:
            d = directions_of(layer, found_of(layer), typed_of(layer), orientation)
            bearing, source = bearing_up(orientation, d)
            if bearing is None:
                why = source
        crs = crs_for(layer, centre, match, bearing is not None, current)
        shown_in = crs if crs is not None else current
        if bearing is not None:
            grid = grid_bearing(shown_in, centre[0], centre[1], bearing, self.project)
            rotation = rotation_for(grid if grid is not None else bearing)
        elif self.before is not None and self.before.get("rotation") is not None:
            # North up (asked, or the way not known): the rotation an image put on before taken off.
            rotation = self.before["rotation"]
        if self.before is None:
            self.before = {"crs": QgsCoordinateReferenceSystem(current), "rotation": self.canvas.rotation()}
        self._set(crs, rotation)
        self.layer_ids |= {x.id() for x in layers}
        parts = []
        if crs is not None:
            parts.append(self.tr("投影法を {name} に").format(name=crs.userFriendlyIdentifier() or crs.authid()))
        if bearing is not None:
            parts.append(self.tr("{way}（地図の回転 {r}、{source}から）").format(
                way=ORIENTATION_LABELS[orientation], r=turned_text(self.canvas.rotation()), source=source))
        elif why:
            parts.append(self.tr("北が上のまま: {why}").format(why=why))
        text = self.tr("地図: ") + "、".join(parts) if parts else ""
        if text:
            self.said.emit(text)
        return text

    def fitted(self, layers) -> Optional[object]:
        """The extent that shows the layers whole on the map as it is turned now (None when it can not)."""
        s = self.canvas.mapSettings()
        return fitted_extent(layers, s.destinationCrs(), self.canvas.rotation(), project=self.project)
