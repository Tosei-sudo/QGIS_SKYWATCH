"""
The satellites as a checklist (the viewer's satellite-tree.ts, without the
widgets): kind (optical / SAR) → series → satellite → mode. A group checks
everything in it (a satellite by its usual mode, unless some of its modes
are checked already) or clears it. Shared by the imaging plan and the
satellite pass docks; ui/satellite_tree.py draws it.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable, Optional, Sequence

from .access import tle_epoch
from .output import OLD_TLE, js_round, natural_key
from .satellites import SAMPLE_SOURCE, SatelliteSpec, platform_key, satellite_label


def satellite_key(sat: SatelliteSpec) -> str:
    """The same satellite (and mode) again after the list is read again."""
    return platform_key(sat) + (f"\n{sat.mode}" if sat.mode else "")


@dataclass
class Listed:
    sat: SatelliteSpec
    key: str
    checked: bool
    #: Listed under its satellite's name, as one of several modes.
    mode_only: bool = False


@dataclass
class Group:
    key: str
    name: str
    level: int
    members: list
    #: Groups and Listed, in order.
    children: list = field(default_factory=list)


def facts_of(sat: SatelliteSpec) -> str:
    """「最大 30°・幅 10 km・間隔 10 分」: a satellite's specifications."""
    parts = [
        f"最大 {js_round(sat.maxOffNadir)}°",
        f"最小 {js_round(sat.minOffNadir)}°" if sat.minOffNadir > 0 else "",
        f"幅 {js_round(sat.swath)} km",
        f"連続 {sat.scenesPerPass} シーン" if sat.scenesPerPass > 1 else "",
        f"間隔 {js_round(sat.minInterval)} 分" if sat.minInterval > 0 else "",
        f"1 日 {sat.maxPerDay} 回" if sat.maxPerDay > 0 else "",
        f"{js_round(sat.leadTime)} 時間前までに発注" if sat.leadTime > 0 else "",
        "" if sat.lookSide == "both" else ("右向き" if sat.lookSide == "right" else "左向き"),
        f"分解能 {js_round(sat.resolution, 2)} m" if sat.resolution is not None else "",
    ]
    return "・".join(p for p in parts if p)


def tle_age_days(sat: SatelliteSpec, now_ms: float) -> float:
    return (now_ms - tle_epoch(sat)) / 86400000


def is_old(sat: SatelliteSpec, now_ms: float) -> bool:
    age = tle_age_days(sat, now_ms)
    return sat.source != SAMPLE_SOURCE and age == age and abs(age) > OLD_TLE


def source_text(sat: SatelliteSpec, now_ms: float) -> str:
    if sat.source == SAMPLE_SOURCE:
        return "サンプル（決め打ちの軌道。架空の衛星）"
    age = tle_age_days(sat, now_ms)
    return sat.source + ("" if age != age else f"・TLE {js_round(age, 0)} 日前")


def _by(items: list, key: Callable) -> list:
    groups: dict = {}
    for item in items:
        groups.setdefault(key(item), []).append(item)
    return sorted(groups.items(), key=lambda kv: natural_key(kv[0]))


class SatelliteChoice:
    """Which satellites (and modes) are chosen."""

    def __init__(self) -> None:
        self.listed: list[Listed] = []

    def set(self, sats: Sequence[SatelliteSpec], check_new: Callable[[SatelliteSpec, bool], bool] = lambda s, first: True) -> set:
        """Lists `sats`, keeping which were checked; returns the keys new to the list that start checked."""
        before = {l.key: l.checked for l in self.listed}
        first = not self.listed
        platforms: dict = {}
        for sat in sats:
            platforms[platform_key(sat)] = platforms.get(platform_key(sat), 0) + 1
        added = set()
        listed = []
        for sat in sats:
            key = satellite_key(sat)
            checked = before[key] if key in before else (sat.primary is not False and check_new(sat, first))
            if not first and key not in before and checked:
                added.add(key)
            listed.append(Listed(sat, key, checked, platforms[platform_key(sat)] > 1))
        self.listed = listed
        return added

    def tree(self) -> list[Group]:
        """The kinds, each with its series, each with its satellites (a satellite with several modes as a group of them)."""
        kinds = [("optical", "光学", [l for l in self.listed if not l.sat.sar]), ("sar", "SAR", [l for l in self.listed if l.sat.sar])]
        out = []
        for kind, name, members in kinds:
            if not members:
                continue
            series_groups = []
            for series, in_series in _by(members, lambda l: l.sat.series):
                children = []
                for sat_name, same in _by(in_series, lambda l: l.sat.name):
                    for key, modes in _by(same, lambda l: platform_key(l.sat)):
                        children.append(Group(f"{kind}/{series}/{key}", sat_name, 3, modes, list(modes)) if len(modes) > 1 else modes[0])
                series_groups.append(Group(f"{kind}/{series}", series, 2, in_series, children))
            out.append(Group(kind, name, 1, members, series_groups))
        return out

    def checked(self) -> list[SatelliteSpec]:
        return [l.sat for l in self.listed if l.checked]

    def _platforms(self, members: Sequence[Listed]) -> list[list[Listed]]:
        groups: dict = {}
        for m in members:
            groups.setdefault(platform_key(m.sat), []).append(m)
        return list(groups.values())

    def choose(self, members: Sequence[Listed], on: bool) -> None:
        """Checks `members` (each satellite with none of its modes checked by its usual mode) or clears them."""
        if not on:
            for m in members:
                m.checked = False
            return
        for same in self._platforms(members):
            if any(m.checked for m in same):
                continue
            next((m for m in same if m.sat.primary is not False), same[0]).checked = True

    def check_all(self, on: bool) -> None:
        self.choose(self.listed, on)

    def check_only(self, names: Sequence[str]) -> None:
        """Checks the satellites named (or with these ids) by their usual modes, a name with its mode just that mode; clears the rest."""
        for l in self.listed:
            l.checked = False
        wanted = set(names)
        for l in self.listed:
            s = l.sat
            if s.mode and ({f"{s.name}（{s.mode}）", f"{s.name} {s.mode}", f"{s.id} {s.mode}"} & wanted):
                l.checked = True
        self.choose([l for l in self.listed if l.sat.name in wanted or l.sat.id in wanted], True)

    def mode_names(self) -> list[str]:
        out: list[str] = []
        for l in self.listed:
            if l.sat.mode and l.sat.mode not in out:
                out.append(l.sat.mode)
        return out

    def check_mode(self, mode: str, on: bool) -> None:
        for l in self.listed:
            if l.sat.mode == mode:
                l.checked = on

    def tally(self, members: Optional[Sequence[Listed]] = None) -> tuple[int, int]:
        """Satellites with a mode checked, and satellites, among `members` (all by default)."""
        platforms = self._platforms(self.listed if members is None else members)
        return sum(1 for same in platforms if any(m.checked for m in same)), len(platforms)

    def mode_state(self, mode: str) -> tuple[int, int]:
        members = [l for l in self.listed if l.sat.mode == mode]
        return sum(1 for l in members if l.checked), len(members)

    @staticmethod
    def matches(l: Listed, text: str) -> bool:
        """Whether a satellite stays shown for a search: all words in its name, id, series, mode or sensor (checked ones always)."""
        words = text.strip().lower().split()
        hay = f"{l.sat.name} {l.sat.id} {l.sat.series} {l.sat.mode or ''} {l.sat.sensor or ''}".lower()
        return l.checked or all(w in hay for w in words)


def label_of(l: Listed) -> str:
    return l.sat.mode if l.mode_only and l.sat.mode else satellite_label(l.sat)
