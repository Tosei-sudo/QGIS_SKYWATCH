"""
Detections into a database (as the viewer's ai/transfer.ts): the AI layers'
features (as the model made them, as people corrected them, and ones people
added) written to an Esri feature layer, its attribute names mapped by role
in the settings file's `detectionOutputs`.

What the model made and what was sent where are kept by the caller (on the
QGIS layer); here, how a feature's status, attributes and signature come out
of them, so a transfer adds what is new, updates what was changed since it
was sent, and leaves the rest.
"""
from __future__ import annotations

import json
import math
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Optional

#: What a feature's attribute can be written as, by role.
TRANSFER_ROLES = {
    "imageId": "画像ID",
    "fileName": "ファイル名",
    "imageTime": "撮像日時",
    "detectedAt": "検出日時",
    "sentAt": "転記日時",
    "class": "クラス",
    "classId": "クラス番号",
    "score": "確信度",
    "model": "モデル",
    "status": "状態（AI・修正・手動）",
    "lon": "経度",
    "lat": "緯度",
    "area": "面積（m²）",
    "length": "長辺（m）",
    "width": "短辺（m）",
    "angle": "向き（°）",
    "note": "備考",
}

#: Whether a feature is the model's own, corrected by a person, or added by a person.
STATUSES = ("ai", "corrected", "manual")
STATUS_LABELS = {"ai": "AI", "corrected": "修正", "manual": "手動"}

#: Features per applyEdits request.
BATCH = 250


@dataclass
class DetectionOutput:
    """One `detectionOutputs` entry."""
    label: str
    #: The feature layer, `…/FeatureServer/0`.
    url: str
    #: Attribute names by role; roles not given are not written.
    fields: dict[str, str]
    #: The values written for each status (default AI, 修正, 手動).
    status_values: dict[str, Any] = field(default_factory=lambda: dict(STATUS_LABELS))
    #: Attributes written as they are on every feature (`{"SOURCE": "viewer"}`).
    constants: dict[str, Any] = field(default_factory=dict)
    token: Optional[str] = None


def _is_name(value: Any) -> bool:
    return isinstance(value, str) and re.fullmatch(r"[\w.]+", value, re.ASCII) is not None


def detection_output_of(value: Any, problems: list[str], index: int) -> Optional[DetectionOutput]:
    """One `detectionOutputs` entry; None (with the reason in `problems`) when it cannot be used."""
    at = f"detectionOutputs[{index}]"
    if not isinstance(value, dict):
        problems.append(f"{at} はオブジェクトで指定")
        return None
    url = value.get("url")
    if (not isinstance(url, str) or not re.match(r"^(https?:)?//|^\.{0,2}/", url)
            or not re.search(r"/FeatureServer/\d+/?$", url.split("?", 1)[0], re.IGNORECASE)):
        problems.append(f"{at} の url は …/FeatureServer/0 のようなレイヤーの URL で指定")
        return None
    fields = value.get("fields")
    if not isinstance(fields, dict):
        problems.append(f"{at} に fields（役割ごとの属性名）なし")
        return None
    mapped: dict[str, str] = {}
    for role, name in fields.items():
        if role not in TRANSFER_ROLES:
            problems.append(f"{at} の fields.{role} は使用不可の役割")
        elif _is_name(name):
            mapped[role] = name
        else:
            problems.append(f"{at} の fields.{role} は属性名（英数字と _）で指定")
    if not mapped:
        problems.append(f"{at} の fields に書き込む属性なし")
        return None
    status = dict(STATUS_LABELS)
    if isinstance(value.get("statusValues"), dict):
        for key in STATUSES:
            v = value["statusValues"].get(key)
            if isinstance(v, (str, int, float)) and not isinstance(v, bool):
                status[key] = v
    fixed: dict[str, Any] = {}
    if isinstance(value.get("constants"), dict):
        for name, v in value["constants"].items():
            if _is_name(name) and (v is None or isinstance(v, (str, int, float, bool))):
                fixed[name] = v
            else:
                problems.append(f"{at} の constants.{name} は属性名と値（文字・数・真偽）で指定")
    label = value.get("label")
    token = value.get("token")
    return DetectionOutput(
        label=label if isinstance(label, str) and label else f"転記先 {index + 1}",
        url=url.split("?", 1)[0].rstrip("/"),
        fields=mapped,
        status_values=status,
        constants=fixed,
        token=token if isinstance(token, str) and token else None,
    )


# The target layer.

@dataclass
class TargetField:
    """A field of the target layer, as its REST description gives it."""
    name: str
    type: str
    length: Optional[int] = None
    editable: Optional[bool] = None


@dataclass
class Target:
    """The target layer as its REST description gives it."""
    name: str
    geometry_type: str
    object_id_field: str
    can_create: bool
    can_update: bool
    #: Fields by lower-case name.
    fields: dict[str, TargetField]


def target_of(url: str, raw: dict[str, Any]) -> Target:
    """The target layer of its REST description (a layer that does not list its capabilities is taken as editable)."""
    listed = [f for f in raw.get("fields") or [] if isinstance(f, dict) and isinstance(f.get("name"), str)]
    fields = {f["name"].lower(): TargetField(f["name"], str(f.get("type") or ""), f.get("length") if isinstance(f.get("length"), int) else None,
                                             f.get("editable") if isinstance(f.get("editable"), bool) else None) for f in listed}
    oid = raw.get("objectIdField")
    if not isinstance(oid, str) or not oid:
        oid = next((f.name for f in fields.values() if f.type == "esriFieldTypeOID"), "OBJECTID")
    if isinstance(raw.get("capabilities"), str):
        caps = {c.strip().lower() for c in raw["capabilities"].split(",")}
        editing = "editing" in caps and "create" not in caps and "update" not in caps
        can_create, can_update = editing or "create" in caps, editing or "update" in caps
    else:
        can_create = can_update = True
    name = raw.get("name") if isinstance(raw.get("name"), str) and raw.get("name") else url.rsplit("/", 3)[-3]
    return Target(name, str(raw.get("geometryType") or ""), oid, can_create, can_update, fields)


# A feature's status and attributes.

def status_of(origin: Optional[dict[str, Any]], class_value: Any, geometry: str) -> str:
    """Whether a feature is the model's own, corrected (outline or class changed), or added by a person (no origin)."""
    if not origin:
        return "manual"
    return "ai" if origin.get("class") == class_value and origin.get("geometry") == geometry else "corrected"


def _ms(value: Any) -> Optional[float]:
    if isinstance(value, datetime):
        return (value if value.tzinfo else value.replace(tzinfo=timezone.utc)).timestamp() * 1000
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return float(value)
    try:
        t = datetime.fromisoformat(str(value).strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    return (t if t.tzinfo else t.replace(tzinfo=timezone.utc)).timestamp() * 1000


def _number(value: Any) -> Optional[float]:
    if isinstance(value, bool):
        return float(value)
    try:
        n = float(value)
    except (TypeError, ValueError):
        return None
    return n if n == n and abs(n) != float("inf") else None


def fit_value(value: Any, f: TargetField) -> Any:
    """A value fitted to an Esri field type: dates as milliseconds, numbers as numbers, text cut to the field's length."""
    if value is None or value == "":
        return None
    if f.type == "esriFieldTypeDate":
        ms = _ms(value)
        return int(ms) if ms is not None else None
    if f.type in ("esriFieldTypeSmallInteger", "esriFieldTypeInteger", "esriFieldTypeBigInteger"):
        n = _number(value)
        # Halves up, as JavaScript's Math.round.
        return math.floor(n + 0.5) if n is not None else None
    if f.type in ("esriFieldTypeSingle", "esriFieldTypeDouble"):
        return _number(value)
    if f.type == "esriFieldTypeString":
        text = value if isinstance(value, str) else (_iso(value) if isinstance(value, datetime) else str(value))
        return text[:f.length] if f.length else text
    return value


def _iso(t: datetime) -> str:
    return (t if t.tzinfo else t.replace(tzinfo=timezone.utc)).astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def transfer_attributes(values: dict[str, Any], output: DetectionOutput, fields: dict[str, TargetField]) -> dict[str, Any]:
    """
    The attributes written for a feature: each role's value (`values`, the
    status as one of STATUSES) under its mapped name, the status as
    configured, the constants added, all fitted to the target's fields.
    """
    attributes: dict[str, Any] = {}
    for role, name in output.fields.items():
        f = fields.get(name.lower())
        if f is None:
            continue
        value = output.status_values.get(values.get("status")) if role == "status" else values.get(role)
        attributes[f.name] = fit_value(value, f)
    for name, value in output.constants.items():
        f = fields.get(name.lower())
        if f is not None:
            attributes[f.name] = fit_value(value, f)
    return attributes


def missing_fields(output: DetectionOutput, fields: dict[str, TargetField]) -> list[str]:
    """The mapped attribute names the target layer does not have (or cannot write)."""
    names = [*output.fields.values(), *output.constants.keys()]
    return [n for n in names if fields.get(n.lower()) is None or fields[n.lower()].editable is False]


def signature_of(attributes: dict[str, Any], geometry: Any, sent_at_field: Optional[str]) -> str:
    """What was sent of a feature, compared without the time of sending."""
    rest = {k: v for k, v in attributes.items() if k != sent_at_field}
    return json.dumps([rest, geometry], ensure_ascii=False, sort_keys=True)


def action_of(sent: Optional[dict[str, Any]], signature: str) -> tuple[str, Optional[int]]:
    """What a transfer does with a feature, from what was sent of it to this target: ("add"), ("update", id) or ("same", id)."""
    if not sent:
        return "add", None
    return ("same" if sent.get("signature") == signature else "update"), int(sent["id"])


def bearing_text(degrees: float) -> float:
    """A bearing as 0–180° clockwise from north, to 0.1°."""
    return round((degrees % 360) % 180, 1)


@dataclass
class Plan:
    """What a transfer would do."""
    adds: list = field(default_factory=list)
    updates: list = field(default_factory=list)
    same: int = 0
    skipped: int = 0
    statuses: dict[str, int] = field(default_factory=lambda: {s: 0 for s in STATUSES})

    def describe(self, target: Target, missing: int) -> str:
        """The plan as the dialog says it."""
        statuses = "・".join(f"{STATUS_LABELS[s]} {n}" for s, n in self.statuses.items() if n)
        kind = "ポイント：中心点を転記" if target.geometry_type == "esriGeometryPoint" else "ポリゴン"
        return "".join([
            f"{target.name}（{kind}）へ",
            f"新規 {len(self.adds)} 件・更新 {len(self.updates)} 件・転記済みで変更なし {self.same} 件",
            f"（{statuses}）" if statuses else "",
            f"。形が合わない {self.skipped} 件は送信対象外" if self.skipped else "",
            f"。転記先にない属性 {missing} 個は書き込み対象外" if missing else "",
        ])
