"""
画像の評価 (core/image_reviews.py): the window of one image's ratings. The
first row is the whole image (画像全体: the sea far from any named place
too), then the named areas the image shows (the named area layer, cut by
the footprint): those the image's records are in first, then the smaller
first; a search box narrows a long list. Each row: the rating (empty ·
使える · 一部 · 使えない), the cloud seen, the reasons (不可の理由's words) and
a note. The row chosen is lit on the map.

A row not rated yet takes a first rating from the 不可範囲 drawn on the
image (half or more of it covered → 使えない, some → 一部; their reasons),
said as such until saved. Saving writes the rows rated and deletes those
put back to empty.
"""
from __future__ import annotations

from typing import Callable, Optional, Sequence

from qgis.core import QgsWkbTypes
from qgis.gui import QgsRubberBand
from qgis.PyQt.QtCore import Qt, pyqtSignal
from qgis.PyQt.QtGui import QColor, QFont
from qgis.PyQt.QtWidgets import (
    QAbstractItemView, QComboBox, QDialog, QDialogButtonBox, QHBoxLayout, QHeaderView, QLabel, QLineEdit, QMenu, QPushButton,
    QSpinBox, QTableWidget, QTableWidgetItem, QToolButton, QVBoxLayout,
)

from ..core import edit_stamp
from ..core.esri import EsriError
from ..core.named_areas import settings_for
from ..core.image_reviews import COLORS, RATINGS, WHOLE, ImageReview, area_key, from_unseen, load_reviews, save_reviews
from ..core.obs.api import local_time
from ..qgis_io.geometry import from_wgs84, rings_geometry
from ..qgis_io.reviews_io import AreaChoice, areas_over, choices_of, whole_unseen
from ..qgis_io.tasks import error_text, run_task
from .docks import plain_label, plain_tip
from .fit import fit_text

#: The columns.
COLUMNS = ("地名", "評価", "雲量", "理由", "メモ", "前回")
NAME, RATING, CLOUD, REASONS, NOTE, LAST = range(len(COLUMNS))


class _Row:
    """One row of the window: what it rates, what was saved, and its first rating from the 不可範囲."""

    def __init__(self, key: str, area_id: str, name: str, rings: list, saved: Optional[ImageReview],
                 draft: tuple = ("", ()), records: int = 0):
        self.key, self.area_id, self.name, self.rings = key, area_id, name, rings
        self.saved, self.draft, self.records = saved, draft, records


class ImageReviewDialog(QDialog):
    """The ratings of one image (opened by 「評価…」, or by 完了 when the image has none)."""

    #: The image's rows were saved: its id and its rows now.
    saved = pyqtSignal(str, list)

    def __init__(self, context, image, footprint: Optional[list], canvas=None, points: Sequence[tuple] = (),
                 unseen: Sequence[tuple] = (), reasons: Sequence[str] = (), later: bool = False, parent=None):
        super().__init__(parent)
        fit_text(self)
        self.context, self.image, self.footprint, self.canvas = context, image, footprint, canvas
        self.points, self.unseen, self.vocabulary = list(points), list(unseen), list(reasons)
        self.rows: list[_Row] = []
        self.task = None
        self.band: Optional[QgsRubberBand] = None
        self.setWindowTitle(self.tr("画像の評価"))
        self.resize(900, 520)
        layout = QVBoxLayout(self)
        when = local_time(image.time) if getattr(image, "time", None) is not None else self.tr("日時不明")
        head = plain_label(QLabel(f"{when}  {image.name or image.id}"))
        head.setStyleSheet("font-weight: bold;")
        layout.addWidget(head)
        hint = plain_label(QLabel(self.tr("地名ごとに、判読に使えるかを付ける（付けた行だけ保存。空に戻すとその行を消す）")))
        hint.setWordWrap(True)
        layout.addWidget(hint)
        top = QHBoxLayout()
        self.search = QLineEdit()
        self.search.setPlaceholderText(self.tr("地名で絞り込み"))
        self.search.setClearButtonEnabled(True)
        self.search.textChanged.connect(lambda _t: self._filter())
        top.addWidget(self.search, 1)
        self.only_rated = QToolButton()
        self.only_rated.setText(self.tr("付けた行のみ"))
        self.only_rated.setCheckable(True)
        self.only_rated.toggled.connect(lambda _on: self._filter())
        top.addWidget(self.only_rated)
        layout.addLayout(top)
        self.table = QTableWidget(0, len(COLUMNS))
        self.table.setHorizontalHeaderLabels([self.tr(c) for c in COLUMNS])
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.table.verticalHeader().hide()
        header = self.table.horizontalHeader()
        header.setSectionResizeMode(NAME, QHeaderView.ResizeMode.Stretch)
        header.setSectionResizeMode(NOTE, QHeaderView.ResizeMode.Stretch)
        self.table.currentCellChanged.connect(lambda row, _c, _r, _p: self._light(row))
        layout.addWidget(self.table, 1)
        self.status = plain_label(QLabel(self.tr("読み込み中…")))
        self.status.setWordWrap(True)
        layout.addWidget(self.status)
        buttons = QDialogButtonBox()
        self.save_button = QPushButton(self.tr("保存"))
        self.save_button.setDefault(True)
        self.save_button.setEnabled(False)
        self.save_button.clicked.connect(lambda _on=False: self.save())
        buttons.addButton(self.save_button, QDialogButtonBox.ButtonRole.AcceptRole)
        self.later_button = QPushButton(self.tr("あとで") if later else self.tr("閉じる"))
        self.later_button.setToolTip(self.tr("保存せずに閉じる"))
        self.later_button.clicked.connect(self.reject)
        buttons.addButton(self.later_button, QDialogButtonBox.ButtonRole.RejectRole)
        layout.addWidget(buttons)
        self.finished.connect(lambda _r: self._unlight())

    # ---- reading ------------------------------------------------------------------------------------

    @property
    def source(self):
        return self.context.config.image_reviews

    def _token(self, url: str, token: Optional[str]) -> Optional[str]:
        return self.context.token_for(url, token)

    def load(self) -> None:
        """Reads the image's rows and the named areas it shows (in a task), then lists them."""
        source, image, footprint = self.source, self.image, self.footprint
        if source is None:
            self.status.setText(self.tr("画像の評価の表が未設定（管理者に連絡: imageReviews）［E703］"))
            return
        client = self.context.esri()
        token = self._token(source.url, source.token)
        named = settings_for(self.context.config)
        named_token = self._token(named.areas.url, named.areas.token) if named is not None else None

        def work(_task):
            saved = load_reviews(client, source, [image.id], token).get(image.id, [])
            found, more, problem = [], False, ""
            if named is not None and footprint:
                try:
                    found, more = areas_over(client, named, footprint, named_token)
                except EsriError as error:
                    problem = error_text(error)
            return saved, found, more, problem

        def done(result) -> None:
            self.task = None
            saved, found, more, problem = result
            self._fill(saved, found, more, problem, named is not None)

        def failed(message: str) -> None:
            self.task = None
            self.status.setText(self.tr("読み込みに失敗: {m}").format(m=message))

        self.task = run_task(self.tr("画像の評価の取得"), work, done, failed)

    def _fill(self, saved: list[ImageReview], found: list, more: bool, problem: str, named: bool) -> None:
        by_key = {r.key: r for r in saved}
        choices: list[AreaChoice] = choices_of(found, self.footprint, self.points, self.unseen) if found and self.footprint else []
        share, reasons = whole_unseen(self.footprint, self.unseen) if self.footprint and self.unseen else (0.0, ())
        rows = [_Row("", "", WHOLE, self.footprint or [], by_key.pop("", None), from_unseen(share, reasons))]
        for c in choices:
            key = area_key(c.area_id, c.name)
            rows.append(_Row(key, c.area_id, c.name, c.rings, by_key.pop(key, None), from_unseen(c.unseen, c.reasons), c.records))
        # Rows saved of named areas no longer shown (moved, made inactive): kept in the list, to be seen and cleared.
        for r in by_key.values():
            rows.append(_Row(r.key, r.area_id, r.area_name, r.rings or [], r))
        self.rows = rows
        self.table.setRowCount(len(rows))
        drafts = 0
        for i, row in enumerate(rows):
            drafts += self._put(i, row)
        self.table.resizeColumnToContents(RATING)
        self.table.resizeColumnToContents(CLOUD)
        self.table.resizeColumnToContents(LAST)
        notes = [self.tr("地名 {n} 件").format(n=len(rows) - 1)]
        if not named:
            notes = [self.tr("地名の表が未設定のため画像全体だけ（管理者に連絡: namedAreas）")]
        elif not self.footprint:
            notes = [self.tr("画像の範囲が分からないため画像全体だけ")]
        if more:
            notes.append(self.tr("多いため一部のみ（検索で絞り込み）"))
        if problem:
            notes.append(self.tr("地名の取得に失敗: {m}").format(m=problem))
        if drafts:
            notes.append(self.tr("不可範囲から {n} 行に案を入れた（保存するまでは案）").format(n=drafts))
        self.status.setText("。".join(notes))
        self.save_button.setEnabled(True)
        self._filter()
        if self.table.rowCount():
            self.table.selectRow(0)

    def _put(self, i: int, row: _Row) -> bool:
        """Fills one row's cells; whether it took a first rating from the 不可範囲."""
        s = row.saved
        rating, reasons = (s.rating, s.reasons) if s is not None else row.draft
        drafted = s is None and bool(rating)
        name = QTableWidgetItem(row.name + (self.tr("（記録 {n}）").format(n=row.records) if row.records else ""))
        name.setFlags(name.flags() & ~Qt.ItemFlag.ItemIsEditable)
        if row.key == "":
            font = QFont(name.font())
            font.setBold(True)
            name.setFont(font)
        if row.area_id:
            name.setToolTip(plain_tip(self.tr("管理番号 {i}").format(i=row.area_id)))
        self.table.setItem(i, NAME, name)
        combo = QComboBox()
        combo.addItem("")
        combo.addItems([self.tr(r) for r in RATINGS])
        combo.setCurrentIndex(RATINGS.index(rating) + 1 if rating in RATINGS else 0)
        if drafted:
            combo.setToolTip(self.tr("不可範囲からの案（保存するまでは案）"))
        combo.currentIndexChanged.connect(lambda _i, c=combo: self._colour(c))
        self._colour(combo, drafted)
        self.table.setCellWidget(i, RATING, combo)
        cloud = QSpinBox()
        cloud.setRange(-1, 100)
        cloud.setSpecialValueText(self.tr("—"))
        cloud.setSuffix(" %")
        cloud.setValue(int(round(s.cloud)) if s is not None and s.cloud is not None else -1)
        cloud.setToolTip(self.tr("見た雲量（%）。分からなければ —"))
        self.table.setCellWidget(i, CLOUD, cloud)
        pick = QToolButton()
        pick.setPopupMode(QToolButton.ToolButtonPopupMode.InstantPopup)
        menu = QMenu(pick)
        for word in dict.fromkeys([*self.vocabulary, *reasons]):
            action = menu.addAction(word)
            action.setCheckable(True)
            action.setChecked(word in reasons)
            action.toggled.connect(lambda _on, b=pick, m=menu: self._reasons_text(b, m))
        pick.setMenu(menu)
        self._reasons_text(pick, menu)
        self.table.setCellWidget(i, REASONS, pick)
        note = QLineEdit(s.note if s is not None else "")
        note.setMaxLength(2000)
        self.table.setCellWidget(i, NOTE, note)
        last = QTableWidgetItem(f"{s.reviewed_by}・{local_time(s.reviewed_at)}" if s is not None and s.reviewed_at else "")
        last.setFlags(last.flags() & ~Qt.ItemFlag.ItemIsEditable)
        self.table.setItem(i, LAST, last)
        return drafted

    @staticmethod
    def _colour(combo: QComboBox, drafted: bool = False) -> None:
        """The rating's colour (a first rating from the 不可範囲 in italics until changed or saved)."""
        text = combo.currentText()
        style = ("font-style: italic;" if drafted else "") + (f"color: {COLORS[text]};" if text in COLORS else "")
        combo.setStyleSheet(f"QComboBox {{ {style} }}" if style else "")

    @staticmethod
    def _reasons_text(button: QToolButton, menu: QMenu) -> None:
        chosen = [a.text() for a in menu.actions() if a.isChecked()]
        button.setText("・".join(chosen) if chosen else "—")

    # ---- looking -----------------------------------------------------------------------------------

    def rating_of(self, i: int) -> str:
        combo = self.table.cellWidget(i, RATING)
        return combo.currentText() if combo is not None else ""

    def set_rating(self, i: int, rating: str) -> None:
        combo = self.table.cellWidget(i, RATING)
        combo.setCurrentIndex(RATINGS.index(rating) + 1 if rating in RATINGS else 0)

    def row_of(self, name: str) -> int:
        return next(i for i, r in enumerate(self.rows) if r.name == name)

    def _filter(self) -> None:
        text = self.search.text().strip().casefold()
        only = self.only_rated.isChecked()
        for i, row in enumerate(self.rows):
            rated = bool(self.rating_of(i))
            hide = (bool(text) and text not in row.name.casefold() and text not in row.area_id.casefold() and row.key != "") \
                or (only and not rated)
            self.table.setRowHidden(i, hide)

    def _light(self, i: int) -> None:
        """The row chosen lit on the map."""
        self._unlight()
        if self.canvas is None or not 0 <= i < len(self.rows) or not self.rows[i].rings:
            return
        g = rings_geometry(self.rows[i].rings)
        g.transform(from_wgs84(self.canvas.mapSettings().destinationCrs()))
        band = QgsRubberBand(self.canvas, QgsWkbTypes.GeometryType.PolygonGeometry)
        band.setStrokeColor(QColor("#f59e0b"))
        band.setFillColor(QColor(245, 158, 11, 50))
        band.setWidth(2)
        band.setToGeometry(g, None)
        self.band = band

    def _unlight(self) -> None:
        band, self.band = self.band, None
        if band is None:
            return
        try:
            scene = band.scene()
            if scene is not None:
                scene.removeItem(band)
        except RuntimeError:
            pass  # the map gone with the plugin

    # ---- saving ------------------------------------------------------------------------------------

    def wanted(self) -> tuple[list[ImageReview], list[str]]:
        """The rows rated (to write) and the keys put back to empty (to delete)."""
        image = self.image
        out, cleared = [], []
        for i, row in enumerate(self.rows):
            rating = self.rating_of(i)
            if not rating:
                if row.saved is not None:
                    cleared.append(row.key)
                continue
            cloud = self.table.cellWidget(i, CLOUD).value()
            menu = self.table.cellWidget(i, REASONS).menu()
            out.append(ImageReview(
                image_id=image.id, rating=rating, area_id=row.area_id, area_name="" if row.key == "" else row.name,
                cloud=float(cloud) if cloud >= 0 else None, reasons=tuple(a.text() for a in menu.actions() if a.isChecked()),
                note=self.table.cellWidget(i, NOTE).text().strip(), image_name=image.name or "",
                image_time=getattr(image, "time", None), satellite=getattr(image, "satellite", "") or "", rings=row.rings or None))
        return out, cleared

    def save(self, then: Optional[Callable[[], None]] = None) -> None:
        source, image = self.source, self.image
        if source is None or self.task is not None:
            return
        wanted, cleared = self.wanted()
        client, token, user = self.context.esri(), self._token(source.url, source.token), edit_stamp.user_name()
        self.save_button.setEnabled(False)
        self.status.setText(self.tr("保存中…"))

        def done(result) -> None:
            self.task = None
            self.save_button.setEnabled(True)
            if result.problems:
                self.status.setText(self.tr("一部を保存できない: {m}").format(m="、".join(result.problems)))
                return
            self.saved.emit(image.id, list(result.rows))
            self.accept()
            if then:
                then()

        def failed(message: str) -> None:
            self.task = None
            self.save_button.setEnabled(True)
            self.status.setText(self.tr("保存に失敗: {m}").format(m=message))

        self.task = run_task(self.tr("画像の評価の保存"),
                             lambda _t: save_reviews(client, source, image.id, wanted, cleared, token, user=user), done, failed)
