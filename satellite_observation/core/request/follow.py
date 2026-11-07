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
from ..plan.names import NO_NAMES, SatelliteNames, best_named, name_rank
from ..plan.store import between_condition
from .model import ImagingRequest
from .model import day_bounds
from .store import HistoryRow, RequestStoreConfig, esri_rings, update_history, update_requests


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
    `names`) is preferred.
    """
    acquired = catalog.fields.get("acquired")
    if not acquired:
        raise EsriError(f"{catalog.label} に撮像日時の属性（fields.acquired）なし")
    types = client.layer(catalog.url, token).field_types
    out: list[Finding] = []
    for row in rows:
        if cancelled and cancelled():
            break
        if row.result != "採用" or row.captured != "未判定":
            continue
        try:
            day = date.fromisoformat(row.day)
        except ValueError:
            continue
        first, last = day_bounds(day)
        if now < first:
            continue
        req: Optional[ImagingRequest] = requests.get(row.request_id)
        where = " AND ".join([between_condition(acquired, types.get(acquired), first, last),
                              *([f"({catalog.where})"] if catalog.where else [])])
        params = {"where": where, "outFields": "*", "returnGeometry": "false"}
        if req is not None and req.rings:
            params.update(geometry=json.dumps({"rings": esri_rings(req.rings), "spatialReference": {"wkid": 4326}}),
                          geometryType="esriGeometryPolygon", inSR="4326", spatialRel="esriSpatialRelIntersects")
        found = client.ask(f"{catalog.url}/query", params, token).get("features") or []
        sensor = catalog.fields.get("sensor")
        if found and sensor:
            found = fitting_images(found, sensor, row, req, names)
        if found:
            wanted = names.expand(row.satellite) if sensor else set()
            best = best_named(found, lambda x: (x.get("attributes") or {}).get(sensor), wanted)
            a = best.get("attributes") or {}
            id_field = catalog.fields.get("id")
            value = a.get(id_field) if id_field else None
            if value is None:
                value = next((a[k] for k in ("OBJECTID", "objectid", "FID") if a.get(k) is not None), "")
            out.append(Finding(row, "撮像済み", str(value)))
        elif now - last > miss_after_days * 86400000:
            out.append(Finding(row, "未撮像"))
    return out


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
    return [f for f in found if name_rank((f.get("attributes") or {}).get(sensor), wanted) > 0]


def request_changes(findings: Sequence[Finding], requests: dict) -> list[tuple[int, dict]]:
    """What the ledger takes from findings: a request taken loses its raise and, 期間内1回, is 完了."""
    out: dict[int, dict] = {}
    for f in findings:
        req = requests.get(f.row.request_id)
        if f.captured != "撮像済み" or req is None or req.object_id is None:
            continue
        change = {"boost": 0}
        if req.achieve == "期間内1回":
            change["status"] = "完了"
        out[req.object_id] = change
    return list(out.items())


def missed_ids(rows: Sequence[HistoryRow]) -> set:
    """Requests found 未撮像 and not yet offered a raise for it."""
    return {r.request_id for r in rows if r.captured == "未撮像" and not r.boost_done}


def taken_ids(rows: Sequence[HistoryRow]) -> set:
    """Requests found taken at least once."""
    return {r.request_id for r in rows if r.captured == "撮像済み"}


def marked(rows: Sequence[HistoryRow], captured: str, image_id: str = "") -> list[Finding]:
    """Findings set by hand (「撮像済みにする」・「未撮像にする」) for 採用 rows whose outcome differs."""
    return [Finding(r, captured, image_id if captured == "撮像済み" else "") for r in rows
            if r.result == "採用" and r.captured != captured]


def undone_changes(findings: Sequence[Finding], requests: dict, history: Sequence[HistoryRow]) -> list[tuple[int, dict]]:
    """
    A 期間内1回 request marked 未撮像 by hand after it was 完了 goes back to
    有効, unless another of its rows is still 撮像済み.
    """
    changed = {id(f.row) for f in findings}
    still = {r.request_id for r in history if r.captured == "撮像済み" and id(r) not in changed}
    out: dict[int, dict] = {}
    for f in findings:
        req = requests.get(f.row.request_id)
        if (f.captured != "未撮像" or req is None or req.object_id is None or req.achieve != "期間内1回"
                or req.status != "完了" or req.id in still):
            continue
        out[req.object_id] = {"status": "有効"}
    return list(out.items())


def history_changes(findings: Sequence[Finding]) -> list[tuple[int, dict]]:
    """The history rows' new outcome (a row made 未撮像 again is offered its raise again)."""
    out = []
    for f in findings:
        if f.row.object_id is None:
            continue
        change = {"captured": f.captured, "imageId": f.image_id}
        if f.captured == "未撮像":
            change["boostDone"] = 0
        out.append((f.row.object_id, change))
    return out


def settle(findings: Sequence[Finding]) -> None:
    """Puts the findings' outcome on their rows (after they were saved)."""
    for f in findings:
        f.row.captured, f.row.image_id = f.captured, f.image_id
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


def follow_and_save(client: EsriClient, store: RequestStoreConfig, token: Optional[str], catalog: CatalogConfig, catalog_token: Optional[str],
                    history: Sequence[HistoryRow], requests: dict, now: float, names: SatelliteNames = NO_NAMES,
                    cancelled: Optional[Callable[[], bool]] = None) -> tuple[list[Finding], bool]:
    """
    Follows the history up against the catalog and saves what was found:
    the rows' outcome, and on the ledger the raises undone and 完了. The
    findings, and whether the ledger was changed.
    """
    found = follow_up(client, catalog, catalog_token, history, requests, now, store.miss_after_days, cancelled, names)
    if not found:
        return [], False
    update_history(client, store, token, history_changes(found))
    settle(found)
    changes = request_changes(found, requests)
    if changes:
        update_requests(client, store, token, changes, now)
    return found, bool(changes)


def save_marked(client: EsriClient, store: RequestStoreConfig, token: Optional[str], findings: Sequence[Finding], requests: dict,
                history: Sequence[HistoryRow], now: float) -> bool:
    """Saves outcomes set by hand (rows and ledger); whether the ledger was changed."""
    if not findings:
        return False
    update_history(client, store, token, history_changes(findings))
    changes = request_changes(findings, requests) + undone_changes(findings, requests, history)
    settle(findings)
    if changes:
        update_requests(client, store, token, changes, now)
    return bool(changes)


def found_text(found: Sequence[Finding]) -> str:
    taken = sum(1 for f in found if f.captured == "撮像済み")
    return f"撮像済み {taken} 件・未撮像 {len(found) - taken} 件を確認"
