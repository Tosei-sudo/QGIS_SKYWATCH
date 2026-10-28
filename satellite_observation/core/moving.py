"""
Moving objects (ships by AIS, aircraft by ADS-B), as the viewer's moving.ts:
what the feeds send, read into positions, and kept per object as a track.

- Messages: a JSON object, a list of them, GeoJSON (Feature /
  FeatureCollection, points), Esri features (`attributes` + `geometry`, as
  ArcGIS Stream Services send them), lists under a key (`features`, `data`,
  `vessels`, `aircraft`, `ac`…) and OpenSky's `states` arrays. Nested objects
  (aisstream.io's `MetaData` / `Message`) are flattened to dotted keys.
- Fields by role (id, time, lat, lon, heading, course, speed, name, kind):
  found by their usual names (mmsi, icao24, lat, longitude, cog, sog,
  shipname, timestamp…), or as set by hand / in the settings file.
- Tracks: each object's fixes in time order, with caps on the objects and
  fixes kept; positions at any time are interpolated between fixes (for the
  temporal controller, and for checking detections against them).

Times are ms since 1970 (UTC).
"""
from __future__ import annotations

import bisect
import json
import math
import re
from dataclasses import dataclass, field
from typing import Any, Iterable, Optional
from urllib.parse import urljoin, urlsplit

from .geocode import decode_csv, parse_csv
from .obs.image_time import parse_time

ROLES = {
    "id": "ID（MMSI・ICAO）",
    "time": "時刻",
    "lat": "緯度",
    "lon": "経度",
    "heading": "船首方位・機首方位",
    "course": "針路（COG）",
    "speed": "速度",
    "name": "名前",
    "kind": "種別",
}
KINDS = {"ship": "船舶", "aircraft": "航空機"}
FEED_TYPES = {
    "websocket": "WebSocket",
    "sse": "Server-Sent Events",
    "poll": "ポーリング（GeoJSON・JSON）",
    "esri-stream": "ArcGIS Stream Service",
}

#: Records read from one message at most.
MAX_RECORDS_PER_MESSAGE = 50_000
MAX_ATTRS = 40
MAX_TEXT = 200
MAX_KEY = 64
#: Rows read from a history file at most.
MAX_HISTORY_ROWS = 2_000_000


@dataclass
class FeedConfig:
    """One feed of the settings file's `movingFeeds`, or added by URL."""
    label: str
    type: str
    url: str
    #: Seconds between requests, for `poll`.
    interval: Optional[float] = None
    #: Fields by role, over what is found by name.
    fields: dict = field(default_factory=dict)
    kind: Optional[str] = None


@dataclass
class MovingRecord:
    """One position report, read."""
    id: str
    t: float
    lon: float
    lat: float
    heading: Optional[float] = None
    course: Optional[float] = None
    speed: Optional[float] = None
    name: Optional[str] = None
    kind: Optional[str] = None
    attrs: dict = field(default_factory=dict)


# ---- reading messages ----

LIST_KEYS = ["features", "data", "items", "records", "vessels", "ships", "aircraft", "ac", "results", "positions", "rows", "list"]
OPENSKY = ["icao24", "callsign", "origin_country", "time_position", "last_contact", "longitude", "latitude", "baro_altitude", "on_ground",
           "velocity", "true_track", "vertical_rate", "sensors", "geo_altitude", "squawk", "spi", "position_source"]


def _is_number(v: Any) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool)


def _mercator_to_lon_lat(x: float, y: float) -> tuple[float, float]:
    r = 6378137
    return math.degrees(x / r), math.degrees(2 * math.atan(math.exp(y / r)) - math.pi / 2)


def _point_of(geometry: Any) -> Optional[tuple[float, float]]:
    """A point's longitude / latitude from an Esri geometry (`x`, `y`, degrees or Web Mercator) or GeoJSON coordinates."""
    if not isinstance(geometry, dict):
        return None
    x = y = None
    if _is_number(geometry.get("x")) and _is_number(geometry.get("y")):
        x, y = geometry["x"], geometry["y"]
    elif isinstance(geometry.get("coordinates"), list):
        c = geometry["coordinates"]
        # MultiPoint and lines: the first point.
        while isinstance(c, list) and c and isinstance(c[0], list):
            c = c[0]
        if isinstance(c, list) and len(c) >= 2:
            x, y = c[0], c[1]
    if not _is_number(x) or not _is_number(y) or not math.isfinite(x) or not math.isfinite(y):
        return None
    sr = geometry.get("spatialReference")
    wkid = sr.get("wkid") if isinstance(sr, dict) else None
    if wkid in (102100, 3857, 102113) or abs(x) > 180 or abs(y) > 90:
        return _mercator_to_lon_lat(x, y)
    return float(x), float(y)


def flatten(value: dict, into: Optional[dict] = None, prefix: str = "", depth: int = 0) -> dict:
    """`value` flattened: nested objects as dotted keys (three levels), lists left out."""
    into = {} if into is None else into
    for key, v in value.items():
        name = f"{prefix}.{key}" if prefix else str(key)
        if v is None or isinstance(v, bool):
            into[name] = v
        elif _is_number(v):
            into[name] = v if math.isfinite(v) else None
        elif isinstance(v, str):
            into[name] = v[:MAX_TEXT]
        elif isinstance(v, dict) and depth < 3:
            flatten(v, into, name, depth + 1)
    return into


def records_of(message: Any, maximum: int = MAX_RECORDS_PER_MESSAGE) -> list[dict]:
    """The objects in a message, flattened. A point geometry (GeoJSON, Esri) gives `_lon` and `_lat`."""
    out: list[dict] = []

    def one(item: Any) -> None:
        if len(out) >= maximum or not isinstance(item, dict):
            return
        props = item.get("properties") if isinstance(item.get("properties"), dict) else item.get("attributes") if isinstance(item.get("attributes"), dict) else None
        if props is not None and (item.get("type") == "Feature" or "geometry" in item or "attributes" in item):
            flat = flatten(props)
            at = _point_of(item.get("geometry"))
            if at:
                flat["_lon"], flat["_lat"] = at
            if "id" in item and "id" not in flat and (isinstance(item["id"], str) or _is_number(item["id"])):
                flat["id"] = item["id"]
            out.append(flat)
        else:
            out.append(flatten(item))

    def many(items: list) -> None:
        for item in items:
            if len(out) >= maximum:
                break
            one(item)

    if isinstance(message, list):
        many(message)
    elif isinstance(message, dict):
        states = message.get("states")
        if message.get("type") == "FeatureCollection" and isinstance(message.get("features"), list):
            many(message["features"])
        elif isinstance(states, list) and all(isinstance(s, list) for s in states):
            # OpenSky: [icao24, callsign, …] per aircraft.
            for s in states:
                if len(out) >= maximum:
                    break
                o = {k: (s[i].strip() if isinstance(s[i], str) else s[i]) for i, k in enumerate(OPENSKY) if i < len(s)}
                out.append(flatten(o))
        else:
            key = next((k for k in LIST_KEYS if isinstance(message.get(k), list) and any(isinstance(v, dict) for v in message[k])), None)
            if key:
                many(message[key])
            else:
                one(message)
    return out


# ---- fields by role ----

NAMES = {
    "id": ["mmsi", "icao24", "icao", "hex", "userid", "trackid", "vesselid", "imo", "id", "uid", "callsign", "flight"],
    "time": ["timestamp", "timeutc", "time", "datetime", "basedatetime", "lastcontact", "timeposition", "postime", "ts", "date", "updated",
             "lastupdate", "receivedat"],
    "lat": ["lat", "latitude", "_lat"],
    "lon": ["lon", "lng", "long", "longitude", "_lon"],
    "heading": ["trueheading", "heading", "hdg", "truehdg", "navheading", "magheading"],
    "course": ["cog", "course", "truetrack", "track", "dir", "direction"],
    "speed": ["sog", "speed", "velocity", "gs", "groundspeed", "spd"],
    "name": ["shipname", "vesselname", "name", "callsign", "flight", "registration", "reg"],
    "kind": ["kind", "vesseltype", "shiptype", "aircrafttype", "category", "type", "typecode"],
}


def _norm(key: str) -> str:
    return re.sub(r"[\s_-]+", "", key[key.rfind(".") + 1:].lower()) or key


def _name_of(key: str) -> str:
    return key if key in ("_lat", "_lon") else _norm(key)


def detect_fields(keys: Iterable[str], given: Optional[dict] = None) -> dict:
    """The field of each role among `keys`, by name; `given` wins. A field serves one role only."""
    given = given or {}
    everything = list(dict.fromkeys(keys))
    used = {v for v in given.values() if v}
    found = {}
    for role in ROLES:
        if given.get(role):
            found[role] = given[role]
            continue
        for candidate in NAMES[role]:
            key = next((k for k in everything if k not in used and _name_of(k) == candidate), None)
            if key:
                found[role] = key
                used.add(key)
                break
    return found


def can_place(mapping: dict) -> bool:
    """Whether a field map can place objects: an id, a latitude and a longitude."""
    return bool(mapping.get("id") and mapping.get("lat") and mapping.get("lon"))


def guess_kind(keys: Iterable[str]) -> str:
    """Ships or aircraft, from the names of the fields (AIS has MMSIs, ADS-B ICAO addresses and call signs)."""
    names = {_name_of(k) for k in keys}
    ship = any(n in names for n in ("mmsi", "shipname", "vesselname", "sog", "cog", "imo", "shiptype", "vesseltype", "navstat", "navigationalstatus"))
    air = any(n in names for n in ("icao24", "icao", "hex", "squawk", "baroaltitude", "geoaltitude", "altitude", "altbaro", "flight", "onground"))
    return "aircraft" if air and not ship else "ship"


_NUMBER = re.compile(r"^[-+]?(\d+\.?\d*|\.\d+)(e[-+]?\d+)?$", re.IGNORECASE)


def number_of(v: Any) -> Optional[float]:
    """A number from a number or numeric text; None otherwise."""
    if _is_number(v):
        return float(v) if math.isfinite(v) else None
    if isinstance(v, str) and v.strip() and _NUMBER.match(v.strip()):
        return float(v)
    return None


def time_of(v: Any) -> Optional[float]:
    """
    A time from a feed: ms or s since 1970 as numbers, or text (ISO 8601 and
    the like). Text without a time zone is UTC, as AIS and ADS-B times are;
    aisstream.io's `… +0000 UTC` reads too.
    """
    n = number_of(v)
    if n is not None:
        return n if n > 1e11 else n * 1000 if n > 1e8 else None
    if not isinstance(v, str):
        return None
    text = re.sub(r"\s*UTC$", "", v.strip(), flags=re.IGNORECASE)
    if not text:
        return None
    # Nanoseconds and more: milliseconds are enough.
    trimmed = re.sub(r"([.,]\d{3})\d+", r"\1", text)
    zoned = re.search(r"(Z|[+-]\d{2}:?\d{2})$", trimmed, re.IGNORECASE) is not None
    t = None if zoned else parse_time(f"{trimmed}Z")
    if t is None:
        t = parse_time(trimmed)
    return None if t is None else float(t)


def _angle_of(v: Any) -> Optional[float]:
    """An angle in degrees 0–360 (AIS's 511 and more are "not available")."""
    n = number_of(v)
    return None if n is None or n < 0 or n > 360 else n % 360


def read_record(flat: dict, mapping: dict, now: float) -> Optional[MovingRecord]:
    """One record as a position report by `mapping`, or None without an id or a valid position; `now` is its time when it has none."""
    def get(role: str) -> Any:
        return flat.get(mapping[role]) if mapping.get(role) else None

    raw = get("id")
    ident = (str(int(raw)) if float(raw).is_integer() else str(raw)) if _is_number(raw) else raw.strip()[:MAX_KEY] if isinstance(raw, str) else ""
    if not ident:
        return None
    lat, lon = number_of(get("lat")), number_of(get("lon"))
    if lat is None or lon is None or abs(lat) > 90 or abs(lon) > 180:
        return None
    t = time_of(get("time"))
    speed = number_of(get("speed"))

    def text(v: Any) -> Optional[str]:
        if isinstance(v, str) and v.strip():
            return v.strip()[:MAX_TEXT]
        return _number_text(v) if _is_number(v) else None

    mapped = set(mapping.values())
    attrs = {}
    for k, v in flat.items():
        if k in mapped or k in ("_lon", "_lat", "geometry"):
            continue
        if len(attrs) >= MAX_ATTRS:
            break
        attrs[k[:MAX_KEY]] = v
    return MovingRecord(ident, t if t is not None else now, lon, lat, _angle_of(get("heading")), _angle_of(get("course")),
                        speed if speed is not None and 0 <= speed < 10000 else None, text(get("name")), text(get("kind")), attrs)


def _number_text(v: float) -> str:
    return str(int(v)) if float(v).is_integer() else str(v)


# ---- tracks ----

@dataclass
class Fix:
    t: float
    lon: float
    lat: float
    #: The direction drawn: the heading, else the course.
    dir: Optional[float]
    speed: Optional[float]


@dataclass
class Track:
    """An object and its fixes, oldest first."""
    id: str
    name: Optional[str] = None
    kind: Optional[str] = None
    #: The latest report's other values, and its own heading and course.
    attrs: dict = field(default_factory=dict)
    heading: Optional[float] = None
    course: Optional[float] = None
    fixes: list = field(default_factory=list)
    #: The fixes' times, kept beside them for searching.
    times: list = field(default_factory=list)


@dataclass
class TrackLimits:
    max_objects: int = 20_000
    max_fixes_per_track: int = 5_000
    max_fixes: int = 2_000_000


def fix_index(track: Track, t: float) -> int:
    """The index of the last fix at or before `t` (-1 when all are after)."""
    return bisect.bisect_right(track.times, t) - 1


class TrackStore:
    """The tracks of a feed."""

    def __init__(self, limits: Optional[TrackLimits] = None):
        self.limits = limits or TrackLimits()
        self.tracks: dict[str, Track] = {}
        self.fix_count = 0
        #: Reports left out by the caps.
        self.dropped = 0

    def add(self, r: MovingRecord) -> bool:
        """Adds a report; False when a cap left it out."""
        track = self.tracks.get(r.id)
        if track is None:
            if len(self.tracks) >= self.limits.max_objects:
                self.dropped += 1
                return False
            track = self.tracks[r.id] = Track(r.id)
        fix = Fix(r.t, r.lon, r.lat, r.heading if r.heading is not None else r.course, r.speed)
        i = fix_index(track, r.t)
        if i >= 0 and track.fixes[i].t == r.t:
            track.fixes[i] = fix
        else:
            if self.fix_count >= self.limits.max_fixes:
                self.dropped += 1
                return False
            track.fixes.insert(i + 1, fix)
            track.times.insert(i + 1, r.t)
            self.fix_count += 1
            if len(track.fixes) > self.limits.max_fixes_per_track:
                del track.fixes[0], track.times[0]
                self.fix_count -= 1
        # The latest report names the object (an older one arriving late does not).
        if track.fixes[-1] is fix:
            if r.name:
                track.name = r.name
            if r.kind:
                track.kind = r.kind
            track.attrs, track.heading, track.course = r.attrs, r.heading, r.course
        else:
            track.name = track.name if track.name is not None else r.name
            track.kind = track.kind if track.kind is not None else r.kind
        return True

    def prune(self, t: float) -> None:
        """Forgets fixes before `t`, and the objects left with none."""
        for ident in list(self.tracks):
            track = self.tracks[ident]
            i = fix_index(track, t - 1)
            if i < 0:
                continue
            del track.fixes[:i + 1], track.times[:i + 1]
            self.fix_count -= i + 1
            if not track.fixes:
                del self.tracks[ident]

    def range(self) -> Optional[tuple[float, float]]:
        """The first and last times held; None when empty."""
        ends = [(t.times[0], t.times[-1]) for t in self.tracks.values() if t.times]
        return (min(a for a, _ in ends), max(b for _, b in ends)) if ends else None

    def clear(self) -> None:
        self.tracks.clear()
        self.fix_count = 0


@dataclass
class Position:
    lon: float
    lat: float
    dir: Optional[float]
    speed: Optional[float]
    #: How long since the fix it is held at (0 between two fixes, interpolated).
    age: float
    interpolated: bool


def _near_lon(a: float, b: float) -> float:
    """The longitude `b` as close to `a` as the antimeridian allows."""
    d = b - a
    if d > 180:
        d -= 360
    elif d < -180:
        d += 360
    return a + d


def _wrap_lon(lon: float) -> float:
    return ((lon + 180) % 360 + 360) % 360 - 180


def _lerp_angle(a: Optional[float], b: Optional[float], f: float) -> Optional[float]:
    if a is None:
        return b
    if b is None:
        return a
    d = b - a
    if d > 180:
        d -= 360
    elif d < -180:
        d += 360
    return ((a + d * f) % 360 + 360) % 360


def position_at(track: Track, t: float, hold: float, max_gap: float = 6 * 3_600_000, lead: float = 0) -> Optional[Position]:
    """Where `track` is at `t`: between its fixes, interpolated; None when it is not known then."""
    fixes = track.fixes
    if not fixes:
        return None
    i = fix_index(track, t)
    if i < 0:
        first = fixes[0]
        return Position(first.lon, first.lat, first.dir, first.speed, 0, False) if first.t - t <= lead else None
    a = fixes[i]
    b = fixes[i + 1] if i + 1 < len(fixes) else None
    if a.t == t or b is None or b.t - a.t > max_gap:
        age = t - a.t
        if age > hold:
            return Position(b.lon, b.lat, b.dir, b.speed, 0, False) if b is not None and b.t - t <= lead else None
        return Position(a.lon, a.lat, a.dir, a.speed, age, False)
    f = (t - a.t) / (b.t - a.t)
    lon = _wrap_lon(a.lon + (_near_lon(a.lon, b.lon) - a.lon) * f)
    speed = b.speed if a.speed is None else a.speed if b.speed is None else a.speed + (b.speed - a.speed) * f
    return Position(lon, a.lat + (b.lat - a.lat) * f, _lerp_angle(a.dir, b.dir, f), speed, 0, True)


def trail_of(track: Track, start: float, end_time: float, end: Optional[tuple[float, float]], maximum: int = 500) -> list[tuple[float, float]]:
    """
    The track from `start` to `end_time` as longitude / latitude points, ending
    at `end` (the position drawn), at most `maximum` points (the latest).
    Longitudes run on across the antimeridian.
    """
    last = fix_index(track, end_time)
    first = max(fix_index(track, start - 1) + 1, last - maximum + 1)
    points: list[tuple[float, float]] = []
    for i in range(first, last + 1):
        f = track.fixes[i]
        points.append((_near_lon(points[-1][0], f.lon) if points else f.lon, f.lat))
    if end is not None:
        lon = _near_lon(points[-1][0], end[0]) if points else end[0]
        if not points or points[-1] != (lon, end[1]):
            points.append((lon, end[1]))
    return points


# ---- checking detections ----

def distance_metres(a: tuple[float, float], b: tuple[float, float]) -> float:
    """Metres between two longitude / latitude points (haversine on the mean Earth radius)."""
    r = 6_371_008.8
    d_lat, d_lon = math.radians(b[1] - a[1]), math.radians(b[0] - a[0])
    h = math.sin(d_lat / 2) ** 2 + math.cos(math.radians(a[1])) * math.cos(math.radians(b[1])) * math.sin(d_lon / 2) ** 2
    return 2 * r * math.asin(min(1.0, math.sqrt(h)))


@dataclass
class Match:
    id: str
    name: Optional[str]
    #: Metres from the detection to where the object was at its time.
    distance: float
    #: Seconds from the detection's time to the object's nearest fix.
    dt: float


def match_detections(points: list[tuple[float, float, Optional[float]]], tracks: Iterable[Track], window: float, distance: float) -> list[Optional[Match]]:
    """
    The nearest object to each detection (lon, lat, t), at the detection's
    time: where the object was then (interpolated, or held at a fix within
    the window), within the distance. None when none is; detections without
    a time get none.
    """
    everything = list(tracks)
    results: list[Optional[Match]] = [None] * len(points)
    by_time: dict[float, list[int]] = {}
    for i, (_, _, t) in enumerate(points):
        if t is not None and math.isfinite(t):
            by_time.setdefault(t, []).append(i)
    d_lat = math.degrees(distance / 6_371_008.8)
    for t, indices in by_time.items():
        at = []
        for track in everything:
            i = fix_index(track, t)
            before = t - track.times[i] if i >= 0 else math.inf
            after = track.times[i + 1] - t if i + 1 < len(track.times) else math.inf
            dt = min(before, after)
            if dt > window:
                continue
            p = position_at(track, t, hold=window, lead=window, max_gap=math.inf)
            if p is not None:
                at.append((p.lat, p.lon, track, dt))
        at.sort(key=lambda a: a[0])
        lats = [a[0] for a in at]
        for k in indices:
            lon, lat, _ = points[k]
            best = None
            for j in range(bisect.bisect_left(lats, lat - d_lat), len(at)):
                if at[j][0] > lat + d_lat:
                    break
                d = distance_metres((lon, lat), (at[j][1], at[j][0]))
                if d <= distance and (best is None or d < best.distance):
                    best = Match(at[j][2].id, at[j][2].name, d, at[j][3] / 1000)
            results[k] = best
    return results


# ---- feeds ----

PROTOCOLS = {"websocket": ["ws", "wss"], "sse": ["http", "https"], "poll": ["http", "https"], "esri-stream": ["http", "https"]}


def feed_type_of(url: str) -> str:
    """The kind of feed a URL looks like: ws(s) WebSocket, an ArcGIS StreamServer, an event stream, or else polled."""
    if re.match(r"^wss?:", url, re.IGNORECASE):
        return "websocket"
    path = re.split(r"[?#]", url, maxsplit=1)[0]
    if re.search(r"/StreamServer/?$", path, re.IGNORECASE):
        return "esri-stream"
    if re.search(r"(^|[/._-])(sse|events?|event-stream)([/._-]|$)", path, re.IGNORECASE):
        return "sse"
    return "poll"


def feed_url(text: str, kind: str, base: Optional[str] = None) -> str:
    """The absolute URL of a feed, or raises ValueError saying what is wrong: ws / wss for WebSocket, http / https for the rest."""
    text = text.strip()
    url = urljoin(base, text) if base else text
    parts = urlsplit(url)
    if not parts.scheme or not parts.netloc:
        raise ValueError("URL として読み込み不可")
    if parts.scheme.lower() not in PROTOCOLS[kind]:
        raise ValueError(f"{FEED_TYPES[kind]} の URL は {'・'.join(PROTOCOLS[kind])} で指定")
    if parts.username or parts.password:
        raise ValueError("URL にユーザー名・パスワードは書き込み不可")
    return url


def display_url(url: str) -> str:
    """A URL without its query and fragment (tokens)."""
    return re.split(r"[?#]", url, maxsplit=1)[0]


def moving_feed_of(value: Any, problems: list[str], index: int) -> Optional[FeedConfig]:
    """One `movingFeeds` entry of the settings file, or None (saying why) when it cannot be used."""
    at = f"movingFeeds[{index}]"
    if not isinstance(value, dict):
        problems.append(f"{at} はオブジェクトで指定")
        return None
    kind, url = value.get("type"), value.get("url")
    if not isinstance(kind, str) or kind not in FEED_TYPES:
        problems.append(f"{at} の type は {'・'.join(FEED_TYPES)} のいずれかで指定")
        return None
    relative = isinstance(url, str) and re.match(r"^\.{0,2}/", url) is not None and kind != "websocket"
    if not isinstance(url, str) or (not relative and not any(url.lower().startswith(f"{p}://") for p in PROTOCOLS[kind])):
        problems.append(f"{at} の url は {'・'.join(f'{p}://' for p in PROTOCOLS[kind])} で始まる URL で指定")
        return None
    label = value.get("label")
    feed = FeedConfig(label.strip() if isinstance(label, str) and label.strip() else f"フィード {index + 1}", kind, url)
    interval = value.get("interval")
    if interval is not None:
        if _is_number(interval) and interval >= 1:
            feed.interval = interval
        else:
            problems.append(f"{at} の interval は 1 以上の秒数で指定")
    fields = value.get("fields")
    if fields is not None:
        if not isinstance(fields, dict):
            problems.append(f"{at} の fields は役割ごとの属性名のオブジェクトで指定")
        else:
            for role, name in fields.items():
                if role not in ROLES:
                    problems.append(f"{at} の fields.{role} は使用不可の役割（{'・'.join(ROLES)}）")
                elif isinstance(name, str) and name.strip():
                    feed.fields[role] = name.strip()
                else:
                    problems.append(f"{at} の fields.{role} は属性名で指定")
    if "kind" in value:
        if value["kind"] in KINDS:
            feed.kind = value["kind"]
        else:
            problems.append(f"{at} の kind は ship か aircraft で指定")
    return feed


def moving_feeds_of(value: Any, problems: list[str]) -> list[FeedConfig]:
    if not isinstance(value, list):
        problems.append("movingFeeds は配列で指定")
        return []
    return [f for f in (moving_feed_of(v, problems, i) for i, v in enumerate(value)) if f is not None]


def utc_text(t: float) -> str:
    """`2026-10-17 09:30:00 UTC`"""
    from datetime import datetime, timezone

    return datetime.fromtimestamp(t / 1000, timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")


def history_rows(data: bytes, name: str) -> list[dict]:
    """
    The reports in a history file: a CSV / TSV (a header row; a time column
    and positions, such as MarineCadastre's AIS or OpenSky's state vectors),
    or GeoJSON / JSON as a feed would send it. Raises ValueError when neither.
    """
    if re.search(r"\.(csv|tsv|txt)$", name, re.IGNORECASE):
        rows = parse_csv(decode_csv(data))
        header = [h.strip()[:MAX_KEY] or f"列{i + 1}" for i, h in enumerate(rows.pop(0) if rows else [])]
        out = []
        for row in rows:
            if len(out) >= MAX_HISTORY_ROWS:
                break
            if len(row) == 1 and not row[0].strip():
                continue
            o = {}
            for i, h in enumerate(header):
                v = row[i].strip() if i < len(row) else ""
                o[h] = None if v == "" else v[:MAX_TEXT]
            out.append(o)
        return out
    try:
        value = json.loads(data.decode("utf-8-sig"))
    except (UnicodeDecodeError, ValueError):
        raise ValueError("CSV か GeoJSON・JSON として読み込み不可")
    return records_of(value, MAX_HISTORY_ROWS)
