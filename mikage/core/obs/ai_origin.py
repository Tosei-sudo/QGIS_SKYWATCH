"""
Where a record came from when an AI result made it (tsudo 2026-11-27:
「AIのどのレコードからかも識別したい」「備考の役割を無くそう」): columns
of their own, the note left to the person.

- 観測記録 (TBL_OBSERVATION): SOURCE (出どころ: AI検出 / 外部AI; empty for a
  record a person made), AI_LAYER (the AI results' layer: the label of the
  `aiResultSources` / `detectionOutputs` entry, else the detection layer's
  name), AI_RESULT_ID (the result's id there: its own id, else its
  OBJECTID; a detection layer's feature id), AI_SCORE (0–1), AI_MODEL,
  AI_DETECTED_AT, and AI_CONTINUATION (継続の判定: 自動継続, 自動継続:〈ルール〉,
  選択継続; empty for a new thing).
- 確認範囲 (TBL_OBSERVATION_AREA): a result judged wrong (誤検出) is a checked
  area with SOURCE 「AI誤検出」 and the same AI_ columns (no continuation).

The values stay as they were made when the record is changed by a person
(the record still came from that result), and a record continued by hand
from one (継続) is a record of the person's.

A result found by the plugin's own detection and recorded before it was
sent to a `detectionOutputs` table names the detection layer and its
feature id; once sent, it is pointed at the table's row (book.repoint_ai).
"""
from __future__ import annotations

from dataclasses import dataclass, fields as dataclass_fields
from typing import Any, Callable, Optional

#: SOURCE of a record made from a result of the plugin's (or the viewer's) object detection, 「似たものを探す」 too.
AI_DETECTED = "AI検出"
#: SOURCE of a record made from a result of another system (`aiResultSources`).
AI_EXTERNAL = "外部AI"
#: SOURCE of a checked area recording a result as wrong (誤検出): a hard negative for training.
AI_REJECTED = "AI誤検出"
#: The SOURCE values a record may have.
RECORD_SOURCES = (AI_DETECTED, AI_EXTERNAL)

#: The AI columns of a record by role (DEFAULT_OBSERVATION_FIELDS), and the AiOrigin attribute each holds.
RECORD_ROLES: dict[str, tuple[str, str]] = {
    "aiSource": ("SOURCE", "source"),
    "aiLayer": ("AI_LAYER", "layer"),
    "aiResultId": ("AI_RESULT_ID", "result_id"),
    "aiScore": ("AI_SCORE", "score"),
    "aiModel": ("AI_MODEL", "model"),
    "aiDetectedAt": ("AI_DETECTED_AT", "detected_at"),
    "aiContinuation": ("AI_CONTINUATION", "continuation"),
}
#: Those of a checked area (DEFAULT_AREA_FIELDS): no continuation.
AREA_ROLES: dict[str, tuple[str, str]] = {k: v for k, v in RECORD_ROLES.items() if k != "aiContinuation"}
#: The attributes that are numbers, and the time.
_NUMBERS = {"score"}
_TIMES = {"detected_at"}


@dataclass(frozen=True)
class AiOrigin:
    """The AI result a record (or a 誤検出 checked area) was made from."""
    #: SOURCE: AI_DETECTED, AI_EXTERNAL (a record) or AI_REJECTED (a checked area).
    source: str
    #: AI_LAYER: where the result is (the settings' label of its table, else the detection layer's name).
    layer: str = ""
    #: AI_RESULT_ID: the result's id there.
    result_id: str = ""
    #: AI_SCORE: how sure the AI was (0–1), None when it did not say.
    score: Optional[float] = None
    #: AI_MODEL.
    model: str = ""
    #: AI_DETECTED_AT (ms since the epoch).
    detected_at: Optional[int] = None
    #: AI_CONTINUATION: how it was found to be a thing of the images before (継続), '' for a new thing.
    continuation: str = ""

    @property
    def rejected(self) -> bool:
        """Whether it records a result as wrong (a checked area of 誤検出)."""
        return self.source == AI_REJECTED


def origin_of(value: Any) -> Optional[AiOrigin]:
    """An AiOrigin as kept in JSON (dataclasses.asdict: the drafts, the offline stores), else as it is."""
    if value is None or isinstance(value, AiOrigin):
        return value
    if isinstance(value, dict) and value.get("source"):
        names = {f.name for f in dataclass_fields(AiOrigin)}
        return AiOrigin(**{k: v for k, v in value.items() if k in names})
    return None


def origin_values(ai: Optional[AiOrigin], fields: dict[str, str], roles: dict[str, tuple[str, str]]) -> dict[str, Any]:
    """The columns of an origin as written (`fields`: the store's names by role); every one empty for none."""
    out: dict[str, Any] = {}
    for role, (default, attr) in roles.items():
        name = fields.get(role) or default
        value = getattr(ai, attr) if ai is not None else None
        out[name] = None if value in ("", None) else value
    return out


def read_origin(get: Callable[[str], Any], fields: dict[str, str], roles: dict[str, tuple[str, str]],
                text: Callable[[Any], str], number: Callable[[Any], Optional[float]], time: Callable[[Any], Optional[int]]) -> Optional[AiOrigin]:
    """The origin of a row (`get`: an attribute by name), None when SOURCE is empty (a record a person made)."""
    values: dict[str, Any] = {}
    for role, (default, attr) in roles.items():
        raw = get(fields.get(role) or default)
        values[attr] = number(raw) if attr in _NUMBERS else time(raw) if attr in _TIMES else text(raw).strip()
    if not values.get("source"):
        return None
    return AiOrigin(**values)


def score_text(score: Optional[float]) -> str:
    return "" if score is None else f"{score:.2f}"


def origin_label(ai: Optional[AiOrigin]) -> str:
    """What the list's 出どころ column says: 「AI検出 0.87」; '' for a record a person made."""
    if ai is None:
        return ""
    return " ".join(x for x in (ai.source, score_text(ai.score)) if x)


def origin_text(ai: Optional[AiOrigin]) -> str:
    """
    What the form and a row's tip say of it: 「AI検出（yolov8・確信度 0.87・検出結果 #3・自動継続）」;
    '' for a record a person made.
    """
    if ai is None:
        return ""
    where = f"{ai.layer} #{ai.result_id}" if ai.layer and ai.result_id else ai.layer or (f"#{ai.result_id}" if ai.result_id else "")
    parts = [ai.model, f"確信度 {score_text(ai.score)}" if ai.score is not None else "", where, ai.continuation]
    said = "・".join(p for p in parts if p)
    return f"{ai.source}（{said}）" if said else ai.source


def same_time(a: Optional[int], b: Optional[int]) -> bool:
    """Whether two 検出日時 are of one run: to the second (a database may keep no less); both unknown too."""
    if a is None or b is None:
        return a is None and b is None
    return abs(a - b) < 1000


def of_layer_run(ai: Optional[AiOrigin], result_id: str, detected_at: Optional[int], tables: Any = (), layer: str = "") -> bool:
    """
    Whether an origin names a feature of a detection layer (not a table's
    row, `tables`: their labels): its id, of the same run (検出日時; with
    none known, of a layer of that name).
    """
    if ai is None or ai.layer in tables or ai.result_id != str(result_id):
        return False
    if detected_at is None and ai.detected_at is None:
        return bool(layer) and ai.layer == layer
    return same_time(ai.detected_at, detected_at)
