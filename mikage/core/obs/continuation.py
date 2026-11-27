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

What is sure can be set (`aiContinuation`, tsudo 2026-11-26: 「AIの重心が既存
のポリゴンと重なるなら継続みたいな」): rules, each of conditions that must all
hold (near, the AI's centre inside the thing's outline, the thing inside the
AI's polygon, how much their outlines overlap, the kind, length, heading and
the AI's score); a pair that one rule holds for (the first from the top names
it) is sure. With none set, the one rule is the old one: within `radius` m
(or half the length), of the same class or one above or below it, and of a
like length.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any, Iterable, Optional, Sequence

from .flows import CarryOver, _heading_gap, outline_of
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


#: The relations of two kinds each `kind` of a rule allows (class_relation).
KIND_LEVELS: dict[str, tuple[str, ...]] = {
    "same": ("same",),
    "related": ("same", "related"),
    "sibling": ("same", "related", "sibling"),
    "any": ("same", "related", "sibling", "unknown", "other"),
}
#: A rule that says nothing of the kind: any but another top class (船舶 and 航空機 are never one).
NOT_OTHER = ("same", "related", "sibling", "unknown")
#: What each `kind` is called (the rule's name made from its conditions).
KIND_TEXT = {"same": "同じ分類", "related": "同系の分類", "sibling": "同じ大分類", "any": "分類は見ない"}


@dataclass
class Rule:
    """
    One way a found thing is a thing carried over without asking: every
    condition it has holds (one not set is not looked at). A rule has at
    least one of where: near, center_in_record, record_in_ai, overlap.
    """
    #: Its name (the 継続 column and AI_CONTINUATION 「自動継続:…」); '' for one named by its conditions (text).
    label: str = ""
    #: Within this (m) of the thing, or half the longer length.
    near: Optional[float] = None
    #: The AI's centre (of its polygon) inside the thing's outline (a record measured with its width).
    center_in_record: bool = False
    #: The thing's place inside the AI's polygon (or outline).
    record_in_ai: bool = False
    #: Their outlines overlap by at least this (the shared area of both together, 0–1).
    overlap: Optional[float] = None
    #: How alike the kinds must be: same / related / sibling / any; '' any but another top class.
    kind: str = ""
    #: The lengths at most this far apart (of the longer), when both are known.
    length_gap: Optional[float] = None
    #: The headings at most this far apart (°), when both are known.
    heading_gap: Optional[float] = None
    #: The AI's score at least this, when it has one.
    min_score: Optional[float] = None
    #: The judgement before rules could be set (old_rules): AI_CONTINUATION says only 「自動継続」.
    default: bool = False

    @property
    def relations(self) -> tuple[str, ...]:
        return KIND_LEVELS.get(self.kind, NOT_OTHER)

    @property
    def placed(self) -> bool:
        """Whether it says where (a rule without would take anything near enough to be offered)."""
        return (self.near is not None and self.near > 0) or self.center_in_record or self.record_in_ai or self.overlap is not None

    @property
    def text(self) -> str:
        """Its name, else its conditions (「重心が外形の中・同じ分類」)."""
        if self.label:
            return self.label
        said = []
        if self.near is not None and self.near > 0:
            said.append(f"{js_round(self.near)} m 以内")
        if self.center_in_record:
            said.append("重心が外形の中")
        if self.record_in_ai:
            said.append("AIの範囲の中")
        if self.overlap is not None:
            said.append(f"重なり {js_round(self.overlap * 100)}% 以上")
        if self.kind:
            said.append(KIND_TEXT.get(self.kind, self.kind))
        if self.length_gap is not None:
            said.append(f"長さの差 {js_round(self.length_gap * 100)}% 以内")
        if self.heading_gap is not None:
            said.append(f"向きの差 {js_round(self.heading_gap)}° 以内")
        if self.min_score is not None:
            said.append(f"確信度 {self.min_score:.2f} 以上")
        return "・".join(said)


@dataclass
class Rules:
    """How found things are matched: what is sure (rules, one holding is enough) and how near is offered."""
    rules: list[Rule] = field(default_factory=list)
    #: Nearer than this (m) is offered as what it may continue (or twice the length); 0 offers only what a rule holds for.
    candidate_radius: float = CANDIDATE_METRES
    #: Sure only when nothing else could be confused with it (another found thing, another thing carried over).
    unique: bool = True
    #: False: nothing is matched (each found thing is new).
    on: bool = True

    @property
    def reach(self) -> float:
        """How far (m) a found thing looks, at least (lengths add to it)."""
        return max([self.candidate_radius] + [r.near for r in self.rules if r.near is not None])


def old_rules(radius: float = CONTINUE_METRES, candidate_radius: float = CANDIDATE_METRES) -> Rules:
    """
    The judgement before rules could be set: within `radius` m (or half the
    length; 0 never sure), the same class or one above or below, a length
    within 30 %, and unconfused.
    """
    rules = [Rule(near=radius, kind="related", length_gap=LENGTH_GAP, default=True)] if radius > 0 else []
    return Rules(rules, candidate_radius)


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
    #: The AI's score (0–1), None when it gave none.
    score: Optional[float] = None
    #: Its polygon's outer ring, [lon, lat] (WGS 84), else its outline when measured; None for a point.
    ring: Optional[list] = None
    #: How it is matched; None: by `radius` and `candidate_radius` as before rules (old_rules).
    rules: Optional[Rules] = None

    @property
    def settings(self) -> Rules:
        return self.rules if self.rules is not None else old_rules(self.radius, self.candidate_radius)


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
    #: The rule that holds (the first from the top), None when none does.
    rule: Optional[Rule] = None

    @property
    def object_id(self) -> str:
        return self.source.object_id

    @property
    def text(self) -> str:
        said = ([self.rule.label] if self.rule is not None and self.rule.label else []) + self.reasons
        return f"{self.source.object_name}（{'・'.join(said)}）"


@dataclass
class Continuation:
    """What a found thing is: auto (the first option), candidate (the options), or new."""
    tier: str
    options: list[ContinueOption] = field(default_factory=list)

    @property
    def pick(self) -> Optional[ContinueOption]:
        """The thing it continues without asking."""
        return self.options[0] if self.tier == AUTO and self.options else None

    @property
    def tag(self) -> str:
        """What AI_CONTINUATION of the thing continued without asking says: 「自動継続」, with the rule's name when it has one."""
        rule = self.pick.rule if self.pick is not None else None
        return f"自動継続:{rule.text}" if rule is not None and not rule.default else "自動継続"


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


def _xy(origin: Sequence[float], p: Sequence[float]) -> tuple[float, float]:
    """A place in metres east and north of `origin` (near it: a plane is close enough)."""
    k = 111_320.0
    return ((p[0] - origin[0]) * k * math.cos(math.radians(origin[1])), (p[1] - origin[1]) * k)


def _area(ring: Sequence[Sequence[float]]) -> float:
    """Signed (anticlockwise +) area of a ring on the plane."""
    n = len(ring)
    return sum(ring[i][0] * ring[(i + 1) % n][1] - ring[(i + 1) % n][0] * ring[i][1] for i in range(n)) / 2


def _centre(ring: Sequence[Sequence[float]]) -> Optional[tuple[float, float]]:
    """The centre (重心) of a ring on the plane; None when it has no area."""
    a = _area(ring)
    if abs(a) < 1e-9:
        return None
    n = len(ring)
    cx = cy = 0.0
    for i in range(n):
        x1, y1 = ring[i]
        x2, y2 = ring[(i + 1) % n]
        f = x1 * y2 - x2 * y1
        cx += (x1 + x2) * f
        cy += (y1 + y2) * f
    return cx / (6 * a), cy / (6 * a)


def _inside(p: Sequence[float], ring: Sequence[Sequence[float]]) -> bool:
    """Whether a point is inside a ring (even-odd)."""
    x, y = p
    inside = False
    n = len(ring)
    for i in range(n):
        x1, y1 = ring[i]
        x2, y2 = ring[(i + 1) % n]
        if (y1 > y) != (y2 > y) and x < (x2 - x1) * (y - y1) / (y2 - y1) + x1:
            inside = not inside
    return inside


def _clip(subject: Sequence[Sequence[float]], convex: Sequence[Sequence[float]]) -> list:
    """The part of a ring inside a convex one (anticlockwise), Sutherland–Hodgman."""
    out = [tuple(p) for p in subject]
    n = len(convex)
    for i in range(n):
        if not out:
            break
        ax, ay = convex[i]
        bx, by = convex[(i + 1) % n]

        def left(p) -> float:
            return (bx - ax) * (p[1] - ay) - (by - ay) * (p[0] - ax)

        def cut(p, q) -> tuple[float, float]:
            lp, lq = left(p), left(q)
            t = lp / (lp - lq)
            return (p[0] + (q[0] - p[0]) * t, p[1] + (q[1] - p[1]) * t)

        given, out = out, []
        for j, q in enumerate(given):
            p = given[j - 1]
            if left(q) >= 0:
                if left(p) < 0:
                    out.append(cut(p, q))
                out.append(q)
            elif left(p) >= 0:
                out.append(cut(p, q))
    return out


def _overlap(ai: Sequence[Sequence[float]], outline: Sequence[Sequence[float]]) -> float:
    """How much two outlines on the plane overlap: their shared area over the area of both together (0–1)."""
    convex = list(outline) if _area(outline) > 0 else list(reversed(outline))
    a, b = abs(_area(ai)), abs(_area(convex))
    if a <= 0 or b <= 0:
        return 0.0
    shared = abs(_area(_clip(ai, convex))) if len(ai) >= 3 else 0.0
    union = a + b - shared
    return max(0.0, min(1.0, shared / union)) if union > 0 else 0.0


@dataclass
class _Shapes:
    """A found thing on the plane around it: its centre and its polygon (or outline)."""
    origin: tuple[float, float]
    centre: tuple[float, float]
    ring: Optional[list]


def _shapes(found: Found) -> _Shapes:
    origin = (found.lon, found.lat)
    ring = None
    if found.ring and len(found.ring) >= 3:
        ring = [_xy(origin, p) for p in found.ring]
        if len(ring) > 3 and ring[0] == ring[-1]:
            ring = ring[:-1]
        if abs(_area(ring)) < 1e-6:
            ring = None
    centre = (_centre(ring) if ring else None) or (0.0, 0.0)
    return _Shapes(origin, centre, ring)


def _holds(rule: Rule, found: Found, source: Observation, d: float, near_by_length: float, relation: str,
           gap: Optional[float], heading_gap: Optional[float], shapes: _Shapes, outline_of_source, iou) -> bool:
    """Whether all of a rule's conditions hold for a pair."""
    if relation not in rule.relations:
        return False
    if rule.min_score is not None and found.score is not None and found.score < rule.min_score:
        return False
    if rule.length_gap is not None and gap is not None and gap > rule.length_gap:
        return False
    if rule.heading_gap is not None and heading_gap is not None and heading_gap > rule.heading_gap:
        return False
    if rule.near is not None and rule.near > 0 and d > max(rule.near, near_by_length):
        return False
    if rule.center_in_record:
        outline = outline_of_source()
        if outline is None or not _inside(shapes.centre, outline):
            return False
    if rule.record_in_ai:
        if shapes.ring is None or not _inside(_xy(shapes.origin, (source.lon, source.lat)), shapes.ring):
            return False
    if rule.overlap is not None:
        value = iou()
        if value is None or value < rule.overlap:
            return False
    return rule.placed


def _option(found: Found, source: Observation, known: Optional[Sequence[Taxon]],
            shapes: Optional[_Shapes] = None) -> Optional[ContinueOption]:
    settings = found.settings
    shapes = shapes or _shapes(found)
    sizes = [x for x in (found.length, source.length) if x is not None and x > 0]
    size = max(sizes) if sizes else 0.0
    reach = max(settings.candidate_radius, size * 2) if settings.candidate_radius > 0 else 0.0
    d = get_distance((source.lon, source.lat), (found.lon, found.lat))
    relation = class_relation(found.kind, source.kind, known)
    gap = None
    if found.length is not None and found.length > 0 and source.length is not None and source.length > 0:
        gap = abs(found.length - source.length) / max(found.length, source.length)
    turn = _heading_gap(found.heading, source.heading) if found.heading is not None and source.heading is not None else None
    cache: dict[str, Any] = {}

    def outline():
        if "outline" not in cache:
            corners = outline_of(source.lon, source.lat, source.length, source.width, source.heading)
            cache["outline"] = [_xy(shapes.origin, p) for p in corners] if corners else None
        return cache["outline"]

    def iou():
        if "iou" not in cache:
            mine = outline()
            cache["iou"] = _overlap(shapes.ring, mine) if mine is not None and shapes.ring is not None else None
        return cache["iou"]

    # Far beyond any rule's reach (its near, or the thing's and the AI's size) is not looked at further.
    widest = max([size * 2, reach] + [max(r.near or 0.0, size / 2) for r in settings.rules])
    if d > widest + size:
        return None
    rule = next((r for r in settings.rules if _holds(r, found, source, d, size / 2, relation, gap, turn, shapes, outline, iou)), None)
    if rule is None and (d > reach or relation == "other"):
        return None
    if rule is None and settings.candidate_radius <= 0:
        return None
    said, weight = _CLASS.get(relation, ("別の大分類", 0.6))
    reasons = [f"{to_fixed(d / 1000, 1)} km" if d >= 1000 else f"{js_round(d)} m", said]
    score = d / max(reach, widest, 1.0) + weight
    if gap is not None:
        score += min(1.0, gap * 2)
        reasons.append(f"長さ {'同じ' if gap < 0.005 else f'±{js_round(gap * 100)}%'}")
    if turn is not None:
        score += turn / 180 * 0.3
        if turn <= 20:
            reasons.append(f"向き差 {js_round(turn)}°")
    if "iou" in cache and cache["iou"]:
        reasons.append(f"重なり {js_round(cache['iou'] * 100)}%")
    if rule is not None:
        # A pair a rule holds for goes before any it does not (one to one).
        score -= 1.0
    return ContinueOption(source, d, score, rule is not None, reasons, rule)


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
    reach = max([f.settings.reach for f in found] + [longest * 3, 1.0])
    lat_most = max(abs(f.lat) for f in found)
    size = reach / 111_320 / max(math.cos(math.radians(min(lat_most, 89.0))), 0.01)
    grid: dict[tuple[int, int], list[int]] = {}
    for j, s in enumerate(sources):
        grid.setdefault(_cells(s.lon, s.lat, size), []).append(j)
    pairs: list[tuple[float, int, int]] = []
    options: list[dict[int, ContinueOption]] = [{} for _ in found]
    for i, f in enumerate(found):
        if not f.settings.on:
            continue
        cx, cy = _cells(f.lon, f.lat, size)
        shapes = _shapes(f)
        for dx in (-1, 0, 1):
            for dy in (-1, 0, 1):
                for j in grid.get((cx + dx, cy + dy), ()):
                    o = _option(f, sources[j], known, shapes)
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
        sure = mine is not None and options[i][mine].strong
        if sure and found[i].settings.unique:
            sure = sum(1 for o in options[i].values() if o.strong) == 1 and strong_of_source.get(mine, 0) == 1
        out[i] = Continuation(AUTO if sure else CANDIDATE, listed)
    return out
