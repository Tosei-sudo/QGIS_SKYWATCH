"""
Satellite catalogs (`satelliteCatalogs`) and ground stations
(`groundStations`) of the settings file, as the viewer reads them
(imaging-plan.ts: satelliteCatalogOf, groundStationsOf, lookSideOf).
"""
from __future__ import annotations

import math
import re
from dataclasses import dataclass, field
from typing import Any, Optional

from ..util import is_name, is_number, is_record, is_url, layer_url

#: Roles of the satellite catalog's attributes.
SATELLITE_ROLES = (
    "name", "id", "tle", "tle1", "tle2", "maxOffNadir", "minOffNadir", "swath", "length", "lookSide", "kind",
    "scenesPerPass", "minInterval", "maxPerDay", "series", "leadTime", "sensor", "mode", "resolution",
)
DEFAULT_KEYS = ("maxOffNadir", "minOffNadir", "swath", "scenesPerPass", "minInterval", "maxPerDay", "leadTime")

#: Specifications used when neither the catalog nor the settings give them.
BUILT_IN_DEFAULTS: dict[str, float] = {
    "maxOffNadir": 30, "minOffNadir": 0, "swath": 10, "scenesPerPass": 1, "minInterval": 0, "maxPerDay": 0, "leadTime": 0,
}
DEFAULT_SAR = re.compile(r"SAR|radar|レーダ", re.IGNORECASE)
_LAYER = re.compile(r"/(FeatureServer|MapServer)/\d+/?$", re.IGNORECASE)


@dataclass
class SatelliteCatalogConfig:
    """One entry of `satelliteCatalogs`."""
    label: str
    url: str
    fields: dict[str, str]
    swath_unit: str = "km"
    defaults: dict[str, float] = field(default_factory=lambda: dict(BUILT_IN_DEFAULTS))
    sar_pattern: re.Pattern = DEFAULT_SAR
    token: Optional[str] = None
    where: Optional[str] = None
    #: A QGIS authentication configuration id (plugin settings, not the file).
    authcfg: Optional[str] = None


def compile_pattern(value: Any, problems: list[str], at: str, fallback: re.Pattern = DEFAULT_SAR) -> re.Pattern:
    """A `sarPattern` (case-insensitive); the default when it is not one."""
    if isinstance(value, str) and value:
        try:
            return re.compile(value, re.IGNORECASE)
        except re.error:
            problems.append(f"{at} の sarPattern は正規表現として読み込み不可")
    return fallback


def satellite_catalog_of(value: Any, problems: list[str], index: int) -> Optional[SatelliteCatalogConfig]:
    """The catalog of one `satelliteCatalogs` entry; None (the reason in `problems`) when it cannot be used."""
    at = f"satelliteCatalogs[{index}]"
    if not is_record(value):
        problems.append(f"{at} はオブジェクトで指定")
        return None
    url = value.get("url")
    if not is_url(url) or not _LAYER.search(url.split("?", 1)[0]):
        problems.append(f"{at} の url は …/FeatureServer/0 のようなレイヤー（テーブル）の URL で指定")
        return None
    fields = value.get("fields")
    if not is_record(fields):
        problems.append(f"{at} に fields（属性名の対応）なし")
        return None
    mapped: dict[str, str] = {}
    for role in SATELLITE_ROLES:
        name = fields.get(role)
        if name is None or name == "":
            continue
        if is_name(name):
            mapped[role] = name
        else:
            problems.append(f"{at} の fields.{role} は属性名（英数字と _）で指定")
    if not mapped.get("tle") and not (mapped.get("tle1") and mapped.get("tle2")):
        problems.append(f"{at} に fields.tle（または tle1 と tle2）なし")
        return None

    given: dict[str, float] = {}
    defaults = value.get("defaults")
    if is_record(defaults):
        for key in DEFAULT_KEYS:
            v = defaults.get(key)
            if is_number(v) and v >= 0:
                given[key] = v
            elif key in defaults:
                problems.append(f"{at} の defaults.{key} は 0 以上の数で指定")
    label = value.get("label")
    token = value.get("token")
    where = value.get("where")
    return SatelliteCatalogConfig(
        label=label if isinstance(label, str) and label else f"衛星カタログ {index + 1}",
        url=layer_url(url),
        token=token if isinstance(token, str) and token else None,
        fields=mapped,
        where=where.strip() if isinstance(where, str) and where.strip() else None,
        swath_unit="m" if value.get("swathUnit") == "m" else "km",
        defaults={**BUILT_IN_DEFAULTS, **given},
        sar_pattern=compile_pattern(value.get("sarPattern"), problems, at),
    )


def look_side_of(value: Any) -> str:
    """`right` / `left` / `both` of a look side written as right, R, 右 …"""
    text = value.strip() if isinstance(value, str) else ""
    if re.match(r"^(r|right|右)", text, re.IGNORECASE):
        return "right"
    if re.match(r"^(l|left|左)", text, re.IGNORECASE):
        return "left"
    return "both"


@dataclass
class GroundStation:
    """A ground station: where satellites send their pictures down."""
    name: str
    lon: float
    lat: float
    #: The lowest elevation (degrees) at which it can receive.
    min_elevation: float = 5


def ground_stations_of(value: Any, problems: list[str]) -> list[GroundStation]:
    """The stations of `groundStations`, each checked on its own."""
    if not isinstance(value, list):
        problems.append("groundStations は配列で指定")
        return []
    out: list[GroundStation] = []
    for i, v in enumerate(value):
        at = f"groundStations[{i}]"
        if not is_record(v):
            problems.append(f"{at} はオブジェクトで指定")
            continue
        lon, lat, minimum = v.get("lon"), v.get("lat"), v.get("minElevation")
        if not is_number(lon) or not is_number(lat) or abs(lat) > 90 or abs(lon) > 180:
            problems.append(f"{at} の lon・lat は経度・緯度の数で指定")
            continue
        low = minimum if is_number(minimum) and 0 <= minimum < 90 else 5
        if "minElevation" in v and low != minimum:
            problems.append(f"{at} の minElevation は 0〜90 の数で指定")
        name = v.get("name")
        out.append(GroundStation(name if isinstance(name, str) and name else f"地上局 {i + 1}", lon, lat, low))
    return out


# --- Satellites ready for planning ----------------------------------------------------


@dataclass
class SatelliteSpec:
    """One satellite (or one mode of it), ready for planning. Sizes in km, angles in degrees."""
    name: str
    id: str
    tle1: str
    tle2: str
    maxOffNadir: float
    minOffNadir: float
    swath: float
    length: float
    scenesPerPass: int
    minInterval: float
    maxPerDay: int
    leadTime: float
    series: str
    lookSide: str
    sar: bool
    #: Where it came from (the catalog's label, 「貼り付け」 or 「サンプル」).
    source: str
    sensor: Optional[str] = None
    mode: Optional[str] = None
    resolution: Optional[float] = None
    #: Whether this is the satellite's usual mode (None for a satellite with one way of imaging).
    primary: Optional[bool] = None


def platform_key(sat: SatelliteSpec) -> str:
    """The satellite (not the mode) an entry belongs to: its modes share intervals and the daily limit."""
    return f"{sat.source}\n{sat.id}"


def satellite_label(sat: SatelliteSpec) -> str:
    """「ICEYE-X27（スポットライト）」."""
    return f"{sat.name}（{sat.mode}）" if sat.mode else sat.name


def series_of(name: str) -> str:
    """The series a name suggests: the name without its number at the end (`SkySat-C12` → `SkySat`)."""
    base = re.sub(r"\s*\(.*\)\s*$", "", name)
    base = re.sub(r"[\s\-_]*[A-Za-z]?\d+[A-Za-z]?$", "", base)
    base = re.sub(r"[\s\-_]+$", "", base).strip()
    return base or name.strip() or "—"


def tle_lines(text: str) -> Optional[dict]:
    """The two TLE lines (and a name line before them, when there is one) in a text."""
    lines = [l.strip() for l in re.split(r"\r?\n", text) if l.strip()]
    for i, l in enumerate(lines):
        if l.startswith("1 ") and i + 1 < len(lines) and lines[i + 1].startswith("2 "):
            name = re.sub(r"^0 ", "", lines[i - 1]) if i > 0 else ""
            return {"name": name, "tle1": l, "tle2": lines[i + 1]}
    return None


def _number(value: Any) -> Optional[float]:
    """`Number(value)` when finite; None for empty values."""
    if value is None or value == "" or isinstance(value, (list, dict)):
        return None
    if isinstance(value, bool):
        return 1.0 if value else 0.0
    try:
        n = float(value.strip() or "0") if isinstance(value, str) else float(value)
    except (TypeError, ValueError):
        return None
    return n if math.isfinite(n) else None


def _js_round(x: float) -> int:
    """`Math.round` (halves up)."""
    return math.floor(x + 0.5)


def _text(value: Any) -> str:
    from ..util import js_string

    return "" if value is None else js_string(value).strip()


def satellite_of(attributes: dict, catalog: SatelliteCatalogConfig):
    """The satellite of a catalog record's attributes; a problem (text) when its TLE cannot be read."""
    fields, defaults = catalog.fields, catalog.defaults

    def get(role: str):
        name = fields.get(role)
        return attributes.get(name) if name else None

    from ..util import js_string

    if fields.get("tle"):
        text = js_string(get("tle"))
    else:
        text = f"{js_string(get('tle1'))}\n{js_string(get('tle2'))}"
    tle = tle_lines(text)
    raw_name = get("name")
    name = (js_string(raw_name) if raw_name is not None else (tle["name"] if tle else "")).strip()
    if not name and tle:
        name = f"NORAD {tle['tle1'][2:7].strip()}"
    if not tle:
        return f"{name or '名前のない衛星'}: TLE なし"
    scale = 0.001 if catalog.swath_unit == "m" else 1
    width = _number(get("swath"))
    swath = (width if width is not None else defaults["swath"] / scale) * scale
    length = _number(get("length"))

    def num(role: str, fallback: float) -> float:
        v = _number(get(role))
        return v if v is not None else fallback

    kind = get("kind")
    resolution = _number(get("resolution"))
    return SatelliteSpec(
        name=name,
        id=_text(get("id")) or name,
        tle1=tle["tle1"],
        tle2=tle["tle2"],
        maxOffNadir=num("maxOffNadir", defaults["maxOffNadir"]),
        minOffNadir=num("minOffNadir", defaults["minOffNadir"]),
        swath=swath,
        length=swath if length is None else length * scale,
        scenesPerPass=max(1, _js_round(num("scenesPerPass", defaults["scenesPerPass"]))),
        minInterval=max(0, num("minInterval", defaults["minInterval"])),
        maxPerDay=max(0, _js_round(num("maxPerDay", defaults["maxPerDay"]))),
        leadTime=max(0, num("leadTime", defaults["leadTime"])),
        series=_text(get("series")) or series_of(name),
        lookSide=look_side_of(get("lookSide")),
        sar=isinstance(kind, str) and bool(catalog.sar_pattern.search(kind)),
        source=catalog.label,
        sensor=_text(get("sensor")) or None,
        mode=_text(get("mode")) or None,
        resolution=resolution,
    )


def satellites_from_text(text: str, maxOffNadir: float, minOffNadir: float, swath: float, scenesPerPass: float = 1,
                         minInterval: float = 0, maxPerDay: float = 0, leadTime: float = 0, lookSide: str = "both",
                         sar: bool = False) -> list[SatelliteSpec]:
    """Satellites from pasted TLE text (two lines each, or three with a name line), all with the same specifications."""
    lines = [l.strip() for l in re.split(r"\r?\n", text) if l.strip()]
    found: list[SatelliteSpec] = []
    i = 0
    while i < len(lines) - 1:
        if not lines[i].startswith("1 ") or not lines[i + 1].startswith("2 "):
            i += 1
            continue
        before = re.sub(r"^0 ", "", lines[i - 1]) if i > 0 and not re.match(r"^[12] ", lines[i - 1]) else ""
        name = before or f"NORAD {lines[i][2:7].strip()}"
        found.append(SatelliteSpec(
            name=name, id=lines[i][2:7].strip(), tle1=lines[i], tle2=lines[i + 1],
            maxOffNadir=maxOffNadir, minOffNadir=minOffNadir, swath=swath, length=swath,
            scenesPerPass=max(1, _js_round(scenesPerPass)), minInterval=max(0, minInterval),
            maxPerDay=max(0, _js_round(maxPerDay)), leadTime=max(0, leadTime), series=series_of(name),
            lookSide=lookSide, sar=sar, source="貼り付け",
        ))
        i += 2
    return found


#: What samples are called in `source`.
SAMPLE_SOURCE = "サンプル"


def sample_satellites() -> list[SatelliteSpec]:
    """Two made-up satellites for trying the plan without a catalog: optical (like Sentinel-2) and right-looking SAR (like Sentinel-1)."""
    base = dict(minOffNadir=0, minInterval=0, maxPerDay=0, leadTime=0, series="サンプル", source=SAMPLE_SOURCE)
    return [
        SatelliteSpec(**{**base, "name": "サンプル光学衛星", "id": "SAMPLE-OPT",
                         "tle1": "1 90001U 26901A   26280.50000000  .00000100  00000-0  40000-4 0  9990",
                         "tle2": "2 90001  98.5700 350.5000 0001100  90.0000 270.0000 14.30820000    13",
                         "maxOffNadir": 20, "swath": 30, "length": 30, "scenesPerPass": 1, "lookSide": "both", "sar": False}),
        SatelliteSpec(**{**base, "name": "サンプルSAR衛星", "id": "SAMPLE-SAR",
                         "tle1": "1 90002U 26902A   26280.50000000  .00000100  00000-0  40000-4 0  9992",
                         "tle2": "2 90002  98.1800 283.0000 0001300  90.0000 270.0000 14.59200000    16",
                         "maxOffNadir": 45, "minOffNadir": 20, "swath": 50, "length": 50, "scenesPerPass": 1, "lookSide": "right", "sar": True}),
    ]


def side_for(sat: SatelliteSpec, side: Optional[str]) -> Optional[str]:
    """The side a satellite may look to when `side` is asked for; None when none is left."""
    if not side:
        return sat.lookSide
    return side if sat.lookSide in ("both", side) else None
