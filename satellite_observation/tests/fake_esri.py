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

from satellite_observation.core.esri import EsriError


class FakeLayer:
    def __init__(self, fields: dict[str, str], geometry: str = "esriGeometryPoint", max_record_count: int = 1000,
                 pagination: bool = True, oid: str = "OBJECTID", token: Optional[str] = None):
        self.types = {oid: "esriFieldTypeOID", **fields}
        self.geometry = geometry
        self.max_record_count = max_record_count
        self.pagination = pagination
        self.oid = oid
        self.token = token
        self.rows: list[dict[str, Any]] = []
        self.next = 1

    def info(self) -> dict[str, Any]:
        return {
            "currentVersion": 11.1,
            "objectIdField": self.oid,
            "geometryType": self.geometry,
            "maxRecordCount": self.max_record_count,
            "advancedQueryCapabilities": {"supportsPagination": self.pagination},
            "fields": [{"name": n, "type": t} for n, t in self.types.items()],
        }

    def add(self, attributes: dict[str, Any], geometry: Any = None) -> int:
        oid = self.next
        self.next += 1
        self.rows.append({"attributes": {**attributes, self.oid: oid}, "geometry": geometry})
        return oid


def _matches(where: Optional[str], a: dict[str, Any]) -> bool:
    """`1=1`, `A = 'x'`, `A IS NULL`, `F >= TIMESTAMP '…'` joined by AND."""
    if not where or where.strip() == "1=1":
        return True
    for part in re.split(r"\s+AND\s+", where.strip(), flags=re.IGNORECASE):
        part = part.strip().strip("()")
        m = re.fullmatch(r"(\w+)\s*=\s*'(.*)'", part)
        if m:
            if str(a.get(m.group(1))) != m.group(2).replace("''", "'"):
                return False
            continue
        m = re.fullmatch(r"(\w+)\s+IS\s+NULL", part, re.IGNORECASE)
        if m:
            if a.get(m.group(1)) is not None:
                return False
            continue
        m = re.fullmatch(r"(\w+)\s*(>=|<=)\s*TIMESTAMP\s*'(.+)'", part)
        if m:
            t = datetime.fromisoformat(m.group(3).replace(" ", "T")).replace(tzinfo=timezone.utc).timestamp() * 1000
            v = a.get(m.group(1))
            if v is None or not (v >= t if m.group(2) == ">=" else v <= t):
                return False
    return True


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
