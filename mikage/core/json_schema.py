"""
Checking a JSON value against a JSON Schema, for the parts schemas of
ordering systems use (no library needed in a closed network): type,
required, properties, additionalProperties, items, enum, const, pattern,
minimum / maximum (and exclusive ones), minLength / maxLength, minItems /
maxItems, format (date, date-time), anyOf / oneOf / allOf, and `$ref` to
`#/$defs/…` or `#/definitions/…`. Keywords starting `x-` are ignored.
"""
from __future__ import annotations

import re
from typing import Any

_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_DATE_TIME = re.compile(r"^\d{4}-\d{2}-\d{2}[Tt ]\d{2}:\d{2}:\d{2}(\.\d+)?([Zz]|[+-]\d{2}:\d{2})$")


def _type_ok(value: Any, t: str) -> bool:
    if t == "null":
        return value is None
    if t == "boolean":
        return isinstance(value, bool)
    if t == "integer":
        return isinstance(value, int) and not isinstance(value, bool) or isinstance(value, float) and value.is_integer()
    if t == "number":
        return isinstance(value, (int, float)) and not isinstance(value, bool)
    if t == "string":
        return isinstance(value, str)
    if t == "array":
        return isinstance(value, list)
    if t == "object":
        return isinstance(value, dict)
    return True


def resolve(schema: Any, root: Any) -> Any:
    """A schema with its `$ref` (local) followed."""
    seen = 0
    while isinstance(schema, dict) and isinstance(schema.get("$ref"), str) and seen < 20:
        ref = schema["$ref"]
        if not ref.startswith("#/"):
            return {}
        node = root
        for part in ref[2:].split("/"):
            node = node.get(part.replace("~1", "/").replace("~0", "~")) if isinstance(node, dict) else None
        schema = node if isinstance(node, dict) else {}
        seen += 1
    return schema


def validate(value: Any, schema: Any, root: Any = None, path: str = "$") -> list[str]:
    """What is wrong with `value` (texts with where), none when it fits."""
    root = schema if root is None else root
    schema = resolve(schema, root)
    if schema is True or not isinstance(schema, dict):
        return []
    errors: list[str] = []
    t = schema.get("type")
    if t is not None:
        types = t if isinstance(t, list) else [t]
        if not any(_type_ok(value, x) for x in types):
            return [f"{path}: 型が {'・'.join(types)} 以外"]
    if "const" in schema and value != schema["const"]:
        errors.append(f"{path}: {schema['const']!r} 以外の値")
    if isinstance(schema.get("enum"), list) and value not in schema["enum"]:
        errors.append(f"{path}: {value!r} は {schema['enum']} 以外")
    if isinstance(value, str):
        if isinstance(schema.get("minLength"), int) and len(value) < schema["minLength"]:
            errors.append(f"{path}: {schema['minLength']} 文字未満")
        if isinstance(schema.get("maxLength"), int) and len(value) > schema["maxLength"]:
            errors.append(f"{path}: {schema['maxLength']} 文字超過")
        if isinstance(schema.get("pattern"), str):
            try:
                if not re.search(schema["pattern"], value):
                    errors.append(f"{path}: 形式（{schema['pattern']}）に不一致")
            except re.error:
                pass
        fmt = schema.get("format")
        if fmt == "date" and not _DATE.match(value):
            errors.append(f"{path}: 日付（YYYY-MM-DD）以外")
        if fmt == "date-time" and not _DATE_TIME.match(value):
            errors.append(f"{path}: 日時（ISO 8601）以外")
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        if "minimum" in schema and value < schema["minimum"]:
            errors.append(f"{path}: {schema['minimum']} 未満")
        if "maximum" in schema and value > schema["maximum"]:
            errors.append(f"{path}: {schema['maximum']} 超過")
        if "exclusiveMinimum" in schema and isinstance(schema["exclusiveMinimum"], (int, float)) and value <= schema["exclusiveMinimum"]:
            errors.append(f"{path}: {schema['exclusiveMinimum']} 以下")
        if "exclusiveMaximum" in schema and isinstance(schema["exclusiveMaximum"], (int, float)) and value >= schema["exclusiveMaximum"]:
            errors.append(f"{path}: {schema['exclusiveMaximum']} 以上")
    if isinstance(value, list):
        if isinstance(schema.get("minItems"), int) and len(value) < schema["minItems"]:
            errors.append(f"{path}: 要素が {schema['minItems']} 個未満")
        if isinstance(schema.get("maxItems"), int) and len(value) > schema["maxItems"]:
            errors.append(f"{path}: 要素が {schema['maxItems']} 個超過")
        items = schema.get("items")
        if isinstance(items, dict):
            for i, v in enumerate(value):
                errors += validate(v, items, root, f"{path}[{i}]")
    if isinstance(value, dict):
        for name in schema.get("required") or []:
            if name not in value:
                errors.append(f"{path}: 必須の {name} なし")
        props = schema.get("properties") if isinstance(schema.get("properties"), dict) else {}
        for name, v in value.items():
            if name in props:
                errors += validate(v, props[name], root, f"{path}.{name}")
            elif schema.get("additionalProperties") is False:
                errors.append(f"{path}: 定義にない {name}")
            elif isinstance(schema.get("additionalProperties"), dict):
                errors += validate(v, schema["additionalProperties"], root, f"{path}.{name}")
    for sub in schema.get("allOf") or []:
        errors += validate(value, sub, root, path)
    if isinstance(schema.get("anyOf"), list) and schema["anyOf"]:
        if all(validate(value, sub, root, path) for sub in schema["anyOf"]):
            errors.append(f"{path}: anyOf のどれにも不一致")
    if isinstance(schema.get("oneOf"), list) and schema["oneOf"]:
        n = sum(1 for sub in schema["oneOf"] if not validate(value, sub, root, path))
        if n != 1:
            errors.append(f"{path}: oneOf に {n} 個一致（1 個のみ可）")
    return errors
