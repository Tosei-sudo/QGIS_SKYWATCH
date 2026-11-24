"""
Requests made from the organisation's areas (組織の関心地域, tsudo 2026-11-24:
「撮像依頼をネームドエリアから簡単に出せるように」): a request keeps the
area's 管理番号 (`named_area_id`) and whether its area follows the area's
shape (`area_follow`, 地域の形に合わせる). Pure: the area is anything with
the attributes of core/named_areas.OrgArea (id, name, rings, owner_group,
priority).

- `from_area`: a new request's name, area, 依頼グループ and priority from an area,
  linked to it and following its shape.
- `rings_moved`: whether an area differs from another by more than a metre
  (an area changed by hand, or the area's shape changed; the rounding of
  the coordinates by a server or a layer is not a change).
- `area_links`: how many requests each area has (for its list, and so that
  an area's 管理番号 is not changed while requests are linked to it).
- `previous_for` / `conditions_of`: 「前回と同じ条件」, the conditions of the
  area's last request.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, replace
from typing import Any, Iterable, Optional, Sequence

from .last import remembered, with_remembered
from .model import ImagingRequest, clamp_priority

#: How far a vertex may move before an area counts as changed (metres).
MOVED_M = 1.0
#: 「前回と同じ条件」 leaves the area's own 依頼グループ and priority (from 担当グループ・重要度) as they are.
OWN_ITEMS = ("group", "priority")


def from_area(base: ImagingRequest, area: Any, follow: bool = True) -> ImagingRequest:
    """
    `base` (a new request, or the form's) with an area's name, shape, 担当グループ
    (as its 依頼グループ) and 重要度 (as its priority) where the area has them,
    linked to the area (its 管理番号) and following its shape (`follow`).
    """
    changes: dict = {"name": area.name or base.name, "rings": [[list(p[:2]) for p in ring] for ring in (area.rings or [])],
                     "shape_kind": "範囲", "named_area_id": area.id or "", "area_follow": bool(follow and area.id)}
    if getattr(area, "owner_group", ""):
        changes["group"] = area.owner_group
    if getattr(area, "priority", None):
        changes["priority"] = clamp_priority(area.priority)
    return replace(base, **changes)


def unlinked(r: ImagingRequest) -> ImagingRequest:
    """The request without its area (its own area kept)."""
    return replace(r, named_area_id="", area_follow=False)


def _open(ring: Sequence) -> list:
    """A ring's vertices without the closing one (the first repeated)."""
    pts = [(float(p[0]), float(p[1])) for p in ring if len(p) >= 2]
    if len(pts) > 1 and pts[0] == pts[-1]:
        pts.pop()
    return pts


def _metres(a: tuple, b: tuple) -> float:
    lat = math.radians((a[1] + b[1]) / 2)
    return math.hypot((a[0] - b[0]) * 111_320 * math.cos(lat), (a[1] - b[1]) * 110_540)


def _same_ring(a: list, b: list, tolerance: float) -> bool:
    """Whether two rings have the same vertices within `tolerance` metres, from any vertex and either way round."""
    if len(a) != len(b):
        return False
    if not a:
        return True
    for candidate in (b, b[::-1]):
        for k, p in enumerate(candidate):
            if _metres(a[0], p) > tolerance:
                continue
            n = len(a)
            if all(_metres(a[i], candidate[(k + i) % n]) <= tolerance for i in range(n)):
                return True
    return False


def rings_moved(a: Sequence, b: Sequence, tolerance: float = MOVED_M) -> bool:
    """Whether two areas (rings) differ: a ring more or fewer, a vertex more or fewer, or one moved more than `tolerance` metres."""
    ra = [_open(r) for r in (a or []) if len(r) >= 3]
    rb = [_open(r) for r in (b or []) if len(r) >= 3]
    if len(ra) != len(rb):
        return True
    left = list(rb)
    for ring in ra:
        match = next((i for i, other in enumerate(left) if _same_ring(ring, other, tolerance)), None)
        if match is None:
            return True
        left.pop(match)
    return False


@dataclass
class AreaLinks:
    """The requests of one area."""
    #: 有効 ones, all of them (any state), and those following its shape.
    active: int = 0
    total: int = 0
    following: int = 0

    def text(self) -> str:
        return f"{self.active}" if self.active == self.total else f"{self.active}（全 {self.total}）"


def area_links(requests: Iterable[ImagingRequest]) -> dict[str, AreaLinks]:
    """The requests of each area, by its 管理番号 (any case: the ids are compared in upper case)."""
    out: dict[str, AreaLinks] = {}
    for r in requests:
        if not r.named_area_id:
            continue
        links = out.setdefault(r.named_area_id.strip().upper(), AreaLinks())
        links.total += 1
        links.active += r.status == "有効"
        links.following += bool(r.area_follow)
    return out


def links_of(links: dict, area_id: str) -> AreaLinks:
    return links.get((area_id or "").strip().upper(), AreaLinks())


def previous_for(requests: Iterable[ImagingRequest], area_id: str, leave: str = "") -> Optional[ImagingRequest]:
    """The area's request saved last (but `leave`, the one being edited); None when it has none."""
    key = (area_id or "").strip().upper()
    if not key:
        return None
    mine = [r for r in requests if r.named_area_id.strip().upper() == key and r.id != leave]
    if not mine:
        return None
    return max(mine, key=lambda r: (r.updated or 0, r.created or 0, r.id))


def conditions_of(r: ImagingRequest, previous: ImagingRequest) -> ImagingRequest:
    """`r` with the conditions of `previous` (入力の初期値の記憶's items, but the area's own 依頼グループ and priority)."""
    last = {k: v for k, v in remembered(previous).items() if k not in OWN_ITEMS}
    return with_remembered(r, last)
