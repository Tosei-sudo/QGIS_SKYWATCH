"""
観測記録ドックの地図の印 (ui/obs_dock, docs/architecture.md 段階7): the marks of the records on the map. A part of
ObservationDock (ui/observation_dock.py): its methods as they were, on the dock's widgets and state.
"""
from __future__ import annotations

from functools import lru_cache
from typing import Optional

from ...core.obs.api import (
    area_label, box_ring, local_time, ends_of, flow_of, heading_text, outline_of, side_points, two_point, km_text,
    unseen_label, path_of, quantity_mark,
)
from ...qgis_io import settings
from ..obs_overlay import Mark, Scene, color_of


@lru_cache(maxsize=20000)
def _shape(lon: float, lat: float, length: Optional[float], width: Optional[float], heading: Optional[float]) -> tuple:
    """A record's front and rear and its corners on the map, worked out once for its place and size (each drawing asks again)."""
    return ends_of(lon, lat, length, heading), outline_of(lon, lat, length, width, heading)


class OverlayPart:
    """観測記録ドックの地図の印."""

    def scene(self) -> Scene:
        """What is drawn: this image's records, the things carried over, the chosen flow, the areas and the clicked place."""
        s = self._base_scene()
        live = self._live_scene()
        s.marks.extend(live.marks)
        for name in ("pending", "area_pending", "area_pending_unseen", "first", "placed", "cursor", "rubber_outline", "rubber_label"):
            setattr(s, name, getattr(live, name))
        return s

    def _mark_color(self, kind: str) -> str:
        """A record's colour on the map: its top class's (in the order of the kinds the last drawing knew)."""
        return color_of((path_of(kind) or [""])[0], getattr(self, "_mark_kinds", []))

    def _base_scene(self) -> Scene:
        """
        What stays put while the pointer moves (painted once as a picture,
        ObservationOverlay): this image's records, the things carried over,
        the chosen flow, the areas, what 「見本で記録」 has waiting.
        """
        s = Scene()
        book = self.book
        if book is None or book.current is None:
            return s
        image = book.current
        taxa = self._once("taxonomy", book.taxonomy)
        objects = self._once("flows", book.flows)
        self._mark_kinds = list(dict.fromkeys([*book.vocabulary.kinds, *(t.path[0] for t in taxa), *((path_of(f.kind) or [""])[0] for f in objects)]))
        levels = book.confidences()
        ring_of = {c.label: c.color for c in levels if c.color}
        surest = levels[0].label if levels else None
        color = self._mark_color
        living = self._once("living", book.living)
        if self.chosen:
            rows = [o for o in living if o.object_id == self.chosen]
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
        areas = book.image_areas()
        s.areas = [(a.ring, area_label(a)) for a in areas if not a.unseen]
        s.unseen_areas = [(a.ring, area_label(a)) for a in areas if a.unseen]
        for c in self._once("carried", book.carried):
            o = c.source
            ends, outline = _shape(o.lon, o.lat, o.length, o.width, o.heading)
            s.marks.append(Mark("ghost", o.lon, o.lat, color(o.kind), o.object_name + quantity_mark(o), ends=ends, heading=o.heading, key=o.id,
                                outline=outline))
        editing = self.pending[2].id if self.pending and self.pending[2] else None
        waiting = self.waiting_ids()
        dragged = self.dragging[0] if self.dragging else None
        for o in living:
            if o.image_id != image.id or o.id == dragged:
                continue  # the one dragged follows the pointer (_live_scene)
            s.marks.append(self._record_mark(o, o.lon, o.lat, color, ring_of, surest, editing, waiting))
        self.many.paint_waiting(s, color)
        return s

    def _record_mark(self, o, lon: float, lat: float, color, ring_of: dict, surest: Optional[str], editing: Optional[str],
                     waiting: set) -> Mark:
        """A record of this image on the map (at `lon`, `lat`: where it is, or where it is being dragged)."""
        what = (f"・{o.activity}" if o.activity else "") if o.present else f"（{unseen_label(o.unseen)}）" if o.unseen else "（消失）"
        sure = f"（{o.confidence}）" if o.confidence and o.confidence != surest else ""
        late = self.tr("（送信待ち）") if o.id in waiting else ""
        ends, outline = _shape(lon, lat, o.length, o.width, o.heading)
        return Mark("obs", lon, lat, color(o.kind), f"{o.object_name}{quantity_mark(o)}{what}{sure}{late}", o.present, ring_of.get(o.confidence or ""),
                    ends=None if o.unseen else ends, heading=o.heading, editing=o.id == editing,
                    key=o.id, outline=outline if o.present else None, reported=o.id in self.reported, unseen=bool(o.unseen))

    def _live_scene(self) -> Scene:
        """What follows the pointer (painted over the picture each time): the place clicked, the line or area being made, a record dragged."""
        s = Scene()
        book = self.book
        image = book.current if book is not None else None
        if image is None:
            return s
        if self.dragging:
            o = book.record(self.dragging[0])
            if o is not None and o.deleted_at is None and o.image_id == image.id:
                levels = book.confidences()
                editing = self.pending[2].id if self.pending and self.pending[2] else None
                s.marks.append(self._record_mark(o, self.dragging[1], self.dragging[2], self._mark_color, {c.label: c.color for c in levels if c.color},
                                                 levels[0].label if levels else None, editing, self.waiting_ids()))
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
        self.many.paint_live(s, self._mark_color, self.cursor if self.many.mode and self.active and not self.area_mode else None)
        return s

    def _once(self, name: str, make):
        """
        What one showing of the dock (render) reads of the book more than
        once (its things' flows, the classification, what carries over):
        worked out once while it shows (outside one: each time asked).
        """
        memo = getattr(self, "_memo", None)
        if memo is None:
            return make()
        if name not in memo:
            memo[name] = make()
        return memo[name]

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
        self.overlay.set_scene(self._base_scene(), self._live_scene())
        self.overlay.setVisible(self.isVisible())

    def draw_live(self) -> None:
        """
        Only what follows the pointer drawn again (as it moves: measuring,
        making an area, dragging, 「見本で記録」): the records' marks stay
        as they were painted, nothing of the book worked out again.
        """
        if not hasattr(self, "overlay"):
            return
        self.overlay.set_live(self._live_scene())
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
