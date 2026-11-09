"""
Requests as plain attribute rows, for taking 定点 requests in from a layer
and writing them out to one (レイヤーから取り込み・レイヤーへ書き出し).

A layer's fields are matched to request items by the ledger's attribute
names (REQ_ID, START_AT …), the item names (id, start …) or Japanese
headings (名称, 開始 …), ignoring case. Dates are JST days (`2026-11-08`)
or ISO times. Items a layer lacks take the 定点 defaults: 毎回, every day,
有効, priority 500.
"""
from __future__ import annotations

import math
import re
from datetime import date, datetime
from typing import Any, Optional

from ..util import is_number, time_of
from .model import (
    ACHIEVES, CATEGORIES, KINDS, LOOK_SIDES, ORBIT_DIRS, SHAPE_KINDS, STATUSES, TIMES_OF_DAY, ImagingRequest, clamp_priority,
    day_bounds, jst_day, level_value,
)
from .store import DEFAULT_REQUEST_FIELDS, new_request_id

#: Japanese headings a layer may use, by item.
HEADINGS = {
    "id": "依頼ID", "name": "名称", "managementNo": "管理番号", "identificationNo": "識別番号", "category": "区分", "achieve": "達成条件", "start": "開始", "end": "終了",
    "repeat": "繰り返し", "status": "状態", "priority": "優先度", "boost": "繰り上げ量", "kind": "種別",
    "series": "衛星シリーズ", "satellites": "衛星", "modes": "センサーモード", "offNadirMin": "オフナディア角下限",
    "offNadirMax": "オフナディア角上限", "incidenceMin": "入射角下限", "incidenceMax": "入射角上限", "lookSide": "観測方向",
    "orbitDir": "軌道方向", "resolution": "分解能", "timeOfDay": "時間帯", "cloudNoGo": "実施しない雲量",
    "systems": "優先システム", "shapeKind": "範囲の種類", "requester": "依頼者", "note": "メモ",
}
#: The items written out (the ledger's own times left out).
WRITTEN = [k for k in DEFAULT_REQUEST_FIELDS if k not in ("created", "updated")]
NUMBERS = ("priority", "boost", "offNadirMin", "offNadirMax", "incidenceMin", "incidenceMax", "resolution", "cloudNoGo")


def _key(s: str) -> str:
    return re.sub(r"[\s_・\-]", "", s).lower()


def field_roles(names: list) -> dict:
    """Item → the layer's field for it."""
    out = {}
    for role, ledger in DEFAULT_REQUEST_FIELDS.items():
        wanted = {_key(ledger), _key(role), _key(HEADINGS.get(role, ""))} - {""}
        for n in names:
            if _key(n) in wanted:
                out[role] = n
                break
    return out


def _text(v: Any) -> str:
    if v is None:
        return ""
    s = str(v).strip()
    return "" if s.upper() == "NULL" else s


def code_text(v: Any) -> str:
    """A number kept as text (管理番号・識別番号): a whole number from a numeric field without its 「.0」."""
    if isinstance(v, float) and v.is_integer():
        return str(int(v))
    return _text(v)


def _number(v: Any) -> Optional[float]:
    if is_number(v):
        return float(v)
    t = _text(v)
    try:
        n = float(t)
    except ValueError:
        return None
    return n if math.isfinite(n) else None


def _day_ms(v: Any, last: bool) -> float:
    """A JST day's first (or last) moment of a date value; a time as it is."""
    if isinstance(v, datetime):
        return v.timestamp() * 1000
    if isinstance(v, date):
        return day_bounds(v)[1 if last else 0]
    t = _text(v).replace("/", "-")
    m = re.fullmatch(r"(\d{4})-(\d{1,2})-(\d{1,2})", t)
    if m:
        return day_bounds(date(int(m.group(1)), int(m.group(2)), int(m.group(3))))[1 if last else 0]
    return time_of(t) if t else math.nan


def _choice(v: Any, choices, fallback: str) -> str:
    t = _text(v)
    return t if t in choices else fallback


def _list(v: Any) -> list:
    return [s.strip() for s in re.split(r"[,、]", _text(v)) if s.strip()]


def request_of_row(values: dict, roles: dict, rings: list, shape_kind: str, existing: Optional[ImagingRequest] = None,
                   default_category: str = "定点") -> ImagingRequest:
    """A request from a layer's row (`roles`: item → field); `existing` (same id) keeps its object id and history."""
    def get(role):
        name = roles.get(role)
        return values.get(name) if name else None

    rid = _text(get("id")) or (existing.id if existing else new_request_id())
    p = get("priority")
    priority = _number(p)
    if priority is None and _text(p):
        priority = level_value(_text(p))
    r = ImagingRequest(
        id=rid, name=_text(get("name")) or rid,
        management_no=code_text(get("managementNo")), identification_no=code_text(get("identificationNo")),
        category=_choice(get("category"), CATEGORIES, default_category),
        achieve=_choice(get("achieve"), ACHIEVES, "毎回" if default_category == "定点" else "期間内1回"),
        start=_day_ms(get("start"), False), end=_day_ms(get("end"), True),
        repeat=_text(get("repeat")) or "毎日",
        status=_choice(get("status"), STATUSES, "有効"),
        priority=clamp_priority(priority if priority is not None else 500),
        boost=int(_number(get("boost")) or (existing.boost if existing else 0)),
        kind=_choice(get("kind"), KINDS, "任意"),
        series=_list(get("series")), satellites=_list(get("satellites")), modes=_list(get("modes")),
        off_nadir_min=_number(get("offNadirMin")), off_nadir_max=_number(get("offNadirMax")),
        incidence_min=_number(get("incidenceMin")), incidence_max=_number(get("incidenceMax")),
        look_side=_choice(get("lookSide"), LOOK_SIDES, "任意"), orbit_dir=_choice(get("orbitDir"), ORBIT_DIRS, "任意"),
        resolution=_number(get("resolution")), time_of_day=_choice(get("timeOfDay"), TIMES_OF_DAY, "任意"),
        cloud_no_go=_number(get("cloudNoGo")), systems=_list(get("systems")),
        shape_kind=_choice(get("shapeKind"), SHAPE_KINDS, shape_kind),
        requester=_text(get("requester")), note=_text(get("note")),
        rings=rings,
    )
    if existing is not None:
        r.object_id, r.created = existing.object_id, existing.created
    return r


def row_problems(r: ImagingRequest) -> list:
    """Why a row cannot be taken in."""
    out = []
    if not r.rings:
        out.append("範囲なし")
    if not math.isfinite(r.start) or not math.isfinite(r.end):
        out.append("期間（開始・終了）なし")
    elif r.end < r.start:
        out.append("終了が開始より前")
    return out


def row_of(r: ImagingRequest) -> dict:
    """A request as a row by the ledger's attribute names (days as JST dates)."""
    f = DEFAULT_REQUEST_FIELDS
    values = {
        "id": r.id, "name": r.name, "managementNo": r.management_no, "identificationNo": r.identification_no,
        "category": r.category, "achieve": r.achieve,
        "start": jst_day(r.start).isoformat() if math.isfinite(r.start) else None,
        "end": jst_day(r.end).isoformat() if math.isfinite(r.end) else None,
        "repeat": r.repeat, "status": r.status, "priority": r.priority, "boost": r.boost, "kind": r.kind,
        "series": ",".join(r.series), "satellites": ",".join(r.satellites), "modes": ",".join(r.modes),
        "offNadirMin": r.off_nadir_min, "offNadirMax": r.off_nadir_max, "incidenceMin": r.incidence_min,
        "incidenceMax": r.incidence_max, "lookSide": r.look_side, "orbitDir": r.orbit_dir, "resolution": r.resolution,
        "timeOfDay": r.time_of_day, "cloudNoGo": r.cloud_no_go, "systems": ",".join(r.systems), "shapeKind": r.shape_kind,
        "requester": r.requester, "note": r.note,
    }
    return {f[k]: values[k] for k in WRITTEN}
