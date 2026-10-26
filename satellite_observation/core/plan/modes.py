"""
Imaging modes of sensors (`sensorCatalogs`), as the viewer reads them
(sensor-modes.ts). A satellite names its sensor, and is listed once per mode
of that sensor, the mode's values in place of its own.
"""
from __future__ import annotations

import math
import re
from dataclasses import dataclass, field
from typing import Any, Callable, Optional

from ..util import is_name, is_record, layer_url, text_of
from .satellites import DEFAULT_SAR, compile_pattern, look_side_of

SENSOR_ROLES = (
    "sensor", "mode", "kind", "maxOffNadir", "minOffNadir", "swath", "length", "lookSide",
    "scenesPerPass", "minInterval", "maxPerDay", "leadTime", "resolution", "primary",
)
NUMBER_ROLES = ("maxOffNadir", "minOffNadir", "swath", "length", "scenesPerPass", "minInterval", "maxPerDay", "leadTime", "resolution")
_TABLE = re.compile(r"/(FeatureServer|MapServer)/\d+/?$", re.IGNORECASE)
_YES = re.compile(r"^(1|true|yes|y|はい|既定|標準)$", re.IGNORECASE)


@dataclass
class SensorMode:
    """One imaging mode of a sensor; values left out (None) are the satellite's own."""
    sensor: str
    mode: str = ""
    sar: Optional[bool] = None
    maxOffNadir: Optional[float] = None
    minOffNadir: Optional[float] = None
    #: km.
    swath: Optional[float] = None
    #: km; the width when absent.
    length: Optional[float] = None
    lookSide: Optional[str] = None
    scenesPerPass: Optional[float] = None
    minInterval: Optional[float] = None
    maxPerDay: Optional[float] = None
    leadTime: Optional[float] = None
    resolution: Optional[float] = None
    primary: Optional[bool] = None

    def values(self) -> dict[str, Any]:
        """The values it gives (as the viewer's object, without the absent ones)."""
        return {k: v for k, v in self.__dict__.items() if v is not None}


@dataclass
class SensorCatalogConfig:
    """One entry of `sensorCatalogs`."""
    label: str
    fields: dict[str, str]
    swath_unit: str = "km"
    sar_pattern: re.Pattern = DEFAULT_SAR
    modes: list[SensorMode] = field(default_factory=list)
    url: Optional[str] = None
    token: Optional[str] = None
    where: Optional[str] = None
    authcfg: Optional[str] = None


def _number_of(value: Any) -> Optional[float]:
    if value is None or value == "" or isinstance(value, (list, dict)):
        return None
    if isinstance(value, bool):
        n = 1.0 if value else 0.0
    else:
        try:
            n = float(value.strip()) if isinstance(value, str) else float(value)
        except (TypeError, ValueError):
            return None
    return n if math.isfinite(n) and n >= 0 else None


def _yes(value: Any) -> bool:
    return value is True or (value == 1 and not isinstance(value, bool)) or bool(_YES.match(text_of(value)))


def mode_from(get: Callable[[str], Any], scale: float, sar: re.Pattern) -> Optional[SensorMode]:
    """A mode from named values: numbers checked, sizes turned to km with `scale`. None without a sensor name."""
    sensor = text_of(get("sensor"))
    if not sensor:
        return None
    mode = SensorMode(sensor=sensor, mode=text_of(get("mode")))
    kind = get("kind")
    if isinstance(kind, str) and kind.strip():
        mode.sar = bool(sar.search(kind))
    elif isinstance(kind, bool):
        mode.sar = kind
    for key in NUMBER_ROLES:
        v = _number_of(get(key))
        if v is not None:
            setattr(mode, key, v * scale if key in ("swath", "length") else v)
    side = text_of(get("lookSide"))
    if side:
        mode.lookSide = look_side_of(side)
    if _yes(get("primary")):
        mode.primary = True
    return mode


def sensor_catalog_of(value: Any, problems: list[str], index: int) -> Optional[SensorCatalogConfig]:
    """The catalog of one `sensorCatalogs` entry; None (the reason in `problems`) when it cannot be used."""
    at = f"sensorCatalogs[{index}]"
    if not is_record(value):
        problems.append(f"{at} はオブジェクトで指定")
        return None
    sar = compile_pattern(value.get("sarPattern"), problems, at)
    unit = "m" if value.get("swathUnit") == "m" else "km"
    scale = 0.001 if unit == "m" else 1
    label = value.get("label")
    label = label if isinstance(label, str) and label else f"センサーカタログ {index + 1}"

    sensors = value.get("sensors")
    if isinstance(sensors, list):
        # Written here: what a sensor says holds for all its modes unless a mode says otherwise.
        modes: list[SensorMode] = []
        for i, s in enumerate(sensors):
            if not is_record(s) or not isinstance(s.get("sensor"), str) or not s["sensor"].strip():
                problems.append(f"{at}.sensors[{i}] に sensor（センサー名）なし")
                continue
            listed = s.get("modes") if isinstance(s.get("modes"), list) and s.get("modes") else [{}]
            for j, m in enumerate(listed):
                if not is_record(m):
                    problems.append(f"{at}.sensors[{i}].modes[{j}] はオブジェクトで指定")
                    continue

                def get(role: str, s: dict = s, m: dict = m) -> Any:
                    if role == "sensor":
                        return s["sensor"]
                    if role == "mode":
                        return m["mode"] if m.get("mode") is not None else m.get("name")
                    return m[role] if m.get(role) is not None else s.get(role)

                mode = mode_from(get, scale, sar)
                if mode:
                    modes.append(mode)
        return SensorCatalogConfig(label=label, fields={}, swath_unit=unit, sar_pattern=sar, modes=modes)

    url = value.get("url")
    if not isinstance(url, str) or not _TABLE.search(url.split("?", 1)[0]):
        problems.append(f"{at} は sensors（モードの一覧）か、…/FeatureServer/1 のようなテーブルの url を記述")
        return None
    fields = value.get("fields")
    if not is_record(fields):
        problems.append(f"{at} に fields（属性名の対応）なし")
        return None
    mapped: dict[str, str] = {}
    for role in SENSOR_ROLES:
        name = fields.get(role)
        if name is None or name == "":
            continue
        if is_name(name):
            mapped[role] = name
        else:
            problems.append(f"{at} の fields.{role} は属性名（英数字と _）で指定")
    if not mapped.get("sensor"):
        problems.append(f"{at} に fields.sensor（センサー名の属性）なし")
        return None
    token = value.get("token")
    where = value.get("where")
    return SensorCatalogConfig(
        label=label, fields=mapped, swath_unit=unit, sar_pattern=sar, modes=[], url=layer_url(url),
        token=token if isinstance(token, str) and token else None,
        where=where.strip() if isinstance(where, str) and where.strip() else None,
    )


def sensor_mode_of(attributes: dict[str, Any], catalog: SensorCatalogConfig) -> Optional[SensorMode]:
    """The mode of a sensor table's record; None when it names no sensor."""

    def get(role: str) -> Any:
        name = catalog.fields.get(role)
        return attributes.get(name) if name else None

    return mode_from(get, 0.001 if catalog.swath_unit == "m" else 1, catalog.sar_pattern)


def _key(text: Optional[str]) -> str:
    return (text or "").strip().lower()


def with_modes(sats: list, modes: list[SensorMode]) -> list:
    """
    The satellites listed once per imaging mode of their sensor (its
    `sensor`, else its series, else its name), the mode's values in place of
    their own. A satellite with a `mode` already takes that mode's values.
    Of the entries of one satellite, one is `primary` (the mode marked so, or
    the first); a satellite with one entry has none.
    """
    from dataclasses import replace

    from .satellites import platform_key

    by_sensor: dict[str, list[SensorMode]] = {}
    for m in modes:
        by_sensor.setdefault(_key(m.sensor), []).append(m)

    def apply(sat, m: SensorMode):
        swath = m.swath if m.swath is not None else sat.swath
        changes = dict(
            sensor=sat.sensor if sat.sensor is not None else m.sensor,
            sar=m.sar if m.sar is not None else sat.sar,
            maxOffNadir=m.maxOffNadir if m.maxOffNadir is not None else sat.maxOffNadir,
            minOffNadir=m.minOffNadir if m.minOffNadir is not None else sat.minOffNadir,
            swath=swath,
            length=m.length if m.length is not None else (m.swath if m.swath is not None else sat.length),
            lookSide=m.lookSide if m.lookSide is not None else sat.lookSide,
            scenesPerPass=max(1, math.floor(m.scenesPerPass + 0.5)) if m.scenesPerPass is not None else sat.scenesPerPass,
            minInterval=m.minInterval if m.minInterval is not None else sat.minInterval,
            maxPerDay=math.floor(m.maxPerDay + 0.5) if m.maxPerDay is not None else sat.maxPerDay,
            leadTime=m.leadTime if m.leadTime is not None else sat.leadTime,
        )
        if m.mode:
            changes["mode"] = m.mode
        if m.resolution is not None:
            changes["resolution"] = m.resolution
        if m.primary:
            changes["primary"] = True
        return replace(sat, **changes)

    out = []
    for sat in sats:
        found = next((by_sensor[_key(k)] for k in (sat.sensor, sat.series, sat.name) if by_sensor.get(_key(k))), None)
        if not found:
            out.append(sat)
        elif sat.mode:
            same = next((m for m in found if _key(m.mode) == _key(sat.mode)), None)
            out.append(apply(sat, same) if same else replace(sat))
        else:
            out.extend(apply(sat, m) for m in found)
    groups: dict[str, list] = {}
    for sat in out:
        groups.setdefault(platform_key(sat), []).append(sat)
    for items in groups.values():
        if len(items) < 2:
            continue
        usual = next((s for s in items if s.primary), items[0])
        for s in items:
            s.primary = s is usual
    return out


def sample_sensor_modes() -> list[SensorMode]:
    """The made-up modes of the sample SAR satellite: stripmap (usual), spotlight, ScanSAR."""
    return [
        SensorMode("サンプルSAR衛星", "ストリップマップ", minOffNadir=20, maxOffNadir=45, swath=50, length=50, resolution=5, primary=True),
        SensorMode("サンプルSAR衛星", "スポットライト", minOffNadir=15, maxOffNadir=35, swath=10, length=10, resolution=1),
        SensorMode("サンプルSAR衛星", "スキャンSAR", minOffNadir=20, maxOffNadir=45, swath=250, length=250, resolution=20),
    ]
