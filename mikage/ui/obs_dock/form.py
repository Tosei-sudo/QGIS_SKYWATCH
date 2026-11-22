"""
観測記録ドックの入力欄 (ui/obs_dock, docs/architecture.md 段階7): the form of a record: its kind, activity, numbers,
candidates and confidence, and its submission. A part of ObservationDock (ui/observation_dock.py): its
methods as they were, on the dock's widgets and state.
"""
from __future__ import annotations

from typing import Callable, Optional

from qgis.PyQt.QtCore import Qt
from qgis.PyQt.QtWidgets import QComboBox, QGridLayout, QLabel, QLineEdit, QToolButton, QWidget

from ...core.obs.api import (
    FormValues, local_time, MatchCandidate, js_round, UNSEEN, Observation, unseen_label, children_of, find, kind_of,
    level_name, member_index, name_label_of, path_of, spec_text, specs_at,
)
from ...core.util import read_number
from ..docks import plain_tip
from ..flow_layout import FlowLayout


class FormPart:
    """観測記録ドックの入力欄."""

    def open_form(self, lon: float, lat: float, editing: Optional[Observation] = None, source: Optional[Observation] = None,
                  measured: Optional[tuple] = None) -> None:
        """Opens the form for a new record at a place, a record to change, or a thing carried over."""
        if self.book is None:
            return
        self.pending = (lon, lat, editing, source)
        self._draft_id = ""
        known = editing or source
        length = measured[0] if measured else (known.length if known else None)
        heading = measured[1] if measured else (known.heading if known else None)
        width = (measured[2] if len(measured) > 2 else None) if measured else (known.width if known else None)
        self.length_input.setText("" if length is None else f"{length:.1f}")
        self.heading_input.setText("" if heading is None else f"{heading:.1f}")
        self.width_input.setText("" if width is None else f"{width:.1f}")
        measuring = self.points > 1 or length is not None
        for w in (self.length_label, self.length_input, self.heading_label, self.heading_input, self.measure_note):
            w.setVisible(measuring)
        for w in (self.width_label, self.width_input):
            w.setVisible(self.points > 2 or width is not None)
        last = self.book.last
        self.object_input.setText(known.object_name if known else "")
        self.kind_input.setText(known.kind if known else (last.kind or (self.book.vocabulary.kinds[0] if self.book.vocabulary.kinds else "")))
        self.activity_input.setText(known.activity if known else last.activity)
        if not known:
            self._fit_activity()
        self.note_input.setText(editing.note if editing else "")
        self._fill_confidence((known.confidence or "") if known else last.confidence)
        self.delete_button.setVisible(editing is not None)
        self.alert_button.setVisible(editing is not None and self.context.config.alert_reports is not None)
        self.absent_button.setVisible(known is not None)
        # 「不可」: a thing known (carried over, or a record changed) that this image does not show.
        self.unseen_button.setVisible(known is not None and self.book.keeps_unseen())
        self.unseen_menu.clear()
        for reason in self.book.vocabulary.unseen_reasons:
            self.unseen_menu.addAction(reason, lambda r=reason: self.submit(False, unseen=r))
        self.submit_button.setText(self.tr("更新（Enter）") if editing else self.tr("継続（Enter）") if source else self.tr("記録（Enter）"))
        title = editing.object_name if editing else source.object_name if source else self.tr("新規の記録")
        self.form.setTitle(f"{title}（{unseen_label(editing.unseen)}）" if editing is not None and editing.unseen else title)
        self.form.show()
        self.reveal(self.form)
        self._render_chips()
        self._render_kind_path()
        self._render_candidates()
        self.draw()
        # A new thing gets its name typed; a known one is saved with Enter (or 1–9 picks its activity first).
        self.many.render()
        self.raise_()
        (self.submit_button if known else self.object_input).setFocus()
        self._filled = self.form_values()
        if editing is not None:
            self.refresh(force=True)

    def close_form(self) -> None:
        if self.pending is None and not self.form.isVisible():
            return
        self.pending = None
        self.form.hide()
        self.many.render()
        self.offered = []
        self._clear(self.candidates_layout)
        self.candidates_box.hide()
        self.draw()

    def form_values(self) -> FormValues:
        return FormValues(object=self.object_input.text(), kind=self.kind_input.text(), activity=self.activity_input.text(),
                          note=self.note_input.text(), confidence=self.confidence_select.currentData() or "",
                          length=self.length_input.text(), heading=self.heading_input.text(), width=self.width_input.text())

    def _unseen_reason(self) -> str:
        """Why the thing in the form could not be seen: the 不可範囲's reason where it is, else the first offered."""
        if self.book is None or self.pending is None:
            return UNSEEN
        lon, lat = self.pending[0], self.pending[1]
        reasons = self.book.vocabulary.unseen_reasons
        return self.book.unseen_reason_at(lon, lat) or (reasons[0] if reasons else UNSEEN)

    def submit(self, present: bool = True, unseen: str = "") -> None:
        """Records what the form says (a new record, a change, or a thing carried over; `unseen`: 「不可」, why)."""
        if self.book is None or self.pending is None:
            return
        lon, lat, editing, source = self.pending
        if not self._numbers_readable():
            return
        values = self.form_values()
        draft = self._draft_id
        # Offered first by the next new record, in later sessions too.
        self.book.remember(values.kind, values.activity if present else None, values.confidence)
        self.close_form()

        def lost(message: str) -> None:
            # Not saved (the network, the token, a conflict): the form comes back as it was typed.
            self.restore_form(lon, lat, editing, source, values)
            self.refresh(force=True)
            self.iface.messageBar().pushWarning(self.tr("観測記録"), plain_tip(self.tr(
                "記録を保存できませんでした（送信待ちに保管しました）: {m}").format(m=message)))

        self.do(self.tr("記録"), lambda b: b.submit(lon, lat, values, present, editing, source, draft=draft or None, unseen=unseen),
                lambda saved: lost(self.status.text()) if saved is None else self.many.use_record(saved), watch=True, failed=lost)

    def _numbers_readable(self) -> bool:
        """
        The typed length, width and heading can be read (full-width digits and
        a unit such as 120 m or 30° can): otherwise the form stays open and
        says which, rather than saving the record without it (or, editing,
        dropping the value it had).
        """
        for edit, label in ((self.length_input, self.length_label), (self.width_input, self.width_label), (self.heading_input, self.heading_label)):
            try:
                read_number(edit.text())
            except ValueError:
                self.measure_note.setText(self.tr("{label}の「{text}」を読めません（数で）").format(label=label.text(), text=edit.text().strip()))
                self.measure_note.setVisible(True)
                edit.setVisible(True)
                edit.setFocus()
                edit.selectAll()
                return False
        return True

    def restore_form(self, lon: float, lat: float, editing: Optional[Observation], source: Optional[Observation], values: FormValues) -> None:
        """The form opened again with what was typed (a save that did not go through), unless another one is open."""
        if self.book is None or self.pending is not None:
            return
        self.open_form(lon, lat, editing=editing, source=source)
        self._draft_id = self.book.last_draft_id
        self.object_input.setText(values.object)
        self.kind_input.setText(values.kind)
        self.activity_input.setText(values.activity)
        self.note_input.setText(values.note)
        self._fill_confidence(values.confidence)
        for edit, text in ((self.length_input, values.length), (self.heading_input, values.heading), (self.width_input, values.width)):
            edit.setText(text)
            if text:
                edit.setVisible(True)
        self._render_chips()
        self._render_kind_path()

    def _delete_pending(self) -> None:
        if self.pending is None or self.pending[2] is None:
            return
        record = self.pending[2]
        self.close_form()
        self.remove([record])

    @staticmethod
    def _clear(layout: FlowLayout) -> None:
        layout.clear()

    def _chip_button(self, text: str, tip: str, run: Callable, pressed: bool = False) -> QToolButton:
        b = QToolButton()
        b.setText(text)
        b.setToolTip(tip)
        b.setCheckable(True)
        b.setChecked(pressed)
        b.setAutoRaise(False)
        b.clicked.connect(lambda _on=False: run())
        return b

    def _activity_words(self) -> list[str]:
        if self.book is None:
            return []
        return self.book.activity_words(self.kind_input.text())[:9]

    def _fit_activity(self) -> None:
        """The activity left out when the kind's class has its own activities and they do not include it."""
        if self.book is None:
            return
        kind, value = self.kind_input.text(), self.activity_input.text().strip()
        _offered, at = self.book.activities_for(kind)
        if at and value and value not in self.book.activity_words(kind):
            self.activity_input.clear()

    def _render_chips(self) -> None:
        self._clear(self.chips_layout)
        value = self.activity_input.text()
        if self.book is not None:
            kind = self.kind_input.text()
            _offered, at = self.book.activities_for(kind)
            self.activities_model.setStringList(self.book.activity_words(kind))
            self.activity_input.setPlaceholderText(self.tr("{c}の活動").format(c=at[-1]) if at else "")
            self.chips_box.setToolTip(self.tr("分類「{c}」の活動（分類の編集で変更）").format(c=kind_of(at)) if at else self.tr("既定の活動"))
        for i, w in enumerate(self._activity_words()):
            self.chips_layout.addWidget(self._chip_button(f"{i + 1} {w}", f"{w}（{i + 1}）", lambda w=w: self._pick_activity(w), value == w))

    def _pick_activity(self, word: str) -> None:
        self.activity_input.setText(word)
        self._render_chips()

    def activity_words(self) -> list[str]:
        """The activities offered for the form's kind (its buttons 1–9)."""
        return self._activity_words()

    def pick_activity(self, word: str) -> None:
        """An activity chosen, as when its button is pressed."""
        self._pick_activity(word)

    def _chip(self, n: int) -> None:
        words = self._activity_words()
        if self.form.isVisible() and 0 < n <= len(words):
            self._pick_activity(words[n - 1])

    # More choices than this at a level go in a drop-down instead of buttons.
    KIND_CHIPS_MAX = 16

    def _kind_changed(self) -> None:
        """The kind typed or chosen; for a new record, also what the next new record starts with."""
        if self.book is not None and self.pending is not None and self.pending[2] is None and self.pending[3] is None:
            self.book.remember(kind=self.kind_input.text())
        self._fit_activity()
        self._render_chips()
        self._render_kind_path()
        self._render_candidates()

    def kind_level_rows(self) -> list[tuple[str, list[str], str]]:
        """The kind selector's rows: (the level's name, its choices, the chosen one or "")."""
        if self.book is None:
            return []
        taxa = self.book.taxonomy()
        path = path_of(self.kind_input.text())
        rows = []
        for d in range(len(path) + 1):
            choices = [t.path[-1] for t in children_of(taxa, path[:d])]
            chosen = path[d] if d < len(path) else ""
            if chosen and chosen not in choices:
                choices.append(chosen)
            if not choices:
                break
            name = level_name(taxa, path, d) or ("大分類" if d == 0 else f"第 {d + 1} 階層")
            rows.append((name, choices, chosen))
        return rows

    def choose_kind(self, path: list[str]) -> None:
        self.kind_input.setText(kind_of(path))
        self._kind_changed()

    def _render_kind_path(self) -> None:
        """The kind's levels, one row each from 大分類 down: a click picks at that level, a click on the pressed one clears it;
        then the class's known individuals."""
        if self.kind_levels is not None:
            self.kind_levels.setParent(None)
            self.kind_levels.deleteLater()
            self.kind_levels = None
        if self.book is None:
            self.kind_path_box.hide()
            return
        taxa = self.book.taxonomy()
        path = path_of(self.kind_input.text())
        known = {kind_of(t.path) for t in taxa}
        box = QWidget()
        grid = QGridLayout(box)
        grid.setContentsMargins(0, 0, 0, 0)
        grid.setColumnStretch(1, 1)
        row = 0

        def add_row(label: str, widget: QWidget) -> None:
            nonlocal row
            caption = QLabel(f"{label}:")
            caption.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignTop)
            grid.addWidget(caption, row, 0)
            grid.addWidget(widget, row, 1)
            row += 1

        for d, (name, choices, chosen) in enumerate(self.kind_level_rows()):
            up = path[:d]
            if len(choices) > self.KIND_CHIPS_MAX:
                combo = QComboBox()
                combo.addItem(self.tr("（指定なし）"), "")
                for c in choices:
                    combo.addItem(c, c)
                combo.setCurrentIndex(max(0, combo.findData(chosen)))
                combo.setToolTip(f"{name}の選択")
                combo.activated.connect(lambda i, combo=combo, up=up: self.choose_kind(up + ([combo.itemData(i)] if combo.itemData(i) else [])))
                add_row(name, combo)
                continue
            chips = QWidget()
            flow = FlowLayout(chips)
            for c in choices:
                pressed = c == chosen
                outside = kind_of([*up, c]) not in known
                tip = (f"{name}の選択の解除" if pressed else f"{kind_of([*up, c])} を選択") + ("（分類体系にない）" if outside else "")
                flow.addWidget(self._chip_button(c, tip, lambda p=up + ([] if pressed else [c]): self.choose_kind(p), pressed))
            add_row(name, chips)
        label = name_label_of(taxa, path)
        here = find(taxa, path)
        members = (here.members if here else None) or []
        if members:
            chips = QWidget()
            flow = FlowLayout(chips)
            typed = self.object_input.text().strip()
            for m in members[:24]:
                flow.addWidget(self._chip_button(m, f"{m} として記録", lambda m=m: (self.object_input.setText(m), self._object_typed()), typed == m))
            add_row(label or "個体", chips)
        self.kind_levels = box
        self.kind_path_layout.addWidget(box)
        self.kind_path_box.setVisible(row > 0)
        self.object_label.setText(f"モノ（{label}）" if label else "モノ")
        self._render_specs(taxa, path)
        self._render_measure()

    def _render_specs(self, taxa, path: list[str]) -> None:
        specs = specs_at(taxa, path) if path else {}
        fields = {f.name: f for f in self.book.spec_fields}
        order = [*[f.name for f in self.book.spec_fields], *[k for k in specs if k not in fields]]
        text = "・".join(f"{k} {spec_text(fields.get(k), specs[k][0])}" for k in order if k in specs)
        self.specs_label.setText(text)
        self.specs_label.setVisible(bool(text))

    def _render_measure(self) -> None:
        """The measured length and width against the class's 全長 and 全幅, when known."""
        import re

        path = path_of(self.kind_input.text())
        specs = specs_at(self.book.taxonomy(), path) if self.book and path else {}
        parts = []
        for edit, name in ((self.length_input, "全長"), (self.width_input, "全幅")):
            try:
                size = read_number(edit.text()) or 0
            except ValueError:
                size = 0
            spec = specs.get(name)
            try:
                full = float(re.sub(r"[^\d.]", "", spec[0])) if spec else 0
            except ValueError:
                full = 0
            if size > 0 and full > 0:
                parts.append(f"{name} {spec[0]} m（{spec[1][-1]}）の {js_round(size / full * 100)}%")
        self.measure_note.setText("・".join(parts))

    def _object_typed(self) -> None:
        """A known thing's name typed: its kind and latest activity come with it; a known individual's name brings its class."""
        if self.book is None or not self.form.isVisible():
            return
        name = self.object_input.text().strip()
        flow = self.book.flow_by_name(name)
        if flow is None:
            member = member_index(self.book.taxonomy()).get(name)
            if member:
                self.kind_input.setText(kind_of(member))
                self._render_kind_path()
            self._render_candidates()
            return
        self.kind_input.setText(flow.kind)
        self._render_kind_path()
        self._fit_activity()
        if not self.activity_input.text():
            self.activity_input.setText(flow.activity)
        self._render_chips()
        self._render_candidates()

    def _number(self, edit: QLineEdit) -> Optional[float]:
        try:
            return read_number(edit.text())
        except ValueError:
            return None

    def _render_candidates(self) -> None:
        """The things offered as the same one, under 「モノ」 in a new record's form."""
        self._clear(self.candidates_layout)
        p = self.pending
        if self.book is None or p is None or p[2] is not None or p[3] is not None:
            self.offered = []
            self.candidates_box.hide()
            return
        self.offered = self.book.candidates(p[0], p[1], self.kind_input.text(), self._number(self.length_input), self._number(self.heading_input))
        typed = self.object_input.text().strip()
        self.candidates_layout.addWidget(QLabel(self.tr("同一候補:")))
        for i, c in enumerate(self.offered):
            tip = f"{c.kind or '種別なし'}・{'・'.join(c.reasons)}・{local_time(c.source.image_time)} の記録（Alt+{i + 1}）"
            self.candidates_layout.addWidget(self._chip_button(f"{i + 1} {c.name}", tip, lambda c=c: self.pick_candidate(c), typed == c.name))
        self.candidates_box.setVisible(bool(self.offered))

    def pick_candidate(self, c: MatchCandidate) -> None:
        """A candidate chosen: the record is of that thing (its kind and activity come with it)."""
        self.object_input.setText(c.name)
        self._object_typed()
        self.submit_button.setFocus()

    def _submit_key(self) -> None:
        """Ctrl+Enter: the open form saved, wherever in the dock the focus is (Enter alone only in its text boxes)."""
        if self.form.isVisible():
            self.submit(True)
        elif self.area_form.isVisible():
            self._submit_area()
        else:
            self.many.entered()

    def map_entered(self) -> bool:
        """Enter with the map focused (the dock's keys are not active then): the open form or area saved."""
        if self.form.isVisible() and self.pending is not None:
            self.submit(True)
            return True
        if self.area_mode and self.area_form.isVisible():
            self._submit_area()
            return True
        return self.many.entered()

    def _candidate_key(self, n: int) -> None:
        if self.form.isVisible() and 0 < n <= len(self.offered):
            self.pick_candidate(self.offered[n - 1])

    def _fill_confidence(self, value: str) -> None:
        self.confidence_select.blockSignals(True)
        self.confidence_select.clear()
        self.confidence_select.addItem(self.tr("確度なし"), "")
        levels = self.book.confidences() if self.book else []
        for c in levels:
            self.confidence_select.addItem(c.label, c.label)
            if c.note:
                self.confidence_select.setItemData(self.confidence_select.count() - 1, c.note, Qt.ItemDataRole.ToolTipRole)
        if value and not any(c.label == value for c in levels):
            self.confidence_select.addItem(value, value)
        self.confidence_select.setCurrentIndex(max(0, self.confidence_select.findData(value)))
        self.confidence_select.blockSignals(False)
        self._render_confidence()

    def _render_confidence(self) -> None:
        value = self.confidence_select.currentData()
        level = next((c for c in (self.book.confidences() if self.book else []) if c.label == value), None)
        self.confidence_select.setToolTip(level.note or "" if level else "")
        self.confidence_select.setStyleSheet(f"QComboBox {{ border-left: 4px solid {level.color}; }}" if level and level.color else "")
