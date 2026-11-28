"""
観測記録ドックの記録の操作 (ui/obs_dock, docs/architecture.md 段階7): what the book does from the dock: carry over,
absent, remove and restore, filters, going to a record. A part of ObservationDock
(ui/observation_dock.py): its methods as they were, on the dock's widgets and state.
"""
from __future__ import annotations

from typing import Any, Callable, Optional

from qgis.core import QgsRectangle
from qgis.PyQt.QtWidgets import QMessageBox

from ...core.obs.api import (
    CheckedArea, ObservationBook, CarryOver, ObjectFlow, is_mine, Observation, class_by_name, find, kind_of, member_names, path_of,
    within,
)
from ...qgis_io.geometry import from_wgs84


class RecordsPart:
    """観測記録ドックの記録の操作."""

    def ask_clear(self, book: ObservationBook) -> Optional[bool]:
        """Asks what to clear: True only the deleted, False everything, None nothing."""
        living_records = len(book.living())
        gone = len(book.records) - living_records
        areas = len(book.areas)
        text = self.tr("{store} の記録を完全に消去（復元できない）\n\n記録 {n} 件（うち削除済み {d} 件）、確認範囲 {a} 件"
                       "\n分類体系と参考写真は残る").format(store=book.label, n=len(book.records), d=gone, a=areas)
        box = QMessageBox(QMessageBox.Icon.Warning, self.tr("この PC の記録の消去"), text, parent=self)
        everything = box.addButton(self.tr("すべて消去"), QMessageBox.ButtonRole.DestructiveRole)
        deleted = box.addButton(self.tr("削除済みのみ消去"), QMessageBox.ButtonRole.AcceptRole)
        deleted.setEnabled(gone > 0 or any(a.deleted_at is not None for a in book.areas))
        cancel = box.addButton(self.tr("取消"), QMessageBox.ButtonRole.RejectRole)
        box.setDefaultButton(cancel)
        box.exec()
        clicked = box.clickedButton()
        return False if clicked is everything else True if clicked is deleted else None

    def roles_changed(self) -> None:
        """「この PC の記録を消去…」 (cannot be undone): the administrators' (core/roles.py); 「分類体系の編集…」 as its dock."""
        self.clear_button.setVisible(self.allows("observations.clear"))
        self.taxonomy_button.setVisible(self.edit_taxonomy is not None and self.allows("分類体系の編集"))

    def clear_local(self, deleted_only: Optional[bool] = None) -> Optional[tuple[int, int]]:
        """Removes the local records for good, after asking (`deleted_only` given: without asking)."""
        if self.denied("observations.clear"):
            return None
        book = self.book
        if book is None or self.store_set is None or self.store_set.remote or not book.can_clear():
            return None
        if deleted_only is None:
            deleted_only = self.ask_clear(book)
            if deleted_only is None:
                return None
        self.close_form()
        self.chosen = None
        result = self.do(self.tr("この PC の記録の消去"), lambda b: b.clear(deleted_only))
        self.refresh_images()
        return result

    def carry(self, object_id: str, present: bool = True) -> None:
        if self.holds():
            # Listed now (送信中), sent behind the screen (a slow server, tsudo 2026-11-28).
            self.hold(lambda b: b.hold_carry(object_id, present))
            return
        self.do(self.tr("継続の記録"), lambda b: b.carry(object_id, present), watch=True)

    def _carry_shown(self) -> Optional[Callable[[CarryOver], bool]]:
        """Which carried-over things the list shows (None: all), so 「すべて継続」「すべて消失」 leave the hidden ones alone."""
        mine = self.my_names() if self.mine_only.isChecked() else None
        if (not self.class_filter and not mine) or self.book is None:
            return None
        taxa = self._once("taxonomy", self.book.taxonomy) if self.class_filter else []
        return lambda c: (not self.class_filter or self._shown_kind(c.source.kind, taxa)) and \
            (not mine or is_mine(c.source.recorded_by, mine))

    def carry_all(self) -> None:
        only = self._carry_shown()
        self.do(self.tr("すべて継続"), lambda b: b.carry_all(only), watch=True)

    def absent_all(self) -> None:
        only = self._carry_shown()
        self.do(self.tr("すべて消失"), lambda b: b.absent_all(only))

    def absent_inside(self) -> None:
        only = self._carry_shown()
        self.do(self.tr("確認範囲内の消失"), lambda b: b.absent_inside(only))

    def mark_unseen(self) -> None:
        """「不可範囲内 n 件を不可に」: the things carried over into a 不可範囲, recorded 不可 (those the list shows)."""
        only = self._carry_shown()
        self.do(self.tr("不可範囲内の不可"), lambda b: b.mark_unseen(only))

    def remove(self, rows: list[Observation]) -> None:
        self.do(self.tr("記録の削除"), lambda b: b.remove(rows))

    def restore(self, rows: list[Observation]) -> None:
        self.do(self.tr("記録の復元"), lambda b: b.restore(rows), watch=True)

    def remove_areas(self, rows: list[CheckedArea]) -> None:
        self.do(self.tr("確認範囲の削除"), lambda b: b.remove_areas(rows))

    def restore_areas(self, rows: list[CheckedArea]) -> None:
        self.do(self.tr("確認範囲の復元"), lambda b: b.restore_areas(rows))

    def choose(self, object_id: Optional[str]) -> None:
        """
        Shows a thing's flow under the list, and its track on the map (None:
        closes it). A shared store's thing: its whole flow is read first (only
        the records around the images were).
        """
        if object_id != self.chosen:
            self.flow_more.set_open(False)
        self.chosen = object_id or None
        book = self.book
        if book is not None and book.scoped and self.chosen and (id(book), self.chosen) not in self._whole_flows:
            self._whole_flows.add((id(book), self.chosen))
            chosen = self.chosen
            self.do(self.tr("モノの記録の取得"), lambda b: b.fetch_things([chosen]),
                    failed=lambda _m: self._whole_flows.discard((id(book), chosen)), read=True)
        self.render()

    def toggle_flow(self, object_id: str) -> None:
        """A row's 「流れ」: its thing's flow shown, or closed when it was shown already."""
        self.choose(None if self.chosen == object_id else object_id)

    def filter_class(self, path: Optional[list[str]]) -> None:
        """Limits the list to a class and below (None: all): 「分類体系の編集」's 「この画像で絞り込み」."""
        self.class_filter = list(path) if path else None
        self.render()

    def _set_class_filter(self, path) -> None:
        self.class_filter = [str(p) for p in path] if isinstance(path, list) and path else None
        self.render()

    def filter_root(self, root: Optional[str]) -> None:
        """Limits the list to a 大分類 (None: all)."""
        self.class_filter = [root] if root else None
        self.render()

    def kind_path(self, kind: str, taxa) -> list[str]:
        """A record's kind as a path of the classification (a class's other name → its path)."""
        path = path_of(kind)
        if len(path) == 1 and taxa and not find(taxa, path):
            return list(class_by_name(taxa, path[0]) or path)
        return path

    def _shown_kind(self, kind: str, taxa) -> bool:
        """Whether a kind is in the class the lists are limited to."""
        return not self.class_filter or within(kind_of(self.kind_path(kind, taxa)), self.class_filter)

    def _render_root_filter(self, taxa) -> None:
        # The classification shown has the kinds records use added, so its roots cover every record.
        roots = [t.path[0] for t in taxa if len(t.path) == 1]
        chosen = self.class_filter[0] if self.class_filter else ""
        if chosen and chosen not in roots:
            roots.append(chosen)
        combo = self.root_filter
        combo.blockSignals(True)
        combo.clear()
        combo.addItem(self.tr("すべて"), "")
        for r in roots:
            combo.addItem(r, r)
        combo.setCurrentIndex(max(0, combo.findData(chosen)))
        combo.blockSignals(False)

    def _chosen_flow(self) -> Optional[ObjectFlow]:
        return self.book.flow(self.chosen) if self.book and self.chosen else None

    def _rename_chosen(self) -> None:
        flow = self._chosen_flow()
        if flow is None:
            return
        name = self.rename_input.text().strip() or flow.name
        kind = self.rekind_input.text().strip()
        self.do(self.tr("名前の変更"), lambda b: b.rename(flow.object_id, name, kind_of(path_of(kind)) if kind else None))

    def _render_rekind(self) -> None:
        """「このモノの詳細 ▸ 種別」's levels, for the kind in its field (drawn while the box is open)."""
        if self.book is None or not self.flow_more.is_open():
            self.rekind_levels.clear()
            return
        self.rekind_levels.render(self._once("taxonomy", self.book.taxonomy), self.rekind_input.text(), keep=True)
        self._render_flow_member()

    def _render_flow_member(self) -> None:
        """「このモノの詳細 ▸ 既知の個体」: when the thing's name is a known individual's (or one of its other names), its
        class, other names and own specifications on a line."""
        flow = self._chosen_flow()
        hit = self.book.find_member(flow.name) if self.book is not None and flow is not None and flow.name else None
        labels = self.flow_member.parentWidget().layout().labelForField(self.flow_member) if self.flow_member.parentWidget() else None
        for w in (self.flow_member, labels):
            if w is not None:
                w.setVisible(hit is not None)
        self._flow_member = hit
        if hit is None:
            return
        taxa = self._once("taxonomy", self.book.taxonomy)
        t = find(taxa, hit.path)
        said = t.member(hit.name) if t is not None else None
        others = member_names(t, hit.name, self.book.spec_fields) if t is not None else []
        parts = [f"{hit.name}（{kind_of(hit.path)}）"]
        if others:
            parts.append(self.tr("別名 {a}").format(a="、".join(others)))
        parts.extend(f"{k} {v}" for k, v in ((said.specs if said else None) or {}).items() if k != "別名" and v)
        if said is not None and said.note:
            parts.append(said.note)
        self.flow_member_label.setText("・".join(parts))
        self.flow_member_button.setVisible(self.edit_taxonomy is not None and self.allows("分類体系の編集"))

    def _open_flow_member(self) -> None:
        hit = getattr(self, "_flow_member", None)
        if hit is not None and self.edit_taxonomy is not None:
            self.edit_taxonomy(list(hit.path), hit.name)

    def _rekind_chosen(self, path: list) -> None:
        """A level's button pressed: the kind in the field (nothing saved until 「名前・種別の変更」)."""
        self.rekind_input.setText(kind_of(path))

    def _remove_chosen(self) -> None:
        flow = self._chosen_flow()
        if flow is None:
            return
        # Two steps, so one click never deletes a thing's records.
        if not self.remove_all_button.property("armed"):
            self.remove_all_button.setProperty("armed", True)
            self.remove_all_button.setText(self.tr("もう一度押すと削除"))
            return
        object_id = flow.object_id

        def every(b: ObservationBook):
            # Every record of the thing (a shared store holds more than those read here).
            if not b.read_whole([object_id]):
                return False
            whole = b.flow(object_id)
            return b.remove(list(whole.observations)) if whole else True

        self.do(self.tr("記録の削除"), every)

    def _fit(self, points: list, buffer_m: float = 200) -> None:
        if not points:
            return
        transform = from_wgs84(self.canvas.mapSettings().destinationCrs())
        lons = [p[0] for p in points]
        lats = [p[1] for p in points]
        pad = buffer_m / 111_320
        rect = QgsRectangle(min(lons) - pad, min(lats) - pad, max(lons) + pad, max(lats) + pad)
        try:
            self.canvas.setExtent(transform.transformBoundingBox(rect))
        except Exception:  # noqa: BLE001
            return
        self.canvas.refresh()

    def _fit_flow(self, flow: Optional[ObjectFlow]) -> None:
        if flow:
            self._fit([(o.lon, o.lat) for o in flow.observations])

    def _fit_area(self, area: CheckedArea) -> None:
        self._fit(area.ring, 0)

    def _center(self, o: Observation) -> None:
        transform = from_wgs84(self.canvas.mapSettings().destinationCrs())
        try:
            at = transform.transform(o.lon, o.lat)
        except Exception:  # noqa: BLE001
            return
        extent = self.canvas.extent()
        inner = extent.buffered(-min(extent.width(), extent.height()) * 0.1)
        if not inner.contains(at):
            self.canvas.setCenter(at)
            self.canvas.refresh()

    def go_to_record(self, o: Observation, quiet: bool = False) -> bool:
        """Goes to a record's image and centres the map on it (False: its image is not open)."""
        if self.book is None:
            return False
        entry = next((e for e in self.book.images if e.id == o.image_id), None)
        if entry is not None and entry is not self.book.current:
            self.book.current = entry
            if not self._went():
                return False  # (went back to the form's image)
        self._center(o)
        if entry is None:
            if not quiet:
                self.say(self.tr("画像 {n} はまだ開いていない。ダブルクリックで開いて記録を編集").format(n=o.image_name or o.image_id))
            return False
        return True

    def open_record(self, o: Observation) -> None:
        """
        Goes to a record's image and opens it in the form; an image not open
        is opened first (open_image: 開いた画像の履歴, the catalog, else listed
        without its file), tsudo 2026-11-18.
        """
        def form() -> None:
            if self.book is None or self.book.current is None or self.book.current.id != o.image_id:
                return
            self._center(o)
            if not self.active:
                self.set_active(True)
            # The record as the book has it now (one read elsewhere, 「自分の記録」, may be older).
            now = next((r for r in self.book.records if r.id == o.id), o)
            if not self._form_holds(editing=now) and self._let_form_go(self.tr("「{n}」を開く").format(n=self._named(now))):
                self.open_form(now.lon, now.lat, editing=now)

        if self.go_to_record(o, quiet=True):
            form()
            return
        self.open_image(o.image_id, o.image_name, o.image_time, then=form)

    def open_image(self, image_id: str, name: str = "", image_time: Optional[int] = None,
                   then: Optional[Callable[[], Any]] = None) -> bool:
        """
        A record's image recorded on: gone to when open; else opened (tsudo
        2026-11-18, ダブルクリックで開く): with the files of last time when
        this computer opened it before (after a look), else from the image
        catalog by its id, else (no catalog has it) listed without its file
        with its name and time. `then` runs once the image is the current
        one. True when it was open already.
        """
        if self.book is None:
            return False
        if self.go_to(image_id):
            if then is not None:
                then()
            return True
        dock = self.catalog() if self.catalog is not None else None

        def opened(_layers: list) -> None:
            # The layers added listed the image (_layers_changed); listed again when that has not run yet.
            if not self.go_to(image_id):
                self.refresh_images()
                if not self.go_to(image_id):
                    return
            if then is not None:
                then()

        def listed() -> None:
            if self.book is None:
                return
            entry = self.book.list_image(image_id, name, image_time)
            if entry is None or not self._went():
                return
            if then is not None:
                then()
            self.say(self.tr("{n} は画像カタログにないので、ファイルのない画像として一覧に追加").format(n=name or image_id))

        if dock is None or not hasattr(dock, "open_image_id"):
            listed()
            return False
        self.say(self.tr("{n} を表示中…").format(n=name or image_id))
        dock.open_image_id(image_id, then=opened, missing=listed)
        return False

    def open_my_records(self):
        """「自分の記録」: the records one made, across the images (a window of its own, one per session)."""
        from ..my_records_dialog import MyRecordsDialog

        if self._my_records is None:
            self._my_records = MyRecordsDialog(self, self)
        self._my_records.show_again()
        return self._my_records
