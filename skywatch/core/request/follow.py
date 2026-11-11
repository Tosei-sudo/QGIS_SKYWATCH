"""
Following up requests handed out (出力履歴): whether the image catalog has
an image of the request's area taken on its day (撮像済み), or still none
`miss_after_days` after it (未撮像). A request taken has its raise undone
(and, 期間内1回, becomes 完了); one missed is offered a raise the next day.
"""
from __future__ import annotations

import json
import math
from dataclasses import dataclass
from datetime import date
from typing import Callable, Optional, Sequence

from ..catalog import CatalogConfig
from ..esri import EsriClient, EsriError
from ..plan.names import NO_NAMES, SatelliteNames, best_named, names_satellite
from .. import time_zone
from ..time_query import between_condition, text_sample
from ..time_zone import read_text_instant
from .model import ImagingRequest
from .model import day_bounds
from .store import IMAGE_ARRIVED, HistoryRow, RequestStoreConfig, esri_rings, update_history, update_requests


@dataclass
class Finding:
    """What a follow-up found for one history row."""
    row: HistoryRow
    captured: str
    image_id: str = ""


def follow_up(client: EsriClient, catalog: CatalogConfig, token: Optional[str], rows: Sequence[HistoryRow],
              requests: dict, now: float, miss_after_days: float,
              cancelled: Optional[Callable[[], bool]] = None, names: SatelliteNames = NO_NAMES) -> list[Finding]:
    """
    The rows (採用, 未判定, their day begun) whose outcome is now known. Of the
    images found, one whose sensor names the row's satellite (or an alias of
    `names`) is preferred. A row whose request is not in the ledger (or has
    no area) is left 未判定: the day's images anywhere do not settle it (#77).

    The times of a request taken several times a day (1 日の回数) are
    followed up together: each takes an image of its own, `times_gap` hours
    (1) or more from the others' (images of one pass are one time), in the
    order of the times; an image already taken by one of its rows (in
    `rows`, 撮像済み) is not counted again.
    """
    acquired = catalog.fields.get("acquired")
    if not acquired:
        raise EsriError(f"{catalog.label} に撮像日時の属性（fields.acquired）なし")
    types = client.layer(catalog.url, token).field_types
    zone = time_zone.zone_for(catalog.url)
    like: Optional[str] = None
    out: list[Finding] = []
    groups: dict = {}
    settled: dict = {}
    for row in rows:
        if row.result != "採用":
            continue
        if row.captured == "未判定":
            groups.setdefault((row.request_id, row.day), []).append(row)
        elif row.captured == "撮像済み":
            settled.setdefault((row.request_id, row.day), []).append(row)
    for (request_id, day_text), group in groups.items():
        if cancelled and cancelled():
            break
        try:
            day = date.fromisoformat(day_text)
        except ValueError:
            continue
        first, last = day_bounds(day)
        if now < first:
            continue
        req: Optional[ImagingRequest] = requests.get(request_id)
        if req is None or not req.rings:
            # No area to ask about (the request left the ledger): any image of the day would do, so none is asked (#77).
            continue
        if like is None:
            like = text_sample(client, catalog.url, acquired, types.get(acquired), token, catalog.where)
        where = " AND ".join([between_condition(acquired, types.get(acquired), first, last, like, zone),
                              *([f"({catalog.where})"] if catalog.where else [])])
        params = {"where": where, "outFields": "*", "returnGeometry": "false",
                  "geometry": json.dumps({"rings": esri_rings(req.rings), "spatialReference": {"wkid": 4326}}),
                  "geometryType": "esriGeometryPolygon", "inSR": "4326", "spatialRel": "esriSpatialRelIntersects"}
        found = client.ask(f"{catalog.url}/query", params, token).get("features") or []
        sensor = catalog.fields.get("sensor")
        # Taken several times that day (rows of more than one time; rows of one time written twice are each their own).
        several = req.times[1] > 1 or any(r.occurrence > 1 for r in (*group, *settled.get((request_id, day_text), ())))
        # Images taken by the request's other times (already settled): neither they nor their pass count again.
        used = {r.image_id for r in settled.get((request_id, day_text), ()) if r.image_id}
        moments = [t for t in (_moment(f, acquired, zone) for f in found if _image_id(f, catalog) in used) if t is not None]
        for row in sorted(group, key=lambda r: r.occurrence):
            mine = fitting_images(found, sensor, row, req, names) if found and sensor else list(found)
            if several:
                gap = req.gap_ms
                mine = [f for f in mine if _image_id(f, catalog) not in used
                        and (gap <= 0 or (t := _moment(f, acquired, zone)) is None or all(abs(t - u) >= gap for u in moments))]
            if mine:
                wanted = names.expand(row.satellite) if sensor else set()
                best = best_named(mine, lambda x: (x.get("attributes") or {}).get(sensor), wanted)
                value = _image_id(best, catalog)
                used.add(value)
                t = _moment(best, acquired, zone)
                if t is not None:
                    moments.append(t)
                out.append(Finding(row, "撮像済み", value))
            elif now - last > miss_after_days * 86400000:
                out.append(Finding(row, "未撮像"))
    return out


def _image_id(feature: dict, catalog: CatalogConfig) -> str:
    """An image's id in the catalog (its id field, else its object id)."""
    a = feature.get("attributes") or {}
    id_field = catalog.fields.get("id")
    value = a.get(id_field) if id_field else None
    if value is None:
        value = next((a[k] for k in ("OBJECTID", "objectid", "FID") if a.get(k) is not None), "")
    return str(value)


def _moment(feature: dict, acquired: str, zone) -> Optional[float]:
    """When an image was taken (ms), None when its time cannot be read."""
    v = (feature.get("attributes") or {}).get(acquired)
    if isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v):
        return float(v)
    if isinstance(v, str):
        return read_text_instant(v, zone or time_zone.UTC)
    return None


def accepted_names(row: HistoryRow, req: Optional[ImagingRequest], names: SatelliteNames = NO_NAMES) -> set:
    """
    The satellites an image may come from to count for a row: none (any
    satellite) when the request names no satellite or series; otherwise the
    satellite the row was handed to and the request's satellites and series,
    with their other names (`satelliteAliases`).
    """
    if req is None or not (req.satellites or req.series):
        return set()
    return names.expand(row.satellite, *req.satellites, *req.series)


def fitting_images(found: Sequence[dict], sensor: str, row: HistoryRow, req: Optional[ImagingRequest],
                   names: SatelliteNames = NO_NAMES) -> list[dict]:
    """
    The images that count for a row: all of them for a request open to any
    satellite, else those whose sensor names an accepted satellite (an
    image of another satellite over the area does not settle a request
    that named its satellites).
    """
    wanted = accepted_names(row, req, names)
    if not wanted:
        return list(found)
    written = [row.satellite, *req.satellites, *req.series] if req is not None else [row.satellite]
    return [f for f in found if names_satellite((f.get("attributes") or {}).get(sensor), wanted, written)]


def _row_key(r: HistoryRow):
    return r.object_id if r.object_id is not None else id(r)


def captured_times(findings: Sequence[Finding], history: Sequence[HistoryRow] = ()) -> dict:
    """(request id, day) → the rows 撮像済み (`history` as `findings` leave it): how many times the day was taken."""
    state = {_row_key(r): (r, r.captured) for r in history}
    state.update((_row_key(f.row), (f.row, f.captured)) for f in findings)
    out: dict = {}
    for r, captured in state.values():
        if r.result == "採用" and captured == "撮像済み":
            out[(r.request_id, r.day)] = out.get((r.request_id, r.day), 0) + 1
    return out


def request_changes(findings: Sequence[Finding], requests: dict, history: Sequence[HistoryRow] = ()) -> list[tuple[int, dict]]:
    """
    What the ledger takes from findings: a request taken loses its raise
    and, 期間内1回, is 完了. A request taken several times a day (1 日の回数)
    counts as taken when n of its rows of the day are 撮像済み (with those of
    `history`).
    """
    out: dict[int, dict] = {}
    counts = captured_times(findings, history)
    for f in findings:
        req = requests.get(f.row.request_id)
        if f.captured != "撮像済み" or req is None or req.object_id is None:
            continue
        if counts.get((f.row.request_id, f.row.day), 0) < req.times[0]:
            continue
        change = {"boost": 0}
        if req.achieve == "期間内1回":
            change["status"] = "完了"
        out[req.object_id] = change
    return list(out.items())


def missed_ids(rows: Sequence[HistoryRow]) -> set:
    """Requests found 未撮像 and not yet offered a raise for it."""
    return {r.request_id for r in rows if r.captured == "未撮像" and not r.boost_done}


def marked(rows: Sequence[HistoryRow], captured: str, image_id: str = "") -> list[Finding]:
    """Findings set by hand (「撮像済みにする」・「未撮像にする」) for 採用 rows whose outcome differs."""
    return [Finding(r, captured, image_id if captured == "撮像済み" else "") for r in rows
            if r.result == "採用" and r.captured != captured]


def undone_changes(findings: Sequence[Finding], requests: dict, history: Sequence[HistoryRow]) -> list[tuple[int, dict]]:
    """
    A 期間内1回 request marked 未撮像 by hand after it was 完了 goes back to
    有効, unless another of its days is still taken (撮像済み rows, n of them
    for a request taken several times a day).
    """
    counts = captured_times(findings, history)
    still = {rid for (rid, _day), n in counts.items() if n >= (requests[rid].times[0] if rid in requests else 1)}
    out: dict[int, dict] = {}
    for f in findings:
        req = requests.get(f.row.request_id)
        if (f.captured != "未撮像" or req is None or req.object_id is None or req.achieve != "期間内1回"
                or req.status != "完了" or req.id in still):
            continue
        out[req.object_id] = {"status": "有効"}
    return list(out.items())


def history_changes(findings: Sequence[Finding]) -> list[tuple[int, dict]]:
    """
    The history rows' new outcome (a row made 未撮像 again is offered its raise
    again). A row found taken was in the system's plan after all: 採用 (画像の
    入着), whatever its answer was; made 未撮像 by hand again, that 採用 is
    taken back (an answer from the system or by hand stays).
    """
    out = []
    for f in findings:
        if f.row.object_id is None:
            continue
        change = {"captured": f.captured, "imageId": f.image_id, **_adoption_change(f)}
        if f.captured == "未撮像":
            change["boostDone"] = 0
        out.append((f.row.object_id, change))
    return out


def _adoption_change(f: Finding) -> dict:
    row = f.row
    if f.captured == "撮像済み" and row.adoption != "採用":
        return {"adoption": "採用", "adoptionSource": IMAGE_ARRIVED}
    if f.captured != "撮像済み" and row.adoption_source == IMAGE_ARRIVED:
        return {"adoption": "不明", "adoptionSource": None}
    return {}


def settle(findings: Sequence[Finding]) -> None:
    """Puts the findings' outcome on their rows (after they were saved)."""
    for f in findings:
        adoption = _adoption_change(f)
        f.row.captured, f.row.image_id = f.captured, f.image_id
        if adoption:
            f.row.adoption, f.row.adoption_source = adoption["adoption"], adoption["adoptionSource"] or ""
        if f.captured == "未撮像":
            f.row.boost_done = 0


@dataclass
class Tally:
    """How a system's handed-out requests came out."""
    system: str
    taken: int = 0
    missed: int = 0
    pending: int = 0

    @property
    def total(self) -> int:
        return self.taken + self.missed + self.pending

    @property
    def text(self) -> str:
        """「撮像済み 8・未撮像 1・未判定 3（撮像率 89%）」 (the rate over those settled)."""
        settled = self.taken + self.missed
        rate = f"（撮像率 {round(100 * self.taken / settled)}%）" if settled else ""
        return f"撮像済み {self.taken}・未撮像 {self.missed}・未判定 {self.pending}{rate}"


def tally(rows: Sequence[HistoryRow]) -> list[Tally]:
    """The 採用 rows counted per system (in the order met), then all of them as 「計」."""
    by: dict[str, Tally] = {}
    total = Tally("計")
    for r in rows:
        if r.result != "採用":
            continue
        t = by.setdefault(r.system or "—", Tally(r.system or "—"))
        for x in (t, total):
            if r.captured == "撮像済み":
                x.taken += 1
            elif r.captured == "未撮像":
                x.missed += 1
            else:
                x.pending += 1
    return [*by.values(), total] if by else []


def next_step(row: HistoryRow, req: Optional[ImagingRequest], day: date) -> str:
    """
    What becomes of a row's request next (as of `day`, the JST day now):
    完了, 次回の計算で繰り上げ候補 (missed, not yet offered), 繰り上げ確認済み, 期間終了 (missed with
    nothing left of its period), 依頼なし, or 撮像待ち.
    """
    if row.result != "採用":
        return ""
    if req is None:
        return "台帳に依頼なし"
    if row.captured == "撮像済み":
        return "完了" if req.status == "完了" else "撮像済み"
    if row.captured == "未撮像":
        if req.status != "有効":
            return f"依頼は{req.status}"
        if math.isfinite(req.end) and day_bounds(day)[0] > req.end:
            return "期間終了"
        return "繰り上げ確認済み" if row.boost_done else "次回の計算で持ち越し・繰り上げ候補"
    return "撮像待ち"


def in_days(rows: Sequence[HistoryRow], first: date, last: date) -> list[HistoryRow]:
    """The rows whose target day falls in [first, last]."""
    a, b = first.isoformat(), last.isoformat()
    return [r for r in rows if a <= r.day <= b]


def _saved_only(findings: Sequence[Finding], saved) -> list[Finding]:
    """The findings whose row was saved (`saved.ok`: object ids), the others left as they were."""
    return [f for f in findings if f.row.object_id is not None and f.row.object_id in saved.ok]


def follow_and_save(client: EsriClient, store: RequestStoreConfig, token: Optional[str], catalog: CatalogConfig, catalog_token: Optional[str],
                    history: Sequence[HistoryRow], requests: dict, now: float, names: SatelliteNames = NO_NAMES,
                    cancelled: Optional[Callable[[], bool]] = None, problems: Optional[list] = None) -> tuple[list[Finding], bool]:
    """
    Follows the history up against the catalog and saves what was found:
    the rows' outcome, and on the ledger the raises undone and 完了. The
    findings saved, and whether the ledger was changed. Rows that could not
    be saved keep their outcome (they are followed up again next time) and
    the reasons go to `problems`.
    """
    problems = problems if problems is not None else []
    found = follow_up(client, catalog, catalog_token, history, requests, now, store.miss_after_days, cancelled, names)
    if not found:
        return [], False
    saved = update_history(client, store, token, history_changes(found))
    problems += saved.problems
    found = _saved_only(found, saved)
    settle(found)
    changes = request_changes(found, requests, history)
    if changes:
        problems += update_requests(client, store, token, changes, now).problems
    return found, bool(changes)


def save_marked(client: EsriClient, store: RequestStoreConfig, token: Optional[str], findings: Sequence[Finding], requests: dict,
                history: Sequence[HistoryRow], now: float, problems: Optional[list] = None) -> bool:
    """Saves outcomes set by hand (rows and ledger); whether the ledger was changed (the reasons of what failed in `problems`)."""
    problems = problems if problems is not None else []
    if not findings:
        return False
    saved = update_history(client, store, token, history_changes(findings))
    problems += saved.problems
    findings = _saved_only(findings, saved)
    changes = request_changes(findings, requests, history) + undone_changes(findings, requests, history)
    settle(findings)
    if changes:
        problems += update_requests(client, store, token, changes, now).problems
    return bool(changes)


def found_text(found: Sequence[Finding]) -> str:
    taken = sum(1 for f in found if f.captured == "撮像済み")
    return f"撮像済み {taken} 件・未撮像 {len(found) - taken} 件を確認"
