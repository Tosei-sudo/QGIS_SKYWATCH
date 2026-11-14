"""
衛星の使用時間 (tsudo, 2026-11-14: 「システムによっては、軌道の使用時間で
上限管理してるらしい」「上限は、衛星ごとで、準備なども入る」「この衛星を
いつからいつまで使ったって感じで記録するのはどうかな」「見積もりは、その
パス(半周)で最初に取るところから最後のところまでの差分秒数？」「準備秒は
足すだけでよい。定数はシステムのコンフィグに」「上限は1パスあたりでとりあ
えず実装して」「1日の上限も欲しいな」).

A satellite is used on a pass (half a revolution of one orbit direction, as
`shotsPerPass`: the same direction, less than half the period apart) from
the start of its first shot, less the system's setup seconds
(`requestExports[].useTime.setupSeconds`), to the end of its last shot: the
time between its shots counts too. Each shot keeps its own part of it
(TBL_REQUEST_SHOT.USE_START = its start less the setup, USE_END = its end),
so a pass is used from the earliest USE_START to the latest USE_END of its
shots.

Each satellite (all its modes together) of a system with `useTime.perPass`
is used no more seconds a pass, with `useTime.perDay` no more seconds a JST
day (a pass counts on the day its use starts). The day's fitting
(daily.allocate) does not try a placement that would stretch a pass or the
day past them (USE_PASS, USE_DAY: the request tries the next pass, the next
satellite, the next system), and for such a satellite tries first, within
the same tenth of cloud, the placement that stretches its passes least (a
shot inside a pass's use adds nothing). Taken by hand (or pinned) it goes
past them, with a warning (USE_OVER).

The shots of the days before and after the day that were handed out and
not missed nor turned down (read_other_uses) count on the passes the day
shares with them.

A satellite fitted by counting (no orbit, or a system with `orbit: false`)
has no passes: each of its rows is taken as a pass of its own, the setup and
its scenes along the track at GROUND_SPEED.
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from typing import Callable, Optional, Sequence

from ..plan.names import plain
from ..plan.satellites import SatelliteSpec, platform_key
from .model import JST
from .shots import Placement, Shot

#: Why a request is held back: every pass it could go on would be used past its satellite's seconds a pass.
USE_PASS = "使用時間の上限（1 パス）"
#: Why a request is held back: the satellites that could take it would be used past their seconds a day.
USE_DAY = "使用時間の上限（1 日）"
#: The start of the warning of a request taken by hand past a satellite's use time.
USE_OVER = "使用時間の上限を超える（"
#: How fast (km/s) a scene goes by under a satellite fitted by counting (no orbit): what one scene takes.
GROUND_SPEED = 7.0
#: What is left under this share of a limit is told as running low.
LOW_SHARE = 0.1
_EPS = 1e-6


def seconds_text(s: float) -> str:
    """「1,200」 (whole seconds)."""
    return f"{round(s):,}"


def day_of(ms: float) -> date:
    """The JST day of a moment (ms)."""
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc).astimezone(JST).date()


def clock_text(ms: Optional[float]) -> str:
    """A moment as JST 「10:02:11」."""
    if ms is None or not math.isfinite(ms):
        return ""
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc).astimezone(JST).strftime("%H:%M:%S")


# --- Uses and passes -------------------------------------------------------------------------

@dataclass(eq=False)
class Use:
    """One shot's use of its satellite (ms): from its start less the setup to its end."""
    start: float
    end: float
    ascending: bool
    #: The moment it is grouped into passes by (the shot's middle).
    time: float
    #: The shot of the day planned (shots.Shot); None for another day's.
    shot: object = None


@dataclass
class OtherUse:
    """A shot of another day handed out (TBL_REQUEST_SHOT): its satellite's name and its use."""
    satellite: str
    start: float
    end: float
    ascending: bool
    pass_id: str = ""

    def use(self) -> Use:
        return Use(self.start, self.end, self.ascending, (self.start + self.end) / 2)


def passes_of(uses: Sequence[Use], half: float) -> list[list[Use]]:
    """The uses of one satellite by pass: the same direction, less than `half` (ms, half the period) from the pass's first."""
    out = []
    for ascending in (True, False):
        group: list[Use] = []
        for u in sorted((u for u in uses if u.ascending == ascending), key=lambda u: u.time):
            if group and u.time - group[0].time >= half:
                out.append(group)
                group = []
            group.append(u)
        if group:
            out.append(group)
    return sorted(out, key=lambda g: min(u.start for u in g))


def seconds(uses: Sequence[Use]) -> float:
    """How long a pass uses its satellite (s): from its earliest start to its latest end."""
    return (max(u.end for u in uses) - min(u.start for u in uses)) / 1000 if uses else 0.0


def _latitude_argument(rec, minutes: float) -> Optional[float]:
    """The argument of latitude (radians from the ascending node, 0 … 2π) of an SGP4 record `minutes` after its epoch."""
    error, p, v = rec.sgp4_tsince(minutes)
    if error or not all(math.isfinite(x) for x in (*p, *v)):
        return None
    h = (p[1] * v[2] - p[2] * v[1], p[2] * v[0] - p[0] * v[2], p[0] * v[1] - p[1] * v[0])
    hn = math.sqrt(h[0] ** 2 + h[1] ** 2 + h[2] ** 2)
    # The ascending node: z × h.
    node = (-h[1], h[0], 0.0)
    nn = math.hypot(node[0], node[1])
    if hn <= 0 or nn <= 1e-9:
        return None
    cos_u = (node[0] * p[0] + node[1] * p[1]) / nn
    c = (node[1] * p[2] - node[2] * p[1], node[2] * p[0] - node[0] * p[2], node[0] * p[1] - node[1] * p[0])
    sin_u = (c[0] * h[0] + c[1] * h[1] + c[2] * h[2]) / (nn * hn)
    return math.atan2(sin_u, cos_u) % (2 * math.pi)


def pass_id(orbit, ms: float, ascending: bool) -> str:
    """
    「45123昇」: the revolution number of a pass (the TLE's, counted on from
    its epoch at each ascending node; an ascending pass by the node in its
    middle, a descending one by the descending node) and its direction; ""
    when the orbit does not tell.
    """
    rec = getattr(orbit, "satrec", None)
    epoch = getattr(orbit, "epoch", None)
    if rec is None or epoch is None or not ms or not math.isfinite(ms):
        return ""
    try:
        now = _latitude_argument(rec, (ms - epoch) / 60000)
        then = _latitude_argument(rec, 0.0)
        motion = float(rec.no_kozai)
        rev = int(rec.revnum)
    except (AttributeError, TypeError, ValueError, OverflowError):
        return ""
    if now is None or then is None or not motion > 0:
        return ""
    rev += round((then + motion * (ms - epoch) / 60000 - now) / (2 * math.pi))
    if ascending and now >= math.pi:
        # Before the ascending node: the pass is the next revolution's.
        rev += 1
    return f"{rev}{'昇' if ascending else '降'}"


# --- The day's book ------------------------------------------------------------------------------

class UseBook:
    """
    How long the satellites are used on the day being fitted
    (daily.allocate binds it to the day's shots), and the uses of the
    other days' shots on the same passes (`others`, read_other_uses).
    """

    def __init__(self, others: Sequence[OtherUse] = ()):
        self.others = list(others)
        self.book = None
        self.sats: list = []
        self.passes = None
        self.day: Optional[date] = None
        self.index: dict = {}
        #: platform → the use time rules of its system (the first system of its modes).
        self.rules: dict = {}
        #: platform → the seconds of each of its rows fitted by counting.
        self.counted: dict = {}
        self._others: dict = {}

    def bind(self, book, sats: Sequence[SatelliteSpec], systems: Sequence, passes, day: Optional[date]) -> None:
        """Starts a fitting: the day's shots (shots.Book; None when fitted by counting), satellites and systems."""
        from .route import system_of_satellite

        self.book, self.sats, self.passes, self.day = book, list(sats), passes, day
        self.index = {id(s): i for i, s in enumerate(self.sats)}
        self.counted = {}
        self.rules = {}
        names: dict = {}
        for s in self.sats:
            key = platform_key(s)
            system = system_of_satellite(s, systems)
            if system is not None:
                self.rules.setdefault(key, system.use_time)
            for n in (s.name, s.id, *s.aliases):
                if plain(n):
                    names.setdefault(plain(n), key)
        self._others = {}
        for o in self.others:
            key = names.get(plain(o.satellite))
            if key is not None:
                self._others.setdefault(key, []).append(o.use())

    # --- Uses ------------------------------------------------------------------------------------

    def _setup_ms(self, i: int) -> float:
        rule = self.rules.get(platform_key(self.sats[i]))
        return rule.setup * 1000 if rule is not None else 0.0

    def use_of(self, shot: Shot) -> Use:
        return Use(shot.start - self._setup_ms(shot.sat), shot.end, shot.ascending, shot.middle, shot)

    def half(self, i: int, shot: Optional[Shot] = None) -> float:
        """Half the satellite's period (ms): its orbit's, else a shot's."""
        orbit = self.passes.orbits.get(i) if self.passes is not None else None
        motion = getattr(getattr(orbit, "satrec", None), "no_kozai", None)
        if motion and motion > 0:
            return math.pi / motion * 60000
        if shot is not None:
            return math.pi / shot.rate * 1000
        return 45 * 60000.0

    def uses(self, i: int, leave: Optional[Shot] = None, add: Sequence[Use] = ()) -> list[Use]:
        """The uses of a satellite (all its modes): its shots of the day (but `leave`), `add`, and the other days'."""
        mine = [self.use_of(s) for s in self.book.of_platform(i) if s is not leave] if self.book is not None else []
        return mine + list(add) + list(self._others.get(platform_key(self.sats[i]), ()))

    def _day_seconds(self, uses: Sequence[Use], half: float) -> float:
        """The seconds of the passes whose use starts on the day."""
        return sum(seconds(g) for g in passes_of(uses, half) if self.day is None or day_of(min(u.start for u in g)) == self.day)

    def _new(self, p: Placement) -> list[Use]:
        if p.into is not None:
            s = p.into
            moved = Shot(s.sat, s.time, s.frame, s.radius, s.ascending, window=p.window, along=p.along)
            return [self.use_of(moved)]
        return [self.use_of(s) for s in p.shots]

    # --- The fitting ------------------------------------------------------------------------------

    def check(self, system, sat: SatelliteSpec, p: Placement) -> Optional[tuple[str, str]]:
        """
        (USE_PASS or USE_DAY, what goes past) when a placement would use its
        satellite past the system's seconds a pass or a day (and longer than
        without it); None when it fits, or nothing limits it.
        """
        rule = system.use_time
        i = self.index.get(id(sat))
        if not rule.limited or i is None or self.book is None or not (p.shots or p.into is not None):
            return None
        new = self._new(p)
        after = self.uses(i, leave=p.into, add=new)
        half = self.half(i, new[0].shot)
        if rule.per_pass is not None:
            for g in passes_of(after, half):
                if not any(u is n for u in g for n in new):
                    continue
                rest = [u for u in g if not any(u is n for n in new)]
                if p.into is not None:
                    rest.append(self.use_of(p.into))
                now, was = seconds(g), seconds(rest)
                if now > rule.per_pass + _EPS and now > was + _EPS:
                    return USE_PASS, f"1 パス {seconds_text(rule.per_pass)} 秒まで・このパスで {seconds_text(now)} 秒"
        if rule.per_day is not None:
            extra = sum(self.counted.get(platform_key(sat), ()))
            now = self._day_seconds(after, half) + extra
            was = self._day_seconds(self.uses(i), half) + extra
            if now > rule.per_day + _EPS and now > was + _EPS:
                return USE_DAY, f"1 日 {seconds_text(rule.per_day)} 秒まで・この日 {seconds_text(now)} 秒"
        return None

    def check_shots(self, system, sat: SatelliteSpec, shots: Sequence[Shot]) -> Optional[tuple[str, str]]:
        """check for new shots not planned yet (those of an area too big for one shot, as they are added)."""
        return self.check(system, sat, Placement(shots=list(shots))) if shots else None

    def growth(self, system, sat: SatelliteSpec, p: Placement) -> float:
        """How many seconds a placement adds to its satellite's passes (0 for one inside a pass's use); 0 when not limited."""
        i = self.index.get(id(sat))
        if not system.use_time.limited or i is None or self.book is None or not (p.shots or p.into is not None):
            return 0.0
        new = self._new(p)
        half = self.half(i, new[0].shot)
        after = sum(seconds(g) for g in passes_of(self.uses(i, leave=p.into, add=new), half))
        before = sum(seconds(g) for g in passes_of(self.uses(i), half))
        return max(0.0, after - before)

    def counted_seconds(self, system, sat: SatelliteSpec, scenes: int) -> float:
        """The use of a row fitted by counting (s): the setup and its scenes along the track at GROUND_SPEED."""
        return system.use_time.setup + max(0, scenes) * max(0.0, sat.length) / GROUND_SPEED

    def check_counted(self, system, sat: Optional[SatelliteSpec], scenes: int) -> Optional[tuple[str, str]]:
        """check for a row fitted by counting (a pass of its own)."""
        rule = system.use_time
        if not rule.limited or sat is None:
            return None
        now = self.counted_seconds(system, sat, scenes)
        if rule.per_pass is not None and now > rule.per_pass + _EPS:
            return USE_PASS, f"1 パス {seconds_text(rule.per_pass)} 秒まで・この行で {seconds_text(now)} 秒"
        if rule.per_day is not None:
            i = self.index.get(id(sat))
            day = sum(self.counted.get(platform_key(sat), ()))
            if self.book is not None and i is not None:
                day += self._day_seconds(self.uses(i), self.half(i))
            if day + now > rule.per_day + _EPS:
                return USE_DAY, f"1 日 {seconds_text(rule.per_day)} 秒まで・この日 {seconds_text(day + now)} 秒"
        return None

    def take_counted(self, system, sat: Optional[SatelliteSpec], scenes: int) -> None:
        if sat is not None and system.use_time.limited:
            self.counted.setdefault(platform_key(sat), []).append(self.counted_seconds(system, sat, scenes))

    def own(self) -> list[OtherUse]:
        """The uses of the day's shots (for the next day of a run, 上限シミュレーション)."""
        if self.book is None:
            return []
        out = []
        for s in self.book.shots:
            u = self.use_of(s)
            out.append(OtherUse(self.sats[s.sat].name, u.start, u.end, s.ascending))
        return out

    @staticmethod
    def warning(sat: SatelliteSpec, over: tuple[str, str]) -> str:
        """「使用時間の上限を超える（ICEYE-X27 1 パス 300 秒まで・このパスで 320 秒・手動で採用）」."""
        return f"{USE_OVER}{sat.name} {over[1]}・手動で採用）"

    # --- What the day uses -----------------------------------------------------------------------

    def pass_label(self, i: int, uses: Sequence[Use]) -> str:
        """The pass number of a pass of the day (pass_id of its first shot)."""
        first = min(uses, key=lambda u: u.time)
        orbit = self.passes.orbits.get(i) if self.passes is not None else None
        return pass_id(orbit, first.time, first.ascending)

    def pass_of(self, shot: Shot) -> str:
        """The number of a shot's pass (pass_id at its middle)."""
        orbit = self.passes.orbits.get(shot.sat) if self.passes is not None else None
        return pass_id(orbit, shot.middle, shot.ascending)

    def shot_text(self, shots: Sequence[Shot]) -> str:
        """A row's use (the calculation's tooltip): 「使用 10:02:01〜10:02:34（33 秒・パス 45123昇 で計 120 秒）」."""
        if not shots or self.book is None:
            return ""
        uses = [self.use_of(s) for s in shots]
        start, end = min(u.start for u in uses), max(u.end for u in uses)
        text = f"使用 {clock_text(start)}〜{clock_text(end)}（{seconds_text((end - start) / 1000)} 秒"
        i = shots[0].sat
        if 0 <= i < len(self.sats):
            for g in passes_of(self.uses(i), self.half(i, shots[0])):
                if any(u.shot is shots[0] for u in g):
                    label = self.pass_label(i, g)
                    text += f"・パス {label} で計 {seconds_text(seconds(g))} 秒" if label else f"・このパスで計 {seconds_text(seconds(g))} 秒"
        return text + "）"

    def texts(self) -> list[tuple[str, bool]]:
        """
        For each satellite with use limits used on the day: 「ICEYE-X27: 1 パス
        300 秒まで（いちばん長いパス 45123昇 280 秒）・1 日 1,200 秒のうち 760 秒
        （残り 440 秒）」, and whether it runs low (LOW_SHARE).
        """
        out = []
        seen: set = set()
        for i, sat in enumerate(self.sats):
            key = platform_key(sat)
            rule = self.rules.get(key)
            if key in seen or rule is None or not rule.limited:
                continue
            seen.add(key)
            half = self.half(i)
            groups = [g for g in passes_of(self.uses(i), half)
                      if any(u.shot is not None for u in g) and (self.day is None or day_of(min(u.start for u in g)) == self.day)]
            counted = self.counted.get(key, [])
            if not groups and not counted:
                continue
            parts, low = [], False
            if rule.per_pass is not None:
                longest = max(groups, key=seconds, default=None)
                most = max([seconds(g) for g in groups] + counted, default=0.0)
                label = self.pass_label(i, longest) if longest is not None and seconds(longest) >= max(counted, default=0.0) else ""
                where = f"いちばん長いパス {label} " if label else "いちばん長いパス "
                parts.append(f"1 パス {seconds_text(rule.per_pass)} 秒まで（{where}{seconds_text(most)} 秒）")
                low = low or most >= rule.per_pass * (1 - LOW_SHARE)
            if rule.per_day is not None:
                used = sum(seconds(g) for g in groups) + sum(counted)
                left = rule.per_day - used
                tail = f"残り {seconds_text(left)} 秒" if left >= 0 else f"{seconds_text(-left)} 秒超過"
                parts.append(f"1 日 {seconds_text(rule.per_day)} 秒のうち {seconds_text(used)} 秒（{tail}）")
                low = low or left <= rule.per_day * LOW_SHARE
            out.append((f"{sat.name}: " + "・".join(parts), low))
        return out


# --- Other days' shots -----------------------------------------------------------------------------

def read_other_uses(client, store, token: Optional[str], day: date,
                    cancelled: Optional[Callable[[], bool]] = None) -> list[OtherUse]:
    """
    The uses of the shots of the days before and after `day` (with
    USE_START and USE_END) handed out and not missed nor turned down (as the
    yearly quotas tell them, quota.shot_state): the passes the day shares
    with them count them. [] without a shot layer.
    """
    from ..esri import quote
    from .quota import PENDING, USED, shot_state
    from .store import load_history, load_shots_where, shot_days_where

    if not store.shots:
        return []
    a, b = (day - timedelta(days=1)).isoformat(), (day + timedelta(days=1)).isoformat()
    shots = [s for s in load_shots_where(client, store, token, shot_days_where(store, a, b), cancelled=cancelled)
             if s.day != day.isoformat() and s.use_start is not None and s.use_end is not None and s.use_end >= s.use_start]
    if not shots:
        return []
    rows: dict = {}
    if store.history:
        hf = store.history_fields
        where = (f"{hf['day']} >= {quote(a)} AND {hf['day']} <= {quote(b)} AND {hf['shotId']} IS NOT NULL"
                 f" AND {hf['shotId']} <> ''")
        wanted = {s.shot_id for s in shots}
        for r in load_history(client, store, token, where, cancelled=cancelled):
            if r.shot_id in wanted:
                rows.setdefault(r.shot_id, []).append(r)
    return [OtherUse(s.satellite, s.use_start, s.use_end, s.orbit_dir == "昇交", s.pass_id) for s in shots
            if shot_state(s, rows.get(s.shot_id, []), bool(store.history)) in (USED, PENDING)]
