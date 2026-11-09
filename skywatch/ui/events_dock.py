"""
「イベント」: an earthquake, a flood, an accident … and what was done for it
— its images, observation records and imaging requests — kept together
(core/events.py; the events and their links are in Esri, the settings file's
`events`).

The dock lists the events (対応中 first; 終了 ones on demand) with how many
items each holds. 「新規…」「編集…」 ask the name, kind, status, period, area and
note; 「終了」 closes an event (its end set to now when it had none) and
「再開」 opens it again. Below, the chosen event's items by type: open them
(an image as a raster layer, a record in 「観測記録」, a request in 「撮像依頼」),
take them out of the event, set their note. 「地図に表示」 adds the event's
area, its images' footprints and its records' points as layers;
「画像を検索」 searches the image catalog over the event's area and period;
「エクスポート…」 writes its items as CSV and 「概要をコピー」 copies a few lines.

Items come in from the other docks (「イベントに追加」 in 画像カタログ,
観測記録 and 撮像依頼): a menu of the open events, the one chosen here first.
An item can be in several events.
"""
from __future__ import annotations

import time
from typing import Callable, Optional, Sequence

from qgis.core import QgsFeature, QgsGeometry, QgsPointXY, QgsProject, QgsRectangle, QgsVectorLayer
from qgis.PyQt import sip
from qgis.PyQt.QtCore import QDateTime, QPoint, Qt
from qgis.PyQt.QtWidgets import (
    QAbstractItemView, QApplication, QCheckBox, QComboBox, QDateTimeEdit, QDialog, QDialogButtonBox, QFileDialog, QFormLayout,
    QHBoxLayout, QHeaderView, QInputDialog, QLabel, QLineEdit, QMenu, QMessageBox, QPlainTextEdit, QPushButton, QTableWidget,
    QTableWidgetItem, QTabWidget, QVBoxLayout, QWidget,
)

from ..core.catalog.search import record_of
from ..core.esri import quote
from ..core.events import (
    CLOSED, LISTED, OPEN, STATUSES, TYPE_LABELS, TYPES, Event, EventLink, LinkItem, add_links, counts, delete_event,
    export_csv, is_url, load_events, load_links, matches, new_event, remove_links, save_event, set_link_note, set_status, summary_text,
)
from ..qgis_io.catalog_io import Cancelled, PathError, add_record_layers, footprint, layer_area, open_record
from ..qgis_io.geometry import from_wgs84, to_wgs84
from ..qgis_io.plan_layer import make_field
from ..qgis_io.overviews_io import queue as overview_queue
from ..qgis_io.tasks import run_task
from .docks import PluginDock, plain_tip
from .flow_layout import FlowLayout

TITLE = "イベント"


def _local(ms: Optional[int]) -> str:
    return "" if ms is None else time.strftime("%Y-%m-%d %H:%M", time.localtime(ms / 1000))


def _time_edit(ms: Optional[int]) -> QDateTimeEdit:
    e = QDateTimeEdit(QDateTime.fromMSecsSinceEpoch(int(ms if ms is not None else time.time() * 1000)))
    e.setCalendarPopup(True)
    e.setDisplayFormat("yyyy-MM-dd HH:mm")
    return e


def _box_ring(box: QgsRectangle) -> list:
    x0, y0, x1, y1 = box.xMinimum(), box.yMinimum(), box.xMaximum(), box.yMaximum()
    return [[x0, y0], [x0, y1], [x1, y1], [x1, y0], [x0, y0]]


def _same_url(a: str, b: str) -> bool:
    return a.split("?", 1)[0].rstrip("/").lower() == b.split("?", 1)[0].rstrip("/").lower()


class EventForm(QDialog):
    """「新規」「編集」: the name, kind, status, period, area and note of an event."""

    def __init__(self, dock: "EventsDock", event: Optional[Event], kinds: Sequence[str], parent=None):
        super().__init__(parent)
        self.dock = dock
        self.event = event
        self.rings = [list(r) for r in event.rings] if event and event.rings else None
        self.setWindowTitle(self.tr("イベントの編集") if event else self.tr("新しいイベント"))
        form = QFormLayout(self)
        self.name = QLineEdit(event.name if event else "")
        self.name.setPlaceholderText(self.tr("例: 2026年11月 〇〇沖地震"))
        form.addRow(self.tr("名前"), self.name)
        self.kind = QComboBox()
        self.kind.setEditable(True)
        for k in kinds:
            self.kind.addItem(k)
        self.kind.setCurrentText(event.kind if event else "")
        form.addRow(self.tr("種別"), self.kind)
        self.status = QComboBox()
        self.status.addItems(list(STATUSES))
        self.status.setCurrentText(event.status if event else OPEN)
        form.addRow(self.tr("状態"), self.status)
        self.has_start = QCheckBox(self.tr("開始日時"))
        self.has_start.setChecked(event is None or event.start is not None)
        self.start = _time_edit(event.start if event else None)
        self.start.setEnabled(self.has_start.isChecked())
        self.has_start.toggled.connect(self.start.setEnabled)
        self.has_end = QCheckBox(self.tr("終了日時"))
        self.end = _time_edit(event.end if event else None)
        self.has_end.setChecked(bool(event and event.end is not None))
        self.end.setEnabled(self.has_end.isChecked())
        self.has_end.toggled.connect(self.end.setEnabled)
        period = QHBoxLayout()
        period.addWidget(self.has_start)
        period.addWidget(self.start, 1)
        period.addWidget(QLabel("〜"))
        period.addWidget(self.has_end)
        period.addWidget(self.end, 1)
        form.addRow(self.tr("期間"), period)
        area = QHBoxLayout()
        self.area_text = QLabel()
        area.addWidget(self.area_text, 1)
        for text, tip, slot in [
            (self.tr("地図の表示範囲"), self.tr("いまの地図の表示範囲を範囲にする"), self.use_extent),
            (self.tr("選択地物"), self.tr("アクティブなレイヤーの選択地物（ポリゴンはそのまま、ほかは外接矩形）を範囲にする"), self.use_selection),
            (self.tr("消去"), self.tr("範囲なし"), lambda: self.set_rings(None)),
        ]:
            b = QPushButton(text)
            b.setToolTip(tip)
            b.clicked.connect(slot)
            area.addWidget(b)
        form.addRow(self.tr("範囲"), area)
        self.note = QPlainTextEdit(event.note if event else "")
        self.note.setPlaceholderText(self.tr("経緯、対応方針、引き継ぎ事項など"))
        self.note.setMinimumHeight(80)
        form.addRow(self.tr("メモ"), self.note)
        self.problem = QLabel()
        self.problem.setStyleSheet("color: #b91c1c")
        form.addRow(self.problem)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Save | QDialogButtonBox.StandardButton.Cancel)
        self.button_box = buttons
        #: Saves the values (in a task): `saver(values, done, failed)`; the form closes only once saved.
        self.saver: Optional[Callable] = None
        buttons.accepted.connect(self._accept)
        buttons.rejected.connect(self.reject)
        form.addRow(buttons)
        self.set_rings(self.rings)
        self.resize(560, 0)

    def set_rings(self, rings: Optional[list]) -> None:
        self.rings = rings or None
        if not self.rings:
            self.area_text.setText(self.tr("なし"))
            return
        g = footprint({"rings": self.rings})
        b = g.boundingBox() if g is not None else None
        self.area_text.setText(self.tr("あり（{x0:.3f}〜{x1:.3f}, {y0:.3f}〜{y1:.3f}）").format(
            x0=b.xMinimum(), x1=b.xMaximum(), y0=b.yMinimum(), y1=b.yMaximum()) if b else self.tr("あり"))

    def use_extent(self) -> None:
        canvas = self.dock.iface.mapCanvas()
        box = to_wgs84(canvas.mapSettings().destinationCrs()).transformBoundingBox(canvas.extent())
        self.set_rings([_box_ring(box)])

    def use_selection(self) -> None:
        layer = self.dock.iface.activeLayer()
        shape = layer_area(layer, True)
        if shape is None:
            self.problem.setText(self.tr("アクティブなレイヤーに選択地物なし"))
            return
        self.problem.setText("")
        if shape.get("rings"):
            self.set_rings(shape["rings"])
            return
        coords = [p for path in shape.get("paths", []) for p in path] + list(shape.get("points", []))
        xs, ys = [p[0] for p in coords], [p[1] for p in coords]
        pad = 0.005 if max(xs) - min(xs) < 1e-6 or max(ys) - min(ys) < 1e-6 else 0
        self.set_rings([_box_ring(QgsRectangle(min(xs) - pad, min(ys) - pad, max(xs) + pad, max(ys) + pad))])

    def values(self) -> dict:
        return {
            "name": self.name.text().strip(), "kind": self.kind.currentText().strip(), "status": self.status.currentText(),
            "start": int(self.start.dateTime().toMSecsSinceEpoch()) if self.has_start.isChecked() else None,
            "end": int(self.end.dateTime().toMSecsSinceEpoch()) if self.has_end.isChecked() else None,
            "note": self.note.toPlainText().strip(), "rings": self.rings,
        }

    def _accept(self) -> None:
        v = self.values()
        if not v["name"]:
            self.problem.setText(self.tr("名前を入力"))
            return
        if v["end"] is not None and v["start"] is not None and v["end"] < v["start"]:
            self.problem.setText(self.tr("終了日時が開始日時より前"))
            return
        if self.saver is None:
            self.accept()
            return
        self.problem.setText(self.tr("保存中…"))
        self.button_box.setEnabled(False)

        def failed(message: str) -> None:
            # Kept open with what was typed: a save that did not go through loses nothing.
            if sip.isdeleted(self):
                return
            self.button_box.setEnabled(True)
            self.problem.setText(self.tr("保存できませんでした: {m}").format(m=message))

        # Closed meanwhile (Esc): the answer has no form to close.
        self.saver(v, lambda: None if sip.isdeleted(self) else self.accept(), failed)


class EventsDock(PluginDock):
    """「イベント」."""

    def __init__(self, iface, context, open_settings: Callable[[], None], docks: Callable[[], dict], parent=None):
        super().__init__("SatObsEvents", "イベント", context, open_settings, parent)
        self.iface = iface
        self.canvas = iface.mapCanvas()
        self.docks = docks
        #: Every event and every link, as read last (None: not read yet).
        self.events: Optional[list[Event]] = None
        self.links: list[EventLink] = []
        self.shown: list[Event] = []
        self.task = None
        self._then: list[Callable[[], None]] = []
        self._failed: list[Callable[[str], None]] = []
        self._tasks: list = []
        #: Read again when the running reload ends (the event id to choose, or True).
        self._again = None

        self.note = QLabel()
        self.note.setWordWrap(True)
        self.body.addWidget(self.note)
        top = QHBoxLayout()
        self.status_filter = QComboBox()
        self.status_filter.addItem(self.tr("対応中"), OPEN)
        self.status_filter.addItem(self.tr("終了"), CLOSED)
        self.status_filter.addItem(self.tr("すべて"), "")
        self.status_filter.currentIndexChanged.connect(lambda _i: self.render())
        self.search = QLineEdit()
        self.search.setPlaceholderText(self.tr("名前・種別・メモで絞り込み"))
        self.search.setClearButtonEnabled(True)
        self.search.textChanged.connect(lambda _t: self.render())
        self.reload_button = QPushButton(self.tr("再読み込み"))
        self.reload_button.clicked.connect(lambda: self.reload())
        top.addWidget(self.status_filter)
        top.addWidget(self.search, 1)
        top.addWidget(self.reload_button)
        self.body.addLayout(top)

        self.table = QTableWidget(0, 8)
        self.table.setHorizontalHeaderLabels([self.tr("名前"), self.tr("種別"), self.tr("状態"), self.tr("開始"), self.tr("終了"),
                                              self.tr("画像"), self.tr("観測記録"), self.tr("撮像依頼")])
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.verticalHeader().hide()
        self.table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.ResizeToContents)
        self.table.horizontalHeader().setStretchLastSection(True)
        self.table.itemSelectionChanged.connect(self._event_chosen)
        self.table.cellDoubleClicked.connect(lambda _r, _c: self.edit())
        self.body.addWidget(self.table, 1)

        self.buttons: dict[str, QPushButton] = {}
        flow = FlowLayout()
        for key, text, tip, slot in [
            ("new", self.tr("新規…"), self.tr("イベントを作成（いまの地図の表示範囲が範囲の初期値）"), self.create),
            ("edit", self.tr("編集…"), self.tr("名前・種別・状態・期間・範囲・メモを変更"), self.edit),
            ("close", self.tr("終了"), self.tr("終了にする（終了日時が空なら現在）。終了したイベントは追加先に出ない"), self.toggle_status),
            ("delete", self.tr("削除"), self.tr("イベントと紐付けを削除（画像・記録・依頼そのものは残る）"), self.delete),
            ("zoom", self.tr("範囲へ移動"), self.tr("イベントの範囲へ地図を移動"), self.zoom),
            ("map", self.tr("地図に表示"), self.tr("範囲・画像の範囲・観測記録の位置をレイヤーとして追加"), self.show_on_map),
            ("search", self.tr("画像を検索"), self.tr("イベントの範囲・期間で画像カタログを検索"), self.search_images),
            ("export", self.tr("エクスポート…"), self.tr("項目の一覧を CSV で保存"), self.export),
            ("summary", self.tr("概要をコピー"), self.tr("名前・状態・期間・件数・メモをクリップボードへ"), self.copy_summary),
        ]:
            b = QPushButton(text)
            b.setToolTip(tip)
            b.clicked.connect(lambda _on=False, slot=slot: slot())
            flow.addWidget(b)
            self.buttons[key] = b
        holder = QWidget()
        holder.setLayout(flow)
        self.body.addWidget(holder)

        self.event_note = QLabel()
        # Names and notes are other people's text: never read as HTML.
        self.event_note.setTextFormat(Qt.TextFormat.PlainText)
        self.event_note.setWordWrap(True)
        self.event_note.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        self.body.addWidget(self.event_note)
        self.tabs = QTabWidget()
        self.item_tables: dict[str, QTableWidget] = {}
        for t in TYPES:
            table = QTableWidget(0, 5)
            table.setHorizontalHeaderLabels([self.tr("名称"), self.tr("日時"), self.tr("メモ"), self.tr("追加者"), self.tr("追加日時")])
            table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
            table.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
            table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
            table.verticalHeader().hide()
            table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.ResizeToContents)
            table.horizontalHeader().setStretchLastSection(True)
            table.cellDoubleClicked.connect(lambda _r, _c, t=t: self.open_items(t))
            table.itemSelectionChanged.connect(self._update_buttons)
            self.item_tables[t] = table
            self.tabs.addTab(table, TYPE_LABELS[t])
        self.body.addWidget(self.tabs, 1)
        items = QHBoxLayout()
        for key, text, tip, slot in [
            ("open", self.tr("開く"), self.tr("画像はレイヤーとして開く、記録は「観測記録」、依頼は「撮像依頼」で表示"),
             lambda: self.open_items(self.current_type())),
            ("catalog", self.tr("カタログで表示"), self.tr("このイベントの画像を画像カタログの一覧に出す"), self.show_in_catalog),
            ("item_note", self.tr("メモ…"), self.tr("選んだ項目のメモ（このイベントでの扱い）"), self.edit_item_note),
            ("unlink", self.tr("イベントから外す"), self.tr("選んだ項目をこのイベントから外す（項目そのものは残る）"), self.unlink),
        ]:
            b = QPushButton(text)
            b.setToolTip(tip)
            b.clicked.connect(lambda _on=False, slot=slot: slot())
            items.addWidget(b)
            self.buttons[key] = b
        items.addStretch(1)
        self.body.addLayout(items)
        self.tabs.currentChanged.connect(lambda _i: self._update_buttons())
        self.status = QLabel()
        self.status.setTextFormat(Qt.TextFormat.PlainText)
        self.status.setWordWrap(True)
        self.body.addWidget(self.status)

        self.visibilityChanged.connect(lambda on: self.reload() if on and self.events is None and self.settings else None)
        context.configChanged.connect(self._config_changed)
        self._config_changed()

    # ---- state ---------------------------------------------------------------------------------

    @property
    def settings(self):
        return self.context.config.events

    def _config_changed(self) -> None:
        self.events, self.links = None, []
        if self.task is None:
            self._then, self._failed = [], []
        s = self.settings
        self.note.setText("" if s else self.tr("設定ファイルに events（イベントのレイヤーと紐付けテーブル）がないため使用不可"))
        self.note.setVisible(s is None)
        self.render()
        if s is not None and self.isVisible():
            self.reload()

    def say(self, text: str) -> None:
        self.status.setText(text)

    def chosen(self) -> Optional[Event]:
        rows = self.table.selectionModel().selectedRows()
        r = rows[0].row() if rows else -1
        return self.shown[r] if 0 <= r < len(self.shown) else None

    def choose(self, event_id: str) -> None:
        for i, e in enumerate(self.shown):
            if e.id == event_id:
                self.table.selectRow(i)
                return

    def current_type(self) -> str:
        return TYPES[self.tabs.currentIndex()]

    def links_of(self, event: Optional[Event], type_: Optional[str] = None) -> list[EventLink]:
        if event is None:
            return []
        return [x for x in self.links if x.event_id == event.id and (type_ is None or x.type == type_)]

    def chosen_links(self, type_: Optional[str] = None) -> list[EventLink]:
        type_ = type_ or self.current_type()
        table = self.item_tables[type_]
        shown = self.links_of(self.chosen(), type_)
        rows = sorted({i.row() for i in table.selectionModel().selectedRows()})
        return [shown[r] for r in rows if 0 <= r < len(shown)]

    def open_events(self) -> list[Event]:
        return [e for e in self.events or [] if e.open]

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

    def reload(self, then: Optional[Callable[[], None]] = None, keep: Optional[str] = None,
               failed: Optional[Callable[[str], None]] = None) -> None:
        """
        Reads the events and links again (in a task), then lists them (the
        event chosen stays chosen); `then` once read, `failed` with the reason when they cannot be.
        """
        s = self.settings
        if s is None:
            return
        if then is not None:
            self._then.append(then)
        if failed is not None:
            self._failed.append(failed)
        if self.task is not None:
            # Asked while one runs (after an add, or another settings file): read again once it ends.
            self._again = keep or self._again or True
            return
        self._again = None
        chosen = keep or (self.chosen().id if self.chosen() else None)
        client = self.context.esri()
        token = self.context.token_for(s.layer.url, s.layer.token)
        link_token = self.context.token_for(s.links.url, s.links.token)

        def work(_task):
            return load_events(client, s, token), load_links(client, s, None, link_token)

        def done(result) -> None:
            self.task = None
            if s is not self.settings or self._again:
                again = self._again
                self.reload(keep=again if isinstance(again, str) else chosen)
                return
            self.events, self.links = result
            self.render()
            if chosen:
                self.choose(chosen)
            n = len(self.open_events())
            self.say(self.tr("対応中 {n} 件・全 {m} 件").format(n=n, m=len(self.events)))
            waiting, self._then, self._failed = self._then, [], []
            for f in waiting:
                f()

        def not_read(message: str) -> None:
            self.task = None
            if s is not self.settings or self._again:
                self.reload()
                return
            told, self._then, self._failed = self._failed, [], []
            self.say(self.tr("イベントの読み込みに失敗: {m}").format(m=message))
            self._update_buttons()
            for f in told:
                f(message)

        self.task = True
        self._start(self.tr("イベントの読み込み"), work, done, not_read)

    # ---- showing -------------------------------------------------------------------------------

    def render(self) -> None:
        chosen = self.chosen()
        status, text = self.status_filter.currentData(), self.search.text()
        self.shown = [e for e in self.events or [] if matches(e, text, status or "")]
        c = counts(self.links)
        self.table.blockSignals(True)
        self.table.setRowCount(len(self.shown))
        for i, e in enumerate(self.shown):
            n = c.get(e.id, {})
            for j, v in enumerate([e.name, e.kind, e.status, _local(e.start), _local(e.end), *(str(n.get(t, 0)) for t in TYPES)]):
                item = QTableWidgetItem(v)
                if j >= 5:
                    item.setTextAlignment(int(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter))
                self.table.setItem(i, j, item)
        self.table.blockSignals(False)
        if chosen is not None:
            self.choose(chosen.id)
        self._event_chosen()

    def _event_chosen(self) -> None:
        e = self.chosen()
        if e is None:
            self.event_note.setText("")
        else:
            lines = [f"{e.name}{('（' + e.kind + '）') if e.kind else ''}　{e.status}"]
            if e.note:
                lines.append(e.note)
            self.event_note.setText("\n".join(lines))
        for t in TYPES:
            links = self.links_of(e, t)
            table = self.item_tables[t]
            table.blockSignals(True)
            table.setRowCount(len(links))
            for i, x in enumerate(links):
                for j, v in enumerate([x.label or x.item_id, _local(x.time), x.note, x.linked_by, _local(x.linked_at)]):
                    item = QTableWidgetItem(v)
                    item.setToolTip(plain_tip(f"{x.item_id}\n{x.source}" if j == 0 else v))
                    table.setItem(i, j, item)
            table.blockSignals(False)
            self.tabs.setTabText(TYPES.index(t), f"{TYPE_LABELS[t]}（{len(links)}）" if e else TYPE_LABELS[t])
        self._update_buttons()

    def _update_buttons(self) -> None:
        ready = self.settings is not None and self.events is not None and self.task is None
        e = self.chosen()
        for key in ("edit", "close", "delete", "export", "summary"):
            self.buttons[key].setEnabled(ready and e is not None)
        self.buttons["new"].setEnabled(ready)
        self.buttons["close"].setText(self.tr("再開") if e is not None and not e.open else self.tr("終了"))
        has_area = e is not None and bool(e.rings)
        self.buttons["zoom"].setEnabled(has_area)
        self.buttons["map"].setEnabled(e is not None)
        self.buttons["search"].setEnabled(e is not None)
        chosen = bool(self.chosen_links()) if e is not None else False
        for key in ("open", "item_note", "unlink"):
            self.buttons[key].setEnabled(ready and chosen if key != "open" else chosen)
        self.buttons["catalog"].setVisible(self.current_type() == "image")
        self.buttons["catalog"].setEnabled(bool(self.links_of(e, "image")))
        self.reload_button.setEnabled(self.settings is not None and self.task is None)

    # ---- events --------------------------------------------------------------------------------

    def _kinds(self) -> list[str]:
        used = [e.kind for e in self.events or []]
        return self.settings.kind_choices(used) if self.settings else list(dict.fromkeys(k for k in used if k))

    def create(self) -> Optional[EventForm]:
        if self.settings is None:
            return None
        form = EventForm(self, None, self._kinds(), self)
        form.use_extent()
        event_id = new_event("").id
        form.saver = lambda v, done, failed: self.save(Event(id=event_id, **v), lambda _s: done(), failed)
        form.finished.connect(lambda _r: form.deleteLater())
        form.open()
        return form

    def edit(self) -> Optional[EventForm]:
        e = self.chosen()
        if e is None or self.settings is None:
            return None
        form = EventForm(self, e, self._kinds(), self)

        form.saver = lambda v, done, failed: self.save(Event(id=e.id, oid=e.oid, created_by=e.created_by, **v), lambda _s: done(), failed)
        form.finished.connect(lambda _r: form.deleteLater())
        form.open()
        return form

    def save(self, event: Event, then: Optional[Callable[[Event], None]] = None, failed: Optional[Callable[[str], None]] = None) -> None:
        """Saves the event (in a task), then reads the list again with it chosen."""
        s = self.settings
        client, token = self.context.esri(), self.context.token_for(s.layer.url, s.layer.token)

        def done(saved: Event) -> None:
            if self.status_filter.currentData() and saved.status != self.status_filter.currentData():
                self.status_filter.setCurrentIndex(self.status_filter.findData(""))
            self.reload(lambda: (self.say(self.tr("「{n}」を保存").format(n=saved.name)), then(saved) if then else None), keep=saved.id)

        def fail(message: str) -> None:
            self._update_buttons()
            self.say(self.tr("イベントの保存に失敗: {m}").format(m=message))
            if failed is not None:
                failed(message)

        self._start(self.tr("イベントの保存"), lambda _t: save_event(client, s, event, token), done, fail)

    def toggle_status(self) -> None:
        e = self.chosen()
        if e is None:
            return
        s = self.settings
        client, token = self.context.esri(), self.context.token_for(s.layer.url, s.layer.token)
        status = OPEN if not e.open else CLOSED

        def done(saved: Event) -> None:
            if self.status_filter.currentData():
                self.status_filter.setCurrentIndex(self.status_filter.findData(""))
            self.reload(lambda: self.say(self.tr("「{n}」を{s}に").format(n=saved.name, s=saved.status)), keep=saved.id)

        self._start(self.tr("イベントの状態の変更"), lambda _t: set_status(client, s, e, status, token=token), done)

    def delete(self, confirm: bool = True) -> None:
        e = self.chosen()
        if e is None:
            return
        n = len(self.links_of(e))
        if confirm and QMessageBox.question(
                self, TITLE, plain_tip(self.tr("「{name}」を削除しますか？\n紐付け {n} 件も消えます（画像・記録・依頼そのものは残ります）。元に戻せません。")
                .format(name=e.name, n=n)), QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No) != QMessageBox.StandardButton.Yes:
            return
        s = self.settings
        client, token = self.context.esri(), self.context.token_for(s.layer.url, s.layer.token)
        link_token = self.context.token_for(s.links.url, s.links.token)
        self._start(self.tr("イベントの削除"), lambda _t: delete_event(client, s, e, token, link_token),
                    lambda removed: self.reload(lambda: self.say(self.tr("「{name}」を削除（紐付け {n} 件）").format(name=e.name, n=removed))))

    # ---- items ---------------------------------------------------------------------------------

    def link_items(self, event_ids: Sequence[str], items: Sequence[LinkItem], then: Optional[Callable[[int, int], None]] = None) -> None:
        """Puts items in events (in a task); says how many went in and how many were there already."""
        s = self.settings
        if s is None or not items or not event_ids:
            return
        client, token = self.context.esri(), self.context.token_for(s.links.url, s.links.token)
        names = [e.name for e in self.events or [] if e.id in event_ids]

        def done(result) -> None:
            added, skipped = result
            text = self.tr("「{e}」に {n} 件を追加").format(e="」「".join(names), n=added)
            if skipped:
                text += self.tr("（{k} 件は追加済み）").format(k=skipped)
            self.iface.messageBar().pushSuccess(TITLE, plain_tip(text))
            self.reload(lambda: (self.say(text), then(added, skipped) if then else None), keep=event_ids[0] if len(event_ids) == 1 else None)

        self._start(self.tr("イベントへの追加"), lambda _t: add_links(client, s, list(event_ids), list(items), token=token), done)

    def offer(self, anchor: QWidget, items: Callable[[], Sequence[LinkItem]]) -> Optional[QMenu]:
        """
        「イベントに追加」 of the other docks: a menu of the open events (the
        one chosen here first) at `anchor`, and 「新しいイベント…」.
        """
        if self.settings is None:
            return None
        if self.events is None:
            self.reload(lambda: self.offer(anchor, items))
            return None
        menu = QMenu(anchor)
        chosen = self.chosen()
        listed = sorted(self.open_events(), key=lambda e: e is not chosen)
        for e in listed:
            action = menu.addAction(e.name + (self.tr("（選択中）") if e is chosen else ""))
            action.triggered.connect(lambda _on=False, e=e: self.link_items([e.id], list(items())))
        if not listed:
            menu.addAction(self.tr("対応中のイベントなし")).setEnabled(False)
        menu.addSeparator()
        create = menu.addAction(self.tr("新しいイベント…"))
        create.triggered.connect(lambda: self._create_then_link(items))
        menu.popup(anchor.mapToGlobal(QPoint(0, anchor.height())))
        self._menu = menu
        return menu

    def _create_then_link(self, items: Callable[[], Sequence[LinkItem]]) -> None:
        chosen = list(items())
        form = EventForm(self, None, self._kinds(), self)
        form.use_extent()
        event_id = new_event("").id

        def saver(v, done, failed) -> None:
            self.save(Event(id=event_id, **v), lambda saved: (done(), self.link_items([saved.id], chosen)), failed)

        form.saver = saver
        form.finished.connect(lambda _r: form.deleteLater())
        form.open()
        self.show()
        self.raise_()

    def unlink(self, confirm: bool = True) -> None:
        links = self.chosen_links()
        if not links:
            return
        if confirm and QMessageBox.question(
                self, TITLE, self.tr("選んだ {n} 件をこのイベントから外しますか？（項目そのものは残ります）").format(n=len(links)),
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No, QMessageBox.StandardButton.No) != QMessageBox.StandardButton.Yes:
            return
        s = self.settings
        client, token = self.context.esri(), self.context.token_for(s.links.url, s.links.token)
        self._start(self.tr("イベントから外す"), lambda _t: remove_links(client, s, links, token),
                    lambda n: self.reload(lambda: self.say(self.tr("{n} 件を外した").format(n=n))))

    def edit_item_note(self, note: Optional[str] = None) -> None:
        links = self.chosen_links()
        if not links:
            return
        if note is None:
            note, ok = QInputDialog.getText(self, TITLE, self.tr("メモ（選んだ {n} 件）").format(n=len(links)), text=links[0].note)
            if not ok:
                return
        s = self.settings
        client, token = self.context.esri(), self.context.token_for(s.links.url, s.links.token)
        self._start(self.tr("メモの保存"), lambda _t: set_link_note(client, s, links, note.strip(), token),
                    lambda n: self.reload(lambda: self.say(self.tr("{n} 件のメモを保存").format(n=n))))

    # ---- opening the items ---------------------------------------------------------------------

    def _catalog_for(self, source: str):
        catalogs = self.context.config.image_catalogs
        return next((c for c in catalogs if _same_url(c.url, source)), None) or next((c for c in catalogs if c.label == source), None)

    def is_local(self, x: EventLink) -> bool:
        """An image link not of a catalog: opened by hand (its path) or listed without its file."""
        return x.type == "image" and self._catalog_for(x.source) is None and not is_url(x.source)

    def _known_images(self) -> dict:
        """The images the plugin knows without a catalog, by id: the project's raster layers and the images listed in 「観測記録」."""
        from ..qgis_io.obs_images import open_images

        dock = self._observation_dock()
        book = dock.book if dock else None
        out = {i.id: i for i in (book.images if book else []) if not i.file}
        try:
            out.update({i.id: i for i in open_images()})
        except Exception:  # noqa: BLE001 (a layer that cannot be read is left out)
            pass
        return out

    def _open_local(self, links: Sequence[EventLink]) -> list[str]:
        """Opens image links not of a catalog: the layer already open is shown, else the file is opened; what could not be."""
        import os

        from qgis.core import QgsRasterLayer

        from ..qgis_io.obs_images import image_layer, observed_image, set_manual_time

        project = QgsProject.instance()
        known = self._known_images()
        problems: list[str] = []
        shown = []
        for x in links:
            image = known.get(x.item_id)
            layer = project.mapLayer(image.layer_id) if image and image.layer_id else None
            if layer is None and x.source not in ("", LISTED) and os.path.isfile(x.source):
                layer = image_layer(x.source, os.path.basename(x.source), image_id=x.item_id)
                if not layer.isValid():
                    problems.append(self.tr("開けない画像: {p}").format(p=x.source))
                    continue
                if x.time is not None and observed_image(layer).time is None:
                    set_manual_time(layer, x.time)
                project.addMapLayer(layer)
                overview_queue.request([layer], self.context.config.overviews, self.iface.messageBar())
            if isinstance(layer, QgsRasterLayer):
                shown.append(layer)
            elif image is not None and not image.file:
                problems.append(self.tr("{n} はファイルなしの画像（「観測記録」で選択）").format(n=x.label or x.item_id))
            else:
                problems.append(self.tr("ファイルが見つからない画像: {n}").format(n=x.source if x.source not in ("", LISTED) else x.label or x.item_id))
        if shown:
            self.iface.setActiveLayer(shown[-1])
            box: Optional[QgsRectangle] = None
            for layer in shown:
                b = self._wgs84_box(layer)
                if b is None:
                    continue
                if box is None:
                    box = QgsRectangle(b)
                else:
                    box.combineExtentWith(b)
            if box is not None:
                self._fit(box)
        return problems

    @staticmethod
    def _wgs84_box(layer) -> Optional[QgsRectangle]:
        try:
            return to_wgs84(layer.crs()).transformBoundingBox(layer.extent())
        except Exception:  # noqa: BLE001
            return None

    def _fetch_images(self, links: Sequence[EventLink], done: Callable[[list], None]) -> None:
        """The catalog records of image links (in a task): [(catalog, [records])], and why some are missing."""
        groups: dict[int, tuple] = {}
        missing: list[str] = []
        links = [x for x in links if not self.is_local(x)]
        if not links:
            done([])
            return
        for x in links:
            c = self._catalog_for(x.source)
            if c is None:
                missing.append(x.label or x.item_id)
                continue
            groups.setdefault(id(c), (c, []))[1].append(x.item_id)
        client = self.context.esri()
        asks = [(c, ids, self.context.token_for(c.url, c.token)) for c, ids in groups.values()]

        def work(_task):
            out = []
            for c, ids, token in asks:
                id_field = c.fields.get("id")
                info = client.layer(c.url, token)
                records = []
                for i in range(0, len(ids), 100):
                    where = f"{id_field} IN ({','.join(quote(v) for v in ids[i:i + 100])})"
                    for f in client.query_all(c.url, where, token, limit=10_000):
                        records.append(record_of(f, c.fields, info.object_id_field))
                out.append((c, records))
            return out

        def finished(result) -> None:
            found = {r.id for _c, rs in result for r in rs}
            gone = missing + [x.label or x.item_id for x in links if self._catalog_for(x.source) and x.item_id not in found]
            if gone:
                self.say(self.tr("カタログに見つからない画像: {g}").format(g="、".join(gone[:5]) + ("…" if len(gone) > 5 else "")))
            done(result)

        self._start(self.tr("画像の取得"), work, finished)

    def open_items(self, type_: str, links: Optional[Sequence[EventLink]] = None) -> None:
        links = list(links if links is not None else self.chosen_links(type_))
        if not links:
            return
        if type_ == "image":
            local = [x for x in links if self.is_local(x)]
            problems = self._open_local(local) if local else []
            if problems:
                self.iface.messageBar().pushWarning(TITLE, " / ".join(problems))
            if len(local) < len(links):
                self._fetch_images(links, self._open_images)
            elif len(local) > len(problems):
                self.say(self.tr("画像 {n} 枚を表示").format(n=len(local) - len(problems)))
        elif type_ == "observation":
            self._open_observations(links)
        else:
            self._open_requests(links)

    #: The 「観測記録」 dock (plugin.py sets it): the images of a catalog without image files go there.
    observations: Optional[Callable[[], object]] = None

    def _open_images(self, result) -> None:
        catalog_dock = self.docks().get("SatObsImageCatalog")
        problems: list[str] = []
        opened = 0
        without = []
        for catalog, records in result:
            if not catalog.image_files:
                # Their raw data is in another system: recorded on without their files.
                without += records
                continue
            for record in records:
                try:
                    layers = open_record(record, catalog, self.context.config.path_mappings,
                                         catalog_dock.choose_folder if catalog_dock else (lambda m: None),
                                         catalog_dock.choose_images if catalog_dock else None, problems,
                                         names=self.context.known_names())
                except Cancelled:
                    return
                except PathError as error:
                    problems.append(str(error))
                    continue
                add_record_layers(layers, record)
                overview_queue.request(layers, self.context.config.overviews, self.iface.messageBar())
                opened += len(layers)
        listed = 0
        if without:
            from ..core.catalog.search import listed_image

            images = [i for i in (listed_image(r) for r in without) if i is not None]
            dock = self.observations() if self.observations else None
            if images and dock is not None and dock.list_catalog_images(images) is not None:
                listed = len(images)
            else:
                problems.append(self.tr("画像ファイルなしのカタログの画像 {n} 件を観測記録へ追加できず（画像 ID か記録の保存先なし）").format(n=len(without)))
        if problems:
            self.iface.messageBar().pushWarning(TITLE, " / ".join(problems))
        said = [self.tr("画像 {n} 枚を開いた").format(n=opened) if opened else "",
                self.tr("ファイルなしの画像 {n} 件を観測記録へ").format(n=listed) if listed else ""]
        self.say("、".join(x for x in said if x) or self.status.text())

    def show_in_catalog(self) -> None:
        links = [x for x in self.links_of(self.chosen(), "image") if not self.is_local(x)]
        if not links:
            self.say(self.tr("カタログの画像なし（カタログ外・ファイルなしの画像のみ）"))
            return

        def done(result) -> None:
            dock = self.docks().get("SatObsImageCatalog")
            if dock is None or not result:
                return
            catalog, records = max(result, key=lambda cr: len(cr[1]))
            dock.show_records(catalog, records)
            dock.status.setText(self.tr("イベント「{e}」の画像 {n} 件").format(e=self.chosen().name if self.chosen() else "", n=len(records)))
            dock.show()
            dock.raise_()

        self._fetch_images(links, done)

    def _observation_dock(self):
        return self.docks().get("SatObsObservations")

    def _open_observations(self, links: Sequence[EventLink]) -> None:
        dock = self._observation_dock()
        if dock is None:
            return
        book = dock.book
        found = [book.record(x.item_id) for x in links] if book else []
        found = [o for o in found if o is not None]
        if not found:
            sources = {x.source for x in links}
            self.say(self.tr("いまの保存先に記録なし（保存先: {s}）。観測記録ドックで保存先を切り替えて再実行").format(s="、".join(sorted(sources)) or "?"))
            return
        dock.show()
        dock.raise_()
        dock.open_record(found[0])
        if len(found) < len(links):
            self.say(self.tr("{n} 件はいまの保存先になし（別の保存先か削除済み）").format(n=len(links) - len(found)))

    def _open_requests(self, links: Sequence[EventLink]) -> None:
        dock = self.docks().get("SatObsRequests")
        if dock is None:
            return
        dock.show()
        dock.raise_()
        ids = [x.item_id for x in links]

        def select() -> None:
            dock.select(ids)
            missing = len(ids) - len(dock.selected())
            if missing:
                self.say(self.tr("{n} 件は台帳の一覧になし（別の台帳か削除済み、絞り込み中）").format(n=missing))

        if not getattr(dock, "_loaded", True) and hasattr(dock, "reload"):
            dock.reload(select)
        else:
            select()

    # ---- the map -------------------------------------------------------------------------------

    def _fit(self, box: QgsRectangle) -> None:
        crs = self.canvas.mapSettings().destinationCrs()
        pad = max(box.width(), box.height()) * 0.1 + 0.001
        self.canvas.setExtent(from_wgs84(crs).transformBoundingBox(box.buffered(pad)))
        self.canvas.refresh()

    def zoom(self) -> None:
        e = self.chosen()
        g = footprint({"rings": e.rings}) if e and e.rings else None
        if g is not None:
            self._fit(g.boundingBox())

    def show_on_map(self) -> None:
        """The event's area, its images' footprints and its records' points as layers in a group 「イベント: 名前」."""
        e = self.chosen()
        if e is None:
            return
        images = self.links_of(e, "image")
        if images:
            self._fetch_images(images, lambda result: self._add_layers(e, result))
        else:
            self._add_layers(e, [])

    def _add_layers(self, e: Event, image_result) -> list:
        project = QgsProject.instance()
        root = project.layerTreeRoot()
        name = self.tr("イベント: {n}").format(n=e.name)
        old = root.findGroup(name)
        if old is not None:
            for layer_id in old.findLayerIds():
                project.removeMapLayer(layer_id)
            root.removeChildNode(old)
        group = root.insertGroup(0, name)
        layers = []
        box: Optional[QgsRectangle] = None

        def grow(b: QgsRectangle) -> None:
            nonlocal box
            if box is None:
                box = QgsRectangle(b)
            else:
                box.combineExtentWith(b)

        def add(layer: QgsVectorLayer) -> None:
            layer.updateExtents()
            project.addMapLayer(layer, False)
            group.addLayer(layer)
            layers.append(layer)

        g = footprint({"rings": e.rings}) if e.rings else None
        if g is not None:
            area = QgsVectorLayer("MultiPolygon?crs=EPSG:4326", self.tr("範囲"), "memory")
            area.dataProvider().addAttributes([make_field("name", "string"), make_field("status", "string")])
            area.updateFields()
            f = QgsFeature(area.fields())
            f.setGeometry(g)
            f.setAttributes([e.name, e.status])
            area.dataProvider().addFeatures([f])
            area.renderer().symbol().setOpacity(0.35)
            add(area)
            grow(g.boundingBox())
        footprints = [(r.id, r.sensor, footprint(r.geometry)) for _c, rs in image_result for r in rs]
        known = self._known_images()
        for x in self.links_of(e, "image"):
            image = known.get(x.item_id) if self.is_local(x) else None
            if image is not None and image.box:
                w, s_, east, n = image.box
                fg = QgsGeometry.fromRect(QgsRectangle(w, s_, east, n))
                fg.convertToMultiType()
                footprints.append((x.item_id, self.tr("カタログ外"), fg))
        footprints = [(i, sensor, fg) for i, sensor, fg in footprints if fg is not None]
        if footprints:
            layer = QgsVectorLayer("MultiPolygon?crs=EPSG:4326", self.tr("画像の範囲"), "memory")
            layer.dataProvider().addAttributes([make_field("image_id", "string"), make_field("sensor", "string")])
            layer.updateFields()
            feats = []
            for image_id, sensor, fg in footprints:
                f = QgsFeature(layer.fields())
                f.setGeometry(fg)
                f.setAttributes([image_id, sensor])
                feats.append(f)
                grow(fg.boundingBox())
            layer.dataProvider().addFeatures(feats)
            symbol = layer.renderer().symbol()
            symbol.setOpacity(0.6)
            symbol.symbolLayer(0).setBrushStyle(Qt.BrushStyle.NoBrush)
            add(layer)
        dock = self._observation_dock()
        book = dock.book if dock else None
        records = [book.record(x.item_id) for x in self.links_of(e, "observation")] if book else []
        records = [o for o in records if o is not None]
        if records:
            layer = QgsVectorLayer("Point?crs=EPSG:4326", self.tr("観測記録"), "memory")
            layer.dataProvider().addAttributes([make_field(n, "string") for n in ("obs_id", "object", "kind", "activity", "image")])
            layer.updateFields()
            feats = []
            for o in records:
                f = QgsFeature(layer.fields())
                f.setGeometry(QgsGeometry.fromPointXY(QgsPointXY(o.lon, o.lat)))
                f.setAttributes([o.id, o.object_name, o.kind, o.activity, o.image_name or o.image_id])
                feats.append(f)
                grow(QgsRectangle(o.lon, o.lat, o.lon, o.lat))
            layer.dataProvider().addFeatures(feats)
            add(layer)
        missing = len(self.links_of(e, "observation")) - len(records)
        if box is not None:
            self._fit(box)
        text = self.tr("「{n}」をレイヤー {k} 枚で表示").format(n=e.name, k=len(layers))
        if missing:
            text += self.tr("（観測記録 {m} 件はいまの保存先になく表示なし）").format(m=missing)
        self.say(text)
        return layers

    def search_images(self) -> None:
        """The image catalog searched over the event's area (its outline, or its extent) and period."""
        e = self.chosen()
        dock = self.docks().get("SatObsImageCatalog")
        if e is None or dock is None:
            return
        day = lambda ms: time.strftime("%Y-%m-%d", time.localtime(ms / 1000)) if ms is not None else ""  # noqa: E731
        dock.acquired_from.setText(day(e.start))
        dock.acquired_to.setText(day(e.end))
        if e.rings:
            ring = e.rings[0] if len(e.rings) == 1 else None
            if ring is None:
                g = footprint({"rings": e.rings})
                ring = _box_ring(g.boundingBox()) if g is not None else None
            dock.set_drawn([tuple(p[:2]) for p in ring] if ring else None)
        else:
            # No area: anywhere (not whatever area the catalog had before).
            dock.area_box.setCurrentIndex(dock.area_box.findData("none"))
        dock.show()
        dock.raise_()
        dock.search()

    # ---- export --------------------------------------------------------------------------------

    def export(self, path: Optional[str] = None) -> Optional[str]:
        e = self.chosen()
        if e is None:
            return None
        if path is None:
            safe = "".join(ch if ch not in '\\/:*?"<>|' else "_" for ch in e.name)
            path, _f = QFileDialog.getSaveFileName(self, self.tr("イベントの項目を保存"), f"{safe}.csv", self.tr("CSV (*.csv)"))
            if not path:
                return None
        try:
            with open(path, "w", encoding="utf-8-sig", newline="") as f:
                f.write(export_csv(e, self.links))
        except OSError as error:
            self.say(self.tr("保存に失敗: {m}").format(m=error.strerror or error))
            return None
        self.say(self.tr("{n} 件を保存: {p}").format(n=len(self.links_of(e)), p=path))
        return path

    def copy_summary(self) -> str:
        e = self.chosen()
        if e is None:
            return ""
        text = summary_text(e, self.links)
        QApplication.clipboard().setText(text)
        self.say(self.tr("概要をコピー"))
        return text

    def close_down(self) -> None:
        for task in list(self._tasks):
            task.cancel()
