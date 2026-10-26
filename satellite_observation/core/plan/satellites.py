"""
Satellite catalogs (`satelliteCatalogs`) and ground stations
(`groundStations`) of the settings file, as the viewer reads them
(imaging-plan.ts: satelliteCatalogOf, groundStationsOf, lookSideOf).
"""
from __future__ import annotations

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
