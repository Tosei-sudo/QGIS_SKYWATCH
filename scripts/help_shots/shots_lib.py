"""
Helpers for scenario.py (runs inside QGIS): a step runner driven by QTimer,
and screenshots with red frames and numbered badges on what to press.

A scenario is a generator. It yields
* a number: wait that many milliseconds;
* a callable: wait until it returns true (at most `TIMEOUT` seconds);
and between yields it changes widgets and calls `shoot`. Anything that opens
a modal window (QMessageBox, QDialog.exec) is started with `later(fn)` so the
generator is not on the stack while the window's own event loop runs.
"""
from __future__ import annotations

import os
import time
import traceback
from collections.abc import Callable, Iterable
from typing import Union

from qgis.PyQt.QtCore import QPoint, QRect, QRectF, QSize, Qt, QTimer
from qgis.PyQt.QtGui import QColor, QFont, QImage, QPainter, QPen
from qgis.PyQt.QtWidgets import QApplication, QWidget

TIMEOUT = 60
#: The widest picture (the help window's page is about 840 pixels wide).
MAX_WIDTH = 760
#: The red of the frames.
RED = QColor("#e11d48")


class Runner:
    def __init__(self, scenario, log: Callable[[str], None], finished: Callable[[], None]):
        self.scenario = scenario
        self.log = log
        self.finished = finished

    def start(self) -> None:
        QTimer.singleShot(0, self.step)

    def step(self) -> None:
        try:
            wait = next(self.scenario)
        except StopIteration:
            self.finished()
            return
        except Exception:
            self.log(traceback.format_exc())
            self.finished()
            return
        if callable(wait):
            self._poll(wait, time.time() + TIMEOUT)
        else:
            QTimer.singleShot(int(wait or 0), self.step)

    def _poll(self, condition, deadline: float) -> None:
        try:
            ok = bool(condition())
        except Exception:
            ok = False
        if ok or time.time() > deadline:
            if not ok:
                self.log(f"timeout: {getattr(condition, '__name__', condition)}")
            # Some time to paint what changed.
            QTimer.singleShot(300, self.step)
        else:
            QTimer.singleShot(100, lambda: self._poll(condition, deadline))


def later(fn: Callable[[], object], ms: int = 0) -> None:
    QTimer.singleShot(ms, fn)


def settle(rounds: int = 5) -> None:
    for _ in range(rounds):
        QApplication.processEvents()


Mark = Union[QWidget, QRect, tuple]


def _rect_of(mark, target: QWidget) -> QRect:
    if isinstance(mark, QRect):
        return QRect(mark)
    top_left = target.mapFromGlobal(mark.mapToGlobal(QPoint(0, 0)))
    return QRect(top_left, mark.size())


def frame(img: QImage, rects: Iterable[tuple], scale: float = 1.0) -> None:
    """Red rounded frames, a numbered badge beside those with a label (left by default, or "top", "right", "bottom")."""
    p = QPainter(img)
    p.setRenderHint(QPainter.RenderHint.Antialiasing)
    for rect, label, *where in rects:
        where = where[0] if where else "left"
        r = QRectF(rect.x() * scale, rect.y() * scale, rect.width() * scale, rect.height() * scale).adjusted(-3, -3, 3, 3)
        p.setPen(QPen(RED, 3))
        p.setBrush(Qt.BrushStyle.NoBrush)
        p.drawRoundedRect(r, 5, 5)
        if label:
            font = QFont(QApplication.font())
            font.setPixelSize(15)
            font.setBold(True)
            p.setFont(font)
            d = max(24, p.fontMetrics().horizontalAdvance(label) + 12)
            # Left of the frame, at its middle (inside it when there is no room on the left).
            badge = QRectF(r.left() - d - 3, r.center().y() - 12, d, 24)
            if where == "top":
                badge = QRectF(r.center().x() - d / 2, r.top() - 27, d, 24)
            elif where == "right":
                badge = QRectF(r.right() + 3, r.center().y() - 12, d, 24)
            elif where == "bottom":
                badge = QRectF(r.center().x() - d / 2, r.bottom() + 3, d, 24)
            if where in ("top", "bottom"):
                badge.moveLeft(max(1.0, min(badge.left(), img.width() - d - 1.0)))
            elif badge.left() < 1:
                badge = QRectF(r.left() + 4, r.top() - 26, d, 24) if r.top() > 27 else QRectF(r.left() + 4, r.top() + 4, d, 24)
            badge.moveTop(max(1.0, min(badge.top(), img.height() - 25.0)))
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(RED)
            p.drawRoundedRect(badge, 12, 12)
            p.setPen(QColor("white"))
            p.drawText(badge, Qt.AlignmentFlag.AlignCenter, label)
    p.end()


def shoot(path: str, target: QWidget, marks: Iterable[Mark] = (), crop: QRect | QWidget | list | None = None,
          width: int | None = None, margin: int | tuple = 0, pad: int | None = None) -> QImage:
    """
    The target's picture with red frames on the marks (widgets, rectangles in
    the target's pixels, or (mark, label)), cut to `crop` (a rectangle, a
    widget, or several: their union, plus `margin`: pixels, or (sideways,
    up and down)), scaled down to `width`.
    `pad`: room on the left for the numbered badges (default: 30 when a
    badge is drawn on a picture of a part of a window).
    """
    settle()
    img = target.grab().toImage().convertToFormat(QImage.Format.Format_RGB32)
    if img.width() != target.width():
        # Scaled for the screen's dots per inch (the virtual screen is started at 96, where this does not happen).
        img = img.scaled(target.size(), Qt.AspectRatioMode.IgnoreAspectRatio, Qt.TransformationMode.SmoothTransformation)
    rects = []
    for m in marks:
        mark, label, *where = (m if isinstance(m, tuple) else (m, None))
        if mark is None or isinstance(mark, QWidget) and not mark.isVisible():
            continue
        rects.append((_rect_of(mark, target), label, *where))
    area = None
    if crop is not None:
        for c in (crop if isinstance(crop, list) else [crop]):
            if c is None:
                continue
            r = _rect_of(c, target)
            area = r if area is None else area.united(r)
        mx, my = margin if isinstance(margin, tuple) else (margin, margin)
        area = area.adjusted(-mx, -my, mx, my).intersected(img.rect())
        rects = [(r.translated(-area.topLeft()), *rest) for r, *rest in rects]
        img = img.copy(area)
    if pad is None:
        # A part of a window, or a window of its own (a dialog): the badges outside the frames on the left.
        pad = 30 if any(rest[0] for _r, *rest in rects) and not target.inherits("QMainWindow") else 0
    # Never wider than the help shows without scrolling sideways.
    width = min(width or MAX_WIDTH, MAX_WIDTH) - pad
    scale = 1.0
    if img.width() > width:
        scale = width / img.width()
        img = img.scaledToWidth(width, Qt.TransformationMode.SmoothTransformation)
    if pad:
        wider = QImage(img.width() + pad, img.height(), QImage.Format.Format_RGB32)
        wider.fill(target.palette().window().color())
        p = QPainter(wider)
        p.drawImage(pad, 0, img)
        p.end()
        img = wider
        rects = [(QRect(round(r.x() + pad / scale), r.y(), r.width(), r.height()), *rest) for r, *rest in rects]
    frame(img, rects, scale)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    img.save(path)
    return img


def map_rect(canvas, west: float, south: float, east: float, north: float) -> QRect:
    """A rectangle of the map (in the map's CRS) in the canvas's pixels."""
    t = canvas.getCoordinateTransform()
    a = t.transform(west, north)
    b = t.transform(east, south)
    return QRect(QPoint(int(a.x()), int(a.y())), QPoint(int(b.x()), int(b.y()))).normalized()


def fit_right(canvas, west: float, south: float, east: float, north: float, strip: int, border: int = 30) -> None:
    """The map moved and zoomed so that this area is in the canvas's right `strip` pixels (what a picture of a dock and the map beside it shows)."""
    from qgis.core import QgsRectangle

    w, h = canvas.width(), canvas.height()
    unit = max((east - west) / max(1, strip - 2 * border), (north - south) / max(1, h - 2 * border))
    right = east + border * unit
    middle = (south + north) / 2
    canvas.setExtent(QgsRectangle(right - w * unit, middle - h * unit / 2, right, middle + h * unit / 2))
    canvas.refresh()


def table_rows(table, owner: QWidget) -> QRect:
    """A table view's header and rows (without the empty part below them), in the owner's pixels."""
    view = table.viewport()
    n = table.model().rowCount()
    bottom = table.visualRect(table.model().index(n - 1, 0)).bottom() + 1 if n else 0
    rows = moved(QRect(0, 0, view.width(), bottom), view, owner)
    header = table.horizontalHeader()
    return rows.united(moved(QRect(0, 0, header.width(), header.height()), header, owner))


def moved(rect: QRect, source: QWidget, target: QWidget) -> QRect:
    """A rectangle in the source widget's pixels, in the target's."""
    return QRect(target.mapFromGlobal(source.mapToGlobal(rect.topLeft())), rect.size())


def shoot_screen(path: str, area: QRect, marks: Iterable[tuple] = (), width: int | None = None) -> QImage:
    """
    A part of the (virtual) screen, in screen pixels, with red frames on the
    marks ((widget or screen rectangle, label, where)). For what is on top of
    a window (an open drop-down list, a menu), which a widget's grab leaves out.
    """
    settle()
    screen = QApplication.primaryScreen()
    img = screen.grabWindow(0).toImage().convertToFormat(QImage.Format.Format_RGB32)
    area = QRect(area).intersected(img.rect())
    rects = []
    for mark, label, *where in marks:
        if isinstance(mark, QWidget):
            if not mark.isVisible():
                continue
            mark = global_rect(mark)
        rects.append((QRect(mark).translated(-area.topLeft()), label, *where))
    img = img.copy(area)
    width = min(width or MAX_WIDTH, MAX_WIDTH)
    scale = 1.0
    if img.width() > width:
        scale = width / img.width()
        img = img.scaledToWidth(width, Qt.TransformationMode.SmoothTransformation)
    frame(img, rects, scale)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    img.save(path)
    return img


def global_rect(widget: QWidget) -> QRect:
    """A widget's rectangle on the screen (by width and height: the 撮像依頼 form has a field named size)."""
    return QRect(widget.mapToGlobal(QPoint(0, 0)), QSize(widget.width(), widget.height()))


def fit_left(canvas, west: float, south: float, east: float, north: float, strip: int, border: int = 30) -> None:
    """The map moved and zoomed so that this area is in the canvas's left `strip` pixels (a picture of a dock on the left and the map beside it)."""
    from qgis.core import QgsRectangle

    w, h = canvas.width(), canvas.height()
    unit = max((east - west) / max(1, strip - 2 * border), (north - south) / max(1, h - 2 * border))
    left = west - border * unit
    middle = (south + north) / 2
    canvas.setExtent(QgsRectangle(left, middle - h * unit / 2, left + w * unit, middle + h * unit / 2))
    canvas.refresh()


def save_button(path: str, button: QWidget) -> None:
    """A toolbar button's picture as it is (no frame), to stand in a line of text."""
    settle()
    button.grab().toImage().save(path)
