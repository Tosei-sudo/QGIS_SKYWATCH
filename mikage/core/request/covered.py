"""
What of a request's area is already taken, or about to be (tsudo,
2026-11-12: 「広域撮像の分配なんだけど、今1回とったとこって回避されるように
なってる？」 → 「1〜5 を全部やる」).

- 撮像済み (`taken`): the outlines of the catalog's images that settled the
  request's rows (撮像済み): those of each row's day over the request's area
  from a satellite that counts for it (follow.fitting_images), and the image
  each row names (one linked by hand may be of another day). An image
  without a shape counts as the whole area (as before, any image settled
  the request).
- 撮像待ち (`waiting`): what was handed out on earlier days and is not
  followed up yet (採用・未判定, not answered 不採用): the outline of its
  shot (the shot layer), else the area its row was written with.

Only a 期間内1回 request taken once a day with an area (`subtracts`)
counts them: one 毎回, or taken several times a day, wants the whole area
each time.

The shares are measured on points spread over the area (`samples_of`), as
the day's fitting measures an area too big for one shot (passes.target_of).
"""
from __future__ import annotations

import json
import math
from dataclasses import dataclass, field
from datetime import date
from typing import Callable, Optional, Sequence

from .model import ImagingRequest, bbox_of, center_of, day_bounds, in_rings

#: The share of an area that counts as taken (as the imaging plan's goal).
GOAL = 0.95
#: About how many points measure a share of an area.
SHARE_CELLS = 400
#: Request ids asked about in one condition (an IN list of the history).
IDS_PER_QUERY = 100


@dataclass
class Covered:
    """A request's area taken already (撮像済み) and handed out on earlier days, still to be followed up (撮像待ち)."""
    #: Outlines (each a list of rings, longitude / latitude) of the images that settled its rows.
    taken: list = field(default_factory=list)
    #: Outlines of the shots handed out on earlier days, not yet followed up.
    waiting: list = field(default_factory=list)
    #: The target days (`2026-11-10`) of those shots.
    waiting_days: list = field(default_factory=list)


def is_area(req: ImagingRequest) -> bool:
    return req.shape_kind != "点" and bool(req.rings)


def subtracts(req: ImagingRequest) -> bool:
    """Whether what is taken of a request's area counts on later days: 期間内1回, once a day, an area."""
    return req.achieve == "期間内1回" and req.times[1] <= 1 and is_area(req)


def samples_of(req: ImagingRequest, most: int = SHARE_CELLS) -> list:
    """Points (longitude, latitude) spread over a request's area that measure its shares; its middle for a point."""
    from .passes import grid_cells

    if not req.rings:
        return []
    if not is_area(req):
        mid = center_of(req.rings)
        return [tuple(mid)] if mid else []
    cells = grid_cells(req.rings, most)
    if not cells:
        mid = center_of(req.rings)
        cells = [tuple(mid)] if mid else []
    return cells


def _boxed(outlines: Sequence) -> list:
    """Outlines with their boxes (west, south, east, north), so a point is tested only against those it may be in."""
    out = []
    for rings in outlines:
        box = bbox_of(rings)
        if box is not None:
            out.append((box, rings))
    return out


def inside_any(boxed: Sequence, x: float, y: float) -> bool:
    for (w, s, e, n), rings in boxed:
        if w <= x <= e and s <= y <= n and in_rings(rings, x, y):
            return True
    return False


def mask_of(points: Sequence, outlines: Sequence) -> bytearray:
    """1 for each point inside any outline, else 0."""
    boxed = _boxed(outlines)
    return bytearray(1 if boxed and inside_any(boxed, p[0], p[1]) else 0 for p in points)


def share_of(points: Sequence, outlines: Sequence) -> float:
    """The share of the points inside any outline (0 without points)."""
    if not points:
        return 0.0
    return sum(mask_of(points, outlines)) / len(points)


@dataclass
class Shares:
    """What of a request's area is 撮像済み and, besides that, 撮像待ち (shares of its area, 0 … 1)."""
    taken: float = 0.0
    waiting: float = 0.0

    @property
    def both(self) -> float:
        return self.taken + self.waiting


def shares_of(req: ImagingRequest, covered: Optional[Covered], points: Optional[Sequence] = None) -> Shares:
    """The shares of a request's area taken and waiting (the waiting counted only where not taken)."""
    if covered is None or not (covered.taken or covered.waiting):
        return Shares()
    points = samples_of(req) if points is None else points
    taken = share_of(points, covered.taken)
    both = share_of(points, covered.taken + covered.waiting) if covered.waiting else taken
    return Shares(taken, max(0.0, both - taken))


def percent(share: float) -> int:
    return int(math.floor(share * 100 + 0.5))


def short_day(day: str) -> str:
    """`2026-11-10` as 「11/10」."""
    try:
        d = date.fromisoformat(day)
    except ValueError:
        return day
    return f"{d.month}/{d.day}"


# --- What the history and the catalog say ---------------------------------------------------------


def waiting_rows(history: Sequence, requests: dict, day: date) -> list:
    """
    The rows handed out on days before `day` and not followed up yet
    (採用・未判定, not answered 不採用) of requests whose taken part counts
    (`subtracts`): what is about to be taken.
    """
    d = day.isoformat()
    out = []
    for r in history:
        req = requests.get(r.request_id)
        if (req is None or not subtracts(req) or r.result != "採用" or r.captured not in ("", "未判定")
                or r.adoption == "不採用" or not r.day or r.day >= d):
            continue
        out.append(r)
    return out


def waiting_outlines(rows: Sequence, requests: dict, shots: dict) -> dict:
    """
    request id → the outlines of its rows still to be followed up: their
    shots' (`shots`: SHOT_ID → store.ShotRow), else the area the row was
    written with, else the request's.
    """
    out: dict = {}
    for r in rows:
        shot = shots.get(r.shot_id) if r.shot_id else None
        if shot is not None and shot.rings:
            outlines = [[ring] for ring in shot.rings]
        elif r.rings:
            outlines = [[ring] for ring in r.rings] if r.part else [r.rings]
        else:
            req = requests.get(r.request_id)
            outlines = [req.rings] if req is not None and req.rings else []
        out.setdefault(r.request_id, []).extend(outlines)
    return out


def _rings_of(geometry) -> list:
    """The rings (longitude, latitude) of an Esri polygon; [] for anything else."""
    rings = geometry.get("rings") if isinstance(geometry, dict) else None
    if not isinstance(rings, list):
        return []
    out = []
    for ring in rings:
        if isinstance(ring, list):
            pts = [[float(p[0]), float(p[1])] for p in ring if isinstance(p, (list, tuple)) and len(p) >= 2
                   and all(isinstance(v, (int, float)) and math.isfinite(v) for v in p[:2])]
            if len(pts) >= 3:
                out.append(pts)
    return out


def taken_outlines(client, catalog, token: Optional[str], req: ImagingRequest, rows: Sequence, names=None,
                   cancelled: Optional[Callable[[], bool]] = None) -> list:
    """
    The outlines of the catalog's images that settled a request's rows
    (撮像済み): of each row's day over the request's area from a satellite
    that counts for it, and the image each row names. An image without a
    shape is the whole area.
    """
    from .. import time_zone
    from ..esri import EsriError
    from ..plan.names import NO_NAMES
    from ..time_query import between_condition, text_sample
    from .follow import fitting_images, image_id_of
    from .store import esri_rings

    names = names or NO_NAMES
    rows = [r for r in rows if r.result == "採用" and r.captured == "撮像済み"]
    if not rows or not req.rings:
        return []
    acquired = catalog.fields.get("acquired")
    if not acquired:
        raise EsriError(f"{catalog.label} に撮像日時の属性（fields.acquired）なし")
    types = client.layer(catalog.url, token).field_types
    zone = time_zone.zone_for(catalog.url)
    sensor = catalog.fields.get("sensor")
    like = text_sample(client, catalog.url, acquired, types.get(acquired), token, catalog.where)
    area = json.dumps({"rings": esri_rings(req.rings), "spatialReference": {"wkid": 4326}})
    seen: dict = {}

    def ask(where: str, spatial: bool) -> list:
        params = {"where": where, "outFields": "*", "returnGeometry": "true", "outSR": "4326"}
        if spatial:
            params.update({"geometry": area, "geometryType": "esriGeometryPolygon", "inSR": "4326",
                           "spatialRel": "esriSpatialRelIntersects"})
        return client.ask(f"{catalog.url}/query", params, token).get("features") or []

    by_day: dict = {}
    for r in rows:
        by_day.setdefault(r.day, []).append(r)
    for day_text, day_rows in sorted(by_day.items()):
        if cancelled and cancelled():
            break
        try:
            first, last = day_bounds(date.fromisoformat(day_text))
        except ValueError:
            continue
        where = " AND ".join([between_condition(acquired, types.get(acquired), first, last, like, zone),
                              *([f"({catalog.where})"] if catalog.where else [])])
        found = ask(where, True)
        for r in day_rows:
            for f in (fitting_images(found, sensor, r, req, names) if sensor else found):
                seen.setdefault(image_id_of(f, catalog), f)
    # The images the rows name that the days' did not bring (linked by hand to an image of another day).
    named = sorted({r.image_id for r in rows if r.image_id} - set(seen))
    if named and not (cancelled and cancelled()):
        id_field = catalog.fields.get("id")
        if id_field:
            kind = types.get(id_field) or ""
            numeric = kind in ("esriFieldTypeInteger", "esriFieldTypeSmallInteger", "esriFieldTypeOID", "esriFieldTypeDouble")
            values = [v for v in named if not numeric or _is_number(v)]
            listed = ",".join(v if numeric else "'" + v.replace("'", "''") + "'" for v in values)
            where = f"{id_field} IN ({listed})" if values else ""
        else:
            values = [v for v in named if v.isdigit()]
            where = f"OBJECTID IN ({','.join(values)})" if values else ""
        if where:
            try:
                for f in ask(where, False):
                    seen.setdefault(image_id_of(f, catalog), f)
            except EsriError:
                pass  # the days' images still count
    out = []
    for f in seen.values():
        rings = _rings_of(f.get("geometry"))
        out.append(rings or [list(r) for r in req.rings])
    return out


def _is_number(text: str) -> bool:
    try:
        float(text)
    except ValueError:
        return False
    return True


def read_covered(client, store, token: Optional[str], catalog, catalog_token: Optional[str], requests: Sequence[ImagingRequest],
                 day: date, history: Sequence, names=None, cancelled: Optional[Callable[[], bool]] = None) -> tuple[dict, list]:
    """
    What of each request of the day whose taken part counts (`subtracts`)
    is already taken (the catalog's images of its 撮像済み rows) and waiting
    (`history`: the rows still open, store.history_where(open_rows=True));
    request id → Covered, and what could not be read.
    """
    from ..esri import EsriError
    from .store import history_where, load_history, load_shots_where, quote

    notes: list = []
    first, last = day_bounds(day)
    wanted = {r.id: r for r in requests
              if subtracts(r) and r.status == "有効" and math.isfinite(r.start) and math.isfinite(r.end)
              and r.start <= last and r.end >= first}
    out: dict = {}
    if not wanted or not store.history:
        return out, notes
    # 撮像待ち: the open rows of earlier days, outlined by their shots.
    rows = waiting_rows(history, wanted, day)
    shots: dict = {}
    ids = sorted({r.shot_id for r in rows if r.shot_id})
    if ids and store.shots:
        sf = store.shot_fields
        try:
            for i in range(0, len(ids), IDS_PER_QUERY):
                chunk = ids[i:i + IDS_PER_QUERY]
                for s in load_shots_where(client, store, token, f"{sf['shotId']} IN ({','.join(quote(x) for x in chunk)})",
                                          geometry=True, cancelled=cancelled):
                    shots[s.shot_id] = s
        except EsriError as error:
            notes.append(f"撮像待ちの撮像の範囲を読めず、出力履歴の範囲で計算（{error}）")
    for rid, outlines in waiting_outlines(rows, wanted, shots).items():
        c = out.setdefault(rid, Covered())
        c.waiting.extend(outlines)
    for r in rows:
        c = out.get(r.request_id)
        if c is not None and r.day not in c.waiting_days:
            c.waiting_days.append(r.day)
    # 撮像済み: the rows settled taken of the requests still 有効 (one taken whole is 完了 and not asked about).
    if catalog is None:
        return out, notes
    hf = store.history_fields
    keys = sorted(wanted)
    settled: dict = {}
    try:
        for i in range(0, len(keys), IDS_PER_QUERY):
            where = f"({history_where(store, request_ids=keys[i:i + IDS_PER_QUERY])}) AND {hf['captured']} = '撮像済み'"
            for r in load_history(client, store, token, where, cancelled=cancelled):
                settled.setdefault(r.request_id, []).append(r)
    except EsriError as error:
        notes.append(f"撮像済みの範囲を読めず、撮像済みを除かずに計算（{error}）")
        return out, notes
    failed = 0
    for rid, got in settled.items():
        if cancelled and cancelled():
            break
        try:
            outlines = taken_outlines(client, catalog, catalog_token, wanted[rid], got, names, cancelled)
        except EsriError:
            failed += 1
            continue
        if outlines:
            out.setdefault(rid, Covered()).taken.extend(outlines)
    if failed:
        notes.append(f"撮像済みの範囲を読めなかった依頼 {failed} 件は撮像済みを除かずに計算")
    return out, notes
