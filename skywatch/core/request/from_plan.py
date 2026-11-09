"""
臨時 requests from the 「撮像計画」 dock's opportunities (tsudo, 2026-11-09:
「本質的に同じ業務。撮像計画ドックは臨時の要求を簡単に出せる機能」). The plan
is not saved apart any more (its 「保存・追跡」 is gone): what is to be taken
goes to the ledger as a request, and from there through the day's files, the
history, the systems' answers and the follow-up like every other.

One request per target: its area (the features' polygons; a point a square;
a line or points their box), the days of the opportunities chosen (JST), the
satellites (and modes) of those, 光学 / SAR when they are all one kind, and a
note of the shots.
"""
from __future__ import annotations

import math
from datetime import datetime
from typing import Optional, Sequence

from ..plan.access import Opportunity, PlanTarget
from ..plan.satellites import SatelliteSpec
from .model import JST, ImagingRequest, day_bounds, jst_day, square_rings

#: How many shots the note lists.
NOTE_SHOTS = 5
#: Padding (degrees) of the box of a line or of several points.
BOX_PAD = 0.005


def _closed(ring: list) -> list:
    ring = [list(p[:2]) for p in ring]
    return ring + [ring[0]] if ring and ring[0] != ring[-1] else ring


def target_rings(target: PlanTarget, point_km: float) -> tuple[list, str]:
    """A target's area as request rings (WGS 84) and its shape kind (範囲 / 点)."""
    rings = [_closed(r) for r in (target.rings or []) if len(r) >= 3]
    if rings:
        return rings, "範囲"
    points = [p for p in (target.points or []) if len(p) >= 2]
    if len(points) >= 2:
        xs, ys = [p[0] for p in points], [p[1] for p in points]
        w, s, e, n = min(xs) - BOX_PAD, min(ys) - BOX_PAD, max(xs) + BOX_PAD, max(ys) + BOX_PAD
        return [[[w, s], [e, s], [e, n], [w, n], [w, s]]], "範囲"
    lon, lat = target.center[0], target.center[1]
    return square_rings(lon, lat, point_km), "点"


def _when(ms: float) -> str:
    return datetime.fromtimestamp(ms / 1000, tz=JST).strftime("%m/%d %H:%M")


def shot_note(ops: Sequence[Opportunity], sats: Sequence[SatelliteSpec]) -> str:
    """「撮像計画から: 11/10 10:32 OPT-X 1（オフナディア 12.3°）、…」."""
    parts = []
    for op in sorted(ops, key=lambda o: o.time)[:NOTE_SHOTS]:
        sat = sats[op.satellite]
        name = f"{sat.name}（{sat.mode}）" if sat.mode else sat.name
        angle = f"（オフナディア {op.offNadir:.1f}°）" if math.isfinite(op.offNadir) else ""
        parts.append(f"{_when(op.time)} {name}{angle}")
    more = f" ほか {len(ops) - NOTE_SHOTS} 回" if len(ops) > NOTE_SHOTS else ""
    return "撮像計画から: " + "、".join(parts) + more if parts else ""


def request_of_ops(ops: Sequence[Opportunity], sats: Sequence[SatelliteSpec], target: PlanTarget, new_id: str,
                   point_km: float = 10.0, base: Optional[ImagingRequest] = None) -> ImagingRequest:
    """A 臨時 request (期間内1回) for one target's opportunities; the rest from `base` (the last inputs)."""
    req = ImagingRequest(**{**base.__dict__}) if base is not None else ImagingRequest(id=new_id)
    req.id, req.object_id, req.created, req.updated = new_id, None, None, None
    req.name = target.label
    req.category, req.achieve, req.status = "臨時", "期間内1回", "有効"
    req.rings, req.shape_kind = target_rings(target, point_km)
    times = [op.time for op in ops]
    if times:
        req.start = day_bounds(jst_day(min(times)))[0]
        req.end = day_bounds(jst_day(max(times)))[1]
    chosen = [sats[op.satellite] for op in sorted(ops, key=lambda o: o.time)]
    req.satellites = list(dict.fromkeys(s.name for s in chosen))
    req.modes = list(dict.fromkeys(s.mode for s in chosen if s.mode))
    # The satellites say what is to be taken; series and systems are not narrowed further.
    req.series = []
    kinds = {s.sar for s in chosen}
    req.kind = ("SAR" if kinds == {True} else "光学") if len(kinds) == 1 else "任意"
    if req.kind != "光学":
        req.cloud_no_go = None
    req.note = shot_note(ops, sats)
    return req


def requests_of_plan(ops: Sequence[Opportunity], sats: Sequence[SatelliteSpec], targets: Sequence[PlanTarget], new_id,
                     point_km: float = 10.0, base: Optional[ImagingRequest] = None) -> list[ImagingRequest]:
    """One request per target of the opportunities (in the targets' order); `new_id()` gives each its id."""
    by_target: dict[int, list] = {}
    for op in ops:
        by_target.setdefault(op.target, []).append(op)
    return [request_of_ops(by_target[i], sats, targets[i], new_id(), point_km, base) for i in sorted(by_target)]
