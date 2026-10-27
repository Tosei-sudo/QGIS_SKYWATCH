"""
ArcGIS REST (feature services): layer information, queries that read every
feature (paging past `maxRecordCount`), `applyEdits` and `deleteFeatures`.

The network is injected (`Transport`), so the tests drive this with a fake
server and the plugin with QgsNetworkAccessManager (qgis_io/network.py),
which follows QGIS's proxy and authentication settings. Every request is a
form POST with `f=json`, so a token stays out of URLs; error messages never
hold the token.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Callable, Iterable, Optional, Protocol
from urllib.parse import urlencode

from .util import iso_text, is_number


class EsriError(Exception):
    """The service could not be asked, or answered with an error."""


class InvalidTokenError(EsriError):
    """The server called the token invalid or expired (498, 499)."""


class Transport(Protocol):
    def __call__(self, url: str, form: dict[str, str]) -> bytes:
        """POSTs `form` (x-www-form-urlencoded) to `url` and returns the body; raises EsriError when it cannot."""
        ...


#: Asks for a fresh token after the server refused one (OAuth2 renewal); None when none can be had.
Renew = Callable[[str], Optional[str]]


def form_body(form: dict[str, str]) -> bytes:
    """The body of a form POST."""
    return urlencode(form).encode("utf-8")


def _redact(text: str, token: Optional[str]) -> str:
    return text.replace(token, "…") if token else text


class EsriClient:
    """Asks Esri feature layers for JSON."""

    def __init__(self, transport: Transport, renew: Optional[Renew] = None):
        self.transport = transport
        self.renew = renew
        self._layers: dict[str, dict[str, Any]] = {}

    def ask(self, url: str, params: Optional[dict[str, str]] = None, token: Optional[str] = None) -> dict[str, Any]:
        """The JSON answer of a request; raises EsriError with the reason when there is none."""
        try:
            return self._ask(url, params or {}, token)
        except InvalidTokenError:
            fresh = self.renew(url) if self.renew else None
            if not fresh:
                raise
            return self._ask(url, params or {}, fresh)

    def _ask(self, url: str, params: dict[str, str], token: Optional[str]) -> dict[str, Any]:
        form = {**params, "f": "json"}
        if token:
            form["token"] = token
        try:
            body = self.transport(url, form)
        except EsriError as error:
            raise EsriError(_redact(str(error), token)) from None
        try:
            value = json.loads(body.decode("utf-8-sig") if isinstance(body, (bytes, bytearray)) else body)
        except (ValueError, UnicodeDecodeError):
            raise EsriError("応答が JSON 以外") from None
        if not isinstance(value, dict):
            raise EsriError("応答が JSON 以外")
        error = value.get("error")
        if isinstance(error, dict):
            code = error.get("code")
            message = _redact(str(error.get("message") or ""), token)
            details = [_redact(str(d), token) for d in error.get("details") or [] if d]
            if token and code in (498, 499):
                raise InvalidTokenError(f"トークンが無効か、期限切れ（{message}）")
            if code in (498, 499, 403):
                raise EsriError(f"トークンが無効か、権限なし（{message}）" if token else "このサービスにはトークンが必要")
            raise EsriError("サービスからエラー応答: " + " ".join([message, *details]).strip())
        return value

    def layer(self, url: str, token: Optional[str] = None, refresh: bool = False) -> "LayerInfo":
        """The layer's information (asked once per URL)."""
        if refresh or url not in self._layers:
            self._layers[url] = self.ask(url, {}, token)
        return LayerInfo(self._layers[url])

    def query_all(
        self,
        url: str,
        where: str = "1=1",
        token: Optional[str] = None,
        out_fields: str = "*",
        geometry: bool = True,
        extra: Optional[dict[str, str]] = None,
        page: int = 2000,
        limit: int = 1_000_000,
        cancelled: Optional[Callable[[], bool]] = None,
    ) -> list[dict[str, Any]]:
        """
        Every feature matching `where` (in WGS 84 when `geometry`): pages of
        `resultOffset` while the layer pages, else the object ids first and
        the features by ids, `maxRecordCount` at a time.
        """
        info = self.layer(url, token)
        params = {"where": where or "1=1", "outFields": out_fields, "returnGeometry": "true" if geometry else "false", **(extra or {})}
        if geometry:
            params["outSR"] = "4326"
        size = max(1, min(page, info.max_record_count or page))
        out: list[dict[str, Any]] = []
        if info.supports_pagination:
            offset = 0
            while offset < limit:
                if cancelled and cancelled():
                    raise EsriError("取消")
                json_ = self.ask(f"{url}/query", {**params, "resultOffset": str(offset), "resultRecordCount": str(size)}, token)
                features = json_.get("features") or []
                out.extend(features)
                # Servers that page say so; some leave the flag out on a full page.
                more = bool(json_.get("exceededTransferLimit")) or len(features) >= size
                if not features or not more:
                    break
                offset += len(features)
            return out
        ids_json = self.ask(f"{url}/query", {"where": params["where"], "returnIdsOnly": "true", **(extra or {})}, token)
        ids = sorted(i for i in ids_json.get("objectIds") or [] if is_number(i))[:limit]
        for start in range(0, len(ids), size):
            if cancelled and cancelled():
                raise EsriError("取消")
            chunk = ids[start:start + size]
            json_ = self.ask(f"{url}/query", {**params, "objectIds": ",".join(str(int(i)) for i in chunk)}, token)
            out.extend(json_.get("features") or [])
        return out

    def apply_edits(
        self,
        url: str,
        adds: Iterable[dict[str, Any]] = (),
        updates: Iterable[dict[str, Any]] = (),
        deletes: Iterable[int] = (),
        token: Optional[str] = None,
    ) -> "EditResults":
        """Adds, updates and deletes features in one request (`rollbackOnFailure=false`)."""
        params = {"rollbackOnFailure": "false"}
        adds, updates, deletes = list(adds), list(updates), list(deletes)
        if adds:
            params["adds"] = json.dumps(adds, ensure_ascii=False)
        if updates:
            params["updates"] = json.dumps(updates, ensure_ascii=False)
        if deletes:
            params["deletes"] = ",".join(str(int(d)) for d in deletes)
        value = self.ask(f"{url}/applyEdits", params, token)
        return EditResults(value)

    def delete_features(self, url: str, object_ids: Iterable[int], token: Optional[str] = None) -> list[dict[str, Any]]:
        """Deletes features by object id; the results one by one."""
        value = self.ask(f"{url}/deleteFeatures", {"objectIds": ",".join(str(int(i)) for i in object_ids)}, token)
        return value.get("deleteResults") or []


@dataclass
class LayerInfo:
    """What a layer says of itself."""
    raw: dict[str, Any]

    @property
    def field_types(self) -> dict[str, str]:
        return {f["name"]: f.get("type", "") for f in self.raw.get("fields") or [] if isinstance(f, dict) and "name" in f}

    @property
    def object_id_field(self) -> str:
        oid = self.raw.get("objectIdField")
        if isinstance(oid, str) and oid:
            return oid
        for f in self.raw.get("fields") or []:
            if isinstance(f, dict) and f.get("type") == "esriFieldTypeOID":
                return f["name"]
        return "OBJECTID"

    @property
    def max_record_count(self) -> int:
        n = self.raw.get("maxRecordCount")
        return int(n) if is_number(n) and n > 0 else 1000

    @property
    def supports_pagination(self) -> bool:
        caps = self.raw.get("advancedQueryCapabilities")
        if isinstance(caps, dict) and "supportsPagination" in caps:
            return bool(caps["supportsPagination"])
        # Layers that do not say: ArcGIS 10.3 and later page.
        return is_number(self.raw.get("currentVersion")) and self.raw["currentVersion"] >= 10.3

    @property
    def geometry_type(self) -> str:
        return str(self.raw.get("geometryType") or "")

    def has(self, name: str) -> bool:
        return name in self.field_types


@dataclass
class EditResults:
    """The answer of `applyEdits`."""
    raw: dict[str, Any]

    @property
    def add_results(self) -> list[dict[str, Any]]:
        return self.raw.get("addResults") or []

    @property
    def update_results(self) -> list[dict[str, Any]]:
        return self.raw.get("updateResults") or []

    @property
    def delete_results(self) -> list[dict[str, Any]]:
        return self.raw.get("deleteResults") or []

    def failures(self) -> list[str]:
        """Why each failed edit failed."""
        out = []
        for name, results in (("追加失敗", self.add_results), ("更新失敗", self.update_results), ("削除失敗", self.delete_results)):
            for r in results:
                if not r.get("success"):
                    error = r.get("error") or {}
                    out.append(error.get("description") or name)
        return out


def fit_value(value: Any, field_type: Optional[str]) -> Any:
    """
    A value as a field of `field_type` takes it (as the viewer writes):
    booleans as 1 / 0 for integers or あり / なし for text, times (ms) as
    ISO 8601 for text.
    """
    if value is None:
        return None
    if isinstance(value, bool):
        if field_type and "Integer" in field_type:
            return 1 if value else 0
        if field_type == "esriFieldTypeString":
            return "あり" if value else "なし"
        return value
    if field_type == "esriFieldTypeString" and is_number(value) and value > 1e11:
        return iso_text(value)
    return value


def quote(text: str) -> str:
    """An SQL string literal."""
    return "'" + str(text).replace("'", "''") + "'"


def ring_geometry(rings: list[list[list[float]]]) -> dict[str, Any]:
    """An Esri polygon of longitude / latitude rings."""
    return {"rings": rings, "spatialReference": {"wkid": 4326}}


def point_geometry(lon: float, lat: float) -> dict[str, Any]:
    """An Esri point of a longitude / latitude."""
    return {"x": lon, "y": lat, "spatialReference": {"wkid": 4326}}
