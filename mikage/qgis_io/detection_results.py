"""
The plugin's own detection (「物体検出（AI）」) as AI results: the layers it
makes (polygons with `class`, `score`, `image_id`, `model`, `detected_at`)
read for one image, so the dock 「AI結果」 lists them beside the external
ones and records the chosen ones as observations the same way.

A result's key is its feature id and its source the run (`検出@<detected_at
in ms>`, the same for every feature of a run and kept when the layer is
saved to a file), so a feature already recorded is not recorded twice.
What the person changed in the layer (a class corrected, a false one
deleted, an outline moved) is what is recorded.
"""
from __future__ import annotations

from typing import Callable, Optional, Sequence

from qgis.core import QgsCoordinateReferenceSystem, QgsCoordinateTransform, QgsFeatureRequest, QgsGeometry, QgsProject, QgsVectorLayer, QgsWkbTypes
from qgis.PyQt.QtCore import QDateTime

from ..core.ai_results import AiResult, AiResultSource, detected_kind
from ..core.util import text_of

#: The attributes a layer of the plugin's detection has.
DETECTION_FIELDS = ("class", "score", "image_id", "model", "detected_at")

WGS84 = QgsCoordinateReferenceSystem("EPSG:4326")


def is_detection_layer(layer) -> bool:
    """Whether a layer is one the plugin's detection made (points or polygons with its attributes)."""
    try:
        if not isinstance(layer, QgsVectorLayer) or not layer.isValid():
            return False
        if layer.geometryType() not in (QgsWkbTypes.GeometryType.PolygonGeometry, QgsWkbTypes.GeometryType.PointGeometry):
            return False
        names = set(layer.fields().names())
    except RuntimeError:
        return False
    return all(n in names for n in DETECTION_FIELDS)


def detection_layers(project: Optional[QgsProject] = None) -> list[QgsVectorLayer]:
    project = project or QgsProject.instance()
    return [layer for layer in project.mapLayers().values() if is_detection_layer(layer)]


def image_ids_of(layer: QgsVectorLayer) -> list[str]:
    """The images a detection layer has results of, in order of first feature."""
    index = layer.fields().indexOf("image_id")
    request = QgsFeatureRequest().setFlags(QgsFeatureRequest.Flag.NoGeometry).setSubsetOfAttributes([index])
    out: list[str] = []
    for f in layer.getFeatures(request):
        v = text_of(_plain(f[index]))
        if v and v not in out:
            out.append(v)
    return out


def _plain(value):
    if value is None or (hasattr(value, "isNull") and not isinstance(value, QDateTime) and value.isNull()):
        return None
    return value


def _ms(value) -> Optional[int]:
    value = _plain(value)
    if isinstance(value, QDateTime):
        return value.toMSecsSinceEpoch() if value.isValid() else None
    return None


def _number(value) -> Optional[float]:
    value = _plain(value)
    return float(value) if isinstance(value, (int, float)) and not isinstance(value, bool) and value == value else None


def local_source(layer: QgsVectorLayer, kinds: Optional[dict] = None) -> AiResultSource:
    """The source the dock lists a detection layer's results under (its name; no continuation radii of its own)."""
    return AiResultSource(label=layer.name(), url="", fields={"imageId": "image_id"}, kinds=dict(kinds or {}), local=True)


def detection_results(layer: QgsVectorLayer, image_id: str, kinds_of: Callable[[str], dict],
                      taxonomy: Sequence = ()) -> list[tuple[AiResult, int]]:
    """
    The features of a detection layer on one image, as results with their
    feature ids: the class's 種別 (the model's `kinds`, else the
    classification), the geometry in WGS 84.
    """
    fields = layer.fields()
    to_wgs84 = QgsCoordinateTransform(layer.crs(), WGS84, QgsProject.instance()) if layer.crs() != WGS84 else None
    has_note = fields.indexOf("note") >= 0
    # 「似たものを探す」 (qgis_io/similar_io.py) says the 見本's heading, size, 活動 and 確度 too.
    extra = {name for name in ("heading", "length_m", "width_m", "activity", "confidence") if fields.indexOf(name) >= 0}
    out: list[tuple[AiResult, int]] = []
    for f in layer.getFeatures():
        if text_of(_plain(f["image_id"])) != image_id:
            continue
        g = QgsGeometry(f.geometry()) if f.hasGeometry() else None
        if g is not None and to_wgs84 is not None:
            try:
                g.transform(to_wgs84)
            except Exception:  # noqa: BLE001 (outside the CRS's area: listed without a place)
                g = None
        cls = text_of(_plain(f["class"]))
        model = text_of(_plain(f["model"]))
        score = _plain(f["score"])
        detected = _ms(f["detected_at"])
        run = f"検出@{detected}" if detected is not None else f"検出@{layer.id()}"
        out.append((AiResult(
            source=run, oid=f.id(), key=str(f.id()), image_id=image_id, cls=cls,
            score=float(score) if isinstance(score, (int, float)) else None, model=model, detected_at=detected,
            name="", kind=detected_kind(cls, kinds_of(model), taxonomy),
            activity=text_of(_plain(f["activity"])) if "activity" in extra else "",
            length=_number(f["length_m"]) if "length_m" in extra else None,
            heading=_number(f["heading"]) if "heading" in extra else None,
            note=text_of(_plain(f["note"])) if has_note else "", geometry=g if g is not None and not g.isEmpty() else None,
            attributes={}, width=_number(f["width_m"]) if "width_m" in extra else None,
            confidence=text_of(_plain(f["confidence"])) if "confidence" in extra else "",
        ), f.id()))
    return out
