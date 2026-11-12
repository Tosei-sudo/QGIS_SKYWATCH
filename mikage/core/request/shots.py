"""
Shots (撮像): what one satellite (mode) takes on one pass, worked out from
the passes (core/request/passes.py) while the day's requests are fitted
(daily.allocate), and the satellites' time they use.

A shot is one swath across the track (`swath`) and up to `scenesPerPass`
scenes one after another along it (`length` each). Requests close together
on a pass are put into one shot when they all fit in it (まとめ, tsudo
2026-11-11: 「連続シーンで取れば次への間隔をクリアできるときはマージして、
そのポリゴンで計画として」): the shot is handed to the system as one
request, with the outline of its scenes as its area, while each request
keeps its own history row (the schedule shows the requests as they were
asked).

The row of requests put together is written with the tightest box along
the track around their own areas (tsudo, 2026-11-11: 「ぴったしだと完全含有
判定されなくて 2 オーダーされる可能性」→「パス並行に」), not the outline of
the scenes; and they are put together only when that box leaves the
system's `mergeMargin` (500 m) inside the shot on every side, so that the
system, framing with its own orbit and scenes, still finds them in one.

A satellite (all its modes) keeps `minInterval` minutes between its shots,
takes at most `maxPerDay` shots a day and, when it has one, `shotsPerPass`
shots a pass (half a revolution of one orbit direction; 改修案 10, G13);
putting a request into a shot already planned uses none of them. An area too big for one shot takes the day's
passes one after another (as the imaging plan's 組み合わせ) until 95% of
it is taken, or the passes run out; it is not put together with others.
Its points already taken (`start`: 撮像済み, 撮像待ち, or the shots of
another satellite sharing it, daily.allocate) are left out: each shot has
to add MIN_GAIN of what is still open (tsudo, 2026-11-12: 「相互に分担すべき
でしょ？」). Such a shot is handed out on a row of its own with the part of
the area it takes (`part_ring`), a box along the track inside its scenes
by the system's `mergeMargin`.

Track angles are radians in the orbit's frame at the shot's moment: across
the track (left positive) and along it (forward positive).
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Callable, Optional, Sequence

from ..plan.access import Opportunity, best_scene, framed, look_at, reach_of
from ..plan.orbit import Frame, R, ecef_of, frame_of, lon_lat_of, norm, point_at, track_angles, track_rate, unit
from ..plan.satellites import SatelliteSpec, platform_key
from .covered import GOAL
from .model import ImagingRequest
from .passes import Passes

#: The least share of an area one more shot has to add.
MIN_GAIN = 0.02
_EPS = 1e-9


@dataclass(eq=False)
class Shot:
    """One satellite's shot on one pass, and the requests it takes."""
    sat: int
    #: The moment of its frame (ms): the first request's closest approach.
    time: float
    frame: Frame
    radius: float
    ascending: bool
    #: The candidates it takes (daily.Candidate), the first being the one whose row carries it.
    members: list = field(default_factory=list)
    #: Each member's reach (x0, x1, y0, y1).
    extents: list = field(default_factory=list)
    window: tuple = (0.0, 0.0)
    along: tuple = (0.0, 0.0)
    frames: int = 1
    #: For a shot of an area too big for one: the share of it taken; None for a shot taking its requests whole.
    gain: Optional[float] = None
    _ring: Optional[tuple] = field(default=None, repr=False)
    _area: Optional[tuple] = field(default=None, repr=False)

    @property
    def rate(self) -> float:
        return track_rate(self.radius)

    def moment(self, y: float) -> float:
        """The moment (ms) the satellite is over `y` along the track."""
        return self.time + y / self.rate * 1000

    @property
    def start(self) -> float:
        return self.moment(self.along[0])

    @property
    def end(self) -> float:
        return self.moment(self.along[1])

    @property
    def middle(self) -> float:
        """The moment of the middle of its scenes."""
        return self.moment((self.along[0] + self.along[1]) / 2)

    def member_time(self, c) -> float:
        """The moment a member is taken (over the middle of its reach)."""
        for m, (_, _, y0, y1) in zip(self.members, self.extents):
            if m.key == c.key:
                return self.moment((y0 + y1) / 2)
        return self.middle

    @property
    def look(self) -> dict:
        """offNadir, incidence (degrees) of the middle of its swath."""
        return look_at((self.window[0] + self.window[1]) / 2, self.radius)

    @property
    def side(self) -> str:
        return "左" if (self.window[0] + self.window[1]) / 2 > 0 else "右"

    @property
    def merged(self) -> bool:
        return len(self.members) > 1

    def _ring_of(self, window: tuple, along: tuple) -> list:
        """A box across and along the track as a ring ([longitude, latitude] …, closed), its long edges in pieces."""
        a, b = window
        y0, y1 = along
        pieces = max(4, 4 * self.frames)
        ring = []
        for x, p, q in ((a, y0, y1), (b, y1, y0)):
            ring.extend(list(lon_lat_of(point_at(self.frame, x, p + (q - p) * i / pieces))) for i in range(pieces + 1))
        ring.append(list(ring[0]))
        return ring

    def ring(self) -> list:
        """The outline of its scenes ([longitude, latitude] …, closed), each edge in pieces so it follows the track."""
        key = (self.window, self.along, self.frames)
        if self._ring is None or self._ring[0] != key:
            self._ring = (key, self._ring_of(self.window, self.along))
        return [list(p) for p in self._ring[1]]

    def box(self) -> tuple:
        """Across and along (x0, x1, y0, y1) of its requests' own areas (and the points they are planned as)."""
        return union([area_box(m.request, self.frame) for m in self.members] + list(self.extents))

    def area_ring(self) -> list:
        """
        The polygon its row is written with: for requests put together, the
        tightest box along the track around their own areas; otherwise the
        outline of its scenes.
        """
        if not self.merged:
            return self.ring()
        key = (self.window, self.along, self.frames, len(self.members))
        if self._area is None or self._area[0] != key:
            x0, x1, y0, y1 = self.box()
            self._area = (key, self._ring_of((x0, x1), (y0, y1)))
        return [list(p) for p in self._area[1]]

    def part_ring(self, margin: float = 0.0) -> list:
        """
        The polygon the row of one shot of an area too big for one is written
        with (分担, tsudo 2026-11-12: 「撮る部分だけをファイルに」): the part of
        the area's box along the track inside its scenes, `margin` (radians,
        the system's mergeMargin) left inside them on every side, so that the
        system finds it in one shot of its own; the scenes' outline when that
        leaves nothing.
        """
        box = area_box(self.members[0].request, self.frame) if self.members else None
        x0, x1 = self.window[0] + margin, self.window[1] - margin
        y0, y1 = self.along[0] + margin, self.along[1] - margin
        if box is not None:
            x0, x1, y0, y1 = max(x0, box[0]), min(x1, box[1]), max(y0, box[2]), min(y1, box[3])
        if x1 - x0 <= _EPS or y1 - y0 <= _EPS:
            return self.ring()
        return self._ring_of((x0, x1), (y0, y1))


@dataclass
class Placement:
    """Where a request could go with one satellite: into a shot already planned, or new shots."""
    #: The shot it joins (None for new shots).
    into: Optional[Shot] = None
    #: The shots made for it (new).
    shots: list = field(default_factory=list)
    #: Its reach in the shot it joins, and the shot's window, along and frames once it is in.
    extent: tuple = ()
    window: tuple = ()
    along: tuple = ()
    frames: int = 0
    #: Scenes it adds to the system's day.
    scenes: int = 0
    #: For an area: the share of it taken (None: whole), with what was taken before (`start`).
    coverage: Optional[float] = None
    #: For an area: the share its own shots add.
    gain: Optional[float] = None
    #: For an area: its points taken once it is in (those taken before too), for the next satellite sharing it.
    cells: Optional[bytearray] = None
    #: Whether a satellite's interval or day is overstepped (only when forced).
    busy: bool = False

    @property
    def new(self) -> bool:
        return self.into is None


def area_box(req: ImagingRequest, frame: Frame) -> Optional[tuple]:
    """Across and along (x0, x1, y0, y1) of a request's own area (its rings' vertices) in a shot's frame; None without."""
    angles = [track_angles(unit(ecef_of(p[:2])), frame) for r in req.rings for p in r]
    if not angles:
        return None
    xs, ys = [a[0] for a in angles], [a[1] for a in angles]
    return min(xs), max(xs), min(ys), max(ys)


def union(boxes: Sequence[Optional[tuple]]) -> tuple:
    """The box (x0, x1, y0, y1) around boxes (those None left out)."""
    boxes = [b for b in boxes if b is not None]
    return min(b[0] for b in boxes), max(b[1] for b in boxes), min(b[2] for b in boxes), max(b[3] for b in boxes)


def _side_ok(side: str, mid: float) -> bool:
    return side in ("both", "", None) or (side == "left") == (mid > 0)


def _conditions_ok(req: ImagingRequest, look: dict, mid: float, ascending: bool) -> bool:
    """Whether looking at `mid` across the track suits a request's own angles, side and direction."""
    if req.off_nadir_min is not None and look["offNadir"] < req.off_nadir_min - 1e-6:
        return False
    if req.off_nadir_max is not None and look["offNadir"] > req.off_nadir_max + 1e-6:
        return False
    if req.incidence_min is not None and look["incidence"] < req.incidence_min - 1e-6:
        return False
    if req.incidence_max is not None and look["incidence"] > req.incidence_max + 1e-6:
        return False
    if req.look_side == "右" and mid > 0 or req.look_side == "左" and mid < 0:
        return False
    if req.orbit_dir == "昇交" and not ascending or req.orbit_dir == "降交" and ascending:
        return False
    return True


class Book:
    """The shots of a day, by satellite, as the requests are fitted."""

    def __init__(self, sats: Sequence[SatelliteSpec], passes: Passes):
        self.sats = list(sats)
        self.passes = passes
        self.shots: list[Shot] = []
        self._frames: dict = {}
        #: Whether the last new_shots left out a pass the satellite had no time for.
        self.saw_busy = False

    def known(self, sat: int) -> bool:
        return sat in self.passes.known

    def _frame(self, sat: int, ms: float):
        key = (sat, ms)
        if key not in self._frames:
            st = self.passes.orbits[sat].state(ms)
            self._frames[key] = None if st is None else (frame_of(st.r, st.v), norm(st.r), st.north)
        return self._frames[key]

    def _extent(self, req: ImagingRequest, frame: Frame) -> Optional[tuple]:
        points = self.passes.points.get(req.id)
        if not points:
            return None
        angles = [track_angles(p, frame) for p in points]
        xs, ys = [a[0] for a in angles], [a[1] for a in angles]
        return min(xs), max(xs), min(ys), max(ys)

    # --- The satellites' time --------------------------------------------------------------------

    def of_platform(self, sat: int) -> list[Shot]:
        key = platform_key(self.sats[sat])
        return [s for s in self.shots if platform_key(self.sats[s.sat]) == key]

    def busy(self, sat: int, start: float, end: float, leave: Optional[Shot] = None) -> bool:
        """Whether a shot from `start` to `end` oversteps its satellite's interval or day (`leave`: the shot it is)."""
        spec = self.sats[sat]
        others = [s for s in self.of_platform(sat) if s is not leave]
        gap = spec.minInterval * 60000
        if gap > 0 and any(s.start - gap < end and start < s.end + gap for s in others):
            return True
        return bool(spec.maxPerDay) and leave is None and len(others) + 1 > spec.maxPerDay

    def pass_full(self, sat: int, shot: Shot, more: Sequence[Shot] = ()) -> bool:
        """
        Whether a new shot would go past its satellite's `shotsPerPass` (1 パス
        の撮像回数; 改修案 10, G13): the satellite's shots (all its modes, and
        `more` not yet planned) on the same pass, half a revolution of the same
        orbit direction (the same direction, less than half the period apart).
        """
        most = self.sats[sat].shotsPerPass
        if not most:
            return False
        half = math.pi / shot.rate * 1000
        on = [s for s in (*self.of_platform(sat), *more) if s.ascending == shot.ascending and abs(s.time - shot.time) < half]
        return len(on) + 1 > most

    # --- Where a request can go ------------------------------------------------------------------

    def _fit(self, sat: int, reqs: Sequence[ImagingRequest], extents: Sequence[tuple], radius: float, ascending: bool,
             margin: float = 0.0):
        """
        The window, along and frames of one shot taking every extent, `margin`
        (radians) left inside it on every side; None when they do not fit in one.
        """
        spec = self.sats[sat]
        sw, sl = spec.swath / R, spec.length / R
        most = max(1, spec.scenesPerPass)
        x0, x1, y0, y1 = union(extents)
        y0, y1 = y0 - margin, y1 + margin
        if x1 - x0 + 2 * margin > sw + _EPS or y1 - y0 > most * sl + _EPS:
            return None
        mid = (x0 + x1) / 2
        near, far = reach_of(spec, spec.maxOffNadir, radius, None, None)
        if not near - _EPS <= abs(mid) <= far + _EPS or not _side_ok(spec.lookSide, mid):
            return None
        look = look_at(mid, radius)
        if any(not _conditions_ok(r, look, mid, ascending) for r in reqs):
            return None
        along = framed(y0, y1, sl, most)
        frames = max(1, math.floor((along[1] - along[0]) / sl + 0.5))
        return (mid - sw / 2, mid + sw / 2), along, frames

    def joins(self, c, sat: int, ops: Sequence[Opportunity], merge: bool, margin_m: float = 0.0) -> list[Placement]:
        """
        Shots of this satellite (mode) already planned that the candidate fits
        into, fewest scenes added first: with every request's own area and
        `margin_m` metres left inside the shot on every side.
        """
        if not merge:
            return []
        spec = self.sats[sat]
        span = max(1, spec.scenesPerPass) * spec.length / R
        margin = max(0.0, margin_m) / 1000 / R
        out = []
        for shot in self.shots:
            if shot.sat != sat or shot.gain is not None or not any(abs(o.time - shot.time) <= span / shot.rate * 1000 + 60000 for o in ops):
                continue
            if any(m.request.id == c.request.id for m in shot.members):
                # Another time of the same request (1 日の回数): its own pass.
                continue
            extent = self._extent(c.request, shot.frame)
            if extent is None:
                continue
            reqs = [m.request for m in shot.members] + [c.request]
            areas = [area_box(r, shot.frame) for r in reqs]
            fit = self._fit(sat, reqs, shot.extents + [extent] + [a for a in areas if a is not None], shot.radius, shot.ascending, margin)
            if fit is None:
                continue
            window, along, frames = fit
            moved = Shot(sat, shot.time, shot.frame, shot.radius, shot.ascending, window=window, along=along)
            out.append(Placement(into=shot, extent=extent, window=window, along=along, frames=frames, scenes=frames - shot.frames,
                                 busy=self.busy(sat, moved.start, moved.end, leave=shot)))
        out.sort(key=lambda p: (p.busy, p.scenes, p.into.time))
        return out

    def new_shots(self, c, sat: int, ops: Sequence[Opportunity], forced: bool = False, start: Optional[bytearray] = None,
                  fits: Optional[Callable[[int, int], bool]] = None) -> list[Placement]:
        """
        New shots for the candidate on this satellite (mode)'s passes: one per pass for a request that fits in one
        (the smallest off-nadir angle first), or the passes one after another for an area too big for one.
        Passes the satellite has no time for are left out, unless `forced` (then they come last, marked busy).
        With `start` (the area's points already taken, `Passes.targets` cells), only the latter, on what is
        still open; `fits(shots, scenes)` says whether an area's shots still fit the system's day.
        """
        out = []
        big = []
        self.saw_busy = False
        if start is not None:
            area = self._area(c, sat, ops, forced, start, fits)
            return [area] if area is not None else []
        for op in sorted(ops, key=lambda o: (o.offNadir, o.time)):
            got = self._frame(sat, op.time)
            if got is None:
                continue
            frame, radius, ascending = got
            extent = self._extent(c.request, frame)
            fit = self._fit(sat, [c.request], [extent], radius, ascending) if extent is not None else None
            if fit is None:
                big.append(op)
                continue
            window, along, frames = fit
            shot = Shot(sat, op.time, frame, radius, ascending, [c], [extent], window, along, frames)
            busy = self.busy(sat, shot.start, shot.end) or self.pass_full(sat, shot)
            if busy and not forced:
                self.saw_busy = True
                continue
            out.append(Placement(shots=[shot], scenes=frames, busy=busy))
        if not out and big:
            area = self._area(c, sat, big, forced, None, fits)
            if area is not None:
                out.append(area)
        out.sort(key=lambda p: p.busy)
        return out

    def _area(self, c, sat: int, ops: Sequence[Opportunity], forced: bool, start: Optional[bytearray] = None,
              fits: Optional[Callable[[int, int], bool]] = None) -> Optional[Placement]:
        """
        An area too big for one shot: the passes in time order while each adds
        enough of what is still open (`start`: the points taken before), until
        GOAL of it is taken (and while `fits` the system's day).
        """
        spec = self.sats[sat]
        sw, sl, most = spec.swath / R, spec.length / R, max(1, spec.scenesPerPass)
        cells = bytearray(start) if start is not None else None
        before = sum(cells) if cells is not None else 0
        taken: list[Shot] = []
        busy_any = False
        share = 0.0
        if cells is not None and cells and before / len(cells) >= GOAL:
            return None
        for op in sorted(ops, key=lambda o: o.time):
            if op.cells is None or op.cellsAlong is None:
                continue
            across, along = op.cells.tolist(), op.cellsAlong.tolist()
            if cells is None or len(cells) != len(across):
                cells = bytearray(len(across))
                before = 0
            best = best_scene(across, along, sw, sl, most, lambda i: not cells[i])
            if best is None or best.count / len(across) < MIN_GAIN:
                continue
            got = self._frame(sat, op.time)
            if got is None:
                continue
            frame, radius, ascending = got
            mid = (best.window[0] + best.window[1]) / 2
            if not _conditions_ok(c.request, look_at(mid, radius), mid, ascending):
                continue
            shot = Shot(sat, op.time, frame, radius, ascending, [c], [(best.window[0], best.window[1], best.along[0], best.along[1])],
                        best.window, best.along, max(1, math.floor((best.along[1] - best.along[0]) / sl + 0.5)))
            gap = spec.minInterval * 60000
            busy = (self.busy(sat, shot.start, shot.end) or self.pass_full(sat, shot, taken)
                    or any(t.start - gap < shot.end and shot.start < t.end + gap for t in taken)
                    or bool(spec.maxPerDay) and len(self.of_platform(sat)) + len(taken) + 1 > spec.maxPerDay)
            if busy and not forced:
                self.saw_busy = True
                continue
            if fits is not None and not forced and not fits(len(taken) + 1, sum(s.frames for s in taken) + shot.frames):
                continue
            busy_any = busy_any or busy
            for k in best.members:
                cells[k] = 1
            share = sum(cells) / len(cells)
            shot.gain = best.count / len(across)
            taken.append(shot)
            if share >= GOAL:
                break
        if not taken:
            return None
        return Placement(shots=taken, scenes=sum(s.frames for s in taken), coverage=share,
                         gain=(sum(cells) - before) / len(cells), cells=cells, busy=busy_any)

    def cells_of(self, req: ImagingRequest, outlines: Sequence) -> Optional[bytearray]:
        """The points of an area too big for one shot (`Passes.targets` cells) inside outlines; None for one planned whole."""
        from .covered import mask_of

        target = self.passes.targets.get(req.id)
        if target is None or not target.cells:
            return None
        return mask_of(target.cells, outlines)

    # --- Taking a placement ----------------------------------------------------------------------

    def take(self, c, p: Placement) -> list[Shot]:
        """Puts a candidate where a placement says; the shots it is in."""
        if p.into is not None:
            shot = p.into
            shot.members.append(c)
            shot.extents.append(p.extent)
            shot.window, shot.along, shot.frames = p.window, p.along, p.frames
            return [shot]
        self.shots.extend(p.shots)
        return list(p.shots)
