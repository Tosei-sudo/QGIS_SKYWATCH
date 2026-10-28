"""
Templates for a new scratch layer, as the viewer's layer-templates.ts and
the design checks of temp-layers.ts: ready designs (a name, a geometry type
and attributes) the person picks and then changes as they like before the
layer is made. Some come built in, an organisation can add its own in the
settings file (`layerTemplates`), and anyone can keep their own designs
(QgsSettings in the plugin).
"""
from __future__ import annotations

import datetime
import math
import re
from dataclasses import dataclass, field, replace
from typing import Any, Optional, Union

#: The geometry types a design may have, as the viewer names them.
GEOMETRY_NAMES = {
    "Point": "ポイント",
    "LineString": "ライン",
    "Polygon": "ポリゴン",
    "MultiPoint": "マルチポイント",
    "MultiLineString": "マルチライン",
    "MultiPolygon": "マルチポリゴン",
}
TYPE_NAMES = {"string": "文字列", "integer": "整数", "double": "小数", "date": "日付"}
DESIGN_TYPES = tuple(TYPE_NAMES)
#: Names Shapefile and GeoPackage writers keep for themselves.
RESERVED = {"fid", "geom", "geometry", "shape"}


@dataclass
class FieldDesign:
    """One attribute of a layer being designed, as typed in the dialog."""
    name: str
    type: str = "string"
    #: Heading in the table; the name when empty.
    alias: str = ""
    #: Whether a value is needed (not null).
    required: bool = False
    #: Longest text, for strings.
    length: Optional[float] = None
    #: Allowed range, for numbers (either end may be left open).
    min: Optional[float] = None
    max: Optional[float] = None
    #: Allowed values, comma separated: `1=住宅` (code = name) or just the value.
    codes: str = ""
    #: The value new features start with, as typed.
    defaultValue: str = ""  # noqa: N815 (the viewer's name)


@dataclass
class LayerTemplate:
    """A design to start a new layer from."""
    #: What the list shows.
    name: str
    #: The made layer's name.
    title: str
    #: None: not given (the viewer chooses for each feature drawn).
    geometryType: Optional[str]  # noqa: N815
    fields: list[FieldDesign] = field(default_factory=list)
    #: One line on what it is for.
    description: str = ""


def _field(name: str, type_: str, alias: str, **more) -> FieldDesign:
    return FieldDesign(name, type_, alias, **more)


#: The built-in templates; the first is the blank design the dialog opens with.
BUILTIN_TEMPLATES: tuple[LayerTemplate, ...] = (
    LayerTemplate("空のレイヤー", "新規レイヤー", "Point", [_field("name", "string", "")], "テキスト属性 name のみ"),
    LayerTemplate(
        "航跡（生存圏計算用）", "航跡", "Point",
        [
            _field("track_id", "string", "目標ID", required=True),
            _field("time", "date", "時刻", required=True),
            _field("course", "double", "針路（度）", min=0, max=359.9),
            _field("speed", "double", "速力（kn）", min=0),
            _field("name", "string", "名称"),
            _field("source", "string", "情報源", codes="目視, レーダー, AIS, 画像判読, その他"),
            _field("note", "string", "備考"),
        ],
        "目標ID・時刻・針路・速力の位置報告。「移動目標撮像計画…」で属性を自動対応、存在予測円を計算",
    ),
    LayerTemplate(
        "調査地点", "調査地点", "Point",
        [_field("name", "string", "地点名", required=True), _field("kind", "string", "種別", codes="建物, 道路, 河川, 植生, その他"),
         _field("surveyed", "date", "調査日"), _field("note", "string", "備考")],
        "地点名・種別・調査日・備考",
    ),
    LayerTemplate(
        "目標・物体", "目標", "Point",
        [
            _field("kind", "string", "種別", required=True),
            _field("count", "integer", "数", min=0, defaultValue="1"),
            _field("confidence", "integer", "確度", codes="1=確実, 2=可能性大, 3=可能性あり", defaultValue="2"),
            _field("observed", "date", "撮影日時"),
            _field("image", "string", "画像名"),
            _field("note", "string", "備考"),
        ],
        "画像から読み取った物体の種別・数・確度・撮影日時・画像名",
    ),
    LayerTemplate(
        "関心区域（AOI）", "関心区域", "Polygon",
        [
            _field("name", "string", "区域名", required=True),
            _field("priority", "integer", "優先度", codes="1=高, 2=中, 3=低", defaultValue="2"),
            _field("start", "date", "開始日"),
            _field("end", "date", "終了日"),
            _field("requester", "string", "依頼元"),
            _field("note", "string", "備考"),
        ],
        "区域名・優先度・期間・依頼元",
    ),
    LayerTemplate(
        "被害状況", "被害状況", "Polygon",
        [
            _field("kind", "string", "種別", codes="倒壊, 浸水, 土砂, 火災, 道路寸断, その他", required=True),
            _field("level", "integer", "程度", codes="1=全壊, 2=半壊, 3=一部損壊, 4=被害なし"),
            _field("checked", "date", "確認日"),
            _field("note", "string", "備考"),
        ],
        "被害の種別・程度・確認日（災害の判読向け）",
    ),
    LayerTemplate(
        "経路・測線", "経路", "LineString",
        [_field("name", "string", "経路名", required=True), _field("kind", "string", "種別", codes="道路, 航路, 測線, その他"),
         _field("note", "string", "備考")],
        "経路名・種別・備考",
    ),
    LayerTemplate(
        "注記", "注記", None,
        [_field("label", "string", "文字", required=True), _field("category", "string", "分類"), _field("note", "string", "備考")],
        "地図上の文字（ジオメトリは作図ごとに選択）",
    ),
)


def _text(v: Any) -> Optional[str]:
    if isinstance(v, str):
        return v
    if isinstance(v, bool):
        return None
    if isinstance(v, int):
        return str(v)
    if isinstance(v, float):
        # As JavaScript's String(1.0) gives "1".
        return str(int(v)) if v.is_integer() else repr(v)
    return None


def _number(v: Any) -> Optional[float]:
    return v if isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v) else None


def template_of(value: Any, problems: list[str], at: str) -> Optional[LayerTemplate]:
    """
    A template from JSON (the settings file or saved designs); the problems
    are collected with `at` naming where. Fields are not checked further
    here: the dialog says what is wrong when the layer is made.
    """
    if not isinstance(value, dict) or not isinstance(value.get("name"), str) or not value["name"].strip():
        problems.append(f"{at} の name は文字列で指定")
        return None
    geometry = value.get("geometryType")
    if geometry is not None and geometry not in GEOMETRY_NAMES:
        problems.append(f"{at} の geometryType は {'・'.join(GEOMETRY_NAMES)}・null のいずれかで指定")
        return None
    if not isinstance(value.get("fields"), list):
        problems.append(f"{at} の fields は配列で指定")
        return None
    fields: list[FieldDesign] = []
    for i, f in enumerate(value["fields"]):
        type_ = f.get("type", "string") if isinstance(f, dict) else None
        if not isinstance(f, dict) or not isinstance(f.get("name"), str) or type_ not in DESIGN_TYPES:
            problems.append(f"{at}.fields[{i}] は name と type（{'・'.join(DESIGN_TYPES)}）で指定")
            continue
        codes = f.get("codes")
        fields.append(FieldDesign(
            name=f["name"],
            type=type_,
            alias=_text(f.get("alias")) or "",
            required=f.get("required") is True,
            length=_number(f.get("length")),
            min=_number(f.get("min")),
            max=_number(f.get("max")),
            codes=", ".join(_text(c) or str(c) for c in codes) if isinstance(codes, list) else (_text(codes) or ""),
            defaultValue=_text(f.get("defaultValue")) or "",
        ))
    name = value["name"].strip()
    description = value.get("description")
    title = value.get("title")
    return LayerTemplate(
        name=name,
        title=title.strip() if isinstance(title, str) and title.strip() else name,
        geometryType=geometry,
        fields=fields,
        description=description.strip() if isinstance(description, str) else "",
    )


def templates_of(value: Any, problems: list[str]) -> list[LayerTemplate]:
    """The settings file's `layerTemplates`."""
    if not isinstance(value, list):
        problems.append("layerTemplates は配列で指定")
        return []
    found = (template_of(v, problems, f"layerTemplates[{i}]") for i, v in enumerate(value))
    return [t for t in found if t is not None]


def template_json(template: LayerTemplate) -> dict:
    """A template as JSON, in the settings file's form (read back by template_of)."""
    out: dict = {"name": template.name, "title": template.title, "geometryType": template.geometryType, "fields": []}
    if template.description:
        out["description"] = template.description
    for f in template.fields:
        item: dict = {"name": f.name, "type": f.type}
        if f.alias:
            item["alias"] = f.alias
        if f.required:
            item["required"] = True
        for key in ("length", "min", "max"):
            if getattr(f, key) is not None:
                item[key] = getattr(f, key)
        if f.codes:
            item["codes"] = f.codes
        if f.defaultValue:
            item["defaultValue"] = f.defaultValue
        out["fields"].append(item)
    return out


def copy_template(template: LayerTemplate) -> LayerTemplate:
    """A copy to change (the built-in ones stay as they are)."""
    return replace(template, fields=[replace(f) for f in template.fields])


Code = Union[str, float, int]


def parse_codes(text: str, type_: str) -> Union[list[tuple[Code, str]], str]:
    """The allowed values of `text` (`1=住宅, 2=店舗` or `赤, 青`) as (value, name), numbers for numeric types; or what is wrong."""
    items = [s.strip() for s in re.split(r"[\n,、]", text) if s.strip()]
    codes: list[tuple[Code, str]] = []
    for item in items:
        eq = item.find("=")
        raw = (item[:eq] if eq >= 0 else item).strip()
        name = (item[eq + 1:] if eq >= 0 else item).strip() or raw
        if not raw:
            return f"候補値「{item}」の値が空"
        code: Code = raw
        if type_ in ("integer", "double"):
            n = _js_number(raw)
            if n is None or (type_ == "integer" and not float(n).is_integer()):
                return f"候補値「{raw}」は{'整数' if type_ == 'integer' else '数値'}で指定"
            code = int(n) if type_ == "integer" else n
        if any(c == code for c, _ in codes):
            return f"候補値「{raw}」が重複"
        codes.append((code, name))
    return codes


def _js_number(text: str) -> Optional[float]:
    """A number as JavaScript's Number() reads a trimmed text (finite only)."""
    try:
        n = float(text)
    except ValueError:
        return None
    return n if math.isfinite(n) else None


@dataclass
class FieldSpec:
    """A checked attribute, ready to make."""
    name: str
    type: str
    alias: str
    required: bool
    length: Optional[int] = None
    #: Range: (min, max), either None for open.
    range: Optional[tuple[Optional[float], Optional[float]]] = None
    codes: Optional[list[tuple[Code, str]]] = None
    #: The starting value: a str, a number, or an ISO date-time text for dates.
    default: Any = None


def _default_of(spec: FieldSpec, text: str) -> Union[tuple[Any], str]:
    """The default value typed for `spec` (in a 1-tuple), or what is wrong (the viewer's parseValue)."""
    label = spec.alias
    t = text.strip()
    if spec.type in ("integer", "double"):
        n = _js_number(t)
        if n is None:
            return f"{label} は数値で入力"
        if spec.type == "integer" and not n.is_integer():
            return f"{label} は整数で入力"
        lo, hi = spec.range or (None, None)
        if (lo is not None and n < lo) or (hi is not None and n > hi):
            return f"{label} は {_num(lo, '-Infinity')}〜{_num(hi, 'Infinity')} の範囲で入力"
        return (int(n) if spec.type == "integer" else n,)
    if spec.type == "date":
        iso = date_text(t)
        return (iso,) if iso else f"{label} は日時で入力"
    if spec.length and len(t) > spec.length:
        return f"{label} は {spec.length} 文字以内で入力"
    return (t,)


def _num(v: Optional[float], open_end: str) -> str:
    if v is None:
        return open_end
    return str(int(v)) if float(v).is_integer() else str(v)


def date_text(text: str) -> Optional[str]:
    """
    A typed date or date-time as ISO 8601 (`2026-10-17`, `2026-10-17 09:30`,
    `2026/10/17`, with seconds or a zone); None when it is not one. A date
    alone is midnight UTC, as JavaScript's Date reads it.
    """
    m = re.fullmatch(r"(\d{4})[-/](\d{1,2})[-/](\d{1,2})(?:[T ](\d{1,2}):(\d{2})(?::(\d{2})(?:\.\d+)?)?\s*(Z|[+-]\d{2}:?\d{2})?)?", text.strip())
    if not m:
        return None
    y, mo, d, h, mi, s, zone = m.groups()
    try:
        datetime.datetime(int(y), int(mo), int(d), int(h or 0), int(mi or 0), int(s or 0))
    except ValueError:
        return None
    out = f"{int(y):04d}-{int(mo):02d}-{int(d):02d}"
    if h is None:
        return out + "T00:00:00Z"
    out += f"T{int(h):02d}:{int(mi):02d}:{int(s or 0):02d}"
    if zone:
        out += "Z" if zone == "Z" else (zone if ":" in zone else f"{zone[:3]}:{zone[3:]}")
    return out


def design_fields(title: str, fields: list[FieldDesign]) -> Union[list[FieldSpec], list[str]]:
    """
    The checked attributes of a design, or what is wrong with it (in the
    person's words, as the viewer's designFields). Tell them apart with
    `isinstance(result[0], str)` (an empty list is a design without attributes).
    """
    errors: list[str] = []
    specs: list[FieldSpec] = []
    seen: set[str] = set()
    if not title.strip():
        errors.append("レイヤーの名前を入力")
    for i, d in enumerate(fields):
        name = d.name.strip()
        label = f"属性「{name}」" if name else f"{i + 1} 番目の属性"
        if not name:
            errors.append(f"{label}の名前を入力")
            continue
        if any(ord(c) < 0x20 or c == '"' for c in name):
            errors.append(f"{label}の名前に使用不可の文字あり")
        if name.lower() in RESERVED:
            errors.append(f"{label}は書き出しで使う名前なので、別の名前で指定")
        if name.lower() in seen:
            errors.append(f"{label}が重複（大文字・小文字の区別なし）")
        seen.add(name.lower())
        spec = FieldSpec(name, d.type, d.alias.strip() or name, bool(d.required))
        if d.type == "string" and d.length is not None:
            if not float(d.length).is_integer() or d.length < 1:
                errors.append(f"{label}の文字数の上限は 1 以上の整数で指定")
            else:
                spec.length = int(d.length)
        if d.type in ("integer", "double") and (d.min is not None or d.max is not None):
            if d.min is not None and d.max is not None and d.min > d.max:
                errors.append(f"{label}の範囲は 最小 ≦ 最大 で指定")
            else:
                spec.range = (d.min, d.max)
        if d.codes.strip() and d.type != "date":
            codes = parse_codes(d.codes, d.type)
            if isinstance(codes, str):
                errors.append(f"{label}: {codes}")
            elif codes:
                spec.codes = codes
        if d.defaultValue.strip():
            value = _default_of(spec, d.defaultValue)
            if isinstance(value, str):
                errors.append(f"既定値: {value}")
            elif spec.codes and not any(c == value[0] for c, _ in spec.codes):
                errors.append(f"{label}の既定値は候補値から選択")
            else:
                spec.default = value[0]
        specs.append(spec)
    return errors if errors else specs


def summary(geometry: Optional[str], fields: list) -> str:
    """`ポイント・属性 3 個`, as the dialog describes a saved design."""
    kind = GEOMETRY_NAMES[geometry] if geometry else "ジオメトリ指定なし"
    return f"{kind}・属性 {len(fields)} 個"
