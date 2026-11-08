"""
保存できなかった記録 (drafts): a record whose save did not go through (the
network, the sign-in, someone else's change) is kept on this computer
(QgsSettings, per store) with what the form said, so that it is not lost
when another form is open, the dock is closed or QGIS is quit (tsudo,
2026-11-08). It is sent again from the list 「未保存の記録」, or opened in
the form again, or thrown away.

The record keeps its own id from the first try: a save whose answer was lost
(the server may hold it) becomes an update when sent again (#73), never a
second record. A change to a record keeps the 更新日時 it was made from:
sent again after someone else changed that record, it is not saved over
their change (#74).
"""
from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field, fields
from typing import Any, Optional

from .model import Observation

#: At most this many are kept per store (the oldest dropped first).
LIMIT = 200


@dataclass
class Draft:
    """A record not saved, and what is needed to send it again."""
    record: Observation
    #: What the form said (book.FormValues as a dict), to open it again.
    values: dict[str, str] = field(default_factory=dict)
    present: bool = True
    #: The record changed (its id) and the 更新日時 it had then; '' / None for a new record.
    editing_id: str = ""
    base_updated: Optional[int] = None
    #: The record carried over (継続), '' for none.
    source_id: str = ""
    #: When it was kept (ms), and why the last try failed.
    kept_at: int = 0
    reason: str = ""

    @property
    def id(self) -> str:
        return self.record.id

    @property
    def editing(self) -> bool:
        return bool(self.editing_id)

    def to_json(self) -> dict[str, Any]:
        out = asdict(self)
        out["record"] = asdict(self.record)
        return out


_RECORD_FIELDS = {f.name for f in fields(Observation)}


def draft_of(value: Any) -> Optional[Draft]:
    """A kept draft; None when it cannot be read (left out)."""
    if not isinstance(value, dict) or not isinstance(value.get("record"), dict):
        return None
    try:
        record = Observation(**{k: v for k, v in value["record"].items() if k in _RECORD_FIELDS})
        values = value.get("values") if isinstance(value.get("values"), dict) else {}
        return Draft(record, {str(k): str(v) for k, v in values.items()}, bool(value.get("present", True)),
                     str(value.get("editing_id") or ""), value.get("base_updated"), str(value.get("source_id") or ""),
                     int(value.get("kept_at") or 0), str(value.get("reason") or ""))
    except (TypeError, ValueError):
        return None


def parse_drafts(text: Optional[str]) -> list[Draft]:
    try:
        value = json.loads(text) if text else []
    except ValueError:
        return []
    return [d for d in (draft_of(v) for v in value) if d is not None] if isinstance(value, list) else []


def drafts_text(drafts: list[Draft]) -> str:
    return json.dumps([d.to_json() for d in drafts[-LIMIT:]], ensure_ascii=False)
