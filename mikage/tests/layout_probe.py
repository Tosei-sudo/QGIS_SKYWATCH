"""
Finds what a window draws over something else, or cuts off: used by the
tests (test_qgis_layout_overlap.py) and by scripts/help_shots/check_layout.py
(a real QGIS on a virtual screen).

`problems(root)` walks the visible widgets under `root` and reports
* "short": a word-wrapped label lower than its lines need;
* "overlap": two widgets of one layout drawn over each other;
* "cut": a label or button narrower or lower than its text (no wrap), or a
  box (combo, line edit, spin box) lower than it is drawn.
`squeezed(root)`: the widgets with a layout smaller than the layout needs
(not a problem by itself: a table or a text box in it may just be lower).
"""
from __future__ import annotations

from qgis.PyQt.QtWidgets import (
    QAbstractButton,
    QAbstractScrollArea,
    QAbstractSpinBox,
    QComboBox,
    QLabel,
    QLayout,
    QLineEdit,
    QSizePolicy,
    QSplitter,
    QStackedWidget,
    QTabBar,
    QToolBar,
    QWidget,
)

#: Pixels of slack (rounding, frames that touch).
SLACK = 1


def name_of(w: QWidget) -> str:
    """A short name for a widget: its class, object name, and text."""
    parts = [type(w).__name__]
    if w.objectName() and not w.objectName().startswith("qt_"):
        parts.append("#" + w.objectName())
    text = ""
    if isinstance(w, (QLabel, QAbstractButton)):
        text = w.text()
    elif hasattr(w, "title") and callable(w.title):
        try:
            text = w.title()
        except TypeError:
            text = ""
    if text:
        text = " ".join(str(text).split())
        parts.append(f"「{text[:40]}{'…' if len(text) > 40 else ''}」")
    return " ".join(parts)


def path_of(w: QWidget, root: QWidget) -> str:
    """The widget's name with those of the nearest named or titled widgets above it."""
    names = [name_of(w)]
    p = w.parentWidget()
    while p is not None and p is not root and len(names) < 4:
        if (p.objectName() and not p.objectName().startswith("qt_")) or (hasattr(p, "title") and callable(p.title)):
            names.append(name_of(p))
        p = p.parentWidget()
    return " < ".join(names)


def _layout_widgets(layout: QLayout) -> list:
    """The widgets a layout places (also those of the layouts inside it)."""
    out = []
    for i in range(layout.count()):
        item = layout.itemAt(i)
        if item is None:
            continue
        if item.widget() is not None:
            out.append(item.widget())
        elif item.layout() is not None:
            out.extend(_layout_widgets(item.layout()))
    return out


def _needed(w: QWidget) -> tuple[int, int] | None:
    """The width and height the widget's layout needs at the widget's width (None: no layout)."""
    layout = w.layout()
    if layout is None:
        return None
    minimum = layout.totalMinimumSize()
    height = minimum.height()
    if layout.hasHeightForWidth():
        height = max(height, layout.totalHeightForWidth(max(w.width(), minimum.width())))
    return minimum.width(), height


def _scrolled(w: QWidget) -> bool:
    """Inside a scroll area's viewport (a table's cell widget, a scrolled body): may be partly out of sight on purpose."""
    p = w.parentWidget()
    while p is not None:
        if isinstance(p.parentWidget(), QAbstractScrollArea) and p is p.parentWidget().viewport():
            return True
        p = p.parentWidget()
    return False


def _elided_on_purpose(w: QWidget) -> bool:
    return w.sizePolicy().horizontalPolicy() == QSizePolicy.Policy.Ignored


def _shown(root: QWidget) -> list:
    out = []
    for w in [root] + root.findChildren(QWidget):
        if not w.isVisible() or w.width() <= 0 or w.height() <= 0:
            continue
        if isinstance(w, (QTabBar, QToolBar)) or isinstance(w.parentWidget(), (QTabBar, QToolBar)):
            continue  # they scroll or fold their own buttons
        out.append(w)
    return out


def _collapsed(w: QWidget) -> bool:
    return bool(getattr(w, "isCollapsed", None)) and w.isCollapsed()


def squeezed(root: QWidget) -> list[dict]:
    found = []
    for w in _shown(root):
        need = _needed(w)
        if need is None or isinstance(w, (QStackedWidget, QSplitter)) or _collapsed(w):
            continue
        nw, nh = need
        if w.width() + SLACK < nw or w.height() + SLACK < nh:
            found.append({"kind": "squeezed", "widget": path_of(w, root), "size": [w.width(), w.height()], "need": [nw, nh]})
    return found


def _text_widget(w: QWidget) -> bool:
    if isinstance(w, QLabel):
        return not w.wordWrap() and bool(w.text().strip()) and w.pixmap() is None
    if isinstance(w, QAbstractButton):
        return bool(w.text().strip()) and not isinstance(w.parentWidget(), QComboBox)
    return isinstance(w, (QComboBox, QLineEdit, QAbstractSpinBox))


def problems(root: QWidget, cut: bool = True) -> list[dict]:
    """What `root` (a dock, a dialog, a page) draws over something else or cuts off, as it is shown now."""
    found = []
    for w in _shown(root):
        if isinstance(w, QLabel) and w.wordWrap() and w.text().strip():
            hfw = w.heightForWidth(w.width())
            if hfw > 0 and w.height() + SLACK < hfw:
                found.append({"kind": "short", "widget": path_of(w, root), "size": [w.width(), w.height()], "need": [w.width(), hfw]})
        if cut and _text_widget(w) and not _scrolled(w) and not isinstance(w.parentWidget(), (QComboBox, QAbstractSpinBox)):
            hint = w.minimumSizeHint() if isinstance(w, (QComboBox, QLineEdit, QAbstractSpinBox)) else w.sizeHint()
            narrow = w.width() + SLACK < hint.width() and not _elided_on_purpose(w) and not isinstance(w, (QComboBox, QLineEdit, QAbstractSpinBox))
            low = w.height() + SLACK < hint.height() and w.sizePolicy().verticalPolicy() != QSizePolicy.Policy.Ignored
            if narrow or low:
                found.append({"kind": "cut", "widget": path_of(w, root), "size": [w.width(), w.height()], "need": [hint.width(), hint.height()]})
        layout = w.layout()
        if layout is not None:
            placed = [c for c in _layout_widgets(layout) if c.isVisible() and c.parentWidget() is w and type(c).__name__ != "QgsMessageBar"]
            for i, a in enumerate(placed):
                ga = a.geometry().adjusted(SLACK, SLACK, -SLACK, -SLACK)
                for b in placed[i + 1:]:
                    if ga.intersects(b.geometry().adjusted(SLACK, SLACK, -SLACK, -SLACK)):
                        found.append({"kind": "overlap", "widget": path_of(a, root), "other": path_of(b, root),
                                      "size": [a.geometry().getRect()], "need": [b.geometry().getRect()]})
    return found


def summary(found: list[dict]) -> str:
    return "\n".join(f"{p['kind']}: {p['widget']}" + (f" × {p['other']}" if "other" in p else "") + f" {p['size']} → {p['need']}" for p in found)
