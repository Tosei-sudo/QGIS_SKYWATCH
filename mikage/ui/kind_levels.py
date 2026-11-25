"""
種別の段ごとのボタン: the classification's levels from 大分類 down, one row
each (the level's name, its choices, the chosen one pressed). A click picks at
that level; a click on the pressed one clears that level and the ones below.
A level with more than KIND_CHIPS_MAX choices is a drop-down instead. A kind
outside the classification still shows on its level.

Made for the record form (tsudo 2026-11-07, PR #46); the same rows are used by
the flow's 「このモノの詳細 ▸ 種別」 and 「一括編集」's 種別 (tsudo 2026-11-25:
「観測記録の流れの、種別を一気に直せる機能、あそこにツリー選択がほしい」).
"""
from __future__ import annotations

from typing import Callable, Optional, Sequence

from qgis.PyQt.QtCore import Qt, pyqtSignal
from qgis.PyQt.QtWidgets import QComboBox, QGridLayout, QLabel, QToolButton, QVBoxLayout, QWidget

from ..core.obs.api import children_of, kind_of, level_name, path_of
from .flow_layout import FlowLayout

#: More choices than this at a level go in a drop-down instead of buttons.
KIND_CHIPS_MAX = 16


def kind_level_rows(taxa: Sequence, kind: str) -> list[tuple[str, list[str], str]]:
    """The rows for a kind: (the level's name, its choices, the chosen one or ""), from 大分類 down to the level below the kind's."""
    taxa = list(taxa)
    path = path_of(kind)
    rows = []
    for d in range(len(path) + 1):
        choices = [t.path[-1] for t in children_of(taxa, path[:d])]
        chosen = path[d] if d < len(path) else ""
        if chosen and chosen not in choices:
            choices.append(chosen)
        if not choices:
            break
        name = level_name(taxa, path, d) or ("大分類" if d == 0 else f"第 {d + 1} 階層")
        rows.append((name, choices, chosen))
    return rows


def chip_button(text: str, tip: str, run: Callable, pressed: bool = False) -> QToolButton:
    """A button that stays pressed when chosen (a level's choice, an activity, an individual)."""
    b = QToolButton()
    b.setText(text)
    b.setToolTip(tip)
    b.setCheckable(True)
    b.setChecked(pressed)
    b.setAutoRaise(False)
    b.clicked.connect(lambda _on=False: run())
    return b


class KindLevels(QWidget):
    """The rows of a kind; `chosen` (the new path, [] when 大分類 is cleared) on a click. `render` draws them again."""

    chosen = pyqtSignal(list)

    def __init__(self, parent: Optional[QWidget] = None):
        super().__init__(parent)
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        #: The rows in a grid of their own, made again on each drawing (its rowCount is the rows shown).
        self.box: Optional[QWidget] = None
        self.grid: Optional[QGridLayout] = None
        #: The rows shown (the levels, and any added after them).
        self.rows = 0
        #: The levels drawn (kind_level_rows), None when cleared.
        self.drawn: Optional[list] = None
        self.clear()

    def clear(self) -> None:
        if self.box is not None:
            # Off the widget at once (a click being handled may be on one of its buttons), deleted later.
            self.box.setParent(None)
            self.box.deleteLater()
        self.box = QWidget()
        self.grid = QGridLayout(self.box)
        self.grid.setContentsMargins(0, 0, 0, 0)
        self.grid.setColumnStretch(1, 1)
        self.layout().addWidget(self.box)
        self.rows = 0
        self.drawn = None

    def add_row(self, label: str, widget: QWidget) -> None:
        """A row after the levels (the form's individuals)."""
        caption = QLabel(f"{label}:")
        caption.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignTop)
        self.grid.addWidget(caption, self.rows, 0)
        self.grid.addWidget(widget, self.rows, 1)
        self.rows += 1

    def render(self, taxa: Sequence, kind: str, keep: bool = False) -> int:
        """Draws the rows of `kind` in the classification `taxa`; how many there are. With `keep`, rows the same as those
        drawn are left as they are (an open drop-down stays open while the dock draws again)."""
        taxa = list(taxa)
        levels = kind_level_rows(taxa, kind)
        if keep and levels == self.drawn:
            return self.rows
        self.clear()
        self.drawn = levels
        path = path_of(kind)
        known = {kind_of(t.path) for t in taxa}
        for d, (name, choices, chosen) in enumerate(levels):
            up = path[:d]
            if len(choices) > KIND_CHIPS_MAX:
                combo = QComboBox()
                combo.addItem(self.tr("（指定なし）"), "")
                for c in choices:
                    combo.addItem(c, c)
                combo.setCurrentIndex(max(0, combo.findData(chosen)))
                combo.setToolTip(f"{name}の選択")
                combo.activated.connect(lambda i, combo=combo, up=up: self.chosen.emit(up + ([combo.itemData(i)] if combo.itemData(i) else [])))
                self.add_row(name, combo)
                continue
            chips = QWidget()
            flow = FlowLayout(chips)
            for c in choices:
                pressed = c == chosen
                outside = kind_of([*up, c]) not in known
                tip = (f"{name}の選択の解除" if pressed else f"{kind_of([*up, c])} を選択") + ("（分類体系にない）" if outside else "")
                flow.addWidget(chip_button(c, tip, lambda p=up + ([] if pressed else [c]): self.chosen.emit(p), pressed))
            self.add_row(name, chips)
        return self.rows

    def buttons(self) -> list[QToolButton]:
        """The level buttons shown (for tests and scripts)."""
        return self.findChildren(QToolButton)

    def press(self, text: str) -> None:
        """Clicks the shown button with that text, as a person would (for tests and scripts)."""
        [b] = [b for b in self.buttons() if b.text() == text]
        b.click()
