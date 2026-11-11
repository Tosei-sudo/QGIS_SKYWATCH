"""
入力の初期値の記憶 for imaging requests: the conditions of the request last
saved from the form (category, how it is achieved, the repeat, priority,
the satellites, series, modes, systems, angles, resolution, cloud, the
requester and the 依頼グループ) become a new request's starting values. Its name, period, area,
status and note are always the new request's own.

Pure: the values are JSON (kept in QgsSettings by the dock).
"""
from __future__ import annotations

import math
from dataclasses import replace
from typing import Mapping

from .model import ACHIEVES, CATEGORIES, KINDS, LOOK_SIDES, ORBIT_DIRS, TIMES_OF_DAY, ImagingRequest, clamp_priority, repeat_of, repeat_text

#: Fields chosen from a list: the values allowed.
CHOICES = {
    "category": CATEGORIES, "achieve": ACHIEVES, "kind": KINDS, "look_side": LOOK_SIDES,
    "orbit_dir": ORBIT_DIRS, "time_of_day": TIMES_OF_DAY,
}
#: Fields that are lists of names.
LISTS = ("series", "satellites", "modes", "systems")
#: Fields that are a number or None (not given).
NUMBERS = ("off_nadir_min", "off_nadir_max", "incidence_min", "incidence_max", "resolution", "cloud_no_go")
#: Every field remembered.
FIELDS = (*CHOICES, "repeat", "priority", *LISTS, *NUMBERS, "requester", "group")


def remembered(r: ImagingRequest) -> dict:
    """What of a saved request is offered to the next new one."""
    return {f: list(getattr(r, f)) if f in LISTS else getattr(r, f) for f in FIELDS}


def _number(v) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v)


def with_remembered(r: ImagingRequest, last: Mapping) -> ImagingRequest:
    """A new request started from what was last saved (values that no longer fit are left as the request has them)."""
    changes: dict = {}
    for f, allowed in CHOICES.items():
        if last.get(f) in allowed:
            changes[f] = last[f]
    if isinstance(last.get("repeat"), str) and last["repeat"]:
        changes["repeat"] = repeat_text(repeat_of(last["repeat"]))
    if _number(last.get("priority")):
        changes["priority"] = clamp_priority(last["priority"])
    for f in LISTS:
        v = last.get(f)
        if isinstance(v, list):
            changes[f] = [str(x) for x in v if isinstance(x, (str, int, float)) and str(x).strip()]
    for f in NUMBERS:
        if f in last and (last[f] is None or _number(last[f])):
            changes[f] = None if last[f] is None else float(last[f])
    for f in ("requester", "group"):
        if isinstance(last.get(f), str):
            changes[f] = last[f]
    return replace(r, **changes)
