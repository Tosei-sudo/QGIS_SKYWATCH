"""
External AI results (`aiResultSources`): Esri feature layers (points or
polygons) where another AI system writes what it found in the catalog's
images. For the image opened from the catalog, its results are read by the
image id, shown, and the chosen ones recorded as observations.

The viewer has no such feature (its AI only writes, `detectionOutputs`);
the settings follow `detectionOutputs`: the layer's URL and its attribute
names by role.

The `detectionOutputs` tables (「検出結果を DB へ転記」, the plugin's and the
viewer's detections) are read back the same way (`output_source`,
tsudo 2026-11-09: one person detects, others review), their attribute names
taken from the entry's roles; `"aiResults": false` leaves one out. A result
the person's own detection layer sent there is listed once (as the layer's,
which they may still be correcting).

A result recorded as an observation carries a tag in its note
(`[AI:<source>#<object id>]`), so the same result is not recorded twice.
A detection layer's result already sent to a table carries the table's tag
too; one recorded from the layer before it was sent is known in the table by
its run (`検出@<detected at>`) and its place (`run_of`).
"""
from __future__ import annotations

import math
import re
from dataclasses import dataclass, field
from typing import Any, Iterable, Optional, Sequence

from .esri import quote
from .obs.continuation import CANDIDATE_METRES, CONTINUE_METRES, KIND_LEVELS, Rule, Rules, old_rules
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
    "width": "幅（m）",
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
    #: Nearer than this (m) to a thing of the image before or after (and sure) is recorded as it (継続); 0 never without asking.
    continue_radius: float = CONTINUE_METRES
    #: Nearer than this (m) is offered as what it may continue; 0 offers nothing.
    candidate_radius: float = CANDIDATE_METRES
    #: The plugin's own detection (物体検出（AI）) read from a layer of the project, not a feature service.
    local: bool = False
    #: A `detectionOutputs` table read back (detections sent there by the plugin or the viewer).
    detections: bool = False
    #: Its own matching (継続), the keys it writes only: `continuation` (rules / candidate_radius / unique / on), and
    #: the older `continueRadius` (rules) and `candidateRadius` (0: on False); the rest from `aiContinuation`.
    continuation: dict[str, Any] = field(default_factory=dict)


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
            problems.append(f"{at} の fields.{role} は使えない役割（{' / '.join(RESULT_ROLES)}）")
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
    radii = {}
    own: dict[str, Any] = {}
    for key, default in (("continueRadius", CONTINUE_METRES), ("candidateRadius", CANDIDATE_METRES)):
        v = value.get(key)
        if key in value and not (is_number(v) and 0 <= v <= 100_000):
            problems.append(f"{at} の {key} は 0〜100000（m）で指定")
        radii[key] = float(v) if is_number(v) and 0 <= v <= 100_000 else default
        if key in value and is_number(v) and 0 <= v <= 100_000:
            if key == "continueRadius":
                own["rules"] = old_rules(float(v)).rules
            elif v > 0:
                own["candidate_radius"] = float(v)
            else:
                # 「0 で照合しない」 as it always was.
                own["on"] = False
    if "continuation" in value:
        # Its own rules win over the older keys.
        own.update(continuation_parts(value["continuation"], problems, f"{at} の continuation"))
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
        continue_radius=radii["continueRadius"],
        candidate_radius=radii["candidateRadius"],
        continuation=own,
    )


#: A rule's keys in the settings file (`aiContinuation.rules[]`) and what each is.
RULE_KEYS = {
    "label": "名前",
    "near": "記録の位置からの距離（m）",
    "centerInRecord": "AI の重心が記録の外形の中",
    "recordInAi": "記録の位置が AI のポリゴンの中",
    "overlap": "外形の重なり率（0〜1）",
    "kind": "種別の一致（same / related / sibling / any）",
    "lengthGap": "長さの差の上限（0〜1）",
    "headingGap": "向きの差の上限（°）",
    "minScore": "AI の確信度の下限（0〜1）",
}


def _rule_of(value: Any, problems: list[str], at: str) -> Optional[Rule]:
    if not is_record(value):
        problems.append(f"{at} は条件のオブジェクトで指定")
        return None
    for key in value:
        if key not in RULE_KEYS:
            problems.append(f"{at} の {key} は使えない条件（{' / '.join(RULE_KEYS)}）")
    rule = Rule()
    label = value.get("label")
    if isinstance(label, str):
        rule.label = label.strip().replace("]", "")
    elif "label" in value:
        problems.append(f"{at} の label は文字列で指定")
    for key, attr, most, what in (("near", "near", 100_000, "0〜100000（m）"), ("overlap", "overlap", 1, "0〜1 "),
                                  ("lengthGap", "length_gap", 1, "0〜1 "), ("headingGap", "heading_gap", 180, "0〜180（°）"),
                                  ("minScore", "min_score", 1, "0〜1 ")):
        if key not in value or value[key] is None:
            continue
        v = value[key]
        if is_number(v) and 0 <= v <= most:
            setattr(rule, attr, float(v))
        else:
            problems.append(f"{at} の {key} は {what}で指定")
    for key, attr in (("centerInRecord", "center_in_record"), ("recordInAi", "record_in_ai")):
        if key not in value:
            continue
        if isinstance(value[key], bool):
            setattr(rule, attr, value[key])
        else:
            problems.append(f"{at} の {key} は true か false で指定")
    kind = value.get("kind")
    if kind in KIND_LEVELS:
        rule.kind = kind
    elif kind not in (None, ""):
        problems.append(f"{at} の kind は {' / '.join(KIND_LEVELS)} で指定")
    if not rule.placed:
        problems.append(f"{at} に位置の条件（near・centerInRecord・recordInAi・overlap のどれか）なし")
        return None
    return rule


def continuation_parts(value: Any, problems: list[str], at: str = "aiContinuation") -> dict[str, Any]:
    """
    The keys of a matching (継続) setting it writes (`aiContinuation`, or a
    source's `continuation`): `false` matches nothing; an object of `rules`,
    `candidateRadius` and `unique`.
    """
    if value is False:
        return {"on": False}
    if value is True:
        return {"on": True}
    if not is_record(value):
        problems.append(f"{at} は false かオブジェクト（rules・candidateRadius・unique）で指定")
        return {}
    out: dict[str, Any] = {"on": True}
    for key in value:
        if key not in ("rules", "candidateRadius", "unique"):
            problems.append(f"{at} の {key} は使えないキー（rules / candidateRadius / unique）")
    if "rules" in value:
        rules = value["rules"]
        if isinstance(rules, list):
            out["rules"] = [r for r in (_rule_of(x, problems, f"{at} の rules[{i}]") for i, x in enumerate(rules)) if r is not None]
        else:
            problems.append(f"{at} の rules は条件のオブジェクトの配列で指定")
    if "candidateRadius" in value:
        v = value["candidateRadius"]
        if is_number(v) and 0 <= v <= 100_000:
            out["candidate_radius"] = float(v)
        else:
            problems.append(f"{at} の candidateRadius は 0〜100000（m）で指定")
    if "unique" in value:
        if isinstance(value["unique"], bool):
            out["unique"] = value["unique"]
        else:
            problems.append(f"{at} の unique は true か false で指定")
    return out


def ai_continuation_of(value: Any, problems: list[str]) -> dict[str, Any]:
    """`aiContinuation`: the matching (継続) of every source's results, the keys it writes."""
    return continuation_parts(value, problems)


def rules_for(source: AiResultSource, config: Any = None) -> Rules:
    """
    How a source's results are matched (継続): the keys the source writes,
    else `aiContinuation`'s, else the judgement before rules could be set.
    """
    base = old_rules()
    parts = dict(getattr(config, "ai_continuation", None) or {})
    parts.update(source.continuation)
    return Rules(
        rules=list(parts.get("rules", base.rules)),
        candidate_radius=parts.get("candidate_radius", base.candidate_radius),
        unique=parts.get("unique", base.unique),
        on=parts.get("on", True),
    )


#: The roles of a `detectionOutputs` table read as results (its role → the result's).
OUTPUT_ROLES = {"imageId": "imageId", "class": "class", "score": "score", "model": "model", "detectedAt": "detectedAt",
                "length": "length", "width": "width", "note": "note"}


def output_source(output: Any) -> Optional[AiResultSource]:
    """
    A `detectionOutputs` entry (core/ai/transfer.DetectionOutput) as a source
    of results: its layer, its attribute names by role; None when it is left
    out (`aiResults: false`) or does not write the image id.
    """
    if not getattr(output, "ai_results", True) or "imageId" not in output.fields:
        return None
    fields = {mine: output.fields[role] for role, mine in OUTPUT_ROLES.items() if role in output.fields}
    return AiResultSource(label=output.label, url=output.url, fields=fields, token=output.token, detections=True)


def result_sources(config: Any) -> list[AiResultSource]:
    """
    Where the dock 「AI結果」 reads: `aiResultSources`, then the
    `detectionOutputs` tables not already among them (a label taken is given
    「（転記先）」).
    """
    out = list(getattr(config, "ai_result_sources", []) or [])
    urls = {s.url.lower() for s in out}
    labels = {s.label for s in out}
    for output in getattr(config, "detection_outputs", []) or []:
        source = output_source(output)
        if source is None or source.url.lower() in urls:
            continue
        if source.label in labels:
            source.label = f"{source.label}（転記先）"
        urls.add(source.url.lower())
        labels.add(source.label)
        out.append(source)
    return out


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
    width: Optional[float] = None
    #: 確度 the result says (「似たものを探す」: the 見本's), '' for the source's.
    confidence: str = ""

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
    width = _number(_get(a, f.get("width")))
    return AiResult(
        source=source.label, oid=oid, key=own or text_of(oid), image_id=text_of(_get(a, f["imageId"])),
        cls=text_of(_get(a, f.get("class"))), score=score, model=text_of(_get(a, f.get("model"))),
        detected_at=ms_of(_get(a, f.get("detectedAt"))), name=text_of(_get(a, f.get("name"))),
        kind=text_of(_get(a, f.get("kind"))), activity=text_of(_get(a, f.get("activity"))),
        length=length if length is not None and length > 0 else None,
        heading=None if heading is None else (heading % 360 + 360) % 360,
        note=text_of(_get(a, f.get("note"))), geometry=feature.get("geometry"), attributes=dict(a),
        width=width if width is not None and width > 0 else None,
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


_RUN = re.compile(r"^検出@(-?\d+)$")


def detection_runs(note: str) -> list[tuple[str, Optional[int]]]:
    """
    The tags in a note of results of the plugin's own detection layers
    (`[AI:検出@<detected at, ms>#<feature id>]`), with the run's time (None
    when the run had none).
    """
    out: list[tuple[str, Optional[int]]] = []
    for m in _TAG.finditer(note or ""):
        if not m.group(1).startswith("検出@"):
            continue
        run = _RUN.match(m.group(1))
        out.append((f"[AI:{m.group(1)}#{m.group(2)}]", int(run.group(1)) if run else None))
    return out


def same_run(detected_at: Optional[int], run: Optional[int]) -> bool:
    """Whether a table's result may be of a run (its 検出日時; to the second: a database may keep no less)."""
    return detected_at is None or run is None or abs(detected_at - run) < 1000


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
    #: The record of the thing (of the image before or after) this one continues (継続); None for a new thing.
    carry: Optional[Any] = None
    width: Optional[float] = None
    #: Where it is in the image's file (file, pixel x, pixel y), when found before (None: found from the image as it lies now).
    pixel: Optional[tuple] = None


def source_head(source: AiResultSource) -> str:
    """How a recorded result's note names where it came from."""
    return "AI検出" if source.local else f"AI検出（{source.label}）" if source.detections else f"外部AI {source.label}"


def import_entry(result: AiResult, source: AiResultSource, lon: float, lat: float, length: Optional[float] = None,
                 width: Optional[float] = None, also: Sequence[str] = ()) -> ImportEntry:
    """
    A result as an observation: its point, 種別, 活動, 確度, the note with what
    the AI said and the tag (and `also`: the tags it has elsewhere, a table it
    was sent to).
    """
    said = " ".join(x for x in (result.cls, score_text(result.score), result.model) if x)
    head = source_head(source)
    note = " ".join(x for x in (head + (f": {said}" if said else ""), result.note, result.tag, *also) if x)
    return ImportEntry(
        lon=lon, lat=lat, object=result.name, kind=kind_for(result, source),
        activity=result.activity or source.activity, confidence=result.confidence or source.confidence, note=note,
        length=result.length if result.length is not None else length, heading=result.heading,
        width=result.width if result.width is not None else width,
    )


def detected_kind(cls: str, kinds: dict[str, str], taxonomy: Sequence[Any] = ()) -> str:
    """
    The 種別 a class found by the plugin's own detection is recorded as: the
    model's `kinds` entry for it (any case), else the class of the
    classification it names (its path, own name, 別名 or 英名; a model
    trained here has the classification's kinds as its classes), else the
    class as it is.
    """
    from .obs.taxonomy import class_by_name, find, kind_of, path_of

    name = (cls or "").strip()
    if not name:
        return ""
    if name in kinds:
        return kinds[name]
    lower = name.lower()
    hit = next((v for k, v in kinds.items() if k.lower() == lower), None)
    if hit:
        return hit
    items = list(taxonomy)
    path = path_of(name)
    if items and path and find(items, path) is None and len(path) == 1:
        found = class_by_name(items, path[0])
        if found:
            return kind_of(list(found))
    return kind_of(path) if path else name
