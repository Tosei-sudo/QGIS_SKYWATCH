"""
新着画像のサムネイル (tsudo, 2026-11-14): `imageCatalogs[].thumbnail`, the
address of an image's small picture written as a pattern, the image's values
put in where `{…}` stands (as the settings file's other patterns, `{now:%Y}`):

* `{id}` `{sensor}` `{angle}` `{azimuth}`: the image's roles;
  `{acquired}` `{registered}` `{systemRegistered}` with a date format
  (`{acquired:%Y%m%d}`, `iso`, `unix`, `unixms`) and a zone after `@`
  (`{acquired:%Y%m%d@Asia/Tokyo}`; UTC when not told);
* `{attr.NAME}`: any attribute of the catalog (a date format makes a date
  of it, a number format such as `05d` a number);
* the footprint's coordinates (longitude / latitude, WGS 84): `{xmin}`
  `{ymin}` `{xmax}` `{ymax}` `{bbox}` (`xmin,ymin,xmax,ymax`), the centre
  `{center.lon}` `{center.lat}` (`{lon}` `{lat}`), and the corners
  `{ul.lon}` `{ul.lat}` `{ur.…}` `{lr.…}` `{ll.…}` (upper left, upper
  right, lower right, lower left; also `{ulx}` `{uly}` …): the footprint's
  own vertex nearest each corner of the box around it (the image's real
  corner when it lies askew). A number format sets the digits
  (`{ur.lat:.6f}`), `@3857` gives Web Mercator metres (`{bbox@3857}`);
* `{width}` `{height}`: the size asked for (pixels), `{token}` the
  catalog's token (empty with an authentication configuration).

`{{` and `}}` are braces. In an http(s) address the text values are
percent-encoded (spaces, `&`, `#` …; `/` and `:` kept); a pattern that is not
an address is a file path (a share's), used as it comes out.
"""
from __future__ import annotations

import math
import re
from dataclasses import dataclass, field
from typing import Any, Callable, Optional, Sequence
from urllib.parse import quote

from ..util import is_number, is_record, js_string, time_of

#: The size asked for (and shown on the card) when the settings file does not say: 200 pixels high (tsudo, 2026-11-14: 「縦200pixelぐらいでいいよ」).
DEFAULT_WIDTH, DEFAULT_HEIGHT = 270, 200
_SIZE = (16, 2048)
_PLACEHOLDER = re.compile(r"\{\{|\}\}|\{([^{}]*)\}")
_ROLES = ("id", "sensor", "angle", "azimuth")
_TIMES = ("acquired", "registered", "systemRegistered")
_BOX = ("xmin", "ymin", "xmax", "ymax")
_CORNERS = {"ul": (0, 3), "ur": (2, 3), "lr": (2, 1), "ll": (0, 1)}  # indexes into (xmin, ymin, xmax, ymax)
_ALIASES = {"lon": "center.lon", "lat": "center.lat", "ulx": "ul.lon", "uly": "ul.lat", "urx": "ur.lon", "ury": "ur.lat",
            "lrx": "lr.lon", "lry": "lr.lat", "llx": "ll.lon", "lly": "ll.lat"}
_CRS = {"4326": 4326, "epsg:4326": 4326, "3857": 3857, "epsg:3857": 3857, "900913": 3857}
_ATTRIBUTE = re.compile(r"^attr\.([A-Za-z_][A-Za-z0-9_]*)$")
_MERCATOR = 20037508.342789244


@dataclass(frozen=True)
class ThumbnailPattern:
    """`thumbnail`: the address pattern and the size asked for."""
    url: str
    width: int = DEFAULT_WIDTH
    height: int = DEFAULT_HEIGHT

    def to_json(self) -> dict[str, Any]:
        return {"url": self.url, "width": self.width, "height": self.height}

    @staticmethod
    def from_json(value: Any) -> Optional["ThumbnailPattern"]:
        if isinstance(value, str) and value:
            return ThumbnailPattern(value)
        if is_record(value) and isinstance(value.get("url"), str) and value["url"]:
            return ThumbnailPattern(value["url"], _size(value.get("width"), DEFAULT_WIDTH), _size(value.get("height"), DEFAULT_HEIGHT))
        return None


def _size(value: Any, default: int) -> int:
    return int(value) if is_number(value) and _SIZE[0] <= value <= _SIZE[1] else default


def is_address(text: str) -> bool:
    """Whether a pattern (or what came out of it) is an http(s) address, not a file path."""
    return bool(re.match(r"^https?://", text.strip(), re.IGNORECASE))


@dataclass(frozen=True)
class _Part:
    """One `{…}`: what (a role, `attr.NAME`, a coordinate), its format and what comes after `@` (a zone or a CRS)."""
    name: str
    format: str
    at: str


def _parts(text: str) -> tuple[list[Any], Optional[str]]:
    """The pattern as literal text and `_Part`s, and why it cannot be read (a brace left alone)."""
    out: list[Any] = []
    last = 0
    for m in _PLACEHOLDER.finditer(text):
        literal = text[last:m.start()]
        if "{" in literal or "}" in literal:
            return out, "{ と } の対応が合わない（文字としての { } は {{ }}）"
        out.append(literal)
        token = m.group(0)
        if token in ("{{", "}}"):
            out.append(token[0])
        else:
            inner = m.group(1).strip()
            at = ""
            if "@" in inner:
                inner, at = inner.rsplit("@", 1)
            name, _, fmt = inner.partition(":")
            out.append(_Part(_ALIASES.get(name.strip(), name.strip()), fmt, at.strip()))
        last = m.end()
    tail = text[last:]
    if "{" in tail or "}" in tail:
        return out, "{ と } の対応が合わない（文字としての { } は {{ }}）"
    out.append(tail)
    return out, None


def _is_coordinate(name: str) -> bool:
    if name in _BOX or name == "bbox":
        return True
    corner, _, axis = name.partition(".")
    return (corner in _CORNERS or corner == "center") and axis in ("lon", "lat")


def _part_problem(part: _Part) -> Optional[str]:
    from ..request.export import format_time
    from ..time_zone import UTC, parse_time_zone

    name, fmt, at = part.name, part.format, part.at
    label = "{" + name + "}"
    if name in _TIMES or _ATTRIBUTE.match(name) and "%" in fmt:
        if at and parse_time_zone(at) is None:
            return f"{label} のタイムゾーン「{at}」が不明（Asia/Tokyo・UTC・+09:00 など）"
        if fmt and fmt not in ("iso", "iso-date", "unix", "unixms"):
            try:
                format_time(0.0, fmt, UTC)
            except (ValueError, TypeError):
                return f"{label} の日時の書式「{fmt}」を使えない"
        return None
    if at and not _is_coordinate(name):
        return f"{label} に @ は使えない（日時のタイムゾーンか座標の 3857 だけ）"
    if _is_coordinate(name):
        if at and at.lower() not in _CRS:
            return f"{label} の座標系「{at}」は 4326（経緯度）か 3857（Web メルカトル）で指定"
        if fmt and name != "bbox":
            try:
                format(1.5, fmt)
            except (ValueError, TypeError):
                return f"{label} の数の書式「{fmt}」を使えない（.6f など）"
        return None
    if name in _ROLES or _ATTRIBUTE.match(name):
        if fmt:
            try:
                format(1, fmt) if not re.search(r"[sc]$", fmt) else format("a", fmt)
            except (ValueError, TypeError):
                try:
                    format("a", fmt)
                except (ValueError, TypeError):
                    return f"{label} の書式「{fmt}」を使えない"
        return None
    if name in ("width", "height", "token"):
        return None
    return (f"{label} は使えない（id・sensor・acquired・attr.属性名・xmin・bbox・center.lat・ur.lat・width など。"
            "文字としての { } は {{ }}）")


def pattern_problems(text: str) -> list[str]:
    """What is wrong in a pattern (a brace alone, a name not known, a format that cannot be used); empty when none."""
    parts, problem = _parts(text)
    if problem:
        return [problem]
    return [p for p in (_part_problem(x) for x in parts if isinstance(x, _Part)) if p]


def thumbnail_of(value: Any, problems: list[str], at: str) -> Optional[ThumbnailPattern]:
    """`thumbnail` of a catalog (a pattern, or `{ "url", "width", "height" }`); None, the reason in `problems`, when unusable."""
    if value is None or value == "":
        return None
    if isinstance(value, str):
        url, width, height = value, DEFAULT_WIDTH, DEFAULT_HEIGHT
    elif is_record(value) and isinstance(value.get("url"), str) and value["url"].strip():
        url = value["url"]
        width, height = DEFAULT_WIDTH, DEFAULT_HEIGHT
        for key in ("width", "height"):
            v = value.get(key)
            if v is None:
                continue
            if not (is_number(v) and float(v).is_integer() and _SIZE[0] <= v <= _SIZE[1]):
                problems.append(f"{at}.{key} は {_SIZE[0]}〜{_SIZE[1]} の整数（ピクセル）で指定")
                continue
            width, height = (int(v), height) if key == "width" else (width, int(v))
    else:
        problems.append(f'{at} はサムネイルのアドレスの文字列か {{ "url", "width", "height" }} で指定')
        return None
    found = pattern_problems(url)
    if found:
        problems.extend(f"{at}: {p}" for p in found)
        return None
    return ThumbnailPattern(url.strip(), width, height)


# --- Coordinates ----------------------------------------------------------------------------------

def _points(geometry: Optional[dict[str, Any]]) -> list[tuple[float, float]]:
    """Every vertex of an Esri geometry (rings, paths, points, a point or an envelope), WGS 84."""
    if not is_record(geometry):
        return []
    out: list[tuple[float, float]] = []

    def add(p: Any) -> None:
        if isinstance(p, (list, tuple)) and len(p) >= 2 and all(is_number(v) and math.isfinite(v) for v in p[:2]):
            out.append((float(p[0]), float(p[1])))

    for key in ("rings", "paths"):
        for part in geometry.get(key) or []:
            for p in part if isinstance(part, list) else []:
                add(p)
    for p in geometry.get("points") or []:
        add(p)
    if is_number(geometry.get("x")) and is_number(geometry.get("y")):
        add([geometry["x"], geometry["y"]])
    if all(is_number(geometry.get(k)) for k in _BOX):
        for x, y in ((geometry["xmin"], geometry["ymin"]), (geometry["xmax"], geometry["ymax"])):
            add([x, y])
    return out


@dataclass(frozen=True)
class Footprint:
    """The coordinates a pattern can use: the box around the footprint, its corners (vertices) and centre (WGS 84)."""
    box: tuple[float, float, float, float]
    corners: dict[str, tuple[float, float]]
    center: tuple[float, float]


def footprint_of(geometry: Optional[dict[str, Any]]) -> Optional[Footprint]:
    """The footprint's box, corner vertices and centre; None without a geometry."""
    points = _points(geometry)
    if not points:
        return None
    xs, ys = [p[0] for p in points], [p[1] for p in points]
    box = (min(xs), min(ys), max(xs), max(ys))
    # Distances on the ground: a degree of longitude is shorter away from the equator.
    k = max(0.01, math.cos(math.radians((box[1] + box[3]) / 2)))
    corners = {}
    for name, (ix, iy) in _CORNERS.items():
        cx, cy = box[ix], box[iy]
        corners[name] = min(points, key=lambda p: ((p[0] - cx) * k) ** 2 + (p[1] - cy) ** 2)
    return Footprint(box, corners, ((box[0] + box[2]) / 2, (box[1] + box[3]) / 2))


def _mercator(lon: float, lat: float) -> tuple[float, float]:
    lat = max(-85.05112878, min(85.05112878, lat))
    x = lon * _MERCATOR / 180
    y = math.log(math.tan((90 + lat) * math.pi / 360)) / (math.pi / 180) * _MERCATOR / 180
    return x, y


def _number_text(v: float, fmt: str, metres: bool) -> str:
    if fmt:
        return format(v, fmt)
    text = f"{v:.{3 if metres else 8}f}".rstrip("0").rstrip(".")
    return "0" if text in ("-0", "") else text


def _coordinate(name: str, place: Footprint, fmt: str, at: str) -> str:
    metres = _CRS.get(at.lower(), 4326) == 3857

    def xy(lon: float, lat: float) -> tuple[float, float]:
        return _mercator(lon, lat) if metres else (lon, lat)

    if name in _BOX or name == "bbox":
        x0, y0 = xy(place.box[0], place.box[1])
        x1, y1 = xy(place.box[2], place.box[3])
        values = {"xmin": x0, "ymin": y0, "xmax": x1, "ymax": y1}
        if name == "bbox":
            return ",".join(_number_text(values[k], fmt, metres) for k in _BOX)
        return _number_text(values[name], fmt, metres)
    corner, _, axis = name.partition(".")
    lon, lat = place.center if corner == "center" else place.corners[corner]
    x, y = xy(lon, lat)
    return _number_text(x if axis == "lon" else y, fmt, metres)


# --- Filling in -----------------------------------------------------------------------------------

def _text(value: Any, fmt: str) -> str:
    if value is None:
        return ""
    if fmt:
        tries = [value]
        if is_number(value) and not re.search(r"[sc]$", fmt):
            # 「{attr.CLOUD:03d}」 of 7.0 and 「{attr.CLOUD:.1f}」 of 7 both work.
            tries = [int(value), float(value)] if float(value).is_integer() else [float(value)]
        for v in tries:
            try:
                return format(v, fmt)
            except (ValueError, TypeError):
                continue
        return js_string(value)
    return js_string(value)


def _time(value: Any, fmt: str, at: str) -> str:
    from ..request.export import format_time
    from ..time_zone import UTC, parse_time_zone

    ms = time_of(value)
    if math.isnan(ms):
        return "" if value is None else js_string(value)
    written = format_time(ms, fmt or "iso", parse_time_zone(at) or UTC if at else UTC)
    return "" if written is None else js_string(written)


def fill(pattern: str, attributes: dict[str, Any], fields: dict[str, Any], geometry: Optional[dict[str, Any]],
         width: int = DEFAULT_WIDTH, height: int = DEFAULT_HEIGHT, token: Optional[str] = None,
         encode: Optional[bool] = None) -> Optional[str]:
    """
    The pattern with an image's values put in (`attributes` the catalog's,
    `fields` its roles); None when it asks for coordinates and the image has
    no footprint. Text values are percent-encoded in an http(s) address
    (`encode` says otherwise).
    """
    parts, problem = _parts(pattern)
    if problem:
        return None
    encode = is_address(pattern) if encode is None else encode
    place: Optional[Footprint] = None
    out = []
    for part in parts:
        if not isinstance(part, _Part):
            out.append(part)
            continue
        name = part.name
        if _is_coordinate(name):
            place = place or footprint_of(geometry)
            if place is None:
                return None
            out.append(_coordinate(name, place, part.format, part.at))
            continue
        if name in ("width", "height"):
            out.append(str(width if name == "width" else height))
            continue
        if name == "token":
            value = token or ""
        elif name in _TIMES:
            role = fields.get(name)
            value = _time(attributes.get(role) if role else None, part.format, part.at)
        elif name in _ROLES:
            role = fields.get(name)
            raw = attributes.get(role) if role else None
            value = _text(raw, part.format)
        else:
            m = _ATTRIBUTE.match(name)
            raw = attributes.get(m.group(1)) if m else None
            value = _time(raw, part.format, part.at) if "%" in part.format or part.format in ("iso", "iso-date", "unix", "unixms") \
                else _text(raw, part.format)
        out.append(quote(value, safe="/:,") if encode else value)
    return "".join(out)


def thumbnail_address(pattern: ThumbnailPattern, feature: dict[str, Any], fields: dict[str, Any],
                      token: Optional[str] = None) -> Optional[str]:
    """Where an image's thumbnail is (its feature: attributes and WGS 84 geometry); None when the pattern needs what it lacks."""
    return fill(pattern.url, feature.get("attributes") or {}, fields, feature.get("geometry"), pattern.width, pattern.height, token)


# --- 写っている地物 (footprintLayers) --------------------------------------------------------------

#: The most features of one layer read for a card (tsudo, 2026-11-14: 「略さずすべて」; this only keeps a huge layer from stalling).
HARD_LIMIT = 10_000
#: Brackets left with nothing in them once a value was empty (「横須賀本港（）」 → 「横須賀本港」).
_EMPTY_BRACKETS = re.compile(r"\s*(\(\s*\)|（\s*）|\[\s*\]|［\s*］|「\s*」|【\s*】|<\s*>|〈\s*〉)")


#: A line's background as the settings file or an attribute may write it: #rgb, #rrggbb, #aarrggbb or a colour's name.
_COLOR = re.compile(r"#(?:[0-9A-Fa-f]{3}|[0-9A-Fa-f]{6}|[0-9A-Fa-f]{8})|[A-Za-z]{3,30}")


def is_color(value: Any) -> bool:
    return isinstance(value, str) and bool(_COLOR.fullmatch(value.strip()))


@dataclass(frozen=True)
class RowColors:
    """
    `background` of a footprint layer (tsudo, 2026-11-14: 「その行の背景色を属性から変えたい」):
    each feature's line coloured by one attribute, through `colors` (value → colour),
    else the value itself as the colour; `default` for the rest (none: no background).
    """
    field: str
    colors: tuple[tuple[str, str], ...] = ()
    default: Optional[str] = None

    def color_for(self, value: Optional[str]) -> Optional[str]:
        if value is not None and value != "":
            if self.colors:
                found = next((c for v, c in self.colors if v == value), None)
                if found is not None:
                    return found
            elif is_color(value):
                return value.strip()
        return self.default


def row_colors_of(value: Any, problems: list[str], at: str) -> Optional[RowColors]:
    """`background`: an attribute's name (its values are colours) or `{ "field", "colors": { value: colour }, "default" }`."""
    from ..util import is_name

    if value is None:
        return None
    if isinstance(value, str):
        if is_name(value.strip()):
            return RowColors(value.strip())
        problems.append(f"{at} は属性名（英数字と _）か {{ \"field\", \"colors\", \"default\" }} で指定")
        return None
    if not is_record(value) or not (isinstance(value.get("field"), str) and is_name(value["field"].strip())):
        problems.append(f"{at} は属性名（英数字と _）か {{ \"field\", \"colors\", \"default\" }} で指定（field は属性名）")
        return None
    colors: list[tuple[str, str]] = []
    given = value.get("colors")
    if given is not None and not is_record(given):
        problems.append(f"{at}.colors は {{ \"属性の値\": \"#rrggbb\" }} で指定")
    for v, c in (given.items() if is_record(given) else []):
        if is_color(c):
            colors.append((str(v).strip(), c.strip()))
        else:
            problems.append(f"{at}.colors の「{v}」の色は #rrggbb か色の名前（pink など）で指定")
    default = value.get("default")
    if default is not None and default != "" and not is_color(default):
        problems.append(f"{at}.default は #rrggbb か色の名前（pink など）で指定")
        default = None
    return RowColors(value["field"].strip(), tuple(colors), default.strip() if isinstance(default, str) and default.strip() else None)


@dataclass(frozen=True)
class FootprintLayer:
    """
    One entry of `footprintLayers` (tsudo, 2026-11-14: 「この画像に入っている地物は何か」):
    an Esri layer whose features over an image's footprint its card names,
    all of them (`max` only when the settings file sets one).
    """
    label: str
    url: str
    fields: tuple[str, ...]
    format: str = ""
    where: Optional[str] = None
    max: Optional[int] = None
    token: Optional[str] = None
    #: Each line's background from an attribute (`background`).
    background: Optional[RowColors] = None

    def key(self) -> str:
        """What a kept answer was asked with (asked again when the settings file changes it; not for its colours)."""
        key = "|".join([self.url, ",".join(self.fields), self.format, self.where or "", str(self.max or "")])
        return key + ("|background=" + self.background.field if self.background else "")

    def out_fields(self) -> tuple[str, ...]:
        return tuple(dict.fromkeys(self.fields + ((self.background.field,) if self.background else ())))


def footprint_layers_of(value: Any, problems: list[str], at: str) -> list[FootprintLayer]:
    """`footprintLayers` of a catalog: an array of `{ "label", "url", "fields", "format", "where", "max", "token", "background" }`."""
    from ..esri import condition_problem
    from ..util import is_name, is_url, layer_url

    if value is None or value == []:
        return []
    if not isinstance(value, list):
        problems.append(f'{at} は配列で指定（[{{ "label", "url", "fields" }}]）')
        return []
    out: list[FootprintLayer] = []
    for i, entry in enumerate(value):
        where = f"{at}[{i}]"
        if not is_record(entry):
            problems.append(f'{where} は {{ "label", "url", "fields" }} で指定')
            continue
        url = entry.get("url")
        if not is_url(url) or not re.search(r"/(FeatureServer|MapServer)/\d+/?$", url.split("?", 1)[0], re.IGNORECASE):
            problems.append(f"{where}.url は …/FeatureServer/0 のようなレイヤーの URL で指定")
            continue
        names = entry.get("fields")
        names = [names] if isinstance(names, str) else names
        if not isinstance(names, list) or not names or not all(is_name(n) for n in names):
            problems.append(f"{where}.fields は表示する属性名（英数字と _）か、その配列で指定")
            continue
        fmt = entry.get("format") or ""
        if not isinstance(fmt, str):
            problems.append(f"{where}.format は文字列で指定（{{NAME}}（{{NAME_EN}}） など）")
            fmt = ""
        elif fmt:
            bad = [n for n in re.findall(r"\{([^{}]*)\}", fmt.replace("{{", "").replace("}}", "")) if n.strip() not in names]
            if bad:
                problems.append(f"{where}.format の {{{bad[0]}}} は fields にない属性（fields に足すか、書式から外す）")
                fmt = ""
        condition = entry.get("where")
        if condition is not None and not (isinstance(condition, str) and not condition_problem(condition)):
            problems.append(f"{where}.where は 1 つの条件式の文字列で指定"
                            + (f"（{condition_problem(condition)}）" if isinstance(condition, str) else ""))
            condition = None
        most = entry.get("max")
        if most is not None and not (is_number(most) and float(most).is_integer() and 1 <= most <= HARD_LIMIT):
            problems.append(f"{where}.max はカードに出す地物の数の上限（1〜{HARD_LIMIT}。書かなければすべて）で指定")
            most = None
        label = entry.get("label")
        token = entry.get("token")
        background = row_colors_of(entry.get("background"), problems, f"{where}.background")
        out.append(FootprintLayer(
            label=label.strip() if isinstance(label, str) and label.strip() else f"レイヤー {i + 1}", url=layer_url(url),
            fields=tuple(dict.fromkeys(names)), format=fmt, where=condition.strip() if isinstance(condition, str) and condition.strip() else None,
            max=int(most) if most is not None else None, token=token if isinstance(token, str) and token else None,
            background=background))
    return out


def feature_text(layer: FootprintLayer, attributes: dict[str, Any]) -> str:
    """How a card names one feature: its `format` filled in (brackets left empty dropped), else its fields' values with spaces."""
    def value(name: str) -> str:
        v = attributes.get(name)
        return "" if v is None else js_string(v).strip()

    if not layer.format:
        return " ".join(v for v in (value(n) for n in layer.fields) if v)
    text = re.sub(r"\{\{|\}\}|\{([^{}]*)\}", lambda m: m.group(0)[0] if m.group(1) is None else value(m.group(1).strip()), layer.format)
    text = _EMPTY_BRACKETS.sub("", text)
    return re.sub(r"\s{2,}", " ", text).strip()


def _geometry_params(geometry: dict[str, Any]) -> dict[str, str]:
    """An image's footprint (Esri JSON, WGS 84) as the spatial filter of a query: what crosses it."""
    import json

    kind = ("esriGeometryPolygon" if geometry.get("rings") else "esriGeometryPolyline" if geometry.get("paths")
            else "esriGeometryMultipoint" if geometry.get("points") else "esriGeometryEnvelope" if "xmin" in geometry else "esriGeometryPoint")
    shape = {k: v for k, v in geometry.items() if k != "spatialReference"}
    return {"geometry": json.dumps({**shape, "spatialReference": {"wkid": 4326}}), "geometryType": kind, "inSR": "4326",
            "spatialRel": "esriSpatialRelIntersects"}


@dataclass
class Contents:
    """
    The features of one layer over an image: their names in the layer's order
    (a name several features share once, with ×n), how many features were read
    and how many there are (more only when cut at `max` or HARD_LIMIT).
    """
    texts: list[str]
    count: int
    read: Optional[int] = None
    #: Each line's value of the layer's `background` attribute (empty: no background asked).
    values: list[Optional[str]] = field(default_factory=list)

    @property
    def cut(self) -> bool:
        return self.read is not None and self.count > self.read

    def value_at(self, i: int) -> Optional[str]:
        return self.values[i] if i < len(self.values) else None

    def to_json(self) -> dict[str, Any]:
        return {"texts": self.texts, "count": self.count, **({"read": self.read} if self.read is not None else {}),
                **({"values": self.values} if any(v is not None for v in self.values) else {})}

    @staticmethod
    def from_json(value: Any) -> Optional["Contents"]:
        if not is_record(value) or not isinstance(value.get("texts"), list) or not is_number(value.get("count")):
            return None
        read = value.get("read")
        texts = [str(t) for t in value["texts"]]
        values = value.get("values")
        values = [None if v is None else str(v) for v in values] if isinstance(values, list) and len(values) == len(texts) else []
        return Contents(texts, int(value["count"]), int(read) if is_number(read) else None, values)


def _named(lines: Sequence[tuple[str, Optional[str]]]) -> tuple[list[str], list[Optional[str]]]:
    """
    Each name (with its `background` value) once, in order; one several features have gets ×n;
    the features without a name last, 「（名前なし ×n）」. The names and their values.
    """
    counts: dict[tuple[str, Optional[str]], int] = {}
    for line in sorted(lines, key=lambda x: not x[0]):  # stable: the named ones in order, then the unnamed
        counts[line] = counts.get(line, 0) + 1
    texts = [(n if c == 1 else f"{n} ×{c}") if n else ("（名前なし）" if c == 1 else f"（名前なし ×{c}）") for (n, _v), c in counts.items()]
    return texts, [v for _n, v in counts]


def contents_of(client, layer: FootprintLayer, geometry: Optional[dict[str, Any]], token: Optional[str] = None,
                cancelled: Optional[Callable[[], bool]] = None) -> Contents:
    """
    The features of `layer` over a footprint (an EsriClient asks; every page
    of them, up to `max` or HARD_LIMIT); none for an image without a footprint.
    """
    from ..esri import own_condition

    if not _points(geometry):
        return Contents([], 0)
    limit = layer.max or HARD_LIMIT
    spatial = _geometry_params(geometry)
    where = own_condition(layer.where) if layer.where else "1=1"
    features = client.query_all(layer.url, where, token, out_fields=",".join(layer.out_fields()), geometry=False, extra=spatial,
                                limit=limit, cancelled=cancelled, cut=True)

    def colour_value(attributes: dict[str, Any]) -> Optional[str]:
        v = attributes.get(layer.background.field) if layer.background else None
        return None if v is None or js_string(v).strip() == "" else js_string(v).strip()

    lines = [(feature_text(layer, f.get("attributes") or {}), colour_value(f.get("attributes") or {})) for f in features if is_record(f)]
    texts, values = _named(lines)
    values = values if layer.background else []
    if len(features) < limit:
        return Contents(texts, len(features), values=values)
    # Cut at the limit: how many there are in all.
    count = len(features)
    try:
        total = client.ask(f"{layer.url}/query", {"where": where, **spatial, "returnCountOnly": "true"}, token).get("count")
        count = max(count, int(total)) if is_number(total) else count
    except Exception:  # noqa: BLE001 (an old service: the names read are enough)
        pass
    return Contents(texts, count, len(features), values)


def contents_lines(contents: Contents) -> list[str]:
    """The names one to a line (「横須賀本港」「長浦港 ×2」, tsudo 2026-11-14: 箇条書きみたいに、点はなし); 「なし」 for none."""
    return list(contents.texts) if contents.texts else ["なし"]


def layers_key(layers: Sequence[FootprintLayer]) -> str:
    """The kept answers of a catalog's layers are used while this stays the same."""
    return "\n".join(layer.key() for layer in layers)
