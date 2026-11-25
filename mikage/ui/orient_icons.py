"""
The icons of 「地図の向き」 (画像補正ドック; tsudo 2026-11-26: 「テキストだと長いので、
矢印とアイコン(例えば北を上なら、太い↑の中にNの文字)」): a thick arrow up with
a letter in it, one colour each: N 北が上, 撮 撮像方向が上, 衛 衛星が上, 進
進行方向が上. Drawn with QPainter (the letters in the system's font), at the
screen's pixel ratio.
"""
from __future__ import annotations

from qgis.PyQt.QtCore import QPointF, QRectF, Qt
from qgis.PyQt.QtGui import QColor, QFont, QIcon, QPainter, QPen, QPixmap, QPolygonF

#: Each way up: its letter and colour.
LOOKS = {
    "north": ("N", "#dc2626"),
    "look": ("撮", "#2563eb"),
    "satellite": ("衛", "#7c3aed"),
    "flight": ("進", "#0f766e"),
}


def arrow_points(size: float) -> list[tuple[float, float]]:
    """The thick arrow up in a `size` square: a short head on a wide shaft (room for the letter)."""
    s = size / 32
    pts = [(16, 0.8), (31.6, 14), (25.2, 14), (25.2, 31.2), (6.8, 31.2), (6.8, 14), (0.4, 14)]
    return [(x * s, y * s) for x, y in pts]


def orient_pixmap(way: str, size: int = 28, ratio: float = 1.0, enabled: bool = True) -> QPixmap:
    letter, colour = LOOKS.get(way, ("?", "#64748b"))
    px = QPixmap(round(size * ratio), round(size * ratio))
    px.setDevicePixelRatio(ratio)
    px.fill(Qt.GlobalColor.transparent)
    p = QPainter(px)
    p.setRenderHint(QPainter.RenderHint.Antialiasing)
    p.setRenderHint(QPainter.RenderHint.TextAntialiasing)
    fill = QColor(colour if enabled else "#cbd5e1")
    p.setPen(QPen(fill.darker(150), max(1.0, size / 28)))
    p.setBrush(fill)
    p.drawPolygon(QPolygonF([QPointF(x, y) for x, y in arrow_points(size)]))
    font = QFont()
    font.setBold(True)
    font.setPixelSize(max(6, round(size * 0.5)))
    p.setFont(font)
    p.setPen(QColor("#ffffff"))
    s = size / 32
    p.drawText(QRectF(0, 11 * s, size, 20.5 * s), int(Qt.AlignmentFlag.AlignCenter), letter)
    p.end()
    return px


def orient_icon(way: str, size: int = 28, ratio: float = 1.0) -> QIcon:
    """The icon drawn at each size it is likely shown at (a toolbar's 16–32, the dock's `size`), sharp on any screen."""
    icon = QIcon()
    for px in sorted({16, 20, 24, 32, size}):
        for r in sorted({1.0, 2.0, ratio}):
            icon.addPixmap(orient_pixmap(way, px, r), QIcon.Mode.Normal)
            icon.addPixmap(orient_pixmap(way, px, r, enabled=False), QIcon.Mode.Disabled)
    return icon
