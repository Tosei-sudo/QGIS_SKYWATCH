"""
年間撮像上限 (yearly quotas of scenes, per sensor and mode; tsudo,
2026-11-11: 「センサー、モードごとに、年間撮像枚数の上限があるので、それを
管理したい」「年の途中から運用しているので、当初年は現在の消費枚数を設定
したい」「9月から1年とかあるので、ウィンドウは変えられるように」「全部ある
わけではない」).

The quotas are rows of an Esri table (`requestStores[].quotas`,
TBL_IMAGING_QUOTA): a sensor (its name as the sensor modes table writes it,
or a satellite's name), one of its modes or all of them (an empty mode),
the scenes a year (none: not limited, only counted), the month its year
starts in (1 when empty), and for the year the plugin started being used
in, the scenes already used before a day (INITIAL_USED, COUNT_FROM).
A sensor or mode without a row has no limit.

What a year has used is counted per shot (TBL_REQUEST_SHOT: one row of a
system's file, the requests put together in it riding on it; tsudo: 「個の
画像に関して管理する情報はショットのほうがいい」), never per request:

- taken (撮像済み: any request of the shot found taken): its scenes, less
  those the system does not count (UNCOUNTED_SCENES: noise, jamming …,
  set here since the image catalogs cannot be written to: 「こちらで完結
  できることが必須」);
- handed out and not settled yet (見込み): its scenes, kept off what is
  left so that the days planned meanwhile do not go past the limit;
- missed (未撮像) or turned down by the system (不採用): nothing.

The year of the first use counts INITIAL_USED and only the shots from
COUNT_FROM on. The day's calculation (daily.allocate) gives a mode no more
scenes than its quotas have left (年間上限), but for requests taken by
hand (warned); the shots of the day planned are worked out again, so they
are not 見込み for it.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from typing import Any, Callable, Iterable, Optional, Sequence

from ..esri import EsriClient, EsriError, quote
from ..plan.names import plain
from ..util import is_number
from .model import JST
from .store import (
    HistoryRow, RequestStoreConfig, ShotRow, load_history, load_shots_where, shot_days_where,
)

#: Why a request is held back: every mode that could take it has used its year's scenes.
QUOTA = "年間上限（残りの枚数なし）"
#: The start of the warning of a request taken by hand past a quota.
QUOTA_OVER = "年間上限を超える（"
#: What became of a shot, for the quotas (NO_ROWS: no history row names it any more, e.g. its rows were written again).
USED, PENDING, MISSED, REJECTED, NO_ROWS = "撮像済み", "見込み", "未撮像", "不採用", "履歴なし"
#: Reasons offered for scenes the system does not count (any text may be written).
UNCOUNTED_REASONS = ("ノイズ", "ジャミング", "システム側の不具合", "その他")
#: What is left under this share of the limit is told as running low.
LOW_SHARE = 0.1


def _text(v: Any) -> str:
    return "" if v is None else str(v).strip()


def _whole(v: Any) -> Optional[int]:
    """A whole number ≥ 0 of an attribute (a number, or text of one); None when empty or not one."""
    if isinstance(v, str):
        v = v.strip()
        if not v:
            return None
        try:
            v = float(v)
        except ValueError:
            return None
    if not is_number(v) or isinstance(v, bool) or not math.isfinite(v) or v < 0:
        return None
    return int(math.floor(v + 0.5))


def _day(v: Any) -> Optional[date]:
    """A day of a date attribute (ms, as the JST day) or of text 「2026-11-12」; None when not one."""
    if is_number(v) and not isinstance(v, bool) and math.isfinite(v):
        return datetime.fromtimestamp(v / 1000, tz=timezone.utc).astimezone(JST).date()
    text = _text(v)[:10].replace("/", "-")
    try:
        return date.fromisoformat(text)
    except ValueError:
        return None


# --- Years -----------------------------------------------------------------------------------

@dataclass(frozen=True)
class Window:
    """One quota year: from its first day to the day before `end`."""
    first: date
    end: date

    @property
    def last(self) -> date:
        return self.end - timedelta(days=1)

    def has(self, day: Any) -> bool:
        """Whether a day (a date, or text 「2026-11-12」) falls in it."""
        text = day.isoformat() if isinstance(day, date) else _text(day)[:10]
        return bool(text) and self.first.isoformat() <= text < self.end.isoformat()

    @property
    def text(self) -> str:
        """「2026/9/1〜2027/8/31」."""
        a, b = self.first, self.last
        return f"{a.year}/{a.month}/{a.day}〜{b.year}/{b.month}/{b.day}"

    def before(self) -> "Window":
        """The year before."""
        return Window(_shift(self.first, -1), self.first)


def _shift(d: date, years: int) -> date:
    return date(d.year + years, d.month, 1)


def window_of(day: date, start_month: int = 1) -> Window:
    """The quota year a day falls in, the year starting on the 1st of `start_month`."""
    m = start_month if 1 <= start_month <= 12 else 1
    year = day.year if day.month >= m else day.year - 1
    first = date(year, m, 1)
    return Window(first, _shift(first, 1))


# --- Quotas ----------------------------------------------------------------------------------

@dataclass
class Quota:
    """One row of the quotas table."""
    sensor: str
    #: The mode; "" for every mode of the sensor together.
    mode: str = ""
    #: Scenes a year; None: not limited (only counted).
    limit: Optional[int] = None
    #: The month the year starts in (1 … 12).
    start_month: int = 1
    #: Scenes used before `count_from`, in the year it falls in (the plugin started being used in it).
    initial_used: int = 0
    count_from: Optional[date] = None
    note: str = ""
    object_id: Optional[int] = None

    @property
    def label(self) -> str:
        """「WV110（パンシャープン）」, 「WV110（全モード）」."""
        return f"{self.sensor}（{self.mode or '全モード'}）"

    def window(self, day: date) -> Window:
        return window_of(day, self.start_month)

    def matches(self, sensor: Any, mode: Any, *names: Any) -> bool:
        """
        Whether scenes of a sensor's mode count against it: its sensor is the
        sensor (or one of `names`, the satellite's), and its mode the mode
        (any mode when it names none). Names compare as plain letters and digits.
        """
        keys = {plain(x) for x in (sensor, *names)} - {""}
        if plain(self.sensor) not in keys:
            return False
        return not self.mode or plain(self.mode) == plain(mode)

    def matches_satellite(self, sat) -> bool:
        """Whether a satellite (one mode of it, plan.satellites.SatelliteSpec) counts against it."""
        return self.matches(sat.sensor, sat.mode, sat.name, sat.id)

    def matches_shot(self, shot: ShotRow) -> bool:
        return self.matches(shot.sensor, shot.mode, shot.satellite)


def quota_of(a: dict, f: dict, oid: str = "OBJECTID") -> Optional[Quota]:
    """The quota of a row's attributes (by the table's names); None without a sensor."""
    sensor = _text(a.get(f["sensor"]))
    if not sensor:
        return None
    month = _whole(a.get(f["startMonth"]))
    return Quota(sensor=sensor, mode=_text(a.get(f["mode"])), limit=_whole(a.get(f["limit"])),
                 start_month=month if month is not None and 1 <= month <= 12 else 1,
                 initial_used=_whole(a.get(f["initialUsed"])) or 0, count_from=_day(a.get(f["countFrom"])),
                 note=_text(a.get(f["note"])), object_id=int(a[oid]) if is_number(a.get(oid)) else None)


def quotas_of(rows: Iterable[dict], f: dict, problems: Optional[list] = None, oid: str = "OBJECTID") -> list[Quota]:
    """The quotas of the table's rows (by sensor, then mode); what cannot be used goes to `problems`."""
    problems = problems if problems is not None else []
    out: list[Quota] = []
    seen: dict = {}
    for a in rows:
        q = quota_of(a, f, oid)
        if q is None:
            continue
        month = a.get(f["startMonth"])
        if _text(month) and q.start_month != _whole(month):
            problems.append(f"年間撮像上限「{q.label}」の年の始まりの月は 1〜12 で（1 月として数える）")
        if q.initial_used and q.count_from is None:
            problems.append(f"年間撮像上限「{q.label}」の当初の消費枚数には数え始める日が必要（当初の消費枚数は数えない）")
            q.initial_used = 0
        key = (plain(q.sensor), plain(q.mode))
        if key in seen:
            problems.append(f"年間撮像上限「{q.label}」の行が 2 つ以上（どちらの上限も守る）")
        seen[key] = q
        out.append(q)
    return sorted(out, key=lambda q: (q.sensor.casefold(), q.mode != "", q.mode.casefold()))


def load_quotas(client: EsriClient, store: RequestStoreConfig, token: Optional[str],
                cancelled: Optional[Callable[[], bool]] = None, problems: Optional[list] = None) -> list[Quota]:
    """The quotas of a store's table ([] without one); EsriError when it cannot be read."""
    if not store.quotas:
        return []
    f = store.quota_fields
    info = client.layer(store.quotas, token, refresh=True)
    if f["sensor"] not in set(info.field_types):
        raise EsriError(f"年間撮像上限のテーブルに属性 {f['sensor']} なし")
    rows = client.query_all(store.quotas, "1=1", token, geometry=False, limit=10000, cancelled=cancelled)
    return quotas_of((r.get("attributes") or {} for r in rows), f, problems, info.object_id_field)


# --- What a year has used ------------------------------------------------------------------------

def shot_state(shot: ShotRow, rows: Sequence[HistoryRow] = (), looked: bool = True) -> str:
    """
    What became of a shot (USED, PENDING, MISSED, REJECTED), from the history
    rows of the requests in it (`rows`, those naming its SHOT_ID), else from
    the shot's own CAPTURED. A shot not settled that no row names although
    they were looked for (`looked`) is NO_ROWS: counted nowhere.
    """
    taken = [r for r in rows if r.result == "採用"]
    if not taken:
        settled = {"撮像済み": USED, "未撮像": MISSED}.get(shot.captured)
        return settled or (NO_ROWS if looked and not rows else REJECTED if rows else PENDING)
    if any(r.captured == "撮像済み" for r in taken):
        return USED
    if all(r.captured == "未撮像" or r.adoption == "不採用" for r in taken):
        return MISSED if any(r.captured == "未撮像" for r in taken) else REJECTED
    return PENDING


@dataclass
class ShotUse:
    """One shot of a quota's year, and what it counts."""
    shot: ShotRow
    state: str
    rows: list = field(default_factory=list)
    #: A shot of a day planned again (not counted as 見込み for it).
    replanned: bool = False

    @property
    def scenes(self) -> int:
        return max(0, self.shot.scenes or 0)

    @property
    def uncounted(self) -> int:
        """The scenes the system does not count (never more than it has)."""
        return min(self.scenes, self.shot.uncounted or 0)

    @property
    def counted(self) -> int:
        return self.scenes - self.uncounted


@dataclass
class Usage:
    """What one quota's year has used, and what is left of it."""
    quota: Quota
    window: Window
    #: INITIAL_USED, when the year is the one COUNT_FROM falls in.
    initial: int = 0
    #: Scenes of the shots taken, less those not counted.
    used: int = 0
    #: Scenes of the shots handed out and not settled yet.
    pending: int = 0
    #: Scenes of the shots taken that the system does not count.
    uncounted: int = 0
    shots: list = field(default_factory=list)

    @property
    def consumed(self) -> int:
        return self.initial + self.used

    @property
    def remaining(self) -> Optional[int]:
        """What is left (the 見込み taken off); None when not limited. Below 0 when gone past."""
        if self.quota.limit is None:
            return None
        return self.quota.limit - self.consumed - self.pending

    def low(self, spent: int = 0) -> bool:
        """Whether what is left after `spent` more is under LOW_SHARE of the limit."""
        left = self.remaining
        return left is not None and left - spent <= self.quota.limit * LOW_SHARE

    def text(self, spent: int = 0) -> str:
        """
        「WV110（パン）: 年間 500 枚のうち 消費 380（当初 300）・見込み 20・今回 15・
        残り 85（2026/9/1〜2027/8/31）」.
        """
        parts = [f"消費 {self.consumed}" + (f"（当初 {self.initial}）" if self.initial else "")]
        if self.pending:
            parts.append(f"見込み {self.pending}")
        if spent:
            parts.append(f"今回 {spent}")
        if self.uncounted:
            parts.append(f"数えない {self.uncounted}")
        if self.quota.limit is None:
            return f"{self.quota.label}: 上限なし・" + "・".join(parts) + f"（{self.window.text}）"
        left = self.remaining - spent
        parts.append(f"残り {left}" if left >= 0 else f"{-left} 枚超過")
        return f"{self.quota.label}: 年間 {self.quota.limit} 枚のうち " + "・".join(parts) + f"（{self.window.text}）"


def usage_of(quota: Quota, window: Window, shots: Sequence[ShotRow], rows: Optional[dict] = None,
             replanned: Optional[tuple] = None, extra: int = 0, looked: bool = True) -> Usage:
    """
    What a quota's year has used: `shots` (any; those of the year and of the
    quota's sensor and mode count), `rows` the history rows by SHOT_ID of
    the shots not yet settled on the shot layer (`looked`: they were read),
    `replanned` (first, last day: text) the days planned again (their open
    shots are not 見込み), `extra` scenes handed out besides (a simulation's
    earlier days).
    """
    rows = rows or {}
    u = Usage(quota, window, pending=extra)
    start = quota.count_from if quota.count_from is not None and window.has(quota.count_from) else None
    if start is not None:
        u.initial = quota.initial_used
    for shot in shots:
        if not window.has(shot.day) or not quota.matches_shot(shot):
            continue
        if start is not None and shot.day[:10] < start.isoformat():
            # Before the plugin counted: in INITIAL_USED.
            continue
        mine = rows.get(shot.shot_id, [])
        use = ShotUse(shot, shot_state(shot, mine, looked), list(mine))
        u.shots.append(use)
        if use.state == USED:
            u.used += use.counted
            u.uncounted += use.uncounted
        elif use.state == PENDING:
            if replanned and replanned[0] <= shot.day[:10] <= replanned[1]:
                use.replanned = True
                continue
            u.pending += use.counted
    u.shots.sort(key=lambda s: (s.shot.day, s.shot.pass_time or 0, s.shot.shot_id))
    return u


@dataclass
class QuotaData:
    """The quotas of a store and the shots of their years, read once (read_quota_data)."""
    quotas: list = field(default_factory=list)
    shots: list = field(default_factory=list)
    #: History rows by SHOT_ID, of the shots not settled on the shot layer.
    rows: dict = field(default_factory=dict)
    #: Whether the history was read for those shots (there is a history table).
    looked: bool = True
    problems: list = field(default_factory=list)

    def usages(self, day: date, replanned: Optional[tuple] = None, extra: Optional[dict] = None,
               limited: bool = False) -> list[Usage]:
        """Each quota's year of `day` (only those with a limit when `limited`); `extra`: (quota index, year's first day) → scenes."""
        out = []
        for i, q in enumerate(self.quotas):
            if limited and q.limit is None:
                continue
            w = q.window(day)
            out.append(usage_of(q, w, self.shots, self.rows, replanned, (extra or {}).get((i, w.first), 0), self.looked))
        return out

    def book(self, day: date, replanned: Optional[tuple] = None, extra: Optional[dict] = None) -> "QuotaBook":
        """What the limited quotas have left on `day`, for the fitting (the day itself planned again unless said)."""
        days = replanned or (day.isoformat(), day.isoformat())
        return QuotaBook(self.usages(day, days, extra, limited=True), indices=[
            i for i, q in enumerate(self.quotas) if q.limit is not None])


def read_quota_data(client: EsriClient, store: RequestStoreConfig, token: Optional[str], days: Sequence[date],
                    cancelled: Optional[Callable[[], bool]] = None) -> QuotaData:
    """
    The quotas and the shots of their years for `days` (the years each day
    falls in, and the year before for the window list), with the history rows
    of the shots the shot layer does not tell settled. EsriError when the
    quotas table cannot be read.
    """
    out = QuotaData()
    out.quotas = load_quotas(client, store, token, cancelled, out.problems)
    if not out.quotas or not days:
        return out
    if not store.shots:
        out.problems.append("撮像の範囲（requestStores[].shots）がないため、年間撮像上限は当初の消費枚数だけで数える")
        return out
    windows = [q.window(d) for q in out.quotas for d in days]
    first, last = min(w.first for w in windows), max(w.last for w in windows)
    out.shots = load_shots_where(client, store, token, shot_days_where(store, first.isoformat(), last.isoformat()),
                                 cancelled=cancelled)
    open_shots = [s for s in out.shots if s.captured not in ("撮像済み", "未撮像")]
    out.looked = bool(store.history)
    if open_shots and store.history:
        hf = store.history_fields
        a, b = min(s.day for s in open_shots), max(s.day for s in open_shots)
        where = (f"{hf['day']} >= {quote(a)} AND {hf['day']} <= {quote(b)} AND {hf['shotId']} IS NOT NULL"
                 f" AND {hf['shotId']} <> ''")
        wanted = {s.shot_id for s in open_shots}
        for r in load_history(client, store, token, where, cancelled=cancelled):
            if r.shot_id in wanted:
                out.rows.setdefault(r.shot_id, []).append(r)
    return out


# --- The day's fitting ---------------------------------------------------------------------------

@dataclass
class QuotaBook:
    """What the quotas have left on the day planned, and what the fitting gives each (daily.allocate)."""
    usages: list = field(default_factory=list)
    #: The index in QuotaData.quotas of each usage (a simulation adds a day's scenes to it).
    indices: list = field(default_factory=list)
    #: id(usage) → scenes given by the fitting (reset by each fitting).
    spent: dict = field(default_factory=dict)

    def reset(self) -> None:
        self.spent = {}

    def of(self, sat) -> list[Usage]:
        """The limited quotas a satellite (a mode of it) counts against."""
        return [u for u in self.usages if u.quota.limit is not None and u.quota.matches_satellite(sat)]

    def left(self, u: Usage) -> int:
        return (u.remaining or 0) - self.spent.get(id(u), 0)

    def over(self, sat, scenes: int) -> Optional[Usage]:
        """The first quota that `scenes` more of a satellite would go past; None when all have room (or none limits it)."""
        if sat is None or scenes <= 0:
            return None
        return next((u for u in self.of(sat) if scenes > self.left(u)), None)

    def take(self, sat, scenes: int) -> None:
        if sat is None or scenes <= 0:
            return
        for u in self.of(sat):
            self.spent[id(u)] = self.spent.get(id(u), 0) + scenes

    def over_text(self, u: Usage) -> str:
        """The warning of a request taken by hand past a quota: 「年間上限を超える（WV110（パン）残り 0 枚・手動で採用）」."""
        return f"{QUOTA_OVER}{u.quota.label} 残り {max(0, self.left(u))} 枚・手動で採用）"

    def relevant(self, sats: Iterable) -> list[Usage]:
        """The usages some of `sats` count against, in the table's order."""
        sats = list(sats)
        return [u for u in self.usages if any(u.quota.matches_satellite(s) for s in sats)]

    def texts(self, sats: Optional[Iterable] = None) -> list[tuple[str, bool]]:
        """(Usage.text with what the fitting gave, running low or past) of the quotas `sats` count against (all when None)."""
        usages = self.usages if sats is None else self.relevant(sats)
        return [(u.text(self.spent.get(id(u), 0)), u.low(self.spent.get(id(u), 0))) for u in usages]
