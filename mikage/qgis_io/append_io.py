"""
アペンド（既存レイヤーへ追加） in QGIS (core/append.py): the inputs' features
read (their values, the field map's expressions worked out, their geometries
turned into the target's CRS and kind), the target described (a QGIS layer,
or an ArcGIS feature layer by its URL), the target's features whose keys
the inputs have, and the write.

Threads: `InputSource` and a QGIS layer's `Target` are made on the main
thread; `prepare` (and an Esri target's `describe_esri`, `write`, `undo`)
run in a task; a QGIS layer is written on the main thread (`write_layer`,
`undo_layer`: its edit buffer, then saved unless the person is editing it).
An ArcGIS feature layer QGIS opened itself is written through the plugin's
Esri client (who and when, the audit log, the time zone), then reloaded.
"""
from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import date, datetime
from typing import Any, Callable, Optional, Sequence

from qgis.core import (
    QgsArcGisRestContext, QgsArcGisRestUtils, QgsCoordinateReferenceSystem, QgsCoordinateTransform, QgsExpression,
    QgsExpressionContext, QgsExpressionContextUtils, QgsFeatureRequest, QgsFieldConstraints, QgsFields, QgsGeometry, QgsProject,
    QgsVectorDataProvider, QgsVectorLayer, QgsVectorLayerFeatureSource, QgsVectorLayerUtils, QgsWkbTypes,
)
from qgis.PyQt.QtCore import QDate, QDateTime, QTime, QVariant

from ..core import append as ap
from ..core import edit_stamp
from ..core.esri import EsriClient
from .time_zone_fields import layer_url

ESRI_PROVIDER = "arcgisfeatureserver"
WGS84 = QgsCoordinateReferenceSystem("EPSG:4326")

_TYPES = {
    QVariant.String: ap.STRING, QVariant.Int: ap.INTEGER, QVariant.LongLong: ap.INTEGER, QVariant.UInt: ap.INTEGER,
    QVariant.ULongLong: ap.INTEGER, QVariant.Double: ap.DOUBLE, QVariant.Date: ap.DATE, QVariant.DateTime: ap.DATETIME,
    QVariant.Bool: ap.BOOL,
}


def kind_of(field_type: int) -> str:
    return _TYPES.get(field_type, ap.OTHER)


def geometry_kind(wkb_type) -> str:
    """A QGIS geometry type as core/append.py names it."""
    family = QgsWkbTypes.geometryType(wkb_type)
    if family == QgsWkbTypes.GeometryType.PointGeometry:
        return ap.MULTIPOINT if QgsWkbTypes.isMultiType(wkb_type) else ap.POINT
    if family == QgsWkbTypes.GeometryType.LineGeometry:
        return ap.LINE
    if family == QgsWkbTypes.GeometryType.PolygonGeometry:
        return ap.POLYGON
    return ap.NO_GEOMETRY


def plain(value: Any) -> Any:
    """A QGIS attribute value as core/append.py takes it (None, text, numbers, booleans, `datetime` in UTC, `date`)."""
    if isinstance(value, QVariant):
        value = None if value.isNull() else value.value()
    if value is None:
        return None
    if isinstance(value, QDateTime):
        return ap.moment(value.toMSecsSinceEpoch()) if value.isValid() else None
    if isinstance(value, QDate):
        return date(value.year(), value.month(), value.day()) if value.isValid() else None
    if isinstance(value, QTime):
        return value.toString("HH:mm:ss") if value.isValid() else None
    if isinstance(value, (bytes, bytearray)):
        return None
    if isinstance(value, (list, tuple, dict)):
        return str(value)
    if hasattr(value, "data") and type(value).__name__ == "QByteArray":
        return None
    return value


def qt_value(value: Any, kind: int) -> Any:
    """A value of core/append.py as a QGIS field of `kind` takes it."""
    if value is None:
        return None
    if isinstance(value, datetime):
        ms = int(round(value.timestamp() * 1000)) if value.tzinfo else int(round(value.astimezone().timestamp() * 1000))
        return QDateTime.fromMSecsSinceEpoch(ms)
    if isinstance(value, date):
        return QDate(value.year, value.month, value.day)
    return value


# ---- the target ------------------------------------------------------------------------------------------


@dataclass
class Target:
    """Where the features go: a QGIS layer of the project, or an ArcGIS feature layer by its URL."""
    #: "layer" (QGIS's edit buffer) or "esri" (the plugin's Esri client).
    kind: str
    name: str
    layer_id: str = ""
    url: str = ""
    schema: Optional[ap.TargetSchema] = None
    crs: QgsCoordinateReferenceSystem = field(default_factory=lambda: QgsCoordinateReferenceSystem(WGS84))
    #: An Esri layer's spatial reference as it says it ({"wkid": …}).
    spatial_reference: Optional[dict] = None
    #: A QGIS layer's geometry type (what its features take).
    wkb_type: Any = None
    #: A QGIS layer's features, read in a task (keys for updates).
    source: Any = None
    #: The QGIS layer of an Esri target, when one is open (reloaded after a write).
    shown_layer_id: str = ""

    @property
    def is_esri(self) -> bool:
        return self.kind == "esri"

    def key(self) -> str:
        """What the field map last used for it is kept under."""
        return self.url.split("?", 1)[0].rstrip("/").lower() if self.is_esri else f"layer:{self.name}"


def is_esri_layer(layer) -> bool:
    return isinstance(layer, QgsVectorLayer) and layer.providerType() == ESRI_PROVIDER and bool(layer_url(layer))


def layer_schema(layer: QgsVectorLayer) -> ap.TargetSchema:
    """A QGIS layer's fields (those it keeps itself) and geometry: its numbering, its constraints, its value maps as domains."""
    fields = layer.fields()
    provider = layer.dataProvider()
    pk = set(provider.pkAttributeIndexes()) if provider is not None else set()
    out = []
    for i, f in enumerate(fields):
        origin = fields.fieldOrigin(i)
        automatic = ""
        if origin not in (QgsFields.FieldOrigin.OriginProvider, QgsFields.FieldOrigin.OriginEdit):
            automatic = "式・結合の列"
        elif fields.fieldOriginIndex(i) in pk and origin == QgsFields.FieldOrigin.OriginProvider:
            automatic = "自動の番号"
        kind = kind_of(f.type())
        if not automatic and kind == ap.OTHER:
            automatic = "書き込めない型"
        constraints = f.constraints().constraints()
        nullable = not bool(constraints & QgsFieldConstraints.Constraint.ConstraintNotNull)
        default = bool(layer.defaultValueDefinition(i).expression())
        if not default and origin == QgsFields.FieldOrigin.OriginProvider and provider is not None:
            default = bool(provider.defaultValueClause(fields.fieldOriginIndex(i)))
        domain = None
        setup = f.editorWidgetSetup()
        if setup.type() == "ValueMap":
            coded = {}
            config = setup.config().get("map")
            for item in config if isinstance(config, list) else [config] if isinstance(config, dict) else []:
                if isinstance(item, dict):
                    for description, value in item.items():
                        if value != "{2839923C-8B7D-419E-B84B-CA2FE9B80EC7}":  # QGIS's 「空」 entry
                            coded[value] = description
            if coded:
                domain = ap.Domain(coded=coded)
        length = f.length() if kind == ap.STRING and f.length() > 0 else 0
        out.append(ap.TargetField(f.name(), kind, f.alias(), length, nullable=nullable, editable=not automatic, automatic=automatic,
                                  domain=domain, has_default=default, type_text=f.typeName()))
    wkb = layer.wkbType()
    return ap.TargetSchema(out, geometry_kind(wkb) if layer.isSpatial() else ap.NO_GEOMETRY, layer.name(), QgsWkbTypes.hasZ(wkb),
                           QgsWkbTypes.hasM(wkb))


def target_for_layer(layer: QgsVectorLayer) -> Target:
    """The target of a project's layer (main thread): an ArcGIS feature layer QGIS opened is written by its URL."""
    if is_esri_layer(layer):
        return Target("esri", layer.name(), url=layer_url(layer), shown_layer_id=layer.id())
    return Target("layer", layer.name(), layer_id=layer.id(), schema=layer_schema(layer), crs=layer.crs(), wkb_type=layer.wkbType(),
                  source=QgsVectorLayerFeatureSource(layer))


def target_for_url(url: str, name: str = "") -> Target:
    """An ArcGIS feature layer by its URL (described in a task: `describe_esri`)."""
    url = url.strip()
    return Target("esri", name or _url_name(url), url=url)


def _url_name(url: str) -> str:
    """`…/services/Things/FeatureServer/0` → 「Things/0」."""
    parts = url.split("?", 1)[0].rstrip("/").split("/")
    return "/".join(parts[-3::2]) if len(parts) > 3 else url


def describe_esri(target: Target, client: EsriClient, token: Optional[str]) -> Target:
    """An Esri target's fields and spatial reference (in a task: its layer information)."""
    info = client.layer(target.url, token, refresh=True)
    tracked = {name for name, _kind in edit_stamp.tracked_fields(info.field_types).values()}
    target.schema = ap.esri_schema(info, tracked)
    if target.schema.name and target.name in ("", target.url, _url_name(target.url)):
        target.name = f"{target.schema.name}（{_url_name(target.url)}）"
    raw = info.raw
    sr = raw.get("sourceSpatialReference") or (raw.get("extent") or {}).get("spatialReference") or raw.get("spatialReference")
    target.spatial_reference = None
    target.crs = QgsCoordinateReferenceSystem(WGS84)
    if isinstance(sr, dict) and (sr.get("wkid") or sr.get("latestWkid") or sr.get("wkt")):
        crs = QgsArcGisRestUtils.convertSpatialReference(sr)
        if crs.isValid():
            target.crs = crs
            target.spatial_reference = {k: sr[k] for k in ("wkid", "latestWkid", "wkt") if sr.get(k)}
    if target.spatial_reference is None:
        target.spatial_reference = {"wkid": 4326}
    return target


def own_tables(context) -> list:
    """The settings file's tables the plugin's own screens write (観測記録・撮像依頼…): appended to by 管理者 only."""
    if context is None:
        return []
    others = {"起動時レイヤー", "背景地図", "サーバ処理", "AI モデル", "文章の手伝い", "雲量予報", "移動体の情報源"}
    try:
        services = context.services()
    except Exception:  # noqa: BLE001 (no settings file read yet: none)
        return []
    return [s for s in services if s.role not in others]


def own_table_of(url: str, context) -> Optional[str]:
    """The role of the plugin's own table at `url` (「観測記録の保存先」…), or None."""
    for s in own_tables(context):
        if ap.same_table(url, s.url):
            return s.role
    return None


def config_tables(context) -> list:
    """The settings file's ArcGIS feature layers (`…/FeatureServer/0`, `…/MapServer/2`): (label, role, URL)."""
    import re

    if context is None:
        return []
    try:
        services = context.services()
    except Exception:  # noqa: BLE001
        return []
    layer_url = re.compile(r"/(FeatureServer|MapServer)/\d+/?$", re.IGNORECASE)
    out = [(s.label, s.role, s.url) for s in services if layer_url.search(s.url or "")]
    # 起動時レイヤー of Esri: the service's URL and the layer's id.
    for x in getattr(getattr(context, "config", None), "startup_layers", None) or ():
        if x.type == "esri" and re.search(r"/(FeatureServer|MapServer)/?$", x.url or "", re.IGNORECASE) and str(x.layer).isdigit():
            url = f"{x.url.rstrip('/')}/{x.layer}"
            out.append((_url_name(url), "起動時レイヤー", url))
    return out


# ---- the inputs -------------------------------------------------------------------------------------------


def fields_of(layer: QgsVectorLayer) -> list:
    """A layer's fields as an input offers them to the field map."""
    return [ap.SourceField(f.name(), kind_of(f.type()), f.alias()) for f in layer.fields()]


class InputSource:
    """An input layer, taken on the main thread (its features read in a task)."""

    def __init__(self, layer: QgsVectorLayer, selected_only: bool = False, condition: str = "", name: str = ""):
        self.name = name or layer.name()
        self.layer_id = layer.id()
        self.source = QgsVectorLayerFeatureSource(layer)
        self.crs = layer.crs()
        self.qgs_fields = QgsFields(layer.fields())
        provider = layer.dataProvider()
        pk = set(provider.pkAttributeIndexes()) if provider is not None and layer.providerType() != "memory" else set()
        fields = layer.fields()
        self.pk = {fields.at(i).name() for i in range(fields.count())
                   if fields.fieldOrigin(i) == QgsFields.FieldOrigin.OriginProvider and fields.fieldOriginIndex(i) in pk}
        #: Its fields (all of them for the field map; without its own numbering for スキーマタイプ TEST).
        self.fields = fields_of(layer)
        self.geometry = geometry_kind(layer.wkbType()) if layer.isSpatial() else ap.NO_GEOMETRY
        self.context = QgsExpressionContext(QgsExpressionContextUtils.globalProjectLayerScopes(layer))
        self.selection = list(layer.selectedFeatureIds()) if selected_only else None
        self.condition = (condition or "").strip()
        self.count = len(self.selection) if self.selection is not None else layer.featureCount()

    @property
    def test_fields(self) -> list:
        return [f for f in self.fields if f.name not in self.pk]

    def condition_problem(self) -> str:
        if not self.condition:
            return ""
        e = QgsExpression(self.condition)
        if e.hasParserError():
            return f"絞り込みの式の誤り（{e.parserErrorString()}）"
        names = {f.name() for f in self.qgs_fields}
        missing = sorted(c for c in e.referencedColumns() if c != QgsFeatureRequest.ALL_ATTRIBUTES and c not in names)
        if missing:
            return f"絞り込みの式の誤り（「{self.name}」に列「{'」「'.join(missing)}」がない）"
        return ""


def _transform(source: QgsCoordinateReferenceSystem, target: QgsCoordinateReferenceSystem) -> Optional[QgsCoordinateTransform]:
    if not source.isValid() or not target.isValid() or source == target:
        return None
    return QgsCoordinateTransform(source, target, QgsProject.instance())


def fit_geometry(g: Optional[QgsGeometry], transform: Optional[QgsCoordinateTransform], target: Target) -> tuple[Any, str, str]:
    """
    A feature's geometry as the target takes it (its CRS, single or multi, Z
    and M; Esri JSON for an Esri layer), why it cannot, or what was done to it.
    """
    schema = target.schema
    if schema is None or schema.geometry == ap.NO_GEOMETRY:
        return None, "", ""
    if g is None or g.isNull() or g.isEmpty():
        return None, "", "形状なしで追加"
    g = QgsGeometry(g)
    own = geometry_kind(g.wkbType())
    family = {ap.MULTIPOINT: ap.POINT}.get(own, own)
    wanted = {ap.MULTIPOINT: ap.POINT}.get(schema.geometry, schema.geometry)
    if family != wanted:
        return None, f"形状が{ap.GEOMETRY_NAMES[schema.geometry]}でない（{ap.GEOMETRY_NAMES.get(own, own)}）", ""
    note = ""
    if transform is not None:
        try:
            g.transform(transform)
        except Exception:  # noqa: BLE001 (outside the CRS's area)
            return None, "座標を追加先の座標系に変換できない", ""
    single = (schema.geometry == ap.POINT) if target.is_esri else not QgsWkbTypes.isMultiType(target.wkb_type)
    if single and g.isMultipart():
        parts = g.asGeometryCollection()
        if len(parts) != 1:
            return None, f"マルチの形状（{len(parts)} 個）は 1 つの{ap.GEOMETRY_NAMES[schema.geometry]}に入らない", ""
        g = parts[0]
        note = "マルチの形状を 1 つに変換"
    if target.is_esri:
        if QgsWkbTypes.isCurvedType(g.wkbType()):
            g.convertToStraightSegment()
        if not schema.has_z and QgsWkbTypes.hasZ(g.wkbType()):
            g.get().dropZValue()
        if not schema.has_m and QgsWkbTypes.hasM(g.wkbType()):
            g.get().dropMValue()
        if schema.geometry == ap.MULTIPOINT and not g.isMultipart():
            g.convertToMultiType()
        value = QgsArcGisRestUtils.geometryToJson(g, QgsArcGisRestContext())
        if not value:
            return None, "形状を Esri の形に変換できない", ""
        value = dict(value)
        value["spatialReference"] = dict(target.spatial_reference or {"wkid": 4326})
        return value, "", note
    coerced = g.coerceToType(target.wkb_type)
    if len(coerced) != 1 or coerced[0].isNull():
        return None, f"形状を{ap.GEOMETRY_NAMES[schema.geometry]}に変換できない", ""
    return coerced[0], "", note


def _expressions(source: InputSource, field_map: ap.FieldMap) -> dict:
    """The field map's expressions for an input: target field → QgsExpression (prepared), or an ExpressionError."""
    out = {}
    for entry in field_map.entries:
        rule = entry.rule(source.name)
        if rule is None or rule.source != ap.EXPRESSION:
            continue
        e = QgsExpression(rule.expression)
        if e.hasParserError():
            out[entry.target] = ap.ExpressionError(e.parserErrorString())
            continue
        e.prepare(source.context)
        out[entry.target] = e
    return out


def read_rows(inputs: Sequence[InputSource], field_map: ap.FieldMap, target: Target, skipped: Sequence[str] = (),
              check: Callable[[], None] = lambda: None, progress: Callable[[float], None] = lambda _p: None) -> list:
    """Every input feature (its selection, its condition) as a SourceRow (in a task)."""
    rows = []
    total = max(1, sum(s.count for s in inputs if s.name not in skipped))
    seen = 0
    target_crs = target.crs
    for source in inputs:
        if source.name in skipped:
            continue
        request = QgsFeatureRequest()
        # (a request has one filter: the selection's ids, the condition then checked feature by feature)
        condition = None
        if source.selection is not None:
            request.setFilterFids(source.selection)
            if source.condition:
                condition = QgsExpression(source.condition)
                condition.prepare(source.context)
        elif source.condition:
            request.setFilterExpression(source.condition)
            request.setExpressionContext(source.context)
        no_geometry = target.schema is not None and target.schema.geometry == ap.NO_GEOMETRY
        if no_geometry and (condition is None or not condition.needsGeometry()):
            request.setFlags(QgsFeatureRequest.Flag.NoGeometry)
        expressions = _expressions(source, field_map)
        transform = _transform(source.crs, target_crs) if not no_geometry and target.schema is not None else None
        names = [f.name for f in source.fields]
        context = QgsExpressionContext(source.context)
        for feature in source.source.getFeatures(request):
            check()
            if condition is not None:
                context.setFeature(feature)
                if not condition.evaluate(context):
                    continue
            values = {n: plain(feature.attribute(i)) for i, n in enumerate(names)}
            evaluated = {}
            if expressions:
                context.setFeature(feature)
                for name, e in expressions.items():
                    if isinstance(e, ap.ExpressionError):
                        evaluated[name] = e
                        continue
                    v = e.evaluate(context)
                    evaluated[name] = ap.ExpressionError(e.evalErrorString()) if e.hasEvalError() else plain(v)
            geometry, problem, note = fit_geometry(feature.geometry() if feature.hasGeometry() else None, transform, target)
            rows.append(ap.SourceRow(source.name, feature.id(), values, evaluated, geometry, problem, note))
            seen += 1
            if seen % 200 == 0:
                progress(min(99.0, 100.0 * seen / total))
    return rows


def layer_existing(target: Target, match_fields: Sequence[str], keys: set, out_fields: Sequence[str], geometry: bool,
                   check: Callable[[], None] = lambda: None) -> dict:
    """A QGIS target's features with these keys (key → [Existing]), with what an update changes as it is (in a task)."""
    schema = target.schema
    fields = [schema.field(k) for k in match_fields]
    fields = [f for f in fields if f is not None]
    if not fields or not keys or target.source is None:
        return {}
    request = QgsFeatureRequest()
    if not geometry:
        request.setFlags(QgsFeatureRequest.Flag.NoGeometry)
    out: dict = {}
    for feature in target.source.getFeatures(request):
        check()
        key = tuple(ap.key_text(ap.convert(plain(feature[f.name]), ap.TargetField(f.name, f.type)).value) for f in fields)
        if key in keys:
            attributes = {n: feature[n] for n in out_fields}
            out.setdefault(key, []).append(ap.Existing(feature.id(), attributes, QgsGeometry(feature.geometry()) if geometry else None))
    return out


# ---- the whole -------------------------------------------------------------------------------------------


@dataclass
class Job:
    """One append: the inputs, the target, the field map and the options."""
    inputs: list
    target: Target
    field_map: ap.FieldMap
    options: ap.Options = field(default_factory=ap.Options)
    #: The field map's problems (fields an input does not have …), shown with the check.
    map_problems: list = field(default_factory=list)


def input_problems(job: Job) -> dict:
    """The inputs whose fields do not match the target's (スキーマタイプ TEST / TEST_AND_SKIP): name → why."""
    if job.options.schema_type == ap.NO_TEST or job.target.schema is None:
        return {}
    out = {}
    for source in job.inputs:
        problems = ap.schema_problems(job.target.schema, source.test_fields)
        if problems:
            out[source.name] = "、".join(problems[:4]) + (f" ほか {len(problems) - 4} 件" if len(problems) > 4 else "")
    return out


def used_map(job: Job) -> ap.FieldMap:
    """The field map that applies: the names alone for スキーマタイプ TEST / TEST_AND_SKIP."""
    if job.options.schema_type == ap.NO_TEST:
        return job.field_map
    return ap.auto_map(job.target.schema, [(s.name, s.fields) for s in job.inputs])


def prepare(job: Job, client: Optional[EsriClient] = None, token: Optional[str] = None, check: Callable[[], None] = lambda: None,
            progress: Callable[[float], None] = lambda _p: None) -> ap.Plan:
    """What each input feature becomes (in a task): read, mapped, checked and, with key fields, matched against the target."""
    if job.target.is_esri and job.target.schema is None:
        describe_esri(job.target, client, token)
    skipped = input_problems(job)
    if job.options.schema_type == ap.TEST and skipped:
        return ap.plan([], job.field_map, job.target.schema, job.options, skipped_inputs=skipped)
    field_map = used_map(job)
    rows = read_rows(job.inputs, field_map, job.target, list(skipped), check, progress)
    existing = None
    options = job.options
    if options.match_fields:
        # The keys first (the plan's own values), then the target's features that have them.
        trial = ap.plan(rows, field_map, job.target.schema, replace(options, match_fields=[]), skipped_inputs=skipped)
        keys = set()
        names = [job.target.schema.field(k).name for k in options.match_fields if job.target.schema.field(k) is not None]
        for p in trial.rows:
            if p.action == ap.ADD:
                key = tuple(ap.key_text(p.values.get(n)) for n in names)
                if all(k is not None for k in key):
                    keys.add(key)
        out_fields = [e.target for e in field_map.entries
                      if any(ap.fills(e.rule(s.name)) for s in job.inputs) and job.target.schema.field(e.target) is not None
                      and job.target.schema.field(e.target).editable]
        if job.target.is_esri:
            existing = ap.esri_existing(client, job.target.url, token, job.target.schema, names, keys, out_fields, options.update_geometry, check)
        else:
            existing = layer_existing(job.target, names, keys, out_fields, options.update_geometry, check)
    progress(100.0)
    return ap.plan(rows, field_map, job.target.schema, options, existing, skipped)


def write_esri(job: Job, the_plan: ap.Plan, client: EsriClient, token: Optional[str], check: Callable[[], None] = lambda: None,
               progress: Callable[[float], None] = lambda _p: None) -> ap.Applied:
    """The plan sent to an Esri target (in a task)."""
    return ap.write_esri(client, job.target.url, token, the_plan, job.target.schema, job.options.all_or_nothing, check, progress)


def target_layer(target: Target, project: Optional[QgsProject] = None) -> Optional[QgsVectorLayer]:
    layer = (project or QgsProject.instance()).mapLayer(target.layer_id or target.shown_layer_id)
    return layer if isinstance(layer, QgsVectorLayer) else None


def write_layer(job: Job, the_plan: ap.Plan, layer: Optional[QgsVectorLayer] = None) -> ap.Applied:
    """
    The plan written into a QGIS target (main thread): into its edit buffer,
    then saved, or left for the person to save when they are editing it. A
    save that fails is taken back whole.
    """
    layer = layer or target_layer(job.target)
    if layer is None:
        raise ValueError("追加先のレイヤーが閉じられた")
    caps = layer.dataProvider().capabilities()
    adds = [p for p in the_plan.writes if p.action == ap.ADD]
    updates = [p for p in the_plan.writes if p.action == ap.UPDATE]
    if adds and not caps & QgsVectorDataProvider.Capability.AddFeatures:
        raise ValueError("このレイヤーには地物を追加できない")
    if updates and not caps & QgsVectorDataProvider.Capability.ChangeAttributeValues:
        raise ValueError("このレイヤーの地物は変更できない")
    editing = layer.isEditable()
    if not editing and not layer.startEditing():
        raise ValueError("このレイヤーは編集できない")
    applied = ap.Applied(in_buffer=editing)
    fields = layer.fields()
    kinds = {f.name(): f.type() for f in fields}
    layer.beginEditCommand("アペンド")
    temp_ids = []
    try:
        features = []
        for p in adds:
            attributes = {fields.indexOf(k): qt_value(v, kinds.get(k)) for k, v in p.values.items() if fields.indexOf(k) >= 0}
            geometry = p.geometry if isinstance(p.geometry, QgsGeometry) else QgsGeometry()
            features.append(QgsVectorLayerUtils.createFeature(layer, geometry, attributes, layer.createExpressionContext()))
        for f in features:
            # One by one: the feature gets its id in the edit buffer (taken back by it).
            if not layer.addFeature(f):
                raise ValueError("地物を追加できない")
            temp_ids.append(f.id())
        for p in updates:
            before = the_plan.before.get(p.target_id)
            for k, v in p.values.items():
                index = fields.indexOf(k)
                if index >= 0:
                    layer.changeAttributeValue(p.target_id, index, qt_value(v, kinds.get(k)))
            if isinstance(p.geometry, QgsGeometry):
                layer.changeGeometry(p.target_id, p.geometry)
            applied.updated.append((p.target_id, dict(before.attributes) if before else {},
                                    before.geometry if before is not None and isinstance(p.geometry, QgsGeometry) else None))
        layer.endEditCommand()
    except Exception:
        layer.destroyEditCommand()
        if not editing:
            layer.rollBack()
        raise
    if editing:
        applied.added = temp_ids
        return applied
    added_ids: list = []

    def committed(_layer_id, added) -> None:
        added_ids.extend(f.id() for f in added)

    layer.committedFeaturesAdded.connect(committed)
    try:
        ok = layer.commitChanges()
    finally:
        try:
            layer.committedFeaturesAdded.disconnect(committed)
        except (TypeError, RuntimeError):
            pass
    if not ok:
        errors = [e for e in layer.commitErrors() if e]
        layer.rollBack()
        why = errors[-1] if errors else "保存に失敗"
        applied = ap.Applied(failed=[(p, why) for p in the_plan.writes], rolled_back=True)
        return applied
    applied.added = added_ids or temp_ids
    return applied


def undo_layer(job: Job, applied: ap.Applied, layer: Optional[QgsVectorLayer] = None) -> list:
    """Takes back a write into a QGIS target (main thread): what could not be taken back."""
    layer = layer or target_layer(job.target)
    if layer is None:
        return ["追加先のレイヤーが閉じられた"]
    editing = layer.isEditable()
    if not editing and not layer.startEditing():
        return ["このレイヤーは編集できない"]
    fields = layer.fields()
    layer.beginEditCommand("アペンドの取り消し")
    if applied.added:
        layer.deleteFeatures(list(applied.added))
    for fid, attributes, geometry in applied.updated:
        for k, v in attributes.items():
            index = fields.indexOf(k)
            if index >= 0:
                layer.changeAttributeValue(fid, index, v)
        if isinstance(geometry, QgsGeometry):
            layer.changeGeometry(fid, geometry)
    layer.endEditCommand()
    if editing:
        return []
    if not layer.commitChanges():
        errors = [e for e in layer.commitErrors() if e]
        layer.rollBack()
        return errors[-1:] or ["保存に失敗"]
    return []


def reload_shown(target: Target) -> None:
    """The QGIS layer of an Esri target drawn again with what was written (main thread)."""
    layer = target_layer(target)
    if layer is not None:
        layer.dataProvider().reloadData()
        layer.triggerRepaint()

