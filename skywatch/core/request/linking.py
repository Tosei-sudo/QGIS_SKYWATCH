"""
画像とショットの手動関連付け (tsudo, 2026-11-11: 「手の照合は、今の方法に加えて、
この画像カタログのレコードがこのショットという本当の手動関連付け画面がほしい」):
a day's shots and the image catalog's records over each, to say by hand
which record took which shot.

A shot is a row of the shot layer (`requestStores[].shots`, SHOT_ID) with
the history rows (採用) that name it; rows without a shot (no shot layer, or
written before it) are a request's and those put into it (MERGED_INTO). Its
area is the shot's outline, else the rows' areas, else the ledger's.

Linking puts 撮像済み and the image's id on every 採用 row of the shot,
through follow.save_marked as 「撮像済みにする」 does: the ledger follows
(raise undone, 期間内1回 完了) and the shot layer too (sync_shots).
Unlinking puts the rows back to 未判定 (照合 may find an image again) or
未撮像 (carried over and offered a raise at the next 「計算」); a request
完了 by them is 有効 again unless another day of it is still taken.
"""
from __future__ import annotations

import json
import math
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import Callable, Optional, Sequence

from .. import time_zone
from ..catalog import CatalogConfig
from ..esri import EsriClient, EsriError, quote
from ..plan.names import NO_NAMES, SatelliteNames
from ..time_query import between_condition, text_sample
from .follow import Finding, _image_id, _moment, fitting_images
from .model import JST, ImagingRequest, day_bounds
from .store import HistoryRow, RequestStoreConfig, ShotRow, esri_rings, load_history, shot_outcome

#: Images read for one shot at most (in the catalog's order: more are told on screen, to look fewer days around).
MAX_IMAGES = 300
#: Image ids asked in one query of the history.
IDS_PER_QUERY = 200


def _ring(r: Sequence) -> list:
    return [[float(p[0]), float(p[1])] for p in r]


@dataclass
class ShotUnit:
    """One shot to link an image to: its rows (採用) and, when there is one, its row of the shot layer."""
    key: str
    day: str
    system: str
    satellite: str
    rows: list
    shot: Optional[ShotRow] = None
    #: Its area (longitude / latitude rings): the shot's outline, else the rows' or the ledger's areas.
    rings: list = field(default_factory=list)

    @property
    def pass_time(self) -> Optional[float]:
        return self.shot.pass_time if self.shot is not None else None

    @property
    def captured(self) -> str:
        return shot_outcome(self.rows)[0]

    @property
    def image_id(self) -> str:
        return shot_outcome(self.rows)[1]

    @property
    def names(self) -> list[str]:
        return [r.name or r.request_id for r in self.rows]

    @property
    def sensor_text(self) -> str:
        s = self.shot
        return "・".join(x for x in ((s.sensor, s.mode) if s is not None else ()) if x)

    def target_time(self) -> float:
        """The moment its images are compared with: its pass, else the middle of its day."""
        if self.pass_time is not None:
            return self.pass_time
        first, last = day_bounds(date.fromisoformat(self.day))
        return (first + last) / 2


def unit_key(r: HistoryRow) -> str:
    """
    The shot a history row is taken in: its SHOT_ID; without one (no shot
    layer, or written before it) a request and those put into it (MERGED_INTO)
    are one shot, each time of the day (1 日の回数) its own, as the 撮像予定
    dock tells (shot_view.companions).
    """
    return r.shot_id or f"{r.system}|{r.merged_into or r.request_id}|{r.occurrence}"


def units_of(rows: Sequence[HistoryRow], shots: Sequence[ShotRow], requests: dict) -> list[ShotUnit]:
    """The shots of some history rows (their 採用 rows by SHOT_ID, else by the request they were put into), by system and time."""
    by_shot = {s.shot_id: s for s in shots if s.shot_id}
    groups: dict[str, list] = {}
    for r in rows:
        if r.result != "採用":
            continue
        groups.setdefault(unit_key(r), []).append(r)
    out = []
    for key, members in groups.items():
        # The request whose row the file carries first, then those put into its shot.
        members.sort(key=lambda r: (bool(r.merged_into), r.request_id, r.occurrence))
        shot = by_shot.get(key)
        rings = [_ring(r) for r in shot.rings] if shot is not None and shot.rings else []
        if not rings:
            for r in members:
                req: Optional[ImagingRequest] = requests.get(r.request_id)
                rings += [_ring(x) for x in (r.rings or (req.rings if req is not None else []))]
        first = members[0]
        out.append(ShotUnit(key, first.day, first.system, (shot.satellite if shot is not None and shot.satellite else first.satellite),
                            members, shot, rings))
    return sorted(out, key=lambda u: (u.day, u.system, u.pass_time if u.pass_time is not None else math.inf,
                                      min(r.occurrence for r in u.rows), u.names))


@dataclass
class ImageCandidate:
    """An image of the catalog over a shot."""
    image_id: str
    acquired: Optional[float]
    sensor: str
    #: Its footprint (longitude / latitude rings).
    rings: list
    #: Its satellite is one the shot's requests accept (or they accept any).
    matches: bool
    attributes: dict = field(default_factory=dict)
    #: The rows of other shots it is linked to already.
    used_by: list = field(default_factory=list)


def time_text(ms: Optional[float]) -> str:
    """A moment in JST: 「2026-11-10 22:04」 ('' when unknown)."""
    if ms is None or not math.isfinite(ms):
        return ""
    return datetime.fromtimestamp(ms / 1000, tz=JST).strftime("%Y-%m-%d %H:%M")


def gap_text(ms: Optional[float], target: float) -> str:
    """How far an image's time is from a shot's: 「+1時間05分」, 「−12分」, 「+2日3時間」."""
    if ms is None or not math.isfinite(ms):
        return ""
    minutes = round((ms - target) / 60000)
    sign = "+" if minutes >= 0 else "−"
    days, rest = divmod(abs(minutes), 1440)
    hours, mins = divmod(rest, 60)
    if days:
        return f"{sign}{days}日{hours}時間"
    if hours:
        return f"{sign}{hours}時間{mins:02d}分"
    return f"{sign}{mins}分"


def search_span(day: str, before: int, after: int) -> tuple[float, float]:
    """The moments (ms) images are looked for in: from `before` days before the shot's day to `after` days after it (JST)."""
    d = date.fromisoformat(day)
    return day_bounds(d - timedelta(days=max(0, before)))[0], day_bounds(d + timedelta(days=max(0, after)))[1]


def find_images(client: EsriClient, catalog: CatalogConfig, token: Optional[str], unit: ShotUnit, requests: dict,
                before: int = 1, after: int = 1, names: SatelliteNames = NO_NAMES, limit: int = MAX_IMAGES,
                cancelled: Optional[Callable[[], bool]] = None) -> list[ImageCandidate]:
    """
    The catalog's images over a shot's area taken in its span: those of an
    accepted satellite first, then the nearest in time to its pass (or day).
    """
    acquired = catalog.fields.get("acquired")
    if not acquired:
        raise EsriError(f"{catalog.label} に撮像日時の属性（fields.acquired）なし")
    if not unit.rings:
        raise EsriError("このショットには範囲がないため、画像を探せない")
    types = client.layer(catalog.url, token).field_types
    zone = time_zone.zone_for(catalog.url)
    like = text_sample(client, catalog.url, acquired, types.get(acquired), token, catalog.where)
    start, end = search_span(unit.day, before, after)
    where = " AND ".join([between_condition(acquired, types.get(acquired), start, end, like, zone),
                          *([f"({catalog.where})"] if catalog.where else [])])
    extra = {"geometry": json.dumps({"rings": esri_rings(unit.rings), "spatialReference": {"wkid": 4326}}),
             "geometryType": "esriGeometryPolygon", "inSR": "4326", "spatialRel": "esriSpatialRelIntersects"}
    found = client.query_all(catalog.url, where, token, geometry=True, extra=extra, limit=limit, cut=True, cancelled=cancelled)
    sensor = catalog.fields.get("sensor")
    fitting: set = set()
    if sensor:
        for r in unit.rows:
            fitting |= {id(f) for f in fitting_images(found, sensor, r, requests.get(r.request_id), names)}
    target = unit.target_time()
    out = []
    for f in found:
        a = f.get("attributes") or {}
        rings = [_ring(r) for r in (f.get("geometry") or {}).get("rings") or []]
        out.append(ImageCandidate(_image_id(f, catalog), _moment(f, acquired, zone), str(a.get(sensor) or "") if sensor else "",
                                  rings, not sensor or id(f) in fitting, a))
    out.sort(key=lambda c: (not c.matches, abs(c.acquired - target) if c.acquired is not None else math.inf, c.image_id))
    return out


def links_of(client: EsriClient, store: RequestStoreConfig, token: Optional[str], image_ids: Sequence[str],
             cancelled: Optional[Callable[[], bool]] = None) -> dict[str, list[HistoryRow]]:
    """The history rows (撮像済み) already linked to some images, by image id."""
    ids = sorted({i for i in image_ids if i})
    out: dict[str, list[HistoryRow]] = {}
    if not store.history or not ids:
        return out
    hf = store.history_fields
    for i in range(0, len(ids), IDS_PER_QUERY):
        part = ids[i:i + IDS_PER_QUERY]
        where = f"{hf['imageId']} IN ({','.join(quote(x) for x in part)}) AND {hf['captured']} = '撮像済み'"
        for r in load_history(client, store, token, where, cancelled=cancelled):
            out.setdefault(r.image_id, []).append(r)
    return out


def with_links(candidates: Sequence[ImageCandidate], links: dict, unit: ShotUnit) -> list[ImageCandidate]:
    """The candidates with the rows of other shots each is linked to."""
    mine = {r.object_id for r in unit.rows if r.object_id is not None}
    for c in candidates:
        c.used_by = [r for r in links.get(c.image_id, ()) if r.object_id not in mine]
    return list(candidates)


def link_text(rows: Sequence[HistoryRow]) -> str:
    """Where an image is linked already: 「2026-11-10 SYS-A 横須賀」…"""
    return "、".join(dict.fromkeys(f"{r.day} {r.system} {r.name or r.request_id}".strip() for r in rows))


def link_findings(unit: ShotUnit, image_id: str) -> list[Finding]:
    """The rows of a shot made 撮像済み with an image (those not so already)."""
    return [Finding(r, "撮像済み", image_id) for r in unit.rows if (r.captured, r.image_id) != ("撮像済み", image_id)]


def unlink_findings(unit: ShotUnit, captured: str = "未判定") -> list[Finding]:
    """The rows of a shot put back to 未判定 or 未撮像, without an image."""
    if captured not in ("未判定", "未撮像"):
        raise ValueError(captured)
    return [Finding(r, captured) for r in unit.rows if r.captured != captured or r.image_id]
