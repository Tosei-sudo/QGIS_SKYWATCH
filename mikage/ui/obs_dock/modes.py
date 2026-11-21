"""
観測記録ドックの記録のしかた (ui/obs_dock, docs/architecture.md 段階7): recording on or off, the points a record takes,
many at once, the checked and unseen areas. A part of ObservationDock (ui/observation_dock.py): its
methods as they were, on the dock's widgets and state.
"""
from __future__ import annotations

from ...qgis_io import settings
from ..docks import plain_tip
from .common import HINT, HINT_POINTS, HINT_AREA, HINT_UNSEEN, NO_IMAGE


class ModesPart:
    """観測記録ドックの記録のしかた."""

    def _use_tool(self, on: bool) -> None:
        if on:
            if self.canvas.mapTool() is not self.tool:
                self._previous_tool = self.canvas.mapTool()
                self.canvas.setMapTool(self.tool)
        elif self.canvas.mapTool() is self.tool:
            self.canvas.unsetMapTool(self.tool)
            if self._previous_tool is not None:
                self.canvas.setMapTool(self._previous_tool)

    def _tool_gone(self) -> None:
        # Another map tool chosen: recording stops.
        if self.area_mode:
            self.set_area_mode(False)
        if self.active:
            self.record_button.setChecked(False)

    def set_active(self, on: bool, quiet: bool = False, claimed: bool = False) -> None:
        """
        Turns 「クリック記録」 on or off. With `assignments`, turning it on takes
        the image first (in a task; recording starts once it is one's own). In
        協業 (the default) others holding it do not matter, and a table that
        cannot be reached only warns; with `mode: "exclusive"` it is refused
        when someone else holds it or the table cannot be reached.
        `claimed`: the taking was tried already (no second try).
        """
        if on and (self.book is None or self.book.current is None):
            self.say(NO_IMAGE)
            on = False
        if on and not claimed and not self.active and self.assignment_bar.needs_claim(self.book.current):
            self._claim_then_record(self.book.current, quiet)
            on = False
        if self.record_button.isChecked() != on:
            self.record_button.blockSignals(True)
            self.record_button.setChecked(on)
            self.record_button.blockSignals(False)
        if on == self.active:
            return
        self.active = on
        self.record_button.setText(self.tr("クリック記録中") if on else self.tr("クリック記録"))
        self._use_tool(on or self.area_mode)
        if not on:
            self._clear_points()
            self.close_form()
            if self.many.mode:
                self.set_many_mode("")
        elif not quiet:
            self.say(HINT_POINTS.get(self.points, HINT))
        self.draw()

    def _claim_then_record(self, image, quiet: bool) -> None:
        """Takes the image (分担), then starts 「クリック記録」 on it; refused: an error, and no recording."""
        bar = self.assignment_bar
        if bar.claiming is not None:
            self.say(self.tr("{name} の担当を取得中…").format(name=bar.claiming.name or bar.claiming.id))
            return
        self.record_button.setEnabled(False)
        self.record_button.setText(self.tr("担当を取得中…"))
        self.say(self.tr("{name} の担当を取得中…").format(name=image.name or image.id))

        def end() -> None:
            self.record_button.setEnabled(True)
            self.record_button.setText(self.tr("クリック記録中") if self.active else self.tr("クリック記録"))

        def done(_x) -> None:
            end()
            # Still the image shown, and the dock still open: recording starts.
            current = self.book.current if self.book is not None else None
            if current is not None and current.id == image.id and self.isVisible():
                self.set_active(True, quiet=True)
                if not quiet:
                    self.say(self.tr("{name} を担当に。").format(name=image.name or image.id) + HINT_POINTS.get(self.points, HINT))

        def failed(message: str) -> None:
            end()
            current = self.book.current if self.book is not None else None
            if bar.shared and current is not None and current.id == image.id and self.isVisible():
                # 協業: nobody is kept out, so the work goes on; only the 分担 row is missing.
                self.set_active(True, quiet=True, claimed=True)
                text = self.tr("分担を記録できませんでした（クリック記録は続けられます）: {m}").format(m=message)
                self.say(text)
                self.iface.messageBar().pushWarning(self.tr("観測記録"), plain_tip(text))
                return
            text = self.tr("クリック記録できません: {m}").format(m=message)
            self.say(text)
            self.iface.messageBar().pushWarning(self.tr("観測記録"), plain_tip(text))

        bar.claim(image, done, failed)

    def set_points(self, n: int) -> None:
        """
        How many clicks a record takes: 1 a point; 2 the rear then the front
        (centre, length, heading); 3 a side too (as wide on the other side);
        4 both sides (the centre halfway between them).
        """
        n = n if n in (1, 2, 3, 4) else 1
        if self.points_select.currentData() != n:
            self.points_select.blockSignals(True)
            self.points_select.setCurrentIndex(n - 1)
            self.points_select.blockSignals(False)
        self.points = n
        self._clear_points()
        settings.set_point_count(n)
        if self.many.mode:
            # Measuring is by the form: 連続・並べる end.
            self.set_many_mode("")
        if n > 1 and not self.active:
            self.set_active(True, quiet=True)
        self.say(HINT_POINTS.get(n, HINT))
        self.draw()

    def use_points(self, n: int) -> None:
        """The clicks a record takes, chosen elsewhere (「担当と個人設定」): only the choice, recording stays as it is."""
        n = n if n in (1, 2, 3, 4) else 1
        self.points_select.blockSignals(True)
        self.points_select.setCurrentIndex(n - 1)
        self.points_select.blockSignals(False)
        if n != self.points:
            self.points = n
            self._clear_points()
            self.draw()
        settings.set_point_count(n)

    def set_many_mode(self, mode: str) -> None:
        """
        「連続」 ('stamp'), 「並べる」 ('row') or neither (''): many alike things
        put down as the 見本 (ui/obs_many.py); either starts 「クリック記録」.
        """
        mode = mode if mode in ("stamp", "row") else ""
        if mode and (self.book is None or self.book.current is None):
            self.say(NO_IMAGE)
            mode = ""
        for button, m in ((self.stamp_button, "stamp"), (self.row_button, "row")):
            if button.isChecked() != (mode == m):
                button.blockSignals(True)
                button.setChecked(mode == m)
                button.blockSignals(False)
        if mode:
            if self.area_mode:
                self.set_area_mode(False)
            self._clear_points()
            self.cursor = None
            # A new record being made is let go, so what the clicks do now is plain (tsudo 2026-11-11); a record
            # (or a thing carried over) open in the form stays, for 「開いている記録を見本に」.
            if self.pending is not None and self.pending[2] is None and self.pending[3] is None:
                self.close_form()
        self.many.set_mode(mode)
        if mode and not self.active:
            self.set_active(True, quiet=True)
            if self.active:
                self.many.set_mode(mode)
        if mode:
            self.reveal(self.many)
        self.draw()

    def set_two_point(self, on: bool) -> None:
        """「2 点」 on (rear then front) or off (a point), as before the 3 and 4 points."""
        self.set_points(2 if on else 1)

    @property
    def two_point(self) -> bool:
        return self.points > 1

    def _clear_points(self) -> None:
        self.rear = None
        self.placed = []

    def set_area_mode(self, on: bool, unseen: bool = False) -> None:
        """
        Turns 「確認範囲」 on (two corners on the map, the view or the image, then its kind) or off;
        `unseen`: 「不可範囲」 instead (then why: 雲・霞…).
        """
        if on and (self.book is None or self.book.current is None):
            self.say(NO_IMAGE)
            on = False
        if on and unseen and not self.book.keeps_unseen_areas():
            self.say(self.tr("保存先に不可の理由の属性（UNSEEN_REASON）がないため、不可範囲は記録できません"))
            on = False
        unseen = on and unseen
        for button, want in ((self.area_button, on and not unseen), (self.unseen_area_button, unseen)):
            if button.isChecked() != want:
                button.blockSignals(True)
                button.setChecked(want)
                button.blockSignals(False)
        self.area_mode = on
        self.area_unseen = unseen
        self.area_ring = None
        self.area_corner = None
        tr = self.tr
        self.area_form.setTitle(tr("不可範囲（雲など）") if unseen else tr("確認範囲"))
        for w in (self.area_kind_label, self.area_kind):
            w.setVisible(not unseen)
        for w in (self.area_reason_label, self.area_reason):
            w.setVisible(unseen)
        self.area_submit_button.setText(tr("不可範囲の記録（Enter）") if unseen else tr("確認範囲の記録（Enter）"))
        self.area_form.setVisible(on)
        self._use_tool(on or self.active)
        if on:
            if self.many.mode:
                self.set_many_mode("")
            self.close_form()
            self._clear_points()
            self.area_kind.clear()
            self.area_note.clear()
            if unseen:
                self.area_reason.clear()
                self.area_reason.addItems(self.book.vocabulary.unseen_reasons)
                self.area_reason.setCurrentIndex(0)
            self.area_image_button.setEnabled(bool(self.book.current.box))
            self.say(self._area_hint())
        self._render_area_range()
        self.draw()

    def _area_hint(self) -> str:
        return HINT_UNSEEN if self.area_unseen else HINT_AREA

    def escape(self) -> None:
        if self.dragging is not None:
            # A record being dragged (Esc on the map): left where it was.
            self.dragging = None
            self.draw()
        elif self.area_mode:
            # A corner clicked: that goes first; then the mode.
            if self.area_corner is not None and self.area_ring is None:
                self.area_corner = None
                self.say(self._area_hint())
                self.draw()
            else:
                self.set_area_mode(False)
        elif self.rear is not None:
            if self.placed:
                self.placed.pop()
                self.say(self.tr("側面の取消") if self.placed else self.tr("前端の取消"))
            else:
                self._clear_points()
                self.say(self.tr("後端の取消"))
            self.draw()
        elif self.form.isVisible():
            self.close_form()
        elif self.many.escape():
            pass
        elif self.list_form.isVisible():
            self.list_button.setChecked(False)
