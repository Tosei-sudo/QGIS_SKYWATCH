"""
観測記録ドックの地図での操作 (ui/obs_dock, docs/architecture.md 段階7): the clicks, the hover and the drags on the map,
and the area being drawn. A part of ObservationDock (ui/observation_dock.py): its methods as they were,
on the dock's widgets and state.
"""
from __future__ import annotations

from typing import Optional

from ...core.obs.api import box_ring, side_points, get_distance, km_text, UNSEEN
from ..obs_overlay import Mark


class MapInputPart:
    """観測記録ドックの地図での操作."""

    def map_clicked(self, lon: float, lat: float, mark: Optional[Mark]) -> None:
        """A click on the map while recording (or making an area)."""
        if self.book is None:
            return
        if self.area_mode:
            if self.area_corner is None or self.area_ring is not None:
                self.area_corner = (lon, lat)
                self.area_ring = None
                self.say(self.tr("対角をクリックする（Esc で取り消し）"))
            else:
                (x1, y1), (x2, y2) = self.area_corner, (lon, lat)
                self._set_area_ring(box_ring([min(x1, x2), min(y1, y2), max(x1, x2), max(y1, y2)]))
            self.draw()
            return
        if not self.active:
            return
        if self.many.mode and self.many.clicked(lon, lat, mark):
            return
        if self.rear is None and mark is not None and mark.key:
            if mark.role == "obs":
                record = self.book.record(mark.key)
                if record is not None:
                    return self.open_form(record.lon, record.lat, editing=record)
            if mark.role == "ghost":
                c = next((x for x in self.book.carried() if x.source.id == mark.key), None)
                if c is not None:
                    return self.open_form(c.source.lon, c.source.lat, source=c.source)
        if self.points < 2:
            return self.open_form(lon, lat)
        # The rear, then the front (tsudo, 2026-11-02), then a side or both (tsudo, 2026-11-03).
        if self.rear is None:
            self.close_form()
            self.rear = (lon, lat)
            self.cursor = None
            self.say(self.tr("前端をクリックする（Esc で取り消し）"))
            self.draw()
            return
        clicked = [*self.placed, (lon, lat)]
        if len(clicked) < self.points - 1:
            self.placed = clicked
            self.cursor = None
            if len(clicked) == 1:
                self.say(self.tr("側面（舷側）をクリックする（Esc で取り消し）") if self.points == 3 else self.tr("片側の側面をクリックする（Esc で取り消し）"))
            else:
                self.say(self.tr("反対側の側面をクリックする（Esc で取り消し）"))
            self.draw()
            return
        m = side_points(clicked[0], self.rear, clicked[1:])
        self._clear_points()
        self.open_form(m.lon, m.lat, measured=(m.length, m.heading, m.width))

    def map_hovered(self, lon: float, lat: float) -> None:
        if self.rear is not None or (self.area_mode and self.area_corner is not None and self.area_ring is None) or (
                self.many.mode and self.active and not self.area_mode):
            self.cursor = (lon, lat)
            self.draw()

    def map_dragged(self, mark: Mark, lon: float, lat: float, done: bool) -> None:
        """A record's mark dragged: its place corrected when let go."""
        if self.book is None or not mark.key:
            return
        if not done:
            self.dragging = (mark.key, lon, lat)
            self.draw()
            return
        self.dragging = None
        key = mark.key
        self.do(self.tr("位置の修正"), lambda b: b.move(key, lon, lat), watch=True)

    def _extent_changed(self) -> None:
        if self.overlay.isVisible():
            self.overlay.updatePosition()
            self.overlay.update()

    def _set_area_ring(self, ring: list) -> None:
        self.area_ring = ring
        self.area_corner = None
        self._render_area_range()
        (self.area_reason if self.area_unseen else self.area_kind).setFocus()

    def _area_view(self) -> None:
        self._set_area_ring(box_ring(self._view_box()))
        self.draw()

    def _area_image(self) -> None:
        image = self.book.current if self.book else None
        if image is not None and image.box:
            self._set_area_ring(box_ring(image.box))
            self.draw()

    def _render_area_range(self) -> None:
        ring = self.area_ring
        if not ring:
            self.area_range.setText(self.tr("範囲はまだない。地図で対角の 2 点をクリックする。"))
            return
        lons = [p[0] for p in ring]
        lats = [p[1] for p in ring]
        w, e, s, n = min(lons), max(lons), min(lats), max(lats)
        mid = (s + n) / 2
        self.area_range.setText(self.tr("範囲: {w} × {h}").format(w=km_text(get_distance([w, mid], [e, mid])), h=km_text(get_distance([w, s], [w, n]))))

    def _submit_area(self) -> None:
        if not self.area_ring:
            self.say(self.tr("地図で対角の 2 点をクリックするか、「表示範囲」「画像の範囲」を押して範囲を決める。"))
            return
        ring, kind, note = self.area_ring, self.area_kind.text(), self.area_note.text()

        def after(area) -> None:
            if area is not None:
                message = self.status.text()
                self.set_area_mode(False)
                self.say(message)

        if self.area_unseen:
            reason = self.area_reason.currentText().strip() or UNSEEN
            self.do(self.tr("不可範囲の記録"), lambda b: b.add_area(ring, "", note, unseen=reason), after)
            return
        self.do(self.tr("確認範囲の記録"), lambda b: b.add_area(ring, kind, note), after)
