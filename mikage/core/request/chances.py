"""
残りの撮像機会 (改修案 4, tsudo 2026-11-12: 「締め切りを残り日数ではなく残り
の撮像機会で見る」): how many more chances a 期間内1回 request has after the
day planned, from the satellites' passes over the next days (`lookAheadDays`,
7) and the forecast at their moments.

- A chance is one pass of a satellite (any of its modes, once) able to take
  the request, inside its period and conditions (passes.find_passes), of a
  satellite fitted on the orbits. An optical pass at or above the request's
  「実施しない雲量」 (for either kind, daily.optical_no_go) is no chance; a
  pass beyond the forecast is one.
- Scarce: the request has a pass on the day, its deadline falls inside the
  days looked at, and SCARCE or fewer chances follow (「今日を逃すと残り 1
  回」). It is then treated as near its deadline (daily.DEADLINE_DAYS): it
  goes whatever the clouds (warned), is offered a raise, what was handed out
  for it before is not left out, and it comes first in its priority band.
- Clearer later: an optical request whose day is cloudy (YIELD_FROM % or
  more at its least cloudy pass) and that has a pass inside the days looked
  at YIELD_TENTHS tenths or more clearer, with YIELD_CHANCES chances or
  more left (or its deadline past the days looked at): it comes last in its
  priority band, so on a day short of room the others go first (it is not
  held back otherwise).

A request whose satellites have no known orbits (no TLE, `orbit: false`)
has no chances worked out: its deadline counts by days as before.

Pure Python (no Qt): the dock, the limit simulation and the tests share it.
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import date, timedelta
from typing import Optional, Sequence

from ..plan.satellites import SatelliteSpec, platform_key
from ..plan.weather import CloudSeries
from .model import ImagingRequest, day_bounds, jst_day, occurs_on
from .passes import Passes
from .weather import cloud_step, cloud_when

#: The days after the day planned looked at (requestStores[].lookAheadDays; 0: not looked at).
LOOK_AHEAD_DAYS = 7
#: The most days that may be set.
MOST_DAYS = 16
#: At most this many chances after the day (its deadline inside the days looked at): the day is its chance.
SCARCE = 1
#: The cover of the day (percent) from which an optical request may give way to the others for a clearer pass later.
YIELD_FROM = 30
#: How many tenths clearer a later pass has to be for that.
YIELD_TENTHS = 3
#: The chances it needs left for that (unless its deadline is past the days looked at).
YIELD_CHANCES = 2


@dataclass(frozen=True)
class Chances:
    """A request's chances on the day planned and after it."""
    #: Passes on the day (in its window that day).
    today: int
    #: Chances after the day, to its deadline or the last day looked at (too cloudy optical passes left out).
    left: int
    #: The last day looked at (its deadline, when inside the days looked at).
    until: date
    #: Whether its deadline falls inside the days looked at (`left` is then all it has).
    whole: bool
    #: (day, cover) of the clearest later pass, when YIELD_TENTHS tenths clearer than the day's; None otherwise.
    clearer: Optional[tuple] = None

    @property
    def scarce(self) -> bool:
        return self.whole and self.today > 0 and self.left <= SCARCE

    def yields(self, cloud: Optional[float]) -> bool:
        """Whether an optical request whose day is `cloud` gives way for a clearer pass later."""
        return (self.clearer is not None and cloud is not None and cloud >= YIELD_FROM
                and (self.left >= YIELD_CHANCES or not self.whole))


def look_ahead_days(value) -> int:
    """`requestStores[].lookAheadDays`: 0 … MOST_DAYS (LOOK_AHEAD_DAYS when not a number)."""
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        return LOOK_AHEAD_DAYS
    return int(max(0, min(MOST_DAYS, math.floor(value + 0.5))))


def wanted(requests: Sequence[ImagingRequest], day: date, missed: Sequence[str] = ()) -> list[ImagingRequest]:
    """The requests whose chances are looked at: 期間内1回, 有効, on the day (or carried over to it), ending after it."""
    from .daily import carries_over

    _, last = day_bounds(day)
    missed = set(missed)
    return [r for r in requests if r.achieve == "期間内1回" and r.status == "有効" and math.isfinite(r.end) and r.end > last
            and (occurs_on(r, day) or r.id in missed and carries_over(r, day))]


def ahead_span(day: date, days: int) -> tuple[float, float]:
    """From the end of the day planned to the end of the last day looked at (ms)."""
    return day_bounds(day)[1], day_bounds(day + timedelta(days=max(0, days)))[1]


def _distinct(ops: Sequence, sats: Sequence[SatelliteSpec]) -> int:
    """Passes counted once per satellite (all its modes) and minute."""
    return len({(platform_key(sats[o.satellite]), round(o.time / 60000)) for o in ops})


def chances_of(requests: Sequence[ImagingRequest], day: date, today: Optional[Passes], ahead: Optional[Passes],
               sats: Sequence[SatelliteSpec], forecasts: Optional[dict] = None, used: Optional[set] = None,
               clouds: Optional[dict] = None, days: int = LOOK_AHEAD_DAYS) -> dict:
    """
    request id → Chances, for those of `requests` (wanted) whose satellites'
    orbits are known: `today` the day's passes, `ahead` those after it
    (find_passes over ahead_span; the same Passes may give both), `forecasts`
    the clouds' (CloudSeries by place), `used` the satellites fitted on the
    orbits (workflow.orbiting; None: all), `clouds` the day's cover by request
    (workflow.clouds_of), `days` the days looked at.
    """
    from .daily import mixed_kinds, optical_no_go, wants_weather

    out: dict = {}
    if today is None or ahead is None or days <= 0:
        return out
    _, day_end = day_bounds(day)
    last_day = day + timedelta(days=days)
    _, horizon = day_bounds(last_day)
    forecasts = forecasts or {}
    for req in requests:
        if req.id not in today.targets and req.id not in ahead.targets:
            continue
        mine = [o for o in today.of(req, day) if used is None or o.satellite in used]
        deadline = jst_day(req.end)
        whole = deadline <= last_day
        end = min(req.end, horizon)
        later = [o for o in ahead.ops.get(req.id, ()) if day_end < o.time <= end and (used is None or o.satellite in used)]
        weather, mixed = wants_weather(req, sats), mixed_kinds(req, sats)
        no_go = req.cloud_no_go if weather else optical_no_go(req) if mixed else None
        good: list = []
        clearest: Optional[tuple] = None
        for o in later:
            optical = not sats[o.satellite].sar
            cloud = cloud_when(req, forecasts, o.time) if optical and (weather or mixed) and forecasts else None
            if cloud is not None and no_go is not None and cloud >= no_go:
                continue
            good.append(o)
            if weather and cloud is not None and (clearest is None or cloud < clearest[1]):
                clearest = (jst_day(o.time), cloud)
        clearer = None
        now = (clouds or {}).get(req.id)
        if weather and clearest is not None and now is not None and cloud_step(now) - cloud_step(clearest[1]) >= YIELD_TENTHS:
            clearer = clearest
        out[req.id] = Chances(_distinct(mine, sats), _distinct(good, sats), min(deadline, last_day), whole, clearer)
    return out


def short_day(d: date) -> str:
    return f"{d.month}/{d.day}"


def scarce_text(ch: Chances) -> str:
    """「残りの撮像機会 1 回（11/14 まで）」."""
    return f"残りの撮像機会 {ch.left} 回（{short_day(ch.until)} まで）"


def clearer_text(ch: Chances) -> str:
    """「11/14 のほうが晴れる見込み（雲 10%）」."""
    d, cloud = ch.clearer
    return f"{short_day(d)} のほうが晴れる見込み（雲 {round(cloud)}%）"


# --- 依頼フォームの撮像機会 (改修案 7) ---------------------------------------------------------------

#: The days ahead the request form counts a request's chances over.
FORM_DAYS = 14


def form_span(req: ImagingRequest, now: float, days: int = FORM_DAYS) -> Optional[tuple]:
    """(first, last, whole) ms: its period from now on, `days` days at most (`whole`: to its end); None when it is over."""
    if not math.isfinite(req.start) or not math.isfinite(req.end):
        return None
    first = max(req.start, now)
    last = min(req.end, first + days * 86400000)
    if last <= first:
        return None
    return first, last, last >= req.end


def _when(ms: float) -> str:
    from datetime import datetime

    from .model import JST

    t = datetime.fromtimestamp(ms / 1000, tz=JST)
    return f"{t.month}/{t.day} {t.hour:02d}:{t.minute:02d}"


def form_text(req: ImagingRequest, passes: Optional[Passes], sats: Sequence[SatelliteSpec], span: Optional[tuple],
              days: int = FORM_DAYS) -> tuple[str, bool]:
    """
    The request form's line on how often the satellites can take the request
    as written (tsudo 2026-11-12, 改修案 7: one line, nothing to set): its
    passes (find_passes over `span`, form_span) counted once per satellite and
    minute, and the first; whether it has none (shown red). Clouds are not
    weighed (no forecast that far).

    「撮像機会: 今後 14 日で 9 回（最初 11/13 10:32 ISS）」,
    「撮像機会なし（この条件で撮れる衛星なし）」.
    """
    from .route import why_not

    if span is None:
        return "撮像機会: 期間が終わっている", False
    if passes is None:
        return "", False
    first, last, whole = span
    able = [i for i in sorted(passes.known) if why_not(req, sats[i]) is None]
    if not passes.known:
        return "撮像機会: 衛星の軌道（TLE）が分からず数えられない", False
    if not able:
        return "撮像機会なし（この条件で撮れる衛星なし）", True
    # Only the days it is asked for (定点: those of its repeat).
    ops = [o for o in passes.ops.get(req.id, ()) if first <= o.time <= last and occurs_on(req, jst_day(o.time))]
    span_text = f"{short_day(jst_day(last))} まで" if whole else f"今後 {days} 日"
    if not ops:
        return f"撮像機会なし（{span_text}、範囲の上を通るパスなし）", True
    o = min(ops, key=lambda x: x.time)
    until = f"{span_text}に" if whole else f"{span_text}で"
    return f"撮像機会: {until} {_distinct(ops, sats)} 回（最初 {_when(o.time)} {sats[o.satellite].name}）", False
