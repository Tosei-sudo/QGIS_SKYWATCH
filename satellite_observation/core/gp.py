"""
Server-side geoprocessing, as the viewer runs it (gp/model.ts, gp/client.ts):
ArcGIS GPServer tasks and OGC API – Processes descriptions read into one
kind of parameter list, the values turned into what the server takes, a run
waited for (`execute`, an OGC execute that answers at once) or followed as a
job (`submitJob`, `Prefer: respond-async`) with its messages and cancelling,
and the results read back (features, images, files, plain values).

The network is a transport given by the caller (qgis_io/gp_io.py in QGIS),
so everything here runs without QGIS. Everything comes from a server: it is
checked as it is read, feature counts and text are capped, only http(s) URLs
are kept, and a token goes only to the service's own origin.
"""
from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable, Optional, Protocol
from urllib.parse import quote, unquote, urlencode, urljoin, urlsplit

from .util import is_record

#: The most features read from one result, and sent as one input.
MAX_GP_FEATURES = 50_000
#: The most tasks listed for one service.
MAX_TASKS = 500
#: The longest text kept from a server (a description, a value).
MAX_TEXT = 2_000
#: The largest JSON response read.
MAX_RESPONSE_BYTES = 64 * 1024 * 1024

LINEAR_UNITS = ["esriMeters", "esriKilometers", "esriFeet", "esriMiles", "esriNauticalMiles", "esriYards", "esriInches", "esriCentimeters",
                "esriMillimeters", "esriDecimalDegrees"]
#: Units offered for a linear unit, with their Japanese names.
LINEAR_UNIT_NAMES = {"esriMeters": "m", "esriKilometers": "km", "esriFeet": "フィート", "esriMiles": "マイル", "esriNauticalMiles": "海里",
                     "esriYards": "ヤード", "esriInches": "インチ", "esriCentimeters": "cm", "esriMillimeters": "mm", "esriDecimalDegrees": "度"}
#: Japanese names of the job states, for the status line.
JOB_STATE_NAMES = {"waiting": "待機中", "running": "実行中", "succeeded": "完了", "failed": "失敗", "cancelled": "取り消し"}


class GpError(Exception):
    """A request or a run that failed, in the person's words."""


class GpCancelled(GpError):
    def __init__(self):
        super().__init__("取り消し")


def _text(v: Any, limit: int = MAX_TEXT) -> Optional[str]:
    return v.strip()[:limit] if isinstance(v, str) and v.strip() else None


def _number(v: Any) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool) and v == v and abs(v) != float("inf")


def safe_url(url: Any, base: Optional[str] = None) -> Optional[str]:
    """`url` resolved against `base`, when it is http(s); None for anything else (`javascript:`, `file:`, `data:`…)."""
    if not isinstance(url, str) or not url.strip():
        return None
    try:
        full = urljoin(base, url.strip()) if base else url.strip()
        u = urlsplit(full)
    except ValueError:
        return None
    return full if u.scheme in ("http", "https") and u.netloc else None


def same_origin(url: str, service_url: str) -> bool:
    """Whether `url` is on the same origin as `service_url`: only then does a token go with it."""
    try:
        a, b = urlsplit(url), urlsplit(service_url)
    except ValueError:
        return False
    return bool(a.netloc) and (a.scheme, a.netloc.lower()) == (b.scheme, b.netloc.lower())


def classify_url(url: str) -> dict:
    """What a URL points at: a GPServer, one of its tasks, or (anything else) an OGC API – Processes endpoint."""
    clean = re.sub(r"[?#].*$", "", url).rstrip("/")
    m = re.match(r"^(.*/GPServer)(?:/([^/]+))?$", clean, re.IGNORECASE)
    if m:
        if m.group(2):
            return {"kind": "esri-task", "root": m.group(1), "task": unquote(m.group(2))}
        return {"kind": "esri-service", "root": m.group(1)}
    return {"kind": "ogc", "url": clean}


def service_root(url: str) -> str:
    """The URL a service's sign-in is kept under: the GPServer, or the OGC API landing page."""
    kind = classify_url(url)
    if kind["kind"] != "ogc":
        return kind["root"]
    return re.sub(r"/(?:processes|jobs)(?:/.*)?$", "", kind["url"])


# ---- Descriptions ----

@dataclass
class GpField:
    """One input of a task."""
    name: str
    label: str
    #: string, choice, integer, number, boolean, date, linear-unit, features, table, raster, file, multi or json.
    type: str
    required: bool
    description: str = ""
    choices: list = field(default_factory=list)
    #: The server's default, in the control's terms (a number, True, an ISO date…); None: none.
    default: Any = None
    #: features: what the server takes (Point, LineString, Polygon) when it says.
    geometry: Optional[str] = None
    #: features (OGC): one geometry, one feature or a collection.
    shape: str = "collection"
    #: linear-unit: the server's default unit.
    units: str = "esriMeters"
    #: features / table (Esri): the attributes the task's default value lists.
    fields: list = field(default_factory=list)
    data_type: str = ""
    minimum: Optional[float] = None
    maximum: Optional[float] = None


@dataclass
class GpOutputSpec:
    name: str
    label: str
    data_type: str = ""


@dataclass
class GpTask:
    """A task (Esri) or process (OGC), read."""
    kind: str
    url: str
    id: str
    title: str
    inputs: list
    outputs: list
    #: Run as a job rather than waiting for the answer.
    run_async: bool = False
    description: str = ""
    #: Esri: the map service a job draws its results in (`resultMapServerName`).
    result_map_server: str = ""


@dataclass
class GpService:
    """A service, read: the tasks to choose from, as (id, title, url)."""
    kind: str
    url: str
    title: str
    tasks: list


def parse_esri_service(root: str, value: Any) -> GpService:
    """The tasks of a GPServer (`{tasks: ['Buffer', …]}`)."""
    if not is_record(value) or not isinstance(value.get("tasks"), list):
        raise GpError("GPServer の応答に tasks なし")
    names = []
    for t in value["tasks"]:
        name = t if isinstance(t, str) else _text(t.get("name"), 200) if is_record(t) else None
        if name:
            names.append(name)
    tasks = [(n, n, f"{root}/{quote(n, safe='')}") for n in names[:MAX_TASKS]]
    parts = root.split("/")
    return GpService("esri", root, unquote(parts[-2]) if len(parts) > 1 else root, tasks)


_ESRI_GEOMETRIES = {"esriGeometryPoint": "Point", "esriGeometryMultipoint": "Point", "esriGeometryPolyline": "LineString",
                    "esriGeometryPolygon": "Polygon", "esriGeometryEnvelope": "Polygon"}


def _esri_fields_of(value: Any) -> list:
    if not is_record(value) or not isinstance(value.get("fields"), list):
        return []
    out = []
    for f in value["fields"][:500]:
        if is_record(f) and isinstance(f.get("name"), str) and isinstance(f.get("type"), str):
            out.append({"name": f["name"], "type": f["type"], **({"alias": f["alias"]} if isinstance(f.get("alias"), str) else {})})
    return out


def esri_field(p: Any) -> Optional[GpField]:
    """One parameter of a GP task; None for outputs and derived ones."""
    if not is_record(p) or not isinstance(p.get("name"), str) or not isinstance(p.get("dataType"), str):
        return None
    if p.get("direction") is not None and p.get("direction") != "esriGPParameterDirectionInput":
        return None
    if p.get("parameterType") == "esriGPParameterTypeDerived":
        return None
    data_type = p["dataType"]
    base = dict(name=p["name"], label=_text(p.get("displayName"), 200) or p["name"], required=p.get("parameterType") == "esriGPParameterTypeRequired",
                description=_text(p.get("description")) or "", data_type=data_type)
    choices = [c for c in p.get("choiceList") or [] if isinstance(c, (str, int, float)) and not isinstance(c, bool)][:1000]
    default = p.get("defaultValue")
    if data_type == "GPString":
        return GpField(**base, type="choice" if choices else "string", choices=choices, default=default if isinstance(default, str) else None)
    if data_type in ("GPLong", "GPDouble"):
        if choices:
            return GpField(**base, type="choice", choices=choices, default=default if _number(default) else None)
        return GpField(**base, type="integer" if data_type == "GPLong" else "number", default=default if _number(default) else None)
    if data_type == "GPBoolean":
        return GpField(**base, type="boolean", default=default is True)
    if data_type == "GPDate":
        iso = datetime.fromtimestamp(default / 1000, timezone.utc).isoformat().replace("+00:00", "Z") if _number(default) else None
        return GpField(**base, type="date", default=iso)
    if data_type == "GPLinearUnit":
        d = default if is_record(default) else {}
        units = d.get("units") if d.get("units") in LINEAR_UNITS else "esriMeters"
        return GpField(**base, type="linear-unit", units=units, default=d.get("distance") if _number(d.get("distance")) else None)
    if data_type == "GPFeatureRecordSetLayer":
        geometry = _ESRI_GEOMETRIES.get(default.get("geometryType")) if is_record(default) else None
        return GpField(**base, type="features", geometry=geometry, fields=_esri_fields_of(default))
    if data_type == "GPRecordSet":
        return GpField(**base, type="table", fields=_esri_fields_of(default))
    if data_type in ("GPRasterDataLayer", "GPRasterData", "GPDataFile"):
        return GpField(**base, type="file" if data_type == "GPDataFile" else "raster", default=safe_url(default.get("url")) if is_record(default) else None)
    if data_type.startswith("GPMultiValue"):
        joined = ", ".join(str(v) for v in default if isinstance(v, (str, int, float))) if isinstance(default, list) else None
        return GpField(**base, type="multi", choices=choices, default=joined)
    # Anything else (GPField, GPSpatialReference…) as the JSON the server takes.
    return GpField(**base, type="json", default=json.dumps(default, ensure_ascii=False)[:MAX_TEXT] if default is not None else None)


def parse_esri_task(url: str, value: Any, service: Any = None) -> GpTask:
    """A GP task's description (`…/GPServer/Buffer?f=json`)."""
    if not is_record(value) or not isinstance(value.get("parameters"), list):
        raise GpError("GP タスクの応答に parameters なし")
    params = value["parameters"][:200]
    inputs = [f for f in (esri_field(p) for p in params) if f is not None]
    outputs = [GpOutputSpec(p["name"], _text(p.get("displayName"), 200) or p["name"], p.get("dataType") if isinstance(p.get("dataType"), str) else "")
               for p in params if is_record(p) and p.get("direction") == "esriGPParameterDirectionOutput" and isinstance(p.get("name"), str)]
    task_id = _text(value.get("name"), 200) or unquote(url.rstrip("/").split("/")[-1])
    map_server = _text(service.get("resultMapServerName"), 200) if is_record(service) else None
    return GpTask("esri", url, task_id, _text(value.get("displayName"), 200) or task_id, inputs, outputs,
                  value.get("executionType") == "esriExecutionTypeAsynchronous", _text(value.get("description")) or "",
                  map_server if map_server and re.fullmatch(r"[\w.-]+", map_server) else "")


def link_of(links: Any, rels: list, base: str) -> Optional[str]:
    """The link of one of `rels` among `links` (an OGC API links array), resolved against `base`."""
    if not isinstance(links, list):
        return None
    for rel in rels:
        for link in links:
            if is_record(link) and link.get("rel") == rel and (link.get("type") is None or re.search("json", str(link.get("type")), re.IGNORECASE)):
                href = safe_url(link.get("href"), base)
                if href:
                    return href
    return None


_PROCESSES_REL = ["http://www.opengis.net/def/rel/ogc/1.0/processes", "processes"]


def ogc_processes_url(url: str, value: Any) -> Optional[str]:
    """The processes URL of an OGC API landing page; None when it has none."""
    return link_of(value.get("links"), _PROCESSES_REL, url if url.endswith("/") else url + "/") if is_record(value) else None


def parse_ogc_process_list(url: str, value: Any) -> GpService:
    """The processes of an OGC API – Processes list (`/processes`)."""
    if not is_record(value) or not isinstance(value.get("processes"), list):
        raise GpError("processes の一覧なし")
    base = url if url.endswith("/") else url + "/"
    tasks = []
    for p in value["processes"]:
        if not is_record(p) or not isinstance(p.get("id"), str) or not p["id"]:
            continue
        own = link_of(p.get("links"), ["self", "process", "http://www.opengis.net/def/rel/ogc/1.0/process"], url)
        tasks.append((p["id"], _text(p.get("title"), 200) or p["id"], own or urljoin(base, quote(p["id"], safe=""))))
    return GpService("ogc", url, _text(value.get("title"), 200) or "OGC API – Processes", tasks[:MAX_TASKS])


def _geojson_shape(schema: dict) -> Optional[str]:
    """Whether a JSON schema stands for GeoJSON, and which: collection, feature or geometry."""
    hint = " ".join(str(schema.get(k)) for k in ("format", "contentMediaType", "$ref") if isinstance(schema.get(k), str)).lower()
    if re.search(r"feature-?collection", hint):
        return "collection"
    if "geometry" in hint:
        return "geometry"
    if re.search(r"geojson-feature|(^|[^a-z])feature(geojson|[^a-z]|$)", hint):
        return "feature"
    if re.search(r"geojson|geo\+json", hint):
        return "collection"
    return None


def _schema_of(schema: Any) -> dict:
    """The schema an input takes, with `oneOf` / `anyOf` / `allOf` of one useful member unwrapped."""
    if not is_record(schema):
        return {}
    for key in ("oneOf", "anyOf", "allOf"):
        members = schema.get(key)
        if isinstance(members, list) and members:
            geo = next((s for s in members if is_record(s) and _geojson_shape(s)), None)
            if geo:
                return geo
            if len(members) == 1:
                return {**{k: v for k, v in schema.items() if k != key}, **_schema_of(members[0])}
    return schema


def ogc_field(name: str, value: Any) -> GpField:
    """One input of an OGC process description."""
    d = value if is_record(value) else {}
    schema = _schema_of(d.get("schema"))
    default = schema.get("default")
    required = default is None if d.get("minOccurs") is None else _number(d.get("minOccurs")) and d["minOccurs"] > 0
    base = dict(name=name, label=_text(d.get("title"), 200) or name, required=bool(required), description=_text(d.get("description")) or "")
    shape = _geojson_shape(schema)
    if shape:
        return GpField(**base, type="features", shape=shape)
    if isinstance(schema.get("enum"), list) and schema["enum"]:
        choices = [c for c in schema["enum"] if isinstance(c, (str, int, float)) and not isinstance(c, bool)][:1000]
        return GpField(**base, type="choice", choices=choices, default=default if isinstance(default, (str, int, float)) else None)
    kind = schema.get("type")
    limits = dict(minimum=schema["minimum"] if _number(schema.get("minimum")) else None, maximum=schema["maximum"] if _number(schema.get("maximum")) else None)
    if kind == "string":
        if schema.get("format") in ("date", "date-time"):
            return GpField(**base, type="date", default=default if isinstance(default, str) else None)
        if schema.get("format") in ("uri", "url"):
            return GpField(**base, type="file", default=safe_url(default))
        return GpField(**base, type="string", default=default if isinstance(default, str) else None)
    if kind in ("integer", "number"):
        return GpField(**base, type=kind, default=default if _number(default) else None, **limits)
    if kind == "boolean":
        return GpField(**base, type="boolean", default=default is True)
    return GpField(**base, type="json", default=json.dumps(default, ensure_ascii=False)[:MAX_TEXT] if default is not None else None)


def parse_ogc_process(url: str, value: Any) -> GpTask:
    """An OGC process description (`/processes/{id}`)."""
    if not is_record(value) or not isinstance(value.get("id"), str):
        raise GpError("プロセスの説明に id なし")

    # 1.0 has inputs as a map; drafts had a list of {id, …}.
    def entries(v: Any) -> list:
        if isinstance(v, list):
            return [(x["id"], x) for x in v if is_record(x) and isinstance(x.get("id"), str)]
        return list(v.items()) if is_record(v) else []

    inputs = [ogc_field(name, x) for name, x in entries(value.get("inputs"))[:200]]
    outputs = [GpOutputSpec(name, (_text(x.get("title"), 200) if is_record(x) else None) or name) for name, x in entries(value.get("outputs"))[:200]]
    control = value.get("jobControlOptions") if isinstance(value.get("jobControlOptions"), list) else ["sync-execute"]
    return GpTask("ogc", url, value["id"], _text(value.get("title"), 200) or value["id"], inputs, outputs, "async-execute" in control,
                  _text(value.get("description")) or "")


# ---- Inputs ----

@dataclass
class FeatureInput:
    """Features handed to a `features` or `table` input, made by the caller in both forms a server may take."""
    #: Esri features in Web Mercator: {geometry?, attributes}.
    esri: list
    #: GeoJSON features in WGS 84: {type, geometry, properties}.
    geojson: list
    #: Esri field definitions ({name, type, alias}) of the attributes sent.
    fields: list
    #: Esri geometry type (esriGeometryPolygon…), empty for rows only.
    geometry_type: str = ""

    def __len__(self) -> int:
        return len(self.esri)


_WEB_MERCATOR = {"wkid": 102100, "latestWkid": 3857}


def esri_feature_set(value: FeatureInput, table: bool = False) -> dict:
    """Features as an Esri feature set in Web Mercator (GPFeatureRecordSetLayer), or rows without geometry (GPRecordSet)."""
    if len(value) > MAX_GP_FEATURES:
        raise GpError(f"送れる地物は {MAX_GP_FEATURES:,} 件まで")
    names = {f["name"] for f in value.fields}
    rows = [{**({} if table else {"geometry": f["geometry"]}), "attributes": {k: v for k, v in f["attributes"].items() if k in names}}
            for f in value.esri if table or f.get("geometry")]
    if table:
        return {"fields": value.fields, "features": rows}
    return {"geometryType": value.geometry_type or "esriGeometryPolygon", "spatialReference": _WEB_MERCATOR, "fields": value.fields, "features": rows}


def ogc_geojson(value: FeatureInput, shape: str = "collection") -> dict:
    """Features as GeoJSON (WGS 84) in the shape an OGC input takes."""
    if len(value) > MAX_GP_FEATURES:
        raise GpError(f"送れる地物は {MAX_GP_FEATURES:,} 件まで")
    if shape == "geometry":
        geometry = next((f["geometry"] for f in value.geojson if f.get("geometry")), None)
        if not geometry:
            raise GpError("図形なし")
        return geometry
    if shape == "feature":
        if not value.geojson:
            raise GpError("地物なし")
        return value.geojson[0]
    return {"type": "FeatureCollection", "features": value.geojson}


def _empty(v: Any) -> bool:
    return v is None or v == "" or (isinstance(v, FeatureInput) and len(v) == 0)


def check_values(task: GpTask, values: dict) -> list:
    """What is missing or wrong in `values` for `task`, in the person's words; empty when nothing."""
    problems = []
    for f in task.inputs:
        v = values.get(f.name)
        if _empty(v):
            if f.required and f.type != "boolean":
                problems.append(f"「{f.label}」を入力")
            continue
        if f.type in ("integer", "number") and _number(v):
            if f.type == "integer" and float(v) != int(v):
                problems.append(f"「{f.label}」は整数で指定")
            elif (f.minimum is not None and v < f.minimum) or (f.maximum is not None and v > f.maximum):
                lo = "" if f.minimum is None else f"{f.minimum:g}"
                hi = "" if f.maximum is None else f"{f.maximum:g}"
                problems.append(f"「{f.label}」は {lo}〜{hi} で指定")
        if f.type in ("raster", "file") and not safe_url(v):
            problems.append(f"「{f.label}」は http(s) の URL で指定")
        if f.type == "json" and isinstance(v, str):
            try:
                json.loads(v)
            except ValueError:
                problems.append(f"「{f.label}」は JSON で記述")
    return problems


def _ms(value: Any) -> Optional[int]:
    if _number(value):
        return int(value)
    if isinstance(value, datetime):
        return int((value if value.tzinfo else value.replace(tzinfo=timezone.utc)).timestamp() * 1000)
    if isinstance(value, str):
        try:
            t = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
        except ValueError:
            return None
        return int((t if t.tzinfo else t.replace(tzinfo=timezone.utc)).timestamp() * 1000)
    return None


def esri_params(task: GpTask, values: dict) -> dict:
    """The parameters of an Esri `execute` / `submitJob` (each a string, as the form-encoded request takes them)."""
    problems = check_values(task, values)
    if problems:
        raise GpError("。".join(problems))
    # Results in Web Mercator, so they need no lookup of a CRS.
    params = {"env:outSR": "102100"}
    for f in task.inputs:
        v = values.get(f.name)
        if _empty(v):
            continue
        if isinstance(v, FeatureInput):
            params[f.name] = json.dumps(esri_feature_set(v, f.type == "table"), ensure_ascii=False)
        elif f.type == "boolean":
            params[f.name] = "true" if v else "false"
        elif f.type == "date":
            t = _ms(v)
            if t is not None:
                params[f.name] = str(t)
        elif f.type == "linear-unit":
            params[f.name] = json.dumps(v if is_record(v) else {"distance": float(v), "units": f.units or "esriMeters"})
        elif f.type in ("raster", "file"):
            params[f.name] = json.dumps({"url": safe_url(v)})
        elif f.type == "multi":
            params[f.name] = json.dumps([s.strip() for s in re.split(r"[,、\n]", str(v)) if s.strip()], ensure_ascii=False)
        elif _number(v) and f.type == "integer":
            params[f.name] = str(int(v))
        else:
            params[f.name] = str(v)
    return params


def ogc_inputs(task: GpTask, values: dict) -> dict:
    """The `inputs` of an OGC execute request."""
    problems = check_values(task, values)
    if problems:
        raise GpError("。".join(problems))
    inputs: dict = {}
    for f in task.inputs:
        v = values.get(f.name)
        if _empty(v):
            continue
        if isinstance(v, FeatureInput):
            inputs[f.name] = ogc_geojson(v, f.shape)
        elif f.type in ("file", "raster"):
            inputs[f.name] = {"href": safe_url(v)}
        elif f.type == "json":
            inputs[f.name] = json.loads(str(v))
        elif f.type == "linear-unit":
            inputs[f.name] = v.get("distance") if is_record(v) else v
        elif f.type == "date":
            t = _ms(v)
            inputs[f.name] = datetime.fromtimestamp(t / 1000, timezone.utc).isoformat().replace("+00:00", "Z") if t is not None else v
        elif f.type == "choice":
            # Keep the type the server listed (a number stays a number).
            inputs[f.name] = next((c for c in f.choices if str(c) == str(v)), v)
        elif f.type == "integer" and _number(v):
            inputs[f.name] = int(v)
        else:
            inputs[f.name] = v
    return inputs


# ---- Results ----

@dataclass
class GpOutput:
    """One result of a run: features (Esri JSON or GeoJSON), an image or a link (a URL), or a plain value."""
    kind: str
    name: str
    label: str
    #: features: esri or geojson.
    format: str = ""
    features: list = field(default_factory=list)
    #: features: Esri field definitions (Esri), or (name, type) inferred (GeoJSON).
    fields: list = field(default_factory=list)
    geometry_type: str = ""
    truncated: bool = False
    #: features: EPSG:… or WKT; None: unknown.
    srs: Optional[str] = None
    url: str = ""
    text: str = ""


def esri_srs(sr: Any) -> Optional[str]:
    """The CRS of an Esri `spatialReference` (`EPSG:6677`), or its WKT; None when it has neither."""
    if not is_record(sr):
        return None
    wkid = sr.get("latestWkid") or sr.get("wkid")
    if isinstance(wkid, int) and wkid > 0:
        return "EPSG:3857" if wkid in (102100, 102113, 900913) else f"EPSG:{wkid}"
    return sr["wkt"] if isinstance(sr.get("wkt"), str) and len(sr["wkt"]) < 20_000 else None


def _value_text(value: Any) -> str:
    s = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)
    return s[:MAX_TEXT] + "…" if len(s) > MAX_TEXT else s


_IMAGE_LIKE = re.compile(r"\.(tiff?|png|jpe?g|webp|gif|jp2|ntf|nitf)(?:[?#]|$)", re.IGNORECASE)


def esri_output(result: Any, label: Optional[str] = None, base: Optional[str] = None) -> Optional[GpOutput]:
    """One result of an Esri task (`{paramName, dataType, value}`)."""
    if not is_record(result) or not isinstance(result.get("paramName"), str):
        return None
    name = result["paramName"]
    head = dict(name=name, label=label or name)
    value = result.get("value")
    data_type = result.get("dataType") if isinstance(result.get("dataType"), str) else ""
    if data_type in ("GPFeatureRecordSetLayer", "GPRecordSet") and is_record(value):
        if not isinstance(value.get("features"), list):
            return GpOutput("value", **head, text="（地物なし）")
        features = [f for f in value["features"][:MAX_GP_FEATURES + 1] if is_record(f)]
        return GpOutput("features", **head, format="esri", features=features[:MAX_GP_FEATURES], truncated=len(features) > MAX_GP_FEATURES,
                        fields=_esri_fields_of(value), geometry_type=value.get("geometryType") if isinstance(value.get("geometryType"), str) else "",
                        srs=esri_srs(value.get("spatialReference")))
    if data_type in ("GPRasterDataLayer", "GPRasterData", "GPDataFile") and is_record(value):
        url = safe_url(value.get("url"), base)
        if not url:
            return GpOutput("value", **head, text="（URL なし）")
        image = data_type != "GPDataFile" or bool(_IMAGE_LIKE.search(url)) or bool(re.fullmatch(r"tiff?|png|jpe?g", str(value.get("format") or ""), re.IGNORECASE))
        return GpOutput("image" if image else "link", **head, url=url)
    if data_type == "GPDate" and _number(value):
        return GpOutput("value", **head, text=datetime.fromtimestamp(value / 1000, timezone.utc).isoformat().replace("+00:00", "Z"))
    if data_type == "GPLinearUnit" and is_record(value):
        return GpOutput("value", **head, text=f"{value.get('distance')} {LINEAR_UNIT_NAMES.get(str(value.get('units')), value.get('units'))}")
    return GpOutput("value", **head, text="（なし）" if value is None else _value_text(value))


_GEOJSON_TYPES = {"FeatureCollection", "Feature", "Point", "MultiPoint", "LineString", "MultiLineString", "Polygon", "MultiPolygon", "GeometryCollection"}


def _is_geojson(value: Any) -> bool:
    return is_record(value) and value.get("type") in _GEOJSON_TYPES


def infer_fields(features: list) -> list:
    """(name, type) of every property of GeoJSON features, typed by their values (integer, double, string, other)."""
    kinds: dict = {}
    for f in features[:1000]:
        for key, v in ((f.get("properties") or {}) if is_record(f) else {}).items():
            if v is None:
                continue
            kind = ("integer" if isinstance(v, int) else "double") if _number(v) else "other" if isinstance(v, (dict, list)) else "string"
            kinds.setdefault(key, set()).add(kind)
    out = []
    for name, seen in kinds.items():
        out.append((name, "other" if "other" in seen else "string" if "string" in seen else "double" if "double" in seen else "integer"))
    return out


def ogc_output(name: str, value: Any, label: Optional[str] = None, base: Optional[str] = None) -> GpOutput:
    """One OGC output value: GeoJSON, a link (`{href, type}`), a qualified value (`{value, mediaType}`) or a plain value."""
    head = dict(name=name, label=label or name)
    if _is_geojson(value):
        if value["type"] == "FeatureCollection":
            features = [f for f in value.get("features") or [] if is_record(f)][:MAX_GP_FEATURES + 1]
        elif value["type"] == "Feature":
            features = [value]
        else:
            features = [{"type": "Feature", "geometry": value, "properties": {}}]
        return GpOutput("features", **head, format="geojson", features=features[:MAX_GP_FEATURES], truncated=len(features) > MAX_GP_FEATURES,
                        fields=infer_fields(features), srs="EPSG:4326")
    if is_record(value) and isinstance(value.get("href"), str):
        url = safe_url(value["href"], base)
        if not url:
            return GpOutput("value", **head, text="（http(s) 以外のリンクは表示不可）")
        kind = value.get("type") if isinstance(value.get("type"), str) else ""
        return GpOutput("image" if re.match(r"^image/", kind, re.IGNORECASE) or _IMAGE_LIKE.search(url) else "link", **head, url=url)
    if is_record(value) and "value" in value and all(k in ("value", "mediaType", "encoding", "schema", "format") for k in value):
        return ogc_output(name, value["value"], label, base)
    return GpOutput("value", **head, text="（なし）" if value is None else _value_text(value))


def ogc_outputs(task: GpTask, value: Any, base: Optional[str] = None) -> list:
    """The results of an OGC run: a map of outputs by id, or the one output itself (a raw GeoJSON response)."""
    labels = {o.name: o.label for o in task.outputs}
    first = task.outputs[0] if task.outputs else None
    if _is_geojson(value) or not is_record(value):
        return [ogc_output(first.name if first else "result", value, first.label if first else None, base)]
    # 1.0 drafts: {outputs: [{id, value}]}.
    if isinstance(value.get("outputs"), list):
        return [ogc_output(o["id"], o.get("value", o), labels.get(o["id"]), base)
                for o in value["outputs"][:200] if is_record(o) and isinstance(o.get("id"), str)]
    return [ogc_output(k, v, labels.get(k), base) for k, v in list(value.items())[:200] if k != "links"]


def esri_job_state(status: Any) -> str:
    """The state of an Esri job (esriJobSucceeded…) as waiting, running, succeeded, failed or cancelled."""
    if status == "esriJobSucceeded":
        return "succeeded"
    if status in ("esriJobFailed", "esriJobTimedOut"):
        return "failed"
    if status in ("esriJobCancelled", "esriJobCancelling", "esriJobDeleted"):
        return "cancelled"
    return "running" if status == "esriJobExecuting" else "waiting"


def ogc_job_state(status: Any) -> str:
    """The state of an OGC job (accepted, running, successful, failed, dismissed)."""
    return {"successful": "succeeded", "failed": "failed", "dismissed": "cancelled", "running": "running"}.get(status, "waiting")


def esri_messages(messages: Any, limit: int = 20) -> list:
    """The messages of an Esri job or result, newest last, at most `limit`."""
    if not isinstance(messages, list):
        return []
    out = []
    for m in messages:
        if is_record(m) and isinstance(m.get("description"), str):
            prefix = "エラー: " if m.get("type") == "esriJobMessageTypeError" else "警告: " if m.get("type") == "esriJobMessageTypeWarning" else ""
            out.append(prefix + m["description"][:500])
    return out[-limit:]


# ---- Requests ----

class Http(Protocol):
    """One request: (status, headers with lower-case names, body); raises GpError when there is no answer."""

    def __call__(self, method: str, url: str, body: Optional[bytes], headers: dict) -> tuple:
        ...


@dataclass
class RunResult:
    outputs: list
    messages: list
    #: Esri: the map service drawing the job's results (`…/MapServer/jobs/<id>`).
    map_server: str = ""


class GpClient:
    """Reads processing services and runs their tasks over `http`, with a token for the service's origin only."""

    def __init__(self, http: Http, token: Optional[str] = None, sleep: Callable[[float], None] = time.sleep,
                 cancelled: Callable[[], bool] = lambda: False, poll_seconds: float = 1.0):
        self.http = http
        self.token = token
        self.sleep = sleep
        self.cancelled = cancelled
        self.poll_seconds = poll_seconds

    @staticmethod
    def _json(body: bytes) -> Any:
        if len(body) > MAX_RESPONSE_BYTES:
            raise GpError("応答が大きすぎ")
        try:
            return json.loads(body.decode("utf-8-sig"))
        except (ValueError, UnicodeDecodeError):
            raise GpError("応答が JSON 以外") from None

    def esri(self, url: str, params: Optional[dict], service_url: str) -> dict:
        """An ArcGIS REST call (always `f=json`, a POST), the token only to the service's origin."""
        safe = safe_url(url)
        if not safe:
            raise GpError("http(s) 以外の URL には接続不可")
        own = self.token if self.token and same_origin(safe, service_url) else None
        form = {**(params or {}), "f": "json", **({"token": own} if own else {})}
        status, _, body = self.http("POST", safe, urlencode(form).encode(), {"content-type": "application/x-www-form-urlencoded"})
        value = self._json(body)
        if not is_record(value):
            raise GpError("応答の形式が不正")
        error = value.get("error")
        if is_record(error):
            if error.get("code") in (498, 499, 403):
                raise GpError(f"トークンが無効か、権限なし（{error.get('message') or ''}）" if own else "このサービスには認証が必要（認証設定を選択）")
            details = [d for d in error.get("details") or [] if isinstance(d, str)]
            raise GpError(("サービスからエラー応答: " + " ".join([str(error.get("message") or ""), *details]))[:500])
        if status >= 400:
            raise GpError(f"サーバーからエラー応答（HTTP {status}）")
        return value

    def ogc(self, url: str, service_url: str, method: str = "GET", payload: Optional[dict] = None, prefer_async: bool = False) -> tuple:
        """An OGC API call: JSON in and out, the token as a bearer header to the service's origin only; (json, status, headers)."""
        safe = safe_url(url)
        if not safe:
            raise GpError("http(s) 以外の URL には接続不可")
        headers = {"accept": "application/json"}
        if self.token and same_origin(safe, service_url):
            headers["authorization"] = f"Bearer {self.token}"
        body = None
        if payload is not None:
            headers["content-type"] = "application/json"
            body = json.dumps(payload, ensure_ascii=False).encode()
        if prefer_async:
            headers["prefer"] = "respond-async"
        status, answer_headers, raw = self.http(method, safe, body, headers)
        if status in (401, 403):
            raise GpError("トークンが無効か、権限なし" if self.token else "このサービスには認証が必要（認証設定を選択）")
        if status >= 400:
            try:
                problem = json.loads(raw.decode("utf-8-sig"))
            except (ValueError, UnicodeDecodeError):
                problem = None
            detail = ": ".join(str(problem[k]) for k in ("title", "detail", "description") if is_record(problem) and isinstance(problem.get(k), str))
            raise GpError(f"サーバーからエラー応答（HTTP {status}）" + (f": {detail[:500]}" if detail else ""))
        if status == 204 or not raw:
            return None, status, answer_headers
        return self._json(raw), status, answer_headers

    def read(self, url: str) -> "GpService | GpTask":
        """A GPServer (its tasks), a GP task, an OGC API landing page or `/processes` (its processes), or one OGC process."""
        if not safe_url(url):
            raise GpError("http(s) の URL を入力")
        kind = classify_url(url)
        if kind["kind"] == "esri-service":
            return parse_esri_service(kind["root"], self.esri(kind["root"], None, kind["root"]))
        if kind["kind"] == "esri-task":
            return self.read_task("esri", f"{kind['root']}/{quote(kind['task'], safe='')}")
        value, _, _ = self.ogc(kind["url"], kind["url"])
        if is_record(value) and isinstance(value.get("processes"), list):
            return parse_ogc_process_list(kind["url"], value)
        if is_record(value) and isinstance(value.get("id"), str) and ("inputs" in value or "outputs" in value):
            return parse_ogc_process(kind["url"], value)
        processes = ogc_processes_url(kind["url"], value)
        if not processes:
            raise GpError("GPServer・GP タスク・OGC API – Processes のどれとしても読み込み失敗")
        listed, _, _ = self.ogc(processes, kind["url"])
        return parse_ogc_process_list(processes, listed)

    def read_task(self, kind: str, url: str) -> GpTask:
        """One task of a service listed by `read`."""
        if kind == "esri":
            root = re.sub(r"/[^/]+/?$", "", url)
            task = self.esri(url, None, url)
            try:
                service = self.esri(root, None, root)
            except GpError:
                service = None
            return parse_esri_task(url, task, service)
        value, _, _ = self.ogc(url, url)
        return parse_ogc_process(url, value)

    def _wait(self, seconds: float) -> None:
        """Waits between polls, stopping as soon as the run is cancelled."""
        end = time.monotonic() + seconds
        while time.monotonic() < end:
            if self.cancelled():
                raise GpCancelled()
            self.sleep(min(0.1, max(0.0, end - time.monotonic())))

    def run(self, task: GpTask, values: dict, status: Callable[[str, Optional[float]], None] = lambda text, percent: None) -> RunResult:
        """Runs a task with the values (FeatureInput for features and tables)."""
        return self._run_esri(task, values, status) if task.kind == "esri" else self._run_ogc(task, values, status)

    def _esri_result(self, value: Any, task: GpTask, base: str) -> Optional[GpOutput]:
        name = value.get("paramName") if is_record(value) else None
        label = next((o.label for o in task.outputs if o.name == name), None)
        return esri_output(value, label, base)

    def _run_esri(self, task: GpTask, values: dict, status) -> RunResult:
        params = esri_params(task, values)
        if not task.run_async:
            status("実行中…", None)
            answer = self.esri(f"{task.url}/execute", params, task.url)
            if self.cancelled():
                raise GpCancelled()
            results = answer.get("results") if isinstance(answer.get("results"), list) else []
            outputs = [o for o in (self._esri_result(r, task, task.url) for r in results[:200]) if o]
            return RunResult(outputs, esri_messages(answer.get("messages")))
        job = self.esri(f"{task.url}/submitJob", params, task.url)
        job_id = job.get("jobId")
        if not isinstance(job_id, str) or not re.fullmatch(r"[\w.-]{1,200}", job_id):
            raise GpError("ジョブ ID の返却なし")
        job_url = f"{task.url}/jobs/{quote(job_id, safe='')}"
        delay = self.poll_seconds
        try:
            while True:
                state = esri_job_state(job.get("jobStatus"))
                messages = esri_messages(job.get("messages"))
                progress = job.get("progress") if is_record(job.get("progress")) else {}
                percent = progress.get("percent") if _number(progress.get("percent")) else None
                last = progress.get("message") if isinstance(progress.get("message"), str) else (messages[-1] if messages else "")
                status(f"ジョブ {JOB_STATE_NAMES[state]}" + (f": {last[:200]}" if last else ""), percent)
                if state == "succeeded":
                    break
                if state == "failed":
                    errors = [m for m in messages if m.startswith("エラー")][-3:]
                    raise GpError("ジョブが失敗" + (f": {' / '.join(errors) or messages[-1]}" if messages else ""))
                if state == "cancelled":
                    raise GpCancelled()
                self._wait(delay)
                delay = min(delay * 1.5, 5.0)
                job = self.esri(job_url, None, task.url)
        except GpCancelled:
            status("取り消し中…", None)
            try:
                self.esri(f"{job_url}/cancel", None, task.url)
            except GpError:
                pass
            raise
        outputs = []
        results = job.get("results") if is_record(job.get("results")) else {}
        for name, ref in list(results.items())[:200]:
            status(f"結果「{name}」を読み込み中…", None)
            param_url = ref.get("paramUrl") if is_record(ref) and isinstance(ref.get("paramUrl"), str) else f"results/{name}"
            # Results live under the job; anything else is not followed.
            url = safe_url(param_url, f"{job_url}/")
            if not url or not url.startswith(f"{job_url}/"):
                continue
            output = self._esri_result(self.esri(url, {"returnType": "data"}, task.url), task, url)
            if output:
                outputs.append(output)
        map_server = ""
        if task.result_map_server:
            candidate = f"{re.sub(r'/[^/]+/GPServer/[^/]+$', '', task.url, flags=re.IGNORECASE)}/{task.result_map_server}/MapServer/jobs/{quote(job_id, safe='')}"
            map_server = candidate if safe_url(candidate) else ""
        return RunResult(outputs, esri_messages(job.get("messages")), map_server)

    def _run_ogc(self, task: GpTask, values: dict, status) -> RunResult:
        payload = {"inputs": ogc_inputs(task, values)}
        status("ジョブを送信中…" if task.run_async else "実行中…", None)
        answer, code, headers = self.ogc(f"{task.url}/execution", task.url, "POST", payload, task.run_async)
        if self.cancelled() and not task.run_async:
            raise GpCancelled()
        job = answer if is_record(answer) else None
        is_job = code == 201 or (job is not None and isinstance(job.get("jobID"), str) and isinstance(job.get("status"), str) and "type" not in job)
        if not is_job:
            return RunResult(ogc_outputs(task, answer, task.url), [])
        # The job: Location, a monitor link, or …/jobs/{jobID} next to /processes.
        root = re.sub(r"/processes/[^/]+$", "", task.url)
        job_id = job.get("jobID") if job and isinstance(job.get("jobID"), str) and re.fullmatch(r"[\w.-]{1,200}", job["jobID"]) else None
        job_url = (safe_url(headers.get("location"), task.url)
                   or link_of((job or {}).get("links"), ["monitor", "status", "self", "http://www.opengis.net/def/rel/ogc/1.0/job"], task.url)
                   or (f"{root}/jobs/{quote(job_id, safe='')}" if job_id else None))
        if not job_url or not same_origin(job_url, task.url):
            raise GpError("ジョブの URL が不明")
        job = job or {}
        delay = self.poll_seconds
        try:
            while True:
                state = ogc_job_state(job.get("status"))
                percent = job.get("progress") if _number(job.get("progress")) else None
                message = job.get("message")[:200] if isinstance(job.get("message"), str) else ""
                status(f"ジョブ {JOB_STATE_NAMES[state]}" + (f": {message}" if message else ""), percent)
                if state == "succeeded":
                    break
                if state == "failed":
                    raise GpError("ジョブが失敗" + (f": {message}" if message else ""))
                if state == "cancelled":
                    raise GpCancelled()
                self._wait(delay)
                delay = min(delay * 1.5, 5.0)
                polled, _, _ = self.ogc(job_url, task.url)
                job = polled if is_record(polled) else {}
        except GpCancelled:
            status("取り消し中…", None)
            try:
                self.ogc(job_url, task.url, "DELETE")
            except GpError:
                pass
            raise
        results_url = (link_of(job.get("links"), ["http://www.opengis.net/def/rel/ogc/1.0/results", "results"], job_url)
                       or f"{job_url.rstrip('/')}/results")
        if not same_origin(results_url, task.url):
            raise GpError("結果の URL がサービスと別のホスト")
        status("結果を読み込み中…", None)
        results, _, _ = self.ogc(results_url, task.url)
        return RunResult(ogc_outputs(task, results, results_url), [job["message"][:500]] if isinstance(job.get("message"), str) else [])


# ---- Settings file ----

@dataclass
class GpServiceConfig:
    """A processing service of the settings file (`gpServices`): its name in the list, its URL, and a token when it needs one."""
    label: str
    url: str
    token: str = ""


def gp_service_of(value: Any, problems: list, index: int) -> Optional[GpServiceConfig]:
    """One entry of `gpServices` (a URL alone will do)."""
    entry = {"url": value} if isinstance(value, str) else value
    if not is_record(entry) or not isinstance(entry.get("url"), str) or not re.match(r"^https?://.", entry["url"].strip(), re.IGNORECASE):
        problems.append(f"gpServices[{index}] の url は http(s) の URL で指定")
        return None
    url = entry["url"].strip()
    label = entry["label"].strip() if isinstance(entry.get("label"), str) and entry["label"].strip() else url
    return GpServiceConfig(label, url, entry["token"] if isinstance(entry.get("token"), str) else "")
