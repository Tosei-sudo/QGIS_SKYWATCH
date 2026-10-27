"""
What the records say over time, as the viewer (observations.ts): a thing's
flow (流れ) in image-time order, the things of other images that may still
be in this one (継続), the things that may be the one at a place (同一候補),
and a thing measured by its front and rear (2 点).
"""
from __future__ import annotations

import math
import time
from dataclasses import dataclass, field, replace
from typing import Any, Callable, Iterable, Optional, Sequence

from .geo import bearing, get_distance, js_round, offset, to_fixed
from .model import MATCH_METRES, Observation, new_observation_id
from .taxonomy import kind_of, path_of, within

#: Farther than this (m) between two records counts as having moved.
MOVE_METRES = 30.0


def time_key(o: Observation) -> int:
    """When a record sits in a flow: its image's time, else when it was made."""
    return o.image_time if o.image_time is not None else o.created_at


def _by_time(o: Observation) -> tuple[int, int]:
    return (time_key(o), o.created_at)


def living(records: Iterable[Observation]) -> list[Observation]:
    """The records that count (not deleted)."""
    return [o for o in records if o.deleted_at is None]


def _distance(a: Observation, b: Observation) -> float:
    return get_distance((a.lon, a.lat), (b.lon, b.lat))


@dataclass
class FlowStep:
    """A stretch of a flow: one activity (or absence) over consecutive images."""
    activity: str
    present: bool
    start: int
    end: int
    count: int
    #: How far (m) the thing had moved since the step before, at the step's first record.
    moved: Optional[float]
    observations: list[Observation] = field(default_factory=list)


@dataclass
class ObjectFlow:
    """A thing's flow: its records in image-time order, and what they say."""
    object_id: str
    name: str
    kind: str
    observations: list[Observation]
    first: int
    last: int
    #: The images it was seen in.
    seen: int
    #: The latest record's activity, and whether it was there then.
    activity: str
    present: bool
    #: The path length (m) between the places it was seen.
    distance: float
    steps: list[FlowStep]


def flow_of(records: Iterable[Observation]) -> ObjectFlow:
    """The flow of one thing (its living records, any order)."""
    rows = sorted(records, key=_by_time)
    last = rows[-1]
    steps: list[FlowStep] = []
    path = 0.0
    previous: Optional[Observation] = None
    for o in rows:
        moved = _distance(previous, o) if previous is not None and o.present and previous.present else None
        if moved is not None:
            path += moved
        step = steps[-1] if steps else None
        same = (step is not None and step.present == o.present and (not o.present or step.activity == o.activity)
                and not (moved is not None and moved > MOVE_METRES))
        if same:
            step.end = time_key(o)
            step.count += 1
            step.observations.append(o)
        else:
            steps.append(FlowStep(o.activity if o.present else "", o.present, time_key(o), time_key(o), 1, moved if steps else None, [o]))
        if o.present:
            previous = o
    # The name and kind as last written; the kind as deep as any record knows it below that (one image may tell only 「船舶」).
    named = next((o for o in reversed(rows) if o.object_name), last)
    written = next((o for o in reversed(rows) if o.kind), named)
    kind = written.kind
    for o in rows:
        if within(o.kind, path_of(kind)) and len(path_of(o.kind)) > len(path_of(kind)):
            kind = o.kind
    return ObjectFlow(
        object_id=last.object_id,
        name=named.object_name or last.object_id,
        kind=kind_of(path_of(kind)),
        observations=rows,
        first=time_key(rows[0]),
        last=time_key(last),
        seen=sum(1 for o in rows if o.present),
        activity=last.activity,
        present=last.present,
        distance=path,
        steps=steps,
    )


def _groups(records: Iterable[Observation]) -> dict[str, list[Observation]]:
    groups: dict[str, list[Observation]] = {}
    for o in records:
        groups.setdefault(o.object_id, []).append(o)
    return groups


def flows(records: Iterable[Observation]) -> list[ObjectFlow]:
    """Every thing's flow (from living records), the most recently seen first."""
    out = [flow_of(g) for g in _groups(living(records)).values()]
    return sorted(out, key=lambda f: (-f.last, f.name))


@dataclass
class CarryOver:
    """A thing recorded in another image that may be in this one too."""
    #: Its nearest record in time.
    source: Observation
    #: 'before' (an earlier image) or 'after'.
    side: str


@dataclass
class ImageRef:
    """An image as the records see it."""
    id: str
    name: str = ""
    time: Optional[int] = None


def carry_overs(records: Iterable[Observation], image: ImageRef, inside: Optional[Callable[[float, float], bool]] = None) -> list[CarryOver]:
    """
    What may still be there in an image: each thing not yet recorded in it,
    by its nearest record in time — an earlier one first, else a later one —
    unless that record says it had gone. `inside` limits them to a place
    (the image's footprint).
    """
    here: set[str] = set()
    others: dict[str, list[Observation]] = {}
    for o in living(records):
        if o.image_id == image.id:
            here.add(o.object_id)
        else:
            others.setdefault(o.object_id, []).append(o)
    out: list[CarryOver] = []
    for object_id, rows in others.items():
        if object_id in here:
            continue
        rows.sort(key=_by_time)
        before = rows if image.time is None else [o for o in rows if time_key(o) <= image.time]
        pick = CarryOver(before[-1], "before") if before else CarryOver(rows[0], "after")
        if not pick.source.present:
            continue
        if inside is not None and not inside(pick.source.lon, pick.source.lat):
            continue
        out.append(pick)
    return sorted(out, key=lambda c: c.source.object_name)


def carried_into(source: Observation, image: ImageRef, now: Optional[int] = None, **changes: Any) -> Observation:
    """A copy of `source` for another image (the same thing, its place and activity), stamped now."""
    now = now_ms() if now is None else now
    return replace(source, **{"note": "", **changes}, id=new_observation_id(), image_id=image.id, image_name=image.name,
                   image_time=image.time, created_at=now, updated_at=now, deleted_at=None)


@dataclass
class MatchCandidate:
    """A thing recorded in other images that may be the one at a place (同一候補)."""
    object_id: str
    name: str
    kind: str
    #: The record it is judged by: its nearest in time where it was seen.
    source: Observation
    #: How far (m) that record is from the place.
    distance: float
    #: Lower is likelier.
    score: float
    #: Why it is offered: 「120 m」「同じ分類」「長さ ±4%」…
    reasons: list[str]


def _heading_gap(a: float, b: float) -> float:
    d = abs(math.fmod(math.fmod(a - b, 360) + 360, 360))
    return min(d, 360 - d)


def match_candidates(records: Iterable[Observation], image: ImageRef, lon: float, lat: float, kind: str = "",
                     length: Optional[float] = None, heading: Optional[float] = None,
                     radius: float = MATCH_METRES, limit: int = 5) -> list[MatchCandidate]:
    """
    The things recorded in other images (not yet in this one) that may be the
    one at a place, likeliest first: near (within `radius` m) where they were
    last seen (the nearest record in time, an earlier one first), of the same
    class, of a like length and heading, and seen not long before.
    """
    here: set[str] = set()
    others: dict[str, list[Observation]] = {}
    for o in living(records):
        if o.image_id == image.id:
            here.add(o.object_id)
        elif o.present:
            others.setdefault(o.object_id, []).append(o)
    mine = path_of(kind or "")
    out: list[MatchCandidate] = []
    for object_id, rows in others.items():
        if object_id in here:
            continue
        rows.sort(key=_by_time)
        before = rows if image.time is None else [o for o in rows if time_key(o) <= image.time]
        source = before[-1] if before else rows[0]
        d = get_distance((source.lon, source.lat), (lon, lat))
        if d > radius:
            continue
        flow = flow_of(rows)
        reasons = [f"{to_fixed(d / 1000, 1)} km" if d >= 1000 else f"{js_round(d)} m"]
        score = d / radius
        # The class: the same one (or one above or below it) counts for it, a sibling of the same top class a little against it, another top class against it, an unknown one a little.
        theirs = path_of(flow.kind)
        if mine and theirs:
            if within(flow.kind, mine) or within(kind_of(mine), theirs):
                reasons.append("同じ分類" if kind_of(mine) == flow.kind else "同系の分類")
                score -= 0.1
            else:
                score += 0.4 if mine[0] == theirs[0] else 1
        else:
            score += 0.2
        if length is not None and length > 0 and source.length is not None and source.length > 0:
            gap = abs(length - source.length) / max(length, source.length)
            score += min(1, gap * 2)
            reasons.append(f"長さ {'同じ' if gap < 0.005 else f'±{js_round(gap * 100)}%'}")
        if heading is not None and source.heading is not None:
            gap = _heading_gap(heading, source.heading)
            score += gap / 180 * 0.3
            if gap <= 20:
                reasons.append(f"向き差 {js_round(gap)}°")
        # Seen long before (or after): a little less likely.
        if image.time is not None and source.image_time is not None:
            score += min(1, abs(image.time - source.image_time) / (30 * 86_400_000)) * 0.2
        out.append(MatchCandidate(object_id, flow.name, flow.kind, source, d, score, reasons))
    out.sort(key=lambda c: (c.score, c.distance))
    return out[:limit]


@dataclass
class Measure:
    """A thing measured by two points: its centre, length (m) and heading (rear to front)."""
    lon: float
    lat: float
    length: float
    heading: float


def two_point(front: Sequence[float], rear: Sequence[float]) -> Measure:
    """A thing measured by two points, its front then its rear."""
    length = get_distance(rear, front)
    heading = bearing(rear, front)
    # The point halfway along.
    lon, lat = offset(rear, length / 2, math.radians(heading))
    return Measure(lon, lat, length, heading)


def ends_of(lon: float, lat: float, length: Optional[float], heading: Optional[float]) -> Optional[tuple[list[float], list[float]]]:
    """The front and rear (lon, lat) of a measured record; None when it is a point."""
    if length is None or heading is None or not length > 0:
        return None
    centre = (lon, lat)
    return offset(centre, length / 2, math.radians(heading)), offset(centre, length / 2, math.radians(heading + 180))


def heading_text(deg: Optional[float]) -> str:
    """`045°`"""
    return "" if deg is None else f"{js_round(deg) % 360:03d}°"


def next_name(kind: str, taken: Iterable[str]) -> str:
    """The next free name of a kind (its deepest class): 船舶-1, 〇〇級-2…"""
    path = path_of(kind)
    base = path[-1] if path else "モノ"
    prefix = base + "-"
    top = 0
    for name in taken:
        if name.startswith(prefix) and name[len(prefix):].isdigit() and name[len(prefix):].isascii():
            top = max(top, int(name[len(prefix):]))
    return f"{base}-{top + 1}"


def now_ms() -> int:
    return int(time.time() * 1000)
