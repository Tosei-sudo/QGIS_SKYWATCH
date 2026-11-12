"""
Which things found by an AI are the things of the images before or after
(継続): each found thing is matched, one to one, to a thing carried over
into its image (carry_overs), by how near it is, then by its class, length
and heading.

The AI's classes and the classification (Taxonomy) need not agree, so the
class only weighs: two things of different top classes (船舶 and 航空機) are
never one, and a class the classification does not know only makes a match
less sure. A match is one of three:

- auto: near (within `radius` m, or half the thing's length), of the same
  class (or one above or below it), of a like length, and nothing else
  close enough to be confused with it: recorded as the same thing.
- candidate: within `candidate_radius` m but not sure (another one near, an
  unknown or sibling class, another length): offered, recorded as a new
  thing unless chosen.
- new: nothing carried over near it.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Iterable, Optional, Sequence

from .flows import CarryOver, _heading_gap
from .geo import get_distance, js_round, to_fixed
from .model import Observation
from .taxonomy import Taxon, kind_of, path_of, within

#: Nearer than this (m) can be the same thing without asking (or half its length, the longer).
CONTINUE_METRES = 50.0
#: Nearer than this (m) is offered as a candidate (or twice its length, the longer).
CANDIDATE_METRES = 300.0
#: Lengths further apart than this (of the longer) are not sure.
LENGTH_GAP = 0.3

AUTO, CANDIDATE, NEW = "auto", "candidate", "new"


@dataclass
class Found:
    """A thing an AI found: where, as what, how long and which way."""
    lon: float
    lat: float
    kind: str = ""
    length: Optional[float] = None
    heading: Optional[float] = None
    radius: float = CONTINUE_METRES
    candidate_radius: float = CANDIDATE_METRES


@dataclass
class ContinueOption:
    """A thing carried over that a found one may be."""
    #: Its record the match is judged by (the nearest in time).
    source: Observation
    distance: float
    #: Lower is likelier.
    score: float
    #: Whether it would be the same thing without asking (were nothing else near).
    strong: bool
    #: Why: 「12 m」「同じ分類」「分類不明」「長さ ±4%」…
    reasons: list[str] = field(default_factory=list)

    @property
    def object_id(self) -> str:
        return self.source.object_id

    @property
    def text(self) -> str:
        return f"{self.source.object_name}（{'・'.join(self.reasons)}）"


@dataclass
class Continuation:
    """What a found thing is: auto (the first option), candidate (the options), or new."""
    tier: str
    options: list[ContinueOption] = field(default_factory=list)

    @property
    def pick(self) -> Optional[ContinueOption]:
        """The thing it continues without asking."""
        return self.options[0] if self.tier == AUTO and self.options else None


def class_relation(mine: str, theirs: str, known: Optional[Sequence[Taxon]] = None) -> str:
    """
    How two kinds stand: 'same', 'related' (one above the other), 'sibling'
    (the same top class), 'other' (other top classes), or 'unknown' (one
    missing, or not in the classification `known` when one is given).
    """
    m, t = path_of(mine), path_of(theirs)
    if not m or not t:
        return "unknown"
    if within(theirs, m) or within(mine, t):
        return "same" if m == t else "related"
    if known:
        paths = {kind_of(x.path) for x in known}
        if kind_of(m) not in paths or kind_of(t) not in paths:
            return "unknown"
    return "sibling" if m[0] == t[0] else "other"


_CLASS = {"same": ("同じ分類", -0.1), "related": ("同系の分類", 0.0), "unknown": ("分類不明", 0.2), "sibling": ("別の分類", 0.4)}


def _option(found: Found, source: Observation, known: Optional[Sequence[Taxon]]) -> Optional[ContinueOption]:
    sizes = [x for x in (found.length, source.length) if x is not None and x > 0]
    size = max(sizes) if sizes else 0.0
    near = max(found.radius, size / 2) if found.radius > 0 else 0.0
    reach = max(found.candidate_radius, size * 2, near)
    d = get_distance((source.lon, source.lat), (found.lon, found.lat))
    if d > reach:
        return None
    relation = class_relation(found.kind, source.kind, known)
    if relation == "other":
        return None
    said, weight = _CLASS[relation]
    reasons = [f"{to_fixed(d / 1000, 1)} km" if d >= 1000 else f"{js_round(d)} m", said]
    score = d / reach + weight
    like = True
    if found.length is not None and found.length > 0 and source.length is not None and source.length > 0:
        gap = abs(found.length - source.length) / max(found.length, source.length)
        score += min(1.0, gap * 2)
        like = gap <= LENGTH_GAP
        reasons.append(f"長さ {'同じ' if gap < 0.005 else f'±{js_round(gap * 100)}%'}")
    if found.heading is not None and source.heading is not None:
        gap = _heading_gap(found.heading, source.heading)
        score += gap / 180 * 0.3
        if gap <= 20:
            reasons.append(f"向き差 {js_round(gap)}°")
    strong = near > 0 and d <= near and relation in ("same", "related") and like
    return ContinueOption(source, d, score, strong, reasons)


def _cells(lon: float, lat: float, size: float) -> tuple[int, int]:
    return (int(math.floor(lon / size)), int(math.floor(lat / size)))


def continuations(found: Sequence[Found], carried: Iterable[CarryOver], known: Optional[Sequence[Taxon]] = None) -> list[Continuation]:
    """
    What each found thing is (in the same order), matched one to one with the
    things carried over into its image: the likeliest pairs first, so no two
    found things continue the same thing.
    """
    sources = [c.source for c in carried]
    out = [Continuation(NEW) for _ in found]
    if not found or not sources:
        return out
    # A grid of cells (in degrees, as wide as the farthest reach) so each found thing looks only near itself.
    longest = max([f.length or 0 for f in found] + [s.length or 0 for s in sources])
    reach = max([max(f.candidate_radius, f.radius) for f in found] + [longest * 2, 1.0])
    lat_most = max(abs(f.lat) for f in found)
    size = reach / 111_320 / max(math.cos(math.radians(min(lat_most, 89.0))), 0.01)
    grid: dict[tuple[int, int], list[int]] = {}
    for j, s in enumerate(sources):
        grid.setdefault(_cells(s.lon, s.lat, size), []).append(j)
    pairs: list[tuple[float, int, int]] = []
    options: list[dict[int, ContinueOption]] = [{} for _ in found]
    for i, f in enumerate(found):
        cx, cy = _cells(f.lon, f.lat, size)
        for dx in (-1, 0, 1):
            for dy in (-1, 0, 1):
                for j in grid.get((cx + dx, cy + dy), ()):
                    o = _option(f, sources[j], known)
                    if o is not None:
                        options[i][j] = o
                        pairs.append((o.score, i, j))
    # One to one, the likeliest first.
    pairs.sort()
    taken_found: dict[int, int] = {}
    taken_source: set[int] = set()
    for _score, i, j in pairs:
        if i in taken_found or j in taken_source:
            continue
        taken_found[i] = j
        taken_source.add(j)
    strong_of_source: dict[int, int] = {}
    for i in range(len(found)):
        for j, o in options[i].items():
            if o.strong:
                strong_of_source[j] = strong_of_source.get(j, 0) + 1
    for i in range(len(found)):
        if not options[i]:
            continue
        mine = taken_found.get(i)
        ranked = sorted(options[i].items(), key=lambda x: (x[0] != mine, x[1].score, x[1].distance))
        listed = [o for _j, o in ranked]
        sure = (mine is not None and options[i][mine].strong
                and sum(1 for o in options[i].values() if o.strong) == 1
                and strong_of_source.get(mine, 0) == 1)
        out[i] = Continuation(AUTO if sure else CANDIDATE, listed)
    return out
