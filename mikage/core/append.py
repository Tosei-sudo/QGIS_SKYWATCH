"""
アペンド（既存レイヤーへ追加）, as ArcGIS's Append (追加) tool: the features of
one or more input layers added to a target layer that is there already.

* The field map (フィールドマップ) says where each of the target's fields
  takes its value from, for each input: one or more of its fields (with a
  merge rule when there are several, and a part of the text when asked), an
  expression (QGIS's, worked out by qgis_io/append_io.py), a fixed value, or
  nothing. It starts from the names (case and aliases aside), can be changed
  field by field, kept under a name and written to a JSON file.
* The schema types (スキーマタイプ): the field map (NO_TEST), the inputs'
  fields must match the target's (TEST, else nothing is added), inputs that
  do not match are skipped (TEST_AND_SKIP).
* Matching features are updated (ArcGIS Pro 3's update options): with key
  fields, a feature whose key the target has already updates that one (its
  geometry too when asked), the others are added.
* The check before (確認): what each feature becomes (追加・更新・飛ばす) and
  why one is skipped: a value outside the target's domain, a required field
  left empty, text longer than the field takes, a value of another type, a
  geometry of another kind.
* Written to an Esri feature layer here (EsriClient.apply_edits: who and
  when in the tracking fields, the audit log and the time zone as for every
  write of the plugin), to a QGIS layer by qgis_io/append_io.py. With 「全部
  か無しか」 (all or nothing, as ArcGIS), a failure takes back what went in;
  what went in can also be taken back afterwards (取り消し).

Pure Python: the values are Python's (text, numbers, `datetime` in UTC for a
moment, `date` for a day, booleans), the geometries whatever the caller
gives (Esri JSON for an Esri layer).
"""
from __future__ import annotations

import json
import math
import re
import statistics
import unicodedata
from dataclasses import dataclass, field, replace
from datetime import date, datetime, timedelta, timezone
from typing import Any, Callable, Iterable, Optional, Sequence

from .esri import EDIT_BATCH, EsriClient, EsriError, LayerInfo, quote
from .time_text import local_ms, parse_time
from .util import iso_text, is_number, read_number

# ---- types -------------------------------------------------------------------------------------------

STRING, INTEGER, DOUBLE, DATE, DATETIME, BOOL, OTHER = "string", "integer", "double", "date", "datetime", "bool", "other"
TYPE_NAMES = {STRING: "文字列", INTEGER: "整数", DOUBLE: "小数", DATE: "日付", DATETIME: "日時", BOOL: "真偽", OTHER: "その他"}

#: Esri field types → the kinds here (OID, GlobalID, blobs, rasters: written by no one, OTHER).
ESRI_TYPES = {
    "esriFieldTypeString": STRING, "esriFieldTypeGUID": STRING,
    "esriFieldTypeSmallInteger": INTEGER, "esriFieldTypeInteger": INTEGER, "esriFieldTypeBigInteger": INTEGER,
    "esriFieldTypeSingle": DOUBLE, "esriFieldTypeDouble": DOUBLE,
    "esriFieldTypeDate": DATETIME, "esriFieldTypeTimestampOffset": DATETIME, "esriFieldTypeDateOnly": DATE,
    "esriFieldTypeTimeOnly": STRING,
}
#: Esri field types no one writes (the server fills them).
ESRI_AUTOMATIC = {"esriFieldTypeOID", "esriFieldTypeGlobalID", "esriFieldTypeGeometry", "esriFieldTypeRaster", "esriFieldTypeBlob",
                  "esriFieldTypeXML"}

#: The kinds of geometry a layer holds (qgis_io turns QGIS's and Esri's into these).
POINT, MULTIPOINT, LINE, POLYGON, NO_GEOMETRY = "point", "multipoint", "line", "polygon", "none"
GEOMETRY_NAMES = {POINT: "ポイント", MULTIPOINT: "マルチポイント", LINE: "ライン", POLYGON: "ポリゴン", NO_GEOMETRY: "形状なし（表）"}
ESRI_GEOMETRIES = {"esriGeometryPoint": POINT, "esriGeometryMultipoint": MULTIPOINT, "esriGeometryPolyline": LINE,
                   "esriGeometryPolygon": POLYGON, "": NO_GEOMETRY}

# ---- the field map -------------------------------------------------------------------------------------

#: Where a target field takes its value from, for an input.
FIELDS, EXPRESSION, VALUE, EMPTY = "fields", "expression", "value", "empty"
SOURCE_NAMES = {FIELDS: "元の列", EXPRESSION: "式", VALUE: "固定値", EMPTY: "空"}
#: How the values of several fields become one (ArcGIS's merge rules), in the order they are offered.
MERGE_RULES = {
    "first": "最初の値", "last": "最後の値", "join": "連結", "sum": "合計", "mean": "平均", "median": "中央値", "mode": "最頻値",
    "min": "最小", "max": "最大", "stddev": "標準偏差", "count": "個数", "range": "範囲",
}
#: The merge rules that take numbers.
NUMERIC_RULES = {"sum", "mean", "median", "min", "max", "stddev", "range"}
#: The key of the rules that hold for every input (a field map from the Processing tool, or kept for any input).
ANY_INPUT = "*"
#: The version of the field map's JSON.
MAP_VERSION = 1

#: The schema types.
NO_TEST, TEST, TEST_AND_SKIP = "NO_TEST", "TEST", "TEST_AND_SKIP"
SCHEMA_NAMES = {NO_TEST: "フィールドマップで合わせる", TEST: "すべての列が一致しなければ中止", TEST_AND_SKIP: "列が一致しない入力は飛ばす"}

#: What a feature becomes.
ADD, UPDATE, SKIP = "add", "update", "skip"
ACTION_NAMES = {ADD: "追加", UPDATE: "更新", SKIP: "飛ばす"}


@dataclass(frozen=True)
class Domain:
    """An Esri domain: coded values (code → name) or a range."""
    coded: Optional[dict] = None
    low: Optional[float] = None
    high: Optional[float] = None
    name: str = ""

    def text(self) -> str:
        if self.coded is not None:
            shown = "、".join(f"{c}（{n}）" if str(n) != str(c) else str(c) for c, n in list(self.coded.items())[:8])
            return shown + ("…" if len(self.coded) > 8 else "")
        return f"{_plain_number(self.low)}〜{_plain_number(self.high)}"


@dataclass(frozen=True)
class TargetField:
    """A field of the target layer."""
    name: str
    type: str = STRING
    alias: str = ""
    #: The text a field takes (0: any).
    length: int = 0
    #: Whether it may be left empty.
    nullable: bool = True
    #: Whether anyone writes it: not an object id, a GlobalID, an area the server works out, a field the plugin
    #: stamps itself (who and when, editTracking) or one a QGIS provider numbers itself.
    editable: bool = True
    #: Why it is not (shown in the field map).
    automatic: str = ""
    domain: Optional[Domain] = None
    #: Whether the layer fills it when it is left out (a default value).
    has_default: bool = False
    #: The layer's own type name (shown).
    type_text: str = ""

    def label(self) -> str:
        return f"{self.name}（{self.alias}）" if self.alias and self.alias != self.name else self.name


@dataclass(frozen=True)
class SourceField:
    """A field of an input."""
    name: str
    type: str = STRING
    alias: str = ""


@dataclass
class TargetSchema:
    """What the target holds: its fields and its kind of geometry."""
    fields: list
    geometry: str = NO_GEOMETRY
    name: str = ""
    has_z: bool = False
    has_m: bool = False

    def field(self, name: str) -> Optional[TargetField]:
        lower = name.lower()
        return next((f for f in self.fields if f.name.lower() == lower), None)

    @property
    def mappable(self) -> list:
        return [f for f in self.fields if f.editable]


@dataclass
class InputRule:
    """Where a target field takes its value from, for one input."""
    source: str = FIELDS
    #: The input's fields (FIELDS), merged by the entry's rule when there are several.
    fields: list = field(default_factory=list)
    #: A part of the text (FIELDS): from the `start`-th character to the `end`-th (1-based, both included; None: the start / end).
    start: Optional[int] = None
    end: Optional[int] = None
    #: A QGIS expression (EXPRESSION).
    expression: str = ""
    #: The value as typed (VALUE), turned into the field's type.
    value: str = ""

    def text(self) -> str:
        """The cell of the field map's table."""
        if self.source == EMPTY or (self.source == FIELDS and not self.fields):
            return "（空）"
        if self.source == EXPRESSION:
            return f"式: {self.expression}"
        if self.source == VALUE:
            return f"固定値: {self.value}"
        part = ""
        if self.start is not None or self.end is not None:
            part = f" [{self.start or 1}〜{self.end or ''}文字目]"
        return " + ".join(self.fields) + part

    def to_json(self) -> dict:
        out: dict[str, Any] = {"from": self.source}
        if self.source == FIELDS:
            out["fields"] = list(self.fields)
            if self.start is not None:
                out["start"] = self.start
            if self.end is not None:
                out["end"] = self.end
        elif self.source == EXPRESSION:
            out["expression"] = self.expression
        elif self.source == VALUE:
            out["value"] = self.value
        return out

    @staticmethod
    def from_json(value: Any, problems: list, where: str) -> Optional["InputRule"]:
        if isinstance(value, str):
            # A field's name alone.
            return InputRule(FIELDS, [value]) if value.strip() else InputRule(EMPTY)
        if isinstance(value, list):
            names = [str(v) for v in value if isinstance(v, str) and v.strip()]
            return InputRule(FIELDS, names) if names else InputRule(EMPTY)
        if not isinstance(value, dict):
            problems.append(f"{where}: 指定の形が違う")
            return None
        source = value.get("from", FIELDS if "fields" in value else EXPRESSION if "expression" in value else VALUE if "value" in value else EMPTY)
        if source not in SOURCE_NAMES:
            problems.append(f"{where}: from は fields・expression・value・empty のどれか")
            return None
        rule = InputRule(source)
        if source == FIELDS:
            names = value.get("fields")
            if isinstance(names, str):
                names = [names]
            rule.fields = [str(n) for n in names or [] if isinstance(n, str) and n.strip()]
            for key in ("start", "end"):
                n = value.get(key)
                if n is not None:
                    if not isinstance(n, int) or isinstance(n, bool) or n < 1:
                        problems.append(f"{where}: {key} は 1 以上の整数")
                    else:
                        setattr(rule, key, n)
        elif source == EXPRESSION:
            rule.expression = str(value.get("expression") or "").strip()
            if not rule.expression:
                problems.append(f"{where}: 式が空")
                return None
        elif source == VALUE:
            v = value.get("value")
            rule.value = "" if v is None else (json.dumps(v) if isinstance(v, bool) else str(v))
        return rule


@dataclass
class FieldEntry:
    """A target field of the field map: its merge rule and where it takes its value from, by input."""
    target: str
    merge: str = "first"
    delimiter: str = ", "
    #: By the input's name (ANY_INPUT: every input without its own).
    rules: dict = field(default_factory=dict)

    def rule(self, input_name: str) -> Optional[InputRule]:
        return self.rules.get(input_name) or self.rules.get(ANY_INPUT)

    def to_json(self) -> dict:
        out: dict[str, Any] = {"target": self.target}
        if self.merge != "first":
            out["merge"] = self.merge
        if self.merge == "join":
            out["delimiter"] = self.delimiter
        out["inputs"] = {name: r.to_json() for name, r in self.rules.items()}
        return out


@dataclass
class FieldMap:
    """The field map: an entry per target field, in the target's order."""
    entries: list = field(default_factory=list)
    #: The inputs' names when it was made (a kept map given to other inputs: by position).
    inputs: list = field(default_factory=list)

    def entry(self, target: str) -> Optional[FieldEntry]:
        lower = target.lower()
        return next((e for e in self.entries if e.target.lower() == lower), None)

    def to_json(self) -> dict:
        return {"version": MAP_VERSION, "inputs": list(self.inputs), "fields": [e.to_json() for e in self.entries]}

    def text(self) -> str:
        return json.dumps(self.to_json(), ensure_ascii=False, indent=1)

    def copy(self) -> "FieldMap":
        return map_from_json(self.to_json(), [])


def map_from_json(value: Any, problems: list) -> FieldMap:
    """
    A field map from its JSON (as `FieldMap.to_json` writes it): `fields` a
    list of {target, merge, delimiter, inputs: {input name or "*": rule}}, a
    rule {from: fields|expression|value|empty, fields, start, end,
    expression, value}, a field's name or a list of names. Also a plain
    {target: rule} object. What cannot be read goes to `problems` (and is
    left out).
    """
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except ValueError as error:
            problems.append(f"JSON として読めない（{error}）")
            return FieldMap()
    if not isinstance(value, dict):
        problems.append("フィールドマップは JSON のオブジェクト")
        return FieldMap()
    out = FieldMap(inputs=[str(n) for n in value.get("inputs") or [] if isinstance(n, str)])
    fields = value.get("fields")
    if fields is None:
        # {target: rule, …}: the same for every input.
        fields = [{"target": k, "inputs": {ANY_INPUT: v}} for k, v in value.items() if k not in ("version", "inputs")]
    if not isinstance(fields, list):
        problems.append("fields はリスト")
        return out
    seen: set = set()
    for i, item in enumerate(fields, 1):
        if not isinstance(item, dict) or not isinstance(item.get("target"), str) or not item["target"].strip():
            problems.append(f"{i} 番目: target（追加先の列名）がない")
            continue
        target = item["target"].strip()
        if target.lower() in seen:
            problems.append(f"{target}: 2 回ある（先のものを使用）")
            continue
        seen.add(target.lower())
        merge = item.get("merge", "first")
        if merge not in MERGE_RULES:
            problems.append(f"{target}: merge は {'・'.join(MERGE_RULES)} のどれか")
            merge = "first"
        entry = FieldEntry(target, merge, str(item.get("delimiter", ", ")))
        inputs = item.get("inputs")
        if inputs is None and any(k in item for k in ("from", "fields", "expression", "value")):
            inputs = {ANY_INPUT: {k: v for k, v in item.items() if k in ("from", "fields", "start", "end", "expression", "value")}}
        if not isinstance(inputs, dict):
            problems.append(f"{target}: inputs は入力名ごとの指定")
            continue
        for name, rule_value in inputs.items():
            rule = InputRule.from_json(rule_value, problems, f"{target}（{name}）")
            if rule is not None:
                entry.rules[str(name)] = rule
        out.entries.append(entry)
    return out


def _key(text: str) -> str:
    """A name compared without case, width or the marks between words (`Obs_Name`, `OBS NAME`, `ｏｂｓｎａｍｅ`)."""
    return re.sub(r"[\s_\-・.]", "", unicodedata.normalize("NFKC", text or "")).casefold()


def match_field(target: TargetField, fields: Sequence[SourceField]) -> Optional[str]:
    """The input's field for a target field: the same name (case aside), else the same name or alias either way."""
    for f in fields:
        if f.name.lower() == target.name.lower():
            return f.name
    keys = {_key(target.name)} | ({_key(target.alias)} if target.alias else set())
    for f in fields:
        if _key(f.name) in keys or (f.alias and _key(f.alias) in keys):
            return f.name
    return None


def auto_map(schema: TargetSchema, inputs: Sequence[tuple]) -> FieldMap:
    """The field map from the names: each writable target field from the input's field of that name (or alias)."""
    out = FieldMap(inputs=[name for name, _fields in inputs])
    for target in schema.mappable:
        entry = FieldEntry(target.name)
        for name, fields in inputs:
            found = match_field(target, fields)
            entry.rules[name] = InputRule(FIELDS, [found]) if found else InputRule(EMPTY)
        out.entries.append(entry)
    return out


def fit_map(kept: FieldMap, schema: TargetSchema, inputs: Sequence[tuple], others_auto: bool = True) -> tuple[FieldMap, list]:
    """
    A field map (kept, read from a file or given to the Processing tool) for
    these inputs and this target: each writable target field gets its entry
    (taken from `kept` by name, else from the names when `others_auto`, else
    empty); each input its rules (by its name, else ANY_INPUT, else the kept
    input at the same position). Fields the target or an input does not
    have are left out and told.
    """
    problems: list = []
    auto = auto_map(schema, inputs)
    out = FieldMap(inputs=[name for name, _ in inputs])
    names = {name: {f.name.lower(): f.name for f in fields} for name, fields in inputs}
    for target in kept.entries:
        if schema.field(target.target) is None:
            problems.append(f"追加先に列「{target.target}」がない（無視）")
        elif not schema.field(target.target).editable:
            problems.append(f"追加先の列「{target.target}」は自動で入る（無視）")
    for target in schema.mappable:
        given = kept.entry(target.name)
        base = auto.entry(target.name)
        entry = FieldEntry(target.name, given.merge if given else "first", given.delimiter if given else ", ")
        for position, (name, _fields) in enumerate(inputs):
            rule = None
            if given is not None:
                rule = given.rules.get(name) or given.rules.get(ANY_INPUT)
                if rule is None and position < len(kept.inputs):
                    rule = given.rules.get(kept.inputs[position])
            if rule is None:
                rule = base.rules[name] if others_auto else InputRule(EMPTY)
            else:
                rule = replace(rule, fields=list(rule.fields))
                if rule.source == FIELDS:
                    present = []
                    for f in rule.fields:
                        real = names[name].get(f.lower())
                        if real is None:
                            problems.append(f"入力「{name}」に列「{f}」がない（{target.name} から外した）")
                        else:
                            present.append(real)
                    rule.fields = present
                    if not present:
                        rule = InputRule(EMPTY)
            entry.rules[name] = rule
        out.entries.append(entry)
    return out, problems


def schema_problems(schema: TargetSchema, fields: Sequence[SourceField]) -> list:
    """
    Why an input's fields do not match the target's (スキーマタイプ TEST): a
    writable target field the input does not have (by name, case aside) or
    has with another type, a field of the input the target does not have.
    """
    out = []
    by_name = {f.name.lower(): f for f in fields}
    targets = {f.name.lower(): f for f in schema.fields}
    for t in schema.mappable:
        f = by_name.get(t.name.lower())
        if f is None:
            out.append(f"列「{t.name}」がない")
        elif not _same_kind(f.type, t.type):
            out.append(f"列「{t.name}」の型が違う（{TYPE_NAMES.get(f.type, f.type)} → {TYPE_NAMES.get(t.type, t.type)}）")
    for f in fields:
        t = targets.get(f.name.lower())
        if t is None:
            out.append(f"追加先に列「{f.name}」がない")
    return out


def _same_kind(source: str, target: str) -> bool:
    if source == target:
        return True
    return (source, target) in {(INTEGER, DOUBLE), (DATE, DATETIME), (DATETIME, DATE), (BOOL, INTEGER)}


# ---- values ------------------------------------------------------------------------------------------


def _plain_number(v: Any) -> str:
    if v is None:
        return ""
    if isinstance(v, float) and v.is_integer() and abs(v) < 1e15:
        return str(int(v))
    return str(v)


def text_of(value: Any) -> str:
    """A value as text (a whole float without its .0, a moment as ISO 8601, a day as yyyy-mm-dd)."""
    if value is None:
        return ""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, float):
        return _plain_number(value)
    if isinstance(value, datetime):
        return iso_text(_ms(value))
    if isinstance(value, date):
        return value.isoformat()
    return str(value)


def _ms(value: datetime) -> int:
    if value.tzinfo is None:
        value = value.astimezone()
    return int(round(value.timestamp() * 1000))


def moment(ms: float) -> datetime:
    """A moment (ms since the epoch) as the values here keep it."""
    return datetime(1970, 1, 1, tzinfo=timezone.utc) + timedelta(milliseconds=ms)


def _number(value: Any) -> Optional[float]:
    if isinstance(value, bool):
        return 1.0 if value else 0.0
    if is_number(value):
        return float(value)
    if isinstance(value, str):
        try:
            return read_number(value)
        except ValueError:
            return None
    return None


_TRUE = {"true", "t", "yes", "y", "1", "はい", "あり", "有", "真", "on"}
_FALSE = {"false", "f", "no", "n", "0", "いいえ", "なし", "無", "偽", "off"}


@dataclass
class Converted:
    """A value made into a field's type: the value, or why it could not be (then None), and a note when it changed."""
    value: Any = None
    problem: str = ""
    note: str = ""


def convert(value: Any, f: TargetField, truncate: bool = False) -> Converted:
    """`value` as `f` takes it (its type and length), or why it cannot."""
    if value is None or (isinstance(value, float) and math.isnan(value)):
        return Converted(None)
    kind = f.type
    if isinstance(value, str) and kind != STRING and not value.strip():
        return Converted(None)
    if kind == STRING:
        text = text_of(value)
        if f.length and len(text) > f.length:
            if truncate:
                return Converted(text[:f.length], note=f"「{f.name}」を {f.length} 文字に切り詰め")
            return Converted(None, f"「{f.name}」の文字数が多すぎる（{len(text)} 文字、最大 {f.length} 文字）")
        return Converted(text)
    if kind in (INTEGER, DOUBLE):
        if isinstance(value, (datetime, date)):
            return Converted(None, f"「{f.name}」に日付は入らない（{text_of(value)}）")
        n = _number(value)
        if n is None or not math.isfinite(n):
            return Converted(None, f"「{f.name}」に数値でない値は入らない（{text_of(value)}）")
        if kind == DOUBLE:
            return Converted(float(n))
        whole = int(round(n))
        return Converted(whole, note="" if float(n).is_integer() else f"「{f.name}」の小数を丸め（{_plain_number(n)} → {whole}）")
    if kind in (DATE, DATETIME):
        ms: Optional[float] = None
        if isinstance(value, datetime):
            ms = _ms(value)
        elif isinstance(value, date):
            ms = local_ms(value.year, value.month, value.day)
        elif isinstance(value, str):
            ms = parse_time(unicodedata.normalize("NFKC", value).strip())
        elif is_number(value) and not isinstance(value, bool) and abs(value) > 1e11:
            # ms since the epoch (as Esri and the plugin keep moments).
            ms = float(value)
        if ms is None:
            return Converted(None, f"「{f.name}」に日付として読めない値は入らない（{text_of(value)}）")
        if kind == DATE:
            if isinstance(value, date) and not isinstance(value, datetime):
                return Converted(value)
            return Converted(datetime.fromtimestamp(ms / 1000).date())
        return Converted(moment(ms))
    if kind == BOOL:
        if isinstance(value, bool):
            return Converted(value)
        if is_number(value):
            return Converted(bool(value))
        t = unicodedata.normalize("NFKC", str(value)).strip().casefold()
        if t in _TRUE:
            return Converted(True)
        if t in _FALSE:
            return Converted(False)
        return Converted(None, f"「{f.name}」に真偽でない値は入らない（{text_of(value)}）")
    return Converted(value if isinstance(value, (str, int, float)) else text_of(value))


def in_domain(value: Any, domain: Optional[Domain]) -> tuple[Any, bool]:
    """
    The value for a domain (a coded value's name made its code) and whether it
    is allowed (an empty value always is).
    """
    if domain is None or value is None:
        return value, True
    if domain.coded is not None:
        for code in domain.coded:
            if code == value or text_of(code) == text_of(value):
                return code, True
        t = text_of(value).strip().casefold()
        for code, name in domain.coded.items():
            if str(name).strip().casefold() == t:
                return code, True
        return value, False
    n = _number(value)
    if n is None:
        return value, False
    return value, (domain.low is None or n >= domain.low) and (domain.high is None or n <= domain.high)


def _part(value: Any, rule: InputRule) -> Any:
    if value is None or (rule.start is None and rule.end is None):
        return value
    text = text_of(value)
    start = max(1, rule.start or 1) - 1
    return text[start:rule.end] if rule.end is not None else text[start:]


def merged(values: list, rule: str, delimiter: str = ", ") -> tuple[Any, str]:
    """The values of several fields made one by a merge rule, and a note when some could not take part."""
    present = [v for v in values if v is not None and not (isinstance(v, str) and not v.strip())]
    if rule == "first":
        return (values[0] if values else None), ""
    if rule == "last":
        return (values[-1] if values else None), ""
    if rule == "join":
        return (delimiter.join(text_of(v) for v in present) if present else None), ""
    if rule == "count":
        return len(present), ""
    if rule == "mode":
        if not present:
            return None, ""
        counts: dict = {}
        for v in present:
            counts[text_of(v)] = counts.get(text_of(v), 0) + 1
        best = max(counts.values())
        return next(v for v in present if counts[text_of(v)] == best), ""
    numbers = [n for n in (_number(v) for v in present) if n is not None and math.isfinite(n)]
    note = "数値でない値を除いて計算" if len(numbers) < len(present) else ""
    if not numbers:
        return None, note
    if rule == "sum":
        return math.fsum(numbers), note
    if rule == "mean":
        return math.fsum(numbers) / len(numbers), note
    if rule == "median":
        return statistics.median(numbers), note
    if rule == "min":
        return min(numbers), note
    if rule == "max":
        return max(numbers), note
    if rule == "range":
        return max(numbers) - min(numbers), note
    if rule == "stddev":
        return (statistics.stdev(numbers) if len(numbers) > 1 else None), note
    return (values[0] if values else None), ""


# ---- the plan ----------------------------------------------------------------------------------------


@dataclass
class SourceRow:
    """A feature of an input, as qgis_io/append_io.py reads it."""
    input: str
    #: The feature's number in its input (its feature id), to find it again.
    number: Any
    values: dict = field(default_factory=dict)
    #: Target field → the value of its expression (EXPRESSION rules), or an `ExpressionError`.
    expressions: dict = field(default_factory=dict)
    #: The geometry as the target takes it (already turned: its CRS, single or multi), or None.
    geometry: Any = None
    #: Why the geometry cannot go in (then the feature is skipped).
    geometry_problem: str = ""
    #: What was done to it (a multi with one part made single…).
    geometry_note: str = ""


@dataclass(frozen=True)
class ExpressionError:
    message: str


@dataclass
class Existing:
    """A feature of the target whose key an input feature has: its id and what an update would change (to take it back)."""
    id: Any
    attributes: dict = field(default_factory=dict)
    geometry: Any = None


@dataclass
class Options:
    schema_type: str = NO_TEST
    #: The target's key fields (matching features are updated); none: everything is added.
    match_fields: list = field(default_factory=list)
    update_geometry: bool = False
    #: Values outside a domain skip their feature (else they go as they are).
    enforce_domains: bool = True
    #: Text longer than the field takes is cut (else the feature is skipped).
    truncate: bool = False
    #: A failure takes back everything that went in.
    all_or_nothing: bool = True


@dataclass
class Planned:
    """What one input feature becomes."""
    input: str
    number: Any
    action: str
    #: Target field → value (an update: only the fields the map fills).
    values: dict = field(default_factory=dict)
    geometry: Any = None
    #: The target feature updated.
    target_id: Any = None
    reasons: list = field(default_factory=list)
    notes: list = field(default_factory=list)

    @property
    def label(self) -> str:
        return ACTION_NAMES[self.action]


@dataclass
class Plan:
    rows: list = field(default_factory=list)
    #: Inputs skipped whole (TEST_AND_SKIP) or why nothing goes (TEST), with why.
    skipped_inputs: dict = field(default_factory=dict)
    #: TEST failed: nothing is added.
    stopped: bool = False
    #: The target's features updated, as they were (to take them back): by id.
    before: dict = field(default_factory=dict)

    def count(self, action: str) -> int:
        return sum(1 for p in self.rows if p.action == action)

    def summary(self) -> str:
        if self.stopped:
            return "列が一致しない入力があるため中止（追加 0 件）"
        parts = [f"追加 {self.count(ADD):,} 件"]
        if self.count(UPDATE):
            parts.append(f"更新 {self.count(UPDATE):,} 件")
        parts.append(f"飛ばす {self.count(SKIP):,} 件")
        if self.skipped_inputs:
            parts.append(f"飛ばした入力 {len(self.skipped_inputs)} 個")
        return "・".join(parts)

    def reason_counts(self) -> dict:
        """Why features are skipped: reason (its kind, without the value) → how many."""
        out: dict = {}
        for p in self.rows:
            if p.action == SKIP:
                for r in p.reasons[:1]:
                    kind = re.sub(r"（.*?）$", "", r)
                    out[kind] = out.get(kind, 0) + 1
        return dict(sorted(out.items(), key=lambda kv: -kv[1]))

    @property
    def writes(self) -> list:
        return [p for p in self.rows if p.action in (ADD, UPDATE)]


def key_text(value: Any) -> Optional[str]:
    """A key value compared (text trimmed, 1 and 1.0 the same); None for an empty one (never matches)."""
    if value is None:
        return None
    if isinstance(value, str):
        t = value.strip()
        return t or None
    if isinstance(value, bool):
        return "1" if value else "0"
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    if isinstance(value, datetime):
        return str(_ms(value))
    return text_of(value)


def mapped_value(row: SourceRow, entry: FieldEntry, rule: Optional[InputRule]) -> tuple[Any, str, str]:
    """The value the map gives a target field for a feature, and why it has none (an expression's error) or a note."""
    if rule is None or rule.source == EMPTY:
        return None, "", ""
    if rule.source == VALUE:
        return (rule.value if rule.value != "" else None), "", ""
    if rule.source == EXPRESSION:
        v = row.expressions.get(entry.target)
        if isinstance(v, ExpressionError):
            return None, f"「{entry.target}」の式の誤り（{v.message}）", ""
        return v, "", ""
    if not rule.fields:
        return None, "", ""
    values = [_part(row.values.get(f), rule) for f in rule.fields]
    if len(values) == 1 and entry.merge in ("first", "last"):
        return values[0], "", ""
    v, note = merged(values, entry.merge, entry.delimiter)
    return v, "", (f"「{entry.target}」: {note}" if note else "")


def fills(rule: Optional[InputRule]) -> bool:
    return rule is not None and rule.source != EMPTY and not (rule.source == FIELDS and not rule.fields)


def plan(rows: Iterable[SourceRow], field_map: FieldMap, schema: TargetSchema, options: Options,
         existing: Optional[dict] = None, skipped_inputs: Optional[dict] = None) -> Plan:
    """
    What each input feature becomes: added, updated (`options.match_fields`:
    the target's features by key, `existing` key → [Existing]) or skipped,
    with the reasons. `skipped_inputs`: inputs left out whole (their name →
    why), as TEST_AND_SKIP leaves them; with TEST, any of them stops it all.
    """
    out = Plan(skipped_inputs=dict(skipped_inputs or {}))
    if options.schema_type == TEST and out.skipped_inputs:
        out.stopped = True
        return out
    keys = [schema.field(k) for k in options.match_fields]
    keys = [k for k in keys if k is not None]
    existing = existing or {}
    taken: set = set()
    entries = [e for e in field_map.entries if schema.field(e.target) is not None and schema.field(e.target).editable]
    for row in rows:
        if row.input in out.skipped_inputs:
            continue
        p = Planned(row.input, row.number, ADD)
        values: dict = {}
        filled: set = set()
        for entry in entries:
            target = schema.field(entry.target)
            rule = entry.rule(row.input)
            raw, problem, note = mapped_value(row, entry, rule)
            if problem:
                p.reasons.append(problem)
                continue
            if note:
                p.notes.append(note)
            if target.domain is not None and target.domain.coded is not None and isinstance(raw, str):
                # A coded value written by its name (「二」 for 2): its code.
                raw = in_domain(raw, target.domain)[0]
            c = convert(raw, target, options.truncate)
            if c.problem:
                coded = target.domain is not None and target.domain.coded is not None and isinstance(raw, str)
                # (Text for a coded field that is none of its names: said with the values it takes.)
                p.reasons.append(f"「{target.name}」の値がドメインにない（{raw}。使える値: {target.domain.text()}）" if coded else c.problem)
                continue
            if c.note:
                p.notes.append(c.note)
            value, allowed = in_domain(c.value, target.domain)
            if not allowed:
                text = f"「{target.name}」の値がドメインにない（{text_of(value)}。使える値: {target.domain.text()}）"
                if options.enforce_domains:
                    p.reasons.append(text)
                    continue
                p.notes.append(text)
            values[target.name] = value
            if fills(rule):
                filled.add(target.name)
        if row.geometry_problem:
            p.reasons.append(row.geometry_problem)
        elif row.geometry_note:
            p.notes.append(row.geometry_note)
        p.geometry = row.geometry
        match: list = []
        if keys and not p.reasons:
            key = tuple(key_text(values.get(k.name)) for k in keys)
            if all(k is not None for k in key):
                if key in taken:
                    p.reasons.append("入力の中でキーが重なる（先の 1 件だけ）")
                else:
                    taken.add(key)
                    match = existing.get(key) or []
                    if len(match) > 1:
                        p.reasons.append(f"追加先に同じキーの地物が {len(match)} 件ある")
        if not p.reasons and len(match) == 1:
            p.action = UPDATE
            p.target_id = match[0].id
            p.values = {k: v for k, v in values.items() if k in filled}
            if not options.update_geometry:
                p.geometry = None
            out.before[match[0].id] = match[0]
        else:
            # (An empty value is left out: the layer's default, or empty.)
            p.values = {k: v for k, v in values.items() if v is not None}
        if not p.reasons:
            for target in schema.mappable:
                if target.nullable or target.has_default:
                    continue
                if p.action == ADD and values.get(target.name) is None:
                    p.reasons.append(f"必須の列「{target.name}」が空")
                elif p.action == UPDATE and target.name in p.values and p.values[target.name] is None:
                    p.reasons.append(f"必須の列「{target.name}」が空")
        if p.reasons:
            p.action = SKIP
            p.values = {}
            p.target_id = None
        out.rows.append(p)
    return out


# ---- Esri targets -------------------------------------------------------------------------------------


def esri_schema(info: LayerInfo, tracked: Iterable[str] = ()) -> TargetSchema:
    """An Esri layer's fields and geometry (`tracked`: the fields the plugin stamps itself, editTracking)."""
    raw = info.raw
    oid = info.object_id_field.lower()
    stamped = {t.lower() for t in tracked}
    fields = []
    for f in raw.get("fields") or []:
        if not isinstance(f, dict) or not isinstance(f.get("name"), str):
            continue
        esri_type = str(f.get("type") or "")
        name = f["name"]
        automatic = ""
        if esri_type in ESRI_AUTOMATIC or name.lower() == oid:
            automatic = "サーバーが付ける"
        elif f.get("editable") is False:
            automatic = "サーバーが付ける"
        elif name.lower() in stamped:
            automatic = "作成者・更新者（自動）"
        domain = None
        d = f.get("domain")
        if isinstance(d, dict):
            if d.get("type") == "codedValue" and isinstance(d.get("codedValues"), list):
                coded = {c.get("code"): c.get("name", c.get("code")) for c in d["codedValues"] if isinstance(c, dict) and "code" in c}
                domain = Domain(coded=coded, name=str(d.get("name") or ""))
            elif d.get("type") == "range" and isinstance(d.get("range"), list) and len(d["range"]) == 2:
                low, high = d["range"]
                domain = Domain(low=float(low) if is_number(low) else None, high=float(high) if is_number(high) else None,
                                name=str(d.get("name") or ""))
        if not automatic and esri_type not in ESRI_TYPES:
            automatic = "書き込めない型"
        length = f.get("length") if isinstance(f.get("length"), int) and esri_type == "esriFieldTypeString" else 0
        fields.append(TargetField(
            name, ESRI_TYPES.get(esri_type, OTHER), str(f.get("alias") or ""), length or 0, nullable=f.get("nullable") is not False,
            editable=not automatic, automatic=automatic, domain=domain, has_default=f.get("defaultValue") is not None,
            type_text=esri_type.replace("esriFieldType", "")))
    return TargetSchema(fields, ESRI_GEOMETRIES.get(info.geometry_type, NO_GEOMETRY), str(raw.get("name") or ""),
                        bool(raw.get("hasZ")), bool(raw.get("hasM")))


def esri_value(value: Any, f: TargetField) -> Any:
    """A value as applyEdits sends it: a moment as ms since the epoch, a day as yyyy-mm-dd (DateOnly) or ms."""
    if isinstance(value, datetime):
        return _ms(value)
    if isinstance(value, date):
        if f.type_text == "DateOnly":
            return value.isoformat()
        return local_ms(value.year, value.month, value.day)
    return value


def _in_list(name: str, values: Sequence[Any]) -> str:
    texts = []
    for v in values:
        if isinstance(v, (int, float)) and not isinstance(v, bool):
            texts.append(_plain_number(float(v)) if isinstance(v, float) else str(v))
        else:
            texts.append(quote(str(v)))
    return f"{name} IN ({','.join(texts)})"


def esri_existing(client: EsriClient, url: str, token: Optional[str], schema: TargetSchema, match_fields: Sequence[str],
                  keys: Iterable[tuple], out_fields: Sequence[str], geometry: bool, check: Callable[[], None] = lambda: None) -> dict:
    """
    The target's features with these keys (key → [Existing]), with the
    fields an update changes (and the geometry) as they are, to take an
    update back. Asked 100 keys at a time by the first key field that is not
    a date (all of the layer when every key is a date).
    """
    fields = [schema.field(k) for k in match_fields]
    fields = [f for f in fields if f is not None]
    wanted = {k for k in keys if all(v is not None for v in k)}
    if not fields or not wanted:
        return {}
    info = client.layer(url, token)
    oid = info.object_id_field
    names = list(dict.fromkeys([oid, *(f.name for f in fields), *out_fields]))
    out: dict = {}
    by = next((i for i, f in enumerate(fields) if f.type not in (DATE, DATETIME)), None)

    def take(features: Iterable[dict]) -> None:
        for feature in features:
            a = feature.get("attributes") or {}
            lower = {k.lower(): v for k, v in a.items()}
            # As the plan's keys: the value made the field's type (a date's ms a day or a moment), then compared as text.
            key = tuple(key_text(convert(lower.get(f.name.lower()), replace(f, length=0)).value) for f in fields)
            if key in wanted:
                attributes = {n: a.get(n, lower.get(n.lower())) for n in out_fields}
                out.setdefault(key, []).append(Existing(lower.get(oid.lower()), attributes, feature.get("geometry") if geometry else None))

    if by is None:
        take(client.query_all(url, "1=1", token, out_fields=",".join(names), geometry=geometry, limit=1_000_000))
        return out
    values = sorted({k[by] for k in wanted})
    f = fields[by]
    for i in range(0, len(values), 100):
        check()
        chunk = values[i:i + 100]
        typed: list = []
        for v in chunk:
            if f.type in (INTEGER, DOUBLE):
                n = _number(v)
                typed.append(int(n) if n is not None and n.is_integer() and f.type == INTEGER else n)
            else:
                typed.append(v)
        where = _in_list(f.name, [t for t in typed if t is not None])
        take(client.query_all(url, where, token, out_fields=",".join(names), geometry=geometry, limit=1_000_000))
    return out


@dataclass
class Applied:
    """What a write did: the features added (their ids), updated (as they were before), failed and why; whether it was taken back."""
    added: list = field(default_factory=list)
    #: (id, attributes before, geometry before or None).
    updated: list = field(default_factory=list)
    #: (Planned, why).
    failed: list = field(default_factory=list)
    rolled_back: bool = False
    #: Taking back failed for some (what is left).
    undo_problems: list = field(default_factory=list)
    #: The edits wait in the layer's edit buffer (a QGIS layer being edited): saved by the person.
    in_buffer: bool = False
    #: Stopped part way (cancelled).
    cancelled: bool = False

    def text(self) -> str:
        if self.rolled_back:
            first = self.failed[0][1] if self.failed else ""
            left = f"。取り消せなかったもの: {'、'.join(self.undo_problems[:3])}" if self.undo_problems else ""
            return f"{len(self.failed):,} 件が失敗したため、今回の分をすべて取り消し（最初の理由: {first}）{left}"
        parts = [f"追加 {len(self.added):,} 件"]
        if self.updated:
            parts.append(f"更新 {len(self.updated):,} 件")
        if self.failed:
            parts.append(f"失敗 {len(self.failed):,} 件")
        text = "・".join(parts)
        if self.cancelled:
            text += "（途中で取消）"
        if self.in_buffer:
            text += "。編集中のレイヤーのため、保存は手動"
        return text


def _failure(result: dict) -> str:
    error = result.get("error") or {}
    return str(error.get("description") or error.get("message") or "サーバーが受け付けない")


def write_esri(client: EsriClient, url: str, token: Optional[str], the_plan: Plan, schema: TargetSchema, all_or_nothing: bool = True,
               check: Callable[[], None] = lambda: None, progress: Callable[[float], None] = lambda _p: None) -> Applied:
    """
    The plan's adds and updates sent to an Esri layer, EDIT_BATCH at a time
    (applyEdits: who and when, the audit log and the time zone as for every
    write of the plugin). `all_or_nothing`: on the first failure (a feature
    refused, a request that failed, 取消), what went in is taken back.
    """
    applied = Applied()
    writes = the_plan.writes
    oid = client.layer(url, token).object_id_field
    done = 0
    try:
        for start in range(0, len(writes), EDIT_BATCH):
            check()
            part = writes[start:start + EDIT_BATCH]
            adds = [p for p in part if p.action == ADD]
            updates = [p for p in part if p.action == UPDATE]
            result = client.apply_edits(url, adds=[_esri_feature(p, schema) for p in adds],
                                        updates=[_esri_feature(p, schema, oid) for p in updates], token=token)
            add_results, update_results = result.add_results, result.update_results
            for i, p in enumerate(adds):
                r = add_results[i] if i < len(add_results) else {"success": False}
                if r.get("success"):
                    applied.added.append(r.get("objectId"))
                else:
                    applied.failed.append((p, _failure(r)))
            for i, p in enumerate(updates):
                r = update_results[i] if i < len(update_results) else {"success": False}
                if r.get("success"):
                    before = the_plan.before.get(p.target_id)
                    applied.updated.append((p.target_id, dict(before.attributes) if before else {},
                                            before.geometry if before is not None and p.geometry is not None else None))
                else:
                    applied.failed.append((p, _failure(r)))
            done += len(part)
            progress(100.0 * done / max(1, len(writes)))
            if applied.failed and all_or_nothing:
                break
    except Exception as error:  # noqa: BLE001 (a request that failed: taken back when asked, else told)
        cancelled = type(error).__name__ in ("Cancelled", "QgsProcessingException") and "取消" in (str(error) or "取消")
        if not all_or_nothing:
            applied.cancelled = cancelled
            if not cancelled:
                applied.failed += [(p, str(error)) for p in writes[done:]]
            return applied
        applied.failed += [(p, "取消" if cancelled else str(error)) for p in writes[done:done + EDIT_BATCH]]
        applied.cancelled = cancelled
    if all_or_nothing and (applied.failed or applied.cancelled):
        applied.undo_problems = undo_esri(client, url, token, applied)
        applied.rolled_back = True
    return applied


def _esri_feature(p: Planned, schema: TargetSchema, oid: str = "") -> dict:
    attributes = {k: esri_value(v, schema.field(k)) for k, v in p.values.items()}
    if oid:
        attributes[oid] = p.target_id
    out: dict = {"attributes": attributes}
    if p.geometry is not None:
        out["geometry"] = p.geometry
    return out


def undo_esri(client: EsriClient, url: str, token: Optional[str], applied: Applied) -> list:
    """
    Takes back a write: the features it added deleted, those it updated given
    their values (and geometries) back. What could not be taken back.
    """
    problems = []
    oid = client.layer(url, token).object_id_field
    if applied.added:
        try:
            results = client.delete_features(url, applied.added, token)
            problems += [f"追加した {r.get('objectId')} を削除できない" for r in results if not r.get("success")]
        except EsriError as error:
            problems.append(f"追加した {len(applied.added):,} 件を削除できない（{error}）")
    if applied.updated:
        back = []
        for target_id, attributes, geometry in applied.updated:
            feature: dict = {"attributes": {**attributes, oid: target_id}}
            if geometry is not None:
                feature["geometry"] = geometry
            back.append(feature)
        try:
            result = client.apply_edits(url, updates=back, token=token)
            problems += [f"更新した {r.get('objectId')} を戻せない" for r in result.update_results if not r.get("success")]
        except EsriError as error:
            problems.append(f"更新した {len(applied.updated):,} 件を戻せない（{error}）")
    return problems


# ---- own tables --------------------------------------------------------------------------------------


def same_table(a: str, b: str) -> bool:
    """Whether two layer URLs are the same table (case, a trailing slash and a query aside)."""
    def clean(u: str) -> str:
        return (u or "").split("?", 1)[0].rstrip("/").lower()
    return bool(a) and clean(a) == clean(b)
