"""
前回と同じ撮り方 (改修案 5, tsudo 2026-11-12): a 定点 request is looked at
image against image, and two images compare best when taken the same way. So
the day's calculation prefers, for each 定点 request, how its last image was
taken: the same satellite (mode), the same orbit direction (昇交 / 降交) and
look side (右 / 左), and an incidence within SAME_INCIDENCE degrees.

- The last look: the shot (TBL_REQUEST_SHOT) of the request's latest history
  row 撮像済み of the LOOK_BACK_DAYS days before the day planned; its
  satellite, mode, direction, side and incidence as the shot layer keeps them.
  None for a request never taken in them, nor without a shot layer.
- Only an order, never a hold: among the satellites a request tries (those it
  names keep the order it names them in), the last one first, then those
  with a pass the same direction and side, after the clouds; among a
  satellite's passes, the same direction and side first, then the closest
  incidence, after the clouds and the satellite's time. A
  request with no such pass goes on another one, as before, said so in its
  note (look_text: only when it differs).
- Off with `requestStores[].sameGeometry: false`.

Pure Python (no Qt) but read_last_looks, which asks the history and the shot layer.
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import date, timedelta
from typing import Callable, Optional, Sequence

from ..plan.access import look_at
from ..plan.satellites import SatelliteSpec
from .model import ImagingRequest, occurs_on

#: How far back (days) a 定点 request's last image is looked for.
LOOK_BACK_DAYS = 120
#: Incidences this close (degrees) count as the same.
SAME_INCIDENCE = 3.0
#: History rows asked for at a time (by request id), shots (by shot id).
IDS_PER_QUERY = 200


@dataclass(frozen=True)
class LastLook:
    """How a 定点 request's last image was taken (its shot's)."""
    #: The JST day, `2026-11-08`.
    day: str
    satellite: str
    mode: str = ""
    #: 昇交 / 降交, 右 / 左 ("" when the shot does not say).
    orbit_dir: str = ""
    look_side: str = ""
    incidence: Optional[float] = None


@dataclass(frozen=True)
class Look:
    """How a placement takes a request."""
    ascending: bool
    side: str
    incidence: float

    @property
    def orbit_dir(self) -> str:
        return "昇交" if self.ascending else "降交"


def look_of(p) -> Optional[Look]:
    """How a placement (shots.Placement) takes its request: its first new shot's, or the shot it joins once it is in."""
    if p.into is not None:
        if not p.window:
            return None
        mid = (p.window[0] + p.window[1]) / 2
        return Look(p.into.ascending, "左" if mid > 0 else "右", look_at(mid, p.into.radius)["incidence"])
    if not p.shots:
        return None
    first = p.shots[0]
    return Look(first.ascending, first.side, first.look["incidence"])


def pass_look(op) -> Look:
    """How a pass (plan.access.Opportunity) looks at its target."""
    return Look(bool(op.ascending), "左" if op.side == "left" else "右", float(op.incidence))


def shot_look(c) -> Optional[Look]:
    """How a candidate taken on the orbits is taken (its first shot's); None when fitted by counting."""
    if not c.shots:
        return None
    first = c.shots[0]
    return Look(first.ascending, first.side, first.look["incidence"])


def satellite_rank(last: Optional[LastLook], sat: SatelliteSpec) -> int:
    """0 the last satellite and mode, 1 the last satellite in another mode, 2 another (or no last look)."""
    if last is None or not last.satellite or sat.name != last.satellite:
        return 2
    return 0 if not last.mode or (sat.mode or "") == last.mode else 1


def mismatch(last: Optional[LastLook], look: Optional[Look]) -> tuple:
    """
    How far a look is from the last (the smaller first): another direction
    or side, then the incidence off by more than SAME_INCIDENCE (by whole
    degrees). (0, 0) without a last look or a look.
    """
    if last is None or look is None:
        return 0, 0
    turned = int(bool(last.orbit_dir) and last.orbit_dir != look.orbit_dir) + int(bool(last.look_side) and last.look_side != look.side)
    off = 0
    if last.incidence is not None and math.isfinite(last.incidence):
        d = abs(look.incidence - last.incidence)
        off = 0 if d <= SAME_INCIDENCE else math.ceil(d)
    return turned, off


def same(last: Optional[LastLook], sat: Optional[SatelliteSpec], look: Optional[Look]) -> bool:
    """Whether a shot is taken as the last image was: its satellite, direction, side and (when known) incidence."""
    return last is not None and sat is not None and look is not None and satellite_rank(last, sat) < 2 and mismatch(last, look) == (0, 0)


def _short(day: str) -> str:
    try:
        d = date.fromisoformat(day)
    except ValueError:
        return day
    return f"{d.month}/{d.day}"


def look_text(c) -> str:
    """
    For a 定点 request taken on the orbits otherwise than its last image (no
    pass of the day taken the same way had room): what differs,
    「前回（11/8）と違う撮り方（降交・入射角 32° → 昇交・入射角 41°）」 (""
    otherwise: taken the same way, the way not known, or not taken). Only
    the difference is said, so that the note stays short on the usual days.
    """
    last = getattr(c, "last_look", None)
    look = shot_look(c)
    if last is None or look is None or c.satellite is None or same(last, c.satellite, look):
        return ""
    # Only what the last shot tells is compared (an older shot may not tell all).
    pairs = []
    rank = satellite_rank(last, c.satellite)
    if rank == 2:
        pairs.append((last.satellite, c.satellite.name))
    elif rank == 1:
        pairs.append((last.mode, c.satellite.mode or ""))
    if last.orbit_dir and last.orbit_dir != look.orbit_dir:
        pairs.append((last.orbit_dir, look.orbit_dir))
    if last.look_side and last.look_side != look.side:
        pairs.append((last.look_side, look.side))
    if mismatch(last, look)[1]:
        pairs.append((f"入射角 {round(last.incidence)}°", f"入射角 {round(look.incidence)}°"))
    before = "・".join(a for a, _ in pairs if a) or "—"
    now = "・".join(b for _, b in pairs if b) or "—"
    return f"前回（{_short(last.day)}）と違う撮り方（{before} → {now}）"


def wanted(requests: Sequence[ImagingRequest], day: date) -> list[ImagingRequest]:
    """The requests whose last look is asked for: 定点, 有効, taken on the day."""
    return [r for r in requests if r.category == "定点" and r.status == "有効" and occurs_on(r, day)]


def last_looks_of(rows: Sequence, shots: Sequence) -> dict:
    """
    request id → LastLook, from history rows 撮像済み naming a shot (the
    latest day of each request; the latest written that day) and those shots
    (store.ShotRow). A request whose shot is not found has none.
    """
    by_id = {s.shot_id: s for s in shots if s.shot_id}
    latest: dict = {}
    for r in rows:
        if r.captured != "撮像済み" or not r.shot_id or r.shot_id not in by_id:
            continue
        key = (r.day, r.run_date or 0.0)
        if r.request_id not in latest or key > latest[r.request_id][0]:
            latest[r.request_id] = (key, r.shot_id)
    out = {}
    for request_id, (_, shot_id) in latest.items():
        s = by_id[shot_id]
        if not s.satellite:
            continue
        out[request_id] = LastLook(s.day, s.satellite, s.mode or "", s.orbit_dir or "", s.look_side or "",
                                   s.incidence if s.incidence is not None and math.isfinite(s.incidence) else None)
    return out


def read_last_looks(client, store, token: Optional[str], requests: Sequence[ImagingRequest], day: date,
                    cancelled: Optional[Callable[[], bool]] = None) -> dict:
    """
    request id → LastLook of the day's 定点 requests (wanted): their history
    rows 撮像済み of the LOOK_BACK_DAYS days before, and those rows' shots
    ({} when `sameGeometry` is off, or without a history or a shot layer).
    """
    from .store import history_where, load_history, load_shots_where, quote

    ids = [r.id for r in wanted(requests, day)]
    if not ids or not store.same_geometry or not store.history or not store.shots:
        return {}
    hf, sf = store.history_fields, store.shot_fields
    first, last = (day - timedelta(days=LOOK_BACK_DAYS)).isoformat(), (day - timedelta(days=1)).isoformat()
    rows: list = []
    for i in range(0, len(ids), IDS_PER_QUERY):
        where = history_where(store, first=first, last=last, request_ids=ids[i:i + IDS_PER_QUERY])
        where += f" AND {hf['captured']} = '撮像済み' AND {hf['shotId']} IS NOT NULL"
        rows += load_history(client, store, token, where, cancelled=cancelled)
    shot_ids = sorted({r.shot_id for r in rows if r.shot_id})
    shots: list = []
    for i in range(0, len(shot_ids), IDS_PER_QUERY):
        chunk = shot_ids[i:i + IDS_PER_QUERY]
        shots += load_shots_where(client, store, token, f"{sf['shotId']} IN ({','.join(quote(x) for x in chunk)})", cancelled=cancelled)
    return last_looks_of(rows, shots)
