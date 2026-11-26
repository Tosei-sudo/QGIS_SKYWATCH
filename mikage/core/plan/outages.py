"""
衛星の障害情報 (tsudo, 2026-11-16: 「衛星の障害情報を登録できるようにしてほしい
理由や、発生時刻、復旧見込み、影響などで、影響は選択式とかで、昇降や、オフナディア
制限とかかな？」「昇交の左のみしか取れないとかは指定できるの？」; design doc
「衛星の障害情報の登録 方針案」, all its recommendations chosen).

An outage is one row of an Esri table (`satelliteOutages`,
TBL_SATELLITE_OUTAGE; data that changes stays out of the settings file): the
satellite it hits (its name, NORAD id, another name or its series: then every
satellite of it), optionally one sensor and one mode, the reason, when it
started (a later moment for a planned stop), when it is expected to be over
(none: not known) and when it was (復旧日時). Its impacts are columns, an empty
one none:

* 撮像停止: no picture at all;
* 使えない方向: each of 昇交・右, 昇交・左, 降交・右, 降交・左 (the orbit
  direction and the side looked to) ticked on its own, so that 「昇交の左のみ」
  (the three others ticked) and 「昇交は左のみ、降交は自由」 (昇交・右 only)
  are both one outage;
* オフナディア角の上限 (degrees), 1 日の撮像回数の上限, 1 パスのシーン数の上限,
  撮像間隔の下限 (minutes): the satellite's own values, made stricter;
* 夜間は撮像できない: the sun below the horizon at the place (as a request's 夜);
* その他の影響 (text): not used by the calculation, told as a warning.

システムの障害 (tsudo, 2026-11-24: 「4,5,7を実装して」, design doc 「画像の評価・
システムの障害・参考情報 設計方針案」, all its recommendations chosen): a row may
name a system of `requestExports` (SYSTEM_NAME) instead of a satellite: its
impacts then hit every satellite of the system, 撮像停止 being called 受付停止
(the system takes no order for a picture in the period: the period is the
pictures' moment, as a satellite's), and two more of the system's own: the most
requests (件数) and scenes (枚数) a day handed to it, made stricter than its
`limit` on a day the row touches (never looser: the contract's values stay in
the settings file). Which system a satellite belongs to is the caller's
(`attach(…, system_of=…)`, core/request's route.system_of_satellite), else the
satellite catalog's own `system`.

When it holds: from its start to its recovery (復旧日時); without one, to the
expected end; without that either, on and on. An expected end gone by without
a recovery keeps it on (「見込み超過」, said in the lists) so that nothing is
ordered on a satellite that may still be broken (decision 2, A).

The passes (撮像機会) are judged at their moment (`blocking`); the values of a
day (回数・シーン数・間隔・角度の上限) hold all day on a day the outage touches
at all (`on_day`). Rows are never deleted on recovery: they are the history.
"""
from __future__ import annotations

import dataclasses
import math
import uuid
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from typing import Any, Callable, Iterable, Optional, Sequence

from ..esri import EsriClient, EsriError, fit_value
from ..util import is_number, js_string, ms_of
from .names import plain

#: The plugin's days are Japan's (as the requests', core/request/model.JST).
JST = timezone(timedelta(hours=9))
#: The attributes by role.
DEFAULT_OUTAGE_FIELDS = {
    "id": "OUTAGE_ID", "satellite": "SAT_NAME", "sensor": "SENSOR", "mode": "SENSOR_MODE", "reason": "OUTAGE_REASON",
    "start": "START_TIME", "expected": "EXPECTED_END", "recovered": "RECOVERED_AT",
    "stop": "STOP_IMAGING", "noAscRight": "NO_ASC_RIGHT", "noAscLeft": "NO_ASC_LEFT", "noDescRight": "NO_DESC_RIGHT",
    "noDescLeft": "NO_DESC_LEFT", "maxOffNadir": "LIMIT_OFF_NADIR", "maxPerDay": "LIMIT_PER_DAY",
    "scenesPerPass": "LIMIT_SCENES_PER_PASS", "minInterval": "LIMIT_INTERVAL_MIN", "noNight": "NO_NIGHT",
    "other": "OTHER_IMPACT", "note": "NOTE",
    "system": "SYSTEM_NAME", "ordersPerDay": "LIMIT_ORDERS_PER_DAY", "scenesPerDay": "LIMIT_SCENES_PER_DAY",
}
#: Roles without which the table is not used.
NEEDED = ("id", "satellite", "start")
#: The four directions (orbit direction × side looked to): (role, ascending, side, label).
DIRECTIONS = (
    ("noAscRight", True, "right", "昇交・右"),
    ("noAscLeft", True, "left", "昇交・左"),
    ("noDescRight", False, "right", "降交・右"),
    ("noDescLeft", False, "left", "降交・左"),
)
#: What an outage is said to be in, at a moment.
PLANNED, ONGOING, OVERDUE, RECOVERED = "予定", "発生中", "見込み超過", "復旧済み"
STATUSES = (ONGOING, OVERDUE, PLANNED, RECOVERED)
#: Why a request is held back: every satellite that could take it is hit by an outage then.
OUTAGE = "衛星の障害"
#: The same when every outage that held it back names a system: stopped (受付停止), else another of its impacts.
SYSTEM_STOP, SYSTEM_OUTAGE = "システムの受付停止", "システムの障害"
#: The beginnings of the reasons of a request held back by outages (as the limit simulation counts them).
OUTAGE_REASONS = (OUTAGE, SYSTEM_STOP, SYSTEM_OUTAGE)
#: The start of the warning of a request taken on a satellite with an outage on the day (of its system's only: the second).
OUTAGE_WARNING = "衛星の障害あり（"
SYSTEM_WARNING = "システムの障害あり（"
#: More rows than this is an error (never cut silently).
LIMIT = 10_000


def _text(v: Any) -> str:
    return "" if v is None else js_string(v).strip()


def _yes(v: Any) -> bool:
    """A ticked box of the table: 1 (any number but 0), true, or text like はい / あり / 1."""
    if isinstance(v, bool):
        return v
    if is_number(v):
        return v != 0
    return _text(v).lower() in ("1", "true", "yes", "y", "はい", "あり", "○", "◯", "不可")


def _positive(v: Any) -> Optional[float]:
    """A limit of the table: a number above 0 (text of one too); None when empty or not one."""
    if isinstance(v, bool) or v is None or v == "":
        return None
    try:
        n = float(v.strip()) if isinstance(v, str) else float(v)
    except (TypeError, ValueError):
        return None
    return n if math.isfinite(n) and n > 0 else None


def when_text(ms: Optional[float], empty: str = "未定") -> str:
    """「11/20 18:00」 (JST; with the year when not this year's)."""
    if ms is None or not math.isfinite(ms):
        return empty
    d = datetime.fromtimestamp(ms / 1000, tz=JST)
    # (No %-m: Windows' strftime does not know it.)
    text = f"{d.month}/{d.day} {d:%H:%M}"
    return text if d.year == datetime.now(JST).year else f"{d.year}/{text}"


@dataclass(frozen=True)
class Outage:
    """One satellite (or system) outage (a row of TBL_SATELLITE_OUTAGE). Moments in ms."""
    id: str
    #: The satellite's name, NORAD id, another name or series (then every satellite of it); "" when it names a system.
    satellite: str
    start: float
    #: One sensor / mode only ("" for all of them).
    sensor: str = ""
    mode: str = ""
    reason: str = ""
    #: When it is expected to be over (None: not known), and when it was (None: not yet).
    expected: Optional[float] = None
    recovered: Optional[float] = None
    #: 撮像停止.
    stop: bool = False
    #: 使えない方向: (ascending, side) of the directions ticked.
    closed: frozenset = frozenset()
    #: The stricter values while it holds (None: the satellite's own).
    max_off_nadir: Optional[float] = None
    max_per_day: Optional[int] = None
    scenes_per_pass: Optional[int] = None
    #: Minutes.
    min_interval: Optional[float] = None
    #: 夜間は撮像できない.
    no_night: bool = False
    #: その他の影響 (not used by the calculation).
    other: str = ""
    note: str = ""
    #: The system (`requestExports[].system`) it names instead of a satellite ("" for a satellite's).
    system: str = ""
    #: A system's stricter daily limits while it holds: requests (件数) and scenes (枚数) handed to it a day.
    orders_per_day: Optional[int] = None
    scenes_per_day: Optional[int] = None
    object_id: Optional[int] = field(default=None, compare=False)
    updated_at: Optional[float] = field(default=None, compare=False)

    @property
    def for_system(self) -> bool:
        """Whether it names a system (then never a satellite)."""
        return bool(self.system) and not self.satellite

    @property
    def target(self) -> str:
        """「ICEYE-X27」, 「WV110（パンクロ）」, 「SAR-Y（スポットライト）」, 「システム SYS-A」."""
        if self.for_system:
            return f"システム {self.system}"
        part = " ".join(x for x in (self.sensor, self.mode) if x)
        return f"{self.satellite}（{part}）" if part else self.satellite

    def end(self, now: float) -> float:
        """When it stops holding: its recovery, else its expected end still to come, else never (見込み超過 too)."""
        if self.recovered is not None:
            return self.recovered
        if self.expected is not None and self.expected > now:
            return self.expected
        return math.inf

    def status(self, now: float) -> str:
        if self.recovered is not None and self.recovered <= now:
            return RECOVERED
        if self.start > now:
            return PLANNED
        if self.recovered is None and self.expected is not None and self.expected <= now:
            return OVERDUE
        return ONGOING

    def open(self, now: float) -> bool:
        """Not recovered yet (planned, ongoing or past its expected end)."""
        return self.status(now) != RECOVERED

    def at(self, ms: float, now: float) -> bool:
        """Whether it holds at a moment."""
        return self.start <= ms < self.end(now)

    def during(self, first: float, last: float, now: float) -> bool:
        """Whether it holds at some moment of [first, last]."""
        return self.start <= last and self.end(now) > first

    @property
    def limits(self) -> bool:
        """Whether the calculation is changed by it (not only その他の影響)."""
        return bool(self.stop or self.closed or self.no_night or self.max_off_nadir is not None or self.max_per_day is not None
                    or self.scenes_per_pass is not None or self.min_interval is not None
                    or self.orders_per_day is not None or self.scenes_per_day is not None)

    @property
    def stop_text(self) -> str:
        """What its 撮像停止 is called: 受付停止 for a system's."""
        return "受付停止" if self.for_system else "撮像停止"

    @property
    def needs_pass(self) -> bool:
        """Whether it judges each pass (direction, angle, night), which satellites fitted by counting cannot be."""
        return bool(self.closed or self.no_night or self.max_off_nadir is not None)

    def impacts(self) -> list[str]:
        """「撮像停止」, 「使えない方向: 昇交・右」, 「オフナディア角 20° まで」 … in the order of the table."""
        out = []
        if self.stop:
            out.append(self.stop_text)
        if self.orders_per_day is not None:
            out.append(f"1 日 {self.orders_per_day} 件まで")
        if self.scenes_per_day is not None:
            out.append(f"1 日 {self.scenes_per_day} 枚まで")
        if self.closed:
            out.append("使えない方向: " + "・".join(label.replace("・", "") for _r, a, s, label in DIRECTIONS if (a, s) in self.closed))
        if self.max_off_nadir is not None:
            out.append(f"オフナディア角 {_num(self.max_off_nadir)}° まで")
        if self.max_per_day is not None:
            out.append(f"1 日 {self.max_per_day} 回まで")
        if self.scenes_per_pass is not None:
            out.append(f"1 パス {self.scenes_per_pass} シーンまで")
        if self.min_interval is not None:
            out.append(f"撮像間隔 {_num(self.min_interval)} 分以上")
        if self.no_night:
            out.append("夜間は撮像できない")
        if self.other:
            out.append(self.other)
        return out

    def impact_text(self) -> str:
        return "、".join(self.impacts()) or "影響の指定なし"

    def short_text(self, now: float, bare: bool = False) -> str:
        """
        「ICEYE-X27: 姿勢制御の不具合、撮像停止、復旧見込み 11/20 18:00」 (the reason and warnings of the
        calculation); `bare`: a system's by its name alone (under a heading that says システム).
        """
        parts = [p for p in (self.reason, self.impact_text()) if p]
        status = self.status(now)
        if status == RECOVERED:
            parts.append(f"{when_text(self.recovered)} 復旧")
        elif status == OVERDUE:
            parts.append(f"復旧見込み {when_text(self.expected)} を超過")
        else:
            if status == PLANNED:
                parts.append(f"{when_text(self.start)} から")
            parts.append(f"復旧見込み {when_text(self.expected)}")
        return f"{self.system if bare and self.for_system else self.target}: " + "、".join(parts)

    def blocks(self, ascending: bool, side: str, off_nadir: Optional[float], sun: Optional[float]) -> Optional[str]:
        """What of it keeps a pass from being used (撮像停止, the direction …); None when the pass may be used."""
        if self.stop:
            return self.stop_text
        if (bool(ascending), side) in self.closed:
            return "使えない方向"
        if self.max_off_nadir is not None and off_nadir is not None and off_nadir > self.max_off_nadir + 1e-9:
            return "オフナディア角の上限"
        if self.no_night and sun is not None and sun < 0:
            return "夜間は撮像できない"
        return None


def _num(v: float) -> str:
    return str(int(v)) if float(v).is_integer() else f"{v:g}"


def matches_names(o: Outage, satellite: Any, sensor: Any = "", mode: Any = "", *others: Any) -> bool:
    """
    Whether an outage hits pictures of a satellite (`others`: its id, other
    names, series): its satellite one of those names, its sensor (when it names
    one) the sensor or the satellite, its mode (when it names one) the mode.
    Names compare as plain letters and digits (as the yearly quotas do).
    """
    names = {plain(x) for x in (satellite, *others)} - {""}
    if plain(o.satellite) not in names:
        return False
    if o.sensor and plain(o.sensor) not in ({plain(sensor), plain(satellite)} - {""}):
        return False
    return not o.mode or plain(o.mode) == plain(mode)


def matches(o: Outage, sat, system: Optional[str] = None) -> bool:
    """
    Whether an outage hits a satellite (one mode of it, SatelliteSpec); a
    system's outage, when the satellite is of that system (`system`, else the
    satellite catalog's own `system`).
    """
    if o.for_system:
        own = system if system is not None else (getattr(sat, "system", None) or "")
        return bool(own) and plain(own) == plain(o.system)
    return matches_names(o, sat.name, sat.sensor or "", sat.mode or "", sat.id, sat.series, *(sat.aliases or ()))


def attach(sats: Sequence, outages: Sequence[Outage], system_of: Optional[Callable[[Any], Optional[str]]] = None) -> list:
    """
    The satellites, each with the outages that hit it (`SatelliteSpec.outages`);
    those unchanged as they are. `system_of`: the system a satellite is ordered
    through (by the settings' `requestExports`; without it, the catalog's own).
    """
    out = []
    for sat in sats:
        system = (system_of(sat) or "") if system_of is not None else None
        own = tuple(o for o in outages if matches(o, sat, system))
        out.append(sat if own == tuple(getattr(sat, "outages", ()) or ()) else dataclasses.replace(sat, outages=own))
    return out


def blocking(sat, ms: float, ascending: bool, side: str, off_nadir: Optional[float], sun: Optional[float],
             now: float) -> Optional[tuple[Outage, str]]:
    """The outage of a satellite that keeps a pass at `ms` from being used, and what of it; None when none."""
    for o in getattr(sat, "outages", ()) or ():
        if o.at(ms, now):
            why = o.blocks(ascending, side, off_nadir, sun)
            if why:
                return o, why
    return None


def leave_out(result, sats: Sequence, now: float) -> None:
    """
    Leaves the passes the satellites' outages hit out of a plan's result
    (access.PlanResult, in place: its `outaged` counts them), as the day's
    calculation does (passes.find_passes).
    """
    kept = []
    for op in result.opportunities:
        if blocking(sats[op.satellite], op.time, op.ascending, op.side, op.offNadir, op.sunElevation, now) is None:
            kept.append(op)
    result.outaged += len(result.opportunities) - len(kept)
    result.opportunities = kept


def handed_hit(outages: Sequence[Outage], satellite: str, sensor: str, mode: str, others: Sequence[str], at: Optional[float],
               orbit_dir: str = "", look_side: str = "", off_nadir: Optional[float] = None, center: Optional[Sequence[float]] = None,
               now: Optional[float] = None, system: str = "") -> Optional[tuple[Outage, str]]:
    """
    For a shot already handed out (撮像予定; decision 4: told, never changed):
    the outage that hits it at its moment, and what of it. Its direction
    (昇交 / 降交, 右 / 左), angle and place (`center`, for the night) when
    known; without them only 撮像停止 is sure, an outage judging passes is
    told as such. A system's outage hits the rows handed to that system
    (`system`).
    """
    if at is None or not math.isfinite(at):
        return None
    now = now if now is not None else datetime.now(timezone.utc).timestamp() * 1000
    for o in outages:
        if not o.at(at, now):
            continue
        if o.for_system:
            if not system or plain(system) != plain(o.system):
                continue
        elif not matches_names(o, satellite, sensor, mode, *others):
            continue
        if orbit_dir not in ("昇交", "降交") or look_side not in ("右", "左"):
            if o.stop:
                return o, o.stop_text
            if o.needs_pass:
                return o, "障害中（撮り方が分からず確かめられない）"
            continue
        sun = None
        if o.no_night and center is not None:
            from .orbit import sun_elevation

            sun = sun_elevation(center, at)
        why = o.blocks(orbit_dir == "昇交", "right" if look_side == "右" else "left", off_nadir, sun)
        if why:
            return o, why
    return None


def day_range(day: date) -> tuple[float, float]:
    """ms of a JST day's first and last moment."""
    start = datetime(day.year, day.month, day.day, tzinfo=JST)
    return start.timestamp() * 1000, (start + timedelta(days=1)).timestamp() * 1000 - 1


def touching(sat, first: float, last: float, now: float) -> list[Outage]:
    """The outages of a satellite that hold at some moment between `first` and `last`."""
    return [o for o in getattr(sat, "outages", ()) or () if o.during(first, last, now)]


def on_day(sats: Sequence, day: date, now: float) -> list:
    """
    The satellites as they are on a day: one an outage touches gets its
    stricter values (オフナディア角・1 日の回数・1 パスのシーン数・撮像間隔) for the
    whole day; the others as they are. In the same order (the passes' indices).
    """
    first, last = day_range(day)
    out = []
    for sat in sats:
        values: dict = {}
        for o in touching(sat, first, last, now):
            if o.max_off_nadir is not None and o.max_off_nadir < values.get("maxOffNadir", sat.maxOffNadir):
                values["maxOffNadir"] = o.max_off_nadir
            if o.max_per_day is not None:
                most = values.get("maxPerDay", sat.maxPerDay)
                if not most or o.max_per_day < most:
                    values["maxPerDay"] = o.max_per_day
            if o.scenes_per_pass is not None and o.scenes_per_pass < values.get("scenesPerPass", sat.scenesPerPass):
                values["scenesPerPass"] = max(1, o.scenes_per_pass)
            if o.min_interval is not None and o.min_interval > values.get("minInterval", sat.minInterval):
                values["minInterval"] = o.min_interval
        out.append(dataclasses.replace(sat, **values) if values else sat)
    return out


def system_limits(sats: Iterable, system: str, day: date, now: float) -> dict:
    """
    A system's stricter daily limits on a day, from the outages naming it that
    touch the day (on its satellites): {"count": n, "scenes": n}, each only
    when some outage says; the smallest of them.
    """
    first, last = day_range(day)
    out: dict = {}
    seen: set = set()
    for sat in sats:
        for o in touching(sat, first, last, now):
            if not o.for_system or o.id in seen or plain(o.system) != plain(system):
                continue
            seen.add(o.id)
            for key, value in (("count", o.orders_per_day), ("scenes", o.scenes_per_day)):
                if value is not None and (key not in out or value < out[key]):
                    out[key] = value
    return out


def stopped(sat, first: float, last: float, now: float) -> Optional[Outage]:
    """An outage stopping the satellite's pictures at some moment between `first` and `last` (for satellites counted)."""
    return next((o for o in touching(sat, first, last, now) if o.stop), None)


def warning_of(sat, first: float, last: float, now: float) -> str:
    """The warning of a request taken on a satellite with outages that day ("" when none)."""
    found = touching(sat, first, last, now)
    if not found:
        return ""
    systems = all(o.for_system for o in found)
    return (SYSTEM_WARNING if systems else OUTAGE_WARNING) + "；".join(o.short_text(now, systems) for o in found) + "）"


def summary_text(sats: Iterable, first: float, last: float, now: float, most: int = 3) -> str:
    """「障害中の衛星: ICEYE-X27（撮像停止）、WV110（夜間は撮像できない） ほか 1」 for the satellites with outages then ("" when none)."""
    seen: dict = {}
    for sat in sats:
        for o in touching(sat, first, last, now):
            seen.setdefault(o.id, o)
    if not seen:
        return ""
    items = sorted(seen.values(), key=lambda o: (o.start, o.target))
    shown = [f"{o.target}（{o.impact_text()}）" for o in items[:most]]
    more = f" ほか {len(items) - most}" if len(items) > most else ""
    head = "障害中の衛星・システム: " if any(o.for_system for o in items) else "障害中の衛星: "
    return head + "、".join(shown) + more


def stop_text(o: Outage, now: float) -> str:
    """「SYS-A: 年末年始、1/4 0:00 まで」: a system's 受付停止 told in the reason of the requests it held back."""
    parts = [o.reason] if o.reason else []
    status = o.status(now)
    if status == OVERDUE:
        parts.append(f"{when_text(o.expected)} までの見込みを超過")
    elif o.recovered is not None or o.expected is not None:
        parts.append(f"{when_text(o.recovered if o.recovered is not None else o.expected)} まで")
    else:
        parts.append("終わり未定")
    rest = [i for i in o.impacts() if i != o.stop_text]
    return f"{o.system}: " + "、".join(parts + rest)


def outage_reason(found: Sequence[Outage], now: float) -> str:
    """
    「衛星の障害（ICEYE-X27: 姿勢制御の不具合、撮像停止、復旧見込み 11/20 18:00）」;
    held back by systems' outages only, 「システムの受付停止（SYS-A: 年末年始、1/4 0:00 まで）」
    (受付停止 all of them) or 「システムの障害（…）」.
    """
    seen = list(dict.fromkeys(found))
    more = f" ほか {len(seen) - 2}" if len(seen) > 2 else ""
    if seen and all(o.for_system for o in seen):
        if all(o.stop for o in seen):
            return f"{SYSTEM_STOP}（" + "；".join(stop_text(o, now) for o in seen[:2]) + more + "）"
        return f"{SYSTEM_OUTAGE}（" + "；".join(o.short_text(now, True) for o in seen[:2]) + more + "）"
    return f"{OUTAGE}（" + "；".join(o.short_text(now) for o in seen[:2]) + more + "）"


# ---- the table --------------------------------------------------------------------------------

def outage_of(attributes: dict, fields: dict, oid_field: str = "OBJECTID", updated: str = "") -> Optional[Outage]:
    """The outage of a row's attributes (by the table's names); None without its id, satellite or start."""
    a = attributes

    def get(role: str) -> Any:
        name = fields.get(role)
        return a.get(name) if name else None

    oid = a.get(oid_field)
    start = ms_of(get("start"))
    outage_id, satellite, system = _text(get("id")), _text(get("satellite")), _text(get("system"))
    if not outage_id or not (satellite or system) or start is None:
        return None
    # A row naming both a satellite and a system is the satellite's (the system and its daily limits left
    # aside). A system's row has every impact of a satellite's too (on each of its satellites), and its daily
    # limits (件数・枚数).
    sat_row = bool(satellite)
    per_day = _positive(get("maxPerDay"))
    scenes = _positive(get("scenesPerPass"))
    orders_day = None if sat_row else _positive(get("ordersPerDay"))
    scenes_day = None if sat_row else _positive(get("scenesPerDay"))
    return Outage(
        id=outage_id, satellite=satellite, start=float(start), sensor=_text(get("sensor")) if sat_row else "",
        mode=_text(get("mode")) if sat_row else "",
        reason=_text(get("reason")), expected=_float(ms_of(get("expected"))), recovered=_float(ms_of(get("recovered"))),
        stop=_yes(get("stop")),
        closed=frozenset((asc, side) for role, asc, side, _l in DIRECTIONS if _yes(get(role))),
        max_off_nadir=_positive(get("maxOffNadir")),
        max_per_day=max(1, round(per_day)) if per_day is not None else None,
        scenes_per_pass=max(1, round(scenes)) if scenes is not None else None,
        min_interval=_positive(get("minInterval")), no_night=_yes(get("noNight")),
        other=_text(get("other")), note=_text(get("note")),
        system="" if sat_row else system,
        orders_per_day=max(1, round(orders_day)) if orders_day is not None else None,
        scenes_per_day=max(1, round(scenes_day)) if scenes_day is not None else None,
        object_id=int(oid) if is_number(oid) else None,
        updated_at=_float(ms_of(a.get(updated))) if updated else None,
    )


def _float(v: Optional[int]) -> Optional[float]:
    return float(v) if v is not None else None


def _check(info, source) -> None:
    missing = [source.fields[r] for r in NEEDED if source.fields[r] not in info.field_types]
    if missing:
        raise EsriError(f"衛星の障害情報の表に必要な項目がない（管理者に連絡: {'・'.join(missing)}）", code="E704")


def load_outages(client: EsriClient, source, token: Optional[str] = None,
                 cancelled: Optional[Callable[[], bool]] = None) -> list[Outage]:
    """Every outage of the table (the history too), the latest start first."""
    info = client.layer(source.url, token, refresh=True)
    _check(info, source)
    from .. import edit_stamp

    updated = edit_stamp.tracking().fields.get("updatedAt", "")
    rows = client.query_all(source.url, source.where or "1=1", token, geometry=False, limit=LIMIT, cancelled=cancelled)
    found = [outage_of(r.get("attributes") or {}, source.fields, info.object_id_field, updated) for r in rows]
    return sorted((o for o in found if o is not None), key=lambda o: (-o.start, o.target, o.id))


def new_id(now: Optional[float] = None) -> str:
    """「OUT-20261116-181530-1a2b」 (JST, with a few letters so that two made at once differ)."""
    d = datetime.fromtimestamp((now if now is not None else datetime.now(timezone.utc).timestamp() * 1000) / 1000, tz=JST)
    return f"OUT-{d:%Y%m%d-%H%M%S}-{uuid.uuid4().hex[:4]}"


def attributes_of(o: Outage, fields: dict, types: dict, lengths: dict) -> dict:
    """The row of an outage as the table takes it (only the attributes the table has)."""
    values = {
        "id": o.id, "satellite": o.satellite or None, "sensor": o.sensor or None, "mode": o.mode or None, "reason": o.reason or None,
        "start": int(o.start), "expected": int(o.expected) if o.expected is not None else None,
        "recovered": int(o.recovered) if o.recovered is not None else None,
        "stop": 1 if o.stop else None, "maxOffNadir": o.max_off_nadir, "maxPerDay": o.max_per_day,
        "scenesPerPass": o.scenes_per_pass, "minInterval": o.min_interval, "noNight": 1 if o.no_night else None,
        "other": o.other or None, "note": o.note or None,
        "system": o.system or None, "ordersPerDay": o.orders_per_day, "scenesPerDay": o.scenes_per_day,
    }
    for role, asc, side, _l in DIRECTIONS:
        values[role] = 1 if (asc, side) in o.closed else None
    out = {}
    for role, v in values.items():
        name = fields.get(role)
        if not name or name not in types:
            continue
        if isinstance(v, str) and lengths.get(name):
            v = v[:lengths[name]]
        out[name] = fit_value(v, types[name])
    return out


def check_outage(o: Outage) -> list[str]:
    """What keeps an outage from being saved (empty when it can be)."""
    problems = []
    if not o.satellite.strip() and not o.system.strip():
        problems.append("衛星かシステムを選ぶ")
    if o.expected is not None and o.expected <= o.start:
        problems.append("復旧見込みは発生日時より後に")
    if o.recovered is not None and o.recovered < o.start:
        problems.append("復旧日時は発生日時より後に")
    return problems


def takes_systems(info, source) -> bool:
    """Whether the table has the attribute of a system's outages (SYSTEM_NAME; tables made before 2026-11-24 do not)."""
    name = source.fields.get("system")
    return bool(name) and name in info.field_types


def save_outage(client: EsriClient, source, o: Outage, token: Optional[str] = None) -> Outage:
    """Adds the outage (no object id yet) or updates it; the outage as saved (with its object id)."""
    problems = check_outage(o)
    if problems:
        raise ValueError("、".join(problems))
    info = client.layer(source.url, token, refresh=True)
    _check(info, source)
    if o.for_system and not takes_systems(info, source):
        raise EsriError(f"衛星の障害情報の表にシステムの項目がない（管理者に連絡: {source.fields.get('system') or 'SYSTEM_NAME'}）",
                        code="E704")
    attributes = attributes_of(o, source.fields, info.field_types, info.field_lengths)
    from .. import edit_stamp

    if o.object_id is None:
        # Saved already (the answer lost on the way, then 保存 again): updated, not added twice.
        there = client.find(source.url, source.fields["id"], [o.id], token, out_fields=f"{info.object_id_field},{source.fields['id']}")
        if o.id in there and is_number(there[o.id].get(info.object_id_field)):
            return save_outage(client, source, dataclasses.replace(o, object_id=int(there[o.id][info.object_id_field])), token)
        result = client.apply_edits(source.url, adds=[{"attributes": attributes}], token=token)
        r = (result.add_results or [{}])[0]
        if not r.get("success"):
            raise EsriError(code="E305", detail="追加: " + ((r.get("error") or {}).get("description") or ""))
        return dataclasses.replace(o, object_id=int(r["objectId"]) if is_number(r.get("objectId")) else None,
                                   updated_at=float(edit_stamp.now_ms()))
    attributes[info.object_id_field] = o.object_id
    result = client.apply_edits(source.url, updates=[{"attributes": attributes}], token=token)
    r = (result.update_results or [{}])[0]
    if not r.get("success"):
        raise EsriError(code="E305", detail="更新: " + ((r.get("error") or {}).get("description") or "" or "別の PC で削除済みか"))
    return dataclasses.replace(o, updated_at=float(edit_stamp.now_ms()))


def delete_outages(client: EsriClient, source, outages: Sequence[Outage], token: Optional[str] = None) -> tuple[list[str], list[str]]:
    """Deletes outages (a row entered by mistake); the ids deleted and why the others were not."""
    mine = [o for o in outages if o.object_id is not None]
    failed = [f"{o.target}: 未保存" for o in outages if o.object_id is None]
    if not mine:
        return [], failed
    try:
        results = client.delete_features(source.url, [o.object_id for o in mine], token)
    except EsriError as error:
        return [], failed + [f"{o.target}: {error}" for o in mine]
    by_oid = {r.get("objectId"): r for r in results if isinstance(r, dict)}
    done = []
    for o in mine:
        r = by_oid.get(o.object_id) or {"success": all(x.get("success") for x in results if isinstance(x, dict))}
        if r.get("success"):
            done.append(o.id)
        else:
            failed.append(f"{o.target}: " + ((r.get("error") or {}).get("description") or "削除失敗"))
    return done, failed


def satellite_outages_of(value: Any, problems: list[str]):
    """`satelliteOutages`: the table of outages (URL or `{ "url", "fields", "where" }`)."""
    from ..sources import esri_source_of

    return esri_source_of(value, "satelliteOutages", DEFAULT_OUTAGE_FIELDS, problems, editable=True)
