"""
The time zone a source's dates and times are really in, when it is not the
one the service says (settings file: `timeZone` on a source, or a
`timeZones` rule matching its URL), as the viewer reads it (time-zone.ts).

A source with a zone set has its date values read as clock times in that
zone, whatever the service declares: an Esri date (milliseconds, read by the
service as clock time in its own zone, UTC unless its
`dateFieldsTimeReference` says otherwise, or `serviceTimeZone`) and text with
or without a zone suffix (`2026-10-28T09:00:00Z` read as 09:00 in the set
zone). Edits sent back are written the same way, so the source keeps its own
clock. Sources without one are read as before.

IANA names come from Python's zoneinfo (the system's database, or the
`tzdata` package: the Windows zip carries one in ext/win64). Without either,
zones that keep one offset all year (Asia/Tokyo and the like) still read, from
a small table here, so a closed network needs nothing more.
"""
from __future__ import annotations

import math
import os
import re
import sys
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Optional, Sequence


@dataclass(frozen=True)
class TimeZone:
    """A time zone: an IANA name (`Asia/Tokyo`) or a fixed offset (`+09:00`)."""
    #: As written in the settings file, cleaned up (`Asia/Tokyo`, `UTC+09:00`).
    name: str
    #: ms the zone's clock is ahead of UTC at an instant (ms since 1970).
    offset_at: Callable[[float], float] = field(compare=False, repr=False)

    def offset(self, instant: float) -> float:
        return self.offset_at(instant) if math.isfinite(instant) else 0.0


def _fixed(minutes: int) -> TimeZone:
    sign = "-" if minutes < 0 else "+"
    a = abs(minutes)
    name = "UTC" if minutes == 0 else f"UTC{sign}{a // 60:02d}:{a % 60:02d}"
    return TimeZone(name, lambda _t, m=minutes: m * 60_000.0)


UTC = _fixed(0)

#: Zones that keep one offset all year (minutes), for a Python without a zone database.
_STEADY = {
    "Asia/Tokyo": 540, "Asia/Seoul": 540, "Asia/Pyongyang": 540, "Asia/Shanghai": 480, "Asia/Hong_Kong": 480,
    "Asia/Taipei": 480, "Asia/Singapore": 480, "Asia/Manila": 480, "Asia/Kuala_Lumpur": 480, "Asia/Bangkok": 420,
    "Asia/Ho_Chi_Minh": 420, "Asia/Jakarta": 420, "Asia/Kolkata": 330, "Asia/Dubai": 240, "Pacific/Honolulu": -600,
    "Etc/UTC": 0, "Etc/GMT": 0,
}

#: Windows zone names ArcGIS writes in `dateFieldsTimeReference`, as IANA names.
_WINDOWS = {
    "utc": "UTC",
    "coordinated universal time": "UTC",
    "gmt standard time": "Europe/London",
    "greenwich standard time": "Atlantic/Reykjavik",
    "tokyo standard time": "Asia/Tokyo",
    "korea standard time": "Asia/Seoul",
    "china standard time": "Asia/Shanghai",
    "taipei standard time": "Asia/Taipei",
    "singapore standard time": "Asia/Singapore",
    "se asia standard time": "Asia/Bangkok",
    "india standard time": "Asia/Kolkata",
    "arabian standard time": "Asia/Dubai",
    "w. europe standard time": "Europe/Berlin",
    "romance standard time": "Europe/Paris",
    "central europe standard time": "Europe/Budapest",
    "central european standard time": "Europe/Warsaw",
    "e. europe standard time": "Europe/Chisinau",
    "russian standard time": "Europe/Moscow",
    "aus eastern standard time": "Australia/Sydney",
    "new zealand standard time": "Pacific/Auckland",
    "hawaiian standard time": "Pacific/Honolulu",
    "alaskan standard time": "America/Anchorage",
    "pacific standard time": "America/Los_Angeles",
    "mountain standard time": "America/Denver",
    "central standard time": "America/Chicago",
    "eastern standard time": "America/New_York",
}


def _windows_tzdata() -> None:
    """The Windows zip's tzdata (ext/win64, put there by scripts/build_zip.py --win64) on the path, when there is one."""
    folder = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "ext", "win64")
    if os.path.isdir(os.path.join(folder, "tzdata")) and folder not in sys.path:
        sys.path.append(folder)


_iana_cache: dict[str, Optional[TimeZone]] = {}


def _iana(name: str) -> Optional[TimeZone]:
    if name in _iana_cache:
        return _iana_cache[name]
    zone: Optional[TimeZone] = None
    try:
        _windows_tzdata()
        from zoneinfo import ZoneInfo

        info = ZoneInfo(name)

        def offset(instant: float, info=info) -> float:
            try:
                at = datetime.fromtimestamp(math.floor(instant / 1000), info)
            except (OverflowError, OSError, ValueError):
                return 0.0
            delta = at.utcoffset() or timedelta(0)
            return delta.total_seconds() * 1000

        zone = TimeZone(str(info.key), offset)
    except Exception:  # noqa: BLE001 (no database, or not a zone: ZoneInfoNotFoundError, ValueError …)
        steady = next((m for k, m in _STEADY.items() if k.lower() == name.lower()), None)
        if steady is not None:
            key = next(k for k in _STEADY if k.lower() == name.lower())
            zone = TimeZone(key, lambda _t, m=steady: m * 60_000.0)
    _iana_cache[name] = zone
    return zone


_OFFSET = re.compile(r"^(?:utc|gmt)?\s*([+-])(\d{1,2})(?::?(\d{2}))?$", re.IGNORECASE)
_IANA_NAME = re.compile(r"^[A-Za-z][\w+-]*(/[\w+-]+)*$")


def parse_time_zone(value: Any) -> Optional[TimeZone]:
    """
    A zone from the settings file or a service: an IANA name (`Asia/Tokyo`,
    `UTC`), an offset (`+09:00`, `+0900`, `+9`, `UTC+9`, `GMT-05:30`) or a
    Windows name (`Tokyo Standard Time`); None when it is none of these.
    """
    if not isinstance(value, str):
        return None
    text = value.strip()
    if not text:
        return None
    if re.fullmatch(r"utc|gmt|z", text, re.IGNORECASE):
        return UTC
    m = _OFFSET.match(text)
    if m:
        minutes = int(m.group(2)) * 60 + int(m.group(3) or 0)
        if minutes > 14 * 60:
            return None
        return _fixed(-minutes if m.group(1) == "-" else minutes)
    windows = _WINDOWS.get(text.lower())
    if windows:
        return UTC if windows == "UTC" else _iana(windows)
    return _iana(text) if _IANA_NAME.match(text) else None


def instant_of(clock: float, zone: TimeZone) -> float:
    """The instant a clock time in `zone` stands for (`clock`: the clock's fields as if UTC, ms)."""
    # The offset at the guess, then again at the result: right across daylight saving changes.
    first = clock - zone.offset(clock)
    second = clock - zone.offset(first)
    return first if second == first else clock - zone.offset(second)


def clock_of(instant: float, zone: TimeZone) -> float:
    """The clock time in `zone` at `instant` (its fields as if UTC, ms)."""
    return instant + zone.offset(instant)


@dataclass(frozen=True)
class TimeShift:
    """How a source's times are read: the zone its values really are in, and the one the service reads them in."""
    data: TimeZone
    #: The zone the service reads its stored clock times in (Esri dates; UTC by default).
    service: TimeZone = UTC


def read_instant(raw: float, shift: TimeShift) -> float:
    """A value from the service (ms) as the instant it stands for."""
    if not isinstance(raw, (int, float)) or not math.isfinite(raw):
        return raw
    return instant_of(clock_of(raw, shift.service), shift.data)


def write_instant(instant: float, shift: TimeShift) -> float:
    """An instant as the value the service keeps (ms)."""
    if not isinstance(instant, (int, float)) or not math.isfinite(instant):
        return instant
    return instant_of(clock_of(instant, shift.data), shift.service)


#: Date and time as text (ISO 8601, `T` or a space, a zone suffix or none), as sources write them.
_DATE_TIME = re.compile(r"^(\d{4})-(\d{2})-(\d{2})([T ])(\d{2}):(\d{2})(?::(\d{2})(?:\.(\d{1,9}))?)?\s*(Z|z|[+-]\d{2}(?::?\d{2})?)?$")
_EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)


def _clock_ms(y: int, mo: int, d: int, h: int, mi: int, s: int, ms: int) -> Optional[float]:
    try:
        return (datetime(y, mo, d, h, mi, s, tzinfo=timezone.utc) - _EPOCH) / timedelta(milliseconds=1) + ms
    except ValueError:
        return None


def is_date_time_text(value: Any) -> bool:
    """Whether `value` is date-time text that read_text_instant reads."""
    return isinstance(value, str) and _DATE_TIME.match(value.strip()) is not None


def read_text_instant(text: str, zone: TimeZone) -> Optional[float]:
    """The instant a date-time text stands for, read as clock time in `zone` (its own suffix ignored); None when it is not one."""
    m = _DATE_TIME.match(text.strip()) if isinstance(text, str) else None
    if not m:
        return None
    y, mo, d, _sep, h, mi, s, f, _end = m.groups()
    clock = _clock_ms(int(y), int(mo), int(d), int(h), int(mi), int(s or 0), round(float(f"0.{f}") * 1000) if f else 0)
    return None if clock is None else instant_of(clock, zone)


def _clock_text(clock: float, separator: str = "T") -> str:
    """Clock fields (as if UTC) as `yyyy-mm-ddThh:mm:ss(.sss)`."""
    d = _EPOCH + timedelta(milliseconds=clock)
    ms = d.microsecond // 1000
    return d.strftime(f"%Y-%m-%d{separator}%H:%M:%S") + (f".{ms:03d}" if ms else "")


def _suffix(offset: float) -> str:
    """An offset as a suffix: `+09:00`, `Z` for none."""
    if offset == 0:
        return "Z"
    minutes = round(abs(offset) / 60_000)
    return f"{'-' if offset < 0 else '+'}{minutes // 60:02d}:{minutes % 60:02d}"


def write_text(instant: float, zone: TimeZone, like: str) -> str:
    """
    An instant written back the way the source wrote its text (`like`):
    clock time in `zone`, with the same separator and the same kind of suffix
    (`Z` stays `Z`, an offset stays one, none stays none).
    """
    m = _DATE_TIME.match(like.strip()) if isinstance(like, str) else None
    text = _clock_text(clock_of(instant, zone), m.group(4) if m else "T")
    end = m.group(9) if m else None
    if not end:
        return text
    if end in ("Z", "z"):
        return text + end
    return text + _suffix(zone.offset(instant))


#: How date inputs of OGC processes are written: with `Z`.
ZULU = "2000-01-01T00:00:00Z"


# ---- Which sources have a zone ----

@dataclass(frozen=True)
class TimeZoneRule:
    """A zone for the sources whose URL matches."""
    #: Tested against the URL (a layer's `…/FeatureServer/0`, a catalog's or a feed's URL).
    match: re.Pattern
    zone: TimeZone
    #: The zone the service reads its stored values in; None: what the service says (Esri), or UTC.
    service: Optional[TimeZone] = None
    label: str = ""


_rules: list[TimeZoneRule] = []


def set_rules(rules: Sequence[TimeZoneRule]) -> None:
    """Sets the rules (the settings file's `timeZones` and every source's own `timeZone`); the first match wins."""
    global _rules
    _rules = list(rules)


def rules() -> list[TimeZoneRule]:
    return list(_rules)


def rule_for(url: Optional[str]) -> Optional[TimeZoneRule]:
    """The rule for `url`, or None: the source is read as the service says."""
    if not url:
        return None
    return next((r for r in _rules if r.match.search(url)), None)


def zone_for(url: Optional[str]) -> Optional[TimeZone]:
    rule = rule_for(url)
    return rule.zone if rule else None


def source_rule(url: str, zone: TimeZone, service: Optional[TimeZone] = None, label: str = "") -> TimeZoneRule:
    """A rule for one source's URL and the layers under it (`…/FeatureServer` also matches `…/FeatureServer/0`)."""
    clean = re.sub(r"[?#].*$", "", url).rstrip("/")
    return TimeZoneRule(re.compile(f"^{re.escape(clean)}(?:[/?#]|$)", re.IGNORECASE), zone, service, label)


def service_zone_of(layer_info: Any, rule: TimeZoneRule) -> TimeZone:
    """The zone an Esri layer reads its dates in: the rule's `serviceTimeZone`, else its `dateFieldsTimeReference`, else UTC."""
    if rule.service:
        return rule.service
    ref = layer_info.get("dateFieldsTimeReference") if isinstance(layer_info, dict) else None
    if isinstance(ref, dict):
        return parse_time_zone(ref.get("timeZoneIANA")) or parse_time_zone(ref.get("timeZone")) or UTC
    return UTC


def shift_text(shift: TimeShift) -> str:
    """The shift as text for a layer's information: `Asia/Tokyo（サービス側: UTC）`."""
    return f"{shift.data.name}（サービス側: {shift.service.name}）"


# ---- The settings file ----

#: The settings file's entries of external sources that can say their dates' time zone (`timeZone`).
TIME_ZONE_SOURCES = (
    "layers", "imageCatalogs", "planStores", "observationStores", "detectionOutputs", "aiResultSources",
    "satelliteCatalogs", "sensorCatalogs", "movingFeeds", "stacCatalogs", "gpServices",
)


def _urls_in(value: Any, depth: int = 0, out: Optional[list[str]] = None) -> list[str]:
    """Every URL in an entry (http(s), ws(s) or relative): its own `url`, and those of the tables and layers it names."""
    out = [] if out is None else out
    if depth > 3:
        return out
    if isinstance(value, str):
        v = value.strip()
        if re.match(r"^(https?|wss?)://.", v, re.IGNORECASE) or re.match(r"^\.{0,2}/[^/]", v):
            out.append(v)
    elif isinstance(value, list):
        for v in value:
            _urls_in(v, depth + 1, out)
    elif isinstance(value, dict):
        for k, v in value.items():
            if k != "token":
                _urls_in(v, depth + 1, out)
    return out


def _zone_of(value: Any, at: str, problems: list[str]) -> Optional[TimeZone]:
    if value is None or value == "":
        return None
    zone = parse_time_zone(value)
    if not zone:
        problems.append(f"{at} の「{value}」はタイムゾーンとして読み込み不可（Asia/Tokyo・UTC・+09:00 の形で指定）")
    return zone


def time_zones_of(value: dict, problems: list[str]) -> list[TimeZoneRule]:
    """
    The time zone rules of a settings file: each source entry's `timeZone`
    (and `serviceTimeZone`) for its URLs, first, then the `timeZones` list of
    `{ match, timeZone, serviceTimeZone }`.
    """
    out: list[TimeZoneRule] = []
    for key in TIME_ZONE_SOURCES:
        entries = value.get(key)
        if not isinstance(entries, list):
            continue
        for i, entry in enumerate(entries):
            if not isinstance(entry, dict) or "timeZone" not in entry:
                continue
            at = f"{key}[{i}]"
            zone = _zone_of(entry.get("timeZone"), f"{at}.timeZone", problems)
            service = _zone_of(entry.get("serviceTimeZone"), f"{at}.serviceTimeZone", problems)
            if not zone:
                continue
            urls = list(dict.fromkeys(_urls_in(entry)))
            if not urls:
                problems.append(f"{at} の timeZone は URL のない項目のため無視")
                continue
            label = entry["label"] if isinstance(entry.get("label"), str) else ""
            out += [source_rule(u, zone, service, label) for u in urls]
    if "timeZones" in value:
        listed = value["timeZones"]
        if not isinstance(listed, list):
            problems.append("timeZones は配列で指定")
            return out
        for i, entry in enumerate(listed):
            at = f"timeZones[{i}]"
            if not isinstance(entry, dict):
                problems.append(f"{at} はオブジェクトで指定")
                continue
            if not isinstance(entry.get("match"), str) or not entry["match"]:
                problems.append(f"{at} に match（URL の正規表現）なし")
                continue
            try:
                match = re.compile(entry["match"], re.IGNORECASE)
            except re.error as error:
                problems.append(f"{at} の match は正規表現として読み込み不可（{error}）")
                continue
            zone = _zone_of(entry.get("timeZone"), f"{at}.timeZone", problems)
            service = _zone_of(entry.get("serviceTimeZone"), f"{at}.serviceTimeZone", problems)
            if entry.get("timeZone") in (None, ""):
                problems.append(f"{at} に timeZone なし")
            if not zone:
                continue
            out.append(TimeZoneRule(match, zone, service, entry["label"] if isinstance(entry.get("label"), str) else ""))
    return out


# ---- Esri requests ----

#: A layer's operations whose dates are converted: (layer URL, operation).
_OPERATION = re.compile(r"^(.*/(?:FeatureServer|MapServer)/\d+|.*/ImageServer)/(query|applyEdits|addFeatures|updateFeatures)$", re.IGNORECASE)
_TIMESTAMP = re.compile(r"\bTIMESTAMP\s+'(\d{4})-(\d{2})-(\d{2})[ T](\d{2}):(\d{2})(?::(\d{2})(?:\.(\d{1,3}))?)?'", re.IGNORECASE)


def esri_operation(url: str) -> Optional[tuple[str, str]]:
    """The layer URL and operation of a request whose dates may need converting, or None."""
    m = _OPERATION.match(re.sub(r"[?#].*$", "", url).rstrip("/"))
    return (m.group(1), m.group(2).lower()) if m else None


@dataclass
class LayerTimes:
    """How a layer's dates are converted: the shift, and its date fields."""
    shift: TimeShift
    dates: set[str]


def layer_times(layer_info: Any, rule: TimeZoneRule) -> LayerTimes:
    fields = layer_info.get("fields") if isinstance(layer_info, dict) else None
    dates = {f["name"] for f in fields or [] if isinstance(f, dict) and f.get("type") == "esriFieldTypeDate" and isinstance(f.get("name"), str)}
    return LayerTimes(TimeShift(rule.zone, service_zone_of(layer_info, rule)), dates)


def _attributes_to_service(text: Optional[str], times: LayerTimes) -> Optional[str]:
    """Date attributes of features sent (`[{ attributes }]` as JSON) as the service keeps them."""
    import json

    if not text:
        return text
    try:
        items = json.loads(text)
    except ValueError:
        return text
    if not isinstance(items, list):
        return text
    from .obs.image_time import parse_time

    for item in items:
        attributes = item.get("attributes") if isinstance(item, dict) else None
        if not isinstance(attributes, dict):
            continue
        for name in times.dates:
            v = attributes.get(name)
            if isinstance(v, (int, float)) and not isinstance(v, bool):
                attributes[name] = round(write_instant(v, times.shift))
            elif isinstance(v, str) and v.strip():
                t = parse_time(v)
                if t is not None:
                    attributes[name] = round(write_instant(t, times.shift))
    return json.dumps(items, ensure_ascii=False)


def _where_to_service(where: str, shift: TimeShift) -> str:
    """`TIMESTAMP 'yyyy-mm-dd hh:mm:ss'` (an instant, written in UTC) as the service keeps it."""
    def one(m: re.Match) -> str:
        y, mo, d, h, mi, s, f = m.groups()
        clock = _clock_ms(int(y), int(mo), int(d), int(h), int(mi), int(s or 0), round(float(f"0.{f}") * 1000) if f else 0)
        if clock is None:
            return m.group(0)
        return f"TIMESTAMP '{_clock_text(write_instant(clock, shift), ' ')}'"

    return _TIMESTAMP.sub(one, where)


def to_service(params: dict[str, str], operation: str, times: LayerTimes) -> dict[str, str]:
    """A request's parameters with their dates as the service keeps them."""
    out = dict(params)
    if out.get("where"):
        out["where"] = _where_to_service(out["where"], times.shift)
    if operation == "applyedits":
        for key in ("adds", "updates"):
            if out.get(key):
                out[key] = _attributes_to_service(out[key], times)
    elif operation in ("addfeatures", "updatefeatures") and out.get("features"):
        out["features"] = _attributes_to_service(out["features"], times)
    return out


def from_service(answer: Any, times: LayerTimes) -> None:
    """The answer's date attributes (and those its `fields` call dates) as the instants they stand for."""
    if not isinstance(answer, dict) or not isinstance(answer.get("features"), list):
        return
    dates = set(times.dates)
    for f in answer.get("fields") or []:
        if isinstance(f, dict) and f.get("type") == "esriFieldTypeDate" and isinstance(f.get("name"), str):
            dates.add(f["name"])
    if not dates:
        return
    for feature in answer["features"]:
        a = feature.get("attributes") if isinstance(feature, dict) else None
        if not isinstance(a, dict):
            continue
        for name in dates:
            v = a.get(name)
            if isinstance(v, (int, float)) and not isinstance(v, bool):
                a[name] = round(read_instant(v, times.shift))

