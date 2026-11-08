"""
The Esri stores of records and checked areas while their server cannot be
reached (core/offline.py): reads come from the cache of the last ones (the
transport answers them), changes are kept as 送信待ち (waiting to be sent) and
read back over what the server held, so work goes on; `upload` sends them
once the server answers again.

Not the local store 「ローカル」: what waits here belongs to its server's
store (its label) and is only on its way there — allowed even when
`localObservations` is false.
"""
from __future__ import annotations

from dataclasses import asdict, fields, replace
from typing import Any, Callable, Optional

from ..esri import ConflictError, EsriError, UnreachableError
from ..offline import OfflineWork, Pending, UploadReport, now_ms, tried_of
from .areas import CheckedArea
from .drafts import Draft
from .model import Observation
from .stores import EsriAreaStore, EsriObservationStore, _marked

_RECORD_FIELDS = {f.name for f in fields(Observation)}
_AREA_FIELDS = {f.name for f in fields(CheckedArea)}


def record_of(p: Pending) -> Optional[Observation]:
    try:
        return Observation(**{k: v for k, v in (p.data.get("record") or {}).items() if k in _RECORD_FIELDS})
    except TypeError:
        return None


def area_of(p: Pending) -> Optional[CheckedArea]:
    try:
        return CheckedArea(**{k: v for k, v in (p.data.get("area") or {}).items() if k in _AREA_FIELDS})
    except TypeError:
        return None


class _Waiting:
    """What the two stores share: what waits for this store, read over the server's."""
    kind = ""
    key = ""

    def __init__(self, inner, work: OfflineWork):
        self.inner = inner
        self.work = work
        self.label = inner.label

    def __getattr__(self, name: str) -> Any:
        if name in ("inner", "clear"):
            raise AttributeError(name)
        return getattr(self.inner, name)

    @property
    def url(self) -> str:
        return self.inner.config.url

    @property
    def offline(self) -> bool:
        return self.work.down(self.url)

    def pending_ids(self) -> set[str]:
        """The ids of what waits to be sent (to mark them on screen)."""
        return self.work.ids(self.kind, self.label)

    def _item_of(self, p: Pending):
        raise NotImplementedError

    def _overlay(self, items: list, only: Optional[set] = None) -> list:
        """`items` with what waits put over them (changed ones replaced, new ones added, those deleted for good left out)."""
        waiting = self.work.pending(self.kind, self.label)
        if not waiting:
            return items
        out = {x.id: x for x in items}
        for p in waiting:
            if only is not None and p.item_id not in only and p.item_id not in out:
                continue
            if p.op == "remove" and p.data.get("forGood"):
                out.pop(p.item_id, None)
                continue
            item = self._item_of(p)
            if item is not None:
                out[item.id] = item
        return list(out.values())

    def _keep(self, items: list, op: str, for_good: bool = False) -> None:
        for x in items:
            before = self.work.item(self.kind, self.label, x.id)
            known = before.known if before is not None else x.id in self.inner.oids
            if op == "remove" and not known:
                # Made while offline and deleted again: nothing to send.
                self.work.drop(self.kind, self.label, [x.id])
                continue
            seen = getattr(self.inner, "stamps", {}).get(x.id) if known else None
            data = {self.key: asdict(x), "forGood": for_good}
            lost = getattr(self.inner, "pending", {}).get(x.id)
            if lost is not None:
                # The save that found the server down timed out: it may be there (not someone else's change when sent).
                data["tried"] = [lost]
            self.work.keep(self.kind, self.label, x.id, op, data, seen, known, self._title(x))

    def _sent(self, ids) -> None:
        """Items saved (or removed) on the server directly: what waited for them is superseded, not sent later over them."""
        ids = [i for i in ids if i]
        if ids and self.work.ids(self.kind, self.label) & set(ids):
            self.work.drop(self.kind, self.label, ids)

    def _title(self, x) -> str:
        return ""

    def load(self) -> list:
        """What the server holds (or the cache, while it cannot be reached), with what waits over it."""
        return self._overlay(self.inner.load())


class OfflineObservationStore(_Waiting):
    """An EsriObservationStore that keeps on working while its server cannot be reached."""
    kind = "observation"
    key = "record"

    def __init__(self, inner: EsriObservationStore, work: OfflineWork):
        super().__init__(inner, work)
        self.last_saved: set[str] = set()
        self.last_conflicts: set[str] = set()
        self.last_removed: set[str] = set()
        self.last_marked: list[Observation] = []

    def _item_of(self, p: Pending):
        return record_of(p)

    def _title(self, o: Observation) -> str:
        return " ".join(t for t in (o.object_name, o.image_name) if t)

    def load_changed(self, since: float) -> Optional[list[Observation]]:
        if self.offline:
            return None  # read whole (from the cache), with what waits over it
        changed = self.inner.load_changed(since)
        return None if changed is None else self._overlay(changed, only={o.id for o in changed})

    def removes_for_good(self) -> bool:
        try:
            return self.inner.removes_for_good()
        except UnreachableError:
            return not self.inner.config.soft_delete

    def save(self, records: list[Observation]) -> None:
        self.last_saved, self.last_conflicts = set(), set()
        if not records:
            return
        if not self.offline:
            try:
                self.inner.save(records)
                self.last_saved, self.last_conflicts = set(self.inner.last_saved), set(self.inner.last_conflicts)
                self._sent(self.last_saved)
                return
            except EsriError:
                # Cut off on the way (its checks may then have read the cache): what was not saved waits.
                if not self.offline:
                    self.last_saved, self.last_conflicts = set(self.inner.last_saved), set(self.inner.last_conflicts)
                    self._sent(self.last_saved)
                    raise
                done = set(self.inner.last_saved)
                self._sent(done)
                records = [o for o in records if o.id not in done]
                self.last_saved = done
        self._keep(records, "save")
        self.last_saved |= {o.id for o in records}

    def remove(self, records: list[Observation], now: Optional[int] = None) -> list[Observation]:
        self.last_removed, self.last_marked, self.last_conflicts = set(), [], set()
        done: set[str] = set()
        marked_done: list[Observation] = []
        if not self.offline:
            try:
                out = self.inner.remove(records, now)
                self.last_removed, self.last_marked = set(self.inner.last_removed), list(self.inner.last_marked)
                self.last_conflicts = set(self.inner.last_conflicts)
                self._sent(self.last_removed)
                return out
            except EsriError:
                if not self.offline:
                    self.last_removed, self.last_marked = set(self.inner.last_removed), list(self.inner.last_marked)
                    self.last_conflicts = set(self.inner.last_conflicts)
                    self._sent(self.last_removed)
                    raise
                done = set(self.inner.last_removed)
                self._sent(done)
                marked_done = list(self.inner.last_marked)
                records = [o for o in records if o.id not in done]
        for_good = self.removes_for_good()
        marked = _marked(records, now)
        self._keep(marked, "remove", for_good)
        self.last_removed = done | {o.id for o in records}
        if for_good:
            self.last_marked = marked_done
            return []
        self.last_marked = marked_done + marked
        return marked_done + marked

    def upload(self, report: UploadReport, keep_draft: Callable[[Draft], None], now: Callable[[], int] = now_ms) -> None:
        """
        Sends what waits. A record someone else changed meanwhile is not saved
        over: it goes to 「未保存の記録」 (`keep_draft`) to be looked at and sent
        from there. What cannot be sent (the server refused) stays waiting.
        """
        waiting = self.work.pending(self.kind, self.label)
        if not waiting:
            return
        at = now()
        saves: list[Observation] = []
        gone: list[Observation] = []
        by_id: dict[str, Pending] = {}
        for p in waiting:
            o = record_of(p)
            if o is None:
                self.work.drop(self.kind, self.label, [p.item_id])
                continue
            # Sent now: others' 「最新を反映」 reads what changed since their last read.
            o = replace(o, updated_at=at, deleted_at=at if o.deleted_at is not None else None)
            by_id[o.id] = p
            if p.op == "remove" and p.data.get("forGood"):
                gone.append(o)
                continue
            if p.known:
                self.inner.expect(o.id, p.base, at, tried_of(p))
            saves.append(o)
        if saves:
            try:
                self.inner.save(saves)
            except ConflictError:
                pass
            except EsriError as error:
                for o in saves:
                    if o.id not in self.inner.last_saved:
                        self.work.note(by_id[o.id], str(error))
                        # This send's answer lost (a timeout): it may be on the server when sent again.
                        self.work.add_tried(self.kind, self.label, o.id, [self.inner.pending.get(o.id)])
                if not self.inner.last_saved:
                    report.kept.append(f"{KIND_NAME}（{self.label}）: {error}")
            sent = set(self.inner.last_saved)
            self.work.drop(self.kind, self.label, sent)
            report.add(self.kind, len(sent))
            for o in saves:
                if o.id in self.inner.last_conflicts:
                    p = by_id[o.id]
                    original = record_of(p) or o
                    keep_draft(Draft(original, _form_values(original), original.present, editing_id=o.id,
                                     base_updated=int(p.base) if p.base is not None else None, kept_at=at,
                                     reason="オフライン中の変更: ほかの人が先に更新したため送らず"))
                    self.work.drop(self.kind, self.label, [o.id])
                    report.drafts += 1
        if gone:
            try:
                self.inner.know([o.id for o in gone])
                self.inner.remove(gone, at)
            except UnreachableError as error:
                report.kept.append(f"{KIND_NAME}の削除（{self.label}）: {error}")
                return
            except EsriError as error:
                report.kept.append(f"{KIND_NAME}の削除（{self.label}）: {error}")
            done = {o.id for o in gone} & set(self.inner.last_removed)
            self.work.drop(self.kind, self.label, done)
            report.add(self.kind, len(done))


KIND_NAME = "観測記録"


def _form_values(o: Observation) -> dict[str, str]:
    def text(v) -> str:
        return "" if v is None else str(v)
    return {"object": o.object_name, "kind": o.kind, "activity": o.activity, "note": o.note, "confidence": o.confidence,
            "length": text(o.length), "heading": text(o.heading), "width": text(o.width)}


class OfflineAreaStore(_Waiting):
    """An EsriAreaStore that keeps on working while its server cannot be reached."""
    kind = "area"
    key = "area"

    def __init__(self, inner: EsriAreaStore, work: OfflineWork):
        super().__init__(inner, work)

    def _item_of(self, p: Pending):
        return area_of(p)

    def _title(self, a: CheckedArea) -> str:
        return a.image_name

    def _for_good(self) -> bool:
        try:
            return self.inner.removes_for_good()
        except UnreachableError:
            return not self.inner.config.soft_delete

    def save(self, areas: list[CheckedArea]) -> None:
        if not areas:
            return
        if not self.offline:
            try:
                self.inner.save(areas)
                self._sent([a.id for a in areas])
                return
            except EsriError:
                if not self.offline:
                    raise
        self._keep(areas, "save")

    def remove(self, areas: list[CheckedArea], now: Optional[int] = None) -> list[CheckedArea]:
        if not self.offline:
            try:
                out = self.inner.remove(areas, now)
                self._sent([a.id for a in areas])
                return out
            except EsriError:
                if not self.offline:
                    raise
        for_good = self._for_good()
        marked = _marked(areas, now)
        self._keep(marked, "remove", for_good)
        return [] if for_good else marked

    def upload(self, report: UploadReport, now: Callable[[], int] = now_ms) -> None:
        waiting = self.work.pending(self.kind, self.label)
        if not waiting:
            return
        at = now()
        saves, gone = [], []
        for p in waiting:
            a = area_of(p)
            if a is None:
                self.work.drop(self.kind, self.label, [p.item_id])
                continue
            a = replace(a, updated_at=at, deleted_at=at if a.deleted_at is not None else None)
            (gone if p.op == "remove" and p.data.get("forGood") else saves).append(a)
        try:
            if saves:
                self.inner.save(saves)
                self.work.drop(self.kind, self.label, [a.id for a in saves])
                report.add(self.kind, len(saves))
            if gone:
                self.inner.know([a.id for a in gone])
                self.inner.remove(gone, at)
                self.work.drop(self.kind, self.label, [a.id for a in gone])
                report.add(self.kind, len(gone))
        except EsriError as error:
            report.kept.append(f"確認範囲（{self.label}）: {error}")
