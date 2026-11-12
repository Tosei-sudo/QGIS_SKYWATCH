"""
保存できなかった記録 (drafts): a record whose save did not go through (the
network, the sign-in, someone else's change) is kept on this computer with
what the form said, so that it is not lost when another form is open, the
dock is closed or QGIS is quit (tsudo, 2026-11-08).

Since 2026-11-09 the drafts wait in the 送信待ち outbox of the offline work
(core/offline.py, kind "draft"), beside the changes made while a server was
down: one list (ui/outbox_dialog.py) of everything not on the server yet,
with why, sent the same way (core/obs/offline_stores.upload_drafts), opened
in the form again, or thrown away. Drafts kept before (QgsSettings, per
store) are moved there the first time their store is read.

The record keeps its own id from the first try: a save whose answer was lost
(the server may hold it) becomes an update when sent again (#73), never a
second record. A change to a record keeps the 更新日時 it was made from:
sent again after someone else changed that record, it is not saved over
their change (#74); such a draft waits for the person (`needs_check`): it is
sent only when chosen in the list, after being looked at in the form.
"""
from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field, fields
from typing import Any, Optional

from .model import Observation

#: At most this many are kept per store (the oldest dropped first).
LIMIT = 200
#: The outbox kind of the drafts (core/offline.py KINDS).
KIND = "draft"


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
    #: Someone else changed the record since (or it is gone): to be looked at, not sent again by itself.
    needs_check: bool = False

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

    def title(self) -> str:
        """What it is, in a line (「あおば: 変更 船舶（停泊）」)."""
        r = self.record
        what = "変更" if self.editing else "継続" if self.source_id else "新規"
        text = f"{what} {r.kind}" + (f"（{r.activity}）" if r.activity else "") + ("" if r.present else "（不在）")
        return f"{r.object_name}: {text}" if r.object_name else text


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
                     int(value.get("kept_at") or 0), str(value.get("reason") or ""), bool(value.get("needs_check", False)))
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


def prefs_key(store_label: str) -> str:
    """Where a store's drafts were kept before the outbox (book.Prefs / QgsSettings `observations/drafts/<store>`)."""
    return f"drafts/{store_label}"


class PrefsDrafts:
    """
    Drafts kept in the book's small values (book.Prefs): a book with no
    outbox (scripts, tests). The plugin keeps them in the outbox (OutboxDrafts).
    """

    def __init__(self, prefs: Any):
        self.prefs = prefs

    def all(self, store: str) -> list[Draft]:
        return parse_drafts(self.prefs.get(prefs_key(store)))

    def get(self, store: str, draft_id: str) -> Optional[Draft]:
        return next((d for d in self.all(store) if d.id == draft_id), None)

    def keep(self, store: str, draft: Draft, replaces: Optional[str] = None) -> None:
        kept = [d for d in self.all(store) if d.id not in (draft.id, replaces)]
        self.prefs.set(prefs_key(store), drafts_text(kept + [draft]))

    def drop(self, store: str, draft_ids: list[str]) -> int:
        kept = self.all(store)
        rest = [d for d in kept if d.id not in set(draft_ids)]
        if len(rest) != len(kept):
            self.prefs.set(prefs_key(store), drafts_text(rest))
        return len(kept) - len(rest)


class OutboxDrafts:
    """
    Drafts in the 送信待ち outbox (core/offline.py OfflineWork, kind "draft"):
    the item's data is the draft, its op "check" when it needs the person,
    its note the reason. `prefs` (QgsSettings): where drafts were kept
    before, moved here the first time a store's drafts are read.
    """

    def __init__(self, work: Any, prefs: Any = None):
        self.work = work
        self.prefs = prefs
        self._moved: set[str] = set()

    def _move_old(self, store: str) -> None:
        if self.prefs is None or store in self._moved:
            return
        self._moved.add(store)
        old = parse_drafts(self.prefs.get(prefs_key(store)))
        for d in old:
            self.keep(store, d)
        if old or self.prefs.get(prefs_key(store)):
            self.prefs.set(prefs_key(store), "")

    def all(self, store: str) -> list[Draft]:
        self._move_old(store)
        return [d for d in (draft_of(p.data) for p in self.work.pending(KIND, store)) if d is not None]

    def get(self, store: str, draft_id: str) -> Optional[Draft]:
        self._move_old(store)
        p = self.work.item(KIND, store, draft_id)
        return draft_of(p.data) if p is not None else None

    def keep(self, store: str, draft: Draft, replaces: Optional[str] = None) -> None:
        self._move_old(store)
        if replaces and replaces != draft.id:
            self.work.drop(KIND, store, [replaces])
        self.work.keep(KIND, store, draft.id, "check" if draft.needs_check else "retry", draft.to_json(),
                       title=draft.title(), note=draft.reason)
        waiting = self.work.pending(KIND, store)
        if len(waiting) > LIMIT:
            self.work.drop(KIND, store, [p.item_id for p in waiting[:len(waiting) - LIMIT]])

    def drop(self, store: str, draft_ids: list[str]) -> int:
        self._move_old(store)
        return self.work.drop(KIND, store, draft_ids)
