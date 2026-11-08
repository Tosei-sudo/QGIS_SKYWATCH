"""
Imaging plans for moving targets, as the viewer (imaging-plan-moving.ts and
moving-target.ts): each target is carried on from its last report along its
course at its speed (dead reckoning), and a circle that grows with the time
since that report says how far off the guess may be. Passes over where the
target will be are found in two steps: the guessed places every hour are
planned as points, then each pass found is planned again for the circle
around where the target is at that very moment, so the scene is aimed there
and its coverage of the circle tells how likely the target is in the picture.
"""
from __future__ import annotations

import calendar
import math
import re
import time
from dataclasses import dataclass, replace
from typing import Any, Callable, Iterable, Optional, Sequence

from .access import Opportunity, PlanOptions, PlanResult, PlanTarget, plan_access


@dataclass
class UncertaintyModel:
    """How far off a guessed place may be: `base` km at the report, growing by `share` of the distance run and `perHour` km an hour."""
    base: float = 0.5
    share: float = 0.1
    perHour: float = 0.5


DEFAULT_UNCERTAINTY = UncertaintyModel()


@dataclass
class ShipState:
    """A target's last report: where it was, which way and how fast it went."""
    id: str
    name: str
    #: ms since 1970.
    t: float
    lon: float
    lat: float
    #: Degrees clockwise from north; None when unknown (the target is taken as staying).
    course: Optional[float]
    #: Knots; None when unknown.
    speed: Optional[float]
    model: Optional[UncertaintyModel] = None


#: How far ahead of a target's last report it is guessed at most, hours.
MAX_HORIZON = 72
#: The most targets planned at once.
MAX_SHIPS = 50
#: Hours between the places planned in the first step.
SAMPLE_HOURS = 1
#: The most passes planned again in the second step.
MAX_CANDIDATES = 400
KNOT_KMH = 1.852
R = 6371.0088
RAD = math.pi / 180
HOUR = 3600000


def _mod(a: float, n: float) -> float:
    return ((a % n) + n) % n


def destination(lon_lat: Sequence[float], bearing: float, km: float) -> tuple:
    """The point `km` from `lon_lat` along the great circle at `bearing` degrees."""
    lon, lat = lon_lat
    d = km / R
    p, l, b = lat * RAD, lon * RAD, bearing * RAD
    p2 = math.asin(max(-1.0, min(1.0, math.sin(p) * math.cos(d) + math.cos(p) * math.sin(d) * math.cos(b))))
    l2 = l + math.atan2(math.sin(b) * math.sin(d) * math.cos(p), math.cos(d) - math.sin(p) * math.sin(p2))
    return (_mod(l2 / RAD + 180, 360) - 180, p2 / RAD)


def ship_at(ship: ShipState, t: float) -> tuple:
    """Where the target is guessed to be at `t`: its last place carried on along its course at its speed."""
    hours = (t - ship.t) / HOUR
    if ship.course is None or not ship.speed or hours <= 0:
        return (ship.lon, ship.lat)
    return destination((ship.lon, ship.lat), ship.course, ship.speed * KNOT_KMH * hours)


def uncertainty_at(ship: ShipState, t: float) -> float:
    """How far off the guess at `t` may be, km."""
    m = ship.model or DEFAULT_UNCERTAINTY
    hours = max(0.0, (t - ship.t) / HOUR)
    return m.base + hours * (m.share * (ship.speed or 0) * KNOT_KMH + m.perHour)


_RINGS = ((0.3, 8), (0.55, 14), (0.8, 20), (0.97, 26))


def circle_target(ship: ShipState, t: float) -> PlanTarget:
    """A circle target around where the target is at `t`: its rim and points spread inside (for the coverage)."""
    center = ship_at(ship, t)
    radius = uncertainty_at(ship, t)
    points = [destination(center, i * 15, radius) for i in range(24)]
    cells = [center]
    for ring, n in _RINGS:
        cells.extend(destination(center, i * 360 / n, radius * ring) for i in range(n))
    return PlanTarget(ship.name, center, points, cells, uncertainty=radius, guessed=t - ship.t)


def ship_window(ships: Sequence[ShipState], start: float, end: float, horizon: float = MAX_HORIZON) -> tuple:
    """The period from `start` to `end` within the targets' reach: until the last report plus the horizon."""
    last = max(s.t for s in ships)
    return (start, min(end, last + horizon * HOUR))


def sample_targets(ships: Sequence[ShipState], start: float, end: float, horizon: float = MAX_HORIZON) -> tuple:
    """The places of the first step: each target every hour over the period, and (target index, time) each stands for."""
    step = SAMPLE_HOURS * HOUR
    targets: list[PlanTarget] = []
    samples: list[tuple] = []
    for k, ship in enumerate(ships):
        until = min(end, ship.t + horizon * HOUR)
        t = start
        while t <= until + step / 2:
            at = min(t, until)
            targets.append(PlanTarget(ship.name, ship_at(ship, at), []))
            samples.append((k, at))
            if at == until:
                break
            t += step
    return targets, samples


@dataclass
class ShipShot:
    """One shot of a target: its pass and the circle it is aimed at."""
    ship: int
    op: Opportunity
    target: PlanTarget


def refine_shots(first: PlanResult, samples: Sequence[tuple], ships: Sequence[ShipState], sats: Sequence,
                 options: PlanOptions, cancelled: Optional[Callable[[], bool]] = None) -> list[ShipShot]:
    """
    The second step: the first step's passes whose moment falls in the hour a
    place stands for, each planned again (twice, to follow the target to the
    moment of the pass) for the circle around where the target is then. One
    shot per satellite pass and target, in time order.
    """
    half = SAMPLE_HOURS * HOUR / 2
    candidates = [op for op in first.opportunities if abs(op.time - samples[op.target][1]) <= half][:MAX_CANDIDATES]
    shots: list[ShipShot] = []
    for candidate in candidates:
        if cancelled and cancelled():
            break
        k = samples[candidate.target][0]
        ship = ships[k]
        sat = sats[candidate.satellite]
        t = candidate.time
        found = None
        for _ in range(2):
            target = circle_target(ship, t)
            near = plan_access([sat], [target], replace(options, start=t - 20 * 60000, end=t + 20 * 60000, stations=[])).opportunities
            if not near:
                break
            op = min(near, key=lambda o: abs(o.time - t))
            op.satellite = candidate.satellite
            if candidate.downlink:
                op.downlink = candidate.downlink
            found = (op, target)
            if abs(op.time - t) < 1000:
                break
            t = op.time
        if not found:
            continue
        op, target = found
        if any(s.ship == k and s.op.satellite == op.satellite and abs(s.op.time - op.time) < 10 * 60000 for s in shots):
            continue
        shots.append(ShipShot(k, op, target))
    shots.sort(key=lambda s: s.op.time)
    return shots


def ship_plan(shots: Sequence[ShipShot], first: PlanResult) -> tuple:
    """The shots as a plan: one target (the circle) per shot, the shots pointing at them. (targets, result)"""
    ops = []
    for i, s in enumerate(shots):
        s.op.target = i
        ops.append(s.op)
    return [s.target for s in shots], PlanResult(ops, list(first.problems), first.tooSoon)


def plan_ships(sats: Sequence, all_ships: Sequence[ShipState], options: PlanOptions,
               on_progress: Optional[Callable[[float], None]] = None,
               cancelled: Optional[Callable[[], bool]] = None) -> tuple:
    """
    Both steps (as the viewer's runShips): the latest {@link MAX_SHIPS}
    targets, planned over the period within their reach. (targets, result,
    options as planned); a text when nothing can be planned.
    """
    ships = sorted(all_ships, key=lambda s: -s.t)[:MAX_SHIPS]
    left = len(all_ships) - len(ships)
    if not ships:
        return "位置報告のある移動目標なし"
    start, end = ship_window(ships, options.start, options.end)
    if end <= start:
        return f"移動目標の位置は最後の報告から {MAX_HORIZON} 時間先までのみ推測。開始日時を報告の近くで指定"
    planned = replace(options, start=start, end=end)
    targets, samples = sample_targets(ships, start, end)
    first = plan_access(sats, targets, planned, (lambda d: on_progress(d * 0.8)) if on_progress else None)
    shots = refine_shots(first, samples, ships, sats, planned, cancelled)
    if on_progress:
        on_progress(1.0)
    circles, result = ship_plan(shots, first)
    if left > 0:
        result.problems.append(f"移動目標が多いため最近報告のあった {MAX_SHIPS} 件だけを計算（ほか {left} 件）")
    return circles, result, planned


# Reading targets from a layer of reports (moving-target.ts, moving.ts).

#: Roles of the fields, in the viewer's order.
MOVING_ROLES = ("id", "time", "lat", "lon", "heading", "course", "speed", "name", "kind")
NAMES = {
    "id": ["mmsi", "icao24", "icao", "hex", "userid", "trackid", "vesselid", "imo", "id", "uid", "callsign", "flight"],
    "time": ["timestamp", "timeutc", "time", "datetime", "basedatetime", "lastcontact", "timeposition", "postime", "ts", "date", "updated", "lastupdate", "receivedat"],
    "lat": ["lat", "latitude", "_lat"],
    "lon": ["lon", "lng", "long", "longitude", "_lon"],
    "heading": ["trueheading", "heading", "hdg", "truehdg", "navheading", "magheading"],
    "course": ["cog", "course", "truetrack", "track", "dir", "direction"],
    "speed": ["sog", "speed", "velocity", "gs", "groundspeed", "spd"],
    "name": ["shipname", "vesselname", "name", "callsign", "flight", "registration", "reg"],
    "kind": ["kind", "vesseltype", "shiptype", "aircrafttype", "category", "type", "typecode"],
}

#: Roles of a target layer and how the dialog names them.
TARGET_ROLES = {"id": "ID", "time": "時刻", "course": "針路（度）", "speed": "速度", "name": "名前"}
SPEED_UNITS = {"kn": "ノット", "kmh": "km/h", "ms": "m/s"}
TO_KNOTS = {"kn": 1.0, "kmh": 1 / 1.852, "ms": 3600 / 1852}


def _norm(key: str) -> str:
    return re.sub(r"[\s_-]+", "", key[key.rfind(".") + 1:].lower()) or key


def _name_of(key: str) -> str:
    return key if key in ("_lat", "_lon") else _norm(key)


def detect_fields(keys: Iterable[str], given: Optional[dict] = None) -> dict:
    """The field of each role among `keys`, by name; `given` wins. A field serves one role only."""
    given = given or {}
    all_keys = list(dict.fromkeys(keys))
    used = {v for v in given.values() if v}
    found: dict = {}
    for role in MOVING_ROLES:
        if given.get(role):
            found[role] = given[role]
            continue
        for candidate in NAMES[role]:
            key = next((k for k in all_keys if k not in used and _name_of(k) == candidate), None)
            if key:
                found[role] = key
                used.add(key)
                break
    return found


def guess_target_fields(keys: Iterable[str]) -> dict:
    """The attributes found by name among `keys` (as the moving-object feeds find them)."""
    f = detect_fields(keys)
    out = {"id": f.get("id"), "time": f.get("time"), "course": f.get("course") or f.get("heading"), "speed": f.get("speed"), "name": f.get("name")}
    return {k: v for k, v in out.items() if v}


def guess_speed_unit(field: Optional[str]) -> str:
    """The unit a speed field's name suggests (sog and the like are knots)."""
    name = (field or "").lower()
    return "kmh" if re.search(r"kmh|km_h|kph", name) else "ms" if re.search(r"ms$|m_s|mps", name) else "kn"


_NUMBER = re.compile(r"^[-+]?(\d+\.?\d*|\.\d+)(e[-+]?\d+)?$", re.I)


def number_of(v: Any) -> Optional[float]:
    """A number from a number or numeric text; None otherwise."""
    if isinstance(v, bool):
        return None
    if isinstance(v, (int, float)):
        return float(v) if math.isfinite(v) else None
    if isinstance(v, str) and v.strip() and _NUMBER.match(v.strip()):
        return float(v.strip())
    return None


_ISO = re.compile(r"^(\d{4})([-/.:])(\d{1,2})\2(\d{1,2})(?:[T\s]+(\d{1,2}):(\d{2})(?::(\d{2})(?:[.,](\d+))?)?)?\s*(Z|z|[+-]\d{2}:?\d{2}|[+-]\d{2})?$")
_COMPACT = re.compile(r"^(\d{4})(\d{2})(\d{2})(?:[T_ ]?(\d{2})(\d{2})(\d{2})?(?:\.(\d+))?)?(Z)?$")
_JAPANESE = re.compile(r"^(\d{4})年(\d{1,2})月(?:(\d{1,2})日)?\s*(?:(\d{1,2})[時:](?:(\d{1,2})分?)?(?:(\d{1,2})秒?)?)?$")


def _utc(y, mo, d, h=0, mi=0, s=0, ms=0, zone: Optional[str] = None) -> Optional[float]:
    if not (1800 <= y <= 2200 and 1 <= mo <= 12 and 1 <= d <= 31 and h <= 24 and mi <= 59 and s <= 60):
        return None
    if d > calendar.monthrange(y, mo)[1]:
        return None
    t = (calendar.timegm((y, mo, d, 0, 0, 0)) + h * 3600 + mi * 60 + s) * 1000.0 + ms
    if zone and zone not in ("Z", "z"):
        m = re.match(r"^([+-])(\d{2}):?(\d{2})?$", zone)
        if not m:
            return None
        offset = (int(m.group(2)) * 60 + int(m.group(3) or 0)) * (-1 if m.group(1) == "-" else 1)
        t -= offset * 60000
    return t


def _int(s: Optional[str]) -> int:
    return int(s) if s else 0


def _fraction(s: Optional[str]) -> int:
    return math.floor(float(f"0.{s}") * 1000 + 0.5) if s else 0


def time_of(v: Any) -> Optional[float]:
    """
    A report's time: ms or s since 1970 as numbers, or text (ISO 8601 and the
    like). Text without a time zone is UTC, as AIS and ADS-B times are.
    """
    n = number_of(v)
    if n is not None:
        return n if n > 1e11 else n * 1000 if n > 1e8 else None
    if not isinstance(v, str):
        return None
    text = re.sub(r"\s*UTC$", "", v.strip(), flags=re.I)
    if not text:
        return None
    text = re.sub(r"([.,]\d{3})\d+", r"\1", text, count=1)
    m = _ISO.match(text)
    if m:
        y, _, mo, d, h, mi, s, f, zone = m.groups()
        return _utc(int(y), int(mo), int(d), _int(h), _int(mi), _int(s), _fraction(f), zone)
    m = _COMPACT.match(text)
    if m:
        y, mo, d, h, mi, s, f, _zone = m.groups()
        return _utc(int(y), int(mo), int(d), _int(h), _int(mi), _int(s), _fraction(f))
    m = _JAPANESE.match(text)
    if m:
        y, mo, d, h, mi, s = m.groups()
        return _utc(int(y), int(mo), int(d) if d else 1, _int(h), _int(mi), _int(s))
    return None


def _inverse(a: Sequence[float], b: Sequence[float]) -> tuple:
    """The great-circle distance (km) and initial bearing (degrees) from `a` to `b`."""
    p1, p2, dl = a[1] * RAD, b[1] * RAD, (b[0] - a[0]) * RAD
    h = math.sin((p2 - p1) / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    km = 2 * R * math.asin(min(1.0, math.sqrt(h)))
    bearing = (math.atan2(math.sin(dl) * math.cos(p2), math.cos(p1) * math.sin(p2) - math.sin(p1) * math.cos(p2) * math.cos(dl)) / RAD + 360) % 360
    return km, bearing


def _angle_of(v: Any) -> Optional[float]:
    n = number_of(v)
    return None if n is None or n < 0 or n >= 360 else n


@dataclass
class Report:
    """One report: a feature's attributes and its place."""
    attributes: dict
    lonLat: tuple


def targets_of(reports: Sequence[Report], fields: dict, unit: str, model: UncertaintyModel, now: Optional[float] = None) -> list[ShipState]:
    """
    The targets of `reports` by `fields`: one per id, at its last report
    (`now` for reports without a time). A target without a course or a speed
    of its own takes them from its last two reports at least a minute apart.
    Speeds are read in `unit` and kept in knots. Reports without an id are left out.
    """
    if now is None:
        now = time.time() * 1000
    by_id: dict = {}
    for report in reports:
        raw = report.attributes.get(fields["id"]) if fields.get("id") else None
        if isinstance(raw, bool):
            raw = None
        rid = (str(int(raw)) if isinstance(raw, float) and raw.is_integer() else str(raw)) if isinstance(raw, (int, float)) else raw.strip() if isinstance(raw, str) else ""
        if not rid or not all(math.isfinite(x) for x in report.lonLat):
            continue
        t = (time_of(report.attributes.get(fields["time"])) if fields.get("time") else None)
        by_id.setdefault(rid, []).append((now if t is None else t, report))
    targets = []
    for rid, items in by_id.items():
        items.sort(key=lambda r: r[0])
        t, report = items[-1]
        attrs = report.attributes
        course = _angle_of(attrs.get(fields["course"])) if fields.get("course") else None
        given = number_of(attrs.get(fields["speed"])) if fields.get("speed") else None
        speed = given * TO_KNOTS[unit] if given is not None and given >= 0 else None
        before = next((r for r in reversed(items) if r[0] < t - 60000), None)
        if before and (course is None or speed is None):
            km, bearing = _inverse(before[1].lonLat, report.lonLat)
            if course is None:
                course = bearing if km > 0.01 else None
            if speed is None:
                speed = km / 1.852 / ((t - before[0]) / HOUR)
        name_raw = attrs.get(fields["name"]) if fields.get("name") else None
        name = name_raw.strip() if isinstance(name_raw, str) and name_raw.strip() else (
            str(name_raw) if isinstance(name_raw, (int, float)) and not isinstance(name_raw, bool) else rid)
        targets.append(ShipState(rid, name, t, report.lonLat[0], report.lonLat[1], course, speed, model))
    return targets


@dataclass
class PredictedPlace:
    """A predicted place of a target: where it is guessed at `t` and the circle it may be off by."""
    target: ShipState
    t: float
    center: tuple
    #: km.
    radius: float
    #: The circle, closed.
    ring: list


def predicted_places(targets: Sequence[ShipState], start: float, horizon: float, step_hours: float) -> list[PredictedPlace]:
    """
    The places of each target every `step_hours` from `start` (or its last
    report, when later) to `horizon` hours after its report, the last one at
    the horizon itself.
    """
    step = max(0.25, step_hours) * HOUR
    out = []
    for target in targets:
        until = target.t + horizon * HOUR
        t = max(start, target.t)
        while t <= until + 1:
            at = min(t, until)
            circle = circle_target(target, at)
            out.append(PredictedPlace(target, at, ship_at(target, at), uncertainty_at(target, at), [*circle.points, circle.points[0]]))
            if at == until:
                break
            t += step
    return out


def predicted_track(target: ShipState, horizon: float) -> list:
    """The predicted track of a target, from its report to the horizon, as points every hour."""
    return [ship_at(target, target.t + h * HOUR) for h in range(0, math.floor(horizon) + 1)]


def _r(v: Optional[float], digits: int) -> Optional[float]:
    if v is None:
        return None
    p = 10 ** digits
    return math.floor(v * p + 0.5) / p


def places_layers(targets: Sequence[ShipState], horizon: float, step_hours: float) -> tuple:
    """
    The predicted places as two layers' contents: a circle per target every
    `step_hours` (「存在予測」, with its time) and each target's predicted
    track (「予測航跡」, cut at the antimeridian). (circles, tracks)
    """
    from .output import LayerField, PlanLayer
    from .track import wrap_line, wrap_polygon

    fields = [LayerField("kind", "string", "種類"), LayerField("target_id", "string", "目標ID"), LayerField("name", "string", "名前"),
              LayerField("time", "date", "時刻"), LayerField("hours", "double", "報告からの時間"),
              LayerField("radius_km", "double", "誤差の半径km"), LayerField("course", "double", "針路"),
              LayerField("speed_kn", "double", "速力ノット")]
    circles = [(wrap_polygon(p.ring), {"kind": "存在予測", "target_id": p.target.id, "name": p.target.name, "time": p.t,
                           "hours": _r((p.t - p.target.t) / HOUR, 2), "radius_km": _r(p.radius, 2),
                           "course": _r(p.target.course, 1), "speed_kn": _r(p.target.speed, 1)})
               for p in predicted_places(targets, -math.inf, horizon, step_hours)]
    tracks = [(wrap_line(predicted_track(t, horizon)), {"kind": "予測航跡", "target_id": t.id, "name": t.name, "time": t.t, "hours": horizon,
                                                         "radius_km": None, "course": _r(t.course, 1), "speed_kn": _r(t.speed, 1)})
              for t in targets]
    return PlanLayer("存在予測", fields, circles), PlanLayer("予測航跡", fields, [t for t in tracks if t[0]])
