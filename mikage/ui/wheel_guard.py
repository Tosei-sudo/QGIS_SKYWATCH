"""
The mouse wheel scrolls the dock, not the choice under the pointer
(tsudo, 2026-11-18: 「画像補正のカラースケールの誤操作が多い」).

Qt gives a wheel turn to the combo box, spin box or slider under the
pointer even when it was never clicked, so scrolling a long dock down
changed whatever passed under the pointer on the way: the colour scale
coloured the image, a slider moved. `guard_wheel(widget)` has the combo
boxes, spin boxes and sliders in `widget` take the wheel only once they
have the focus (clicked, or reached with Tab); before that the wheel goes
on to the scroll area around them, which scrolls.
"""
from __future__ import annotations

from qgis.PyQt.QtCore import QEvent, QObject, Qt
from qgis.PyQt.QtWidgets import QAbstractSlider, QAbstractSpinBox, QComboBox, QScrollBar, QWidget

#: The kinds of widget whose value a wheel turn changes.
GUARDED = (QComboBox, QAbstractSpinBox, QAbstractSlider)


class WheelGuard(QObject):
    """Event filter: a wheel turn over a guarded widget without the focus goes on to its parents (the scroll area)."""

    def eventFilter(self, obj, event) -> bool:  # noqa: N802 (Qt's name)
        if event.type() == QEvent.Type.Wheel and isinstance(obj, QWidget) and not obj.hasFocus():
            # Not accepted, so Qt hands the turn to the parents in turn; the widget itself never sees it.
            event.ignore()
            return True
        return False


def guarded(widget: QWidget) -> list:
    """The widgets in `widget` whose value the wheel changes (not scroll bars, which are for the wheel)."""
    found = []
    for kind in GUARDED:
        found += [w for w in widget.findChildren(kind) if not isinstance(w, QScrollBar) and w not in found]
    return found


def guard_wheel(widget: QWidget, guard: WheelGuard | None = None) -> WheelGuard:
    """
    The combo boxes, spin boxes and sliders in `widget` take the wheel only
    with the focus (and get the focus only by a click or Tab, not the wheel).
    Call again with the guard returned for widgets added later.
    """
    guard = guard or WheelGuard(widget)
    for w in guarded(widget):
        if w.focusPolicy() == Qt.FocusPolicy.WheelFocus:
            w.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        w.removeEventFilter(guard)
        w.installEventFilter(guard)
    return guard
