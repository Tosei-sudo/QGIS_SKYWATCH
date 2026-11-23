"""
依頼文から下書き (requestDraft, core/text_request.py) on the request form: the
row 「依頼文」 at the top of 撮像依頼の登録・編集 (not of a batch), shown only
while the settings give the task a model (`textAssist.tasks`).

A mail or chat text pasted there and 「欄の案を作成」: the named areas read
first (`namedAreas` of the observation export or the 通報), the words read
by code (dates, numbers, satellites, the words of each choice), the model
asked only for the lines the code cannot write (the name, the places, …;
core/text_request.asked_keys: none for 「12月末まで延長」, then it is not
asked) and its values kept only where the words give them a footing. The proposal shows under the text: one check box per item
(all ticked), the places with the areas they can be (地名テーブル → 過去の
依頼 → 座標 → 地名・住所検索; the model never makes a shape), what to check
(確認), 「メモに依頼文を残す」. 「欄に入力」 puts the ticked items in the
fields (tinted until touched); with two places or more ticked (a new
request) it reads 「n 件の依頼を作成」 and opens the batch form (one request
per place, the rest as here). Nothing is saved until 「保存」.

Editing a request: only what the text names is proposed (「12月末まで延長」:
the end of the period), the rest stays as it is.
"""
from __future__ import annotations

import time
from dataclasses import replace
from datetime import date
from typing import Callable, Optional, Sequence

from qgis.PyQt.QtCore import QCoreApplication, QObject
from qgis.PyQt.QtWidgets import (
    QCheckBox, QComboBox, QGridLayout, QHBoxLayout, QLabel, QPlainTextEdit, QPushButton, QSizePolicy, QVBoxLayout, QWidget,
)

from ..core.obs.api import load_named_areas, settings_for
from ..core.request.api import ImagingRequest, jst_day
from ..core.text_request import (
    ITEMS, TEXT_LIMIT, WRITTEN, AreaChoice, RequestDraft, applied, area_choices, asked_keys, batch_names, found_choice, point_choice,
    request_draft, request_messages,
)
from ..core.text_tasks import lines_done
from ..qgis_io.place_search import place_search_of
from ..qgis_io.tasks import run_task
from .text_assist_ui import OFFER_STYLE, PLAIN_STYLE, ProposalPanel, _Asking

TASK = "requestDraft"


def _tr(text: str) -> str:
    return QCoreApplication.translate("RequestDraft", text)

#: The places a geocoder's answers are taken for one name (the first ones).
FOUND = 3


class DraftHost:
    """
    What 依頼文から下書き needs from the 「撮像依頼」 dock: the named areas, the
    requests of the ledger (their names and areas), the place search, and
    the batch form for several places.
    """

    def __init__(self, context, requests: Callable[[], Sequence[ImagingRequest]],
                 open_batch: Callable[[ImagingRequest, list, str], None], today: Optional[Callable[[], date]] = None):
        self.context = context
        self.requests = requests
        self.open_batch = open_batch
        self.today = today or (lambda: jst_day(time.time() * 1000))

    def named_areas(self, then: Callable[[list, str], None]) -> None:
        """The named areas (read in a task, kept 10 minutes), then `then(areas, why none)`."""
        config = self.context.config
        source = settings_for(config.observation_exports, config.alert_reports).named_areas
        if source is None:
            then([], "")
            return
        client, token = self.context.esri(), self.context.token_for(source.url, source.token)
        run_task(_tr("地名の取得"), lambda _t: load_named_areas(client, source, token, max_age=600),
                 lambda areas: then(list(areas), ""), lambda m: then([], m), hidden=True)

    def find_places(self, words: Sequence[str], then: Callable[[dict, list], None]) -> None:
        """Each word looked up with the settings' geocoders (in a task), then `then({word: [Place]}, errors)`."""
        search = place_search_of(self.context)
        if not words or not search.enabled:
            then({}, [])
            return

        def work(task):
            found, errors = {}, []
            for w in words:
                if task.isCanceled():
                    break
                places, problems = search.search(w, task.isCanceled)
                found[w] = places[:FOUND]
                errors += problems
            return found, errors

        run_task(_tr("地名・住所の検索"), work, lambda r: then(*r), lambda m: then({}, [m]), hidden=True)


class _AreaRow:
    """One place of the text: a check box, and the areas it can be (or why there is none)."""

    def __init__(self, place: str, choices: list[AreaChoice]):
        self.place = place
        self.choices = choices
        self.check = QCheckBox(place)
        self.combo = QComboBox()
        for c in choices:
            self.combo.addItem(c.label)
        self.combo.setEnabled(bool(choices))
        self.check.setEnabled(bool(choices))
        if not choices:
            self.combo.addItem(_tr("見つからず（地図で指定）"))

    @property
    def chosen(self) -> Optional[AreaChoice]:
        return self.choices[self.combo.currentIndex()] if self.check.isChecked() and self.choices else None


class DraftPanel(ProposalPanel):
    """Under the text: the items proposed (each with a check box), the places and their areas, what to check."""

    def __init__(self, parent: Optional[QWidget] = None):
        super().__init__(take=_tr("欄に入力"), parent=parent)
        box: QVBoxLayout = self.layout()
        self.items = QWidget()
        self.items_box = QVBoxLayout(self.items)
        self.items_box.setContentsMargins(6, 0, 0, 0)
        self.items_box.setSpacing(0)
        box.insertWidget(1, self.items)
        self.areas = QWidget()
        self.areas_box = QGridLayout(self.areas)
        self.areas_box.setContentsMargins(6, 0, 0, 0)
        box.insertWidget(2, self.areas)
        self.note_check = QCheckBox(self.tr("メモに依頼文を残す"))
        self.note_check.setChecked(True)
        self.note_check.setToolTip(self.tr("依頼文を日付と一緒にメモの末尾に追加（どの文から作ったかが残る）"))
        box.insertWidget(4, self.note_check)
        self.replace_button.setToolTip(self.tr("チェックした項目と範囲を欄に入力（保存はまだ）"))
        self.checks: dict[str, QCheckBox] = {}
        self.rows: list[_AreaRow] = []
        self.editing = False
        self.draft_value: Optional[RequestDraft] = None
        self._extras(False)

    def _extras(self, shown: bool) -> None:
        for w in (self.items, self.areas, self.note_check):
            w.setVisible(shown)

    def writing(self, stage: str, text: str = "") -> None:
        super().writing(stage, text)
        self._extras(False)

    def say(self, text: str, notes: Sequence[str] = ()) -> None:
        super().say(text, notes)
        self._extras(False)

    def _clear(self) -> None:
        for layout in (self.items_box, self.areas_box):
            while layout.count():
                w = layout.takeAt(0).widget()
                if w is not None:
                    w.deleteLater()
        self.checks, self.rows = {}, []

    def offer_draft(self, draft: RequestDraft, rows: list[_AreaRow], notes: Sequence[str], seconds: float = 0.0,
                    editing: bool = False) -> None:
        self._clear()
        self.draft_value, self.editing = draft, editing
        took = self.tr("（{s:.1f} 秒）").format(s=seconds) if seconds >= 0.05 else ""
        self.label.setText(self.tr("案{took}: 入れる項目を選んで「欄に入力」").format(took=took))
        self.label.setStyleSheet(OFFER_STYLE)
        for key, _label in ITEMS:
            if key in draft.values:
                check = QCheckBox(draft.text_of(key))
                check.setChecked(True)
                check.toggled.connect(lambda _on: self._update())
                self.items_box.addWidget(check)
                self.checks[key] = check
        if rows:
            self.areas_box.addWidget(QLabel(self.tr("範囲")), 0, 0, 1, 2)
        self.rows = list(rows)
        # Written coordinates are the area when there are any (a place name beside them names the same spot);
        # editing, one area (the first).
        points = any(c.source == WRITTEN for r in rows for c in r.choices)
        first = True
        for i, row in enumerate(rows, start=1):
            self.areas_box.addWidget(row.check, i, 0)
            self.areas_box.addWidget(row.combo, i, 1)
            if row.choices:
                pick = first if editing else (row.choices[0].source == WRITTEN if points else True)
                row.check.setChecked(pick)
                first = first and not pick
            row.check.toggled.connect(lambda on, row=row: self._toggled(row, on))
        self.areas_box.setColumnStretch(1, 1)
        self.note_check.setChecked(True)
        self.warning.setText(self.tr("確認: ") + "。".join(notes) if notes else "")
        self.warning.setVisible(bool(notes))
        self._extras(True)
        self.areas.setVisible(bool(rows))
        self.replace_button.show()
        self.append_button.hide()
        self.cancel_button.hide()
        self.close_button.show()
        self.draft = None
        self._update()
        self.show()

    def _toggled(self, row: _AreaRow, on: bool) -> None:
        if on and self.editing:
            # Editing: one area (or none: the area stays as it is).
            for other in self.rows:
                if other is not row and other.check.isChecked():
                    other.check.setChecked(False)
        self._update()

    def keys(self) -> list[str]:
        return [key for key, check in self.checks.items() if check.isChecked()]

    def chosen(self) -> list[AreaChoice]:
        return [c for c in (r.chosen for r in self.rows) if c is not None]

    def _update(self) -> None:
        n = len(self.chosen())
        if n >= 2 and not self.editing:
            self.replace_button.setText(self.tr("{n} 件の依頼を作成").format(n=n))
            self.replace_button.setToolTip(self.tr("地名ごとに 1 件ずつ、まとめて登録の画面で依頼を作成（チェックした項目は全件に入力。保存はまだ）"))
        else:
            self.replace_button.setText(self.tr("欄に入力"))
            self.replace_button.setToolTip(self.tr("チェックした項目と範囲を欄に入力（保存はまだ）"))
        self.replace_button.setEnabled(bool(self.keys() or n))


class RequestDraftRow(QObject):
    """
    The row 「依頼文」 of the request form. `form`: the RequestForm (its
    `request`, `value()`, `take_draft(...)`, `satellites_known()`, `size`);
    `host`: a DraftHost.
    """

    task = TASK

    def __init__(self, form, assistant, host: DraftHost, parent: Optional[QObject] = None):
        super().__init__(parent or form)
        self.form = form
        self.assistant = assistant
        self.host = host
        self.label = QLabel(self.tr("依頼文"))
        self.edit = QPlainTextEdit()
        self.edit.setPlaceholderText(self.tr("メールやチャットの依頼文を貼り付け（例: 来週月曜から2週間、横須賀港を光学で毎日。雲30%以下、至急）"))
        self.edit.setFixedHeight(64)
        self.button = QPushButton(self.tr("欄の案を作成"))
        self.button.setAutoDefault(False)
        self.button.setToolTip(self.tr("依頼文から名称・期間・頻度・優先度・条件・範囲の案を作成（この PC の小さな生成 AI）。"
                                       "「欄に入力」を押すまで欄は変わらない"))
        self.button.setEnabled(False)
        self.taken = QLabel()
        self.taken.setWordWrap(True)
        self.taken.setStyleSheet(PLAIN_STYLE)
        self.taken.hide()
        self.panel = DraftPanel()
        self.widget = QWidget()
        v = QVBoxLayout(self.widget)
        v.setContentsMargins(0, 0, 0, 0)
        v.setSpacing(2)
        v.addWidget(self.edit)
        h = QHBoxLayout()
        h.setContentsMargins(0, 0, 0, 0)
        h.addWidget(self.button)
        h.addWidget(self.taken, 1)
        h.addStretch(0)
        self.button.setSizePolicy(QSizePolicy.Policy.Maximum, QSizePolicy.Policy.Fixed)
        v.addLayout(h)
        v.addWidget(self.panel)
        self._asking = _Asking(assistant, self.panel, self)
        self.panel.cancel_button.clicked.connect(lambda _on=False: self._stop_waiting())
        self.panel.replace_button.clicked.connect(lambda _on=False: self.take())
        self.button.clicked.connect(lambda _on=False: self.ask())
        self.edit.textChanged.connect(self._text_changed)
        assistant.changed.connect(self._settings_changed)
        #: The text the proposal is for, and while the named areas or the places are read.
        self.given = ""
        self.waiting = False
        self._settings_changed()

    @property
    def offered(self) -> bool:
        return self.assistant.has(self.task)

    @property
    def editing(self) -> bool:
        return self.form.request.object_id is not None

    def _settings_changed(self) -> None:
        shown = self.offered
        self.label.setVisible(shown)
        self.widget.setVisible(shown)
        if not shown:
            self._asking.cancel()
            self.waiting = False
            self.panel.hide()

    def _text_changed(self) -> None:
        text = self.edit.toPlainText()
        self.button.setEnabled(bool(text.strip()))
        self.taken.hide()
        if self.panel.isVisible() and text != self.given:
            # Another text: the proposal was for the one before.
            self._asking.cancel()
            self.waiting = False
            self.panel.hide()

    def _stop_waiting(self) -> None:
        if self.waiting:
            self.waiting = False
            self.panel.hide()

    def stop(self) -> None:
        """The form closed: the model's writing cancelled, nothing more shown."""
        self.waiting = False
        self._asking.cancel()

    def ask(self) -> bool:
        """Asks for the proposal (the named areas read first). False when it cannot start."""
        s = self.assistant.settings
        t = s.task(self.task) if s is not None else None
        text = self.edit.toPlainText()
        if t is None or not text.strip() or self._asking.running or self.waiting:
            return False
        self.waiting = True
        self.given = text
        self.taken.hide()
        self.panel.writing(self.tr("地名を取得中…"))
        today = self.host.today()
        request = self.form.request if self.editing else None

        def ready(areas: list, problem: str) -> None:
            if not self.waiting or self.edit.toPlainText() != text:
                return
            self.waiting = False
            notes = [self.tr("地名テーブルの読み込みに失敗: {m}").format(m=problem)] if problem else []
            if len(text.strip()) > TEXT_LIMIT:
                notes.append(self.tr("依頼文は先頭 {n} 文字まで読む").format(n=TEXT_LIMIT))
            keys = asked_keys(text, today, request)

            def done(raw: str, seconds: float) -> None:
                draft = request_draft(raw, text, today, self.form.satellites_known(), [a.name for a in areas], request)
                self._areas(draft, areas, notes, seconds, asked=bool(keys))

            if not keys:
                # The words say all (「12月末まで延長」): the model is not asked.
                done("", 0.0)
                return
            messages = request_messages(t, text, today, request, keys)
            self._asking.start(self.task, self.tr("依頼文から下書き"), messages, done, until=lines_done(keys))

        self.host.named_areas(ready)
        return True

    def _areas(self, draft: RequestDraft, areas: list, notes: list, seconds: float, asked: bool = True) -> None:
        """The areas each place can be; the places no table knows looked up with the geocoders first."""
        km = self.form.size.value()
        before = [r for r in self.host.requests() if r.object_id != self.form.request.object_id or r.object_id is None]
        rows = [(f"{lat:.4f}, {lon:.4f}", [point_choice(lon, lat, km)]) for lon, lat in draft.points]
        rows += [(w, area_choices(w, areas, before)) for w in draft.places]
        missing = [w for w, choices in rows if not choices]
        text = self.given

        def show(found: dict, errors: list) -> None:
            if self.edit.toPlainText() != text:
                return
            final = [(w, choices or [found_choice(w, p, km) for p in found.get(w, [])]) for w, choices in rows]
            more = [*notes, *(self.tr("地名・住所検索: {e}").format(e=e) for e in dict.fromkeys(errors))]
            self._offer(draft, final, more, seconds, asked)

        if missing:
            self.waiting = True
            self.panel.writing(self.tr("地名を検索中…"), "、".join(missing))

            def found(result: dict, errors: list) -> None:
                if not self.waiting:
                    return
                self.waiting = False
                show(result, errors)

            self.host.find_places(missing, found)
        else:
            show({}, [])

    def _offer(self, draft: RequestDraft, rows: list, notes: list, seconds: float, asked: bool = True) -> None:
        checks = [*draft.checks, *notes]
        caution = self.assistant.caution(self.task) if asked else ""
        if caution:
            checks.append(caution)
        area_rows = [_AreaRow(w, choices) for w, choices in rows] if draft.area_asked else []
        if draft.empty and not any(r.choices for r in area_rows):
            self.panel.say(self.tr("直すところなし（文に変更の手がかりなし）") if self.editing else self.tr("案の作成に失敗（文から項目を読み取れない）"),
                           checks)
            return
        self.panel.offer_draft(draft, area_rows, checks, seconds, self.editing)

    def take(self) -> None:
        """The items ticked (and the area chosen) in the fields; or, with several places, the batch form."""
        draft = self.panel.draft_value
        if draft is None:
            return
        keys = self.panel.keys()
        chosen = self.panel.chosen()
        note = self.panel.note_check.isChecked()
        text = self.given
        self.panel.hide()
        if len(chosen) >= 2 and not self.editing:
            template = applied(self.form.value(), draft, keys, text, note)
            named = replace(draft, values={**draft.values, "name": template.name, "kind": template.kind})
            names = batch_names(named, [c.place for c in chosen])
            items = [(name, [list(x) for x in c.rings], c.kind, {}) for name, c in zip(names, chosen)]
            self.host.open_batch(template, items, "、".join(names[:3]) + ("…" if len(names) > 3 else ""))
            return
        n = self.form.take_draft(draft, keys, chosen[0] if chosen else None, text, note)
        self.taken.setText(self.tr("依頼文から {n} 項目を入力（色の付いた欄。保存はまだ）").format(n=n))
        self.taken.show()

