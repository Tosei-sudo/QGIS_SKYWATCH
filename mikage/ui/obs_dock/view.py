"""
観測記録ドックの表示 (ui/obs_dock, docs/architecture.md 段階7): what the dock shows: the list, the idle note, the
image's records, an object's flow. A part of ObservationDock (ui/observation_dock.py): its methods as
they were, on the dock's widgets and state.
"""
from __future__ import annotations

from typing import Optional

from qgis.core import QgsCoordinateReferenceSystem, QgsGeometry, QgsPointXY, QgsProject
from qgis.PyQt.QtCore import Qt
from qgis.PyQt.QtGui import QColor
from qgis.PyQt.QtWidgets import QHeaderView, QTableWidgetItem

from ...core.obs.api import (
    area_label, local_time, CarryOver, flow_of, heading_text, time_key, km_text, is_mine, my_names, Observation, state_text,
    unseen_label, kind_of,
)
from ...core import edit_stamp
from ..obs_overlay import UNSEEN_COLOR
from .common import NO_IMAGE


class ViewPart:
    """観測記録ドックの表示."""

    def render(self) -> None:
        """Shows the book as it is now: with no image chosen, only a note (and the records not read)."""
        self.render_drafts()
        self._show_footprints()
        book = self.book
        image = book.current if book else None
        # The images.
        self.image_select.blockSignals(True)
        self.image_select.clear()
        names = [e.name for e in book.images] if book else []
        for e in book.images if book else []:
            files = len(e.layer_ids)
            notes = [x for x in (e.satellite, "ファイルなし" if not e.file else f"{files} ファイル" if files > 1 else "") if x]
            twin = QgsProject.instance().mapLayer(e.layer_id) if names.count(e.name) > 1 and e.layer_id else None
            if twin is not None:
                notes.append(twin.name())  # files of the same name in other folders: told apart by their layers
            note = f"（{'・'.join(notes)}）" if notes else ""
            self.image_select.addItem(f"{local_time(e.time) if e.time is not None else '日時不明'}  {e.name}{note}")
            self.image_select.setItemData(self.image_select.count() - 1, self._image_tip(e), Qt.ItemDataRole.ToolTipRole)
        if book and image in book.images:
            self.image_select.setCurrentIndex(book.images.index(image))
        self.image_select.blockSignals(False)
        many = bool(book and len(book.images) > 1)
        self.prev_button.setEnabled(many)
        self.next_button.setEnabled(many)
        self.time_row.setVisible(image is not None and image.time is None)
        self.satellite_row.setVisible(image is not None and not image.satellite and book.keeps_satellite())
        self.image_select.setToolTip(self._image_tip(image))
        self.assignment_bar.show_image(image)
        self.image_event_button.setVisible(image is not None and self.context.config.events is not None)
        self.clear_button.setEnabled(bool(book and self.store_set and not self.store_set.remote and book.can_clear()))
        self.only.setEnabled(image is not None)
        self.list_button.setEnabled(book is not None)
        if book is None or image is None:
            self._render_idle()
            return
        if self._idle is not False:
            self._idle = False
            self.idle_note.hide()
            self.work_area.show()
        if not book.loaded:
            # An image chosen: its records (and the others', for what carries over) are read now.
            self._read_records()
            if book.loaded:
                return  # the local store: read and shown at once
        elif book.needs_around():
            # Another image of a shared store: its records and those around it are read now (shown once read).
            self._read_around()
        if not self.status.text() and book.status:
            self.status.setText(book.status)
        # Only what the list shows (大分類), which is all 「すべて継続」「すべて消失」 and 「確認範囲内…を消失」 record.
        shown_carry = self._carry_shown()
        carried = [c for c in book.carried() if shown_carry is None or shown_carry(c)]
        inside = book.inside_carried(carried)
        hidden = book.unseen_carried(carried)
        self.carry_label.setText(self.tr("前後の画像のモノ {n} 件").format(n=len(carried)) if carried else "")
        # Those in a 不可範囲 are left to 「不可範囲内 n 件を不可に」; all in checked areas: 「確認範囲内 n 件を消失に」 does the same.
        rest = len(carried) - len(inside) - len(hidden)
        self.carry_all_button.setVisible(rest > 0)
        self.absent_all_button.setVisible(rest > 0)
        self.absent_inside_button.setVisible(bool(inside))
        self.absent_inside_button.setText(self.tr("確認範囲内 {n} 件を消失に").format(n=len(inside)))
        self.unseen_inside_button.setVisible(bool(hidden))
        self.unseen_inside_button.setText(self.tr("不可範囲内 {n} 件を不可に").format(n=len(hidden)))
        self.unseen_area_button.setVisible(book.keeps_unseen_areas())
        shown = book.records if self.deleted_check.isChecked() else book.living()
        here = sorted((o for o in shown if image and o.image_id == image.id), key=lambda o: o.object_name)
        objects = book.flows()
        taxa = book.taxonomy()
        every = len([o for o in here if o.deleted_at is None])
        # What the form offers.
        self.objects_model.setStringList(list(dict.fromkeys(f.name for f in objects if f.name)))
        kinds = list(dict.fromkeys(k for k in [*book.vocabulary.kinds, *(kind_of(t.path) for t in taxa), *(f.kind for f in objects)] if k))
        self.kinds_model.setStringList(kinds)
        self.area_kinds_model.setStringList(kinds)
        self.activities_model.setStringList(book.activity_words(self.kind_input.text()))
        self._render_root_filter(taxa)
        deeper = bool(self.class_filter) and len(self.class_filter) > 1
        self.class_filter_label.setText(self.tr("分類: {k} 以下").format(k=kind_of(self.class_filter)) if deeper else "")
        self.unfilter_button.setVisible(bool(self.class_filter))
        if self.class_filter:
            here = [o for o in here if self._shown_kind(o.kind, taxa)]
        if self.mine_only.isChecked():
            mine = self.my_names()
            here = [o for o in here if is_mine(o.recorded_by, mine)]
        listed = len([o for o in here if o.deleted_at is None])
        self.count_label.setText(self.tr("この画像 {n} 件").format(n=every) if listed == every else
                                 self.tr("この画像 {n} / {all} 件").format(n=listed, all=every))
        self.count_label.setToolTip(self.tr("この画像の記録の件数（大分類・「自分の記録のみ」で絞り込み中は 表示 / 全体）"))
        shown_as = ("image", image.id if image is not None else None)
        kept = self._chosen_rows() if shown_as == self._shown_as else None
        scrolled = self.table.verticalScrollBar().value()
        # The columns sized once when filled: sized to the contents as each cell is set, a list of a few hundred
        # rows (a car park: 2026-11-18, 300 rows took 20 s) costs a pass over every row per cell.
        header = self.table.horizontalHeader()
        header.setSectionResizeMode(QHeaderView.ResizeMode.Interactive)
        try:
            self._render_image(here, carried, inside, hidden)
        finally:
            header.setSectionResizeMode(QHeaderView.ResizeMode.ResizeToContents)
        self._shown_as = shown_as
        if kept is not None:
            self._choose_rows(*kept)
            # Where the list was scrolled to, too (shown anew, it starts at the top).
            self.table.updateGeometries()
            self.table.verticalScrollBar().setValue(scrolled)
        self._render_flow()
        self.many.follow_book()
        self._show_offline()
        self.changed.emit()
        self.draw()

    def _render_idle(self) -> None:
        """
        No image chosen (or no store): only a note (tsudo 2026-11-10). What was
        being done on an image ends; the lists, the forms and recording wait
        for an image.
        """
        if self._idle is False:
            self.status.clear()  # said of the image no longer chosen
        self._idle = True
        if self.area_mode:
            self.set_area_mode(False)
        if self.many.mode:
            self.set_many_mode("")
        if self.active:
            self.set_active(False)
        self.close_form()
        self.work_area.hide()
        self._shown_as = None
        self._render_idle_note()
        self.idle_note.show()
        self.changed.emit()
        self.draw()

    def _render_idle_note(self) -> None:
        tr = self.tr
        if self.book is None:
            text = tr("保存先なし") if self.context.config.local_observations else \
                tr("保存先なし（管理者に連絡: observationStores）［E703］")
        else:
            text = tr("画像が未選択\n"
                      "画像カタログで画像を開くか、「ファイルなしで記録」を押すと、ここに記録の一覧と入力欄を表示")
        said = self.status.text()
        self.idle_note.setText(text + (f"\n\n{said}" if said and said != NO_IMAGE else ""))

    def _render_image(self, here: list[Observation], carried: list[CarryOver], inside: list[CarryOver],
                      hidden: Optional[list[CarryOver]] = None) -> None:
        tr = self.tr
        self._set_head(tr("モノ"), tr("種別"), tr("活動"), tr("長さ（× 幅）・向き"), tr("備考"), tr("記録者"), tr("更新"), "")
        book = self.book
        mine = self.my_names() if self.mine_only.isChecked() else None

        def measure(o: Observation) -> str:
            if o.length is None:
                return ""
            size = f"{km_text(o.length)} × {km_text(o.width)}" if o.width is not None else km_text(o.length)
            return f"{size}・{heading_text(o.heading)}"

        waiting = self.waiting_ids()
        for o in here:
            gone = o.deleted_at is not None
            widget = self._small((tr("復元"), tr("削除した記録を復元"), lambda o=o: self.restore([o]))) if gone else \
                self._small((tr("流れ"), tr("このモノの流れ（帯グラフ・表・地図の軌跡）を一覧の下に表示。もう一度押すと閉じる"),
                             lambda o=o: self.toggle_flow(o.object_id), o.object_id == self.chosen))
            self._row([o.object_name, o.kind, o.activity if o.present else tr(state_text(o)), measure(o), o.note, self._made_by(o),
                       local_time(o.updated_at), ""],
                      None if gone else (lambda o=o: (self._ensure_active(), self.open_form(o.lon, o.lat, editing=o), self._center(o))),
                      style="deleted" if gone else "unseen" if o.unseen else "", widget=widget, records=[] if gone else [o], reported=o.id in self.reported,
                      waiting=o.id in waiting, key=f"o:{o.id}")
        inside_ids = {id(c) for c in inside}
        hidden_ids = {id(c) for c in hidden or []}
        for c in carried:
            s = c.source
            widget = self._small((tr("継続"), tr("この画像でも継続として記録"), lambda s=s: self.carry(s.object_id, True)),
                                 (tr("消失"), tr("この画像での消失を記録"), lambda s=s: self.carry(s.object_id, False)))
            side = tr("前") if c.side == "before" else tr("後")
            self._row([s.object_name, s.kind, f"{s.activity}（{side}の画像 {local_time(s.image_time)}）", measure(s),
                       tr("不可範囲内（記録なし）") if id(c) in hidden_ids else tr("確認範囲内（記録なし）") if id(c) in inside_ids else "",
                       s.recorded_by, "", ""],
                      lambda s=s: (self._ensure_active(), self.open_form(s.lon, s.lat, source=s), self._center(s)),
                      style="unseen" if id(c) in hidden_ids else "inside" if id(c) in inside_ids else "ghost", widget=widget, key=f"c:{s.object_id}")
        taxa = book.taxonomy() if self.class_filter else []
        for a in book.image_areas(deleted=self.deleted_check.isChecked()):
            if a.kind and not self._shown_kind(a.kind, taxa):
                continue
            if mine and not is_mine(a.recorded_by, mine):
                continue
            gone = a.deleted_at is not None
            what = tr("不可範囲") if a.unseen else tr("確認範囲")
            widget = self._small((tr("復元"), tr("削除した{what}を復元").format(what=what), lambda a=a: self.restore_areas([a]))) if gone else \
                self._small((tr("削除"), tr("この不可範囲を削除（中の「不可」の記録も削除）") if a.unseen else tr("この確認範囲を削除"),
                             lambda a=a: self.remove_areas([a])))
            self._row([area_label(a), a.kind, "", "", a.note, a.recorded_by, local_time(a.updated_at), ""], lambda a=a: self._fit_area(a),
                      style="deleted" if gone else "unseen" if a.unseen else "area", widget=widget, key=f"a:{a.id}")
        if self.table.rowCount() == 0:
            self._row([tr("自分の記録なし。「自分の記録のみ」を外すと全員の記録を表示") if mine else
                       tr("該当する記録なし（大分類の絞り込み中）") if self.class_filter else tr("記録なし。「クリック記録」を押して地図をクリック")])

    def _made_by(self, o: Observation) -> str:
        """「記録者」 of a row: who made the record, and who changed it last when someone else did."""
        made = o.recorded_by.strip()
        by = o.updated_by.strip()
        if by and made and by.casefold() != made.casefold() and o.updated_at > o.created_at:
            return self.tr("{made}（更新 {by}）").format(made=made, by=by)
        return made or by

    def my_names(self) -> list[str]:
        """Oneself: the 「記録者」 name and the user name the edits are stamped with (分担 holds it too)."""
        return my_names(self._observer(), edit_stamp.user_name())

    def _ensure_active(self) -> None:
        if not self.active:
            self.set_active(True, quiet=True)

    def _render_flow(self) -> None:
        book = self.book
        rows = [o for o in book.living() if o.object_id == self.chosen] if self.chosen else []
        show = bool(rows)
        self.flow_box.setVisible(show)
        if not show:
            return
        flow = flow_of(rows)
        tr = self.tr
        self.flow_box.setTitle(f"{flow.name}（{flow.kind or tr('種別なし')}・{flow.object_id}）")
        latest = (flow.activity or tr("活動の記録なし")) if flow.present else tr("消失")
        if flow.unseen:
            latest += tr("（最新の画像では{u}）").format(u=unseen_label(flow.unseen))
        self.flow_summary.setText(f"{local_time(flow.first)} 〜 {local_time(flow.last)}・{flow.seen} 画像で確認・移動 {km_text(flow.distance)}・最新: {latest}")
        self.flow_strip.set_flow(flow)
        self.steps_table.setRowCount(0)
        self._steps = flow.steps
        for s in flow.steps:
            r = self.steps_table.rowCount()
            self.steps_table.insertRow(r)
            cells = [local_time(s.start) if s.start == s.end else f"{local_time(s.start)} 〜 {local_time(s.end)}",
                     tr(unseen_label(s.unseen)) if s.unseen else (s.activity or "—") if s.present else tr("消失"), str(s.count),
                     km_text(s.moved) if s.moved is not None and s.moved > 0 else ""]
            for i, c in enumerate(cells):
                item = QTableWidgetItem(c)
                if s.unseen:
                    item.setForeground(QColor(UNSEEN_COLOR))
                elif not s.present:
                    item.setForeground(QColor("#dc2626"))
                self.steps_table.setItem(r, i, item)
        if self.flow_box.property("object") != flow.object_id:
            self.flow_box.setProperty("object", flow.object_id)
            self.rename_input.setText(flow.name)
            self.rekind_input.setText(flow.kind)
            self.remove_all_button.setProperty("armed", False)
            self.remove_all_button.setText(tr("このモノの記録を削除"))
        self.merge_select.clear()
        self.merge_select.addItem(tr("統合先のモノ…"), "")
        for f in book.flows():
            if f.object_id != flow.object_id:
                self.merge_select.addItem(f"{f.name}（{f.kind}・{local_time(f.last)}）", f.object_id)

    def _step_clicked(self, row: int, _col: int) -> None:
        """
        A row of the thing's flow clicked: its image shown and its record
        flashed on the map, nothing more (tsudo 2026-11-18 asked why a click
        there started 「クリック記録」 with the record in the form: a look at
        the flow is not an edit). A double click opens the record.
        """
        steps = getattr(self, "_steps", [])
        if not 0 <= row < len(steps):
            return
        o = steps[row].observations[0]
        if self.go_to_record(o):
            if 0 <= row < self.steps_table.rowCount():
                self.steps_table.selectRow(row)  # the flow was shown anew for the other image
            self._flash(o)
            self.say(self.tr("{when} の画像へ移動。ダブルクリックでこの記録を編集").format(when=local_time(time_key(o))))

    def _step_double_clicked(self, row: int, _col: int) -> None:
        steps = getattr(self, "_steps", [])
        if 0 <= row < len(steps):
            self.open_record(steps[row].observations[0])

    def _flash(self, o: Observation) -> None:
        """A record's place flashed on the map."""
        try:
            self.canvas.flashGeometries([QgsGeometry.fromPointXY(QgsPointXY(o.lon, o.lat))], QgsCoordinateReferenceSystem("EPSG:4326"))
        except Exception:  # noqa: BLE001 (only a hint)
            pass
