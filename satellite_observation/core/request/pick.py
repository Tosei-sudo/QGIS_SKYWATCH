"""
The satellites of an imaging request picked in the imaging plan's tree
(kind → series → satellite → mode), and back.

A request keeps three lists of names (衛星シリーズ・衛星・センサーモード;
see route.why_not, each one has to hold when given). The form shows them as
checks in the tree and writes them back when the checks change:

- Nothing checked: no condition (any satellite).
- Checks that fill whole series only: those series.
- Otherwise the satellites checked, by name (no series, as both would have
  to hold); satellites already named keep their order (書いた順に優先) and the
  newly checked ones follow in the tree's order.
- Modes only when some mode of a satellite checked is left out: the modes
  checked.

Names are written as the request had them when they still match (an alias
from satelliteAliases stays an alias), and names the catalog does not know
are kept as they are. Pure: no Qt.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional, Sequence

from ..plan.satellites import platform_key
from ..plan.tree import Listed
from .route import _named, _plain


@dataclass
class Picked:
    series: list = field(default_factory=list)
    satellites: list = field(default_factory=list)
    modes: list = field(default_factory=list)


def _sat_names(l: Listed) -> tuple:
    return (l.sat.name, l.sat.id, *l.sat.aliases)


def _mode_names(l: Listed) -> tuple:
    return (l.sat.mode, l.sat.sensor, f"{l.sat.sensor or ''}{l.sat.mode or ''}")


def checks_of(listed: Sequence[Listed], series: Sequence[str], satellites: Sequence[str], modes: Sequence[str]) -> dict:
    """{key: checked} for the listed satellites (modes) a request's names take; none checked for no condition."""
    out = {}
    for l in listed:
        on = bool(series or satellites or modes)
        if satellites and not _named(satellites, *_sat_names(l)):
            on = False
        if series and not _named(series, l.sat.series):
            on = False
        if modes and not _named(modes, *_mode_names(l)):
            on = False
        out[l.key] = on
    return out


def unknown(listed: Sequence[Listed], series: Sequence[str], satellites: Sequence[str], modes: Sequence[str]) -> Picked:
    """The names no listed satellite (series, mode) answers to: kept as written."""
    return Picked(
        [n for n in series if not any(_named([n], l.sat.series) for l in listed)],
        [n for n in satellites if not any(_named([n], *_sat_names(l)) for l in listed)],
        [n for n in modes if not any(_named([n], *_mode_names(l)) for l in listed)],
    )


def _written(before: Sequence[str], names: tuple, fallback: str) -> str:
    """The name as the request had it, when one of its names still matches; else `fallback`."""
    return next((n for n in before if _named([n], *names)), fallback)


def picked(listed: Sequence[Listed], before: Optional[Picked] = None) -> Picked:
    """The request's lists for the checks in `listed` (the names of `before` kept where they still match)."""
    before = before or Picked()
    keep = unknown(listed, before.series, before.satellites, before.modes)
    chosen = [l for l in listed if l.checked]
    if not chosen:
        return keep
    platforms: dict = {}
    for l in listed:
        platforms.setdefault(platform_key(l.sat), []).append(l)
    on = [p for p, same in platforms.items() if any(m.checked for m in same)]

    # Modes: only when a satellite checked has a mode left out.
    modes: list = []
    if any(not m.checked and m.sat.mode for p in on for m in platforms[p]):
        for l in chosen:
            if l.sat.mode:
                name = _written(before.modes, _mode_names(l), l.sat.mode)
                if _plain(name) not in {_plain(m) for m in modes}:
                    modes.append(name)

    # Whole series, or the satellites by name.
    by_series: dict = {}
    for p, same in platforms.items():
        by_series.setdefault(same[0].sat.series or "", []).append(p)
    whole = all(all(q in on for q in by_series[platforms[p][0].sat.series or ""]) for p in on)
    series: list = []
    satellites: list = []
    if whole and all(platforms[p][0].sat.series for p in on):
        for name in by_series:
            if name and any(p in on for p in by_series[name]):
                series.append(_written(before.series, (name,), name))
    else:
        named = {p: _written(before.satellites, _sat_names(platforms[p][0]), platforms[p][0].sat.name) for p in on}
        # Those named before keep their order, the others follow.
        order = {_plain(n): i for i, n in enumerate(before.satellites)}
        first = sorted((p for p in on if _plain(named[p]) in order), key=lambda p: order[_plain(named[p])])
        satellites = [named[p] for p in first] + [named[p] for p in on if p not in first]
    return Picked(series + keep.series, satellites + keep.satellites, modes + keep.modes)


def summary(p: Picked) -> str:
    """「衛星シリーズ OPT・衛星 A → B・センサーモード Spot」, or 「指定なし（どの衛星でも）」."""
    parts = []
    if p.series:
        parts.append("衛星シリーズ " + "、".join(p.series))
    if p.satellites:
        parts.append("衛星 " + " → ".join(p.satellites))
    if p.modes:
        parts.append("センサーモード " + "、".join(p.modes))
    return "・".join(parts) or "指定なし（どの衛星でも）"
