"""
観測記録ドックの入力欄 (ui/obs_dock, docs/architecture.md 段階7): the form of a record: its kind, activity, numbers,
candidates and confidence, and its submission. A part of ObservationDock (ui/observation_dock.py): its
methods as they were, on the dock's widgets and state.
"""
from __future__ import annotations

import re
from dataclasses import asdict
from typing import Callable, Optional

from qgis.PyQt.QtCore import Qt
from qgis.PyQt.QtWidgets import QLabel, QLineEdit, QToolButton, QWidget

from ...core.obs.api import (
    FormValues, local_time, MatchCandidate, js_round, UNSEEN, Observation, unseen_label, find, kind_of, member_names,
    member_specs_at, name_label_of, origin_text, path_of, quantity_of, read_quantity, spec_text, specs_at,
)
from ...core.util import read_number
from ..docks import plain_tip
from ..flow_layout import FlowLayout
from ..kind_levels import chip_button, kind_level_rows


class FormPart:
    """観測記録ドックの入力欄."""

    def open_form(self, lon: float, lat: float, editing: Optional[Observation] = None, source: Optional[Observation] = None,
                  measured: Optional[tuple] = None) -> None:
        """Opens the form for a new record at a place, a record to change, or a thing carried over."""
        if self.book is None:
            return
        self.pending = (lon, lat, editing, source)
        #: The image the form is about (its record goes there, asked about when the image changes: _keep_typing).
        self._form_image = self.book.current
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
        # The AI result the record came from (a record changed keeps it; a thing carried over by hand is the person's).
        origin = origin_text(editing.ai) if editing is not None else ""
        self.origin_text.setText(origin)
        for w in (self.origin_label, self.origin_text):
            w.setVisible(bool(origin))
        self._fill_confidence((known.confidence or "") if known else last.confidence)
        # 数量: a group's (継続 too, from the record carried over); a new record is of one thing until typed.
        many = quantity_of(known) if known else 1
        self.quantity_input.setText(str(many) if many >= 2 else "")
        for w in (self.quantity_label, self.quantity_row):
            w.setVisible(self.book.keeps_quantity())
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
        typed = self.object_input.text().strip()
        self._render_member_hint(typed, self.book.find_member(typed) if typed else None)
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
        self._form_image = None
        self.form.hide()
        self.many.render()
        self.offered = []
        self._clear(self.candidates_layout)
        self.candidates_box.hide()
        self.draw()

    def form_values(self) -> FormValues:
        return FormValues(object=self.object_input.text(), kind=self.kind_input.text(), activity=self.activity_input.text(),
                          note=self.note_input.text(), confidence=self.confidence_select.currentData() or "",
                          length=self.length_input.text(), heading=self.heading_input.text(), width=self.width_input.text(),
                          quantity=self.quantity_input.text() if self.quantity_input.isVisibleTo(self.form) else "")

    def _unseen_reason(self) -> str:
        """Why the thing in the form could not be seen: the 不可範囲's reason where it is, else the first offered."""
        if self.book is None or self.pending is None:
            return UNSEEN
        lon, lat = self.pending[0], self.pending[1]
        reasons = self.book.vocabulary.unseen_reasons
        return self.book.unseen_reason_at(lon, lat) or (reasons[0] if reasons else UNSEEN)

    def submit(self, present: bool = True, unseen: str = "", image=None) -> None:
        """
        Records what the form says (a new record, a change, or a thing carried
        over; `unseen`: 「不可」, why) on the image it was opened on (`image`:
        that one, the image recorded on now otherwise), whatever image is
        recorded on when a shared store gets to it.
        """
        if self.book is None or self.pending is None:
            return
        current, opened_on = self.book.current, self._form_image
        target = image if image is not None else (
            current if opened_on is None or (current is not None and current.id == opened_on.id) else opened_on)
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
                "記録の保存に失敗。送信待ちに保管: {m}").format(m=message)))

        if self.holds():
            # A shared store: listed and drawn now (送信中), sent behind the screen (a slow server, tsudo 2026-11-28);
            # a typed name of an individual not read here is looked for as it is sent.
            held = self.hold(lambda b: b.hold_submit(lon, lat, values, present, editing, source, draft=draft or None, unseen=unseen,
                                                     image=target))
            if held is not None:
                self.many.use_record(held.record)
            return
        self.do(self.tr("記録"), lambda b: b.submit(lon, lat, values, present, editing, source, draft=draft or None, unseen=unseen, image=target),
                lambda saved: lost(self.status.text()) if saved is None else self.many.use_record(saved), watch=True, failed=lost)

    def _numbers_readable(self) -> bool:
        """
        The typed length, width, heading and 数量 can be read (full-width
        digits and a unit such as 120 m, 30° or 30 台 can; a 数量 is a whole
        number of 1 or more): otherwise the form stays open and says which,
        rather than saving the record without it (or, editing, dropping the
        value it had).
        """
        if self.quantity_input.isVisibleTo(self.form):
            try:
                read_quantity(self.quantity_input.text())
            except ValueError:
                self.measure_note.setText(self.tr("数量は 1 以上の整数で入力（「{text}」は読めない）").format(text=self.quantity_input.text().strip()))
                self.measure_note.setVisible(True)
                self.quantity_input.setFocus()
                self.quantity_input.selectAll()
                return False
        for edit, label in ((self.length_input, self.length_label), (self.width_input, self.width_label), (self.heading_input, self.heading_label)):
            try:
                read_number(edit.text())
            except ValueError:
                self.measure_note.setText(self.tr("{label}は数字で入力（「{text}」は読めない）").format(label=label.text(), text=edit.text().strip()))
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
        self.quantity_input.setText(values.quantity)
        for edit, text in ((self.length_input, values.length), (self.heading_input, values.heading), (self.width_input, values.width)):
            edit.setText(text)
            if text:
                edit.setVisible(True)
        self._render_chips()
        self._render_kind_path()
        kept = self.book.draft(self._draft_id) if self._draft_id else None
        if kept is not None and kept.values == asdict(values):
            # Kept in 「保存できなかった観測記録」 as typed: closed unchanged, nothing asked (tsudo 2026-11-28).
            self._filled = self.form_values()

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
        return chip_button(text, tip, run, pressed)

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
        return kind_level_rows(self.book.taxonomy(), self.kind_input.text())

    def choose_kind(self, path: list[str]) -> None:
        self.kind_input.setText(kind_of(path))
        self._kind_changed()

    def _render_kind_path(self) -> None:
        """The kind's levels, one row each from 大分類 down (ui/kind_levels.py: a click picks at that level, a click on the
        pressed one clears it); then the class's known individuals."""
        if self.book is None:
            self.kind_levels.clear()
            self.kind_path_box.hide()
            return
        taxa = self.book.taxonomy()
        path = path_of(self.kind_input.text())
        self.kind_levels.render(taxa, self.kind_input.text())
        label = name_label_of(taxa, path)
        here = find(taxa, path)
        members = (here.members if here else None) or []
        if members:
            chips = QWidget()
            flow = FlowLayout(chips)
            typed = self.object_input.text().strip()
            for m in members[:24]:
                others = member_names(here, m, self.book.spec_fields)
                own = {k: v for k, v in (here.member(m).specs or {}).items() if k not in ("別名",)}
                tip = f"{m} として記録" + (f"（別名: {'、'.join(others)}）" if others else "")
                if own:
                    tip += "\n" + "・".join(f"{k} {v}" for k, v in list(own.items())[:6])
                flow.addWidget(self._chip_button(m, tip, lambda m=m: (self.object_input.setText(m), self._object_typed()), typed == m))
            self.kind_levels.add_row(label or "個体", chips)
        self.kind_path_box.setVisible(self.kind_levels.rows > 0)
        self.object_label.setText(f"モノ（{label}）" if label else "モノ")
        self._render_specs(taxa, path)
        self._render_measure()

    def _member_typed(self, path: list[str]) -> Optional[str]:
        """The known individual of the class at `path` whose name (or other name) is in 「モノ」, else None."""
        hit = self.book.find_member(self.object_input.text()) if self.book and path else None
        return hit.name if hit is not None and kind_of(hit.path) == kind_of(path) else None

    def _specs_here(self, taxa, path: list[str]) -> dict:
        """The specifications shown: the known individual's own first (when 「モノ」 names one of the class), then the class's."""
        if not path:
            return {}
        member = self._member_typed(path)
        return member_specs_at(taxa, path, member) if member else specs_at(taxa, path)

    def _render_specs(self, taxa, path: list[str]) -> None:
        specs = self._specs_here(taxa, path)
        fields = {f.name: f for f in self.book.spec_fields}
        member = self._member_typed(path)
        own = [k for k in specs if member and specs[k][1][-1:] == [member]]
        order = [*[f.name for f in self.book.spec_fields if f.name in own], *[k for k in own if k not in fields],
                 *[f.name for f in self.book.spec_fields if f.name not in own], *[k for k in specs if k not in fields and k not in own]]
        text = "・".join(f"{k} {spec_text(fields.get(k), specs[k][0])}" for k in order if k in specs)
        self.specs_label.setText(text)
        self.specs_label.setVisible(bool(text))

    def _render_measure(self) -> None:
        """The measured length and width against the class's 全長 and 全幅, when known."""
        import re

        path = path_of(self.kind_input.text())
        specs = self._specs_here(self.book.taxonomy(), path) if self.book and path else {}
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
        """A known thing's name typed: its kind and latest activity come with it; a known individual's name (or one of its
        other names) brings its class, and an other name is offered its individual's own name (one click)."""
        if self.book is None or not self.form.isVisible():
            return
        name = self.object_input.text().strip()
        # 「いずも（DDH-183）」 chosen from the list: the individual's own name.
        picked = re.match(r"^(.+)（(.+)）$", name)
        if picked:
            hit = self.book.find_member(picked.group(2))
            if hit is not None and hit.name == picked.group(1).strip():
                name = hit.name
                self.object_input.setText(name)
        hit = self.book.find_member(name) if name else None
        self._render_member_hint(name, hit)
        flow = self.book.flow_by_name(name)
        if flow is None:
            if hit is not None and kind_of(hit.path) != self.kind_input.text().strip():
                self.kind_input.setText(kind_of(hit.path))
                self._render_kind_path()
            else:
                path = path_of(self.kind_input.text())
                self._render_specs(self.book.taxonomy(), path)
                self._render_measure()
            self._render_candidates()
            return
        self.kind_input.setText(flow.kind)
        self._render_kind_path()
        self._fit_activity()
        if not self.activity_input.text():
            self.activity_input.setText(flow.activity)
        self._render_chips()
        self._render_candidates()

    def _render_member_hint(self, typed: str, hit) -> None:
        """Under 「モノ」: an other name (or another way of writing) of a known individual typed, with a button for its own name."""
        if hit is None or hit.name == typed:
            self.member_hint.hide()
            return
        if hit.alias:
            text = self.tr("「{t}」は「{n}」の別名（{k}）").format(t=typed, n=hit.name, k=hit.path[-1])
        else:
            text = self.tr("「{t}」は既知の個体「{n}」（{k}）").format(t=typed, n=hit.name, k=hit.path[-1])
        self.member_hint_label.setText(text)
        self.member_hint_button.setText(self.tr("{n} にする").format(n=hit.name))
        self.member_hint_button.setToolTip(self.tr("モノの名前を既知の個体の名前「{n}」に（語彙をそろえる）").format(n=hit.name))
        self._hinted_member = hit.name
        self.member_hint.show()

    def use_member_name(self) -> None:
        """The hint's button: 「モノ」 set to the known individual's own name."""
        name = getattr(self, "_hinted_member", None)
        if name:
            self.object_input.setText(name)
            self._object_typed()

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
