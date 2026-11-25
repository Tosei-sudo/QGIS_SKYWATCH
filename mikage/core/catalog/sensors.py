"""
The sensors a catalog search asks for (tsudo 2026-11-25: 「画像検索の文から生成AIでやるけん、衛星の種別も
拾えないかな」): the センサー box takes one sensor, or several separated by 「、」 (any of them), and 文で操作's
画像検索 turns what its sentence names into the catalog's sensors: SAR or 光学, a satellite or a series by its
name, id or other name (`satelliteAliases`), or a sensor as the catalog writes it (WV03).

An image's sensor is a satellite's when it names it as 計画と実績の照合 does (names.names_satellite: 「WV03」 for
WorldView-3 by its other names, 「WorldView-3 PAN」, 「ICEYE-X27」 of the series ICEYE); failing every
satellite, a series' (「ALOS-4」 of ALOS). Its kind is that of its satellites in the satellite catalog: SAR when
their kind attribute says so (`sarPattern`), 光学 when it says anything else; a sensor of no satellite there, of
satellites the catalog gives no kind, or of satellites of both kinds is of no known kind (left out of SAR and 光学,
and said), unless its own name says SAR (TerraSAR-X).

Pure Python: a sentence's words are read by core/text_command.py, the card and the search are the plugin's.
"""
from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field
from typing import Any, Optional, Sequence

from ..plan.api import NO_NAMES, SatelliteNames, names_satellite, plain

SAR, OPTICAL = "SAR", "光学"
KINDS = (SAR, OPTICAL)
#: Between the sensors of the センサー box (and of the card's 衛星・種別).
SEPARATOR = "、"
#: A sentence's words for each kind (NFKC).
_KIND_WORDS = {
    SAR: re.compile(r"(?<![A-Za-z])SAR(?![A-Za-z])|レーダ|合成開口", re.IGNORECASE),
    OPTICAL: re.compile(r"光学|可視|(?<![A-Za-z])optical(?![A-Za-z])", re.IGNORECASE),
}
#: A word of the box that is a kind on its own (「SAR」「光学衛星」「レーダー画像」).
_KIND_WORD = {
    SAR: re.compile(r"(sar|レーダー?|合成開口(レーダー?)?)(衛星|画像|センサー?)?"),
    OPTICAL: re.compile(r"(光学|可視|optical)(衛星|画像|センサー?)?"),
}
#: A sensor whose own name says it is SAR (TerraSAR-X, a catalog's 「… SAR」).
_SAR_NAME = re.compile(r"SAR|radar|レーダ", re.IGNORECASE)


def _nfkc(text: Any) -> str:
    return unicodedata.normalize("NFKC", str(text or ""))


def kinds_in(text: str) -> list[str]:
    """The kinds a sentence names (SAR, 光学), in the order written."""
    t = _nfkc(text)
    found = []
    for kind, pattern in _KIND_WORDS.items():
        m = pattern.search(t)
        if m:
            found.append((m.start(), kind))
    return [k for _at, k in sorted(found)]


def kind_word(word: str) -> Optional[str]:
    """The kind a word of the box is on its own (「SAR」「光学」「レーダー」), else None (a satellite or a sensor)."""
    w = re.sub(r"\s+", "", _nfkc(word)).lower()
    return next((k for k, p in _KIND_WORD.items() if p.fullmatch(w)), None)


def sensor_list(text: str) -> list[str]:
    """The sensors of the センサー box: one, or several separated by 「、」 (each once, blanks left out)."""
    return list(dict.fromkeys(p.strip() for p in (text or "").split(SEPARATOR) if p.strip()))


def words_of(text: str) -> list[str]:
    """The words of the card's 衛星・種別 (separated by 「、」 or commas)."""
    return list(dict.fromkeys(p.strip() for p in re.split(r"[、,，]", text or "") if p.strip()))


# ---- the satellites of a sensor, and its kind -------------------------------------------------------------

def satellite_kind(sat: Any) -> Optional[str]:
    """A satellite's kind: SAR when the catalog says so, 光学 when it gives another kind, None when it gives none."""
    if getattr(sat, "sar", False):
        return SAR
    return OPTICAL if getattr(sat, "kind", None) else None


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
    kinds = {satellite_kind(s) for s in satellites_of_sensor(value, sats, names)} - {None}
    if len(kinds) == 1:
        return kinds.pop()
    if not kinds and _SAR_NAME.search(str(value or "")):
        return SAR
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
    kinds = [k for k in dict.fromkeys(kind_word(w) for w in words) if k]
    others = [w for w in words if not kind_word(w)]
    if not values:
        return SensorPick(list(dict.fromkeys(others)), [], kinds, listed=False)
    pick = SensorPick()
    kind_of = {v: sensor_kind(v, sats, names) for v in values}
    chosen: set = set()
    for w in others:
        found = sensors_named(w, values, sats, names)
        if found:
            chosen.update(found)
        else:
            pick.missing.append(w)
    if kinds and others:
        chosen = {v for v in chosen if kind_of[v] in kinds or kind_of[v] is None}
    elif kinds:
        chosen = {v for v in values if kind_of[v] in kinds}
        pick.unknown = [v for v in values if kind_of[v] is None]
        pick.missing += [k for k in kinds if not any(kind_of[v] == k for v in values)]
    pick.sensors = [v for v in values if v in chosen]
    return pick

