"""
The image catalog's list (a QTableView model): the catalog's roles and
columns, 場所, and, for 「類似画像の検索」, 重なり. A click on a heading sorts
by it, empty values last whichever way.
"""
from __future__ import annotations

from typing import Any, Optional, Sequence

from qgis.PyQt.QtCore import QAbstractTableModel, QModelIndex, Qt

from ..core.catalog.config import CatalogConfig
from ..core.catalog.search import CatalogRecord, Column, cell_text, list_columns, place_text, sort_records

PLACE = Column("place", "場所")
OVERLAP = Column("overlap", "重なり")


class CatalogModel(QAbstractTableModel):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.columns: list[Column] = []
        self.records: list[CatalogRecord] = []
        #: The share of the image compared with each record covers (「類似画像の検索」 only), by id(record).
        self.overlaps: Optional[dict[int, Optional[float]]] = None

    def set_records(self, catalog: Optional[CatalogConfig], records: Sequence[CatalogRecord],
                    overlaps: Optional[dict[int, Optional[float]]] = None) -> None:
        self.beginResetModel()
        self.columns = (list_columns(catalog) if catalog else []) + [PLACE] + ([OVERLAP] if overlaps is not None else [])
        self.records = list(records)
        self.overlaps = overlaps
        self.endResetModel()

    def rowCount(self, parent: QModelIndex = QModelIndex()) -> int:  # noqa: N802 (Qt's name)
        return 0 if parent.isValid() else len(self.records)

    def columnCount(self, parent: QModelIndex = QModelIndex()) -> int:  # noqa: N802
        return 0 if parent.isValid() else len(self.columns)

    def text(self, record: CatalogRecord, key: str) -> str:
        if key == "place":
            return place_text(record)
        if key == "overlap":
            v = (self.overlaps or {}).get(id(record))
            return "" if v is None else f"{round(v * 100)}%"
        return cell_text(record, key)

    def data(self, index: QModelIndex, role: int = Qt.ItemDataRole.DisplayRole) -> Any:
        if not index.isValid():
            return None
        record = self.records[index.row()]
        key = self.columns[index.column()].key
        if role == Qt.ItemDataRole.DisplayRole:
            return self.text(record, key)
        if role == Qt.ItemDataRole.ToolTipRole and key == "place" and record.source:
            return record.source.value
        if role == Qt.ItemDataRole.TextAlignmentRole and key in ("angle", "azimuth", "overlap"):
            return int(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        return None

    def headerData(self, section: int, orientation, role: int = Qt.ItemDataRole.DisplayRole) -> Any:  # noqa: N802
        if orientation == Qt.Orientation.Horizontal and role == Qt.ItemDataRole.DisplayRole and 0 <= section < len(self.columns):
            return self.columns[section].label
        return None

    def sort(self, column: int, order=Qt.SortOrder.AscendingOrder) -> None:
        if not 0 <= column < len(self.columns):
            return
        key = self.columns[column].key
        descending = order == Qt.SortOrder.DescendingOrder
        self.layoutAboutToBeChanged.emit()
        if key in ("place", "overlap"):
            def value(r: CatalogRecord):
                return (self.overlaps or {}).get(id(r)) if key == "overlap" else (r.source.kind if r.source else None)
            present = sorted((r for r in self.records if value(r) is not None), key=value, reverse=descending)
            self.records = present + [r for r in self.records if value(r) is None]
        else:
            self.records = sort_records(self.records, key, descending)
        self.layoutChanged.emit()
