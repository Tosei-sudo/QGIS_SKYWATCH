"""
What the observations draw on the map while their dock is open, as the
viewer's overlay: this image's records (filled marks, the ring in the colour
of their 確度, a diamond for 「消失」, a grey dashed ring with 「?」 for
「不可」), the things carried over from other images (hollow marks),
records measured by two points (a line with an arrow at the front) and by
three or four (their outline too), the chosen thing's track, the checked
areas, the 不可範囲 (grey, hatched), the place clicked and the line to
the pointer while measuring.

One canvas item paints everything (nothing is added to the project); the
places are WGS 84 and follow the canvas's CRS.

Styles (「印」, tsudo 2026-11-09: the marks hid what they mark): 'normal' as
above; 'outline' paints nothing filled (rings instead of dots, the outlines
and areas as frames, the line of a record with a width left out, other
lines thin) so the image shows through; 'hidden' paints only what is being
put in (the place clicked, the points while measuring, the area being made)
and the record open in the form.
"""
from __future__ import annotations

import math
from collections import OrderedDict
from dataclasses import dataclass, field
from typing import Callable, Optional, Sequence

from qgis.core import QgsPointXY
from qgis.gui import QgsMapCanvas, QgsMapCanvasItem
from qgis.PyQt.QtCore import QPointF, QRectF, Qt
from qgis.PyQt.QtGui import QBrush, QColor, QFont, QFontMetricsF, QImage, QPainter, QPainterPath, QPen, QPolygonF

from ..qgis_io.geometry import from_wgs84

#: The styles, in the order the dock offers them.
STYLES = ("normal", "outline", "hidden")

#: Colours of the top classes, in the order they come (as the viewer).
PALETTE = ["#e11d48", "#2563eb", "#16a34a", "#d97706", "#7c3aed", "#0891b2", "#db2777", "#65a30d", "#ea580c", "#4f46e5"]


@dataclass
class Mark:
    """
    A thing on the map: a record of this image (`obs`), one carried over
    (`ghost`), a step of the chosen flow (`step`), a place something is being
    put down (`slot`, 「見本で記録」: `present` False for one left out).
    """
    role: str
    lon: float
    lat: float
    color: str = "#111111"
    label: str = ""
    present: bool = True
    #: The ring's colour (its 確度's), else white.
    ring: Optional[str] = None
    #: The front and rear when measured by two points.
    ends: Optional[tuple] = None
    #: The corners when measured with a width (three or four points).
    outline: Optional[list] = None
    heading: Optional[float] = None
    editing: bool = False
    #: What it stands for (a record id), for clicks and drags.
    key: Optional[str] = None
    #: Already reported (通報済み): an amber halo behind it.
    reported: bool = False
    #: 「不可」 (a record, or a step of a flow): could not be seen (under cloud…).
    unseen: bool = False


#: The colour of what could not be seen (不可範囲, 不可 records).
UNSEEN_COLOR = "#6b7280"


@dataclass
class Scene:
    marks: list[Mark] = field(default_factory=list)
    #: (ring, label) of the checked areas of this image.
    areas: list[tuple] = field(default_factory=list)
    #: (ring, label) of the 不可範囲 of this image (雲…).
    unseen_areas: list[tuple] = field(default_factory=list)
    track: list[tuple] = field(default_factory=list)
    #: More tracks (the report map: every thing's).
    tracks: list[list[tuple]] = field(default_factory=list)
    pending: Optional[tuple] = None
    #: The area being made (a 不可範囲: grey and hatched as it will be).
    area_pending: Optional[list] = None
    area_pending_unseen: bool = False
    #: The first point clicked while measuring (the rear), the pointer, and the line's label.
    first: Optional[tuple] = None
    cursor: Optional[tuple] = None
    rubber_label: str = ""
    #: The points clicked after the first (the front, then the sides) and the outline they make with the pointer.
    placed: list[tuple] = field(default_factory=list)
    rubber_outline: Optional[list] = None


class _Labels:
    """
    Each label as painted (its text in its colour inside a white edge) kept
    as a small picture and pasted again (tsudo 2026-11-26: a few hundred
    records stuttered): stroking the letters' outlines was 8 tenths of a
    paint, done again at every move of the map. The same pixels as the
    letters stroked where they are (but for their place rounded to a pixel).
    """

    #: Pictures kept (the oldest let go past it): a few hundred records' names, their activities and dates.
    LIMIT = 4000

    def __init__(self) -> None:
        self._kept: OrderedDict = OrderedDict()

    def get(self, text: str, color: str, font: QFont, ratio: float) -> tuple[QImage, float, float, float]:
        """The picture of a label, where its top left lies from the start of its line, and the line's length."""
        key = (text, color, font.key(), ratio)
        got = self._kept.get(key)
        if got is not None:
            self._kept.move_to_end(key)
            return got
        path = QPainterPath()
        path.addText(QPointF(0, 0), font, text)
        # The white edge (a 3 px pen) reaches 1.5 px out of the letters; a pixel more for the smoothing.
        box = path.boundingRect().adjusted(-3, -3, 3, 3)
        left, top = math.floor(box.left()), math.floor(box.top())
        width, height = math.ceil(box.right()) - left, math.ceil(box.bottom()) - top
        image = QImage(max(1, math.ceil(width * ratio)), max(1, math.ceil(height * ratio)), QImage.Format.Format_ARGB32_Premultiplied)
        image.setDevicePixelRatio(ratio)
        image.fill(Qt.GlobalColor.transparent)
        painter = QPainter(image)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        painter.translate(-left, -top)
        painter.setPen(QPen(QColor("white"), 3))
        painter.setBrush(Qt.BrushStyle.NoBrush)
        painter.drawPath(path)
        painter.fillPath(path, QColor(color))
        painter.end()
        got = (image, float(left), float(top), QFontMetricsF(font).horizontalAdvance(text))
        self._kept[key] = got
        if len(self._kept) > self.LIMIT:
            self._kept.popitem(last=False)
        return got


_LABELS = _Labels()


def _ratio(painter) -> float:
    """The pixels per point of what is painted on (2 on a screen at 200 %)."""
    device = painter.device()
    try:
        return float(device.devicePixelRatioF()) if device is not None else 1.0
    except AttributeError:
        return 1.0


def _halo_text(painter, at: QPointF, text: str, color: str, font: QFont, align_left: bool = False) -> None:
    if not text:
        return
    ratio = _ratio(painter)
    image, left, top, w = _LABELS.get(text, color, font, ratio)
    x = at.x() if align_left else at.x() - w / 2
    # On a whole pixel of the screen (pasted between two, it would be smoothed over both).
    painter.drawImage(QPointF(round((x + left) * ratio) / ratio, round((at.y() + top) * ratio) / ratio), image)


#: How far (pixels) past the map's edges a mark is still painted: its label (centred on it) and its ring reach out.
NEAR_EDGE = 200


class ScenePainter:
    """Paints a Scene through `to_pixel(lon, lat)` → QPointF (the canvas's, or a report map's)."""

    def __init__(self, to_pixel: Callable[[float, float], QPointF], scale: float = 1.0, style: str = "normal"):
        self.to_pixel = to_pixel
        self.scale = scale
        #: 'normal', 'outline' or 'hidden' (see the module's note).
        self.style = style
        self._font = QFont()
        self._font.setPointSizeF(9 * scale)
        self._bold = QFont(self._font)
        self._bold.setBold(True)
        #: Where each mark was painted, for hits.
        self.painted: list[tuple[QPointF, Mark]] = []

    @property
    def hollow(self) -> bool:
        return self.style == "outline"

    def paint(self, painter, s: Scene, view: Optional[QRectF] = None) -> None:
        """`view`: the part of the painter seen (its pixels); marks far off it are not painted (nor hit)."""
        k = self.scale
        painter.setRenderHint(painter.RenderHint.Antialiasing, True)
        self.painted = []
        hidden = self.style == "hidden"
        marks = [m for m in s.marks if m.editing or m.role == "slot"] if hidden else s.marks
        for ring, label in [] if hidden else s.areas:
            self._area(painter, ring, label, "#0f766e", QColor(15, 118, 110, 18))
        for ring, label in [] if hidden else s.unseen_areas:
            self._area(painter, ring, label, UNSEEN_COLOR, QColor(107, 114, 128, 40), hatched=True)
        if s.area_pending and s.area_pending_unseen:
            self._area(painter, s.area_pending, "", UNSEEN_COLOR, QColor(107, 114, 128, 40), hatched=True)
        elif s.area_pending:
            self._area(painter, s.area_pending, "", "#e11d48", QColor(225, 29, 72, 20))
        for track in [] if hidden else [s.track, *s.tracks]:
            if len(track) < 2:
                continue
            points = QPolygonF([self.to_pixel(*p) for p in track])
            painter.setBrush(Qt.BrushStyle.NoBrush)
            painter.setPen(QPen(QColor("white"), 5 * k))
            painter.drawPolyline(points)
            pen = QPen(QColor("#111111"), 2 * k)
            pen.setDashPattern([3, 2])
            painter.setPen(pen)
            painter.drawPolyline(points)
        placed = self._placed(marks, view)
        for mark, _at, _ends, outline in placed:
            if outline:
                self._outline(painter, outline, mark.color, mark.role in ("ghost", "slot"))
        for mark, _at, ends, _outline in placed:
            if ends:
                self._line(painter, mark, ends)
        for mark, at, _ends, _outline in placed:
            self._mark(painter, mark, at)
        if s.pending:
            self._cross(painter, self.to_pixel(*s.pending))
        if s.first:
            first = self.to_pixel(*s.first)
            self._cross(painter, first)
            if s.rubber_outline:
                self._outline(painter, [self.to_pixel(p[0], p[1]) for p in s.rubber_outline], "#e11d48", True)
            for p in s.placed:
                self._cross(painter, self.to_pixel(*p))
            if s.placed:
                painter.setPen(QPen(QColor("#e11d48"), 2 * k))
                painter.drawLine(first, self.to_pixel(*s.placed[0]))
                if s.rubber_label:
                    at = self.to_pixel(*s.placed[0])
                    _halo_text(painter, QPointF(at.x(), at.y() - 14 * k), s.rubber_label, "#111111", self._bold)
            elif s.cursor:
                cursor = self.to_pixel(*s.cursor)
                painter.setPen(QPen(QColor("white"), 5 * k))
                painter.drawLine(first, cursor)
                pen = QPen(QColor("#e11d48"), 2 * k)
                pen.setDashPattern([3, 2])
                painter.setPen(pen)
                painter.drawLine(first, cursor)
                mid = QPointF((first.x() + cursor.x()) / 2, (first.y() + cursor.y()) / 2 - 10 * k)
                _halo_text(painter, mid, s.rubber_label, "#111111", self._bold)

    def _placed(self, marks: Sequence[Mark], view: Optional[QRectF]) -> list[tuple]:
        """
        Each mark with its place, its ends and its corners in pixels (worked
        out once for the three passes), those far off `view` left out: zoomed
        in on a few of many records, the others cost nothing.
        """
        near = view.adjusted(-NEAR_EDGE, -NEAR_EDGE, NEAR_EDGE, NEAR_EDGE) if view is not None else None
        out = []
        for m in marks:
            at = self.to_pixel(m.lon, m.lat)
            if near is not None and not near.contains(at) and not (m.ends or m.outline):
                continue
            ends = (self.to_pixel(*m.ends[0]), self.to_pixel(*m.ends[1])) if m.ends else None
            outline = [self.to_pixel(p[0], p[1]) for p in m.outline] if m.outline else None
            if near is not None and not near.contains(at):
                # A long thing (a ship at a close scale) may cross the map with its middle off it.
                points = [*(ends or ()), *(outline or ())]
                xs, ys = [p.x() for p in points], [p.y() for p in points]
                if not near.intersects(QRectF(QPointF(min(xs), min(ys)), QPointF(max(xs), max(ys)))):
                    continue
            out.append((m, at, ends, outline))
        return out

    def _area(self, painter, ring, label: str, color: str, fill: QColor, hatched: bool = False) -> None:
        points = [self.to_pixel(p[0], p[1]) for p in ring]
        pen = QPen(QColor(color), 2 * self.scale)
        pen.setDashPattern([4, 2])
        painter.setPen(pen)
        painter.setBrush(Qt.BrushStyle.NoBrush if self.hollow else QBrush(fill))
        painter.drawPolygon(QPolygonF(points))
        if hatched:
            # Slanted lines over it (frames only too: it tells it from a checked area at a glance).
            lines = QColor(color)
            lines.setAlpha(110)
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(QBrush(lines, Qt.BrushStyle.BDiagPattern))
            painter.drawPolygon(QPolygonF(points))
        if label:
            # Its name at its top left corner, clear of the marks inside.
            corner = QPointF(min(p.x() for p in points) + 2, min(p.y() for p in points) - 4)
            _halo_text(painter, corner, label, color, self._font, align_left=True)

    def _outline(self, painter, corners: list[QPointF], color: str, dashed: bool) -> None:
        k = self.scale
        points = QPolygonF(corners)
        painter.setBrush(Qt.BrushStyle.NoBrush)
        painter.setPen(QPen(QColor("white"), 4 * k))
        painter.drawPolygon(points)
        pen = QPen(QColor(color), 2 * k)
        if dashed:
            pen.setDashPattern([3, 2])
        painter.setPen(pen)
        fill = QColor(color)
        fill.setAlpha(20 if dashed else 45)
        painter.setBrush(Qt.BrushStyle.NoBrush if self.hollow else QBrush(fill))
        painter.drawPolygon(points)

    def _cross(self, painter, at: QPointF) -> None:
        r = 10 * self.scale
        painter.setPen(QPen(QColor("#e11d48"), 2 * self.scale))
        painter.drawLine(QPointF(at.x() - r, at.y()), QPointF(at.x() + r, at.y()))
        painter.drawLine(QPointF(at.x(), at.y() - r), QPointF(at.x(), at.y() + r))

    def _line(self, painter, mark: Mark, ends: tuple[QPointF, QPointF]) -> None:
        k = self.scale
        front, rear = ends
        ghost = mark.role in ("ghost", "slot")
        if not self.hollow:
            painter.setPen(QPen(QColor("white"), (4 if ghost else 6) * k))
            painter.drawLine(front, rear)
        if not (self.hollow and mark.outline):
            # Hollow: the outline shows the length already; a line alone is thin (it lies along the thing).
            pen = QPen(QColor(mark.color), (1.5 if self.hollow else 2 if ghost else 3) * k)
            if ghost:
                pen.setDashPattern([2, 2])
            painter.setPen(pen)
            painter.drawLine(front, rear)
        # An arrow at the front, along the line on the screen.
        angle = math.atan2(front.y() - rear.y(), front.x() - rear.x())
        size = (6 if ghost else 7) * k
        tip = QPointF(front.x() + math.cos(angle) * size, front.y() + math.sin(angle) * size)
        left = QPointF(front.x() + math.cos(angle + 2.4) * size, front.y() + math.sin(angle + 2.4) * size)
        right = QPointF(front.x() + math.cos(angle - 2.4) * size, front.y() + math.sin(angle - 2.4) * size)
        if self.hollow:
            # Off the thing (it starts at the front), so it may stay filled.
            painter.setPen(QPen(QColor("white"), 1 * k))
            painter.setBrush(QBrush(QColor(mark.color)))
        else:
            painter.setPen(QPen(QColor(mark.color if ghost else "white"), 1.5 * k))
            painter.setBrush(QBrush(QColor(255, 255, 255, 150) if ghost else QColor(mark.color)))
        painter.drawPolygon(QPolygonF([tip, left, right]))

    def _mark(self, painter, mark: Mark, at: QPointF) -> None:
        k = self.scale
        self.painted.append((at, mark))
        color = QColor(mark.color)
        if mark.role == "obs" and mark.unseen:
            # 不可: a grey dashed ring with 「?」 (nothing known of it here).
            r = (9 if mark.editing else 7) * k
            pen = QPen(QColor(UNSEEN_COLOR), 2 * k)
            pen.setDashPattern([2, 1.5])
            painter.setPen(pen)
            painter.setBrush(Qt.BrushStyle.NoBrush if self.hollow else QBrush(QColor(255, 255, 255, 170)))
            painter.drawEllipse(at, r, r)
            painter.setPen(QPen(QColor(UNSEEN_COLOR)))
            painter.setFont(self._bold)
            painter.drawText(QRectF(at.x() - r, at.y() - r, 2 * r, 2 * r), int(Qt.AlignmentFlag.AlignCenter), "?")
            _halo_text(painter, QPointF(at.x(), at.y() - 12 * k), mark.label, "#4b5563", self._bold)
        elif mark.role == "obs":
            if mark.reported:
                if self.hollow:
                    painter.setPen(QPen(QColor(245, 158, 11, 220), 3 * k))
                    painter.setBrush(Qt.BrushStyle.NoBrush)
                else:
                    painter.setPen(Qt.PenStyle.NoPen)
                    painter.setBrush(QBrush(QColor(245, 158, 11, 140)))
                painter.drawEllipse(at, 14 * k, 14 * k)
            if mark.present and self.hollow:
                # A ring in the class's colour (inside a white one, or the 確度's): nothing over the middle.
                r = (9 if mark.editing else 7) * k
                painter.setBrush(Qt.BrushStyle.NoBrush)
                painter.setPen(QPen(QColor(mark.ring) if mark.ring else QColor("white"), (2.5 if mark.ring else 2) * k))
                painter.drawEllipse(at, r + 2 * k, r + 2 * k)
                painter.setPen(QPen(color, 2 * k))
                painter.drawEllipse(at, r, r)
            elif mark.present:
                r = (9 if mark.editing else 7) * k
                ring = QColor(mark.ring) if mark.ring else QColor("white")
                painter.setPen(QPen(ring, (3 if mark.ring else 2) * k))
                painter.setBrush(QBrush(color))
                painter.drawEllipse(at, r, r)
            else:
                d = 8 * k
                painter.setPen(QPen(color, 3 * k))
                painter.setBrush(Qt.BrushStyle.NoBrush)
                painter.drawPolygon(QPolygonF([QPointF(at.x(), at.y() - d), QPointF(at.x() + d, at.y()), QPointF(at.x(), at.y() + d), QPointF(at.x() - d, at.y())]))
            _halo_text(painter, QPointF(at.x(), at.y() - 12 * k), mark.label, "#111111", self._bold)
        elif mark.role == "ghost":
            pen = QPen(color, 2 * k)
            pen.setDashPattern([2, 2])
            painter.setPen(pen)
            painter.setBrush(Qt.BrushStyle.NoBrush if self.hollow else QBrush(QColor(255, 255, 255, 90)))
            painter.drawEllipse(at, 7 * k, 7 * k)
            _halo_text(painter, QPointF(at.x(), at.y() - 12 * k), mark.label, "#555555", self._font)
        elif mark.role == "slot":
            if mark.present:
                pen = QPen(color, 2 * k)
                pen.setDashPattern([2, 2])
                painter.setPen(pen)
                painter.setBrush(QBrush(QColor(255, 255, 255, 110)))
                painter.drawEllipse(at, 5 * k, 5 * k)
            else:
                # Left out: a grey cross.
                d = 5 * k
                painter.setPen(QPen(QColor("#6b7280"), 2 * k))
                painter.drawLine(QPointF(at.x() - d, at.y() - d), QPointF(at.x() + d, at.y() + d))
                painter.drawLine(QPointF(at.x() - d, at.y() + d), QPointF(at.x() + d, at.y() - d))
            _halo_text(painter, QPointF(at.x(), at.y() - 10 * k), mark.label, "#111111", self._font)
        elif mark.role == "step":
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(QBrush(QColor(UNSEEN_COLOR if mark.unseen else "#111111" if mark.present else "#dc2626")))
            painter.drawEllipse(at, 3 * k, 3 * k)
            _halo_text(painter, QPointF(at.x(), at.y() + 16 * k), mark.label, "#111111", self._font)


class ObservationOverlay(QgsMapCanvasItem):
    """
    Paints a Scene over the whole canvas: the records' part (`scene_`) once
    as a picture, pasted again while neither it nor the map's extent changes
    (the canvas paints its items at every move of a drag of the map, every
    hover while measuring: with a few hundred records each paint took a
    fifth of a second, tsudo 2026-11-26), and what follows the pointer
    (`live_`: the line being measured, the place clicked, a record dragged,
    what 「見本で記録」 puts down) painted over it each time.
    """

    def __init__(self, canvas: QgsMapCanvas, style: str = "normal"):
        super().__init__(canvas)
        self.canvas_ = canvas
        self.scene_ = Scene()
        self.live_ = Scene()
        self.setZValue(1100)
        style = style if style in STYLES else "normal"
        self._painter = ScenePainter(self.to_pixel, style=style)
        self._live_painter = ScenePainter(self.to_pixel, style=style)
        #: The records' part as last painted: (what it was painted for, the picture or None, where its marks are).
        self._kept: Optional[tuple] = None
        #: Bumped when the records' part changes (the picture is painted anew).
        self._version = 0
        #: The canvas's CRS from WGS 84 while painting (one for all the points of a paint).
        self._to_map = None

    @property
    def painted(self) -> list[tuple[QPointF, Mark]]:
        kept = self._kept[2] if self._kept is not None else []
        return [*kept, *self._live_painter.painted]

    @property
    def style(self) -> str:
        return self._painter.style

    def set_style(self, style: str) -> None:
        """「印」: 'normal', 'outline' or 'hidden' (see the module's note)."""
        style = style if style in STYLES else "normal"
        self._painter.style = self._live_painter.style = style
        if style == "hidden":
            # Nothing hidden can be clicked or dragged until painted again.
            if self._kept is not None:
                self._kept = (self._kept[0], self._kept[1], [p for p in self._kept[2] if p[1].editing])
            self._live_painter.painted = [p for p in self._live_painter.painted if p[1].editing]
        self.update()

    def set_scene(self, scene: Scene, live: Optional[Scene] = None) -> None:
        """The records' part (painted anew only when it changed) and what follows the pointer (`live`, else nothing)."""
        if scene != self.scene_:
            self.scene_ = scene
            self._version += 1
        self.live_ = live if live is not None else Scene()
        self.updatePosition()
        self.update()

    def set_live(self, live: Scene) -> None:
        """Only what follows the pointer changed: the records' picture is pasted as it was."""
        self.live_ = live
        self.update()

    def updatePosition(self) -> None:  # noqa: N802 (QGIS's name)
        self.setPos(0, 0)
        self.prepareGeometryChange()

    def boundingRect(self) -> QRectF:  # noqa: N802
        size = self.canvas_.size()
        return QRectF(0, 0, size.width(), size.height())

    def to_pixel(self, lon: float, lat: float) -> QPointF:
        transform = self._to_map or from_wgs84(self.canvas_.mapSettings().destinationCrs())
        try:
            p = transform.transform(QgsPointXY(lon, lat))
        except Exception:  # noqa: BLE001 (a place the CRS cannot show)
            return QPointF(-1e6, -1e6)
        return self.toCanvasCoordinates(p)

    def hit(self, x: float, y: float, roles: Sequence[str] = ("obs", "ghost"), tolerance: float = 8,
            keyed: bool = False) -> Optional[Mark]:
        """The mark painted nearest a pixel, within `tolerance` (with `keyed`, only marks with a key)."""
        best, best_d = None, tolerance
        for p, mark in self.painted:
            if mark.role not in roles or (keyed and not mark.key):
                continue
            d = math.hypot(p.x() - x, p.y() - y)
            if d <= best_d:
                best, best_d = mark, d
        return best

    def _painted_for(self, settings, ratio: float) -> tuple:
        """What the records' picture shows: its part, the style, the map's extent, size and CRS, the screen's pixels."""
        extent = settings.visibleExtent()
        size = self.canvas_.size()
        return (self._version, self._painter.style, extent, settings.rotation(), settings.outputSize(), settings.destinationCrs(),
                (size.width(), size.height()), ratio, self.toCanvasCoordinates(extent.center()))

    def _picture(self, ratio: float) -> tuple[Optional[QImage], list]:
        """The records' part painted on a picture the size of the canvas (None when there is nothing to paint)."""
        s = self.scene_
        if not (s.marks or s.areas or s.unseen_areas or s.track or s.tracks or s.pending or s.area_pending or s.first):
            self._painter.painted = []
            return None, []
        rect = self.boundingRect()
        image = QImage(max(1, math.ceil(rect.width() * ratio)), max(1, math.ceil(rect.height() * ratio)),
                       QImage.Format.Format_ARGB32_Premultiplied)
        image.setDevicePixelRatio(ratio)
        image.fill(Qt.GlobalColor.transparent)
        painter = QPainter(image)
        try:
            self._painter.paint(painter, s, rect)
        finally:
            painter.end()
        return image, list(self._painter.painted)

    def paint(self, painter, option=None, widget=None) -> None:
        settings = self.canvas_.mapSettings()
        self._to_map = from_wgs84(settings.destinationCrs())
        try:
            ratio = _ratio(painter)
            key = self._painted_for(settings, ratio)
            if self._kept is None or self._kept[0] != key:
                self._kept = (key, *self._picture(ratio))
            if self._kept[1] is not None:
                painter.drawImage(QPointF(0, 0), self._kept[1])
            self._live_painter.paint(painter, self.live_, self.boundingRect())
        finally:
            self._to_map = None


def color_of(kind_top: str, kinds: Sequence[str]) -> str:
    """A colour per top class, so a branch reads as one."""
    i = list(kinds).index(kind_top) if kind_top in kinds else len(kinds)
    return PALETTE[i % len(PALETTE)]
