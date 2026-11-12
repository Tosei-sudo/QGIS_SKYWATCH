"""
「画像とショットの関連付け」 (tsudo, 2026-11-11: 「手の照合は、今の方法に加えて、
この画像カタログのレコードがこのショットという本当の手動関連付け画面がほしい」):
a target day's shots on the left, the image catalog's records over the
chosen shot on the right (the satellite asked for first, then the nearest
in time), both on the map; one record is linked to the shot by hand, or the
link taken off again. The rows, the ledger and the shot layer follow as
「撮像済みにする」 makes them (core/request/linking.py).
"""
from __future__ import annotations

import time
from datetime import date
from typing import Callable, Optional

from qgis.core import QgsRectangle
from qgis.PyQt.QtCore import QDate, Qt
from qgis.PyQt.QtGui import QBrush, QColor, QStandardItem, QStandardItemModel
from qgis.PyQt.QtWidgets import (
    QAbstractItemView, QCheckBox, QComboBox, QDateEdit, QDialog, QHBoxLayout, QHeaderView, QLabel, QPushButton, QSpinBox, QSplitter,
    QTableView, QVBoxLayout, QWidget,
)

from ..core.esri import EsriError
from ..core.request.follow import save_marked
from ..core.request.follow_job import requests_of
from ..core.request.linking import (
    MAX_IMAGES, find_images, gap_text, link_findings, link_text, links_of, time_text, units_of, unlink_findings, with_links,
)
from ..core.request.model import jst_day
from ..core.request.store import history_where, load_day, load_history, load_shots
from ..qgis_io.geometry import from_wgs84
from ..qgis_io.tasks import run_task
from .docks import ask_no
from .list_views import column_menus
from .plan_overlay import PlanOverlay

SHOT_COLUMNS = ("時刻", "システム", "衛星", "センサー・モード", "依頼", "撮像", "画像ID")
IMAGE_COLUMNS = ("撮像日時", "衛星・センサー", "画像ID", "時刻の差", "関連付け")
SHOWN = ("すべて", "未判定", "撮像済み", "未撮像")
COLORS = {"撮像済み": "#16a34a", "未撮像": "#dc2626", "未判定": "#6b7280"}
#: The map: the shot's outline, the other images, the image chosen.
SHOT_COLOR, IMAGE_COLOR, CHOSEN_COLOR = "#7c3aed", "#2563eb", "#f97316"


def _now_ms() -> float:
    return time.time() * 1000


def _hm(ms: Optional[float]) -> str:
    return time_text(ms)[11:]


class ShotLinkDialog(QDialog):
    """A day's shots and the catalog's images over each, to link one to the other by hand."""

    def __init__(self, dock, parent=None, saved: Optional[Callable[[], None]] = None):
        super().__init__(parent or dock)
        self.dock = dock
        self.context = dock.context
        #: Told when something was saved (the 「計画と実績の照合」 list reads the rows again).
        self.saved = saved or (lambda: None)
        self.setWindowTitle(self.tr("画像とショットの関連付け"))
        self.resize(1250, 620)
        self.overlay = PlanOverlay(dock.canvas)
        self.task = None
        self.units: list = []
        self.shown_units: list = []
        self.requests: dict = {}
        #: The day read (`2026-11-10`), once read.
        self.loaded_day: Optional[str] = None
        #: The shot to choose once the day is read (unit_key).
        self.wanted: Optional[str] = None
        self.candidates: list = []
        self.shown_images: list = []
        #: The shot and the days around it the images are of (key, days).
        self.searched: Optional[tuple] = None
        #: The shot the map was last moved to (not again when the list is shown again).
        self.zoomed: Optional[str] = None
        self._build()

    def _build(self) -> None:
        layout = QVBoxLayout(self)
        top = QHBoxLayout()
        top.addWidget(QLabel(self.tr("対象日")))
        today = jst_day(_now_ms())
        self.day = QDateEdit(QDate(today.year, today.month, today.day))
        self.day.setCalendarPopup(True)
        self.day.setDisplayFormat("yyyy-MM-dd")
        self.day.dateChanged.connect(lambda _=None: self.load())
        top.addWidget(self.day)
        self.system = QComboBox()
        self.system.currentIndexChanged.connect(lambda _=None: self.render_shots())
        top.addWidget(self.system)
        self.shown = QComboBox()
        self.shown.addItems([self.tr(s) for s in SHOWN])
        self.shown.currentIndexChanged.connect(lambda _=None: self.render_shots())
        top.addWidget(self.shown)
        self.load_button = QPushButton(self.tr("読み込み"))
        self.load_button.setToolTip(self.tr("その日のショット（出力履歴・撮像の範囲）を読み込み直す"))
        self.load_button.clicked.connect(lambda: self.load())
        top.addWidget(self.load_button)
        top.addStretch(1)
        layout.addLayout(top)
        self.status = QLabel()
        self.status.setWordWrap(True)
        self.status.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        layout.addWidget(self.status)

        split = QSplitter(Qt.Orientation.Horizontal)
        left = QWidget()
        left_layout = QVBoxLayout(left)
        left_layout.setContentsMargins(0, 0, 0, 0)
        left_layout.addWidget(QLabel(self.tr("ショット")))
        self.shot_model = QStandardItemModel(self)
        self.shot_table = self._table(self.shot_model, QAbstractItemView.SelectionMode.SingleSelection)
        self.shot_table.selectionModel().selectionChanged.connect(lambda *_: self._shot_changed())
        left_layout.addWidget(self.shot_table, 1)
        split.addWidget(left)

        right = QWidget()
        right_layout = QVBoxLayout(right)
        right_layout.setContentsMargins(0, 0, 0, 0)
        find = QHBoxLayout()
        find.addWidget(QLabel(self.tr("画像カタログ: 前後")))
        self.days = QSpinBox()
        self.days.setRange(0, 7)
        self.days.setValue(1)
        self.days.setSuffix(self.tr(" 日"))
        self.days.valueChanged.connect(lambda _=None: self.search())
        find.addWidget(self.days)
        self.only_fitting = QCheckBox(self.tr("依頼の衛星だけ"))
        self.only_fitting.setToolTip(self.tr("依頼が衛星・シリーズを指定していれば、その衛星（別名を含む）の画像だけを表示"))
        self.only_fitting.setChecked(True)
        self.only_fitting.toggled.connect(lambda _=None: self.render_images())
        find.addWidget(self.only_fitting)
        self.search_button = QPushButton(self.tr("画像を探す"))
        self.search_button.setToolTip(self.tr("選んだショットの範囲に重なる画像を、前後の日も含めて画像カタログから探す"))
        self.search_button.clicked.connect(lambda: self.search(force=True))
        find.addWidget(self.search_button)
        find.addStretch(1)
        right_layout.addLayout(find)
        self.image_model = QStandardItemModel(self)
        self.image_table = self._table(self.image_model, QAbstractItemView.SelectionMode.SingleSelection)
        self.image_table.selectionModel().selectionChanged.connect(lambda *_: self._image_changed())
        self.image_table.doubleClicked.connect(lambda _=None: self.link())
        right_layout.addWidget(self.image_table, 1)
        split.addWidget(right)
        split.setStretchFactor(0, 1)
        split.setStretchFactor(1, 1)
        split.setSizes([540, 710])
        layout.addWidget(split, 1)
        # The columns shown, hidden and in order from the header's right-click menu, kept per user.
        self.shot_columns = column_menus("shot-link-shots", self.shot_table, self)
        self.image_columns = column_menus("shot-link-images", self.image_table, self)

        hand = QHBoxLayout()
        self.link_button = QPushButton(self.tr("この画像をこのショットに関連付け"))
        self.link_button.setToolTip(self.tr("ショットの依頼をすべて撮像済みにし、画像IDを記録（繰り上げを戻し、期間内1回は完了）"))
        self.link_button.clicked.connect(lambda: self.link())
        self.unlink_button = QPushButton(self.tr("関連付けを外す（未判定に戻す）"))
        self.unlink_button.setToolTip(self.tr("ショットの依頼を未判定に戻す（次の照合でまた画像を探す。完了にした依頼は有効に戻す）"))
        self.unlink_button.clicked.connect(lambda: self.unlink("未判定"))
        self.missed_button = QPushButton(self.tr("未撮像にする"))
        self.missed_button.setToolTip(self.tr("ショットの依頼を未撮像に（次回の計算で持ち越し・繰り上げ候補）"))
        self.missed_button.clicked.connect(lambda: self.unlink("未撮像"))
        for b in (self.link_button, self.unlink_button, self.missed_button):
            hand.addWidget(b)
        hand.addStretch(1)
        close = QPushButton(self.tr("閉じる"))
        close.clicked.connect(self.close)
        hand.addWidget(close)
        layout.addLayout(hand)
        self._update_buttons()

    def _table(self, model: QStandardItemModel, mode) -> QTableView:
        table = QTableView()
        table.setModel(model)
        table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        table.setSelectionMode(mode)
        table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        table.verticalHeader().hide()
        table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.ResizeToContents)
        return table

    # --- Opening and closing ----------------------------------------------------------------------

    def open_at(self, day: date, key: Optional[str] = None) -> None:
        """Shows a day's shots, with the shot `key` (unit_key of a history row) chosen."""
        self.wanted = key
        q = QDate(day.year, day.month, day.day)
        if self.day.date() != q:
            self.day.setDate(q)  # reads the day
        elif self.loaded_day == day.isoformat() and self.task is None:
            self.render_shots()
        else:
            self.load()

    def showEvent(self, event) -> None:
        super().showEvent(event)
        self.paint()

    def closeEvent(self, event) -> None:
        self.overlay.remove()
        super().closeEvent(event)

    def chosen_day(self) -> date:
        d = self.day.date()
        return date(d.year(), d.month(), d.day())

    # --- Reading the day ------------------------------------------------------------------------

    def load(self) -> None:
        """Reads the day's rows (採用), shots and their requests."""
        dock, store = self.dock, self.dock.store
        if self.task is not None:
            return  # read once the task is over (_finished)
        if store is None:
            self.status.setText(self.tr("設定ファイルに撮像依頼の保存先（requestStores）なし"))
            return
        if not store.history:
            self.status.setText(self.tr("{s} に出力履歴（history）なし").format(s=store.label))
            return
        day = self.chosen_day().isoformat()
        client, token = self.context.esri(), dock.token()
        self.status.setText(self.tr("{d} のショットを読み込み中…").format(d=day))

        def work(task):
            notes = []
            rows = load_day(client, store, token, day, task.isCanceled)
            try:
                shots = load_shots(client, store, token, day, task.isCanceled)
            except EsriError as error:
                # The rows alone still make the shots (their own areas).
                shots = []
                notes.append(f"撮像の範囲を読めず、依頼の範囲で表示（{error}）")
            requests = requests_of(client, store, token, [r.request_id for r in rows if r.result == "採用"], task.isCanceled)
            return day, rows, shots, requests, notes

        def done(result):
            day, rows, shots, requests, notes = result
            self.requests = requests
            self.units = units_of(rows, shots, requests)
            self.loaded_day = day
            self.candidates, self.searched = [], None
            self._fill_systems()
            count = len(self.units)
            notes.insert(0, f"{day} のショット {count} 件" if count else f"{day} に出力した依頼なし")
            self.status.setText("。".join(notes))
            self._finished()
            self.render_shots()

        def failed(message):
            self.status.setText(self.tr("読み込み失敗: {m}").format(m=message))
            self._finished()

        self.task = run_task(self.tr("ショットの読み込み"), work, done, failed)
        self._update_buttons()

    def _finished(self) -> None:
        self.task = None
        self._update_buttons()
        if self.loaded_day is not None and self.loaded_day != self.chosen_day().isoformat():
            self.load()

    def _fill_systems(self) -> None:
        current = self.system.currentText()
        self.system.blockSignals(True)
        self.system.clear()
        self.system.addItem(self.tr("すべてのシステム"))
        for s in sorted({u.system for u in self.units if u.system}):
            self.system.addItem(s)
        index = self.system.findText(current)
        self.system.setCurrentIndex(index if index > 0 else 0)
        self.system.blockSignals(False)
        self.system.setVisible(self.system.count() > 2)

    # --- The shots ------------------------------------------------------------------------------

    def render_shots(self) -> None:
        keep = self.wanted or (self.chosen_unit().key if self.chosen_unit() else None)
        self.wanted = None
        system = self.system.currentText() if self.system.currentIndex() > 0 else ""
        shown = SHOWN[self.shown.currentIndex()] if self.shown.currentIndex() >= 0 else SHOWN[0]
        self.shown_units = [u for u in self.units if (not system or u.system == system) and (shown == "すべて" or u.captured == shown)]
        if keep and not any(u.key == keep for u in self.shown_units):
            # The shot asked for is shown whatever the filters.
            self.shown_units += [u for u in self.units if u.key == keep]
        self.shot_table.selectionModel().blockSignals(True)
        self.shot_model.clear()
        self.shot_model.setHorizontalHeaderLabels([self.tr(c) for c in SHOT_COLUMNS])
        for u in self.shown_units:
            items = [QStandardItem(t) for t in (_hm(u.pass_time), u.system, u.satellite, u.sensor_text, "・".join(u.names),
                                                 u.captured, u.image_id)]
            items[5].setForeground(QBrush(QColor(COLORS.get(u.captured, "#6b7280"))))
            if u.shot is not None:
                items[0].setToolTip(self.tr("ショットID: {s}").format(s=u.key))
            self.shot_model.appendRow(items)
        self.shot_table.selectionModel().blockSignals(False)
        index = next((i for i, u in enumerate(self.shown_units) if u.key == keep), -1)
        if index >= 0:
            self.shot_table.selectRow(index)
            self.shot_table.scrollTo(self.shot_model.index(index, 0))
        self._shot_changed()

    def chosen_unit(self):
        picked = self.shot_table.selectionModel().selectedRows() if self.shot_table.selectionModel() else []
        i = picked[0].row() if picked else -1
        return self.shown_units[i] if 0 <= i < len(self.shown_units) else None

    def _shot_changed(self) -> None:
        unit = self.chosen_unit()
        if unit is None or (self.searched is not None and self.searched[0] != unit.key):
            self.candidates, self.searched = [], None
        self.render_images()
        if unit is not None and unit.key != self.zoomed:
            self.zoomed = unit.key
            self.zoom(unit.rings)
        if unit is not None and self.searched is None:
            self.search()

    # --- The images -----------------------------------------------------------------------------

    def search(self, force: bool = False) -> None:
        """Reads the catalog's images over the chosen shot (again when `force` or the days changed)."""
        unit = self.chosen_unit()
        dock, store = self.dock, self.dock.store
        if unit is None or store is None or self.task is not None:
            return
        n = self.days.value()
        if not force and self.searched == (unit.key, n):
            return
        catalog = dock.catalog()
        if catalog is None:
            self.status.setText(self.tr("画像カタログ（imageCatalogs）なし"))
            return
        if not unit.rings:
            self.status.setText(self.tr("このショットには範囲がないため、画像を探せない"))
            return
        client, token = self.context.esri(), dock.token()
        catalog_token = self.context.token_for(catalog.url, catalog.token)
        requests = self.requests
        self.status.setText(self.tr("{c} から画像を探し中…").format(c=catalog.label))

        def work(task):
            names = self.context.satellite_names(client, task.isCanceled)
            found = find_images(client, catalog, catalog_token, unit, requests, n, n, names, cancelled=task.isCanceled)
            return found, links_of(client, store, token, [c.image_id for c in found], task.isCanceled)

        def done(result):
            found, links = result
            self.candidates = with_links(found, links, unit)
            self.searched = (unit.key, n)
            fitting = sum(1 for c in found if c.matches)
            text = (self.tr("画像 {n} 件（依頼の衛星 {f} 件）").format(n=len(found), f=fitting) if found
                    else self.tr("このショットの範囲に重なる画像なし（前後 {d} 日）").format(d=n))
            if len(found) >= MAX_IMAGES:
                text += self.tr("。多すぎるため {m} 件だけ読んだ（ほかにもある。前後の日数を減らして絞る）").format(m=MAX_IMAGES)
            self.status.setText(text)
            self._finished()
            chosen = self.chosen_unit()
            if chosen is not None and chosen.key == unit.key:
                self.render_images()
            else:
                self._shot_changed()

        def failed(message):
            self.status.setText(self.tr("画像を探せず: {m}").format(m=message))
            self._finished()

        self.task = run_task(self.tr("画像カタログの検索"), work, done, failed)
        self._update_buttons()

    def render_images(self) -> None:
        unit = self.chosen_unit()
        keep = self.chosen_image().image_id if self.chosen_image() else None
        only = self.only_fitting.isChecked()
        self.shown_images = [c for c in self.candidates if c.matches or not only] if unit is not None else []
        self.image_table.selectionModel().blockSignals(True)
        self.image_model.clear()
        self.image_model.setHorizontalHeaderLabels([self.tr(c) for c in IMAGE_COLUMNS])
        for c in self.shown_images:
            mine = unit.captured == "撮像済み" and c.image_id == unit.image_id
            used = self.tr("このショット") if mine else link_text(c.used_by)
            items = [QStandardItem(t) for t in (time_text(c.acquired), c.sensor, c.image_id, gap_text(c.acquired, unit.target_time()), used)]
            if not c.matches:
                for item in items:
                    item.setForeground(QBrush(QColor("#9ca3af")))
                items[1].setToolTip(self.tr("依頼の衛星ではない"))
            if mine:
                items[4].setForeground(QBrush(QColor(COLORS["撮像済み"])))
            self.image_model.appendRow(items)
        self.image_table.selectionModel().blockSignals(False)
        index = next((i for i, c in enumerate(self.shown_images) if c.image_id == keep), -1)
        if index < 0 and unit is not None and unit.image_id:
            index = next((i for i, c in enumerate(self.shown_images) if c.image_id == unit.image_id), -1)
        if index >= 0:
            self.image_table.selectRow(index)
        self._image_changed()

    def chosen_image(self):
        model = self.image_table.selectionModel()
        picked = model.selectedRows() if model else []
        i = picked[0].row() if picked else -1
        return self.shown_images[i] if 0 <= i < len(self.shown_images) else None

    def _image_changed(self) -> None:
        self.paint()
        self._update_buttons()

    # --- The map --------------------------------------------------------------------------------

    def paint(self) -> None:
        unit, chosen = self.chosen_unit(), self.chosen_image()
        if unit is None:
            self.overlay.unpaint()
            return
        polygons = [(c.rings, IMAGE_COLOR, 1, "", True) for c in self.shown_images if c is not chosen and c.rings]
        if unit.rings:
            polygons.append((unit.rings, SHOT_COLOR, 2, "・".join(unit.names[:3]), True))
        if chosen is not None and chosen.rings:
            polygons.append((chosen.rings, CHOSEN_COLOR, 3, chosen.image_id))
        self.overlay.paint(polygons)
        self.dock.canvas.refresh()

    def zoom(self, rings) -> None:
        box: Optional[QgsRectangle] = None
        for ring in rings:
            for x, y in ring:
                if box is None:
                    box = QgsRectangle(x, y, x, y)
                else:
                    box.combineExtentWith(x, y)
        if box is None:
            return
        box = box.buffered(max(box.width(), box.height()) * 0.5 + 0.01)
        canvas = self.dock.canvas
        canvas.setExtent(from_wgs84(canvas.mapSettings().destinationCrs()).transformBoundingBox(box))
        canvas.refresh()

    # --- Linking --------------------------------------------------------------------------------

    def _update_buttons(self) -> None:
        busy = self.task is not None
        unit = self.chosen_unit()
        self.load_button.setEnabled(not busy)
        self.search_button.setEnabled(not busy and unit is not None)
        self.link_button.setEnabled(not busy and unit is not None and self.chosen_image() is not None)
        self.unlink_button.setEnabled(not busy and unit is not None and bool(unlink_findings(unit, "未判定")))
        self.missed_button.setEnabled(not busy and unit is not None and bool(unlink_findings(unit, "未撮像")))

    def link(self) -> None:
        """Links the chosen image to the chosen shot (asking first when it is linked elsewhere, or of another satellite)."""
        unit, image = self.chosen_unit(), self.chosen_image()
        if unit is None or image is None or self.task is not None:
            return
        findings = link_findings(unit, image.image_id)
        if not findings:
            self.status.setText(self.tr("この画像はこのショットに関連付け済み"))
            return
        notes = []
        if unit.captured == "撮像済み" and unit.image_id and unit.image_id != image.image_id:
            notes.append(self.tr("このショットは画像 {i} と関連付け済み（この画像に替わる）").format(i=unit.image_id))
        if image.used_by:
            notes.append(self.tr("この画像は {w} とも関連付け済み").format(w=link_text(image.used_by)))
        if not image.matches:
            notes.append(self.tr("この画像の衛星（{s}）は依頼の衛星ではない").format(s=image.sensor or "?"))
        if notes and not ask_no(self, self.tr("画像とショットの関連付け"),
                                "\n".join(notes) + "\n\n" + self.tr("画像 {i} をこのショットに関連付けますか？").format(i=image.image_id)):
            return
        self._save(findings, self.tr("画像 {i} をショット（{n}）に関連付け").format(i=image.image_id, n="・".join(unit.names)))

    def unlink(self, captured: str) -> None:
        """Puts the chosen shot's rows back to 未判定 (its image taken off) or 未撮像."""
        unit = self.chosen_unit()
        if unit is None or self.task is not None:
            return
        findings = unlink_findings(unit, captured)
        if findings:
            text = (self.tr("ショット（{n}）の関連付けを外し、未判定に") if captured == "未判定"
                    else self.tr("ショット（{n}）を未撮像に")).format(n="・".join(unit.names))
            self._save(findings, text)

    def _save(self, findings: list, text: str) -> None:
        dock, store = self.dock, self.dock.store
        client, token, now = self.context.esri(), dock.token(), _now_ms()
        requests, unit = self.requests, self.chosen_unit()
        ids = [c.image_id for c in self.candidates]
        self.status.setText(self.tr("保存中…"))

        def work(task):
            # Every row of those requests (not only the day's): whether another is still 撮像済み.
            rows = load_history(client, store, token, history_where(store, request_ids=[f.row.request_id for f in findings]),
                                cancelled=task.isCanceled)
            problems = []
            changed = save_marked(client, store, token, findings, requests, rows, now, problems)
            fresh = requests_of(client, store, token, list(requests), task.isCanceled) if changed else None
            return changed, problems, links_of(client, store, token, ids, task.isCanceled), fresh

        def done(result):
            changed, problems, links, fresh = result
            if fresh is not None:
                self.requests.update(fresh)
            if unit is not None:
                self.candidates = with_links(self.candidates, links, unit)
            self.status.setText(text if not problems else self.tr("一部を保存できず: {m}").format(m="、".join(problems[:3])))
            self._finished()
            self.render_shots()
            if changed:
                dock.requests_changed()
            self.saved()

        def failed(message):
            self.status.setText(self.tr("保存に失敗: {m}").format(m=message))
            self._finished()

        self.task = run_task(self.tr("画像とショットの関連付け"), work, done, failed)
        self._update_buttons()
