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
from ..plan.store import between_condition
from .model import ImagingRequest
from .model import day_bounds
from .store import HistoryRow, esri_rings


@dataclass
class Finding:
    """What a follow-up found for one history row."""
    row: HistoryRow
    captured: str
    image_id: str = ""


def _plain(s: str) -> str:
    return "".join(ch for ch in s.lower() if ch.isalnum())


def follow_up(client: EsriClient, catalog: CatalogConfig, token: Optional[str], rows: Sequence[HistoryRow],
              requests: dict, now: float, miss_after_days: float,
              cancelled: Optional[Callable[[], bool]] = None) -> list[Finding]:
    """The rows (採用, 未判定, their day begun) whose outcome is now known."""
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
        if found:
            sensor = catalog.fields.get("sensor")
            name = _plain(row.satellite)

            def named(feature: dict) -> bool:
                s = _plain(str((feature.get("attributes") or {}).get(sensor) or "")) if sensor else ""
                return bool(s and name and (s in name or name in s))

            best = next((x for x in found if named(x)), found[0])
            a = best.get("attributes") or {}
            id_field = catalog.fields.get("id")
            value = a.get(id_field) if id_field else None
            if value is None:
                value = next((a[k] for k in ("OBJECTID", "objectid", "FID") if a.get(k) is not None), "")
            out.append(Finding(row, "撮像済み", str(value)))
        elif now - last > miss_after_days * 86400000:
            out.append(Finding(row, "未撮像"))
    return out


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
