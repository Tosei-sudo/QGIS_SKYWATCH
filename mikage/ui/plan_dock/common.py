"""
What the 撮像計画ドック's parts share (ui/plan_dock).
"""
from __future__ import annotations

import time
from typing import Callable, Optional

from qgis.PyQt.QtCore import QAbstractTableModel, QModelIndex, Qt
from qgis.PyQt.QtGui import QBrush, QColor

from ...core.plan.api import Opportunity
from ...core.plan.api import (
    is_old_tle,
)
from ...core.plan.api import SatelliteSpec

#: The most opportunities listed as rows (the CSV and the layer take them all).
MAX_ROWS = 2000

OLD_COLOR = QColor("#b45309")

DAYS = (1, 3, 7, 14, 30)

#: How often the reminders are checked, ms.
REMIND_EVERY = 20000


def _plain(value):
    """An attribute value as Python (a NULL QVariant as None)."""
    if value is None:
        return None
    if hasattr(value, "isNull") and value.isNull():
        return None
    return value


def _now_ms() -> float:
    return time.time() * 1000


class Column:
    def __init__(self, key: str, label: str, text: Callable[[Opportunity], str], title: Optional[Callable[[Opportunity], str]] = None):
        self.key, self.label, self.text, self.title = key, label, text, title


class OpportunityModel(QAbstractTableModel):
    """The listed opportunities, as the viewer's table."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.columns: list[Column] = []
        self.ops: list[Opportunity] = []
        self.sats: list[SatelliteSpec] = []
        self.rest = 0

    def set(self, columns: list[Column], ops: list[Opportunity], sats: list[SatelliteSpec]) -> None:
        self.beginResetModel()
        self.columns, self.sats = columns, sats
        self.ops = ops[:MAX_ROWS]
        self.rest = max(0, len(ops) - MAX_ROWS)
        self.endResetModel()

    def rowCount(self, parent=QModelIndex()) -> int:  # noqa: N802 (Qt's name)
        return 0 if parent.isValid() else len(self.ops) + (1 if self.rest else 0)

    def columnCount(self, parent=QModelIndex()) -> int:  # noqa: N802
        return 0 if parent.isValid() else len(self.columns)

    def headerData(self, section, orientation, role=Qt.ItemDataRole.DisplayRole):  # noqa: N802
        if orientation == Qt.Orientation.Horizontal and role == Qt.ItemDataRole.DisplayRole and section < len(self.columns):
            return self.columns[section].label
        return None

    def data(self, index, role=Qt.ItemDataRole.DisplayRole):
        if not index.isValid():
            return None
        row, column = index.row(), self.columns[index.column()]
        if row >= len(self.ops):
            if role == Qt.ItemDataRole.DisplayRole and index.column() == 0:
                return self.tr("ほか {n} 件（並べ替えで先頭が変化。CSV とレイヤーにはすべて出力）").format(n=f"{self.rest:,}")
            return None
        op = self.ops[row]
        if role == Qt.ItemDataRole.DisplayRole:
            return column.text(op)
        if role == Qt.ItemDataRole.ToolTipRole and column.title:
            return column.title(op) or None
        if role == Qt.ItemDataRole.ForegroundRole and column.key == "satellite" and is_old_tle(op, self.sats[op.satellite]):
            return QBrush(OLD_COLOR)
        return None

    def op_at(self, row: int) -> Optional[Opportunity]:
        return self.ops[row] if 0 <= row < len(self.ops) else None
