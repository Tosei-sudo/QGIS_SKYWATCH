"""
「自分の記録」 (tsudo, 2026-11-18: 過去の自分がどの画像のどの観測結果を作ったのかが
わかりにくいから、確認や修正が容易になる方法を): the records one made, across
the images, in a window of its own (観測記録 ▸ 自分の記録…, or the dock's
button), also with no image chosen.

- 「記録ごと」: one row per record (撮像日時・画像・モノ・種別・活動・備考・
  記録日時・記録者・更新者), the newest made first; 「画像ごと」: one row per
  image (its records, per 大分類, when recorded on last): the images one
  worked on.
- Filters: 記録者 (oneself by default: the 「記録者」 name or the user name,
  core/obs/mine.py; everyone; or one name), 期間 (30 日 by default; 今日・
  7 日・90 日・期間指定), 大分類, a text, 「ほかの人が変えた記録のみ」 and
  「削除済みも表示」.
- Marks: 通報済み and 送信待ち as in the dock; a record of one's own someone
  else changed afterwards has its own colour (and that person under 更新者).
- A click only chooses; a double click (or 「開いて修正」) opens the record's
  image (opened first when not open: 開いた画像の履歴, else the image catalog,
  else listed without its file) and the record in the form. Chosen records
  can be changed at once (一括編集・削除・通報・イベント) without opening
  their images, and their images listed in the catalog (「画像カタログで表示」).

The records are those the dock has read; before it read them (an Esri
store), only one's own of the period are asked of the layer (load_made),
so the list does not wait for every record.
"""
from __future__ import annotations

import time
from typing import Any, Callable, Optional

from qgis.PyQt.QtCore import QDate, QDateTime, QItemSelection, QItemSelectionModel, QTime, QTimer, Qt
from qgis.PyQt.QtGui import QColor, QKeySequence
from qgis.PyQt.QtWidgets import (
    QAbstractItemView, QCheckBox, QComboBox, QDateEdit, QDialog, QHBoxLayout, QHeaderView, QLabel, QLineEdit, QPushButton, QShortcut,
    QTabBar, QTableWidget, QTableWidgetItem, QVBoxLayout, QWidget,
)

from ..core.obs.api import local_time
from ..core.obs.api import (
    DEFAULT_DAYS, ImageRow, MineFilter, by_image, changed_by_other, is_mine, observer_names, root_of,
    mine_roots_text as roots_text, mine_select as select, since_of,
)
from ..core.obs.api import Observation, state_text
from ..core.obs.api import RecordQuery
from .docks import plain_label
from .fit import fit_text
from .list_views import column_menus

#: The rows' colours (the dock's for 通報済み and 送信待ち).
REPORTED_ROW = "#fde68a"
WAITING_ROW = "#dbeafe"
OTHERS_ROW = "#ede9fe"
#: The periods offered (days; 0: today; None: 期間指定), in this order, 30 日 chosen first.
PERIOD_CHOICES = [(0, "今日"), (7, "7 日"), (30, "30 日"), (90, "90 日"), (None, "期間指定…")]
#: 記録者: oneself, everyone (else a name).
ME, EVERYONE = "\x00me", "\x00all"


def _day_ms(day: QDate) -> int:
    return int(QDateTime(day, QTime(0, 0)).toMSecsSinceEpoch())


class MyRecordsDialog(QDialog):
    def __init__(self, dock, parent: Optional[QWidget] = None):
        super().__init__(parent)
        fit_text(self)
        self.dock = dock
        self.setWindowTitle(self.tr("自分の記録"))
        self.setWindowFlag(Qt.WindowType.Window, True)
        self.setModal(False)
        self.resize(980, 560)
        #: The records read (the dock's book, or one's own of the period asked of the layer), and from where.
        self.records: list[Observation] = []
        self.source = ""
        #: What load_made was asked last ((names, since)) and the book it was for.
        self._made_key: Optional[tuple] = None
        self._book = None
        self._asked = 0
        #: What each row stands for (`o:` a record, `i:` an image) and its records.
        self._row_keys: list[str] = []
        self._row_records: list[list[Observation]] = []
        self._image_rows: list[ImageRow] = []
        self._later = QTimer(self)
        self._later.setSingleShot(True)
        self._later.setInterval(200)
        self._later.timeout.connect(self._dock_changed_now)
        self._build()
        dock.changed.connect(lambda: self._later.start() if self.isVisible() else None)

    # ---- building ----------------------------------------------------------------------------------

    def _build(self) -> None:
        tr = self.tr
        layout = QVBoxLayout(self)
        row = QHBoxLayout()
        row.addWidget(QLabel(tr("記録者")))
        self.who = QComboBox()
        self.who.setToolTip(tr("自分: 「記録者」の名前かユーザー名で記録したもの。名前を変えたことがあれば、前の名前も一覧から選べる"))
        self.who.setSizeAdjustPolicy(QComboBox.SizeAdjustPolicy.AdjustToContents)
        self.who.activated.connect(lambda _i: self.reload())
        row.addWidget(self.who)
        row.addSpacing(8)
        row.addWidget(QLabel(tr("期間")))
        self.period = QComboBox()
        self.period.setToolTip(tr("記録日時（記録を作った日時）の期間"))
        for days, text in PERIOD_CHOICES:
            self.period.addItem(tr(text), days)
        self.period.setCurrentIndex(next(i for i, (d, _t) in enumerate(PERIOD_CHOICES) if d == DEFAULT_DAYS))
        self.period.activated.connect(lambda _i: self._period_chosen())
        row.addWidget(self.period)
        today = QDate.currentDate()
        self.since_edit = QDateEdit(today.addDays(-DEFAULT_DAYS))
        self.until_edit = QDateEdit(today)
        for edit in (self.since_edit, self.until_edit):
            edit.setCalendarPopup(True)
            edit.setDisplayFormat("yyyy-MM-dd")
            edit.dateChanged.connect(lambda _d: self.reload())
            edit.hide()
        self.since_edit.setToolTip(tr("この日から"))
        self.until_edit.setToolTip(tr("この日まで（この日を含む）"))
        self.range_dash = QLabel("〜")
        self.range_dash.hide()
        row.addWidget(self.since_edit)
        row.addWidget(self.range_dash)
        row.addWidget(self.until_edit)
        row.addSpacing(8)
        row.addWidget(QLabel(tr("大分類")))
        self.root = QComboBox()
        self.root.setSizeAdjustPolicy(QComboBox.SizeAdjustPolicy.AdjustToContents)
        self.root.activated.connect(lambda _i: self.render())
        row.addWidget(self.root)
        self.text = QLineEdit()
        self.text.setClearButtonEnabled(True)
        self.text.setPlaceholderText(tr("モノ・種別・活動・備考・画像で絞り込み"))
        self.text.textChanged.connect(lambda _t: self.render())
        row.addWidget(self.text, 1)
        layout.addLayout(row)

        row = QHBoxLayout()
        self.modes = QTabBar()
        self.modes.addTab(tr("記録ごと"))
        self.modes.addTab(tr("画像ごと"))
        self.modes.setTabToolTip(0, tr("1 行 1 記録（新しく記録した順）"))
        self.modes.setTabToolTip(1, tr("1 行 1 画像（自分の記録の件数・最後に記録した日時）。作業した画像の履歴"))
        self.modes.currentChanged.connect(lambda _i: self.render())
        row.addWidget(self.modes)
        self.others_only = QCheckBox(tr("ほかの人が変えた記録のみ"))
        self.others_only.setToolTip(tr("記録したあとで、ほかの人が更新した記録だけ"))
        self.others_only.toggled.connect(lambda _on: self.render())
        row.addWidget(self.others_only)
        self.deleted = QCheckBox(tr("削除済みも表示"))
        self.deleted.toggled.connect(lambda _on: self.render())
        row.addWidget(self.deleted)
        row.addStretch(1)
        self.reload_button = QPushButton(tr("読み直す"))
        self.reload_button.setAutoDefault(False)
        self.reload_button.setToolTip(tr("保存先から読み直す"))
        self.reload_button.clicked.connect(lambda: self.reload(force=True))
        row.addWidget(self.reload_button)
        layout.addLayout(row)

        row = QHBoxLayout()
        self.summary = plain_label(QLabel())
        font = self.summary.font()
        font.setBold(True)
        self.summary.setFont(font)
        row.addWidget(self.summary, 1)
        for color, text in ((REPORTED_ROW, tr("通報済み")), (OTHERS_ROW, tr("ほかの人が変更")), (WAITING_ROW, tr("送信待ち"))):
            chip = QLabel(text)
            chip.setStyleSheet(f"background: {color}; color: #1f2937; padding: 1px 6px; border-radius: 3px;")
            row.addWidget(chip)
        layout.addLayout(row)

        self.table = QTableWidget()
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.verticalHeader().hide()
        self.table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Interactive)
        self.table.horizontalHeader().setStretchLastSection(True)
        self.table.setToolTip(tr("クリックで選択、ダブルクリック（Enter）で画像を開いて記録を編集。Ctrl・Shift + クリックで複数"))
        self.table.cellDoubleClicked.connect(lambda r, _c: self.open_row(r))
        self.table.itemSelectionChanged.connect(self._chosen_changed)
        enter = QShortcut(QKeySequence(Qt.Key.Key_Return), self.table)
        enter.setContext(Qt.ShortcutContext.WidgetShortcut)
        enter.activated.connect(lambda: self.open_row(self.table.currentRow()))
        layout.addWidget(self.table, 1)
        self.columns = column_menus(lambda: f"my-records-{self.modes.currentIndex()}", self.table, self)

        row = QHBoxLayout()
        self.open_button = self._button(tr("開いて修正"), tr("記録の画像を開き（開いていなければ前回のファイルか画像カタログから）、記録を入力フォームで開く"),
                                        lambda: self.open_row(self.table.currentRow()))
        self.image_button = self._button(tr("画像を開く"), tr("選んだ画像を開く（開いていなければ前回のファイルか画像カタログから）"),
                                         lambda: self.open_row(self.table.currentRow()))
        self.bulk_button = self._button(tr("一括編集…"), tr("選んだ記録の種別・活動・確度をまとめて変更（画像は開かない）"), lambda: self.bulk_edit())
        self.remove_button = self._button(tr("選んだ記録を削除…"), tr("選んだ記録をまとめて削除（確認あり）"), lambda: self.remove())
        self.restore_button = self._button(tr("復元"), tr("選んだ削除済みの記録を元に戻す"), lambda: self.restore())
        self.alert_button = self._button(tr("通報に追加"), tr("選んだ記録を通報文に追加"), lambda: self.add_to_alerts())
        self.event_button = self._button(tr("イベントに追加"), tr("選んだ記録を対応中のイベントに追加"), lambda: self.add_to_event())
        self.catalog_button = self._button(tr("画像カタログで表示"), tr("選んだ記録の画像だけを画像カタログの一覧に表示（地図で範囲を見比べてから開ける）"),
                                           lambda: self.show_in_catalog())
        for b in (self.open_button, self.image_button, self.bulk_button, self.remove_button, self.restore_button, self.alert_button,
                  self.event_button, self.catalog_button):
            row.addWidget(b)
        row.addStretch(1)
        close = self._button(tr("閉じる"), "", self.close)
        row.addWidget(close)
        layout.addLayout(row)
        self.status = plain_label(QLabel())
        self.status.setWordWrap(True)
        self.status.setStyleSheet("color: #555555;")
        layout.addWidget(self.status)
        self._fill_who()
        self._chosen_changed()

    def _button(self, text: str, tip: str, slot: Callable) -> QPushButton:
        b = QPushButton(text)
        b.setToolTip(tip)
        b.setAutoDefault(False)
        b.clicked.connect(lambda _on=False: slot())
        return b

    def _say(self, text: str) -> None:
        self.status.setText(text)

    # ---- what is asked -----------------------------------------------------------------------------

    def me(self) -> list[str]:
        return self.dock.my_names()

    def names(self) -> list[str]:
        """The 記録者 shown: oneself's names, a name, or [] for everyone."""
        data = self.who.currentData()
        if data == EVERYONE:
            return []
        if data == ME or data is None:
            return self.me()
        return [str(data)]

    def period_range(self) -> tuple[Optional[int], Optional[int]]:
        """記録日時 from and to (ms; to excluded, None: now)."""
        days = self.period.currentData()
        if days is None:
            since, until = self.since_edit.date(), self.until_edit.date()
            if until < since:
                since, until = until, since
            return _day_ms(since), _day_ms(until.addDays(1))
        return since_of(int(days), time.time() * 1000), None

    def period_text(self) -> str:
        days = self.period.currentData()
        if days is None:
            return f"{self.since_edit.date().toString('yyyy-MM-dd')}〜{self.until_edit.date().toString('yyyy-MM-dd')}"
        return self.tr("今日") if days == 0 else self.tr("直近 {n} 日").format(n=days)

    def filter(self) -> MineFilter:
        since, until = self.period_range()
        return MineFilter(names=self.names(), since=since, until=until, root=self.root.currentData() or "",
                          text=self.text.text(), changed_by_others=self.others_only.isChecked(), deleted=self.deleted.isChecked())

    def _fill_who(self) -> None:
        """自分, すべての人, then the other names of the records read (an older name of one's own among them)."""
        was = self.who.currentData()
        me = self.me()
        self.who.blockSignals(True)
        self.who.clear()
        self.who.addItem(self.tr("自分（{names}）").format(names="・".join(me)) if me else self.tr("自分"), ME)
        self.who.addItem(self.tr("すべての人"), EVERYONE)
        others = [n for n in observer_names(self.records) if not is_mine(n, me)]
        if others:
            self.who.insertSeparator(self.who.count())
            for n in others:
                self.who.addItem(n, n)
        if isinstance(was, str) and was not in (ME, EVERYONE) and self.who.findData(was) < 0:
            self.who.addItem(was, was)
        i = self.who.findData(was) if was is not None else 0
        self.who.setCurrentIndex(i if i >= 0 else 0)
        self.who.blockSignals(False)

    def _fill_roots(self) -> None:
        was = self.root.currentData()
        roots = sorted({root_of(o.kind) for o in self.records if root_of(o.kind)})
        self.root.blockSignals(True)
        self.root.clear()
        self.root.addItem(self.tr("すべて"), "")
        for r in roots:
            self.root.addItem(r, r)
        i = self.root.findData(was) if was else 0
        self.root.setCurrentIndex(i if i >= 0 else 0)
        self.root.blockSignals(False)

    def _period_chosen(self) -> None:
        custom = self.period.currentData() is None
        for w in (self.since_edit, self.range_dash, self.until_edit):
            w.setVisible(custom)
        self.reload()

    # ---- reading -----------------------------------------------------------------------------------

    def show_again(self) -> None:
        """Shown (read again when it was closed), in front."""
        if not self.isVisible():
            self._fill_who()
            self.reload()
            self.dock.refresh_reported()
        self.show()
        self.raise_()
        self.activateWindow()

    def reload(self, force: bool = False) -> None:
        """
        The records again: a local store's, all read (at once); a shared
        store's made by the people chosen (anyone: everyone's) in the period,
        asked of the layer in a task (never every record: everyone's of all
        time is not read).
        """
        dock = self.dock
        book = dock.book
        if book is not self._book:
            self._made_key = None
        self._book = book
        if book is None:
            self._take([], "", self.tr("保存先が未設定（管理者に連絡: observationStores）［E703］"))
            return
        if not book.scoped:
            if dock.records_ready(lambda: self.reload()):
                self._take(list(book.records) if book.loaded else [], "book")
            else:
                self._say(self.tr("記録を取得中…"))
            return
        names = self.names()
        since, _until = self.period_range()
        query = RecordQuery(observers=tuple(names), start=since, basis="created")
        if query.everything():
            self._made_key = None
            self._take([], "made", self.tr("全員の全期間は取得しない（期間を選択）"))
            return
        key = (tuple(n.casefold() for n in names), since)
        if not force and self.source == "made" and self._made_key == key:
            # The same people and period: the copies kept by the dock (changed there since) shown.
            known = {o.id: o for o in book.records}
            self.records = [known.get(o.id, o) for o in self.records]
            self.render()
            return
        if force and book.loaded:
            dock.refresh(force=True)
        self._asked += 1
        asked = self._asked
        self._say(self.tr("記録を取得中…"))

        def done(made) -> None:
            if asked != self._asked or self.dock.book is not book:
                return
            self._made_key = key
            self._take(list(made or []), "made")

        def failed(message: str) -> None:
            if asked == self._asked:
                self._say(self.tr("記録の取得に失敗: {m}").format(m=message))

        dock.fetch(self.tr("自分の記録の取得"), lambda b: b.fetch(query), done, failed)

    def _take(self, records: list[Observation], source: str, note: str = "") -> None:
        self.records = records
        self.source = source
        self._fill_who()
        self._fill_roots()
        self.render()
        self._say(note)

    def closeEvent(self, event) -> None:
        self._later.stop()
        self._asked += 1  # a read still in its task is not shown
        super().closeEvent(event)

    def _dock_changed_now(self) -> None:
        """The dock changed (a save, a read, another store): the list again, as cheaply as that allows."""
        if not self.isVisible():
            return
        book = self.dock.book
        if book is not self._book or (book is not None and book.loaded):
            self.reload()
        else:
            self.render()

    # ---- the list ----------------------------------------------------------------------------------

    def shown(self) -> list[Observation]:
        return select(self.records, self.filter(), self.me())

    def render(self) -> None:
        tr = self.tr
        keys, current = self._chosen_keys()
        records = self.shown()
        me = self.me()
        reported, waiting = set(self.dock.reported), self.dock.waiting_ids()
        images = by_image(records)
        names = self.names()
        who = tr("自分") if self.who.currentData() in (ME, None) else tr("全員") if not names else names[0]
        self.summary.setText(tr("{who}の記録 {n} 件・画像 {m} 枚（{period}）").format(
            who=who, n=len([o for o in records if o.deleted_at is None]), m=len(images), period=self.period_text()))
        open_ids = {e.id: e for e in self.dock.book.images} if self.dock.book is not None else {}
        self.table.setSortingEnabled(False)
        self.table.clearContents()
        self._row_keys, self._row_records = [], []
        self._image_rows = images
        if self.modes.currentIndex() == 0:
            heads = [tr("撮像日時"), tr("画像"), tr("モノ"), tr("種別"), tr("活動"), tr("備考"), tr("記録日時"), tr("記録者"), tr("更新者")]
            self.table.setColumnCount(len(heads))
            self.table.setHorizontalHeaderLabels(heads)
            self.table.setRowCount(len(records))
            for r, o in enumerate(records):
                others = changed_by_other(o, me)
                cells = [local_time(o.image_time) if o.image_time is not None else "", o.image_name or o.image_id,
                         o.object_name or o.object_id, o.kind, tr(state_text(o)) if not o.present else o.activity, o.note,
                         local_time(o.created_at), o.recorded_by, o.updated_by if o.updated_at > o.created_at else ""]
                tip = tr("更新 {when}（{who}）").format(when=local_time(o.updated_at), who=o.updated_by or "—")
                self._put(r, cells, o.deleted_at is not None, o.id in waiting, o.id in reported, others, tip)
                self._row_keys.append(f"o:{o.id}")
                self._row_records.append([o])
        else:
            heads = [tr("撮像日時"), tr("画像"), tr("記録"), tr("大分類ごと"), tr("最後に記録"), tr("この PC で")]
            self.table.setColumnCount(len(heads))
            self.table.setHorizontalHeaderLabels(heads)
            self.table.setRowCount(len(images))
            for r, i in enumerate(images):
                living = [o for o in i.records if o.deleted_at is None]
                entry = open_ids.get(i.image_id)
                state = "" if entry is None else tr("開いている") if entry.file else tr("ファイルなしで一覧")
                cells = [local_time(i.image_time) if i.image_time is not None else "", i.image_name, tr("{n} 件").format(n=len(living)),
                         roots_text(i.roots), local_time(i.last_made), state]
                others = any(changed_by_other(o, me) for o in living)
                self._put(r, cells, not living, any(o.id in waiting for o in i.records), any(o.id in reported for o in living), others,
                          tr("ダブルクリックで画像を開く"))
                self._row_keys.append(f"i:{i.image_id}")
                self._row_records.append(list(i.records))
        if not self._row_keys:
            self.table.setRowCount(1)
            empty = tr("該当する記録なし。期間・記録者・絞り込みを変更") if self.records or self.source else tr("記録なし")
            item = QTableWidgetItem(empty)
            item.setFlags(Qt.ItemFlag.ItemIsEnabled)
            self.table.setItem(0, 0, item)
            self._row_keys.append("")
            self._row_records.append([])
        self.table.resizeColumnsToContents()
        # The last column fills the list again (Qt leaves it as it was when the columns change).
        header = self.table.horizontalHeader()
        header.setStretchLastSection(False)
        header.setStretchLastSection(True)
        self._choose(keys, current)
        self._chosen_changed()

    def _put(self, r: int, cells: list, gone: bool, waiting: bool, reported: bool, others: bool, tip: str) -> None:
        color = WAITING_ROW if waiting else REPORTED_ROW if reported else OTHERS_ROW if others else None
        why = self.tr("送信待ち（まだ送っていない）") if waiting else self.tr("通報済み") if reported else \
            self.tr("ほかの人が記録のあとで変更") if others else ""
        for c, text in enumerate(cells):
            item = QTableWidgetItem("" if text is None else str(text))
            if gone:
                font = item.font()
                font.setStrikeOut(True)
                item.setFont(font)
                item.setForeground(QColor("#888888"))
            elif color:
                item.setBackground(QColor(color))
                item.setForeground(QColor("#1f2937"))
            item.setToolTip("\n".join(t for t in (why, tip) if t))
            self.table.setItem(r, c, item)

    def _chosen_keys(self) -> tuple[set[str], str]:
        rows = {i.row() for i in self.table.selectionModel().selectedRows()} if self.table.selectionModel() else set()
        keys = self._row_keys
        current = self.table.currentRow()
        return ({keys[r] for r in rows if 0 <= r < len(keys) and keys[r]}, keys[current] if 0 <= current < len(keys) else "")

    def _choose(self, keys: set[str], current: str) -> None:
        """The rows chosen before the list was shown anew chosen again (a save, a read)."""
        model, last, sel = self.table.model(), self.table.columnCount() - 1, self.table.selectionModel()
        chosen = QItemSelection()
        for r, key in enumerate(self._row_keys):
            if key and key in keys:
                chosen.select(model.index(r, 0), model.index(r, last))
        sel.clearSelection()
        if not chosen.isEmpty():
            sel.select(chosen, QItemSelectionModel.SelectionFlag.Select | QItemSelectionModel.SelectionFlag.Rows)
        if current and current in self._row_keys:
            sel.setCurrentIndex(model.index(self._row_keys.index(current), 0), QItemSelectionModel.SelectionFlag.NoUpdate)

    def chosen_records(self, living: Optional[bool] = True) -> list[Observation]:
        """The records of the rows chosen (an image's row: its records shown), each once; `living` None: also the deleted."""
        rows = sorted({i.row() for i in self.table.selectionModel().selectedRows()})
        out: dict[str, Observation] = {}
        for r in rows:
            for o in self._row_records[r] if 0 <= r < len(self._row_records) else []:
                if living is None or (o.deleted_at is None) == living:
                    out.setdefault(o.id, o)
        return list(out.values())

    def _chosen_changed(self) -> None:
        records = self.chosen_records()
        gone = self.chosen_records(living=False)
        by_record = self.modes.currentIndex() == 0
        rows = {i.row() for i in self.table.selectionModel().selectedRows()}
        one = len(rows) == 1 and any(self._row_keys[r] for r in rows if r < len(self._row_keys))
        self.open_button.setVisible(by_record)
        self.image_button.setVisible(not by_record)
        self.open_button.setEnabled(one and bool(records))
        self.image_button.setEnabled(one)
        for b in (self.bulk_button, self.remove_button, self.alert_button, self.event_button):
            b.setEnabled(bool(records))
        self.restore_button.setVisible(bool(gone))
        self.catalog_button.setEnabled(bool(records or gone))
        self.alert_button.setVisible(self.dock.context.config.alert_reports is not None)
        self.event_button.setVisible(self.dock.context.config.events is not None)

    # ---- doing -------------------------------------------------------------------------------------

    def _with_book(self, ids: list[str], act: Callable[[list[Observation]], Any], what: str) -> None:
        """`act` on the dock's own records of `ids`, once it has read them (the list may hold an older copy)."""
        dock = self.dock

        def go(_read=None) -> None:
            book = dock.book
            if book is None:
                return
            wanted = set(ids)
            found = [o for o in book.records if o.id in wanted]
            if not found:
                self._say(self.tr("記録が見つからない（削除済みか、保存先が変更済み）"))
                return
            act(found)

        book = dock.book
        if book is not None and book.scoped:
            # As they are on the server now (the list may hold an older copy).
            self._say(self.tr("記録を取得中…（取得後に{what}）").format(what=what))
            dock.fetch(self.tr("記録の取得"), lambda b: b.fetch(RecordQuery(ids=tuple(ids))), go,
                       lambda m: self._say(self.tr("記録の取得に失敗: {m}").format(m=m)))
        elif dock.records_ready(go):
            go()
        else:
            self._say(self.tr("記録を取得中…（取得後に{what}）").format(what=what))

    def open_row(self, row: int) -> None:
        """A row's record opened in the form on its image, or (「画像ごと」) its image opened."""
        if not 0 <= row < len(self._row_keys) or not self._row_keys[row]:
            return
        key = self._row_keys[row]
        if key.startswith("i:"):
            image = next((i for i in self._image_rows if i.image_id == key[2:]), None)
            if image is not None:
                self.open_image(image)
            return
        o = self._row_records[row][0]
        if o.deleted_at is not None:
            self._say(self.tr("削除済みの記録（「復元」で復元できる）"))
            return
        self._with_book([o.id], lambda found: self._open_record(found[0]), self.tr("表示"))

    def _open_record(self, o: Observation) -> None:
        dock = self.dock
        dock.show()
        dock.raise_()
        dock.open_record(o)
        self._say(self.tr("{image} の {thing} を表示").format(
            image=o.image_name or o.image_id, thing=o.object_name or o.object_id))

    def open_image(self, image: ImageRow) -> None:
        dock = self.dock
        dock.show()
        dock.raise_()
        if dock.open_image(image.image_id, image.image_name, image.image_time):
            self._say(self.tr("{image} へ移動").format(image=image.image_name))
        else:
            self._say(self.tr("{image} を表示中（前回のファイルか画像カタログから）").format(image=image.image_name))

    def bulk_edit(self) -> None:
        ids = [o.id for o in self.chosen_records()]
        if ids:
            self._with_book(ids, self.dock.bulk_edit, self.tr("一括編集"))

    def remove(self) -> None:
        ids = [o.id for o in self.chosen_records()]
        if ids:
            self._with_book(ids, lambda found: self.dock.remove_selected(found, ask=True), self.tr("削除"))

    def restore(self) -> None:
        ids = [o.id for o in self.chosen_records(living=False)]
        if ids:
            self._with_book(ids, self.dock.restore, self.tr("復元"))

    def add_to_alerts(self) -> None:
        ids = [o.id for o in self.chosen_records()]
        if ids:
            self._with_book(ids, self.dock.add_to_alerts, self.tr("通報に追加"))

    def add_to_event(self):
        records = self.chosen_records()
        events = self.dock.event_linker() if getattr(self.dock, "event_linker", None) else None
        if not records or events is None:
            return None
        return events.offer(self.event_button, lambda: self.dock.event_items(records))

    def show_in_catalog(self) -> None:
        """「画像カタログで表示」: the images of the chosen records in the catalog's list."""
        records = self.chosen_records(living=None)
        ids = list(dict.fromkeys(o.image_id for o in records if o.image_id))
        catalog = self.dock.catalog() if self.dock.catalog is not None else None
        if not ids or catalog is None:
            self._say(self.tr("画像カタログは使えない") if ids else "")
            return
        catalog.show()
        catalog.raise_()
        catalog.show_image_ids(ids, self.tr("自分の記録の画像"))
        self._say(self.tr("画像 {n} 枚を画像カタログで検索中…").format(n=len(ids)))
