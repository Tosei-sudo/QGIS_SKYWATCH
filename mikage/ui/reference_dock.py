"""
「参考情報」: what was learnt from outside the images (a press release, a
notice to mariners, a news item, another office's report), with where it came
from and how sure it is, at a place and a time (core/reference_info.py; the
infos and their links are in Esri, the settings file's `referenceInfo`).

The dock lists the infos of a period (30 days by default; words of their
title, summary and source; 「地図の範囲だけ」) and draws them as points on
the map while it is shown. 「新規…」「編集…」 ask the title, the time of what
happened and of the announcement, the source and its number or URL, how
sure it is, the summary, the class, the place (a click on the map, a named
area, the map's centre) and a note. 「削除」 is for the creator and the
administrators. 「地図へ」 goes to the info's place, 「URL を開く」 opens its
document. 「JSON 取り込み…」 adds and updates infos from a JSON file
(core/reference_import.py, ui/reference_import_dialog.py).

The other docks link their items here: 観測記録's records and things
(「参考情報 ▾」), イベント's events (its tab 「参考情報」), and an image's
infos (「参考情報 n 件」 under 観測記録's image) are listed here on their own
(「すべてに戻す」 lists the period again).
"""
from __future__ import annotations

import time
from typing import Callable, Optional, Sequence

from qgis.core import QgsPointXY, QgsRectangle, QgsWkbTypes
from qgis.gui import QgsMapToolEmitPoint, QgsRubberBand
from qgis.PyQt import sip
from qgis.PyQt.QtCore import QDateTime, QStringListModel, Qt, QUrl
from qgis.PyQt.QtGui import QColor, QDesktopServices
from qgis.PyQt.QtWidgets import (
    QAbstractItemView, QApplication, QCheckBox, QComboBox, QCompleter, QDateTimeEdit, QDialog, QDialogButtonBox, QFormLayout,
    QHBoxLayout, QHeaderView, QLabel, QLineEdit, QListWidget, QListWidgetItem, QPlainTextEdit, QPushButton, QTableWidget,
    QTableWidgetItem, QVBoxLayout, QWidget,
)

from ..core import audit, edit_stamp
from ..core import reference_import as ri
from ..core.reference_info import (
    DELETE_ANY, ITEM_LABELS, LinkTarget, ReferenceInfo, ReferenceLink, add_links, can_delete, delete_info, detail_lines,
    is_url, load_infos, load_links, local_text, matches, new_id, remove_links, save_info, source_text,
)
from ..qgis_io import map_tools
from ..qgis_io.geometry import from_wgs84, to_wgs84
from ..qgis_io.tasks import run_task
from .docks import PluginDock, ask_no, plain_label, plain_tip
from .fit import fit_text
from .flow_layout import FlowLayout
from .list_views import column_menus
from .ports import Ports

TITLE = "参考情報"
#: The periods offered (days; None: all), 30 days first chosen.
PERIODS = ((7, "7 日"), (30, "30 日"), (90, "90 日"), (365, "1 年"), (None, "すべて"))
DEFAULT_DAYS = 30
COLUMNS = ("出来事の日時", "題名", "出典", "確からしさ", "地名")
#: The points' colours: the infos listed, and the one chosen.
POINT_COLOR, CHOSEN_COLOR = "#1565c0", "#f97316"


def _time_edit(ms: Optional[int]) -> QDateTimeEdit:
    e = QDateTimeEdit(QDateTime.fromMSecsSinceEpoch(int(ms if ms is not None else time.time() * 1000)))
    e.setCalendarPopup(True)
    e.setDisplayFormat("yyyy-MM-dd HH:mm")
    return e


class ReferenceForm(QDialog):
    """「新規」「編集」: one info. Not modal: the map stays to be clicked for its place."""

    def __init__(self, dock: "ReferenceDock", info: Optional[ReferenceInfo], parent=None):
        super().__init__(parent)
        fit_text(self)
        self.dock = dock
        self.info = info
        self.place: Optional[tuple[float, float]] = (info.lon, info.lat) if info and info.has_place else None
        self.area_id = info.area_id if info else ""
        self.setWindowTitle(self.tr("参考情報の編集") if info else self.tr("新しい参考情報"))
        form = QFormLayout(self)
        self.title_edit = QLineEdit(info.title if info else "")
        self.title_edit.setPlaceholderText(self.tr("例: 〇〇港で大型クレーンの設置を発表"))
        form.addRow(self.tr("題名"), self.title_edit)
        self.time_edit = _time_edit(info.time if info else None)
        self.time_edit.setToolTip(self.tr("出来事が起きた・見られた日時（発表の日時ではない）"))
        form.addRow(self.tr("出来事の日時"), self.time_edit)
        published = QHBoxLayout()
        self.has_published = QCheckBox(self.tr("あり"))
        self.has_published.setChecked(bool(info and info.published is not None))
        self.published_edit = _time_edit(info.published if info and info.published is not None else None)
        self.published_edit.setEnabled(self.has_published.isChecked())
        self.has_published.toggled.connect(self.published_edit.setEnabled)
        published.addWidget(self.has_published)
        published.addWidget(self.published_edit, 1)
        form.addRow(self.tr("発表日時"), published)
        self.source_edit = QLineEdit(info.source if info else "")
        self.source_edit.setPlaceholderText(self.tr("例: 海上保安庁、〇〇新聞、△△部"))
        sources = QStringListModel(dock.sources(), self)
        completer = QCompleter(sources, self.source_edit)
        completer.setCaseSensitivity(Qt.CaseSensitivity.CaseInsensitive)
        completer.setFilterMode(Qt.MatchFlag.MatchContains)
        self.source_edit.setCompleter(completer)
        form.addRow(self.tr("出典"), self.source_edit)
        ref = QHBoxLayout()
        self.ref_edit = QLineEdit(info.source_ref if info else "")
        self.ref_edit.setPlaceholderText(self.tr("文書番号か https://…"))
        self.open_button = QPushButton(self.tr("開く"))
        self.open_button.setToolTip(self.tr("URL をブラウザーで開く"))
        self.open_button.clicked.connect(lambda: QDesktopServices.openUrl(QUrl(self.ref_edit.text().strip())))
        self.ref_edit.textChanged.connect(lambda t: self.open_button.setEnabled(is_url(t)))
        self.open_button.setEnabled(is_url(self.ref_edit.text()))
        ref.addWidget(self.ref_edit, 1)
        ref.addWidget(self.open_button)
        form.addRow(self.tr("文書番号・URL"), ref)
        self.reliability = QComboBox()
        self.reliability.addItem("")
        for level in dock.reliabilities():
            self.reliability.addItem(level)
        if info and info.reliability and self.reliability.findText(info.reliability) < 0:
            self.reliability.addItem(info.reliability)
        self.reliability.setCurrentText(info.reliability if info else "")
        form.addRow(self.tr("確からしさ"), self.reliability)
        self.summary_edit = QPlainTextEdit(info.summary if info else "")
        self.summary_edit.setPlaceholderText(self.tr("何が書かれていたか（数行で）"))
        self.summary_edit.setMinimumHeight(70)
        form.addRow(self.tr("要旨"), self.summary_edit)
        self.kind = QComboBox()
        self.kind.setEditable(True)
        self.kind.addItem("")
        for k in dock.kinds():
            self.kind.addItem(k)
        self.kind.setCurrentText(info.kind if info else "")
        self.kind.lineEdit().setPlaceholderText(self.tr("分類体系の種別（任意）"))
        form.addRow(self.tr("分類"), self.kind)
        # The point and its named area on a line, the ways to set them under it.
        place = QVBoxLayout()
        place.setContentsMargins(0, 0, 0, 0)
        self.place_text = plain_label(QLabel())
        self.place_text.setWordWrap(True)
        self.place_text.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        place.addWidget(self.place_text)
        place_buttons = QHBoxLayout()
        place.addLayout(place_buttons)
        self.click_button = QPushButton(self.tr("地図をクリック"))
        self.click_button.setCheckable(True)
        self.click_button.setToolTip(self.tr("地図でクリックした地点にする（Esc かもう一度押すとやめる）"))
        self.click_button.toggled.connect(self._click_mode)
        self.area_button = QPushButton(self.tr("地名から…"))
        self.area_button.setToolTip(self.tr("地名（組織の関心地域・地名）を選び、その中心を地点にする"))
        self.area_button.clicked.connect(lambda: self.choose_area())
        self.area_button.setEnabled(dock.area_book() is not None and dock.area_book().settings is not None)
        centre = QPushButton(self.tr("地図の中心"))
        centre.setToolTip(self.tr("いまの地図の中心を地点にする"))
        centre.clicked.connect(self.use_centre)
        clear = QPushButton(self.tr("消去"))
        clear.setToolTip(self.tr("地点と地名をなしにする"))
        clear.clicked.connect(lambda: self.set_place(None, ""))
        for b in (self.click_button, self.area_button, centre, clear):
            place_buttons.addWidget(b)
        place_buttons.addStretch(1)
        form.addRow(self.tr("場所"), place)
        self.note_edit = QPlainTextEdit(info.note if info else "")
        self.note_edit.setMinimumHeight(50)
        form.addRow(self.tr("メモ"), self.note_edit)
        # The links (edit only): what this info is linked to, each taken away by 「外す」.
        self.links = QListWidget()
        self.links.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self.links.setMaximumHeight(90)
        self.unlink_button = QPushButton(self.tr("紐付けを外す"))
        self.unlink_button.setToolTip(self.tr("選んだ紐付けを外す（イベント・モノ・記録そのものは残る）"))
        self.unlink_button.clicked.connect(lambda: self.unlink())
        holder = QVBoxLayout()
        holder.setContentsMargins(0, 0, 0, 0)
        holder.addWidget(self.links)
        holder.addWidget(self.unlink_button, 0, Qt.AlignmentFlag.AlignLeft)
        self.links_row = QWidget()
        self.links_row.setLayout(holder)
        form.addRow(self.tr("紐付け"), self.links_row)
        self.links_row.setVisible(info is not None and dock.settings is not None and dock.settings.links is not None)
        self.problem = plain_label(QLabel())
        self.problem.setStyleSheet("color: #b91c1c")
        self.problem.setWordWrap(True)
        form.addRow(self.problem)
        self.button_box = QDialogButtonBox(QDialogButtonBox.StandardButton.Save | QDialogButtonBox.StandardButton.Cancel)
        #: Saves the values (in a task): `saver(info, done, failed)`; the form closes only once saved.
        self.saver: Optional[Callable] = None
        self.button_box.accepted.connect(self._accept)
        self.button_box.rejected.connect(self.reject)
        form.addRow(self.button_box)
        self.tool = QgsMapToolEmitPoint(dock.canvas)
        self.tool.canvasClicked.connect(self._clicked)
        dock.canvas.mapToolSet.connect(self._tool_changed)
        self.band = QgsRubberBand(dock.canvas, QgsWkbTypes.GeometryType.PointGeometry)
        self.band.setColor(QColor(CHOSEN_COLOR))
        self.band.setIcon(QgsRubberBand.IconType.ICON_CROSS)
        self.band.setIconSize(16)
        self.band.setWidth(3)
        self.band.setZValue(960)
        self.finished.connect(self._closed)
        self.set_place(self.place, self.area_id)
        self.show_links()
        self.resize(600, 0)

    # ---- the place -----------------------------------------------------------------------------

    def set_place(self, place: Optional[tuple[float, float]], area_id: Optional[str] = None) -> None:
        """The point (WGS 84) and, when given, the named area ('' none)."""
        self.place = place
        if area_id is not None:
            self.area_id = area_id
        name = self.dock.area_name(self.area_id)
        parts = []
        if self.place is not None:
            parts.append(f"{self.place[1]:.5f}, {self.place[0]:.5f}")
        if self.area_id:
            parts.append(self.tr("地名: {n}").format(n=name or self.area_id))
        self.place_text.setText("　".join(parts) or self.tr("なし"))
        if sip.isdeleted(self.band):
            return
        self.band.reset(QgsWkbTypes.GeometryType.PointGeometry)
        if self.place is not None:
            crs = self.dock.canvas.mapSettings().destinationCrs()
            self.band.addPoint(from_wgs84(crs).transform(QgsPointXY(*self.place)))

    def use_centre(self) -> None:
        c = self.dock.canvas.extent().center()
        p = to_wgs84(self.dock.canvas.mapSettings().destinationCrs()).transform(c)
        self.set_place((p.x(), p.y()))

    def _click_mode(self, on: bool) -> None:
        canvas = self.dock.canvas
        if on:
            map_tools.take(canvas, self.tool)
            self.problem.setText(self.tr("地図をクリック（Esc かボタンでやめる）"))
        else:
            map_tools.give_back(canvas, self.tool)
            if self.problem.text().startswith(self.tr("地図をクリック")):
                self.problem.setText("")

    def _tool_changed(self, new_tool, _old=None) -> None:
        if new_tool is not self.tool and self.click_button.isChecked():
            self.click_button.blockSignals(True)
            self.click_button.setChecked(False)
            self.click_button.blockSignals(False)

    def _clicked(self, point, _button=None) -> None:
        p = to_wgs84(self.dock.canvas.mapSettings().destinationCrs()).transform(point)
        self.click_button.setChecked(False)
        self.set_place((p.x(), p.y()))
        self.raise_()
        self.activateWindow()

    def choose_area(self, area=None) -> None:
        """「地名から…」: a named area, its centre the place (its shape read first when not read)."""
        book = self.dock.area_book()
        if book is None:
            return
        if area is None:
            from .area_pick_dialog import AreaPickDialog

            dialog = AreaPickDialog(book, self, self.tr("参考情報の地名"))
            if dialog.exec() != QDialog.DialogCode.Accepted or dialog.chosen() is None:
                return
            area = dialog.chosen()

        def shaped(areas: list) -> None:
            if sip.isdeleted(self):
                return
            a = areas[0] if areas else area
            from ..qgis_io.org_areas_io import geometry_of

            g = geometry_of(a)
            centre = None
            if g is not None and not g.isEmpty():
                inside = g.pointOnSurface().asPoint()
                centre = (inside.x(), inside.y())
            self.set_place(centre if centre is not None else self.place, a.id or "")
            if not a.id:
                self.problem.setText(self.tr("この地名には管理番号がない（地点だけ設定）"))

        book.shapes([area], shaped, lambda m: self.problem.setText(self.tr("地名の形を取得できない: {m}").format(m=m)))

    # ---- the links -----------------------------------------------------------------------------

    def show_links(self) -> None:
        self.links.clear()
        if self.info is None:
            return
        for x in self.dock.links_of(self.info.id):
            item = QListWidgetItem(f"{ITEM_LABELS.get(x.type, x.type)}: {x.label or x.item_id}")
            item.setToolTip(plain_tip(f"{x.item_id}\n{x.linked_by} {local_text(x.linked_at)}".strip()))
            item.setData(Qt.ItemDataRole.UserRole, x.oid)
            self.links.addItem(item)
        self.unlink_button.setEnabled(self.links.count() > 0)

    def unlink(self) -> None:
        if self.info is None:
            return
        chosen = {i.data(Qt.ItemDataRole.UserRole) for i in self.links.selectedItems()}
        links = [x for x in self.dock.links_of(self.info.id) if x.oid in chosen]
        if links:
            self.dock.unlink(links, lambda: None if sip.isdeleted(self) else self.show_links())

    # ---- saving --------------------------------------------------------------------------------

    def value(self) -> ReferenceInfo:
        base = self.info
        return ReferenceInfo(
            id=base.id if base else new_id(), title=self.title_edit.text().strip(),
            time=int(self.time_edit.dateTime().toMSecsSinceEpoch()),
            published=int(self.published_edit.dateTime().toMSecsSinceEpoch()) if self.has_published.isChecked() else None,
            source=self.source_edit.text().strip(), source_ref=self.ref_edit.text().strip(),
            reliability=self.reliability.currentText().strip(), summary=self.summary_edit.toPlainText().strip(),
            kind=self.kind.currentText().strip(), area_id=self.area_id, note=self.note_edit.toPlainText().strip(),
            lon=self.place[0] if self.place else None, lat=self.place[1] if self.place else None,
            oid=base.oid if base else None, created_by=base.created_by if base else "", created_at=base.created_at if base else None,
        )

    def _accept(self) -> None:
        v = self.value()
        if not v.title:
            self.problem.setText(self.tr("題名を入力"))
            return
        if v.published is not None and v.time is not None and v.published < v.time - 366 * 86_400_000:
            self.problem.setText(self.tr("発表日時が出来事の日時より 1 年以上前（日時を確認）"))
            return
        if self.saver is None:
            self.accept()
            return
        self.problem.setText(self.tr("保存中…"))
        self.button_box.setEnabled(False)

        def failed(message: str) -> None:
            if sip.isdeleted(self):
                return
            self.button_box.setEnabled(True)
            self.problem.setText(self.tr("保存に失敗: {m}").format(m=message))

        self.saver(v, lambda: None if sip.isdeleted(self) else self.accept(), failed)

    def _closed(self, _result=None) -> None:
        canvas = self.dock.canvas
        try:
            canvas.mapToolSet.disconnect(self._tool_changed)
        except (TypeError, RuntimeError):
            pass
        map_tools.give_back(canvas, self.tool)
        map_tools.forget(canvas, self.tool)
        band, self.band = self.band, None
        if band is not None:
            try:
                scene = band.scene()
                if scene is not None:
                    scene.removeItem(band)
            except RuntimeError:
                pass

    def keyPressEvent(self, event) -> None:  # noqa: N802 (Qt's name)
        if event.key() == Qt.Key.Key_Escape and self.click_button.isChecked():
            self.click_button.setChecked(False)
            return
        super().keyPressEvent(event)


class ReferencePickDialog(QDialog):
    """「参考情報に紐付け…」: infos chosen (several) from the dock's, or 「新しい参考情報…」."""

    def __init__(self, dock: "ReferenceDock", what: str, parent=None):
        super().__init__(parent)
        fit_text(self)
        self.dock = dock
        self.setWindowTitle(self.tr("参考情報に紐付け"))
        layout = QVBoxLayout(self)
        head = plain_label(QLabel(self.tr("紐付ける項目: {w}").format(w=what)))
        head.setWordWrap(True)
        layout.addWidget(head)
        self.search = QLineEdit()
        self.search.setPlaceholderText(self.tr("題名・要旨・出典で絞り込み"))
        self.search.setClearButtonEnabled(True)
        self.search.textChanged.connect(lambda _t: self.render())
        layout.addWidget(self.search)
        self.table = QTableWidget(0, 3)
        self.table.setHorizontalHeaderLabels([self.tr("出来事の日時"), self.tr("題名"), self.tr("出典")])
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.verticalHeader().hide()
        self.table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.ResizeToContents)
        self.table.horizontalHeader().setStretchLastSection(True)
        self.table.itemSelectionChanged.connect(self._update)
        self.table.cellDoubleClicked.connect(lambda _r, _c: self.accept())
        layout.addWidget(self.table, 1)
        self.status = plain_label(QLabel())
        layout.addWidget(self.status)
        self.buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        self.buttons.button(QDialogButtonBox.StandardButton.Ok).setText(self.tr("紐付け"))
        self.create_button = self.buttons.addButton(self.tr("新しい参考情報…"), QDialogButtonBox.ButtonRole.ActionRole)
        #: Pressed: a new info made, then linked (the dock does it).
        self.wants_new = False
        self.create_button.clicked.connect(self._new)
        self.buttons.accepted.connect(self.accept)
        self.buttons.rejected.connect(self.reject)
        layout.addWidget(self.buttons)
        self.shown: list[ReferenceInfo] = []
        self.resize(560, 380)
        self.render()

    def render(self) -> None:
        text = self.search.text()
        self.shown = [x for x in self.dock.infos or [] if matches(x, text)]
        self.table.setRowCount(len(self.shown))
        for i, x in enumerate(self.shown):
            for j, v in enumerate((local_text(x.time), x.title, source_text(x))):
                item = QTableWidgetItem(v)
                if j == 1 and x.summary:
                    item.setToolTip(plain_tip(x.summary))
                self.table.setItem(i, j, item)
        if self.dock.infos is None:
            self.status.setText(self.tr("参考情報を取得中…"))
        else:
            self.status.setText(self.tr("{n} 件（ドックの期間「{p}」から）").format(n=len(self.shown), p=self.dock.period_text()))
        self._update()

    def chosen(self) -> list[ReferenceInfo]:
        rows = sorted({i.row() for i in self.table.selectionModel().selectedRows()})
        return [self.shown[r] for r in rows if 0 <= r < len(self.shown)]

    def _update(self) -> None:
        self.buttons.button(QDialogButtonBox.StandardButton.Ok).setEnabled(bool(self.chosen()))

    def _new(self) -> None:
        self.wants_new = True
        self.accept()


class ReferenceDock(PluginDock):
    """「参考情報」."""

    def __init__(self, iface, context, open_settings: Callable[[], None], ports: Ports, parent=None):
        super().__init__("SatObsReferences", "参考情報", context, open_settings, parent)
        self.iface = iface
        self.canvas = iface.mapCanvas()
        #: 組織の関心地域 (the named areas: their names, 「地名から…」) and 観測記録 (the classes, the 確度 levels).
        self.org_areas = ports.org_areas
        self.observation_port = ports.observations
        #: The infos of the period, as read last (None: not read yet); their links by info id, read as chosen.
        self.infos: Optional[list[ReferenceInfo]] = None
        self.link_cache: dict[str, list[ReferenceLink]] = {}
        #: Infos listed on their own (an image's, a record's, an event's): (what they are, the infos); None: the period.
        self.focus: Optional[tuple[str, list[ReferenceInfo]]] = None
        self.shown: list[ReferenceInfo] = []
        self.task = None
        self._tasks: list = []
        self._then: list[Callable[[], None]] = []
        self._again = False
        self._form: Optional[ReferenceForm] = None
        self._import_dialog = None

        self.note = plain_label(QLabel())
        self.note.setWordWrap(True)
        self.body.addWidget(self.note)
        top = QHBoxLayout()
        self.period = QComboBox()
        for days, label in PERIODS:
            self.period.addItem(self.tr(label), days)
        self.period.setCurrentIndex(self.period.findData(DEFAULT_DAYS))
        self.period.setToolTip(self.tr("出来事の日時がこの期間の参考情報を一覧に出す"))
        self.period.currentIndexChanged.connect(lambda _i: self.reload())
        self.search = QLineEdit()
        self.search.setPlaceholderText(self.tr("題名・要旨・出典で絞り込み"))
        self.search.setClearButtonEnabled(True)
        self.search.textChanged.connect(lambda _t: self.render())
        self.in_view = QCheckBox(self.tr("地図の範囲だけ"))
        self.in_view.setToolTip(self.tr("いまの地図の表示範囲にある参考情報だけを出す"))
        self.in_view.toggled.connect(lambda _on: self.render())
        self.reload_button = QPushButton(self.tr("最新を取得"))
        self.reload_button.clicked.connect(lambda: self.reload())
        top.addWidget(self.period)
        top.addWidget(self.search, 1)
        top.addWidget(self.in_view)
        top.addWidget(self.reload_button)
        self.body.addLayout(top)
        focus = QHBoxLayout()
        self.focus_label = plain_label(QLabel())
        self.focus_label.setWordWrap(True)
        self.focus_label.setStyleSheet("color: #1565c0;")
        self.unfocus_button = QPushButton(self.tr("すべてに戻す"))
        self.unfocus_button.setToolTip(self.tr("期間の参考情報の一覧に戻す"))
        self.unfocus_button.clicked.connect(lambda: self.set_focus(None))
        focus.addWidget(self.focus_label, 1)
        focus.addWidget(self.unfocus_button)
        self.focus_row = QWidget()
        self.focus_row.setLayout(focus)
        self.focus_row.hide()
        self.body.addWidget(self.focus_row)

        self.table = QTableWidget(0, len(COLUMNS))
        self.table.setHorizontalHeaderLabels([self.tr(c) for c in COLUMNS])
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.verticalHeader().hide()
        self.table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.ResizeToContents)
        self.table.horizontalHeader().setStretchLastSection(True)
        self.table.itemSelectionChanged.connect(self._chosen_changed)
        self.table.cellDoubleClicked.connect(lambda _r, _c: self.edit())
        self.body.addWidget(self.table, 1)
        self.table_columns = column_menus("references", self.table, self)

        self.buttons: dict[str, QPushButton] = {}
        flow = FlowLayout()
        for key, text, tip, slot in [
            ("new", self.tr("新規…"), self.tr("参考情報を登録（地図の中心が地点の初期値）"), self.create),
            ("edit", self.tr("編集…"), self.tr("題名・日時・出典・確からしさ・要旨・分類・場所・メモを変更"), self.edit),
            ("delete", self.tr("削除"), self.tr("参考情報と紐付けを削除（作成者か管理者だけ）"), self.delete),
            ("zoom", self.tr("地図へ"), self.tr("参考情報の地点へ地図を移動"), self.zoom),
            ("url", self.tr("URL を開く"), self.tr("文書番号・URL の URL をブラウザーで開く"), self.open_url),
            ("copy", self.tr("概要をコピー"), self.tr("題名・日時・出典・要旨などの数行をクリップボードへ"), self.copy_text),
            ("import", self.tr("JSON 取り込み…"), self.tr("JSON ファイルの参考情報をまとめて登録・更新（試行してから取り込む）"),
             self.open_import),
        ]:
            b = QPushButton(text)
            b.setToolTip(tip)
            b.clicked.connect(lambda _on=False, slot=slot: slot())
            flow.addWidget(b)
            self.buttons[key] = b
        holder = QWidget()
        holder.setLayout(flow)
        self.body.addWidget(holder)
        self.detail = plain_label(QLabel())
        self.detail.setWordWrap(True)
        self.detail.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        self.body.addWidget(self.detail)
        self.status = plain_label(QLabel())
        self.status.setWordWrap(True)
        self.body.addWidget(self.status)

        # The infos listed as points while the dock is shown; the one chosen bigger, in another colour.
        self.points = self._band(POINT_COLOR, 9)
        self.chosen_point = self._band(CHOSEN_COLOR, 15)
        self.canvas.destinationCrsChanged.connect(self.draw)
        self.canvas.extentsChanged.connect(self._extent_changed)
        self.visibilityChanged.connect(self._visibility)
        context.configChanged.connect(self._config_changed)
        self._config_changed()

    def _band(self, color: str, size: int) -> QgsRubberBand:
        band = QgsRubberBand(self.canvas, QgsWkbTypes.GeometryType.PointGeometry)
        band.setColor(QColor(color))
        band.setIcon(QgsRubberBand.IconType.ICON_CIRCLE)
        band.setIconSize(size)
        band.setWidth(2)
        band.setZValue(950)
        return band

    # ---- state ---------------------------------------------------------------------------------

    @property
    def settings(self):
        return self.context.config.reference_info

    def _config_changed(self) -> None:
        self.infos, self.link_cache, self.focus = None, {}, None
        s = self.settings
        self.note.setText("" if s else self.tr("参考情報は使えない（管理者に連絡: referenceInfo）［E703］"))
        self.note.setVisible(s is None)
        self.render()
        if s is not None and self.isVisible():
            self.reload()

    def _visibility(self, on: bool) -> None:
        if on and self.settings is not None and self.infos is None:
            self.reload()
        if on:
            book = self.area_book()
            if book is not None:
                book.want()
        self.draw()

    def _extent_changed(self) -> None:
        if self.in_view.isChecked():
            self.render()

    def say(self, text: str) -> None:
        self.status.setText(text)

    def area_book(self):
        return self.org_areas() if self.org_areas else None

    def area_name(self, area_id: str) -> str:
        book = self.area_book()
        a = book.find_id(area_id) if book is not None and area_id else None
        return a.name if a is not None else ""

    def period_days(self) -> Optional[int]:
        return self.period.currentData()

    def period_text(self) -> str:
        return self.period.currentText()

    def chosen(self) -> Optional[ReferenceInfo]:
        rows = self.table.selectionModel().selectedRows()
        r = rows[0].row() if rows else -1
        return self.shown[r] if 0 <= r < len(self.shown) else None

    def choose(self, info_id: str) -> None:
        for i, x in enumerate(self.shown):
            if x.id == info_id:
                self.table.selectRow(i)
                return

    def links_of(self, info_id: str) -> list[ReferenceLink]:
        return list(self.link_cache.get(info_id, []))

    def sources(self) -> list[str]:
        """The sources used (the form's suggestions), the most used first."""
        counts: dict[str, int] = {}
        for x in self.infos or []:
            if x.source:
                counts[x.source] = counts.get(x.source, 0) + 1
        return sorted(counts, key=lambda s: (-counts[s], s))

    def _vocabulary(self):
        dock = self.observation_port() if self.observation_port else None
        book = getattr(dock, "book", None) if dock is not None else None
        return getattr(book, "vocabulary", None) or self.context.config.observation_vocabulary

    def reliabilities(self) -> list[str]:
        """観測記録's 確度 levels (the store chosen there, else the settings file's)."""
        return [c.label for c in getattr(self._vocabulary(), "confidences", None) or []]

    def kinds(self) -> list[str]:
        """The classes of the classification (観測記録's store), and those used in the infos."""
        from ..core.obs.api import kind_of

        dock = self.observation_port() if self.observation_port else None
        book = getattr(dock, "book", None) if dock is not None else None
        try:
            taxa = book.taxonomy() if book is not None else list(self._vocabulary().taxonomy)
        except Exception:  # noqa: BLE001 (a store not read yet: the settings file's classes)
            taxa = list(getattr(self._vocabulary(), "taxonomy", []) or [])
        out = [kind_of(t.path) for t in taxa]
        return list(dict.fromkeys([*out, *(x.kind for x in self.infos or [] if x.kind)]))

    # ---- tasks ---------------------------------------------------------------------------------

    def _start(self, description: str, work, done, failed=None) -> None:
        def fail(message: str) -> None:
            self._update_buttons()
            self.say(self.tr("{what}に失敗: {m}").format(what=description, m=message))

        def finish(result) -> None:
            done(result)
            self._update_buttons()

        self.say(self.tr("{what}中…").format(what=description))
        task = run_task(description, work, finish, failed or fail)
        self._tasks.append(task)
        task.taskCompleted.connect(lambda: self._tasks.remove(task) if task in self._tasks else None)
        task.taskTerminated.connect(lambda: self._tasks.remove(task) if task in self._tasks else None)

    def _tokens(self):
        s = self.settings
        token = self.context.token_for(s.layer.url, s.layer.token)
        links = self.context.token_for(s.links.url, s.links.token) if s.links is not None else None
        return token, links

    def reload(self, then: Optional[Callable[[], None]] = None, keep: Optional[str] = None) -> None:
        """Reads the infos of the period again (in a task), then lists them (the one chosen stays chosen)."""
        s = self.settings
        if s is None:
            return
        if then is not None:
            self._then.append(then)
        if self.task is not None:
            self._again = True
            return
        self._again = False
        chosen = keep or (self.chosen().id if self.chosen() else None)
        client = self.context.esri()
        token, _links = self._tokens()
        days = self.period_days()
        since = time.time() * 1000 - days * 86_400_000 if days else None

        def work(_task):
            return load_infos(client, s, token, since=since)

        def done(infos) -> None:
            self.task = None
            if s is not self.settings or self._again:
                self.reload(keep=chosen)
                return
            self.infos = infos
            self.link_cache = {}
            if self.focus is not None:
                # The infos listed on their own, as saved now.
                by_id = {x.id: x for x in infos}
                self.focus = (self.focus[0], [by_id.get(x.id, x) for x in self.focus[1]])
            self.render()
            if chosen:
                self.choose(chosen)
            self.say(self.tr("{n} 件（{p}）").format(n=len(infos), p=self.period_text()))
            waiting, self._then = self._then, []
            for f in waiting:
                f()

        def failed(message: str) -> None:
            self.task = None
            if s is not self.settings or self._again:
                self.reload()
                return
            self._then = []
            self.say(self.tr("参考情報の取得に失敗: {m}").format(m=message))
            self._update_buttons()

        self.task = True
        self._start(self.tr("参考情報の取得"), work, done, failed)

    def _read_links(self, info: ReferenceInfo) -> None:
        """The chosen info's links (in a task), shown under the list."""
        s = self.settings
        if s is None or s.links is None or info.id in self.link_cache:
            return
        client = self.context.esri()
        _token, token = self._tokens()

        def done(links) -> None:
            self.link_cache[info.id] = links
            if self.chosen() is not None and self.chosen().id == info.id:
                self._show_detail()
            if self._form is not None and not sip.isdeleted(self._form) and self._form.info is not None and self._form.info.id == info.id:
                self._form.show_links()

        run_task(self.tr("参考情報の紐付けの取得"), lambda _t: load_links(client, s, [info.id], token=token), done,
                 lambda m: self.say(self.tr("紐付けの取得に失敗: {m}").format(m=m)), hidden=True)

    # ---- showing -------------------------------------------------------------------------------

    def _view_box(self) -> Optional[tuple]:
        if not self.in_view.isChecked():
            return None
        box = to_wgs84(self.canvas.mapSettings().destinationCrs()).transformBoundingBox(self.canvas.extent())
        return box.xMinimum(), box.yMinimum(), box.xMaximum(), box.yMaximum()

    def render(self) -> None:
        chosen = self.chosen()
        listed = self.focus[1] if self.focus is not None else self.infos or []
        box = self._view_box()
        text = self.search.text()
        self.shown = [x for x in listed if matches(x, text, box)]
        self.focus_row.setVisible(self.focus is not None)
        if self.focus is not None:
            self.focus_label.setText(self.tr("{w}: {n} 件").format(w=self.focus[0], n=len(self.focus[1])))
        self.table.blockSignals(True)
        self.table.setRowCount(len(self.shown))
        for i, x in enumerate(self.shown):
            values = (local_text(x.time), x.title, source_text(x), x.reliability, self.area_name(x.area_id) or x.area_id)
            for j, v in enumerate(values):
                item = QTableWidgetItem(v)
                if j == 1:
                    item.setToolTip(plain_tip("\n".join(detail_lines(x, self.area_name(x.area_id)))))
                self.table.setItem(i, j, item)
        self.table.blockSignals(False)
        if chosen is not None:
            self.choose(chosen.id)
        self._chosen_changed()

    def _chosen_changed(self) -> None:
        x = self.chosen()
        if x is not None:
            self._read_links(x)
        self._show_detail()
        self._update_buttons()
        self.draw()

    def _show_detail(self) -> None:
        x = self.chosen()
        self.detail.setText("" if x is None else "\n".join(detail_lines(x, self.area_name(x.area_id), self.links_of(x.id))))

    def _update_buttons(self) -> None:
        ready = self.settings is not None and self.task is None
        x = self.chosen()
        self.buttons["new"].setEnabled(ready)
        self.buttons["import"].setEnabled(self.settings is not None)
        self.buttons["edit"].setEnabled(ready and x is not None)
        self.buttons["delete"].setEnabled(ready and x is not None)
        self.buttons["zoom"].setEnabled(x is not None and x.has_place)
        self.buttons["url"].setEnabled(x is not None and bool(x.url))
        self.buttons["copy"].setEnabled(x is not None)
        self.reload_button.setEnabled(self.settings is not None and self.task is None)

    def draw(self) -> None:
        """The infos listed as points (while the dock is shown)."""
        for band in (self.points, self.chosen_point):
            if not sip.isdeleted(band):
                band.reset(QgsWkbTypes.GeometryType.PointGeometry)
        if not self.isVisible() or sip.isdeleted(self.points):
            return
        to_map = from_wgs84(self.canvas.mapSettings().destinationCrs())
        chosen = self.chosen()
        for x in self.shown:
            if x.has_place:
                self.points.addPoint(to_map.transform(QgsPointXY(x.lon, x.lat)), False)
        self.points.updatePosition()
        self.points.update()
        if chosen is not None and chosen.has_place:
            self.chosen_point.addPoint(to_map.transform(QgsPointXY(chosen.lon, chosen.lat)))

    def set_focus(self, focus: Optional[tuple[str, list[ReferenceInfo]]]) -> None:
        """Infos listed on their own ((what, infos)), or the period again (None)."""
        self.focus = focus
        if focus is not None and self.search.text():
            self.search.clear()
        self.render()
        if self.shown and focus is not None:
            self.table.selectRow(0)

    # ---- the port: what the other docks ask ----------------------------------------------------

    def show_infos(self, infos: Sequence[ReferenceInfo], what: str) -> None:
        """An image's infos (観測記録's 「参考情報 n 件」) listed here, the dock in front."""
        self.show()
        self.raise_()
        if self.infos is None and self.settings is not None:
            self.reload()
        self.set_focus((what, list(infos)))
        self.say(self.tr("{w}: {n} 件").format(w=what, n=len(infos)))

    def show_for_items(self, items: Sequence[tuple[str, str]], what: str) -> None:
        """The infos linked to these items ((type, id): a record, a thing, an event), listed here."""
        s = self.settings
        if s is None or s.links is None:
            self.say(self.tr("参考情報の紐付けは使えない（管理者に連絡: referenceInfo.links）"))
            return
        client = self.context.esri()
        token, links_token = self._tokens()

        def work(_task):
            links = load_links(client, s, items=list(items), token=links_token)
            infos = load_infos(client, s, token, ids=[x.info_id for x in links]) if links else []
            return links, infos

        def done(result) -> None:
            links, infos = result
            for x in infos:
                self.link_cache.pop(x.id, None)
            self.show_infos(infos, what)

        self._start(self.tr("紐付けた参考情報の取得"), work, done)

    def infos_for_items(self, items: Sequence[tuple[str, str]], then: Callable[[list], None],
                        failed: Optional[Callable[[str], None]] = None) -> bool:
        """
        The links of these items with their infos ([(link, info)], in a task;
        イベント's tab 「参考情報」); False without the link table.
        """
        s = self.settings
        if s is None or s.links is None:
            return False
        client = self.context.esri()
        token, links_token = self._tokens()

        def work(_task):
            links = load_links(client, s, items=list(items), token=links_token)
            infos = {x.id: x for x in load_infos(client, s, token, ids=[x.info_id for x in links])} if links else {}
            return [(x, infos[x.info_id]) for x in links if x.info_id in infos]

        run_task(self.tr("紐付けた参考情報の取得"), work, then, failed or (lambda m: None), hidden=True)
        return True

    def can_link(self) -> bool:
        return self.settings is not None and self.settings.links is not None

    def offer_link(self, targets: Sequence[LinkTarget], parent=None, then: Optional[Callable[[], None]] = None):
        """「参考情報に紐付け…」: infos chosen in a window (or a new one), linked to the items; the window, or None."""
        if not self.can_link() or not targets:
            return None
        if self.infos is None:
            self.reload(lambda: self.offer_link(targets, parent, then))
            return None
        what = "、".join(f"{ITEM_LABELS.get(t.type, t.type)}「{t.label or t.item_id}」" for t in targets[:3]) + (
            self.tr(" ほか {n} 件").format(n=len(targets) - 3) if len(targets) > 3 else "")
        dialog = ReferencePickDialog(self, what, parent or self)

        def closed(result: int) -> None:
            dialog.deleteLater()
            if result != QDialog.DialogCode.Accepted:
                return
            if dialog.wants_new:
                self.create(link=list(targets), then=then)
                return
            chosen = dialog.chosen()
            if chosen:
                self.link([x.id for x in chosen], list(targets), then)

        dialog.finished.connect(closed)
        dialog.open()
        self._pick = dialog
        return dialog

    def link(self, info_ids: Sequence[str], targets: Sequence[LinkTarget], then: Optional[Callable[[], None]] = None) -> None:
        """Links the infos to the items (in a task); says how many went in and how many were there already."""
        s = self.settings
        if s is None or s.links is None or not info_ids or not targets:
            return
        client = self.context.esri()
        _token, token = self._tokens()

        def done(result) -> None:
            added, skipped = result
            for i in info_ids:
                self.link_cache.pop(i, None)
            text = self.tr("{n} 件を紐付け").format(n=added) + (self.tr("（{k} 件は紐付け済み）").format(k=skipped) if skipped else "")
            self.say(text)
            self.iface.messageBar().pushSuccess(TITLE, plain_tip(text))
            self._chosen_changed()
            if then:
                then()

        self._start(self.tr("参考情報の紐付け"), lambda _t: add_links(client, s, list(info_ids), list(targets), token), done)

    def unlink(self, links: Sequence[ReferenceLink], then: Optional[Callable[[], None]] = None) -> None:
        """Takes links away (in a task)."""
        s = self.settings
        if s is None or not links:
            return
        client = self.context.esri()
        _token, token = self._tokens()

        def done(n: int) -> None:
            for x in links:
                if x.info_id in self.link_cache:
                    self.link_cache[x.info_id] = [y for y in self.link_cache[x.info_id] if y.oid != x.oid]
            self.say(self.tr("紐付け {n} 件を解除").format(n=n))
            self._show_detail()
            if then:
                then()

        self._start(self.tr("紐付けの解除"), lambda _t: remove_links(client, s, list(links), token), done)

    # ---- infos ---------------------------------------------------------------------------------

    def _open_form(self, info: Optional[ReferenceInfo], link: Sequence[LinkTarget] = (),
                   then: Optional[Callable[[], None]] = None) -> Optional[ReferenceForm]:
        if self.settings is None:
            return None
        if self._form is not None and not sip.isdeleted(self._form):
            self._form.close()
        form = ReferenceForm(self, info, self)
        if info is None:
            form.use_centre()

        def saver(value: ReferenceInfo, done, failed) -> None:
            def saved(x: ReferenceInfo) -> None:
                done()
                if link:
                    self.link([x.id], list(link), then)

            self.save(value, saved, failed)

        form.saver = saver
        form.finished.connect(lambda _r: (form.deleteLater(), setattr(self, "_form", None) if self._form is form else None))
        self._form = form
        form.show()
        return form

    def create(self, link: Sequence[LinkTarget] = (), then: Optional[Callable[[], None]] = None) -> Optional[ReferenceForm]:
        """「新規…」 (with items to link to once saved: 「新しい参考情報…」 of the other docks)."""
        self.show()
        self.raise_()
        return self._open_form(None, link, then)

    def edit(self) -> Optional[ReferenceForm]:
        x = self.chosen()
        if x is None:
            return None
        return self._open_form(x)

    def save(self, info: ReferenceInfo, then: Optional[Callable[[ReferenceInfo], None]] = None,
             failed: Optional[Callable[[str], None]] = None) -> None:
        """Saves the info (in a task), then reads the list again with it chosen."""
        s = self.settings
        client = self.context.esri()
        token, _links = self._tokens()
        new = info.oid is None

        def done(saved: ReferenceInfo) -> None:
            if self.focus is not None and new:
                self.focus = None
            days = self.period_days()
            if days and saved.time is not None and saved.time < time.time() * 1000 - days * 86_400_000:
                # Older than the period: the period widened so that it is listed.
                self.period.blockSignals(True)
                self.period.setCurrentIndex(self.period.findData(None))
                self.period.blockSignals(False)
            self.reload(lambda: (self.say(self.tr("「{t}」を保存").format(t=saved.title)), then(saved) if then else None), keep=saved.id)

        def fail(message: str) -> None:
            self._update_buttons()
            self.say(self.tr("参考情報の保存に失敗: {m}").format(m=message))
            if failed is not None:
                failed(message)

        self._start(self.tr("参考情報の保存"), lambda _t: save_info(client, s, info, token), done, fail)

    def may_delete(self, info: ReferenceInfo) -> bool:
        return can_delete(info, edit_stamp.user_name(), self.allows(DELETE_ANY))

    def delete(self, confirm: bool = True) -> None:
        x = self.chosen()
        if x is None:
            return
        if not self.may_delete(x):
            self.say(self.tr("削除できるのは作成者（{w}）か管理者だけ").format(w=x.created_by))
            return
        n = len(self.links_of(x.id))
        if confirm and not ask_no(self, TITLE, self.tr("「{t}」を削除（復元できない）\n紐付け {n} 件も消え、イベント・モノ・記録そのものは残る")
                                  .format(t=x.title, n=n), yes=self.tr("削除")):
            return
        s = self.settings
        client = self.context.esri()
        token, links_token = self._tokens()

        def done(removed: int) -> None:
            if self.focus is not None:
                self.focus = (self.focus[0], [y for y in self.focus[1] if y.id != x.id])
            self.reload(lambda: self.say(self.tr("「{t}」を削除（紐付け {n} 件）").format(t=x.title, n=removed)))

        self._start(self.tr("参考情報の削除"), lambda _t: delete_info(client, s, x, token, links_token), done)

    def zoom(self) -> None:
        """「地図へ」: the map centred on the info's place (zoomed in when it shows more than a degree)."""
        x = self.chosen()
        if x is None or not x.has_place:
            return
        crs = self.canvas.mapSettings().destinationCrs()
        current = to_wgs84(crs).transformBoundingBox(self.canvas.extent())
        if current.width() > 1 or current.height() > 1:
            self.canvas.setExtent(from_wgs84(crs).transformBoundingBox(QgsRectangle(x.lon - 0.05, x.lat - 0.05, x.lon + 0.05, x.lat + 0.05)))
        else:
            self.canvas.setCenter(from_wgs84(crs).transform(QgsPointXY(x.lon, x.lat)))
        self.canvas.refresh()

    def open_url(self) -> bool:
        x = self.chosen()
        if x is None or not x.url:
            return False
        return QDesktopServices.openUrl(QUrl(x.url))

    def copy_text(self) -> str:
        x = self.chosen()
        if x is None:
            return ""
        text = "\n".join(detail_lines(x, self.area_name(x.area_id), self.links_of(x.id)))
        QApplication.clipboard().setText(text)
        audit.record("copy", "reference-info", detail={"id": x.id}, text=text)
        self.say(self.tr("概要をコピー"))
        return text

    # ---- 「JSON 取り込み…」 -------------------------------------------------------------------

    def open_import(self, path: Optional[str] = None):
        """「参考情報の取り込み（JSON）」 (one window; the file tried at once when given)."""
        from .reference_import_dialog import ReferenceImportDialog

        if self.settings is None:
            return None
        if self._import_dialog is None or sip.isdeleted(self._import_dialog):
            self._import_dialog = ReferenceImportDialog(self, self.window())
        self._import_dialog.show()
        self._import_dialog.raise_()
        if path:
            self._import_dialog.choose_file(path)
        return self._import_dialog

    def import_work(self, data: "ri.ImportFile", save: bool) -> Callable:
        """
        The work of a dry run (`save` False: the plan) or of the import (the
        result) of a file, to run in a task or at once (the Python API): the
        named areas read when the file names some and they are not read yet.
        """
        s = self.settings
        if s is None:
            raise ValueError("参考情報は使えない（管理者に連絡: referenceInfo）［E703］")
        client = self.context.esri()
        token, links_token = self._tokens()
        reliabilities, kinds = self.reliabilities(), self.kinds()
        book = self.area_book()
        named = book.settings if book is not None else None
        listed = book.list if book is not None and named is not None else None
        area_tokens = {k: self.context.token_for(x.url, x.token) for k, x in
                       (("areas", named.areas), ("categories", named.categories), ("countries", named.countries)) if x is not None} \
            if named is not None else {}
        needs = any(isinstance(r, dict) and r.get("area") for r in data.rows)

        def work(_task=None):
            from ..core.named_areas import load_areas, read_shapes

            known = ri.Known(reliabilities=reliabilities, kinds=kinds)
            areas = listed if listed is not None or not needs or named is None else load_areas(client, named, area_tokens)
            if areas is not None:
                known.areas = areas.areas
                known.shapes = lambda wanted: read_shapes(client, named.areas, [a.oid for a in wanted], area_tokens.get("areas"),
                                                          areas.oid_field)
            if not save:
                return ri.plan_import(client, s, data, known, token, links_token)
            result = ri.apply_import(client, s, data, known, token, links_token)
            audit.record("edit", "reference-import", data.name, count=result.added + result.updated,
                         ids=[x.id for x in result.saved][:1000],
                         detail={"summary": result.summary(), "links": result.links, "failures": len(result.failures)},
                         result="ok" if not result.failures else "partial")
            return result

        return work

    def run_import(self, data: "ri.ImportFile", save: bool, done: Callable, failed: Callable[[str], None]) -> None:
        """A dry run or the import of a file in a task; after an import, the infos imported listed on their own."""
        try:
            work = self.import_work(data, save)
        except ValueError as error:
            failed(str(error))
            return

        def finished(value) -> None:
            if save:
                saved = list(value.saved)
                self.reload(then=lambda: self.show_infos(saved, self.tr("取り込んだ参考情報")) if saved else None)
            done(value)

        self._start(self.tr("参考情報の取り込み") if save else self.tr("参考情報の取り込みの試行"), work, finished,
                    lambda m: (self.say(self.tr("参考情報の取り込みに失敗: {m}").format(m=m)), failed(m)))

    def close_down(self) -> None:
        for task in list(self._tasks):
            task.cancel()
        if self._form is not None and not sip.isdeleted(self._form):
            self._form.close()
        if self._import_dialog is not None and not sip.isdeleted(self._import_dialog):
            self._import_dialog.close()
        for band in (self.points, self.chosen_point):
            try:
                scene = band.scene()
                if scene is not None:
                    scene.removeItem(band)
            except RuntimeError:
                pass
        try:
            self.canvas.destinationCrsChanged.disconnect(self.draw)
            self.canvas.extentsChanged.disconnect(self._extent_changed)
        except (TypeError, RuntimeError):
            pass
