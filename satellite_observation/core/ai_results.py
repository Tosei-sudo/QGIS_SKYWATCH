"""
External AI results (`aiResultSources`): Esri feature layers (points or
polygons) where another AI system writes what it found in the catalog's
images. For the image opened from the catalog, its results are read by the
image id, shown, and the chosen ones recorded as observations.

The viewer has no such feature (its AI only writes, `detectionOutputs`);
the settings follow `detectionOutputs`: the layer's URL and its attribute
names by role.

A result recorded as an observation carries a tag in its note
(`[AI:<source>#<object id>]`), so the same result is not recorded twice.
"""
from __future__ import annotations

import math
import re
from dataclasses import dataclass, field
from typing import Any, Iterable, Optional

from .esri import quote
from .util import is_name, is_number, is_record, is_url, layer_url, ms_of, text_of

#: What an attribute of a result can be, by role (imageId is needed).
RESULT_ROLES = {
    "imageId": "画像ID",
    "id": "結果ID",
    "class": "クラス",
    "score": "確信度",
    "model": "モデル",
    "detectedAt": "検出日時",
    "name": "モノの名前",
    "kind": "種別",
    "activity": "活動",
    "length": "長さ（m）",
    "heading": "向き（°）",
    "note": "備考",
}

_LAYER = re.compile(r"/(FeatureServer|MapServer)/\d+/?$", re.IGNORECASE)
_TAG = re.compile(r"\[AI:([^\]#]*)#([^\]]*)\]")


@dataclass
class AiResultSource:
    """One `aiResultSources` entry."""
    label: str
    #: The feature layer, `…/FeatureServer/0` (points or polygons).
    url: str
    #: Attribute names by role; `imageId` is always there.
    fields: dict[str, str]
    #: A condition added to the image's (`STATUS <> 'rejected'`).
    where: Optional[str] = None
    #: Results under this score are not listed at first (0 lists all).
    min_score: float = 0.0
    #: The 種別 recorded for each class (`{"ship": "船舶"}`); a class not listed is recorded as it is.
    kinds: dict[str, str] = field(default_factory=dict)
    #: The 活動 and 確度 recorded when the result has none.
    activity: str = ""
    confidence: str = ""
    max_results: int = 2000
    token: Optional[str] = None


def ai_result_source_of(value: Any, problems: list[str], index: int) -> Optional[AiResultSource]:
    """One `aiResultSources` entry; None (with the reason in `problems`) when it cannot be used."""
    at = f"aiResultSources[{index}]"
    if not is_record(value):
        problems.append(f"{at} はオブジェクトで指定")
        return None
    url = value.get("url")
    if not is_url(url) or not _LAYER.search(url.split("?", 1)[0]):
        problems.append(f"{at} の url は …/FeatureServer/0 のようなレイヤーの URL で指定")
        return None
    fields = value.get("fields")
    if not is_record(fields):
        problems.append(f"{at} に fields（役割ごとの属性名）なし")
        return None
    mapped: dict[str, str] = {}
    for role, name in fields.items():
        if role not in RESULT_ROLES:
            problems.append(f"{at} の fields.{role} は使用不可の役割（{' / '.join(RESULT_ROLES)}）")
        elif is_name(name):
            mapped[role] = name
        elif name not in (None, ""):
            problems.append(f"{at} の fields.{role} は属性名（英数字と _）で指定")
    if "imageId" not in mapped:
        problems.append(f"{at} に fields.imageId（画像IDの入った属性名）なし")
        return None
    kinds: dict[str, str] = {}
    if is_record(value.get("kinds")):
        for k, v in value["kinds"].items():
            if isinstance(v, str) and v.strip():
                kinds[str(k)] = v.strip()
            else:
                problems.append(f"{at} の kinds.{k} は種別（「船舶 > 支援船」など）で指定")
    elif "kinds" in value:
        problems.append(f"{at} の kinds はクラスと種別の対応（オブジェクト）で指定")
    least = value.get("minScore")
    if "minScore" in value and not (is_number(least) and 0 <= least <= 1):
        problems.append(f"{at} の minScore は 0〜1 で指定")
    most = value.get("maxResults")
    label, where, token = value.get("label"), value.get("where"), value.get("token")
    activity, confidence = value.get("activity"), value.get("confidence")
    return AiResultSource(
        label=label if isinstance(label, str) and label else f"AI結果 {index + 1}",
        url=layer_url(url),
        fields=mapped,
        where=where.strip() if isinstance(where, str) and where.strip() else None,
        min_score=float(least) if is_number(least) and 0 <= least <= 1 else 0.0,
        kinds=kinds,
        activity=activity.strip() if isinstance(activity, str) else "",
        confidence=confidence.strip() if isinstance(confidence, str) else "",
        max_results=min(most, 20000) if isinstance(most, int) and not isinstance(most, bool) and most > 0 else 2000,
        token=token if isinstance(token, str) and token else None,
    )


def image_where(source: AiResultSource, image_id: str, field_type: Optional[str] = None) -> str:
    """The query of one image's results: its id (a number for a numeric field), and the source's own condition."""
    name = source.fields["imageId"]
    numeric = field_type in ("esriFieldTypeInteger", "esriFieldTypeSmallInteger", "esriFieldTypeDouble",
                             "esriFieldTypeSingle", "esriFieldTypeBigInteger", "esriFieldTypeOID")
    if numeric and re.fullmatch(r"-?\d+(\.\d+)?", image_id.strip()):
        clause = f"{name} = {image_id.strip()}"
    else:
        clause = f"{name} = {quote(image_id)}"
    return f"{clause} AND ({source.where})" if source.where else clause


@dataclass
class AiResult:
    """One result as read."""
    source: str
    #: The layer's object id (the result's key).
    oid: Any
    #: Its own id when the source names one, else the object id.
    key: str
    image_id: str
    cls: str
    score: Optional[float]
    model: str
    detected_at: Optional[int]
    name: str
    kind: str
    activity: str
    length: Optional[float]
    heading: Optional[float]
    note: str
    #: The Esri geometry (WGS 84) as the service gave it.
    geometry: Optional[dict[str, Any]]
    attributes: dict[str, Any]

    @property
    def tag(self) -> str:
        return result_tag(self.source, self.key)


def _get(attributes: dict[str, Any], name: Optional[str]) -> Any:
    if not name:
        return None
    if name in attributes:
        return attributes[name]
    lower = name.lower()
    return next((v for k, v in attributes.items() if k.lower() == lower), None)


def _number(value: Any) -> Optional[float]:
    if is_number(value):
        return float(value)
    if isinstance(value, str):
        try:
            n = float(value)
        except ValueError:
            return None
        return n if math.isfinite(n) else None
    return None


def result_of(source: AiResultSource, feature: dict[str, Any], oid_field: str) -> AiResult:
    """A feature of the source's layer as a result."""
    a = feature.get("attributes") or {}
    f = source.fields
    oid = _get(a, oid_field)
    own = text_of(_get(a, f.get("id")))
    score = _number(_get(a, f.get("score")))
    if score is not None and score > 1 and score <= 100:
        # Percentages.
        score = score / 100
    length = _number(_get(a, f.get("length")))
    heading = _number(_get(a, f.get("heading")))
    return AiResult(
        source=source.label, oid=oid, key=own or text_of(oid), image_id=text_of(_get(a, f["imageId"])),
        cls=text_of(_get(a, f.get("class"))), score=score, model=text_of(_get(a, f.get("model"))),
        detected_at=ms_of(_get(a, f.get("detectedAt"))), name=text_of(_get(a, f.get("name"))),
        kind=text_of(_get(a, f.get("kind"))), activity=text_of(_get(a, f.get("activity"))),
        length=length if length is not None and length > 0 else None,
        heading=None if heading is None else (heading % 360 + 360) % 360,
        note=text_of(_get(a, f.get("note"))), geometry=feature.get("geometry"), attributes=dict(a),
    )


def result_tag(source: str, key: str) -> str:
    """What a recorded result's note carries."""
    return f"[AI:{source.replace(']', '').replace('#', '')}#{str(key).replace(']', '')}]"


def recorded_tags(notes: Iterable[str]) -> set[str]:
    """The tags of results already recorded (in the notes of living records)."""
    out: set[str] = set()
    for note in notes:
        for m in _TAG.finditer(note or ""):
            out.add(f"[AI:{m.group(1)}#{m.group(2)}]")
    return out


def kind_for(result: AiResult, source: AiResultSource) -> str:
    """The 種別 a result is recorded as: its own, the class's in `kinds`, else the class."""
    if result.kind:
        return result.kind
    if result.cls in source.kinds:
        return source.kinds[result.cls]
    lower = result.cls.lower()
    return next((v for k, v in source.kinds.items() if k.lower() == lower), result.cls)


def score_text(score: Optional[float]) -> str:
    return "" if score is None else f"{score:.2f}"


@dataclass
class ImportEntry:
    """What one result is recorded as (where: the QGIS side finds the point inside a polygon)."""
    lon: float
    lat: float
    object: str
    kind: str
    activity: str
    confidence: str
    note: str
    length: Optional[float]
    heading: Optional[float]


def import_entry(result: AiResult, source: AiResultSource, lon: float, lat: float, length: Optional[float] = None) -> ImportEntry:
    """A result as an observation: its point, 種別, 活動, 確度, the note with what the AI said and the tag."""
    said = " ".join(x for x in (result.cls, score_text(result.score), result.model) if x)
    note = " ".join(x for x in (f"外部AI {source.label}" + (f": {said}" if said else ""), result.note, result.tag) if x)
    return ImportEntry(
        lon=lon, lat=lat, object=result.name, kind=kind_for(result, source),
        activity=result.activity or source.activity, confidence=source.confidence, note=note,
        length=result.length if result.length is not None else length, heading=result.heading,
    )
