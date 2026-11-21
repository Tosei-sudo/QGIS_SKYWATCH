"""
衛星名 of the images recorded on (tsudo 2026-11-20: 「外部画像の存在を考える
と、観測結果テーブルにも衛星名があってもいい」): a record and a checked area
keep the satellite (or sensor) that took their image, so the satellite is not
lost for an image outside the catalog (whose id is its file name: no catalog
row to look it up by), and the dashboards need no join.

Where it comes from, the first found:

1. an image of the catalog: the catalog's 「センサー」 (`fields.sensor`);
2. the file's metadata (`satellite_in_metadata`): GDAL's IMAGERY domain
   (SATELLITEID: Maxar・Pleiades・SPOT・Landsat…), the NITF image source
   (ISORCE), the SAFE products' spacecraft, a SICD's collector…;
3. the file's name (`SatelliteChoices.in_file_name`): a word of it that is a
   known satellite's name (the image catalogs' sensors, the satellite
   catalog, the alias table);
4. typed for the image (「衛星不明（入力）」).

A name found is written as the image catalog writes that satellite
(`SatelliteChoices.spelled`): 「WorldView-3」 read from a file whose
satellite the catalog calls 「WV03」 (the alias table says they are one) is
kept as 「WV03」, so a dashboard does not count one satellite twice.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any, Callable, Iterable, Mapping, Optional, Sequence

from ..plan.api import NO_NAMES, SatelliteNames, plain

#: Where an image's 衛星名 came from (shown with it).
ORIGINS = {
    "catalog": "カタログ",
    "metadata": "メタデータ",
    "name": "ファイル名",
    "manual": "手入力",
}
#: The metadata items that name the satellite, the surest first (compared regardless of case).
METADATA_KEYS = (
    # GDAL's IMAGERY domain (Maxar/DigitalGlobe, GeoEye, Pleiades, SPOT, Landsat, RapidEye, Kompsat, EROS, ALOS, OrbView…).
    "SATELLITEID",
    # NITF: the image's source (ISORCE: 「WV03」・「GE01」…).
    "NITF_ISORCE",
    # SAFE (Sentinel-2: 「Sentinel-2A」; Sentinel-1: MISSION_ID 「S1A」).
    "SPACECRAFT_NAME", "MISSION_ID",
    # Others' own items.
    "SATELLITE_NAME", "SATELLITE", "SATELLITE_ID", "PLATFORM", "PLATFORM_NAME", "PLATFORMNAME", "SPACECRAFT", "COLLECTOR_NAME",
    "SATELLITE_IDENTIFIER", "MISSION",
)
#: What a metadata item says when it does not know.
_UNKNOWN = {"", "unknown", "none", "null", "na", "n/a", "-", "--", "0", "unk"}
#: In a SICD's XML (a complex SAR image) or an image description (XML or JSON): the collector's / platform's name.
_XML_NAMES = re.compile(r"<\s*(?:\w+:)?(CollectorName|SatelliteName|Satellite|Platform|Mission)\s*>\s*([^<]{1,80}?)\s*<", re.IGNORECASE)
_JSON_KEYS = ("satellite_name", "satellite", "platform", "collector_name", "mission")
#: A word of a file name: letters and digits.
_WORD = re.compile(r"[A-Za-z0-9]+")
#: Names shorter than this (in letters and digits) are not looked for in file names (「S1」 would be anywhere).
MIN_NAME_LENGTH = 3
#: The most characters kept (TBL_OBSERVATION.SATELLITE is String(255)).
MAX_LENGTH = 255


def clean(name: Any) -> str:
    """A satellite name as kept: its spaces trimmed (runs of them one), at most MAX_LENGTH characters; '' for none."""
    text = re.sub(r"\s+", " ", str(name or "")).strip()
    return text[:MAX_LENGTH]


def _known(value: Any) -> str:
    text = clean(value)
    return "" if text.casefold() in _UNKNOWN else text


def _in_text(text: str) -> str:
    """A satellite named inside a longer text: a SICD's XML, an image description (JSON or XML)."""
    text = str(text or "")
    if not text.strip():
        return ""
    stripped = text.strip()
    if stripped[:1] in "{[":
        try:
            value = json.loads(stripped)
        except ValueError:
            value = None
        found = _in_json(value)
        if found:
            return found
    m = _XML_NAMES.search(text)
    return _known(m.group(2)) if m else ""


def _in_json(value: Any, depth: int = 0) -> str:
    if depth > 4:
        return ""
    if isinstance(value, dict):
        lowered = {str(k).casefold(): v for k, v in value.items()}
        for key in _JSON_KEYS:
            v = lowered.get(key)
            if isinstance(v, (str, int, float)) and not isinstance(v, bool):
                found = _known(v)
                if found:
                    return found
        for v in value.values():
            found = _in_json(v, depth + 1)
            if found:
                return found
    elif isinstance(value, list):
        for v in value[:20]:
            found = _in_json(v, depth + 1)
            if found:
                return found
    return ""


def satellite_in_metadata(items: Mapping[str, Any], texts: Iterable[str] = ()) -> str:
    """
    The satellite a file's metadata names (its items, of every domain read,
    by name; `texts`: XML or JSON it holds, a SICD's or the TIFF's image
    description): the surest item first; '' when none says.
    """
    by_key = {str(k).strip().upper(): v for k, v in (items or {}).items()}
    for key in METADATA_KEYS:
        found = _known(by_key.get(key))
        if found:
            return found
    for text in [*texts, by_key.get("TIFFTAG_IMAGEDESCRIPTION", "")]:
        found = _in_text(text)
        if found:
            return found
    return ""


@dataclass(frozen=True)
class SatelliteChoices:
    """
    The satellite names known: the image catalogs' sensors (as the records
    write them), the satellite catalog's names, and which names are one
    satellite's (the alias table).
    """
    #: The values of the image catalogs' 「センサー」, as written there.
    catalog: tuple[str, ...] = ()
    #: The satellite catalog's names (TBL_SATELLITE.SAT_NAME).
    satellites: tuple[str, ...] = ()
    names: SatelliteNames = field(default_factory=lambda: NO_NAMES)

    def __bool__(self) -> bool:
        return bool(self.catalog or self.satellites or self.names)

    def offered(self) -> list[str]:
        """The names to choose from: the catalogs' first, then the satellites none of them names."""
        out: list[str] = []
        for name in [*self.catalog, *self.satellites]:
            text = clean(name)
            if text and not any(self._one(text, o) for o in out):
                out.append(text)
        return out

    def _one(self, a: str, b: str) -> bool:
        return plain(a) == plain(b) or self.names.same(a, b)

    def spelled(self, name: Any) -> str:
        """A name as the image catalog writes that satellite (the same letters and digits first, then an alias), else as given."""
        text = clean(name)
        if not text:
            return ""
        key = plain(text)
        same = next((c for c in self.catalog if plain(c) == key), None)
        if same is None:
            same = next((c for c in self.catalog if self.names.same(text, c)), None)
        if same is None:
            same = next((s for s in self.satellites if plain(s) == key), None)
        return clean(same) if same else text

    def in_file_name(self, file_name: str) -> str:
        """
        A known satellite named by a word of a file's name (or two words
        together: 「WorldView-3」), as the catalog writes it; '' when none is.
        Only whole words count, and names of at least MIN_NAME_LENGTH letters and digits.
        """
        stem = re.split(r"[?#]", str(file_name or ""))[0]
        stem = re.sub(r"\.[A-Za-z0-9]{1,5}$", "", stem.replace("\\", "/").rsplit("/", 1)[-1])
        spans = [(m.start(), m.end()) for m in _WORD.finditer(stem)]
        words = [stem[a:b] for a, b in spans]
        if not words:
            return ""
        known: dict[str, str] = {}
        for name in [*self.catalog, *self.satellites]:
            key = plain(name)
            if len(key) >= MIN_NAME_LENGTH and key not in known:
                known[key] = clean(name)
        groups = list(self.names.groups)
        for i in range(len(words)):
            # Words together first (「WorldView」「3」), then one.
            for n in (3, 2, 1):
                if i + n > len(words):
                    continue
                key = plain("".join(words[i:i + n]))
                if len(key) < MIN_NAME_LENGTH or key.isdigit():
                    continue
                if key in known:
                    return self.spelled(known[key])
                # Only a name of one satellite (「SkySat」 may be an alias of SkySat-C12 and of SkySat-C13).
                found = [g for g in groups if key in g]
                group = found[0] if len(found) == 1 else None
                if group is not None:
                    # One of the alias table's names: the catalog's spelling of it, else the satellite catalog's, else as the file writes it.
                    named = next((known[k] for k in known if k in group), None)
                    return self.spelled(named or stem[spans[i][0]:spans[i + n - 1][1]])
        return ""


NO_CHOICES = SatelliteChoices()


def load_satellite_choices(client: Any, image_catalogs: Sequence[Any], satellite_catalogs: Sequence[Any], names: SatelliteNames,
                           token_for: Callable[[str, Optional[str]], Optional[str]],
                           cancelled: Optional[Callable[[], bool]] = None) -> tuple[SatelliteChoices, bool]:
    """
    The names known (in a task): the image catalogs' sensors and the
    satellite catalogs' names (their distinct values), with the alias table's
    `names`; and whether every table was read (else asked again next time).
    """
    from ..catalog.api import sensors_of
    from ..esri import EsriError
    from ..util import js_string

    catalog: list[str] = []
    satellites: list[str] = []
    whole = True
    for c in image_catalogs:
        if cancelled is not None and cancelled():
            return SatelliteChoices(tuple(catalog), tuple(satellites), names), False
        if not c.fields.get("sensor"):
            continue
        for v in sensors_of(client, c, token_for(c.url, c.token)):
            if clean(v) and clean(v) not in catalog:
                catalog.append(clean(v))
    for c in satellite_catalogs:
        if cancelled is not None and cancelled():
            return SatelliteChoices(tuple(catalog), tuple(satellites), names), False
        name = c.fields.get("name")
        if not name:
            continue
        try:
            answer = client.ask(f"{c.url}/query", {
                "where": c.where or "1=1", "outFields": name, "returnDistinctValues": "true", "returnGeometry": "false",
            }, token_for(c.url, c.token))
        except (EsriError, OSError, ValueError):
            whole = False
            continue
        for f in answer.get("features") or []:
            v = (f.get("attributes") or {}).get(name)
            text = clean(js_string(v)) if v is not None else ""
            if text and text not in satellites:
                satellites.append(text)
    return SatelliteChoices(tuple(catalog), tuple(sorted(satellites, key=str.casefold)), names), whole
