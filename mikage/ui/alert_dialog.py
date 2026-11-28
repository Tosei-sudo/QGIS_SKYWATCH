"""
「通報」: the notable observation records as one line each (core/obs/alerts.py).

Tab 「通報文の作成」: a format (the settings file's default first), the
period (登録日時 or 撮像日時), then 「抽出」 reads the rules, the geofences
and what was already reported from Esri (in a task) and lists a line per
record, highest importance score first (the score in the first column,
its parts in the tooltip): untick one to leave it out, 「選択を一覧から削除」 to
take rows off the list, edit a line by hand. 「通報」 adds the
lines to the Esri log table, then copies the text (header, lines, footer)
and writes the file, as the format says; 「コピーのみ」 copies without
recording anything.

Tab 「通報記録」: the log table's rows of a period (and a format): copy
them, go to their record, or delete them so their records can be reported again.
"""
from __future__ import annotations

from typing import Callable, Optional

from qgis.PyQt.QtCore import QDateTime, Qt
from qgis.PyQt.QtWidgets import (
    QAbstractItemView, QApplication, QCheckBox, QComboBox, QDateTimeEdit, QDialog, QFormLayout, QHBoxLayout, QHeaderView, QLabel,
    QPlainTextEdit, QPushButton, QTableWidget, QTableWidgetItem, QTabWidget, QVBoxLayout, QWidget,
)

from ..core import audit, edit_stamp, errors
from ..core.esri import EsriError
from ..core.obs.api import (
    OBJECT_LAYERS, AlertItem, AlertSettings, LogRow, Moment, build_lines, history_query, layer_key_problems, load_geofences,
    load_rules, manual_items, name_places, object_keys, report_text, reported_ids, score_note, alert_select as select,
    uses_source, alert_write_file as write_file,
)
from ..core.obs.api import living
from ..core.obs.api import areas_at, points_of
from ..core.obs.api import AlertLogRepository, RecordQuery
from ..core.time_zone import format_time
from ..qgis_io.tasks import run_task
from .docks import ask_no, plain_label, plain_tip
from .fit import fit_text
from .list_views import column_menus
from .text_assist_ui import NoteTranslator

DAY_MS = 86_400_000


def layer_fields(client, s: AlertSettings, tokens: dict) -> dict[str, list[str]]:
    """The attribute names of the layers the formats' `country.code`… read (those that cannot be read left out)."""
    out = {}
    for name in dict.fromkeys(OBJECT_LAYERS[what][0] for _w, _s, what, _k in object_keys(s, fixed=True) if what in OBJECT_LAYERS):
        source = getattr(s, name)
        if source is None:
            continue
        try:
            out[name] = list(client.layer(source.url, tokens.get(name), refresh=True).field_types)
        except EsriError:
            pass
    return out


def _ms(edit: QDateTimeEdit) -> int:
    return int(edit.dateTime().toMSecsSinceEpoch())


def _time_edit(ms: int) -> QDateTimeEdit:
    e = QDateTimeEdit(QDateTime.fromMSecsSinceEpoch(int(ms)))
    e.setCalendarPopup(True)
    e.setDisplayFormat("yyyy-MM-dd HH:mm")
    return e


class AlertDialog(QDialog):
    """「通報」 (kept by the observation dock, one at a time)."""

    def __init__(self, dock, parent=None):
        super().__init__(parent)
        fit_text(self)
        self.dock = dock
        self.setWindowTitle(self.tr("通報"))
        self.resize(900, 600)
        self.items: list[AlertItem] = []
        self.log_rows: list[LogRow] = []
        #: The rows shown: (record id, the line as made) and the format they were made in.
        self._shown_lines: list[tuple[str, str]] = []
        self._shown_format = ""
        self._filling = False
        layout = QVBoxLayout(self)
        self.note = plain_label(QLabel())
        self.note.setWordWrap(True)
        layout.addWidget(self.note)
        self.tabs = QTabWidget()
        layout.addWidget(self.tabs, 1)

        # 通報文の作成.
        make = QWidget()
        m = QVBoxLayout(make)
        form = QFormLayout()
        self.format_select = QComboBox()
        self.format_select.currentIndexChanged.connect(lambda _i: self._render_lines())
        form.addRow(self.tr("フォーマット"), self.format_select)
        now = QDateTime.currentMSecsSinceEpoch()
        self.by_select = QComboBox()
        self.by_select.addItem(self.tr("登録日時"), "created")
        self.by_select.addItem(self.tr("撮像日時"), "imageTime")
        self.start_edit, self.end_edit = _time_edit(now - DAY_MS), _time_edit(now)
        period = QHBoxLayout()
        period.addWidget(self.by_select)
        period.addWidget(self.start_edit, 1)
        period.addWidget(QLabel("〜"))
        period.addWidget(self.end_edit, 1)
        form.addRow(self.tr("期間"), period)
        self.again_check = QCheckBox(self.tr("通報済みも含める"))
        self.again_check.setToolTip(self.tr("通報テーブルにすでにある記録も抽出（再通報）"))
        form.addRow(self.again_check)
        m.addLayout(form)
        row = QHBoxLayout()
        self.pick_button = QPushButton(self.tr("抽出"))
        self.pick_button.setToolTip(self.tr("通報の条件と通報済みの記録を読み、条件に合う記録を抽出"))
        self.pick_button.clicked.connect(self.pick)
        row.addWidget(self.pick_button)
        # 英訳 (`textAssist.tasks.translate`): the 備考 in English for a format that writes `note.en` (tsudo 2026-11-19).
        self.translator = NoteTranslator(dock.context.text_assist, self._say, self)
        self.translate_button = QPushButton(self.tr("備考を英訳"))
        self.translate_button.setToolTip(self.tr("一覧の記録の備考を英語に翻訳（この PC の生成 AI）。訳は行で確認してから送信"))
        self.translate_button.clicked.connect(lambda _on=False: self.translate_notes())
        row.addWidget(self.translate_button)
        row.addStretch(1)
        self.drop_button = QPushButton(self.tr("選択を一覧から削除"))
        self.drop_button.setToolTip(self.tr("選んだ行を送らずに一覧から除外（条件に合う記録は次の「抽出」でまた表示）"))
        self.drop_button.clicked.connect(lambda _on=False: self.drop_rows())
        row.addWidget(self.drop_button)
        m.addLayout(row)
        self.lines_table = QTableWidget(0, 2)
        self.lines_table.setHorizontalHeaderLabels([self.tr("通報・重要度"), self.tr("行（編集可）")])
        self.lines_table.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)
        self.lines_table.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        self.lines_table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.lines_table.itemChanged.connect(lambda _item: self._render_text())
        self.lines_table.itemSelectionChanged.connect(self._update_drop)
        self.lines_table.cellDoubleClicked.connect(lambda r, _c: self._go(self.items[r].record.id) if r < len(self.items) else None)
        m.addWidget(self.lines_table, 2)
        self.preview = QPlainTextEdit()
        self.preview.setReadOnly(True)
        self.preview.setPlaceholderText(self.tr("通報文（ヘッダー・行・フッター）"))
        m.addWidget(self.preview, 1)
        row = QHBoxLayout()
        # Named for what it does with the format chosen (tsudo 2026-11-28, 点検 14: 「通報」 read as sending): `_name_send`.
        self.send_button = QPushButton(self.tr("記録してコピー"))
        self.send_button.clicked.connect(lambda: self.send(record=True))
        self.copy_button = QPushButton(self.tr("記録せずにコピー"))
        self.copy_button.setToolTip(self.tr("通報テーブルに記録せず、クリップボードへコピー"))
        self.copy_button.clicked.connect(lambda: self.send(record=False))
        row.addStretch(1)
        row.addWidget(self.copy_button)
        row.addWidget(self.send_button)
        m.addLayout(row)
        self.tabs.addTab(make, self.tr("通報文の作成"))

        # 通報記録.
        log = QWidget()
        g = QVBoxLayout(log)
        form = QFormLayout()
        self.log_start, self.log_end = _time_edit(now - 7 * DAY_MS), _time_edit(now + DAY_MS)
        period = QHBoxLayout()
        period.addWidget(self.log_start, 1)
        period.addWidget(QLabel("〜"))
        period.addWidget(self.log_end, 1)
        form.addRow(self.tr("通報日時"), period)
        self.log_format = QComboBox()
        self.log_format.currentIndexChanged.connect(lambda _i: self._render_log())
        form.addRow(self.tr("フォーマット"), self.log_format)
        g.addLayout(form)
        row = QHBoxLayout()
        self.log_button = QPushButton(self.tr("取得"))
        self.log_button.clicked.connect(self.read_log)
        row.addWidget(self.log_button)
        row.addStretch(1)
        g.addLayout(row)
        self.log_table = QTableWidget(0, 5)
        self.log_table.setHorizontalHeaderLabels([self.tr("通報日時"), self.tr("フォーマット"), self.tr("通報者"), self.tr("行"), self.tr("観測ID")])
        for c in (0, 1, 2):
            self.log_table.horizontalHeader().setSectionResizeMode(c, QHeaderView.ResizeMode.ResizeToContents)
        self.log_table.horizontalHeader().setSectionResizeMode(3, QHeaderView.ResizeMode.Stretch)
        self.log_table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.log_table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.log_table.cellDoubleClicked.connect(lambda r, _c: self._go(self._shown_log()[r].obs_id))
        g.addWidget(self.log_table, 1)
        # The columns shown, hidden and in order from the header's right-click menu, kept per user.
        self.log_columns = column_menus("alert-log", self.log_table, self)
        row = QHBoxLayout()
        self.log_copy = QPushButton(self.tr("選択をコピー"))
        self.log_copy.clicked.connect(self.copy_log)
        self.log_remove = QPushButton(self.tr("選択を削除（再通報できる）"))
        self.log_remove.clicked.connect(lambda _on=False: self.remove_log())
        row.addStretch(1)
        row.addWidget(self.log_copy)
        row.addWidget(self.log_remove)
        g.addLayout(row)
        self.tabs.addTab(log, self.tr("通報記録"))

        self.status = plain_label(QLabel())
        self.status.setWordWrap(True)
        layout.addWidget(self.status)
        self.reset()

    # ---- settings ------------------------------------------------------------

    @property
    def settings(self) -> Optional[AlertSettings]:
        return self.dock.context.config.alert_reports

    def _token(self, source) -> Optional[str]:
        return None

    def log_repository(self) -> AlertLogRepository:
        """The alert log's reads and writes (データ窓口), its token as it is now."""
        return AlertLogRepository(self.dock.context.esri(), self.settings, self._token(self.settings.table))

    def reset(self) -> None:
        """The formats of the settings file (again), the default chosen."""
        s = self.settings
        for select_, everything in ((self.format_select, False), (self.log_format, True)):
            was = select_.currentData()
            select_.blockSignals(True)
            select_.clear()
            if everything:
                select_.addItem(self.tr("すべて"), "")
            for f in s.formats if s else []:
                select_.addItem(f.name + (self.tr("（既定）") if f.name == s.default else ""), f.name)
            i = select_.findData(was) if was else -1
            if i < 0 and not everything and s:
                i = select_.findData(s.default)
            select_.setCurrentIndex(max(0, i))
            select_.blockSignals(False)
        # The line says only what is missing, and who sets it (tsudo 2026-11-28: no server's tables on the screen);
        # what the conditions look at goes in 「抽出」's tooltip.
        tip = self.tr("通報の条件と通報済みの記録を読み、条件に合う記録を抽出")
        if s is None:
            self.note.setText(self.tr("通報の設定がない（管理者に連絡: alertReports）"))
        else:
            uses = [w for w, on in ((self.tr("ジオフェンス"), s.geofences), (self.tr("地名"), s.named_areas), (self.tr("国"), s.countries)) if on]
            if uses:
                tip += self.tr("（{w}も見る）").format(w="・".join(uses))
            self.note.setText("" if s.table else self.tr("通報済みを記録する表がないため、通報済みの管理なし（管理者に連絡: alertReports.table）"))
        self.note.setVisible(bool(self.note.text()))
        self.pick_button.setToolTip(tip)
        for w in (self.pick_button, self.send_button, self.copy_button, self.format_select):
            w.setEnabled(s is not None)
        for w in (self.log_button, self.log_copy, self.log_remove):
            w.setEnabled(bool(s and s.table))
        self._render_lines()

    def _format(self):
        s = self.settings
        return s.format(self.format_select.currentData() or "") if s else None

    def _say(self, text: str) -> None:
        self.status.setText(text)

    def _go(self, record_id: str, read: bool = False) -> None:
        book = self.dock.book
        if book is not None and book.scoped and not read:
            # As it is on the server now.
            self._say(self.tr("観測記録を取得中…"))
            self.dock.fetch(self.tr("観測記録の取得"), lambda b: b.fetch(RecordQuery(ids=(record_id,))),
                            lambda _r: self._go(record_id, True), lambda m: self._say(m))
            return
        if book is not None and not book.scoped and not self.dock.records_ready(lambda: self._go(record_id)):
            self._say(self.tr("観測記録を取得中…"))
            return
        o = book.record(record_id) if book else None
        if o is None:
            self._say(self.tr("観測記録が見つからない（別の保存先か削除済み）: {i}").format(i=record_id))
            return
        self.dock.open_record(o)

    # ---- making ---------------------------------------------------------------

    def pick(self) -> None:
        """Reads the rules, geofences and log (in a task), then lists the notable records."""
        s, book = self.settings, self.dock.book
        if s is None or book is None:
            self._say(self.tr("観測記録の保存先なし"))
            return
        if not book.scoped and not self.dock.records_ready(self.pick):
            # Not read yet (no image chosen in 「観測記録」): picked once they are.
            self._say(self.tr("観測記録を取得中…（取得後に抽出）"))
            return
        client = self.dock.context.esri()
        records, levels = list(book.records), book.confidences()
        start, end, by = _ms(self.start_edit), _ms(self.end_edit), self.by_select.currentData()
        again = self.again_check.isChecked()
        tokens = {k: self._token(getattr(s, k)) for k in ("rules", "geofences", "named_areas", "countries", "table")}
        scoped = book.scoped

        def work(_task):
            rules = load_rules(client, s.rules, tokens["rules"]) if s.rules else None
            fences = load_geofences(client, s.geofences, tokens["geofences"]) if s.geofences else []
            done = set() if again or not s.table else reported_ids(client, s, tokens["table"])
            period, history = records, records
            if scoped:
                # A shared store: the records of the period, and those before them around them (for the rarity).
                period = book.fetch(RecordQuery(basis="image" if by == "imageTime" else "created", start=start, end=end, living=True))
                wanted = history_query(living(period), s.score)
                history = book.fetch(wanted) if wanted is not None else period
            picked = select(period, s, rules, fences, levels, done, start, end, by, history=history)
            # The places and countries of the records picked: from the squares kept (place_cache), the server
            # asked only for squares not kept; 抽出 asks whether the layers changed since (`fresh`).
            points = points_of(picked)
            named = areas_at(client, s.named_areas, points, tokens["named_areas"], fresh=True)
            countries = areas_at(client, s.countries, points, tokens["countries"], fresh=True)
            name_places(picked, named, countries, levels)
            return picked, rules, layer_key_problems(s, layer_fields(client, s, tokens))

        def finished(result) -> None:
            self.pick_button.setEnabled(True)
            picked, rules, problems = result
            ids = {i.record.id for i in picked}
            # Records added by hand stay first (unless picked by the conditions too); those picked follow, highest score first.
            self.items = [i for i in self.items if i.manual and i.record.id not in ids] + picked
            self._render_lines()
            rule_note = "" if rules is None else self.tr("（条件 {n} 件）").format(n=len(rules))
            said = self.tr("{n} 件を抽出{r}").format(n=len(self.items), r=rule_note)
            if problems:
                # country.CODE… the layer has no attribute for: written empty (a typo, or a field not added yet).
                said += self.tr("。フォーマットの属性に注意: ") + " / ".join(problems)
                self.dock.iface.messageBar().pushWarning(self.tr("通報"), plain_tip(" / ".join(problems)))
            self._say(said)

        def failed(message: str) -> None:
            self.pick_button.setEnabled(True)
            self._say(self.tr("抽出に失敗: {m}").format(m=message))

        self.pick_button.setEnabled(False)
        self._say(self.tr("抽出中…"))
        run_task(self.tr("通報の抽出"), work, finished, failed)

    def add_records(self, records) -> None:
        """
        Adds records by hand (「通報に追加」), whether or not they meet the
        conditions: their geofences and whether they were reported are read in a task.
        """
        s = self.settings
        records = [o for o in records if o.deleted_at is None and all(i.record.id != o.id for i in self.items)]
        if s is None or not records:
            self._say(self.tr("追加する記録なし（すでに一覧にある記録は追加しない）") if s else self.tr("通報の設定なし（管理者に連絡: alertReports）"))
            return
        self.tabs.setCurrentIndex(0)
        client = self.dock.context.esri()
        tokens = {k: self._token(getattr(s, k)) for k in ("geofences", "named_areas", "countries", "table")}
        book = self.dock.book
        history, levels = (list(book.records), book.confidences()) if book else ([], [])
        scoped = book is not None and book.scoped

        def work(_task):
            nonlocal history
            if scoped:
                wanted = history_query(records, s.score)
                history = book.fetch(wanted) if wanted is not None else records
            fences = load_geofences(client, s.geofences, tokens["geofences"]) if s.geofences else []
            done = reported_ids(client, s, tokens["table"], ids=[o.id for o in records]) if s.table else set()
            points = points_of(records)
            named = areas_at(client, s.named_areas, points, tokens["named_areas"])
            countries = areas_at(client, s.countries, points, tokens["countries"])
            return manual_items(records, fences, s, levels, history, named, countries), done

        def finished(result) -> None:
            added, done = result
            added = self.add_items(added)
            again = sum(1 for i in added if i.record.id in done)
            note = self.tr("（うち通報済み {n} 件）").format(n=again) if again else ""
            self._say(self.tr("手動で {n} 件を追加{a}").format(n=len(added), a=note))

        def failed(message: str) -> None:
            # Added all the same: only the geofence names are missing.
            self.items = manual_items(records, (), s, levels, history) + self.items
            self._render_lines()
            self._say(self.tr("手動で {n} 件を追加（ジオフェンス・地名・国・通報済みの確認に失敗: {m}）").format(n=len(records), m=message))

        self._say(self.tr("追加中…"))
        run_task(self.tr("通報への追加"), work, finished, failed)

    def add_items(self, items: list[AlertItem]) -> list[AlertItem]:
        """Puts records (already chosen) at the top of the list, those not there yet; the ones added."""
        ids = {i.record.id for i in self.items}
        added = [i for i in items if i.record.id not in ids]
        self.items = added + self.items
        self._render_lines()
        return added

    def _chosen_rows(self) -> list[int]:
        """The rows selected in the list (those of a record)."""
        return sorted({i.row() for i in self.lines_table.selectedIndexes() if i.row() < len(self.items)})

    def _update_drop(self) -> None:
        self.drop_button.setEnabled(bool(self._chosen_rows()))

    def drop_rows(self, rows: Optional[list[int]] = None) -> None:
        """
        「選択を一覧から削除」: the selected rows (or `rows`) out of the list,
        not sent; nothing is written. The rows left keep their ticks and edits;
        a record meeting the conditions is listed again by the next 「抽出」.
        """
        rows = self._chosen_rows() if rows is None else sorted({r for r in rows if 0 <= r < len(self.items)})
        if not rows:
            self._say(self.tr("一覧から削除する行なし（行を選択）"))
            return
        gone = set(rows)
        self.items = [i for r, i in enumerate(self.items) if r not in gone]
        self._render_lines()
        self._say(self.tr("{n} 件を一覧から削除（通報テーブルはそのまま）").format(n=len(rows)))

    def _moment(self, count: int) -> Moment:
        return Moment(now=QDateTime.currentMSecsSinceEpoch(), user=edit_stamp.user_name(), count=count)

    def _kept_rows(self) -> dict[str, tuple[bool, Optional[str]]]:
        """
        What the person did to the rows shown, by record: unticked, and the
        line edited by hand (kept while the format is the same), so that adding
        records or picking again does not undo it.
        """
        kept: dict[str, tuple[bool, Optional[str]]] = {}
        same_format = self._shown_format == (self.format_select.currentData() or "")
        for r, (record_id, line) in enumerate(self._shown_lines):
            check, cell = self.lines_table.item(r, 0), self.lines_table.item(r, 1)
            if check is None or cell is None:
                continue
            edited = cell.text() if same_format and cell.text() != line else None
            kept[record_id] = (check.checkState() == Qt.CheckState.Checked, edited)
        return kept

    def _needs_english(self) -> bool:
        fmt = self._format()
        return fmt is not None and uses_source(fmt, "note.en")

    def translate_notes(self) -> bool:
        """「備考を英訳」: the 備考 of the records listed in English (`note.en`), then the lines again; 「英訳を中止」 while it runs."""
        t = self.translator
        if t.running:
            t.cancel()
            return False

        def done() -> None:
            self.translate_button.setText(self.tr("備考を英訳"))
            self._render_lines()

        def failed(message: str) -> None:
            self.translate_button.setText(self.tr("備考を英訳"))
            self._say(self.tr("英訳を中止") if message == "取消" else self.tr("英訳に失敗: {m}").format(m=message))

        started = t.translate([i.record.note for i in self.items], done, failed)
        if started and t.running:
            self.translate_button.setText(self.tr("英訳を中止"))
        return started

    def _name_send(self, fmt) -> None:
        """The send button's name and tooltip: what it does with the format (通報テーブル, clipboard, file); nothing is sent out."""
        table = bool(self.settings and self.settings.table)
        outs = [w for w, on in ((self.tr("コピー"), fmt is None or fmt.clipboard), (self.tr("ファイルに保存"), fmt is not None and fmt.file)) if on]
        if len(outs) == 2:
            outs = [self.tr("コピー・保存")]
        what = (self.tr("記録して") if table else "") + "".join(outs) if outs else self.tr("記録")
        self.send_button.setText(what)
        steps = ([self.tr("通報テーブルに記録")] if table else []) + \
                ([self.tr("クリップボードへコピー")] if fmt is None or fmt.clipboard else []) + \
                ([self.tr("ファイルに保存")] if fmt is not None and fmt.file else [])
        self.send_button.setToolTip(self.tr("チェックした記録を{s}。外へは送らない（送るのは貼り付けた先で）").format(s="・".join(steps) or self.tr("記録")))

    def _render_lines(self) -> None:
        fmt = self._format()
        self._name_send(fmt)
        english = self._needs_english() and self.translator.offered
        self.translate_button.setVisible(english)
        for item in self.items:
            item.note_en = self.translator.known.get(item.record.note.strip(), "") if item.record.note else ""
        missing = bool(self.translator.missing(i.record.note for i in self.items))
        # 「英訳を中止」 while it runs; else only while some 備考 has no English yet.
        self.translate_button.setEnabled(self.translator.running or missing)
        if english and missing and not self.translator.running:
            self._say(self.tr("このフォーマットは備考の英訳（note.en）を使う: 「備考を英訳」で作成（未作成の行は空）"))
        kept = self._kept_rows()
        self._filling = True
        self.lines_table.blockSignals(True)
        self.lines_table.setRowCount(0)
        lines = build_lines(self.items, fmt, self._moment(len(self.items))) if fmt else []
        self._shown_lines = [(i.record.id, line) for i, line in zip(self.items, lines)]
        self._shown_format = self.format_select.currentData() or ""
        for r, line in enumerate(lines):
            self.lines_table.insertRow(r)
            item = self.items[r] if r < len(self.items) else None
            ticked, edited = kept.get(item.record.id, (True, None)) if item is not None else (True, None)
            check = QTableWidgetItem()
            check.setFlags(Qt.ItemFlag.ItemIsUserCheckable | Qt.ItemFlag.ItemIsEnabled)
            check.setCheckState(Qt.CheckState.Checked if ticked else Qt.CheckState.Unchecked)
            self.lines_table.setItem(r, 0, check)
            cell = QTableWidgetItem(edited if edited is not None else line)
            marks, tips = [], []
            if item is not None and item.manual:
                tips.append(self.tr("手動で追加（条件に関係なく通報）"))
                marks.append(self.tr("手動"))
            if item is not None and english and item.note_en:
                check_note = self.translator.checks.get(item.record.note.strip())
                tips.append(self.tr("備考の英訳（生成 AI）: {e}").format(e=item.note_en) + (self.tr("（確認: {c}）").format(c=check_note) if check_note else ""))
            if item is not None and item.score is not None:
                marks.append(f"{item.score:g}")
                tips.append(self.tr("重要度 {s}: {p}").format(s=f"{item.score:g}", p=score_note(item)))
            if marks:
                check.setText(" ".join(marks))
            if tips:
                check.setToolTip(plain_tip("\n".join(tips)))
                cell.setToolTip(plain_tip("\n".join(tips)))
            self.lines_table.setItem(r, 1, cell)
        self.lines_table.blockSignals(False)
        self._filling = False
        self._update_drop()
        self._render_text()

    def chosen(self) -> tuple[list[AlertItem], list[str]]:
        """The ticked records and their lines (as edited)."""
        items, lines = [], []
        for r, item in enumerate(self.items):
            check, cell = self.lines_table.item(r, 0), self.lines_table.item(r, 1)
            if check is not None and check.checkState() == Qt.CheckState.Checked:
                items.append(item)
                lines.append(cell.text() if cell else "")
        return items, lines

    def text(self, at: Optional[Moment] = None) -> str:
        fmt = self._format()
        items, lines = self.chosen()
        return report_text(lines, fmt, at or self._moment(len(items))) if fmt else ""

    def _render_text(self) -> None:
        if not self._filling:
            self.preview.setPlainText(self.text().replace("\r\n", "\n"))

    def send(self, record: bool = True, after: Optional[Callable[[str], None]] = None) -> None:
        """
        「記録してコピー」 and the like (`_name_send`): the lines to the log table (in a task), then the
        clipboard and the file as the format says; 「記録せずにコピー」 (record False): the clipboard only.
        """
        s, fmt = self.settings, self._format()
        items, lines = self.chosen()
        if s is None or fmt is None:
            return
        if not items:
            self._say(self.tr("通報する記録なし（抽出してチェック）"))
            return
        at = self._moment(len(items))
        text = report_text(lines, fmt, at)
        if not record:
            QApplication.clipboard().setText(text)
            audit.record("copy", "alert-text", count=len(items), text=text)
            self._say(self.tr("{n} 件をクリップボードへコピー（通報テーブルには記録せず）").format(n=len(items)))
            return

        def outputs(logged: int) -> None:
            # First: whatever happens below, the button can be pressed again.
            self.send_button.setEnabled(True)
            said = [self.tr("通報テーブルに {n} 件").format(n=logged)] if s.table else []
            if fmt.clipboard:
                QApplication.clipboard().setText(text)
                audit.record("copy", "alert-text", count=len(items), text=text)
                said.append(self.tr("クリップボードへコピー"))
            if fmt.file:
                try:
                    path, replaced = write_file(text, fmt, at)
                    audit.record("export", "alert-text", path, count=len(items), file=path)
                    said.append(self.tr("ファイル {p}").format(p=path))
                    if replaced:
                        said.append(self.tr("{enc} で書けない文字を ? に置き換え").format(enc=fmt.file.encoding))
                except (OSError, ValueError) as error:
                    reason, detail = errors.describe(error)
                    said.append(self.tr("ファイルの書き込みに失敗: {e}").format(e=reason))
                    audit.error("file-save", getattr(error, "filename", None) or "", reason, detail={"what": "alert-text", "said": detail})
            sent = {i.record.id for i in items}
            if s.table and logged:
                self.dock.mark_reported(sent)
            self.items = [i for i in self.items if i.record.id not in sent] if s.table else self.items
            self._render_lines()
            message = self.tr("{n} 件を通報: ").format(n=len(items)) + "・".join(said or [self.tr("出力先なし")])
            self._say(message)
            if after:
                after(message)

        if not s.table:
            outputs(0)
            return
        repo = self.log_repository()

        def failed(message: str) -> None:
            self.send_button.setEnabled(True)
            self._say(self.tr("通報の記録に失敗（コピーもファイル出力もなし）: {m}").format(m=message))
            self.dock.refresh_reported()  # some rows may have gone in

        self.send_button.setEnabled(False)
        run_task(self.tr("通報の記録"), lambda _t: repo.write_log(fmt, items, lines, at), outputs, failed)

    # ---- the log ------------------------------------------------------------

    def read_log(self) -> None:
        s = self.settings
        if s is None or s.table is None:
            return
        repo = self.log_repository()
        start, end = _ms(self.log_start), _ms(self.log_end)

        def finished(rows) -> None:
            self.log_button.setEnabled(True)
            self.log_rows = rows
            self._render_log()
            self._say(self.tr("通報記録 {n} 件").format(n=len(rows)))

        def failed(message: str) -> None:
            self.log_button.setEnabled(True)
            self._say(self.tr("通報記録の取得に失敗: {m}").format(m=message))

        self.log_button.setEnabled(False)
        run_task(self.tr("通報記録の取得"), lambda _t: repo.load_log(start, end), finished, failed)

    def _shown_log(self) -> list[LogRow]:
        name = self.log_format.currentData() or ""
        return [r for r in self.log_rows if not name or r.format == name]

    def _render_log(self) -> None:
        s = self.settings
        fmt = s.format() if s else None
        self.log_table.setRowCount(0)
        for r, row in enumerate(self._shown_log()):
            self.log_table.insertRow(r)
            when = "" if row.reported_at is None or fmt is None else str(format_time(row.reported_at, "%Y-%m-%d %H:%M:%S", fmt.time_zone))
            for c, v in enumerate((when, row.format, row.reported_by, row.text, row.obs_id)):
                self.log_table.setItem(r, c, QTableWidgetItem(v))

    def _selected_log(self) -> list[LogRow]:
        shown = self._shown_log()
        rows = sorted({i.row() for i in self.log_table.selectedIndexes()})
        return [shown[r] for r in rows if r < len(shown)]

    def copy_log(self) -> None:
        rows = self._selected_log() or self._shown_log()
        text = "".join(r.text + "\r\n" for r in rows)
        QApplication.clipboard().setText(text)
        audit.record("copy", "alert-log", count=len(rows), text=text)
        self._say(self.tr("{n} 行をクリップボードへコピー").format(n=len(rows)))

    def remove_log(self, confirm: bool = True) -> None:
        s, rows = self.settings, self._selected_log()
        if s is None or not rows:
            self._say(self.tr("削除する行を選択"))
            return
        if confirm and not ask_no(self, self.tr("通報記録の削除"),
                                  self.tr("選んだ {n} 行を通報済みから削除（その記録は次の抽出でまた表示）").format(n=len(rows)), yes=self.tr("削除")):
            return
        repo = self.log_repository()

        def finished(n: int) -> None:
            gone = {id(r) for r in rows}
            self.log_rows = [r for r in self.log_rows if id(r) not in gone]
            self._render_log()
            self.dock.refresh_reported()  # another format's row may keep a record reported
            self._say(self.tr("通報記録 {n} 行を削除").format(n=n))

        run_task(self.tr("通報記録の削除"), lambda _t: repo.remove_log(rows), finished,
                 lambda m: self._say(self.tr("通報記録の削除に失敗: {m}").format(m=m)))
