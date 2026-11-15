"""
Shapes received from outside (core/link_shapes.py) as temporary layers: one
memory layer per kind (点・線・面) of each sending, at the top of the group
「受け取った図形」, labelled when the sender labelled them, then the map
there — as the viewer's 「リンクの図形」 (main.ts addLinkShapes).
"""
from __future__ import annotations

from datetime import datetime
from typing import Optional, Sequence

from qgis.core import (
    QgsCoordinateReferenceSystem, QgsCoordinateTransform, QgsFeature, QgsFillSymbol, QgsGeometry, QgsLineSymbol, QgsMarkerSymbol,
    QgsPalLayerSettings, QgsPointXY, QgsProject, QgsRectangle, QgsSingleSymbolRenderer, QgsTextBufferSettings, QgsTextFormat,
    QgsVectorLayer, QgsVectorLayerSimpleLabeling,
)
from qgis.PyQt.QtGui import QColor

from ..core.link_shapes import POINT_ZOOM, LinkShape, LinkShapes, bbox_of, family_of, to_wkt, zoom_scale
from .plan_layer import make_field

#: The group the layers go in (at the top of the layer list).
GROUP = "受け取った図形"
#: Each layer's custom property: who sent it.
SENDER_PROPERTY = "mikage/received_from"
#: The colour when the sender gives none (the viewer's link shapes are coloured in turn; here one, apart from the 座標へ移動 mark).
DEFAULT_COLOR = "#e11d48"
#: Each kind's layer type (single, multi) and its word in the layer's name.
KINDS = {"point": ("Point", "MultiPoint", "点"), "line": ("LineString", "MultiLineString", "線"), "polygon": ("Polygon", "MultiPolygon", "面")}
#: Pixels kept around the shapes when zooming to them.
PADDING = 40
WGS84 = QgsCoordinateReferenceSystem("EPSG:4326")


def layers_of_shapes(shapes: LinkShapes, sender: str) -> list[QgsVectorLayer]:
    """The memory layers (WGS 84) of the shapes: one per kind, polygons first (lowest), named after the sending."""
    by_kind: dict[str, list[LinkShape]] = {}
    for s in shapes.shapes:
        by_kind.setdefault(family_of(s.geometry), []).append(s)
    received = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    layers = []
    for kind in ("polygon", "line", "point"):
        items = by_kind.get(kind)
        if not items:
            continue
        single, multi, word = KINDS[kind]
        is_multi = any(s.geometry["type"].startswith("Multi") for s in items)
        name = shapes.title if len(by_kind) == 1 else f"{shapes.title}（{word}）"
        layer = QgsVectorLayer(f"{multi if is_multi else single}?crs=EPSG:4326", name, "memory")
        names = ["label", *(n for n, _ in shapes.fields)]
        fields = [make_field("label", "string"), *(make_field(n, t) for n, t in shapes.fields)]
        provider = layer.dataProvider()
        provider.addAttributes(fields)
        layer.updateFields()
        layer.setFieldAlias(0, "ラベル")
        features = []
        for s in items:
            g = QgsGeometry.fromWkt(to_wkt(s.geometry))
            if g.isNull():
                continue
            if is_multi and not g.isMultipart():
                g.convertToMultiType()
            f = QgsFeature(layer.fields())
            f.setGeometry(g)
            f.setAttributes([s.label, *(s.properties.get(n) for n in names[1:])])
            features.append(f)
        provider.addFeatures(features)
        layer.updateExtents()
        layer.setCustomProperty(SENDER_PROPERTY, sender)
        # Temporary: QGIS need not offer to keep them when the project closes.
        layer.setCustomProperty("skipMemoryLayersCheck", 1)
        layer.setAbstract(f"外部から受け取った図形（MIKAGE、{received}、送信元: {sender}）")
        _style(layer, kind, shapes.color or DEFAULT_COLOR, shapes.labeled)
        layers.append(layer)
    return layers


def _style(layer: QgsVectorLayer, kind: str, color: str, labeled: bool) -> None:
    c = QColor(color)
    rgb = f"{c.red()},{c.green()},{c.blue()}"
    if kind == "point":
        symbol = QgsMarkerSymbol.createSimple({"name": "circle", "color": rgb, "outline_color": "255,255,255", "outline_width": "0.5",
                                               "size": "3.2"})
    elif kind == "line":
        symbol = QgsLineSymbol.createSimple({"line_color": rgb, "line_width": "0.8"})
    else:
        symbol = QgsFillSymbol.createSimple({"color": rgb + ",64", "outline_color": rgb, "outline_width": "0.7"})
    layer.setRenderer(QgsSingleSymbolRenderer(symbol))
    if not labeled:
        return
    text = QgsTextFormat()
    text.setSize(9)
    text.setColor(QColor("#111111"))
    buffer = QgsTextBufferSettings()
    buffer.setEnabled(True)
    buffer.setColor(QColor("#ffffff"))
    buffer.setSize(1.0)
    text.setBuffer(buffer)
    labels = QgsPalLayerSettings()
    labels.fieldName = "label"
    labels.setFormat(text)
    layer.setLabeling(QgsVectorLayerSimpleLabeling(labels))
    layer.setLabelsEnabled(True)


def add_layers(layers: Sequence[QgsVectorLayer], project: Optional[QgsProject] = None) -> None:
    """The layers at the top of the group 「受け取った図形」 (made at the top of the list when missing), points uppermost."""
    project = project or QgsProject.instance()
    root = project.layerTreeRoot()
    group = root.findGroup(GROUP)
    if group is None:
        group = root.insertGroup(0, GROUP)
    group.setExpanded(True)
    group.setItemVisibilityChecked(True)
    for layer in layers:
        project.addMapLayer(layer, False)
        group.insertLayer(0, layer)


def zoom_to(canvas, shapes: LinkShapes) -> None:
    """
    The map at the shapes, as the viewer: a single point centred at the
    zoom given (else POINT_ZOOM); points alone fitted but no closer than
    that; anything else fitted.
    """
    box = bbox_of(shapes.shapes)
    if box is None:
        return
    to_map = QgsCoordinateTransform(WGS84, canvas.mapSettings().destinationCrs(), QgsProject.instance())
    scale = zoom_scale(shapes.zoom if shapes.zoom is not None else POINT_ZOOM)
    points_only = all(s.geometry["type"] in ("Point", "MultiPoint") for s in shapes.shapes)
    # One point (or points all at one place).
    single = box[0] == box[2] and box[1] == box[3]
    empty = canvas.extent().isEmpty() or canvas.extent().width() == 0
    if single:
        center = to_map.transform(QgsPointXY(box[0], box[1]))
        if empty:
            canvas.setExtent(QgsRectangle(center.x() - 1, center.y() - 1, center.x() + 1, center.y() + 1))
        canvas.setCenter(center)
        canvas.zoomScale(scale)
    else:
        try:
            rect = to_map.transformBoundingBox(QgsRectangle(*box))
        except Exception:  # noqa: BLE001 (outside the map's CRS: centred instead)
            rect = None
        if rect is None or rect.isEmpty():
            canvas.setCenter(to_map.transform(QgsPointXY((box[0] + box[2]) / 2, (box[1] + box[3]) / 2)))
        else:
            canvas.setExtent(rect)
            rect.grow(PADDING * canvas.mapUnitsPerPixel())
            canvas.setExtent(rect)
            if points_only and canvas.scale() < scale:
                canvas.zoomScale(scale)
    canvas.refresh()

