"""
The satellites as a checklist (core/plan/tree.py drawn in a QTreeWidget):
kind → series → satellite → mode, with a box per mode name above it and a
search line. Shared by the imaging plan and the satellite pass docks.
"""
from __future__ import annotations

import time
from typing import Optional, Sequence

from qgis.PyQt.QtCore import Qt, pyqtSignal
from qgis.PyQt.QtGui import QBrush, QColor, QIcon, QPixmap
from qgis.PyQt.QtWidgets import (
    QCheckBox, QHBoxLayout, QLabel, QLineEdit, QPushButton, QTreeWidget, QTreeWidgetItem, QVBoxLayout, QWidget,
)

from ..core.plan.satellites import SatelliteSpec
from ..core.plan.tree import Group, Listed, SatelliteChoice, facts_of, is_old, label_of, source_text, tle_age_days
from ..core.plan.output import js_round

ROLE = Qt.ItemDataRole.UserRole
OLD_COLOR = QColor("#b45309")


class SatelliteTree(QWidget):
    """A checklist of satellites; `changed` when the checks change."""

    changed = pyqtSignal()

    def __init__(self, parent: Optional[QWidget] = None):
        super().__init__(parent)
        self.choice = SatelliteChoice()
        self._items: list[tuple[QTreeWidgetItem, object]] = []
        self._mode_boxes: list[tuple[QCheckBox, str]] = []
        self._busy = False

        self.count = QLabel()
        all_ = QPushButton(self.tr("すべて"))
        none = QPushButton(self.tr("解除"))
        all_.clicked.connect(lambda: self.check_all(True))
        none.clicked.connect(lambda: self.check_all(False))
        self.search = QLineEdit()
        self.search.setPlaceholderText(self.tr("衛星名・ID・シリーズで絞り込み"))
        self.search.setClearButtonEnabled(True)
        self.search.textChanged.connect(self._filter)
        head = QHBoxLayout()
        head.addWidget(self.count)
        head.addStretch(1)
        head.addWidget(all_)
        head.addWidget(none)
        self.modes = QHBoxLayout()
        self.tree = QTreeWidget()
        self.tree.setHeaderHidden(True)
        self.tree.setColumnCount(1)
        self.tree.setMinimumHeight(140)
        self.tree.itemChanged.connect(self._item_changed)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addLayout(head)
        layout.addWidget(self.search)
        layout.addLayout(self.modes)
        layout.addWidget(self.tree, 1)

    # --- What the docks use ---------------------------------------------------------------

    def set(self, sats: Sequence[SatelliteSpec]) -> None:
        """Lists `sats`, keeping which were checked (new ones start checked, a satellite's other modes unchecked)."""
        open_keys = {key for item, key in self._groups() if item.isExpanded()}
        first = not self.choice.listed
        added = self.choice.set(sats)
        self._busy = True
        self.tree.clear()
        self._items = []
        now = time.time() * 1000
        many = len(self.choice.listed) <= 8

        def add(parent, node) -> None:
            if isinstance(node, Group):
                item = QTreeWidgetItem([node.name])
                item.setFlags(item.flags() | Qt.ItemFlag.ItemIsUserCheckable)
                item.setData(0, ROLE, node)
                item.setToolTip(0, self.tr("{name}をすべて選択").format(name=node.name))
                (parent.addChild(item) if isinstance(parent, QTreeWidgetItem) else self.tree.addTopLevelItem(item))
                self._items.append((item, node))
                for child in node.children:
                    add(item, child)
                item.setExpanded(node.key in open_keys or (first and (node.level == 1 or many))
                                 or any(m.key in added for m in node.members))
            else:
                item = QTreeWidgetItem([label_of(node)])
                item.setFlags(item.flags() | Qt.ItemFlag.ItemIsUserCheckable)
                item.setData(0, ROLE, node)
                facts = facts_of(node.sat)
                item.setToolTip(0, f"{source_text(node.sat, now)}\n{facts}")
                if is_old(node.sat, now):
                    item.setText(0, f"{label_of(node)}（TLE 古い）")
                    item.setForeground(0, QBrush(OLD_COLOR))
                    item.setToolTip(0, f"{source_text(node.sat, now)}\n{facts}\n"
                                    + self.tr("TLE の元期が {days} 日前。予測がずれる場合あり").format(days=js_round(tle_age_days(node.sat, now), 0)))
                parent.addChild(item)
                self._items.append((item, node))

        for kind in self.choice.tree():
            add(self.tree, kind)
        self._busy = False
        self._mode_bar()
        self.refresh()
        self._filter(self.search.text())

    def checked(self) -> list[SatelliteSpec]:
        return self.choice.checked()

    def check_all(self, on: bool) -> None:
        self.choice.check_all(on)
        self.refresh()
        self.changed.emit()

    def check_only(self, names: Sequence[str]) -> None:
        self.choice.check_only(names)
        self.refresh()
        self.changed.emit()

    def items(self) -> list[Listed]:
        return self.choice.listed

    def set_markers(self, colors: dict) -> None:
        """A colour swatch before each satellite whose key is in `colors` (the satellite pass dock's colours)."""
        self._busy = True
        for item, node in self._items:
            if isinstance(node, Group):
                continue
            color = colors.get(node.key)
            if color:
                pixmap = QPixmap(12, 12)
                pixmap.fill(QColor(color))
                item.setIcon(0, QIcon(pixmap))
            else:
                item.setIcon(0, QIcon())
        self._busy = False

    # --- Drawing ----------------------------------------------------------------------------

    def _groups(self):
        return [(item, node.key) for item, node in self._items if isinstance(node, Group)]

    def refresh(self) -> None:
        """Shows the checks, the groups' states and the counts."""
        self._busy = True
        for item, node in self._items:
            if isinstance(node, Group):
                on, all_ = self.choice.tally(node.members)
                state = Qt.CheckState.Checked if on == all_ else Qt.CheckState.PartiallyChecked if on else Qt.CheckState.Unchecked
                item.setCheckState(0, state)
                item.setText(0, f"{node.name}（{on} / {all_}）")
            else:
                item.setCheckState(0, Qt.CheckState.Checked if node.checked else Qt.CheckState.Unchecked)
        for box, mode in self._mode_boxes:
            on, all_ = self.choice.mode_state(mode)
            box.blockSignals(True)
            box.setCheckState(Qt.CheckState.Checked if on == all_ else Qt.CheckState.PartiallyChecked if on else Qt.CheckState.Unchecked)
            box.blockSignals(False)
        on, all_ = self.choice.tally()
        self.count.setText(self.tr("衛星（{on} / {all}）").format(on=on, all=all_))
        self._busy = False

    def _mode_bar(self) -> None:
        while self.modes.count():
            w = self.modes.takeAt(0).widget()
            if w:
                w.deleteLater()
        self._mode_boxes = []
        names = self.choice.mode_names()
        if not names:
            return
        title = QLabel(self.tr("撮像モード"))
        title.setToolTip(self.tr("モードごとに、そのモードを持つ衛星をまとめて選択（衛星ごとの選択は下の一覧で）"))
        self.modes.addWidget(title)
        for name in names:
            box = QCheckBox(name)
            box.setTristate(False)
            box.clicked.connect(lambda on, n=name: self._mode_clicked(n, on))
            self.modes.addWidget(box)
            self._mode_boxes.append((box, name))
        self.modes.addStretch(1)

    def _mode_clicked(self, mode: str, on: bool) -> None:
        self.choice.check_mode(mode, on)
        self.refresh()
        self.changed.emit()

    def _item_changed(self, item: QTreeWidgetItem, column: int) -> None:
        if self._busy:
            return
        node = item.data(0, ROLE)
        on = item.checkState(0) != Qt.CheckState.Unchecked
        if isinstance(node, Group):
            self.choice.choose(node.members, on)
        else:
            node.checked = on
        self.refresh()
        self.changed.emit()

    def _filter(self, text: str) -> None:
        for item, node in self._items:
            if not isinstance(node, Group):
                item.setHidden(not SatelliteChoice.matches(node, text))
        # Groups: hidden when all they hold is; opened while searching.
        for item, node in reversed(self._items):
            if isinstance(node, Group):
                shown = any(not item.child(i).isHidden() for i in range(item.childCount()))
                item.setHidden(not shown)
                if text.strip() and shown:
                    item.setExpanded(True)
