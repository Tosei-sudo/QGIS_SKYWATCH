"""
観測記録ドックの地図の印 (ui/obs_dock, docs/architecture.md 段階7): the marks of the records on the map. A part of
ObservationDock (ui/observation_dock.py): its methods as they were, on the dock's widgets and state.
"""
from __future__ import annotations

from ...core.obs.api import (
    area_label, box_ring, local_time, ends_of, flow_of, heading_text, outline_of, side_points, two_point, km_text,
    unseen_label, path_of,
)
from ...qgis_io import settings
from ..obs_overlay import Mark, Scene, color_of


class OverlayPart:
    """観測記録ドックの地図の印."""

    def scene(self) -> Scene:
        """What is drawn: this image's records, the things carried over, the chosen flow, the areas and the clicked place."""
        s = Scene()
        book = self.book
        if book is None or book.current is None:
            return s
        image = book.current
        kinds = list(dict.fromkeys([*book.vocabulary.kinds, *(t.path[0] for t in book.taxonomy()), *((path_of(f.kind) or [""])[0] for f in book.flows())]))
        levels = book.confidences()
        ring_of = {c.label: c.color for c in levels if c.color}
        surest = levels[0].label if levels else None

        def color(kind: str) -> str:
            return color_of((path_of(kind) or [""])[0], kinds)

        if self.chosen:
            rows = [o for o in book.living() if o.object_id == self.chosen]
            if rows:
                flow = flow_of(rows)
                seen = [o for o in flow.observations if o.present]
                if len(seen) > 1:
                    s.track = [(o.lon, o.lat) for o in seen]
                last = None
                for o in flow.observations:
                    what = "" if o.present else "（不可）" if o.unseen else "（消失）"
                    label = f"{local_time(o.image_time)[5:]}{what}"
                    if last is not None and (last.lon, last.lat) == (o.lon, o.lat):
                        # The same place (a 不可 or 消失 record stays where it was last seen): one mark, its labels joined.
                        last.label += f"・{label}"
                        continue
                    last = Mark("step", o.lon, o.lat, label=label, present=o.present, unseen=bool(o.unseen))
                    s.marks.append(last)
        if image is not None:
            s.areas = [(a.ring, area_label(a)) for a in book.image_areas() if not a.unseen]
            s.unseen_areas = [(a.ring, area_label(a)) for a in book.image_areas() if a.unseen]
            for c in book.carried():
                o = c.source
                s.marks.append(Mark("ghost", o.lon, o.lat, color(o.kind), o.object_name, ends=ends_of(o.lon, o.lat, o.length, o.heading),
                                    heading=o.heading, key=o.id, outline=outline_of(o.lon, o.lat, o.length, o.width, o.heading)))
            editing = self.pending[2].id if self.pending and self.pending[2] else None
            waiting = self.waiting_ids()
            for o in book.living():
                if o.image_id != image.id:
                    continue
                lon, lat = (self.dragging[1], self.dragging[2]) if self.dragging and self.dragging[0] == o.id else (o.lon, o.lat)
                what = (f"・{o.activity}" if o.activity else "") if o.present else f"（{unseen_label(o.unseen)}）" if o.unseen else "（消失）"
                sure = f"（{o.confidence}）" if o.confidence and o.confidence != surest else ""
                late = self.tr("（送信待ち）") if o.id in waiting else ""
                s.marks.append(Mark("obs", lon, lat, color(o.kind), f"{o.object_name}{what}{sure}{late}", o.present, ring_of.get(o.confidence or ""),
                                    ends=None if o.unseen else ends_of(lon, lat, o.length, o.heading), heading=o.heading, editing=o.id == editing,
                                    key=o.id, outline=outline_of(lon, lat, o.length, o.width, o.heading) if o.present else None,
                                    reported=o.id in self.reported, unseen=bool(o.unseen)))
        p = self.pending
        if p and not p[2]:
            s.pending = (p[0], p[1])
        if self.area_mode:
            s.area_pending_unseen = self.area_unseen
            if self.area_ring:
                s.area_pending = self.area_ring
            elif self.area_corner and self.cursor:
                (x1, y1), (x2, y2) = self.area_corner, self.cursor
                s.area_pending = box_ring([min(x1, x2), min(y1, y2), max(x1, x2), max(y1, y2)])
            elif self.area_corner:
                s.pending = self.area_corner
        if self.rear:
            s.first = self.rear
            s.placed = list(self.placed)
            if self.placed:
                sides = [*self.placed[1:], *([self.cursor] if self.cursor else [])]
                m = side_points(self.placed[0], self.rear, sides)
                s.rubber_outline = outline_of(m.lon, m.lat, m.length, m.width, m.heading)
                size = f"{km_text(m.length)} × {km_text(m.width)}" if m.width else km_text(m.length)
                s.rubber_label = f"{size}・{heading_text(m.heading)}"
            elif self.cursor:
                s.cursor = self.cursor
                m = two_point(self.cursor, self.rear)
                s.rubber_label = f"{km_text(m.length)}・{heading_text(m.heading)}"
        if image is not None:
            self.many.paint_into(s, color, self.cursor if self.many.mode and self.active and not self.area_mode else None)
        return s

    def set_mark_style(self, style: str) -> None:
        """「印」: 'normal', 'outline' or 'hidden', kept for next time (per user)."""
        settings.set_mark_style(style)
        style = settings.mark_style()
        if style != "hidden":
            self._shown_style = style
        if self.mark_style_select.currentData() != style:
            self.mark_style_select.setCurrentIndex(max(0, self.mark_style_select.findData(style)))
        if hasattr(self, "overlay"):
            self.overlay.set_style(style)

    def toggle_marks(self) -> None:
        """「H」 (and Ctrl+Alt+D anywhere): the marks hidden, or back as they were shown."""
        self.set_mark_style(self._shown_style if self.mark_style_select.currentData() == "hidden" else "hidden")

    def draw(self) -> None:
        if not hasattr(self, "overlay"):
            return
        self.overlay.set_scene(self.scene())
        self.overlay.setVisible(self.isVisible())

    def _visibility(self, visible: bool) -> None:
        if visible:
            self.refresh_images()
            return
        if not self.isHidden():
            # Only behind another tab of the right side (画像補正…): the form, 「クリック記録」 and the marks stay
            # (操作性点検の再検証 R1, tsudo 2026-11-23); they end when the dock is closed.
            return
        self._dock_closed()

    def _dock_closed(self) -> None:
        """The dock closed (or the plugin unloading): what was being done on the map ends."""
        if self.area_mode:
            self.set_area_mode(False)
        self.set_active(False)
        self.close_form()
        if self.only.isChecked():
            self.only.setChecked(False)
        self._apply_only()
        self.overlay.hide()
