"""
Changing several imaging requests at once (一括編集): the priority, the
status and the kind (種別) of the requests chosen in the 「撮像依頼」 dock.

Each request is saved on its own through `save_request`, so the ledger's
checks hold for every one of them: a request someone else changed (or
deleted) since it was read is not overwritten (#74), and a save whose
answer was lost is not added twice (#73). The ones that could not be saved
are told by name with the reason; the others are saved.
"""
from __future__ import annotations

from dataclasses import dataclass, field, replace
from typing import Callable, Optional, Sequence

from ..esri import ConflictError, EsriClient
from .model import KINDS, STATUSES, ImagingRequest, clamp_priority
from .store import RequestStoreConfig, save_request


@dataclass
class RequestChange:
    """What is set on every request chosen; None leaves it as it is."""
    priority: Optional[int] = None
    status: Optional[str] = None
    kind: Optional[str] = None
    #: Also takes back the raises (繰り上げ) when the priority is set.
    reset_boost: bool = False

    def problems(self) -> list[str]:
        out = []
        if self.status is not None and self.status not in STATUSES:
            out.append(f"状態は {'・'.join(STATUSES)} のいずれか")
        if self.kind is not None and self.kind not in KINDS:
            out.append(f"種別は {'・'.join(KINDS)} のいずれか")
        return out

    @property
    def empty(self) -> bool:
        return self.priority is None and self.status is None and self.kind is None

    def text(self) -> str:
        """「優先度 300・状態 停止」."""
        parts = []
        if self.priority is not None:
            parts.append(f"優先度 {clamp_priority(self.priority)}" + ("（繰上げ解除）" if self.reset_boost else ""))
        if self.status is not None:
            parts.append(f"状態 {self.status}")
        if self.kind is not None:
            parts.append(f"種別 {self.kind}")
        return "・".join(parts)


def changed(req: ImagingRequest, change: RequestChange) -> Optional[ImagingRequest]:
    """A copy of the request with the change made; None when it already is so."""
    values = {}
    if change.priority is not None:
        p = clamp_priority(change.priority)
        if p != req.priority:
            values["priority"] = p
        if change.reset_boost and req.boost:
            values["boost"] = 0
    if change.status is not None and change.status != req.status:
        values["status"] = change.status
    if change.kind is not None and change.kind != req.kind:
        values["kind"] = change.kind
    return replace(req, **values) if values else None


@dataclass
class BulkResult:
    """What became of a bulk change."""
    saved: list = field(default_factory=list)
    #: Already as asked (not saved again).
    unchanged: int = 0
    #: (name, why) of those not saved; changed by someone else first among them.
    failed: list = field(default_factory=list)
    conflicts: int = 0
    canceled: bool = False

    def text(self) -> str:
        parts = [f"{len(self.saved)} 件を変更"]
        if self.unchanged:
            parts.append(f"変更不要 {self.unchanged} 件")
        if self.failed:
            names = "、".join(f"{n}（{why}）" for n, why in self.failed[:3]) + (" ほか" if len(self.failed) > 3 else "")
            parts.append(f"保存できず {len(self.failed)} 件: {names}")
        if self.canceled:
            parts.append("途中で中止")
        return "・".join(parts)


def apply_change(client: EsriClient, store: RequestStoreConfig, token: Optional[str], requests: Sequence[ImagingRequest],
                 change: RequestChange, now: Callable[[], float], canceled: Callable[[], bool] = lambda: False,
                 progress: Callable[[float], None] = lambda _p: None) -> BulkResult:
    """Saves the change on each request; one that fails does not stop the others."""
    result = BulkResult()
    todo = []
    for r in requests:
        c = changed(r, change)
        if c is None:
            result.unchanged += 1
        else:
            todo.append(c)
    for i, r in enumerate(todo):
        if canceled():
            result.canceled = True
            break
        try:
            result.saved.append(save_request(client, store, token, r, now()))
        except ConflictError:
            result.conflicts += 1
            result.failed.append((r.name or r.id, "ほかの人が読み込み後に更新・削除"))
        except Exception as error:  # noqa: BLE001 (each one told)
            result.failed.append((r.name or r.id, str(error)))
        progress(100 * (i + 1) / len(todo))
    return result
