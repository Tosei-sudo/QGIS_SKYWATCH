"""
Shapes handed to MIKAGE from outside (2026-11-15, tsudo: 「AutoMap から、ミカゲに
送る流れにしたい」): points, lines and polygons sent to the plugin's local
receiver (ui/shape_receiver.py) by AutoMap, a link or a program, read as the
viewer reads the shapes of its links (usecase/image-viewer/src/link-shapes.ts),
with the same parameters:

- `geom=35.6812,139.7671` a point; `geom=A;B;C` several points (any notation
  core.coords reads: latitude / longitude, degrees-minutes-seconds, MGRS,
  UTM); `geom=POLYGON((139.7 35.6, …))` any WKT geometry (longitude first;
  EWKT `SRID=3857;` too);
- `line=A;B;C` a line, `polygon=A;B;C` a polygon (closed for you);
- `geojson={…}` a GeoJSON geometry, Feature or FeatureCollection (WGS 84),
  its features' plain properties kept as attributes;
- `label=` (repeatable) labels the shapes: the n-th label goes to the n-th
  shape parameter; `name=` names the layer, `color=#rrggbb` colours it and
  `zoom=` is the zoom level a single point is shown at.

Every value is read as data only and checked strictly (characters, size,
number of shapes and vertices, coordinate ranges); nothing in it runs, no
file or URL in it is opened. No QGIS here: the shapes come out as GeoJSON
geometries (WGS 84) for qgis_io/link_shapes_io.py.
"""
from __future__ import annotations

import json
import math
import re
from dataclasses import dataclass, field
from typing import Any, Iterable, Optional, Union
from urllib.parse import parse_qsl

from .coords import parse_coordinate

#: The parameters about shapes (others are left alone).
SHAPE_KEYS = ("geom", "line", "polygon", "geojson", "label", "name", "color", "zoom")
#: Parameters that make a shape.
SHAPE_PARAMS = frozenset(("geom", "line", "polygon", "geojson"))
#: Longest value of one parameter in a link (characters); a request's body may hold more (MAX_BODY_VALUE).
MAX_VALUE = 200_000
#: Longest value of one parameter sent in a request's body (characters).
MAX_BODY_VALUE = 8_000_000
#: Most shapes (features) one sending makes.
MAX_SHAPES = 1000
#: Most vertices of all the shapes together.
MAX_VERTICES = 100_000
MAX_LABEL = 200
MAX_NAME = 100
#: Most attributes kept from GeoJSON properties, the longest name and text value.
MAX_FIELDS = 50
MAX_FIELD_NAME = 64
MAX_TEXT = 1000
#: Most nested GeometryCollections.
MAX_DEPTH = 8
#: The zoom level a single point is shown at when the sender gives none (about a town's streets), as in the viewer.
POINT_ZOOM = 14
#: The layer's name when the sender gives none.
DEFAULT_TITLE = "受け取った図形"
#: Web Mercator's sphere (m), for `SRID=3857;`.
_R = 6378137.0
_MERC_MAX = 20037508.342789244

Params = list[tuple[str, Union[str, dict, list]]]


class LinkError(ValueError):
    """A value that cannot be read (its message in Japanese, for the message bar)."""


@dataclass
class LinkShape:
    #: A GeoJSON geometry in WGS 84 (lon, lat): Point, LineString, Polygon or their Multi kinds (never a collection).
    geometry: dict
    label: Optional[str] = None
    #: Plain properties of a GeoJSON feature (str, int or float), by field name.
    properties: dict = field(default_factory=dict)


@dataclass
class LinkShapes:
    """What a sending's shapes became."""
    title: str
    shapes: list[LinkShape]
    #: The GeoJSON properties' fields, in order: (name, "string" | "integer" | "double").
    fields: list[tuple[str, str]]
    #: Whether any shape has a label (the layer then shows them).
    labeled: bool
    #: The colour (`#rrggbb`), or None for the plugin's.
    color: Optional[str]
    #: The zoom level a single point is shown at, or None for POINT_ZOOM.
    zoom: Optional[float]
    #: Values that could not be read, with why (Japanese).
    errors: list[str]


def has_shapes(params: Iterable[tuple[str, Any]]) -> bool:
    return any(k in SHAPE_PARAMS for k, _ in params)


def params_of_query(query: str) -> Params:
    """The shape parameters of a query string (`geom=…&label=…`, a link's or a form's body)."""
    if query.startswith("?"):
        query = query[1:]
    try:
        pairs = parse_qsl(query, keep_blank_values=True, max_num_fields=4 * MAX_SHAPES + 16)
    except ValueError as error:
        raise LinkError("パラメーターが多すぎ") from error
    return [(k, v) for k, v in pairs if k in SHAPE_KEYS]


def params_of_json(value: Any) -> Params:
    """
    The shape parameters of a JSON object (a request's body): the same keys,
    each a string (a number for `zoom`) or a list of them, `geojson` also an
    object. Keys in the object's order (the n-th label goes to the n-th shape).
    """
    if not isinstance(value, dict):
        raise LinkError("JSON はオブジェクトで（geom・line・polygon・geojson・label・name・color・zoom）")
    out: Params = []
    for key, v in value.items():
        if key not in SHAPE_KEYS:
            continue
        for item in v if isinstance(v, list) else [v]:
            if key == "geojson" and isinstance(item, dict):
                out.append((key, item))
            elif isinstance(item, str):
                out.append((key, item))
            elif key == "zoom" and isinstance(item, (int, float)) and not isinstance(item, bool):
                out.append((key, str(item)))
            elif item is None and key == "label":
                out.append((key, ""))
            else:
                raise LinkError(f"{key} は文字列（またはその配列）で")
    return out


def read_link_shapes(params: Params, max_value: int = MAX_VALUE) -> LinkShapes:
    """Reads the shapes of the parameters (in order), as the viewer's readLinkShapes."""
    errors: list[str] = []
    shapes: list[LinkShape] = []
    #: The shapes of each shape parameter, in order, for the labels.
    groups: list[list[LinkShape]] = []
    labels: list[str] = []
    fields: dict[str, str] = {}
    vertices = 0
    full = False
    for key, raw in params:
        if key == "label":
            labels.append(clean(raw if isinstance(raw, str) else "", MAX_LABEL))
            continue
        if key not in SHAPE_PARAMS:
            continue
        text = raw if isinstance(raw, str) else ""
        shown = (text[:40] + "…") if len(text) > 40 else text
        if not isinstance(raw, str):
            shown = "{…}"
        try:
            if isinstance(raw, str) and len(raw) > max_value:
                raise LinkError(f"長すぎ（{max_value:,} 文字まで）")
            if key == "geojson":
                read = read_geojson(raw, fields)
            elif key == "geom" and _WKT_START.match(text):
                read = [LinkShape(g) for g in read_wkt(text)]
            else:
                points = coordinates(text)
                if key == "geom":
                    geometry = {"type": "Point", "coordinates": points[0]} if len(points) == 1 else {"type": "MultiPoint", "coordinates": points}
                elif key == "line":
                    if len(points) < 2:
                        raise LinkError("ラインには 2 点以上が必要")
                    geometry = {"type": "LineString", "coordinates": points}
                else:
                    ring = closed(points)
                    if len(ring) < 4:
                        raise LinkError("ポリゴンには 3 点以上が必要")
                    geometry = {"type": "Polygon", "coordinates": [ring]}
                read = [LinkShape(geometry)]
            n = sum(count_vertices(s.geometry) for s in read)
            if vertices + n > MAX_VERTICES:
                raise LinkError(f"頂点が多すぎ（全体で {MAX_VERTICES:,} 点まで）")
            vertices += n
            room = MAX_SHAPES - len(shapes)
            if len(read) > room:
                read = read[:room]
                full = True
            groups.append(read)
            shapes.extend(read)
        except LinkError as error:
            errors.append(f"{key}={shown}: {error}")
            # A label still goes with the parameter it was written for.
            groups.append([])
        if full:
            break
    if full:
        errors.append(f"図形が多すぎ（{MAX_SHAPES:,} 個まで表示）")
    for i, group in enumerate(groups):
        if i < len(labels) and labels[i]:
            for s in group:
                s.label = labels[i]
    # A GeoJSON feature's own `label` or `name` labels it too.
    for s in shapes:
        if s.label is None:
            for name in ("label", "name"):
                v = s.properties.get(name)
                if isinstance(v, str) and clean(v, MAX_LABEL):
                    s.label = clean(v, MAX_LABEL)
                    break
    labeled = any(s.label for s in shapes)
    zoom_text = next((v for k, v in params if k == "zoom" and isinstance(v, str)), None)
    zoom = _number(zoom_text)
    name = next((v for k, v in params if k == "name" and isinstance(v, str)), "")
    return LinkShapes(
        title=clean(name, MAX_NAME) or DEFAULT_TITLE,
        shapes=shapes,
        fields=[(n, t) for n, t in fields.items()],
        labeled=labeled,
        color=color_of(next((v for k, v in params if k == "color" and isinstance(v, str)), None)),
        zoom=zoom if zoom is not None and 0 <= zoom <= 22 else None,
        errors=errors,
    )


def _number(text: Optional[str]) -> Optional[float]:
    if text is None or not re.fullmatch(r"\s*-?[0-9]+(?:\.[0-9]+)?\s*", text):
        return None
    return float(text)


_CONTROL = re.compile("[\u0000-\u001f\u007f-\u009f​-‏‪-‮⁦-⁩]")


def clean(text: str, max_length: int) -> str:
    """Text for a label or name: no control or direction characters, spaces made one, at most `max_length` characters."""
    return re.sub(r"\s+", " ", _CONTROL.sub(" ", text)).strip()[:max_length]


def color_of(text: Optional[str]) -> Optional[str]:
    """`#rgb` or `#rrggbb` (with or without `#`) as `#rrggbb`, or None."""
    m = re.fullmatch(r"#?([0-9a-fA-F]{3}|[0-9a-fA-F]{6})", (text or "").strip())
    if not m:
        return None
    hex_ = m.group(1)
    if len(hex_) == 3:
        hex_ = "".join(c + c for c in hex_)
    return f"#{hex_.lower()}"


def coordinates(text: str) -> list[list[float]]:
    """Coordinates separated by `;` (or new lines), each in any notation, as [lon, lat]."""
    parts = [p.strip() for p in re.split(r"[;\n]", text) if p.strip()]
    if not parts:
        raise LinkError("座標なし")
    if len(parts) > MAX_VERTICES:
        raise LinkError(f"頂点が多すぎ（{MAX_VERTICES:,} 点まで）")
    out = []
    for p in parts:
        if len(p) > 100:
            raise LinkError(f"座標として読み込み不可: {p[:40]}…")
        lon_lat = parse_coordinate(p)
        if lon_lat is None:
            raise LinkError(f"座標として読み込み不可: {p}")
        out.append([float(lon_lat[0]), float(lon_lat[1])])
    return out


def closed(points: list[list[float]]) -> list[list[float]]:
    """The ring closed: the first point again at the end."""
    if len(points) > 1 and points[0] == points[-1]:
        return points
    return [*points, points[0]]


# --- WKT ------------------------------------------------------------------------------------------------

_WKT_START = re.compile(r"^\s*(?:SRID\s*=\s*\d+\s*;\s*)?(?:POINT|LINESTRING|POLYGON|MULTIPOINT|MULTILINESTRING|MULTIPOLYGON|GEOMETRYCOLLECTION)\b",
                        re.IGNORECASE)
#: Only what WKT is made of: keywords, numbers, parentheses, commas, `SRID=n;`.
_WKT_CHARS = re.compile(r"^[A-Za-z0-9\s().,;=+-]*$")
_TOKEN = re.compile(r"[A-Za-z]+|\(|\)|,|[-+]?(?:\d+\.?\d*|\.\d+)(?:[eE][-+]?\d+)?")
_TYPES = {"POINT": "Point", "LINESTRING": "LineString", "POLYGON": "Polygon", "MULTIPOINT": "MultiPoint",
          "MULTILINESTRING": "MultiLineString", "MULTIPOLYGON": "MultiPolygon", "GEOMETRYCOLLECTION": "GeometryCollection"}


def read_wkt(text: str) -> list[dict]:
    """
    WKT or EWKT in WGS 84 (`SRID=4326;`, or none) or Web Mercator
    (`SRID=3857;`), as GeoJSON geometries (a collection's members one by one).
    """
    if not _WKT_CHARS.match(text):
        raise LinkError("WKT に使用不可の文字あり")
    m = re.match(r"^\s*SRID\s*=\s*(\d+)\s*;\s*", text, re.IGNORECASE)
    srid = int(m.group(1)) if m else 4326
    if srid not in (4326, 3857, 900913):
        raise LinkError(f"SRID={srid} には非対応（4326・3857）")
    body = text[m.end():] if m else text
    tokens = _TOKEN.findall(body)
    # Anything the tokens do not cover (`1..2`, a lone `;`) is not WKT.
    if "".join(tokens) != re.sub(r"\s+", "", body):
        raise LinkError("WKT として読み込み不可")
    mercator = srid != 4326
    reader = _WktReader(tokens, mercator)
    try:
        geometry = reader.geometry(0)
    except (IndexError, ValueError) as error:
        if isinstance(error, LinkError):
            raise
        raise LinkError("WKT として読み込み不可") from error
    if reader.i != len(tokens):
        raise LinkError("WKT として読み込み不可")
    return members(geometry)


class _WktReader:
    def __init__(self, tokens: list[str], mercator: bool):
        self.tokens = tokens
        self.i = 0
        self.mercator = mercator

    def peek(self) -> Optional[str]:
        return self.tokens[self.i] if self.i < len(self.tokens) else None

    def next(self) -> str:
        t = self.tokens[self.i]
        self.i += 1
        return t

    def expect(self, t: str) -> None:
        if self.next() != t:
            raise ValueError(f"expected {t}")

    def point(self) -> list[float]:
        nums = []
        while self.peek() is not None and re.match(r"^[-+.\d]", self.peek()):
            nums.append(float(self.next()))
        if not 2 <= len(nums) <= 4:
            raise ValueError("bad point")
        return lon_lat_of(nums[0], nums[1], self.mercator)

    def list(self, item) -> list:
        self.expect("(")
        out = [item()]
        while self.peek() == ",":
            self.next()
            out.append(item())
        self.expect(")")
        return out

    def ring(self) -> list:
        return self.list(self.point)

    def polygon(self) -> list:
        return [closed(r) for r in self.list(self.ring)]

    def multi_point_item(self) -> list[float]:
        if self.peek() != "(":
            return self.point()
        self.next()
        p = self.point()
        self.expect(")")
        return p

    def geometry(self, depth: int) -> dict:
        if depth > MAX_DEPTH:
            raise LinkError("GEOMETRYCOLLECTION の入れ子が深すぎ")
        kind = _TYPES.get(self.next().upper())
        if kind is None:
            raise ValueError("unknown type")
        if (self.peek() or "").upper() in ("Z", "M", "ZM"):
            self.next()
        if (self.peek() or "").upper() == "EMPTY":
            raise LinkError("空の図形")
        if kind == "Point":
            self.expect("(")
            p = self.point()
            self.expect(")")
            return {"type": kind, "coordinates": p}
        if kind == "LineString":
            line = self.ring()
            if len(line) < 2:
                raise LinkError("ラインには 2 点以上が必要")
            return {"type": kind, "coordinates": line}
        if kind == "Polygon":
            return {"type": kind, "coordinates": _checked_polygon(self.polygon())}
        if kind == "MultiPoint":
            return {"type": kind, "coordinates": self.list(self.multi_point_item)}
        if kind == "MultiLineString":
            lines = self.list(self.ring)
            if any(len(line) < 2 for line in lines):
                raise LinkError("ラインには 2 点以上が必要")
            return {"type": kind, "coordinates": lines}
        if kind == "MultiPolygon":
            return {"type": kind, "coordinates": [_checked_polygon(p) for p in self.list(self.polygon)]}
        return {"type": kind, "geometries": self.list(lambda: self.geometry(depth + 1))}


def _checked_polygon(rings: list) -> list:
    if any(len(r) < 4 for r in rings):
        raise LinkError("ポリゴンには 3 点以上が必要")
    return rings


def lon_lat_of(x: float, y: float, mercator: bool = False) -> list[float]:
    """A position as [lon, lat], checked on the earth (Web Mercator metres turned to degrees)."""
    if not (math.isfinite(x) and math.isfinite(y)):
        raise LinkError("緯度経度の範囲外の座標あり（WKT・GeoJSON は経度・緯度の順）")
    if mercator:
        if abs(x) > _MERC_MAX * 1.0000001 or abs(y) > _MERC_MAX * 1.5:
            raise LinkError("Web メルカトルの範囲外の座標あり")
        x = math.degrees(x / _R)
        y = math.degrees(2 * math.atan(math.exp(y / _R)) - math.pi / 2)
    if abs(x) > 180.0000001 or abs(y) > 90:
        raise LinkError("緯度経度の範囲外の座標あり（WKT・GeoJSON は経度・緯度の順）")
    return [max(-180.0, min(180.0, x)), y]


# --- GeoJSON ------------------------------------------------------------------------------------------------

_GEOMETRY_TYPES = ("Point", "LineString", "Polygon", "MultiPoint", "MultiLineString", "MultiPolygon", "GeometryCollection")
_FIELD_NAME = re.compile(r"^[\w .-]+$")


def read_geojson(raw: Union[str, dict], fields: dict[str, str]) -> list[LinkShape]:
    """A GeoJSON geometry, Feature or FeatureCollection (WGS 84), with the features' plain properties (their fields into `fields`)."""
    if isinstance(raw, str):
        try:
            value = json.loads(raw)
        except (ValueError, RecursionError) as error:
            raise LinkError("GeoJSON（JSON）として読み込み不可") from error
    else:
        value = raw
    kind = value.get("type") if isinstance(value, dict) else None
    if kind not in (*_GEOMETRY_TYPES, "Feature", "FeatureCollection"):
        raise LinkError("GeoJSON の type なし")
    if kind == "FeatureCollection":
        features = value.get("features")
        if not isinstance(features, list):
            raise LinkError("FeatureCollection に features（配列）なし")
    elif kind == "Feature":
        features = [value]
    else:
        features = [{"type": "Feature", "geometry": value, "properties": None}]
    out: list[LinkShape] = []
    for f in features:
        if not isinstance(f, dict) or f.get("type") != "Feature":
            raise LinkError("GeoJSON の Feature として読み込み不可")
        g = f.get("geometry")
        if g is None:
            continue
        geometries = members(geometry_of(g, 0))
        properties = properties_of(f.get("properties"), fields)
        out.extend(LinkShape(m, properties=dict(properties)) for m in geometries)
        if len(out) > MAX_SHAPES:
            break
    if not out:
        raise LinkError("図形なし")
    return out


def geometry_of(g: Any, depth: int) -> dict:
    """A GeoJSON geometry checked and made plain ([lon, lat] floats, third values dropped)."""
    if not isinstance(g, dict) or g.get("type") not in _GEOMETRY_TYPES:
        raise LinkError("GeoJSON の図形として読み込み不可")
    kind = g["type"]
    if kind == "GeometryCollection":
        if depth >= MAX_DEPTH:
            raise LinkError("GeometryCollection の入れ子が深すぎ")
        parts = g.get("geometries")
        if not isinstance(parts, list) or not parts:
            raise LinkError("空の図形")
        return {"type": kind, "geometries": [geometry_of(p, depth + 1) for p in parts]}
    c = g.get("coordinates")
    if not isinstance(c, list) or not c:
        raise LinkError("空の図形")
    try:
        if kind == "Point":
            return {"type": kind, "coordinates": _position(c)}
        if kind == "MultiPoint":
            return {"type": kind, "coordinates": [_position(p) for p in c]}
        if kind == "LineString":
            return {"type": kind, "coordinates": _line(c)}
        if kind == "MultiLineString":
            return {"type": kind, "coordinates": [_line(line) for line in c]}
        if kind == "Polygon":
            return {"type": kind, "coordinates": _rings(c)}
        return {"type": kind, "coordinates": [_rings(p) for p in c]}
    except TypeError as error:
        raise LinkError("GeoJSON の座標として読み込み不可") from error


def _position(p: Any) -> list[float]:
    if not isinstance(p, list) or not 2 <= len(p) <= 4 or not all(isinstance(v, (int, float)) and not isinstance(v, bool) for v in p):
        raise TypeError("position")
    return lon_lat_of(float(p[0]), float(p[1]))


def _line(c: Any) -> list[list[float]]:
    if not isinstance(c, list):
        raise TypeError("line")
    line = [_position(p) for p in c]
    if len(line) < 2:
        raise LinkError("ラインには 2 点以上が必要")
    return line


def _rings(c: Any) -> list[list[list[float]]]:
    if not isinstance(c, list) or not c:
        raise TypeError("polygon")
    rings = []
    for r in c:
        if not isinstance(r, list):
            raise TypeError("ring")
        ring = closed([_position(p) for p in r]) if r else []
        if len(ring) < 4:
            raise LinkError("ポリゴンには 3 点以上が必要")
        rings.append(ring)
    return rings


def properties_of(props: Any, fields: dict[str, str]) -> dict:
    """A feature's plain properties (names of letters, digits, `_ .-`; numbers, text, true/false as text), their types into `fields`."""
    out: dict = {}
    if not isinstance(props, dict):
        return out
    for name, value in props.items():
        if not isinstance(name, str) or not name or len(name) > MAX_FIELD_NAME or not _FIELD_NAME.match(name):
            continue
        if isinstance(value, bool):
            value, kind = ("true" if value else "false"), "string"
        elif isinstance(value, int):
            kind = "integer" if -2**31 <= value < 2**31 else "double"
            value = value if kind == "integer" else float(value)
        elif isinstance(value, float):
            if not math.isfinite(value):
                continue
            kind = "double"
        elif isinstance(value, str):
            value, kind = clean_text(value), "string"
        else:
            continue
        out[name] = value
        if name.lower() == "label":
            continue
        before = fields.get(name)
        if before is None:
            # One field per name, whatever its case (QGIS's fields are not told apart by case everywhere).
            if any(n.lower() == name.lower() for n in fields) or len(fields) >= MAX_FIELDS:
                continue
            fields[name] = kind
        elif before != kind:
            fields[name] = "string" if "string" in (before, kind) else "double"
    return out


def clean_text(text: str) -> str:
    """An attribute's text: no control characters but line breaks and tabs, at most MAX_TEXT characters."""
    return re.sub("[\u0000-\u0008\u000b\u000c\u000e-\u001f\u007f-\u009f‪-‮⁦-⁩]", "", text)[:MAX_TEXT]


def members(geometry: dict) -> list[dict]:
    """A geometry's simple members: itself, or a collection's (nested ones too)."""
    if geometry["type"] != "GeometryCollection":
        return [geometry]
    return [m for g in geometry["geometries"] for m in members(g)]


def count_vertices(geometry: dict) -> int:
    if geometry["type"] == "GeometryCollection":
        return sum(count_vertices(g) for g in geometry["geometries"])

    def walk(c) -> int:
        return 1 if c and isinstance(c[0], (int, float)) else sum(walk(x) for x in c)

    return walk(geometry["coordinates"])


FAMILIES = {"Point": "point", "MultiPoint": "point", "LineString": "line", "MultiLineString": "line",
            "Polygon": "polygon", "MultiPolygon": "polygon"}


def family_of(geometry: dict) -> str:
    """"point", "line" or "polygon"."""
    return FAMILIES[geometry["type"]]


def _num(v: float) -> str:
    # Floating-point noise (139.76710000000001) dropped: 1e-9 degrees is under a millimetre.
    return f"{v:.9f}".rstrip("0").rstrip(".")


def to_wkt(geometry: dict) -> str:
    """A GeoJSON geometry (lon, lat) as WKT."""
    def pt(c) -> str:
        return f"{_num(c[0])} {_num(c[1])}"

    def lst(items, f) -> str:
        return "(" + ", ".join(f(x) for x in items) + ")"

    kind, c = geometry["type"], geometry.get("coordinates")
    if kind == "Point":
        return f"POINT ({pt(c)})"
    if kind in ("LineString", "MultiPoint"):
        return f"{kind.upper()} {lst(c, pt)}"
    if kind in ("Polygon", "MultiLineString"):
        return f"{kind.upper()} {lst(c, lambda r: lst(r, pt))}"
    if kind == "MultiPolygon":
        return f"MULTIPOLYGON {lst(c, lambda p: lst(p, lambda r: lst(r, pt)))}"
    raise ValueError(f"unknown geometry type: {kind}")


def bbox_of(shapes: Iterable[LinkShape]) -> Optional[tuple[float, float, float, float]]:
    """(min lon, min lat, max lon, max lat) of the shapes, or None."""
    xs: list[float] = []
    ys: list[float] = []

    def walk(c) -> None:
        if c and isinstance(c[0], (int, float)):
            xs.append(c[0])
            ys.append(c[1])
        else:
            for x in c:
                walk(x)

    for s in shapes:
        walk(s.geometry["coordinates"])
    if not xs:
        return None
    return min(xs), min(ys), max(xs), max(ys)


def zoom_scale(zoom: float) -> float:
    """The map scale of a Web Mercator zoom level (the viewer's), at the equator and 96 dpi (zoom 16 ≈ 1:8,531)."""
    return 559082264.0287178 / (2 ** zoom)
