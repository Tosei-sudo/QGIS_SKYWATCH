"""
「観測記録のエクスポート」: the records of a period, a place, some 大分類 and
確度 written as CSV or JSON by a named format (core/obs/export.py).

The place is the map's extent, a polygon drawn on the map, the polygons of
a layer (all, or those selected), or a named area (the settings' namedAreas,
as the 通報). The named areas and geofences a format writes (地名, ジオフェンス)
are read from Esri in a task when saving; the file goes where asked.
"""
from __future__ import annotations

import json
from typing import Callable, Optional

from qgis.core import QgsFeatureRequest, QgsMapLayerProxyModel, QgsPointXY, QgsRectangle, QgsWkbTypes
from qgis.gui import QgsMapLayerComboBox, QgsRubberBand
from qgis.PyQt.QtCore import QDate, QDateTime, Qt, QTime, QTimer
from qgis.PyQt.QtGui import QColor
from qgis.PyQt.QtWidgets import (
    QCheckBox, QComboBox, QDateTimeEdit, QDialog, QFileDialog, QFormLayout, QHBoxLayout, QLabel, QListWidget, QListWidgetItem,
    QPushButton, QStackedWidget, QVBoxLayout, QWidget,
)

from ..core import audit, edit_stamp
from ..core.obs.api import load_geofences, load_named_areas
from ..core.obs.api import (
    BASES, FENCE_SOURCES, PLACE_SOURCES, ExportContext, ExportFilter, ExportSettings, items_of, render, select_records, settings_for,
)
from ..core.obs.api import now_ms
from ..core.obs.api import areas_at, points_of
from ..core.obs.api import RecordQuery
from ..qgis_io.geometry import from_wgs84, shape_of, to_wgs84
from ..qgis_io.tasks import run_task
from .docks import plain_label
from .fit import fit_text
from .last_inputs import InputMemory
from .text_assist_ui import SearchWords, SummaryDialog

#: The place choices (their data, their label).
PLACES = (("all", "すべての場所"), ("extent", "地図の表示範囲"), ("drawn", "地図上に描いた範囲"), ("layer", "レイヤーの地物"),
          ("named", "地名"))


def _time_edit(t: QTime) -> QDateTimeEdit:
    e = QDateTimeEdit(QDateTime(QDateTime.currentDateTime().date(), t))
    e.setCalendarPopup(True)
    e.setDisplayFormat("yyyy-MM-dd HH:mm")
    e.setEnabled(False)
    return e


class ExportDialog(QDialog):
    """「観測記録のエクスポート」 (kept by the observation dock; not modal, so the map can be drawn on)."""

    def __init__(self, dock, parent=None):
        super().__init__(parent)
        fit_text(self)
        self.dock = dock
        self.canvas = dock.canvas
        self.setWindowTitle(self.tr("観測記録のエクスポート"))
        self.drawn: Optional[list] = None
        self.named: Optional[list] = None
        self._tool = None
        self._was_tool = None
        #: The drawn polygon on the map (made when drawn, taken off when closed).
        self.band: Optional[QgsRubberBand] = None
        layout = QVBoxLayout(self)
        form = QFormLayout()

        # 言葉で探す (`textAssist.tasks.searchWords`): a sentence made into the conditions below (tsudo 2026-11-19).
        self.search_words = SearchWords(dock.context.text_assist, self, self)
        self.search_label = QLabel(self.tr("言葉で探す"))
        form.addRow(self.search_label, self.search_words.row)
        form.addRow(self.search_words.panel)
        self._after_named: Optional[Callable[[], None]] = None

        self.format_select = QComboBox()
        self.format_select.currentIndexChanged.connect(lambda _i: self._format_changed())
        form.addRow(self.tr("フォーマット"), self.format_select)
        self.format_note = QLabel()
        self.format_note.setWordWrap(True)
        form.addRow("", self.format_note)

        # 期間.
        self.basis_select = QComboBox()
        for key, label in BASES.items():
            self.basis_select.addItem(self.tr(label), key)
        self.basis_select.currentIndexChanged.connect(lambda _i: self.count())
        form.addRow(self.tr("期間の基準"), self.basis_select)
        self.start_check, self.start_edit = QCheckBox(self.tr("指定")), _time_edit(QTime(0, 0))
        self.end_check, self.end_edit = QCheckBox(self.tr("指定")), _time_edit(QTime(23, 59))
        for check, edit, label in ((self.start_check, self.start_edit, self.tr("期間（から）")), (self.end_check, self.end_edit, self.tr("期間（まで）"))):
            check.toggled.connect(edit.setEnabled)
            check.toggled.connect(lambda _on: self.count())
            edit.dateTimeChanged.connect(lambda _d: self.count())
            row = QHBoxLayout()
            row.addWidget(check)
            row.addWidget(edit, 1)
            form.addRow(label, row)

        # 場所.
        self.place_select = QComboBox()
        for key, label in PLACES:
            self.place_select.addItem(self.tr(label), key)
        self.place_select.currentIndexChanged.connect(lambda _i: self._place_changed())
        form.addRow(self.tr("場所"), self.place_select)
        self.place_stack = QStackedWidget()
        self.place_stack.addWidget(QWidget())  # all
        extent = QWidget()
        el = QHBoxLayout(extent)
        el.setContentsMargins(0, 0, 0, 0)
        self.extent_label = QLabel(self.tr("保存時の地図の表示範囲"))
        el.addWidget(self.extent_label, 1)
        self.place_stack.addWidget(extent)
        drawn = QWidget()
        dl = QHBoxLayout(drawn)
        dl.setContentsMargins(0, 0, 0, 0)
        self.drawn_label = QLabel(self.tr("未描画"))
        self.draw_button = QPushButton(self.tr("地図上に描く"))
        self.draw_button.setToolTip(self.tr("クリックで頂点、ダブルクリックか右クリックで閉じる（Esc で中止）"))
        self.draw_button.clicked.connect(self.start_drawing)
        dl.addWidget(self.drawn_label, 1)
        dl.addWidget(self.draw_button)
        self.place_stack.addWidget(drawn)
        layer = QWidget()
        ll = QHBoxLayout(layer)
        ll.setContentsMargins(0, 0, 0, 0)
        self.layer_select = QgsMapLayerComboBox()
        self.layer_select.setFilters(QgsMapLayerProxyModel.Filter.PolygonLayer)
        self.layer_select.layerChanged.connect(lambda _l: self.count())
        self.selected_check = QCheckBox(self.tr("選択した地物のみ"))
        self.selected_check.toggled.connect(lambda _on: self.count())
        ll.addWidget(self.layer_select, 1)
        ll.addWidget(self.selected_check)
        self.place_stack.addWidget(layer)
        named = QWidget()
        nl = QHBoxLayout(named)
        nl.setContentsMargins(0, 0, 0, 0)
        self.named_select = QComboBox()
        self.named_select.setEditable(True)
        self.named_select.setInsertPolicy(QComboBox.InsertPolicy.NoInsert)
        self.named_select.currentIndexChanged.connect(lambda _i: self.count())
        self.named_button = QPushButton(self.tr("最新を取得"))
        self.named_button.setToolTip(self.tr("最新の地名の一覧を取得"))
        self.named_button.clicked.connect(lambda: self.read_named(again=True))
        nl.addWidget(self.named_select, 1)
        nl.addWidget(self.named_button)
        self.place_stack.addWidget(named)
        form.addRow("", self.place_stack)

        # 大分類, 確度, 有無.
        self.all_roots = QCheckBox(self.tr("すべての大分類"))
        self.all_roots.setChecked(True)
        self.all_roots.toggled.connect(lambda on: (self.root_list.setEnabled(not on), self.count()))
        self.root_list = QListWidget()
        self.root_list.setMaximumHeight(110)
        self.root_list.setEnabled(False)
        self.root_list.itemChanged.connect(lambda _i: self.count())
        roots = QVBoxLayout()
        roots.addWidget(self.all_roots)
        roots.addWidget(self.root_list)
        form.addRow(self.tr("大分類"), roots)
        self.confidence_select = QComboBox()
        form.addRow(self.tr("確度"), self.confidence_select)
        self.confidence_select.currentIndexChanged.connect(lambda _i: self.count())
        self.presence_select = QComboBox()
        for key, label in (("all", "すべて"), ("present", "ありのみ"), ("absent", "なしのみ"), ("unseen", "不可のみ")):
            self.presence_select.addItem(self.tr(label), key)
        self.presence_select.currentIndexChanged.connect(lambda _i: self.count())
        form.addRow(self.tr("有無"), self.presence_select)
        self.deleted_check = QCheckBox(self.tr("削除済みの記録も含める"))
        self.deleted_check.toggled.connect(lambda _on: self.count())
        form.addRow("", self.deleted_check)
        layout.addLayout(form)

        self.count_label = plain_label(QLabel())
        layout.addWidget(self.count_label)
        self.status = plain_label(QLabel())
        self.status.setWordWrap(True)
        self.status.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        layout.addWidget(self.status)
        actions = QHBoxLayout()
        actions.addStretch(1)
        self.save_button = QPushButton(self.tr("保存…"))
        self.save_button.setToolTip(self.tr("条件に合う記録をフォーマットのファイルに保存"))
        self.save_button.clicked.connect(lambda: self.save())
        # まとめ (`textAssist.tasks.summary`): the records found, in a few sentences.
        self.summary_button = QPushButton(self.tr("まとめ…"))
        self.summary_button.setToolTip(self.tr("条件に合う記録を数文にまとめた下書き（文章の手伝い。この PC の生成 AI）"))
        self.summary_button.clicked.connect(lambda: self.summarize())
        close = QPushButton(self.tr("閉じる"))
        close.clicked.connect(self.close)
        for b in (self.summary_button, self.save_button, close):
            b.setAutoDefault(False)
            actions.addWidget(b)
        layout.addLayout(actions)
        #: The choices last used (入力の初期値の記憶).
        self.memory = InputMemory("observation_export", {
            "format": self.format_select, "basis": self.basis_select, "place": self.place_select, "presence": self.presence_select,
            "confidence": self.confidence_select,
        })
        #: A shared store's records are asked of it for the period and place (never every record): those read,
        #: for which question, and the question waiting to be asked (once the form has stopped changing).
        self._read: tuple = (None, [])
        self._wanted: Optional[RecordQuery] = None
        self._asking: Optional[RecordQuery] = None
        self._ask_timer = QTimer(self)
        self._ask_timer.setSingleShot(True)
        self._ask_timer.setInterval(500)
        self._ask_timer.timeout.connect(self._ask)
        self.reset()
        self.memory.restore()
        book = dock.book
        if book is not None and book.scoped and not self.start_check.isChecked() and not self.end_check.isChecked():
            # A shared store: from today, unless another period is chosen.
            self.start_check.setChecked(True)
        self._place_changed()
        self.count()

    # ---- settings ------------------------------------------------------------

    @property
    def settings(self) -> ExportSettings:
        config = self.dock.context.config
        return settings_for(config.observation_exports, config.alert_reports)

    def _token(self, source) -> Optional[str]:
        return self.dock.context.token_for(source.url, source.token) if source else None

    def reset(self) -> None:
        """The formats (built-in, then the settings file's), 大分類 and 確度 of the book (again)."""
        s = self.settings
        was = self.format_select.currentData()
        self.format_select.blockSignals(True)
        self.format_select.clear()
        for f in s.formats:
            self.format_select.addItem(f"{f.name}（{f.format.upper()}）" + (self.tr("・既定") if f.name == s.default else ""), f.name)
        i = self.format_select.findData(was) if was else -1
        self.format_select.setCurrentIndex(max(0, i if i >= 0 else self.format_select.findData(s.default)))
        self.format_select.blockSignals(False)
        book = self.dock.book
        checked = {self.root_list.item(i).text() for i in range(self.root_list.count())
                   if self.root_list.item(i).checkState() == Qt.CheckState.Checked}
        fresh = self.root_list.count() == 0
        self.root_list.blockSignals(True)
        self.root_list.clear()
        for r in self.roots():
            item = QListWidgetItem(r)
            item.setFlags(item.flags() | Qt.ItemFlag.ItemIsUserCheckable)
            on = r in checked or fresh and (not self.dock.class_filter or r in self.dock.class_filter)
            item.setCheckState(Qt.CheckState.Checked if on else Qt.CheckState.Unchecked)
            self.root_list.addItem(item)
        self.root_list.blockSignals(False)
        was = self.confidence_select.currentData()
        self.confidence_select.blockSignals(True)
        self.confidence_select.clear()
        self.confidence_select.addItem(self.tr("すべて"), "")
        levels = book.confidences() if book else []
        for i, c in enumerate(levels):
            self.confidence_select.addItem(c.label if i == len(levels) - 1 else f"{c.label} 以上", c.label)
        self.confidence_select.setCurrentIndex(max(0, self.confidence_select.findData(was)))
        self.confidence_select.blockSignals(False)
        named = s.named_areas is not None
        i = self.place_select.findData("named")
        self.place_select.model().item(i).setEnabled(named)
        self.place_select.setItemData(i, None if named else self.tr("設定ファイルに namedAreas（地名レイヤー）なし"), Qt.ItemDataRole.ToolTipRole)
        if not named and self.place_select.currentData() == "named":
            self.place_select.setCurrentIndex(0)
        self._format_changed()

    def roots(self) -> list[str]:
        """The 大分類 of the classification and of the records."""
        book = self.dock.book
        if book is None:
            return []
        taxa = book.taxonomy()
        out = [t.path[0] for t in taxa if len(t.path) == 1]
        for o in book.records:
            r = (self.dock.kind_path(o.kind, taxa) or [""])[0]
            if r and r not in out:
                out.append(r)
        return out

    def _fmt(self):
        return self.settings.format(self.format_select.currentData() or "")

    def _format_changed(self) -> None:
        f = self._fmt()
        s = self.settings
        if f is None:
            self.format_note.setText("")
            return
        extra = []
        if f.needs(PLACE_SOURCES) and s.named_areas is not None:
            extra.append(self.tr("地名"))
        if f.needs(FENCE_SOURCES) and s.geofences is not None:
            extra.append(self.tr("ジオフェンス"))
        what = self.tr("JSON Schema") if f.schema is not None else (self.tr("{n} 列").format(n=len(f.columns)) if f.columns else self.tr("従来の CSV"))
        self.format_note.setText(f"{f.format.upper()}・{what}" + (self.tr("・付加: ") + "・".join(extra) if extra else "") + f"・{f.file_name}")

    def _place_changed(self) -> None:
        key = self.place_select.currentData()
        self.place_stack.setCurrentIndex(max(0, [k for k, _ in PLACES].index(key) if key in [k for k, _ in PLACES] else 0))
        self._show_band()
        if key == "named" and self.named is None:
            self.read_named()
        self.count()

    def _say(self, text: str) -> None:
        self.status.setText(text)

    # ---- the place ------------------------------------------------------------

    def start_drawing(self) -> None:
        """Draws the polygon on the map (the map tool put back afterwards)."""
        from .catalog_overlay import DrawTool

        if self._tool is None:
            self._tool = DrawTool(self.canvas, self.set_drawn)
        if self.canvas.mapTool() is not self._tool:
            self._was_tool = self.canvas.mapTool()
        self.canvas.setMapTool(self._tool)
        self._say(self.tr("地図上でクリックして頂点、ダブルクリックか右クリックで閉じる"))

    def set_drawn(self, corners: list) -> None:
        """The polygon drawn (WGS 84 corners)."""
        ring = [list(p) for p in corners]
        if ring and ring[0] != ring[-1]:
            ring.append(list(ring[0]))
        self.drawn = [ring]
        self.drawn_label.setText(self.tr("描画済み（{n} 頂点）").format(n=len(corners)))
        self._show_band()
        self._stop_drawing()
        self._say("")
        self.count()

    def _show_band(self) -> None:
        """The drawn polygon on the map while 「地図上に描いた範囲」 is chosen."""
        if not self.drawn or self.place_select.currentData() != "drawn":
            self._drop_band()
            return
        if self.band is None:
            self.band = QgsRubberBand(self.canvas, QgsWkbTypes.GeometryType.PolygonGeometry)
            self.band.setStrokeColor(QColor("#7c3aed"))
            self.band.setFillColor(QColor(124, 58, 237, 30))
            self.band.setWidth(2)
            self.band.setZValue(954)
        to_map = from_wgs84(self.canvas.mapSettings().destinationCrs())
        self.band.reset(QgsWkbTypes.GeometryType.PolygonGeometry)
        for x, y in self.drawn[0][:-1]:
            self.band.addPoint(to_map.transform(QgsPointXY(x, y)), False)
        self.band.updatePosition()
        self.band.update()

    def _drop_band(self) -> None:
        if self.band is not None:
            self.canvas.scene().removeItem(self.band)
            self.band = None

    def showEvent(self, event) -> None:  # noqa: N802 (Qt's name)
        super().showEvent(event)
        self._show_band()

    def _stop_drawing(self) -> None:
        if self._tool is not None and self.canvas.mapTool() is self._tool:
            if self._was_tool is not None:
                self.canvas.setMapTool(self._was_tool)
            else:
                self.canvas.unsetMapTool(self._tool)
        self._was_tool = None

    def read_named(self, again: bool = False) -> None:
        """Reads the named areas (in a task) into the choice."""
        s = self.settings
        if s.named_areas is None:
            return
        client, source, token = self.dock.context.esri(), s.named_areas, self._token(s.named_areas)
        self.named_button.setEnabled(False)

        def finished(areas) -> None:
            self.named_button.setEnabled(True)
            self.named = areas
            was = self.named_select.currentText()
            self.named_select.blockSignals(True)
            self.named_select.clear()
            for name in sorted(dict.fromkeys(a.name for a in areas)):
                kinds = "・".join(dict.fromkeys(a.kind for a in areas if a.name == name and a.kind))
                self.named_select.addItem(f"{name}（{kinds}）" if kinds else name, name)
            self.named_select.setCurrentIndex(max(0, self.named_select.findData(was)))
            self.named_select.blockSignals(False)
            self.count()
            then, self._after_named = self._after_named, None
            if then is not None:
                then()

        def failed(message: str) -> None:
            self.named_button.setEnabled(True)
            self._say(self.tr("地名の取得に失敗: {m}").format(m=message))
            then, self._after_named = self._after_named, None
            if then is not None:
                then()  # 言葉で探す goes on without the place names

        run_task(self.tr("地名の取得"), lambda _t: load_named_areas(client, source, token, max_age=0 if again else 600), finished, failed)

    def area(self) -> tuple[Optional[list], str]:
        """The place's polygons (WGS 84; None anywhere) and its name; ([], why) when it cannot be told."""
        key = self.place_select.currentData()
        if key == "extent":
            crs = self.canvas.mapSettings().destinationCrs()
            box: QgsRectangle = to_wgs84(crs).transformBoundingBox(self.canvas.extent())
            w, s, e, n = box.xMinimum(), box.yMinimum(), box.xMaximum(), box.yMaximum()
            return [[[[w, s], [e, s], [e, n], [w, n], [w, s]]]], self.tr("地図の表示範囲")
        if key == "drawn":
            return ([self.drawn], self.tr("描いた範囲")) if self.drawn else ([], self.tr("範囲が未描画"))
        if key == "layer":
            layer = self.layer_select.currentLayer()
            if layer is None:
                return [], self.tr("レイヤーなし")
            transform = to_wgs84(layer.crs())
            selected = self.selected_check.isChecked()
            features = layer.selectedFeatures() if selected else layer.getFeatures(QgsFeatureRequest().setNoAttributes())
            polygons = []
            for f in features:
                shape = shape_of(f.geometry(), transform)
                if shape is not None:
                    polygons += [[[list(p) for p in ring] for ring in rings] for rings in shape.polygons]
            if not polygons:
                return [], self.tr("選択した地物なし") if selected else self.tr("地物なし")
            return polygons, layer.name() + (self.tr("（選択）") if selected else "")
        if key == "named":
            name = self.named_select.currentData()
            if name is None:
                text = self.named_select.currentText().strip()
                name = text if any(a.name == text for a in self.named or []) else None
            if not name:
                return [], self.tr("地名が未選択")
            return [a.rings for a in self.named or [] if a.name == name], name
        return None, ""

    # ---- which records ------------------------------------------------------------

    def options(self) -> tuple[Optional[ExportFilter], str]:
        """The filter the form says (None and why when the place cannot be told)."""
        polygons, name = self.area()
        if polygons is not None and not polygons:
            return None, name
        roots = None
        if not self.all_roots.isChecked():
            roots = [self.root_list.item(i).text() for i in range(self.root_list.count())
                     if self.root_list.item(i).checkState() == Qt.CheckState.Checked]
        return ExportFilter(
            start=self.start_edit.dateTime().toMSecsSinceEpoch() if self.start_check.isChecked() else None,
            end=self.end_edit.dateTime().toMSecsSinceEpoch() + 59_999 if self.end_check.isChecked() else None,
            basis=self.basis_select.currentData() or "image", polygons=polygons, area_name=name, roots=roots,
            confidence=self.confidence_select.currentData() or "", presence=self.presence_select.currentData() or "all",
            deleted=self.deleted_check.isChecked(),
        ), ""

    @staticmethod
    def query_of(flt: ExportFilter) -> RecordQuery:
        """What a shared store is asked for a filter: its period, the box around its place (the rest is tested here)."""
        box = None
        if flt.polygons:
            points = [p for rings in flt.polygons for ring in rings for p in ring]
            if points:
                box = (min(p[0] for p in points), min(p[1] for p in points), max(p[0] for p in points), max(p[1] for p in points))
        return RecordQuery(basis=flt.basis, start=flt.start, end=flt.end, box=box, living=not flt.deleted)

    def records(self) -> tuple[list, str]:
        book = self.dock.book
        if book is None:
            return [], self.tr("観測記録の保存先なし")
        flt, why = self.options()
        if flt is None:
            return [], why
        if book.scoped:
            query = self.query_of(flt)
            if query.everything():
                return [], self.tr("期間か場所を指定（共有の保存先の全件は取得しない）")
            if self._read[0] != query:
                self._wanted = query
                self._ask_timer.start()
                return [], self.tr("観測記録を取得中…")
            rows = self._read[1]
        elif not self.dock.records_ready(self._records_read):
            # Not read yet (no image chosen in 「観測記録」): counted again once they are.
            return [], self.tr("観測記録を取得中…")
        else:
            rows = book.records
        taxa = book.taxonomy()
        return select_records(rows, flt, book.confidences(), lambda kind: self.dock.kind_path(kind, taxa)), ""

    def _ask(self) -> None:
        """Asks the shared store for the records of the form's period and place (one question at a time)."""
        query = self._wanted
        if query is None or self._asking is not None or self._read[0] == query:
            return
        self._asking = query

        def done(rows) -> None:
            self._asking = None
            self._read = (query, list(rows or []))
            if self._wanted == query:
                self._records_read()
            else:
                self._ask()

        def failed(message: str) -> None:
            self._asking = None
            if self._wanted == query:
                self.count_label.setText(self.tr("取得に失敗: {m}").format(m=message))
            else:
                self._ask()

        self.dock.fetch(self.tr("エクスポートする記録の取得"), lambda b: b.fetch(query), done, failed)

    def _records_read(self) -> None:
        """The records just read: the 大分類 they use listed, and those that match counted."""
        self.reset()
        self.count()

    def count(self) -> int:
        rows, why = self.records()
        self.count_label.setText(why or self.tr("該当する記録 {n} 件").format(n=len(rows)))
        self.save_button.setEnabled(bool(rows))
        assist = self.dock.context.text_assist
        self.search_label.setVisible(assist.has("searchWords"))
        self.summary_button.setVisible(assist.has("summary"))
        self.summary_button.setEnabled(bool(rows))
        return len(rows)

    # ---- 文章の手伝い (ui/text_assist_ui.py) --------------------------------------

    def summarize(self) -> Optional[SummaryDialog]:
        """「まとめ…」: the records that meet the conditions in a few sentences."""
        rows, why = self.records()
        if not rows:
            self._say(why or self.tr("該当する記録なし"))
            return None
        from ..core.obs.api import local_time

        dialog = SummaryDialog(self.dock.context.text_assist, rows, local_time, self.tr("条件に合う記録"), self)
        dialog.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
        dialog.show()
        dialog.start()
        self._summary = dialog
        return dialog

    def search_choices(self) -> tuple[list[str], list[str], list[str]]:
        """言葉で探す's choices: the named areas (read), the 大分類 and the 確度 levels."""
        places = [self.named_select.itemData(i) for i in range(self.named_select.count())]
        roots = [self.root_list.item(i).text() for i in range(self.root_list.count())]
        confidences = [self.confidence_select.itemData(i) for i in range(self.confidence_select.count()) if self.confidence_select.itemData(i)]
        return [p for p in places if p], roots, confidences

    def search_kinds(self) -> list[str]:
        """言葉で探す's kinds (「船舶 > 補給艦」): a class the words name gives its 大分類."""
        from ..core.obs.api import kind_of

        book = self.dock.book
        return [kind_of(t.path) for t in book.taxonomy()] if book is not None else []

    def today(self):
        return QDateTime.currentDateTime().date().toPyDate()

    def prepare(self, then: Callable[[], None]) -> None:
        """The named areas read first (when the settings have them and they are not), then `then`."""
        if self.settings.named_areas is None or self.named is not None:
            then()
            return
        self._after_named = then
        self.read_named()

    def apply_search(self, found) -> None:
        """The conditions read by 言葉で探す put in the form (the rest as it was)."""
        if found.start or found.end:
            for check, edit, day, t in ((self.start_check, self.start_edit, found.start, QTime(0, 0)),
                                        (self.end_check, self.end_edit, found.end, QTime(23, 59))):
                check.setChecked(day is not None)
                if day is not None:
                    edit.setDateTime(QDateTime(QDate(day.year, day.month, day.day), t))
        if found.place:
            self.place_select.setCurrentIndex(max(0, self.place_select.findData("named")))
            i = self.named_select.findData(found.place)
            if i >= 0:
                self.named_select.setCurrentIndex(i)
        if found.roots:
            self.all_roots.setChecked(False)
            self.root_list.blockSignals(True)
            for i in range(self.root_list.count()):
                item = self.root_list.item(i)
                item.setCheckState(Qt.CheckState.Checked if item.text() in found.roots else Qt.CheckState.Unchecked)
            self.root_list.blockSignals(False)
        if found.confidence:
            i = self.confidence_select.findData(found.confidence)
            if i >= 0:
                self.confidence_select.setCurrentIndex(i)
        if found.presence:
            self.presence_select.setCurrentIndex(max(0, self.presence_select.findData(found.presence)))
        self.count()

    # ---- saving ------------------------------------------------------------

    def save(self, path: Optional[str] = None) -> None:
        """Writes the records to a file (named areas and geofences read first, in a task, when the format writes them)."""
        self.memory.save()
        rows, why = self.records()
        f = self._fmt()
        if not rows or f is None:
            self._say(why or self.tr("該当する記録なし"))
            return
        flt, _ = self.options()
        s = self.settings
        now = now_ms()
        if not path:
            from ..core.obs.api import file_name_of

            kind = "JSON (*.json)" if f.format == "json" else "CSV (*.csv *.txt)"
            path, _ = QFileDialog.getSaveFileName(self, self.tr("観測記録のエクスポート"), file_name_of(f, now), kind)
        if not path:
            return
        client = self.dock.context.esri()
        named_source = s.named_areas if f.needs(PLACE_SOURCES) else None
        fence_source = s.geofences if f.needs(FENCE_SOURCES) else None
        tokens = (self._token(named_source), self._token(fence_source))
        ctx = ExportContext(f, flt, now, edit_stamp.user_name(), places=named_source is not None, fences=fence_source is not None)

        def load_schema(where: str) -> dict:
            with open(where, encoding="utf-8-sig") as fh:
                return json.load(fh)

        def work(_task):
            # The records' squares from the cache (place_cache): the whole layer is not read.
            named = areas_at(client, named_source, points_of(rows), tokens[0]) if named_source else []
            fences = load_geofences(client, fence_source, tokens[1]) if fence_source else []
            out = render(items_of(rows, named, fences), ctx, load_schema)
            if out.data:
                with open(path, "wb") as fh:
                    fh.write(out.data)
            return out

        def finished(out) -> None:
            self.save_button.setEnabled(True)
            if not out.data:
                self._say(self.tr("保存に失敗: {e}").format(e="；".join(out.errors)))
                audit.error("file-save", path, "；".join(out.errors[:5]), detail={"what": "observations"})
                return
            note = self.tr("（注意: {e}）").format(e="；".join(out.errors[:5]) + ("…" if len(out.errors) > 5 else "")) if out.errors else ""
            audit.record("export", "observations", path, count=out.count, detail={"format": getattr(f, "name", "") or ""}, file=path)
            self._say(self.tr("{n} 件を保存: {p}{note}").format(n=out.count, p=path, note=note))
            self.dock.say(self.tr("観測記録をエクスポート: {p}").format(p=path))

        def failed(message: str) -> None:
            self.save_button.setEnabled(True)
            self._say(self.tr("保存に失敗: {m}").format(m=message))

        self.save_button.setEnabled(False)
        self._say(self.tr("保存中…"))
        run_task(self.tr("観測記録のエクスポート"), work, finished, failed)

    def closeEvent(self, event) -> None:  # noqa: N802 (Qt's name)
        self._stop_drawing()
        self._drop_band()
        self.memory.save()
        super().closeEvent(event)

    def remove(self) -> None:
        """Takes the drawing off the map (the dock is going)."""
        self._stop_drawing()
        if self._tool is not None:
            self._tool.remove()
        self._drop_band()
