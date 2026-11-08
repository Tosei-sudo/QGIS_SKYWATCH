"""
QGIS vector styles as an ArcGIS web map's renderers and label classes (the
viewer's rendererOfSpec and labelingOfSpec). Single symbol, categorized (by
one field) and graduated renderers of simple markers, lines and fills are
written; anything else returns None, and the caller keeps the renderer the
web map came with (or the service's own). Reading goes the other way through
QGIS itself (QgsArcGisRestUtils.convertRenderer / convertLabeling).
"""
from __future__ import annotations

import re
from typing import Optional

from qgis.core import (
    QgsCategorizedSymbolRenderer, QgsGraduatedSymbolRenderer, QgsRuleBasedLabeling, QgsSimpleFillSymbolLayer, QgsSimpleLineSymbolLayer,
    QgsSimpleMarkerSymbolLayer, QgsSingleSymbolRenderer, QgsSymbol, QgsUnitTypes, QgsVectorLayer, QgsVectorLayerSimpleLabeling,
)
from qgis.PyQt.QtCore import Qt
from qgis.PyQt.QtGui import QColor

try:
    from qgis.core import Qgis

    _SHAPES = {Qgis.MarkerShape.Circle: "esriSMSCircle", Qgis.MarkerShape.Square: "esriSMSSquare", Qgis.MarkerShape.Diamond: "esriSMSDiamond",
               Qgis.MarkerShape.Triangle: "esriSMSTriangle", Qgis.MarkerShape.EquilateralTriangle: "esriSMSTriangle",
               Qgis.MarkerShape.Cross: "esriSMSCross", Qgis.MarkerShape.Cross2: "esriSMSX"}
except AttributeError:  # before 3.24
    _SHAPES = {}

_DASHES = {Qt.PenStyle.SolidLine: "esriSLSSolid", Qt.PenStyle.DashLine: "esriSLSDash", Qt.PenStyle.DotLine: "esriSLSDot",
           Qt.PenStyle.DashDotLine: "esriSLSDashDot", Qt.PenStyle.DashDotDotLine: "esriSLSDashDotDot", Qt.PenStyle.NoPen: "esriSLSNull"}


def points(value: float, unit) -> float:
    """A size in QGIS's unit as points (the unit of Esri symbols); map units count as millimetres."""
    if unit == QgsUnitTypes.RenderUnit.RenderPoints:
        pt = value
    elif unit == QgsUnitTypes.RenderUnit.RenderPixels:
        pt = value * 0.75
    elif unit == QgsUnitTypes.RenderUnit.RenderInches:
        pt = value * 72
    else:
        pt = value * 72 / 25.4
    return round(pt, 2)


def _color(color: QColor, opacity: float = 1.0) -> list:
    return [color.red(), color.green(), color.blue(), round(color.alpha() * max(0.0, min(1.0, opacity)))]


def _line(color: QColor, width: float, unit, style, opacity: float) -> dict:
    # A width of 0 is QGIS's hairline.
    return {"type": "esriSLS", "style": _DASHES.get(style, "esriSLSSolid"), "color": _color(color, opacity), "width": points(width, unit) or 0.75}


def esri_symbol(symbol: Optional[QgsSymbol], kind: str) -> Optional[dict]:
    """A QGIS symbol of simple layers as an Esri simple symbol for `kind` (point, line, polygon); None for others."""
    if symbol is None or symbol.symbolLayerCount() == 0:
        return None
    layer = symbol.symbolLayer(0)
    opacity = symbol.opacity()
    if kind == "line":
        if isinstance(layer, QgsSimpleLineSymbolLayer):
            return _line(layer.color(), layer.width(), layer.widthUnit(), layer.penStyle(), opacity)
        return None
    if kind == "polygon":
        if not isinstance(layer, QgsSimpleFillSymbolLayer):
            return None
        solid = layer.brushStyle() != Qt.BrushStyle.NoBrush
        return {"type": "esriSFS", "style": "esriSFSSolid" if solid else "esriSFSNull", "color": _color(layer.fillColor(), opacity if solid else 0),
                "outline": _line(layer.strokeColor(), layer.strokeWidth(), layer.strokeWidthUnit(), layer.strokeStyle(), opacity)}
    if not isinstance(layer, QgsSimpleMarkerSymbolLayer):
        return None
    out = {"type": "esriSMS", "style": _SHAPES.get(layer.shape(), "esriSMSCircle"), "color": _color(layer.fillColor(), opacity),
           "size": points(layer.size(), layer.sizeUnit()), "angle": -layer.angle() if layer.angle() else 0, "xoffset": 0, "yoffset": 0}
    if layer.strokeStyle() != Qt.PenStyle.NoPen and layer.strokeColor().alpha() > 0:
        out["outline"] = {"color": _color(layer.strokeColor(), opacity), "width": points(layer.strokeWidth(), layer.strokeWidthUnit()) or 0.75}
    return out


def _field_of(expression: str, layer: QgsVectorLayer) -> Optional[str]:
    """A renderer's or label's attribute: a field name, or an expression that is only a field (`"NAME"`)."""
    names = layer.fields().names()
    if expression in names:
        return expression
    m = re.fullmatch(r'\s*"((?:[^"]|"")+)"\s*', expression or "")
    name = m.group(1).replace('""', '"') if m else None
    return name if name in names else None


def esri_renderer(layer: QgsVectorLayer, kind: str) -> Optional[dict]:
    """The layer's renderer as a web map's (simple, uniqueValue by one field, classBreaks), or None when it is something else."""
    renderer = layer.renderer()
    if isinstance(renderer, QgsSingleSymbolRenderer):
        symbol = esri_symbol(renderer.symbol(), kind)
        return {"type": "simple", "symbol": symbol} if symbol else None
    if isinstance(renderer, QgsCategorizedSymbolRenderer):
        name = _field_of(renderer.classAttribute(), layer)
        if not name:
            return None
        infos, default = [], None
        for category in renderer.categories():
            symbol = esri_symbol(category.symbol(), kind)
            if symbol is None:
                return None
            value = category.value()
            if value is None or value == "" or (hasattr(value, "isNull") and value.isNull()):
                # QGIS's 「その他すべての値」 category.
                default = symbol if category.renderState() else None
                continue
            if not category.renderState():
                continue
            values = value if isinstance(value, list) else [value]
            for v in values:
                infos.append({"value": str(v), "label": category.label() or str(v), "symbol": symbol})
        return {"type": "uniqueValue", "field1": name, "defaultSymbol": default, "uniqueValueInfos": infos}
    if isinstance(renderer, QgsGraduatedSymbolRenderer):
        name = _field_of(renderer.classAttribute(), layer)
        ranges = sorted(renderer.ranges(), key=lambda r: r.upperValue())
        if not name or not ranges:
            return None
        infos = []
        for r in ranges:
            symbol = esri_symbol(r.symbol(), kind)
            if symbol is None:
                return None
            if r.renderState():
                infos.append({"classMinValue": r.lowerValue(), "classMaxValue": r.upperValue(), "label": r.label(), "symbol": symbol})
        return {"type": "classBreaks", "field": name, "minValue": ranges[0].lowerValue(), "defaultSymbol": None, "classBreakInfos": infos}
    return None


def esri_labeling(layer: QgsVectorLayer) -> Optional[list]:
    """The layer's labels (one attribute, simple labeling or one rule) as a web map's label class, or None."""
    labeling = layer.labeling() if layer.labelsEnabled() else None
    if isinstance(labeling, QgsRuleBasedLabeling):
        rules = labeling.rootRule().children()
        settings = rules[0].settings() if len(rules) == 1 and rules[0].active() and rules[0].settings() else None
    elif isinstance(labeling, QgsVectorLayerSimpleLabeling):
        settings = labeling.settings()
    else:
        settings = None
    if settings is None or not settings.drawLabels:
        return None
    name = _field_of(settings.fieldName, layer)
    if not name:
        return None
    text = settings.format()
    escaped = name.replace("\\", "\\\\").replace('"', '\\"')
    symbol = {"type": "esriTS", "color": _color(text.color(), text.opacity()),
              "font": {"family": text.font().family() or "sans-serif", "size": points(text.size(), text.sizeUnit()),
                       "weight": "bold" if text.font().bold() else "normal"}}
    buffer = text.buffer()
    if buffer.enabled():
        symbol["haloColor"] = _color(buffer.color(), buffer.opacity())
        symbol["haloSize"] = points(buffer.size(), buffer.sizeUnit())
    out = {"labelExpressionInfo": {"expression": f'$feature["{escaped}"]'}, "labelPlacement": "esriServerPointLabelPlacementAboveCenter", "symbol": symbol}
    if settings.scaleVisibility and settings.minimumScale:
        out["minScale"] = settings.minimumScale
    if settings.scaleVisibility and settings.maximumScale:
        out["maxScale"] = settings.maximumScale
    return [out]
