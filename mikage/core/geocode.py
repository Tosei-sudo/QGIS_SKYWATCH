"""
Place names and addresses for the jump field (地名・住所検索), as the viewer
looks them up (geocode.ts). Where to ask is set in the settings file
(`geocoders`), in order, and every one is asked:

- `gsi`: 国土地理院's address search (地名・住所, Japan).
- `nominatim`: OpenStreetMap's Nominatim, or a server of one's own.
- `arcgis`: an ArcGIS geocode service (`…/GeocodeServer`).
- `features`: an Esri feature layer of named places, searched by an
  attribute (`field`), or by several (`field: ["NAME", "NAME_KANA",
  "ALIAS"]`: a place is found when any of them holds what is typed, and
  the one that matched is shown beside it), for a closed network.
- `csv`: a CSV of names with latitude and longitude, read once and
  searched here, for a closed network without any server.

Without `geocoders` (or with `[]`) nothing is looked up, so nothing typed
leaves QGIS (the viewer asks 国土地理院 when the key is missing).
"""
from __future__ import annotations

import json
import math
import re
import unicodedata
from dataclasses import dataclass, field
from typing import Any, Callable, Optional
from urllib.parse import quote

from .esri import contains
from .util import is_record, is_url

GEOCODER_TYPES = ("gsi", "nominatim", "arcgis", "features", "csv")
#: Most places shown from each geocoder.
LIMIT = 8

Extent = tuple[float, float, float, float]


@dataclass
class GeocoderConfig:
    type: str
    #: Shown beside each place it finds.
    label: str
    url: str
    #: Esri services that need one.
    token: Optional[str] = None
    #: `features`: the attribute holding the name. `csv`: the column of names (else the first that reads as one).
    field: Optional[str] = None
    #: `features`: every attribute searched, the name's first (`field` given as a list); just `field` when one.
    fields: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not self.fields and self.field:
            self.fields = (self.field,)
        elif self.fields and not self.field:
            self.field = self.fields[0]


@dataclass
class Place:
    """One place found."""
    name: str
    lon_lat: tuple[float, float]
    #: The geocoder's label.
    source: str
    #: More about it (an address, a kind), when the service says.
    detail: Optional[str] = None
    #: Its extent as (west, south, east, north), when known (a town, a building).
    extent: Optional[Extent] = None


def geocoder_of(value: Any, problems: list[str], index: int) -> Optional[GeocoderConfig]:
    """One entry of `geocoders` (config.ts `geocoderOf`)."""
    if not is_record(value):
        problems.append(f"geocoders[{index}] はオブジェクトで指定")
        return None
    type_, url, label, token, name = (value.get(k) for k in ("type", "url", "label", "token", "field"))
    if type_ not in GEOCODER_TYPES:
        problems.append(f"geocoders[{index}] の type は {'・'.join(GEOCODER_TYPES)} のいずれかで指定")
        return None
    if not is_url(url):
        problems.append(f"geocoders[{index}] に url（http(s) か相対パス）なし")
        return None
    names: tuple[str, ...] = ()
    if isinstance(name, str) and name:
        names = (name,)
    elif isinstance(name, list) and type_ == "features":
        if not name or not all(isinstance(n, str) and n for n in name):
            problems.append(f"geocoders[{index}] の field は属性名か、属性名の配列で指定")
            return None
        names = tuple(dict.fromkeys(name))  # a name twice: once
    elif name is not None and name != "":
        problems.append(f"geocoders[{index}] の field は属性名（文字）で指定"
                        + ("（複数の属性は features のみ）" if isinstance(name, list) else ""))
        return None
    if type_ == "features" and not names:
        problems.append(f"geocoders[{index}] の features には名前の属性（field）が必要")
        return None
    return GeocoderConfig(
        type=type_, url=url, label=label if isinstance(label, str) and label else str(type_),
        token=token if isinstance(token, str) and token else None,
        field=names[0] if names else None, fields=names,
    )


def _num(v: Any) -> float:
    if isinstance(v, bool):
        return math.nan
    if isinstance(v, (int, float)):
        return float(v)
    if isinstance(v, str) and v.strip():
        try:
            return float(v)
        except ValueError:
            return math.nan
    return math.nan


def _valid(lon: float, lat: float) -> bool:
    return math.isfinite(lon) and math.isfinite(lat) and abs(lat) <= 90 and abs(lon) <= 180


def _get(value: Any, *path: str) -> Any:
    for key in path:
        value = value.get(key) if isinstance(value, dict) else None
    return value


def parse_gsi(data: Any, source: str) -> list[Place]:
    """国土地理院 AddressSearch: GeoJSON-like features with a `title`."""
    if not isinstance(data, list):
        return []
    out = []
    for f in data:
        c = _get(f, "geometry", "coordinates")
        name = _get(f, "properties", "title")
        if not isinstance(c, list) or len(c) < 2 or not isinstance(name, str):
            continue
        lon, lat = _num(c[0]), _num(c[1])
        if _valid(lon, lat):
            out.append(Place(name, (lon, lat), source))
    return out


def parse_nominatim(data: Any, source: str) -> list[Place]:
    """Nominatim (`format=jsonv2`): `display_name`, `lat` / `lon` and a bounding box (south, north, west, east)."""
    if not isinstance(data, list):
        return []
    out = []
    for p in data:
        lon, lat = _num(_get(p, "lon")), _num(_get(p, "lat"))
        name = _get(p, "display_name")
        if not _valid(lon, lat) or not isinstance(name, str):
            continue
        first, *rest = name.split(", ")
        b = [_num(v) for v in p["boundingbox"]] if isinstance(p.get("boundingbox"), list) else []
        extent = (b[2], b[0], b[3], b[1]) if len(b) == 4 and all(math.isfinite(v) for v in b) else None
        detail = "、".join(str(x) for x in [p.get("type"), *rest] if x)
        out.append(Place(first, (lon, lat), source, detail, extent))
    return out


def parse_arcgis(data: Any, source: str) -> list[Place]:
    """ArcGIS `findAddressCandidates` (asked with outSR 4326): candidates with a location and an extent."""
    candidates = _get(data, "candidates")
    if not isinstance(candidates, list):
        return []
    out = []
    for c in candidates:
        lon, lat = _num(_get(c, "location", "x")), _num(_get(c, "location", "y"))
        address = _get(c, "address")
        if not _valid(lon, lat) or not isinstance(address, str):
            continue
        e = _get(c, "extent")
        box = [_num(_get(e, k)) for k in ("xmin", "ymin", "xmax", "ymax")]
        extent = tuple(box) if e and all(math.isfinite(v) for v in box) else None
        kind = _get(c, "attributes", "Type")
        out.append(Place(address, (lon, lat), source, str(kind) if kind else None, extent))
    return out


def _js_text(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    return str(value)


def _text_of(value: Any) -> Optional[str]:
    """An attribute as a name: None when empty."""
    if value is None:
        return None
    text = _js_text(value).strip()
    return text or None


def parse_features(data: Any, name_field: "str | list[str] | tuple[str, ...]", source: str,
                   query: Optional[str] = None, labels: Optional[dict[str, str]] = None) -> list[Place]:
    """
    An Esri layer's query (outSR 4326, `returnCentroid` or point geometry): each feature's name and where it is.

    With several attributes (`name_field` a list, the name's first), the name is the first one that has a
    value; when `query` matched another (a reading, an alias), that one is the place's detail, as
    "label: value" (`labels`: the layer's aliases of the attributes). The places come best match first
    (names starting with `query`, then shorter ones), and the same name at the same place only once.
    """
    features = _get(data, "features")
    if not isinstance(features, list):
        return []
    fields = [name_field] if isinstance(name_field, str) else list(name_field)
    labels = labels or {}
    q = fold_name(query) if query else ""
    ranked = []
    seen = set()
    for n, f in enumerate(features):
        values = [(k, _text_of(_get(f, "attributes", k))) for k in fields]
        values = [(k, v) for k, v in values if v is not None]
        if not values:
            continue
        name = values[0][1]
        # Which attribute holds what was typed (the service may match in its own way: then the name).
        at, (key, value) = next(((i, kv) for i, kv in enumerate(values) if q and q in fold_name(kv[1])), (0, values[0]))
        folded = fold_name(value)
        rank = (((0 if folded.startswith(q) else 1000) + len(folded)) if q else 0, at, n)
        detail = f"{labels.get(key) or key}: {value}" if at and value != name else None
        g = f.get("geometry") or {}
        at = f.get("centroid") or (g if math.isfinite(_num(g.get("x"))) else None)
        lon, lat = (_num(at.get("x")), _num(at.get("y"))) if isinstance(at, dict) else (math.nan, math.nan)
        extent = None
        # Lines and polygons without a centroid: the middle of their extent.
        coords = [c for part in (g.get("rings") or g.get("paths") or []) for c in part]
        if coords:
            xs, ys = [_num(c[0]) for c in coords], [_num(c[1]) for c in coords]
            box = (min(xs), min(ys), max(xs), max(ys))
            if all(math.isfinite(v) for v in box):
                extent = box
                if not _valid(lon, lat):
                    lon, lat = (box[0] + box[2]) / 2, (box[1] + box[3]) / 2
        if not _valid(lon, lat):
            continue
        same = (name, round(lon, 6), round(lat, 6))
        if same in seen:
            continue
        seen.add(same)
        ranked.append((rank, Place(name, (lon, lat), source, detail, extent)))
    ranked.sort(key=lambda r: r[0])
    return [p for _, p in ranked]


def fold_name(text: str) -> str:
    """Folds what people type differently: width (NFKC), case, katakana to hiragana, spaces."""
    t = unicodedata.normalize("NFKC", text).lower()
    t = re.sub("[ァ-ヶ]", lambda m: chr(ord(m[0]) - 0x60), t)
    return re.sub(r"\s+", "", t)


def kana_variants(text: str) -> list[str]:
    """`text`, and as hiragana and as katakana when they differ (a reading may be written either way)."""
    hira = re.sub("[ァ-ヶ]", lambda m: chr(ord(m[0]) - 0x60), text)
    kata = re.sub("[ぁ-ゖ]", lambda m: chr(ord(m[0]) + 0x60), text)
    return list(dict.fromkeys((text, hira, kata)))


# --- CSV (the viewer's csv.ts) -----------------------------------------------

def decode_csv(data: bytes) -> str:
    """UTF-8, else Shift_JIS (Windows-31J), as the viewer reads CSV."""
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        return data.decode("cp932", errors="replace")


def delimiter_of(text: str) -> str:
    """The delimiter of a CSV: the one of tab, comma and semicolon found most in its first line (outside quotes)."""
    first = re.sub(r'"[^"]*"', "", re.split(r"\r?\n", text, maxsplit=1)[0])
    counts = sorted(((d, first.count(d)) for d in ("\t", ",", ";")), key=lambda c: -c[1])
    return counts[0][0] if counts[0][1] > 0 else ","


def parse_csv(text: str, delimiter: Optional[str] = None) -> list[list[str]]:
    """The rows of a CSV (RFC 4180: quoted values may hold delimiters, quotes doubled, and line breaks). Blank lines are skipped."""
    delimiter = delimiter or delimiter_of(text)
    rows: list[list[str]] = []
    row: list[str] = []
    value = ""
    quoted = False
    i = 1 if text.startswith("﻿") else 0
    n = len(text)
    while i < n:
        c = text[i]
        if quoted:
            if c == '"' and i + 1 < n and text[i + 1] == '"':
                value += '"'
                i += 1
            elif c == '"':
                quoted = False
            else:
                value += c
        elif c == '"' and value == "":
            quoted = True
        elif c == delimiter:
            row.append(value)
            value = ""
        elif c in "\r\n":
            if c == "\r" and i + 1 < n and text[i + 1] == "\n":
                i += 1
            row.append(value)
            value = ""
            if len(row) > 1 or row[0] != "":
                rows.append(row)
            row = []
        else:
            value += c
        i += 1
    row.append(value)
    if len(row) > 1 or row[0] != "":
        rows.append(row)
    return rows


@dataclass
class Gazetteer:
    """A CSV gazetteer: its rows' names (folded, for searching) and places."""
    rows: list[tuple[str, Place]] = field(default_factory=list)


def read_gazetteer(text: str, source: str, name_field: Optional[str] = None) -> Gazetteer:
    """Reads a CSV of places: a name column, latitude and longitude columns (found by their names), the rest as detail."""
    table = parse_csv(text)
    head, rows = (table[0], table[1:]) if table else ([], [])

    def index(pattern: str) -> int:
        return next((i for i, h in enumerate(head) if re.match(pattern, h.strip(), re.IGNORECASE)), -1)

    lat = index(r"^(lat|latitude|緯度|y)$")
    lon = index(r"^(lon|lng|long|longitude|経度|x)$")
    name = next((i for i, h in enumerate(head) if h.strip() == name_field), -1) if name_field else index(r"^(name|名称|名前|地名|title)$")
    if name < 0:
        name = next((i for i in range(len(head)) if i not in (lat, lon)), -1)
    if lat < 0 or lon < 0 or name < 0:
        raise ValueError(f"{source}: 名前・緯度（lat）・経度（lon）の列なし")
    others = [i for i in range(len(head)) if i not in (lat, lon, name)]
    out = Gazetteer()
    for r in rows:
        cell = lambda i: r[i] if i < len(r) else None  # noqa: E731
        x, y = _num(cell(lon)), _num(cell(lat))
        label = (cell(name) or "").strip()
        if not label or not _valid(x, y):
            continue
        detail = "、".join(v.strip() for v in (cell(i) for i in others) if v and v.strip())
        out.rows.append((fold_name(label), Place(label, (x, y), source, detail or None)))
    return out


def search_gazetteer(gazetteer: Gazetteer, query: str, limit: int = LIMIT) -> list[Place]:
    """The gazetteer's places whose names hold `query`: names starting with it first, then shorter names."""
    q = fold_name(query)
    if not q:
        return []
    found = []
    for n, (key, place) in enumerate(gazetteer.rows):
        at = key.find(q)
        if at >= 0:
            found.append(((0 if at == 0 else 1000) + len(key), n, place))
    found.sort(key=lambda f: (f[0], f[1]))
    return [p for _, _, p in found[:limit]]


# --- Asking ------------------------------------------------------------------

#: GETs a URL (or reads a local file) → its bytes; raises on failure.
GetBytes = Callable[[str], bytes]
#: Asks an Esri service (url, params, token) → its JSON (f=json is added, the token sent in the body).
AskEsri = Callable[[str, dict, Optional[str]], dict]


#: What `quote` leaves as it is in a query (as the viewer's encodeURIComponent). Not inside the f-strings:
#: quotes there need Python 3.12, and QGIS 3.34 runs on 3.10 or 3.11 on some systems.
_SAFE = "!*'()"


def gsi_url(base: str, q: str) -> str:
    return f"{base}?q={quote(q, safe=_SAFE)}"


def nominatim_url(base: str, q: str) -> str:
    url = base if re.search(r"/search$", base) else f"{base}/search"
    return f"{url}?q={quote(q, safe=_SAFE)}&format=jsonv2&limit={LIMIT}&accept-language=ja"


class PlaceSearch:
    """The viewer's place search: every geocoder asked, their places in the order of the geocoders."""

    def __init__(self, geocoders: list[GeocoderConfig], get_bytes: GetBytes, ask_esri: AskEsri,
                 resolve: Callable[[str], str] = lambda url: url):
        self.geocoders = list(geocoders)
        self._get = get_bytes
        self._esri = ask_esri
        #: A geocoder's URL as asked (a relative one against the settings file's place).
        self._resolve = resolve
        self._gazetteers: dict[str, Gazetteer] = {}
        #: A `features` layer's aliases of its attributes, asked once (none when it cannot be read).
        self._aliases: dict[str, dict[str, str]] = {}

    @property
    def enabled(self) -> bool:
        return bool(self.geocoders)

    def search(self, query: str, cancelled: Callable[[], bool] = lambda: False) -> tuple[list[Place], list[str]]:
        """Places for `query`; a geocoder that fails gives none (its error is in the second list)."""
        q = query.strip()
        places: list[Place] = []
        errors: list[str] = []
        if not q:
            return places, errors
        for g in self.geocoders:
            if cancelled():
                break
            try:
                places += self._ask(g, q)[:LIMIT]
            except Exception as error:  # noqa: BLE001 (each geocoder on its own, as Promise.allSettled)
                errors.append(f"{g.label}: {error or type(error).__name__}")
        return places, errors

    def _json(self, url: str) -> Any:
        return json.loads(self._get(url).decode("utf-8-sig"))

    def _aliases_of(self, base: str, g: GeocoderConfig) -> dict[str, str]:
        """The layer's aliases of its attributes (読み, 別名), to say which one matched; the names when not readable."""
        if base not in self._aliases:
            try:
                info = self._esri(base, {}, g.token)
                fields = info.get("fields") if isinstance(info, dict) else None
                self._aliases[base] = {
                    f["name"]: f["alias"] for f in fields or []
                    if isinstance(f, dict) and isinstance(f.get("name"), str) and isinstance(f.get("alias"), str)
                    and f["alias"] and f["alias"] != f["name"]
                }
            except Exception:  # noqa: BLE001 (only the labels; the attribute names do, asked again next time)
                return {}
        return self._aliases[base]

    def _ask(self, g: GeocoderConfig, q: str) -> list[Place]:
        base = re.sub(r"/+$", "", self._resolve(g.url))
        if g.type == "gsi":
            return parse_gsi(self._json(gsi_url(base, q)), g.label)
        if g.type == "nominatim":
            return parse_nominatim(self._json(nominatim_url(base, q)), g.label)
        if g.type == "arcgis":
            params = {"SingleLine": q, "outSR": "4326", "maxLocations": str(LIMIT), "outFields": "Type"}
            return parse_arcgis(self._esri(f"{base}/findAddressCandidates", params, g.token), g.label)
        if g.type == "features":
            names = list(g.fields or (g.field or "",))
            # Several attributes (a name and its reading): kana typed either way finds the reading.
            texts = kana_variants(q) if len(names) > 1 else [q]
            where = " OR ".join(contains(name, t) for name in names for t in texts)
            params = {
                "where": where if len(names) == 1 else f"({where})",
                "outFields": ",".join(names), "returnGeometry": "true", "returnCentroid": "true", "outSR": "4326",
                # Several attributes: more asked, the best kept (ranked here).
                "resultRecordCount": str(LIMIT if len(names) == 1 else LIMIT * 3),
            }
            data = self._esri(f"{base}/query", params, g.token)
            return parse_features(data, names, g.label, q, self._aliases_of(base, g) if len(names) > 1 else None)
        # csv: read once; a failure is tried again next time.
        url = self._resolve(g.url)
        gazetteer = self._gazetteers.get(url)
        if gazetteer is None:
            gazetteer = read_gazetteer(decode_csv(self._get(url)), g.label, g.field)
            self._gazetteers[url] = gazetteer
        return search_gazetteer(gazetteer, q)
