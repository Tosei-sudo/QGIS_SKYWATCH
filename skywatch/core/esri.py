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
import re
from dataclasses import dataclass
from typing import Any, Callable, Iterable, Optional, Protocol
from urllib.parse import urlencode

from . import edit_stamp, time_zone
from .util import iso_text, is_number


class EsriError(Exception):
    """The service could not be asked, or answered with an error."""


class TooManyError(EsriError):
    """A query matched more features than the caller's limit (read nothing rather than a part)."""


class ConflictError(EsriError):
    """Someone else changed (or deleted) the feature since it was read: it is not overwritten (#74)."""


class UnreachableError(EsriError):
    """
    The server could not be reached (no connection, no answer in time, or a
    5xx error): the plugin then works offline (core/offline.py) until it answers again.
    """


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
        """
        The JSON answer of a request; raises EsriError with the reason when
        there is none. A layer whose dates are in another zone than the service
        says (the settings file's timeZone) has them converted both ways.
        """
        params = params or {}
        op = time_zone.esri_operation(url)
        rule = time_zone.rule_for(op[0]) if op else None
        times = self.layer_times(op[0], rule, token) if rule else None
        if times:
            params = time_zone.to_service(params, op[1], times)
        value = self._ask_renewing(url, params, token)
        if times:
            time_zone.from_service(value, times)
        return value

    def layer_times(self, url: str, rule: "time_zone.TimeZoneRule", token: Optional[str] = None) -> "time_zone.LayerTimes":
        """How a layer's dates are converted, from its information (the shift alone when it cannot be read)."""
        try:
            info = self.layer(url, token).raw
        except EsriError:
            info = {}
        return time_zone.layer_times(info, rule)

    def _ask_renewing(self, url: str, params: dict[str, str], token: Optional[str]) -> dict[str, Any]:
        try:
            return self._ask(url, params, token)
        except InvalidTokenError:
            fresh = self.renew(url) if self.renew else None
            if not fresh:
                raise
            return self._ask(url, params, fresh)

    def _ask(self, url: str, params: dict[str, str], token: Optional[str]) -> dict[str, Any]:
        form = {**params, "f": "json"}
        if token:
            form["token"] = token
        try:
            body = self.transport(url, form)
        except EsriError as error:
            raise (UnreachableError if isinstance(error, UnreachableError) else EsriError)(_redact(str(error), token)) from None
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
            self._layers[url] = self._ask_renewing(url, {}, token)
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
        cut: bool = False,
    ) -> list[dict[str, Any]]:
        """
        Every feature matching `where` (in WGS 84 when `geometry`): pages of
        `resultOffset` (in object id order, so that pages neither skip nor
        repeat rows) while the layer pages, else the object ids first and
        the features by ids, `maxRecordCount` at a time.

        More than `limit` features is an error (TooManyError: never a silent
        cut, which would drop the newest rows), unless `cut` (the first
        `limit` are wanted, e.g. a user-set maximum).
        """
        info = self.layer(url, token)
        params = {"where": where or "1=1", "outFields": out_fields, "returnGeometry": "true" if geometry else "false", **(extra or {})}
        if geometry:
            params["outSR"] = "4326"
        size = max(1, min(page, info.max_record_count or page))
        out: list[dict[str, Any]] = []

        def too_many() -> "TooManyError":
            return TooManyError(f"件数が上限（{limit:,} 件）を超えたため読み込みを中止（条件で絞るか、古い行を整理）")

        if info.supports_pagination:
            if "orderByFields" not in params:
                params["orderByFields"] = info.object_id_field
            offset = 0
            while True:
                if cancelled and cancelled():
                    raise EsriError("取消")
                count = size if cut is False else max(1, min(size, limit - offset))
                json_ = self.ask(f"{url}/query", {**params, "resultOffset": str(offset), "resultRecordCount": str(count)}, token)
                features = json_.get("features") or []
                out.extend(features)
                # Servers that page say so; some leave the flag out on a full page.
                more = bool(json_.get("exceededTransferLimit")) or len(features) >= count
                if not features or not more:
                    break
                offset += len(features)
                if offset >= limit:
                    if cut:
                        break
                    # Exactly `limit` is fine: only a row past it is too many.
                    probe = self.ask(f"{url}/query", {**params, "resultOffset": str(offset), "resultRecordCount": "1"}, token)
                    if probe.get("features"):
                        raise too_many()
                    break
            if len(out) > limit and not cut:
                # The last page went past the limit (a page larger than what was left).
                raise too_many()
            return out[:limit]
        ids_json = self.ask(f"{url}/query", {"where": params["where"], "returnIdsOnly": "true", **(extra or {})}, token)
        ids = sorted(i for i in ids_json.get("objectIds") or [] if is_number(i))
        if len(ids) > limit:
            if not cut:
                raise too_many()
            ids = ids[:limit]
        for start in range(0, len(ids), size):
            if cancelled and cancelled():
                raise EsriError("取消")
            chunk = ids[start:start + size]
            json_ = self.ask(f"{url}/query", {**params, "objectIds": ",".join(str(int(i)) for i in chunk)}, token)
            out.extend(json_.get("features") or [])
        return out

    def find(self, url: str, field: str, values: Iterable[Any], token: Optional[str] = None,
             out_fields: str = "*") -> dict[str, dict[str, Any]]:
        """
        The features whose `field` is one of `values` (text), by that value
        (the attributes of the last one met for a value): whether records of
        those ids are already there before adding them (#73).
        """
        wanted = list(dict.fromkeys(str(v) for v in values if v not in (None, "")))
        out: dict[str, dict[str, Any]] = {}
        for i in range(0, len(wanted), 100):
            chunk = wanted[i:i + 100]
            where = f"{field} IN ({','.join(quote(v) for v in chunk)})"
            for f in self.query_all(url, where, token, out_fields=out_fields, geometry=False, limit=100_000):
                a = f.get("attributes") or {}
                if a.get(field) is not None:
                    out[str(a[field]).strip()] = a
        return out

    def apply_edits(
        self,
        url: str,
        adds: Iterable[dict[str, Any]] = (),
        updates: Iterable[dict[str, Any]] = (),
        deletes: Iterable[int] = (),
        token: Optional[str] = None,
    ) -> "EditResults":
        """
        Adds, updates and deletes features in one request
        (`rollbackOnFailure=false`). Adds and updates get the layer's
        tracking fields (who and when, core/edit_stamp.py).
        """
        params = {"rollbackOnFailure": "false"}
        adds, updates, deletes = list(adds), list(updates), list(deletes)
        if (adds or updates) and edit_stamp.tracking().enabled:
            try:
                types = self.layer(url, token).field_types
            except EsriError:
                types = {}
            when, user = edit_stamp.now_ms(), edit_stamp.user_name()
            adds = edit_stamp.stamp(adds, types, True, when, user=user)
            updates = edit_stamp.stamp(updates, types, False, when, user=user)
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


def deleted_count(value: dict[str, Any], what: str = "削除") -> int:
    """
    How many features a deleteFeatures answer says were deleted; raises
    EsriError with the reason when any was not (#81).
    """
    results = value.get("deleteResults")
    if results is None:
        if value.get("success") is False:
            raise EsriError(f"{what}失敗")
        return 0
    failed = [r for r in results if not r.get("success")]
    if failed:
        reason = (failed[0].get("error") or {}).get("description") or ""
        raise EsriError(f"{len(failed)} 件の{what}に失敗{'（' + reason + '）' if reason else ''}")
    return len(results)


@dataclass
class LayerInfo:
    """What a layer says of itself."""
    raw: dict[str, Any]

    @property
    def field_types(self) -> dict[str, str]:
        return {f["name"]: f.get("type", "") for f in self.raw.get("fields") or [] if isinstance(f, dict) and "name" in f}

    @property
    def field_lengths(self) -> dict[str, int]:
        """The length text fields take (those that say it)."""
        return {f["name"]: int(f["length"]) for f in self.raw.get("fields") or []
                if isinstance(f, dict) and "name" in f and isinstance(f.get("length"), int) and f["length"] > 0}

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


def like_text(text: str) -> str:
    """Escapes a text for a SQL LIKE pattern in single quotes (`ESCAPE '\\'`): quotes doubled, `\\ % _` escaped."""
    # The escape character itself first, so that a trailing \ does not escape the closing %.
    return re.sub(r"[\\%_]", lambda m: "\\" + m[0], str(text).replace("'", "''"))


def contains(field: str, text: str, any_case: bool = False) -> str:
    """
    `field` holds `text` anywhere (`LIKE '%…%'`), the text taken as it is (its
    `%`, `_` and quotes are not SQL); `any_case`: compared in capitals
    (`UPPER`), so that ASCII letters match either way. `field` is a settings
    file's attribute name (trusted), not something typed.
    """
    name = f"UPPER({field})" if any_case else field
    value = text.upper() if any_case else text
    return f"{name} LIKE '%{like_text(value)}%' ESCAPE '\\'"


_CONDITION_TOKEN = re.compile(r"'(?:[^']|'')*'|\"(?:[^\"]|\"\")*\"|--|/\*|;|[()]|['\"]")


def condition_problem(text: str) -> Optional[str]:
    """
    Why a condition typed by a person (or kept in a project or a watch)
    cannot be ANDed in parentheses with the settings file's own `where`, or
    None: a `)` that closes the parentheses around it (`1=1) OR (1=1` would
    leave the settings file's condition out), a `(` or a quote left open, a
    `;` or a comment (security audit 2026-11-08). Text inside quotes is not
    looked at.
    """
    depth = 0
    for m in _CONDITION_TOKEN.finditer(text or ""):
        t = m.group(0)
        if t in ("'", '"'):
            return "引用符（' または \"）が閉じていない"
        if t == "(":
            depth += 1
        elif t == ")":
            depth -= 1
            if depth < 0:
                return "閉じ括弧 ) が開き括弧より多い"
        elif t in ("--", "/*"):
            return "コメント（-- や /*）は書けない"
        elif t == ";":
            return "; は書けない（条件は 1 つの式）"
    return "括弧 ( が閉じていない" if depth else None


def own_condition(text: str) -> str:
    """A person's condition in parentheses, to AND with the rest; EsriError when `condition_problem` finds one."""
    problem = condition_problem(text)
    if problem:
        raise EsriError(f"条件を使えない: {problem}（{text.strip()}）")
    return f"({text.strip()})"


def ring_geometry(rings: list[list[list[float]]]) -> dict[str, Any]:
    """An Esri polygon of longitude / latitude rings."""
    return {"rings": rings, "spatialReference": {"wkid": 4326}}


def point_geometry(lon: float, lat: float) -> dict[str, Any]:
    """An Esri point of a longitude / latitude."""
    return {"x": lon, "y": lat, "spatialReference": {"wkid": 4326}}
