"""
A stand-in for Esri feature layers (the viewer's test/plan-store-fake.ts
grown into a general one): layer information, query (paging, object ids,
simple where clauses, a box filter), applyEdits and deleteFeatures, all from
memory. Used as the `Transport` of EsriClient.
"""
from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from typing import Any, Optional

from skywatch.core.esri import EsriError


class FakeLayer:
    def __init__(self, fields: dict[str, str], geometry: str = "esriGeometryPoint", max_record_count: int = 1000,
                 pagination: bool = True, oid: str = "OBJECTID", token: Optional[str] = None, ordered: bool = False):
        self.types = {oid: "esriFieldTypeOID", **fields}
        #: Whether queries follow `orderByFields` (else the rows stay in object id order).
        self.ordered = ordered
        self.geometry = geometry
        self.max_record_count = max_record_count
        self.pagination = pagination
        self.oid = oid
        self.token = token
        self.rows: list[dict[str, Any]] = []
        self.next = 1
        #: The layer's name, said when set.
        self.name: Optional[str] = None

    def info(self) -> dict[str, Any]:
        return {
            "currentVersion": 11.1,
            "objectIdField": self.oid,
            "geometryType": self.geometry,
            "maxRecordCount": self.max_record_count,
            "advancedQueryCapabilities": {"supportsPagination": self.pagination},
            "fields": [{"name": n, "type": t} for n, t in self.types.items()],
            **({"name": self.name} if self.name else {}),
        }

    def add(self, attributes: dict[str, Any], geometry: Any = None) -> int:
        oid = self.next
        self.next += 1
        self.rows.append({"attributes": {**attributes, self.oid: oid}, "geometry": geometry})
        return oid


def _split(text: str, word: str) -> list[str]:
    """`text` split at `word` (AND / OR) outside parentheses and quotes."""
    parts, depth, quoted, start, i = [], 0, False, 0, 0
    pattern = re.compile(rf"\s+{word}\s+", re.IGNORECASE)
    while i < len(text):
        ch = text[i]
        if ch == "'":
            quoted = not quoted
        elif not quoted and ch == "(":
            depth += 1
        elif not quoted and ch == ")":
            depth -= 1
        elif not quoted and depth == 0:
            m = pattern.match(text, i)
            if m:
                parts.append(text[start:i])
                start = i = m.end()
                continue
        i += 1
    parts.append(text[start:])
    return parts


def _unwrap(text: str) -> str:
    """`text` without parentheses around all of it."""
    text = text.strip()
    while text.startswith("(") and text.endswith(")"):
        depth = 0
        for i, ch in enumerate(text):
            depth += ch == "("
            depth -= ch == ")"
            if depth == 0 and i < len(text) - 1:
                return text
        text = text[1:-1].strip()
    return text


def _compare(v: Any, op: str, other: Any) -> bool:
    if v is None:
        return False
    try:
        return {"=": v == other, ">=": v >= other, "<=": v <= other, ">": v > other, "<": v < other, "<>": v != other}[op]
    except TypeError:
        return False


def _like(pattern: str, escape: Optional[str]) -> "re.Pattern[str]":
    """A SQL LIKE pattern (`%`, `_`, an escape character) as a regular expression."""
    out, i = [], 0
    while i < len(pattern):
        ch = pattern[i]
        if escape and ch == escape and i + 1 < len(pattern):
            out.append(re.escape(pattern[i + 1]))
            i += 2
            continue
        out.append(".*" if ch == "%" else "." if ch == "_" else re.escape(ch))
        i += 1
    return re.compile("".join(out), re.DOTALL)


def _matches(where: Optional[str], a: dict[str, Any]) -> bool:
    """
    `1=1`, `A = 'x'`, `A = 0`, `A IS NULL`, `A IN ('x', 'y')`, `A >= 'x'`,
    `F >= TIMESTAMP '…'`, `[UPPER(]A[)] LIKE '…' [ESCAPE '\\']`, joined by
    AND / OR with parentheses. Parts not understood match (as before).
    """
    if not where or where.strip() == "1=1":
        return True
    text = _unwrap(where)
    ors = _split(text, "OR")
    if len(ors) > 1:
        return any(_matches(p, a) for p in ors)
    ands = _split(text, "AND")
    if len(ands) > 1:
        return all(_matches(p, a) for p in ands)
    part = text
    m = re.fullmatch(r"(?:UPPER\((\w+)\)|(\w+))\s+LIKE\s+'((?:[^']|'')*)'(?:\s+ESCAPE\s+'(.)')?", part, re.IGNORECASE | re.DOTALL)
    if m:
        v = a.get(m.group(1) or m.group(2))
        if v is None:
            return False
        text_value = str(v).upper() if m.group(1) else str(v)
        return bool(_like(m.group(3).replace("''", "'"), m.group(4)).fullmatch(text_value))
    m = re.fullmatch(r"(\w+)\s+IS\s+NULL", part, re.IGNORECASE)
    if m:
        return a.get(m.group(1)) is None
    m = re.fullmatch(r"(\w+)\s+IN\s*\((.*)\)", part, re.IGNORECASE | re.DOTALL)
    if m:
        values = [v.replace("''", "'") for v in re.findall(r"'((?:[^']|'')*)'", m.group(2))]
        return str(a.get(m.group(1))) in values
    m = re.fullmatch(r"(\w+)\s*(>=|<=|<>|=|>|<)\s*TIMESTAMP\s*'(.+)'", part)
    if m:
        t = datetime.fromisoformat(m.group(3).replace(" ", "T")).replace(tzinfo=timezone.utc).timestamp() * 1000
        return _compare(a.get(m.group(1)), m.group(2), t)
    m = re.fullmatch(r"(\w+)\s*(>=|<=|<>|=|>|<)\s*'(.*)'", part, re.DOTALL)
    if m:
        v = a.get(m.group(1))
        other = m.group(3).replace("''", "'")
        if m.group(2) == "=":
            return str(v) == other
        return _compare(None if v is None else str(v), m.group(2), other)
    m = re.fullmatch(r"(\w+)\s*(>=|<=|<>|=|>|<)\s*(-?\d+(?:\.\d+)?)", part)
    if m:
        return _compare(a.get(m.group(1)), m.group(2), float(m.group(3)))
    return True


def _box(geometry: dict[str, Any]) -> tuple[float, float, float, float]:
    """The bounding box of an Esri point, multipoint, envelope, polyline or polygon."""
    if "rings" in geometry or "paths" in geometry or "points" in geometry:
        points = ([p for ring in geometry["rings"] for p in ring] if "rings" in geometry
                  else [p for path in geometry["paths"] for p in path] if "paths" in geometry else geometry["points"])
        xs, ys = [p[0] for p in points], [p[1] for p in points]
        return min(xs), min(ys), max(xs), max(ys)
    if "xmin" in geometry:
        return geometry["xmin"], geometry["ymin"], geometry["xmax"], geometry["ymax"]
    return geometry["x"], geometry["y"], geometry["x"], geometry["y"]


def _overlaps(a, b) -> bool:
    return a[0] <= b[2] and b[0] <= a[2] and a[1] <= b[3] and b[1] <= a[3]


def _ordered(rows: list[dict[str, Any]], fields: str) -> list[dict[str, Any]]:
    """`rows` in the order of `orderByFields` (`A DESC, B`; empty values last)."""
    for part in reversed([p.strip() for p in fields.split(",") if p.strip()]):
        name, _, way = part.partition(" ")
        descending = way.strip().upper() == "DESC"
        present = [r for r in rows if r["attributes"].get(name) is not None]
        missing = [r for r in rows if r["attributes"].get(name) is None]
        rows = sorted(present, key=lambda r: r["attributes"][name], reverse=descending) + missing
    return rows


class FakeEsri:
    """Feature layers by URL; call it as a transport."""

    def __init__(self):
        self.layers: dict[str, FakeLayer] = {}
        self.asked: list[tuple[str, dict[str, str]]] = []
        self.fail_next: Optional[dict[str, Any]] = None

    def add_layer(self, url: str, layer: FakeLayer) -> FakeLayer:
        self.layers[url] = layer
        return layer

    def __call__(self, url: str, form: dict[str, str]) -> bytes:
        self.asked.append((url, dict(form)))
        if self.fail_next is not None:
            answer, self.fail_next = self.fail_next, None
            return json.dumps(answer).encode()
        return json.dumps(self.answer(url, form), ensure_ascii=False).encode("utf-8")

    def answer(self, url: str, form: dict[str, str]) -> dict[str, Any]:
        base, _, op = url.rpartition("/")
        layer = self.layers.get(url)
        if layer is not None:
            return self._guard(layer, form) or layer.info()
        layer = self.layers.get(base)
        if layer is None:
            raise EsriError(f"接続不可（{url}）")
        refused = self._guard(layer, form)
        if refused:
            return refused
        if op == "query":
            return self._query(layer, form)
        if op == "applyEdits":
            return self._apply(layer, form)
        if op == "deleteFeatures":
            ids = {int(i) for i in form.get("objectIds", "").split(",") if i}
            if form.get("where"):
                ids |= {r["attributes"][layer.oid] for r in layer.rows if _matches(form["where"], r["attributes"])}
            results = [{"objectId": i, "success": any(r["attributes"][layer.oid] == i for r in layer.rows)} for i in ids]
            layer.rows = [r for r in layer.rows if r["attributes"][layer.oid] not in ids]
            return {"deleteResults": results}
        raise EsriError(f"接続不可（{url}）")

    @staticmethod
    def _guard(layer: FakeLayer, form: dict[str, str]) -> Optional[dict[str, Any]]:
        if layer.token and form.get("token") != layer.token:
            return {"error": {"code": 498 if form.get("token") else 499, "message": "Invalid token."}}
        return None

    def _query(self, layer: FakeLayer, form: dict[str, str]) -> dict[str, Any]:
        found = [r for r in layer.rows if _matches(form.get("where"), r["attributes"])]
        if form.get("objectIds"):
            ids = {int(i) for i in form["objectIds"].split(",")}
            if len(ids) > layer.max_record_count:
                return {"error": {"code": 400, "message": "Too many ids"}}
            found = [r for r in found if r["attributes"][layer.oid] in ids]
        if form.get("geometry"):
            area = _box(json.loads(form["geometry"]))
            found = [r for r in found if r["geometry"] and _overlaps(area, _box(r["geometry"]))]
        if form.get("returnCountOnly") == "true":
            return {"count": len(found)}
        # orderByFields is followed only by an `ordered` layer: the rows stay in object id order otherwise
        # (what query_all asks for when paging).
        if layer.ordered and form.get("orderByFields"):
            found = _ordered(found, form["orderByFields"])
        if form.get("returnIdsOnly") == "true":
            return {"objectIdFieldName": layer.oid, "objectIds": [r["attributes"][layer.oid] for r in found]}
        offset = int(form.get("resultOffset", 0))
        if "resultOffset" in form and not layer.pagination:
            return {"error": {"code": 400, "message": "Pagination is not supported."}}
        count = min(int(form.get("resultRecordCount", layer.max_record_count)), layer.max_record_count)
        page = found[offset:offset + count]
        geometry = form.get("returnGeometry") != "false"
        return {
            "features": [{"attributes": dict(r["attributes"]), **({"geometry": r["geometry"]} if geometry else {})} for r in page],
            "exceededTransferLimit": offset + count < len(found),
        }

    def _apply(self, layer: FakeLayer, form: dict[str, str]) -> dict[str, Any]:
        adds = json.loads(form.get("adds") or "[]")
        updates = json.loads(form.get("updates") or "[]")
        deletes = [int(i) for i in (form.get("deletes") or "").split(",") if i]
        add_results = []
        for f in adds:
            attributes = {k: v for k, v in f["attributes"].items() if k in layer.types and k != layer.oid}
            add_results.append({"success": True, "objectId": layer.add(attributes, f.get("geometry"))})
        update_results = []
        for f in updates:
            oid = f["attributes"].get(layer.oid)
            row = next((r for r in layer.rows if r["attributes"][layer.oid] == oid), None)
            if row is None:
                update_results.append({"success": False, "objectId": oid, "error": {"description": "見つからない地物"}})
                continue
            row["attributes"].update({k: v for k, v in f["attributes"].items() if k in layer.types})
            if f.get("geometry") is not None:
                row["geometry"] = f["geometry"]
            update_results.append({"success": True, "objectId": oid})
        delete_results = [{"objectId": i, "success": any(r["attributes"][layer.oid] == i for r in layer.rows)} for i in deletes]
        layer.rows = [r for r in layer.rows if r["attributes"][layer.oid] not in deletes]
        return {"addResults": add_results, "updateResults": update_results, "deleteResults": delete_results}
