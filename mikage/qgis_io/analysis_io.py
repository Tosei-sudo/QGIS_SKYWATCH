"""
「データ分析」 in QGIS (core/analysis.py): the project's layers and tables as
tables of a query, reading them off the main thread, and a result as a
layer of the project.
"""
from __future__ import annotations

from typing import Any, Callable, Optional, Sequence

from qgis.core import (
    QgsCoordinateReferenceSystem, QgsCoordinateTransform, QgsExpression, QgsExpressionContext, QgsExpressionContextUtils, QgsFeature,
    QgsFeatureRequest, QgsField, QgsGeometry, QgsProject, QgsVectorLayer, QgsVectorLayerFeatureSource, QgsWkbTypes,
)
from qgis.PyQt.QtCore import QDate, QDateTime, QMetaType, QTime, QVariant

from ..core.analysis import (
    DEFAULT_TABLE_LIMIT, SHAPE, AnalysisError, Column, Database, Result, Table, column_type, read_esri, referenced, table_name, unique_name,
    wants_shape,
)

WGS84 = "EPSG:4326"


def qt_value(value: Any) -> Any:
    """A feature's attribute as SQLite keeps it (dates as text, null as None)."""
    if value is None:
        return None
    if isinstance(value, QVariant):
        if value.isNull():
            return None
        value = value.value()
    if isinstance(value, QDateTime):
        return value.toString("yyyy-MM-dd HH:mm:ss") if value.isValid() else None
    if isinstance(value, QDate):
        return value.toString("yyyy-MM-dd") if value.isValid() else None
    if isinstance(value, QTime):
        return value.toString("HH:mm:ss") if value.isValid() else None
    if isinstance(value, (list, tuple, dict)):
        return str(value)
    if hasattr(value, "data") and type(value).__name__ == "QByteArray":
        return bytes(value)
    return value


def _sql_type(f: QgsField) -> str:
    if not f.isNumeric():
        return "TEXT"
    name = (f.typeName() or "").lower()
    if any(k in name for k in ("real", "double", "float")) or (f.precision() > 0 and any(k in name for k in ("numeric", "decimal"))):
        return "REAL"
    try:
        return "REAL" if int(f.type()) == int(QVariant.Double) else "INTEGER"
    except (TypeError, AttributeError):
        return "INTEGER"


def project_tables(project: Optional[QgsProject] = None, taken: Optional[set] = None, skip: Callable[[QgsVectorLayer], bool] = lambda _l: False) -> list[Table]:
    """Every vector layer and table of the project (a query names it by its layer name made a name)."""
    project = project or QgsProject.instance()
    taken = taken if taken is not None else set()
    out = []
    root = project.layerTreeRoot()
    ordered = [n.layer() for n in root.findLayers()] if root is not None else []
    rest = [l for l in project.mapLayers().values() if l not in ordered]
    for layer in [*ordered, *rest]:
        if not isinstance(layer, QgsVectorLayer) or not layer.isValid() or skip(layer):
            continue
        name = unique_name(table_name(layer.name()), taken)
        columns = [Column(f.name(), _sql_type(f), "", f.alias() or "") for f in layer.fields()]
        if layer.isSpatial() and not any(c.name.upper() == SHAPE for c in columns):
            columns.append(Column(SHAPE, "TEXT", "", "形状（WKT）"))
        out.append(Table(name=name, title=layer.name(), source="layer", layer_id=layer.id(), spatial=layer.isSpatial(), columns=columns))
    return out


class LayerReader:
    """A project layer read off the main thread (made on the main thread: its features through a feature source)."""

    def __init__(self, table: Table, layer: QgsVectorLayer, expression: str = "", shape: bool = False, limit: int = DEFAULT_TABLE_LIMIT):
        self.table = table
        self.limit = limit
        self.source = QgsVectorLayerFeatureSource(layer)
        self.fields = [f.name() for f in layer.fields()]
        self.columns = [Column(f.name(), _sql_type(f), "", f.alias() or "") for f in layer.fields()]
        self.shape = shape and layer.isSpatial() and not any(n.upper() == SHAPE for n in self.fields)
        if self.shape:
            self.columns.append(Column(SHAPE, "TEXT", "", "形状（WKT）"))
        self.request = QgsFeatureRequest()
        if not self.shape:
            self.request.setFlags(QgsFeatureRequest.Flag.NoGeometry)
        if expression and expression.strip():
            e = QgsExpression(expression)
            if e.hasParserError():
                raise AnalysisError(f"{table.name} の条件（QGIS の式）が読めない: {e.parserErrorString()}")
            self.request.setFilterExpression(expression)
            context = QgsExpressionContext()
            context.appendScopes(QgsExpressionContextUtils.globalProjectLayerScopes(layer))
            self.request.setExpressionContext(context)
        crs = QgsCoordinateReferenceSystem(WGS84)
        self.transform = QgsCoordinateTransform(layer.crs(), crs, QgsProject.instance()) if self.shape and layer.crs() != crs else None

    def read(self, cancelled: Optional[Callable[[], bool]] = None) -> tuple[list[Column], list[list[Any]]]:
        rows = []
        f = QgsFeature()
        it = self.source.getFeatures(self.request)
        while it.nextFeature(f):
            if cancelled and len(rows) % 1000 == 0 and cancelled():
                raise AnalysisError("取消")
            if len(rows) >= self.limit:
                raise AnalysisError(f"{self.table.name}: 件数が上限（{self.limit:,} 件）を超えたため読み込みを中止（条件で絞る）")
            row = [qt_value(v) for v in f.attributes()]
            if self.shape:
                g = f.geometry()
                if g is None or g.isNull():
                    row.append(None)
                else:
                    g = QgsGeometry(g)
                    if self.transform is not None:
                        g.transform(self.transform)
                    row.append(g.asWkt(8))
            rows.append(row)
        return self.columns, rows


def prepare(tables: Sequence[Table], sql: str, conditions: dict, limit: int = DEFAULT_TABLE_LIMIT,
            project: Optional[QgsProject] = None) -> list[tuple[Table, Optional[LayerReader]]]:
    """The tables the query names, each with its reader when it is a project layer (on the main thread)."""
    project = project or QgsProject.instance()
    by_name = {t.name: t for t in tables}
    used = referenced(sql, list(by_name))
    if not used:
        raise AnalysisError("SQL に左の一覧の表が 1 つもない")
    shape = wants_shape(sql)
    out = []
    for name in used:
        t = by_name[name]
        if t.source == "layer":
            layer = project.mapLayer(t.layer_id)
            if not isinstance(layer, QgsVectorLayer) or not layer.isValid():
                raise AnalysisError(f"{t.name} のレイヤー（{t.title}）がプロジェクトにない")
            out.append((t, LayerReader(t, layer, conditions.get(t.name, ""), shape, limit)))
        else:
            out.append((t, None))
    return out


def run(client: Any, prepared: Sequence[tuple[Table, Optional[LayerReader]]], sql: str, values: dict, conditions: dict,
        token_for: Callable[[Table], Optional[str]], limit: int = DEFAULT_TABLE_LIMIT, task: Any = None) -> tuple[Result, dict]:
    """Reads the tables and runs the query (in a task); the result and how many rows each table gave."""
    cancelled = (lambda: task.isCanceled()) if task is not None else None
    shape = wants_shape(sql)
    db = Database()
    counts: dict[str, int] = {}
    try:
        for i, (t, reader) in enumerate(prepared):
            if task is not None:
                task.progress(100 * i / max(1, len(prepared) + 1))
            if reader is not None:
                columns, rows = reader.read(cancelled)
            else:
                columns, rows = read_esri(client, t, conditions.get(t.name, ""), shape, limit, token_for(t), cancelled)
            db.add(t.name, columns, rows)
            counts[t.name] = len(rows)
        result = db.run(sql, values, cancelled)
    finally:
        db.close()
    return result, counts


def _field(name: str, kind: str) -> QgsField:
    meta = {"INTEGER": "LongLong", "REAL": "Double", "TEXT": "QString"}[kind]
    var = {"INTEGER": "LongLong", "REAL": "Double", "TEXT": "String"}[kind]
    try:
        return QgsField(name, getattr(QMetaType.Type, meta))
    except (TypeError, AttributeError):
        return QgsField(name, getattr(QVariant, var))


def result_layer(result: Result, name: str) -> QgsVectorLayer:
    """A memory layer of the result: the column SHAPE (WKT, WGS 84) as its shapes when it has one, else a table."""
    columns = list(result.columns)
    shape_at = next((i for i, c in enumerate(columns) if c.upper() == SHAPE), None)
    geometry_type = None
    if shape_at is not None:
        for r in result.rows:
            g = QgsGeometry.fromWkt(r[shape_at]) if isinstance(r[shape_at], str) else None
            if g is not None and not g.isNull():
                geometry_type = QgsWkbTypes.multiType(QgsWkbTypes.flatType(g.wkbType()))
                break
    uri = (QgsWkbTypes.displayString(geometry_type) if geometry_type is not None else "None") + (f"?crs={WGS84}" if geometry_type is not None else "")
    layer = QgsVectorLayer(uri, name, "memory")
    keep = [i for i in range(len(columns)) if i != shape_at]
    seen: set = set()
    fields = []
    for i in keep:
        n = columns[i] or f"col{i + 1}"
        while n.lower() in seen:
            n += "_"
        seen.add(n.lower())
        fields.append(_field(n, column_type(r[i] for r in result.rows)))
    provider = layer.dataProvider()
    provider.addAttributes(fields)
    layer.updateFields()
    features = []
    for r in result.rows:
        f = QgsFeature(layer.fields())
        f.setAttributes([r[i] if not isinstance(r[i], bytes) else r[i].hex() for i in keep])
        if geometry_type is not None and isinstance(r[shape_at], str):
            g = QgsGeometry.fromWkt(r[shape_at])
            if not g.isNull():
                g.convertToMultiType()
                if QgsWkbTypes.flatType(g.wkbType()) == geometry_type:
                    f.setGeometry(g)
        features.append(f)
    provider.addFeatures(features)
    layer.updateExtents()
    return layer
