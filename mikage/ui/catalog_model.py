"""
The image catalog's list (a QTableView model): the catalog's roles and
columns, 場所 (not for a catalog without image files), and, for 「類似画像の検索」, 重なり. A click on a heading sorts
by it, empty values last whichever way (the rows chosen follow their images).
「続きを読む」 adds rows at the end (`add_records`). With `assignments`, 分担
next to the image's id; with `imageReviews`, 評価 next to it (画像の評価,
core/image_reviews.py: 「一部（使えない 1/3）」, coloured, each named area in its tip).
"""
from __future__ import annotations

from typing import Any, Optional, Sequence

from qgis.PyQt.QtCore import QAbstractTableModel, QModelIndex, Qt
from qgis.PyQt.QtGui import QColor

from ..core.catalog.api import CatalogConfig
from ..core.catalog.api import CatalogRecord, Column, cell_text, list_columns, place_text, sort_records
from ..core.obs.api import work_text
from ..core.image_reviews import COLORS, RATINGS, ImageReview, catalog_rating, catalog_text, lines_of

PLACE = Column("place", "場所")
OVERLAP = Column("overlap", "重なり")
WORK = Column("work", "分担")
REVIEW = Column("review", "評価")
#: The 分担 (and 評価) cell before the table has been read.
WORK_UNKNOWN = "…"


class CatalogModel(QAbstractTableModel):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.columns: list[Column] = []
        self.records: list[CatalogRecord] = []
        #: The share of the image compared with each record covers (「類似画像の検索」 only), by id(record).
        self.overlaps: Optional[dict[int, Optional[float]]] = None
        #: 分担 of each image by its id ((状態, 担当者); `assignments` only, else None: no column). Images not in it are not read yet.
        self.work: Optional[dict[str, tuple[str, str]]] = None
        #: 画像の評価 of each image by its id (`imageReviews` only, else None: no column). Images not in it are not read yet.
        self.reviews: Optional[dict[str, list[ImageReview]]] = None

    def set_records(self, catalog: Optional[CatalogConfig], records: Sequence[CatalogRecord],
                    overlaps: Optional[dict[int, Optional[float]]] = None, work: bool = False, reviews: bool = False) -> None:
        self.beginResetModel()
        # 場所 says nothing of a catalog without image files (`imageFiles: false`).
        place = [PLACE] if catalog is None or catalog.image_files else []
        columns = (list_columns(catalog) if catalog else []) + place + ([OVERLAP] if overlaps is not None else [])
        # 分担 and 評価 next to the image's id (seen without scrolling sideways).
        self.columns = columns[:1] + ([WORK] if work else []) + ([REVIEW] if reviews else []) + columns[1:]
        self.work = {} if work else None
        self.reviews = {} if reviews else None
        self.records = list(records)
        self.overlaps = overlaps
        self.endResetModel()

    def add_records(self, records: Sequence[CatalogRecord]) -> None:
        """More images (「続きを読む」) after those listed."""
        records = list(records)
        if not records:
            return
        self.beginInsertRows(QModelIndex(), len(self.records), len(self.records) + len(records) - 1)
        self.records.extend(records)
        self.endInsertRows()

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
        if key == "work":
            state = (self.work or {}).get(record.id)
            return work_text(state) if state else WORK_UNKNOWN
        if key == "review":
            rows = (self.reviews or {}).get(record.id)
            return WORK_UNKNOWN if rows is None else catalog_text(rows)
        return cell_text(record, key)

    def work_state(self, record: CatalogRecord) -> Optional[tuple[str, str]]:
        """(状態, 担当者) of a record's image; None before it was read (or without `assignments`)."""
        return (self.work or {}).get(record.id) if self.work is not None else None

    def set_work(self, work: dict[str, tuple[str, str]]) -> None:
        """The 分担 read for the listed images."""
        if self.work is None:
            return
        self.work = dict(work)
        column = next((i for i, c in enumerate(self.columns) if c.key == "work"), -1)
        if column >= 0 and self.records:
            self.dataChanged.emit(self.index(0, column), self.index(len(self.records) - 1, column))

    def set_reviews(self, reviews: dict[str, list[ImageReview]]) -> None:
        """The 画像の評価 read for the listed images (those without a row: an empty list)."""
        if self.reviews is None:
            return
        self.reviews = dict(reviews)
        column = next((i for i, c in enumerate(self.columns) if c.key == "review"), -1)
        if column >= 0 and self.records:
            self.dataChanged.emit(self.index(0, column), self.index(len(self.records) - 1, column))

    def data(self, index: QModelIndex, role: int = Qt.ItemDataRole.DisplayRole) -> Any:
        if not index.isValid():
            return None
        record = self.records[index.row()]
        key = self.columns[index.column()].key
        if role == Qt.ItemDataRole.DisplayRole:
            return self.text(record, key)
        if role == Qt.ItemDataRole.ToolTipRole and key == "place" and record.source:
            return record.source.value
        if key == "review" and role in (Qt.ItemDataRole.ToolTipRole, Qt.ItemDataRole.ForegroundRole):
            rows = (self.reviews or {}).get(record.id) or []
            if role == Qt.ItemDataRole.ToolTipRole:
                return "\n".join(lines_of(rows)) or None
            rating = catalog_rating(rows)
            return QColor(COLORS[rating]) if rating else None
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
        before = list(self.records)
        if key in ("place", "overlap", "work", "review"):
            def value(r: CatalogRecord):
                if key == "work":
                    state = (self.work or {}).get(r.id)
                    return work_text(state) if state else None
                if key == "review":
                    # 使える, 一部, 使えない in that order (by how much is unusable), not rated last.
                    rows = (self.reviews or {}).get(r.id) or []
                    rating = catalog_rating(rows)
                    return (RATINGS.index(rating), catalog_text(rows)) if rating else None
                return (self.overlaps or {}).get(id(r)) if key == "overlap" else (r.source.kind if r.source else None)
            present = sorted((r for r in self.records if value(r) is not None), key=value, reverse=descending)
            self.records = present + [r for r in self.records if value(r) is None]
        else:
            self.records = sort_records(self.records, key, descending)
        # The rows chosen (and the current one) follow their images.
        row_of = {id(r): i for i, r in enumerate(self.records)}
        old = self.persistentIndexList()
        self.changePersistentIndexList(old, [self.index(row_of[id(before[i.row()])], i.column()) for i in old])
        self.layoutChanged.emit()
