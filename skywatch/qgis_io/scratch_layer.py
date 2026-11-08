"""
A new scratch layer (a memory layer, as QGIS's 「新規一時スクラッチレイヤー」)
from a checked design: the attributes with their aliases and lengths,
「必須」 as a not-null constraint, the range and the allowed values as the
attribute form's widgets (Range, Value Map) with matching constraints, and
the default as the field's default value. Also: an existing layer's
attributes as a design (for copying), and the designs kept in QgsSettings.
"""
from __future__ import annotations

import json
from typing import Optional

from qgis.core import (
    QgsCoordinateReferenceSystem, QgsDefaultValue, QgsEditorWidgetSetup, QgsFieldConstraints, QgsSettings, QgsVectorLayer,
    QgsWkbTypes,
)

from ..core.layer_templates import FieldDesign, FieldSpec, LayerTemplate, template_json, template_of
from .plan_layer import make_field
from .settings import PREFIX

SAVED_KEY = PREFIX + "layer_templates"


def _literal(value) -> str:
    """A value as a QGIS expression."""
    if isinstance(value, str):
        return "'" + value.replace("'", "''") + "'"
    return repr(value)


def _codes_expression(name: str, codes) -> str:
    quoted = '"' + name.replace('"', '""') + '"'
    return f"{quoted} IS NULL OR {quoted} IN ({', '.join(_literal(c) for c, _ in codes)})"


def _range_expression(name: str, lo, hi) -> str:
    quoted = '"' + name.replace('"', '""') + '"'
    parts = ([f"{quoted} >= {_literal(lo)}"] if lo is not None else []) + ([f"{quoted} <= {_literal(hi)}"] if hi is not None else [])
    return f"{quoted} IS NULL OR ({' AND '.join(parts)})"


def scratch_layer(title: str, geometry: str, crs: QgsCoordinateReferenceSystem, specs: list[FieldSpec]) -> QgsVectorLayer:
    """An empty memory layer of a checked design (`geometry`: Point, LineString… as GEOMETRY_NAMES)."""
    uri = f"{geometry}?crs={crs.authid()}" if crs.authid() else geometry
    layer = QgsVectorLayer(uri, title.strip(), "memory")
    if not crs.authid() and crs.isValid():
        layer.setCrs(crs)
    fields = []
    for s in specs:
        f = make_field(s.name, s.type)
        if s.length:
            f.setLength(s.length)
        fields.append(f)
    layer.dataProvider().addAttributes(fields)
    layer.updateFields()
    for i, s in enumerate(specs):
        if s.alias != s.name:
            layer.setFieldAlias(i, s.alias)
        if s.required:
            layer.setFieldConstraint(i, QgsFieldConstraints.Constraint.ConstraintNotNull, QgsFieldConstraints.ConstraintStrength.ConstraintStrengthHard)
        if s.codes:
            # The value map keeps values as text; the names are what the form shows.
            layer.setEditorWidgetSetup(i, QgsEditorWidgetSetup("ValueMap", {"map": [{name: str(code)} for code, name in s.codes]}))
            layer.setConstraintExpression(i, _codes_expression(s.name, s.codes), "候補値から選択")
        elif s.range:
            lo, hi = s.range
            config = {"AllowNull": not s.required, "Style": "SpinBox"}
            if lo is not None:
                config["Min"] = lo
            if hi is not None:
                config["Max"] = hi
            if s.type == "double":
                config["Precision"] = 6
                config["Step"] = 1.0
            layer.setEditorWidgetSetup(i, QgsEditorWidgetSetup("Range", config))
            layer.setConstraintExpression(i, _range_expression(s.name, lo, hi), f"{'' if lo is None else lo}〜{'' if hi is None else hi} の範囲で入力")
        elif s.type == "date":
            layer.setEditorWidgetSetup(i, QgsEditorWidgetSetup("DateTime", {
                "field_format": "yyyy-MM-dd HH:mm:ss", "display_format": "yyyy-MM-dd HH:mm:ss", "calendar_popup": True, "allow_null": True}))
        if s.default is not None:
            expression = f"to_datetime({_literal(s.default)})" if s.type == "date" else _literal(s.default)
            layer.setDefaultValueDefinition(i, QgsDefaultValue(expression))
    return layer


#: Whole-number QMetaType / QVariant type ids (Int, UInt, LongLong, ULongLong; Long, Short, UShort, Char…).
_INTEGER_TYPES = {2, 3, 4, 5, 32, 33, 34, 35, 36, 37, 40}


def _design_type(field) -> str:
    """The design type of a field of another layer (whole numbers, numbers, dates and times, the rest text)."""
    if field.isNumeric():
        return "integer" if int(field.type()) in _INTEGER_TYPES else "double"
    return "date" if field.isDateOrTime() else "string"


def _number(v) -> Optional[float]:
    try:
        n = float(v)
    except (TypeError, ValueError):
        return None
    return n if n == n and abs(n) != float("inf") else None


def field_design_of(layer: QgsVectorLayer, index: int) -> FieldDesign:
    """The design of an existing layer's attribute, for copying (its alias, not-null, length, Range or Value Map)."""
    f = layer.fields().at(index)
    type_ = _design_type(f)
    design = FieldDesign(name=f.name(), type=type_, alias=f.alias() if f.alias() and f.alias() != f.name() else "")
    design.required = bool(f.constraints().constraints() & QgsFieldConstraints.Constraint.ConstraintNotNull)
    if type_ == "string" and f.length() > 0:
        design.length = f.length()
    widget = f.editorWidgetSetup()
    config = widget.config() or {}
    if widget.type() == "ValueMap":
        entries = config.get("map")
        pairs: list[tuple[str, str]] = []
        if isinstance(entries, dict):
            pairs = [(str(v), str(k)) for k, v in entries.items()]
        elif isinstance(entries, list):
            pairs = [(str(v), str(k)) for item in entries if isinstance(item, dict) for k, v in item.items()]
        design.codes = ", ".join(name if code == name else f"{code}={name}" for code, name in pairs if not code.startswith("{2839923C"))
    elif widget.type() == "Range" and type_ in ("integer", "double"):
        design.min, design.max = _number(config.get("Min")), _number(config.get("Max"))
    return design


def geometry_of(layer: QgsVectorLayer) -> Optional[str]:
    """The design geometry of a layer (Point, MultiPolygon…), None for a table or other types."""
    flat = QgsWkbTypes.flatType(layer.wkbType())
    name = QgsWkbTypes.displayString(flat)
    return name if name in ("Point", "LineString", "Polygon", "MultiPoint", "MultiLineString", "MultiPolygon") else None


def saved_templates() -> list[LayerTemplate]:
    """The designs kept by the person (QgsSettings)."""
    value = QgsSettings().value(SAVED_KEY, "")
    try:
        items = json.loads(value) if isinstance(value, str) and value else []
    except ValueError:
        return []
    if not isinstance(items, list):
        return []
    return [t for t in (template_of(v, [], str(i)) for i, v in enumerate(items)) if t is not None]


def _store(templates: list[LayerTemplate]) -> None:
    QgsSettings().setValue(SAVED_KEY, json.dumps([template_json(t) for t in templates], ensure_ascii=False))


def save_template(template: LayerTemplate) -> None:
    """Keeps `template`, replacing one of the same name (put last, as the viewer)."""
    _store([t for t in saved_templates() if t.name != template.name] + [template])


def remove_template(name: str) -> None:
    """Removes the kept design named `name`."""
    _store([t for t in saved_templates() if t.name != name])
