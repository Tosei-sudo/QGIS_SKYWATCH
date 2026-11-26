"""
The sensors a catalog search asks for (tsudo 2026-11-25: 「画像検索の文から生成AIでやるけん、衛星の種別も
拾えないかな」): the センサー box takes one sensor, or several separated by 「、」 (any of them), and 文で操作's
画像検索 turns what its sentence names into the catalog's sensors: SAR or 光学, a satellite or a series by its
name, id or other name (`satelliteAliases`), or a sensor as the catalog writes it (WV03).

An image's sensor is a satellite's when it names it as 計画と実績の照合 does (names.names_satellite: 「WV03」 for
WorldView-3 by its other names, 「WorldView-3 PAN」, 「ICEYE-X27」 of the series ICEYE); failing every
satellite, a series' (「ALOS-4」 of ALOS). Its kind is that of its satellites in the satellite catalog (衛星の種別,
`satelliteKinds`: 光学・SAR・IR by default, read from their kind attribute, core/plan/kinds.py); a sensor of no
satellite there, of satellites the catalog gives no kind, or of satellites of several kinds is of no known kind
(left out of every kind, and said), unless its own name says one (TerraSAR-X: a kind's `match`).

The kinds and the words for them (2026-11-26: IR, and the kinds of the settings) are those in use (kinds()).

Pure Python: a sentence's words are read by core/text_command.py, the card and the search are the plugin's.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Optional, Sequence

from ..plan.api import NO_NAMES, SatelliteNames, kinds, names_satellite, plain

#: The kinds of old (the names of the built-in ones).
SAR, OPTICAL = "SAR", "光学"
#: Between the sensors of the センサー box (and of the card's 衛星・種別).
SEPARATOR = "、"


class _Names:
    """The names of the kinds in use (`satelliteKinds`), read each time: `"IR" in KINDS`."""

    def __iter__(self):
        return iter(kinds().names)

    def __contains__(self, value) -> bool:
        return value in kinds().names

    def __len__(self) -> int:
        return len(kinds().names)


KINDS = _Names()


def kinds_in(text: str) -> list[str]:
    """The kinds a sentence names (光学, SAR, IR …), in the order written."""
    return kinds().kinds_in(text)


def kind_word(word: str) -> Optional[str]:
    """The kind a word of the box is on its own (「SAR」「光学」「レーダー」「赤外画像」), else None (a satellite or a sensor)."""
    return kinds().kind_word(word)


def sensor_list(text: str) -> list[str]:
    """The sensors of the センサー box: one, or several separated by 「、」 (each once, blanks left out)."""
    return list(dict.fromkeys(p.strip() for p in (text or "").split(SEPARATOR) if p.strip()))


def words_of(text: str) -> list[str]:
    """The words of the card's 衛星・種別 (separated by 「、」 or commas)."""
    return list(dict.fromkeys(p.strip() for p in re.split(r"[、,，]", text or "") if p.strip()))


# ---- the satellites of a sensor, and its kind -------------------------------------------------------------

def satellite_kind(sat: Any) -> Optional[str]:
    """A satellite's kind (its name: 光学, SAR, IR …) when the catalog gives it one (or it is SAR); None when not."""
    if getattr(sat, "kind", None) or getattr(sat, "sar", False):
        return getattr(sat, "kind_name", None) or (SAR if getattr(sat, "sar", False) else OPTICAL)
    return None


def _names(sat: Any, names: SatelliteNames) -> set:
    return names.expand(sat.name, sat.id, *tuple(getattr(sat, "aliases", ()) or ()))


def satellites_of_sensor(value: str, sats: Sequence[Any], names: SatelliteNames = NO_NAMES) -> list:
    """The satellites (SatelliteSpec) a catalog's sensor names; failing every one, those of the series it names."""
    exact = [s for s in sats if names_satellite(value, _names(s, names), (s.name,))]
    if exact:
        return exact
    return [s for s in sats if getattr(s, "series", None) and names_satellite(value, {plain(s.series)}, (s.series,))]


def sensor_kind(value: str, sats: Sequence[Any], names: SatelliteNames = NO_NAMES) -> Optional[str]:
    """A catalog's sensor's kind (SAR, 光学), None when it is not known."""
    found = {satellite_kind(s) for s in satellites_of_sensor(value, sats, names)} - {None}
    if len(found) == 1:
        return found.pop()
    if not found:
        own = kinds().matched(value)
        return own.name if own is not None else None
    return None


def sensors_named(word: str, values: Sequence[str], sats: Sequence[Any] = (), names: SatelliteNames = NO_NAMES) -> list[str]:
    """
    The catalog's sensors (`values`) a word names: the sensor itself; those
    of the satellites it names (name, id, other names), else of the series it
    names; else those it names as a satellite's name would (「WorldView」:
    WorldView-2 and -3).
    """
    p = plain(word)
    if not p:
        return []
    found = {v for v in values if plain(v) == p}
    named = [s for s in sats if p in _names(s, names)]
    if not named:
        named = [s for s in sats if getattr(s, "series", None) and plain(s.series) == p]
    for s in named:
        found |= {v for v in values if names_satellite(v, _names(s, names) | {p}, (s.name, word))}
    if not found:
        found = {v for v in values if names_satellite(v, {p}, (word,))}
    return [v for v in values if v in found]


@dataclass
class SensorPick:
    """What words of the card name in a catalog."""
    #: The catalog's sensors chosen, in its order.
    sensors: list[str] = field(default_factory=list)
    #: The catalog's sensors of no known kind (left out of a kind asked for).
    unknown: list[str] = field(default_factory=list)
    #: The words that named no sensor of the catalog (a kind none of whose sensors is).
    missing: list[str] = field(default_factory=list)
    #: Whether the catalog's sensors were known (else the names are searched as written).
    listed: bool = True

    @property
    def text(self) -> str:
        """For the センサー box."""
        return SEPARATOR.join(self.sensors)


def pick_sensors(words: Sequence[str], values: Sequence[str], sats: Sequence[Any] = (),
                 names: SatelliteNames = NO_NAMES) -> SensorPick:
    """
    The catalog's sensors (`values`, those it lists) that the words name
    (kinds, satellites, series, sensors). A kind with names (「ICEYE、SAR」)
    keeps those named of that kind (or of none known). Without the catalog's
    sensors (it can not list them): the names as written, the kinds missing.
    """
    asked = [k for k in dict.fromkeys(kind_word(w) for w in words) if k]
    others = [w for w in words if not kind_word(w)]
    if not values:
        return SensorPick(list(dict.fromkeys(others)), [], asked, listed=False)
    pick = SensorPick()
    kind_of = {v: sensor_kind(v, sats, names) for v in values}
    chosen: set = set()
    for w in others:
        found = sensors_named(w, values, sats, names)
        if found:
            chosen.update(found)
        else:
            pick.missing.append(w)
    if asked and others:
        chosen = {v for v in chosen if kind_of[v] in asked or kind_of[v] is None}
    elif asked:
        chosen = {v for v in values if kind_of[v] in asked}
        pick.unknown = [v for v in values if kind_of[v] is None]
        pick.missing += [k for k in asked if not any(kind_of[v] == k for v in values)]
    pick.sensors = [v for v in values if v in chosen]
    return pick

