"""
Sending what waits in the 送信待ち outbox (core/offline.py), store by store as
the settings file names them: the work kept while servers could not be
reached (observation records and checked areas, imaging requests) and the
observation records whose save did not go through (drafts, also of the
local store). What a store no longer in the settings file kept stays waiting
(to be thrown away by the person).

`only`: the items chosen in the list (ui/outbox_dialog.py), as (kind, store,
item id); None sends everything that goes without being chosen (drafts that
need the person are left). `outbox`: the offline work kept to (the plugin's);
by default the one the Esri client keeps to.
"""
from __future__ import annotations

from typing import Any, Callable, Optional

from .esri import EsriClient
from .gpkg import GeoPackage
from .obs.drafts import KIND as DRAFT, OutboxDrafts
from .obs.offline_stores import OfflineAreaStore, OfflineObservationStore, upload_drafts
from .obs.setup import LOCAL_LABEL, store_sets
from .obs.stores import LocalObservationStore
from .offline import KINDS, OfflineWork, UploadReport, now_ms
from .request.store import upload_requests


def upload_all(config: Any, client: Callable[[], EsriClient], token_for: Callable[[str, Optional[str]], Optional[str]],
               prefs: Any, gpkg: Callable[[], GeoPackage], now: Callable[[], int] = now_ms,
               only: Optional[set] = None, outbox: Optional[OfflineWork] = None) -> UploadReport:
    report = UploadReport()
    esri = client()
    if outbox is None:
        outbox = getattr(esri, "offline", None)
    if not isinstance(outbox, OfflineWork):
        return report
    # The work kept offline is sent only while the offline work is on; drafts always.
    work = outbox if outbox.enabled else None
    shelf = OutboxDrafts(outbox, prefs)
    known: dict[str, set] = {k: set() for k in KINDS}
    for s in store_sets(config.observation_stores, config.observation_vocabulary, gpkg, client, token_for, local=False):
        known["observation"].add(s.label)
        known[DRAFT].add(s.label)
        if work is not None and isinstance(s.records, OfflineObservationStore):
            s.records.upload(report, lambda d, label=s.label: shelf.keep(label, d), now, only)
        if work is not None and isinstance(s.areas, OfflineAreaStore):
            known["area"].add(s.areas.label)
            s.areas.upload(report, now, only)
        upload_drafts(s.records, outbox, report, prefs, now, only)
    if shelf.all(LOCAL_LABEL):
        known[DRAFT].add(LOCAL_LABEL)
        upload_drafts(LocalObservationStore(gpkg()), outbox, report, prefs, now, only)
    for r in config.request_stores:
        known["request"].add(r.label)
        if work is not None:
            upload_requests(esri, r, token_for(r.url, r.token), report, now(), only)
    for kind, label, n in [(k, s, n) for k in KINDS for s, n in _stores_of(outbox, k, only).items()]:
        if work is None and kind != DRAFT:
            report.kept.append(f"オフライン作業（offlineWork）が切ってあるため送らない「{label}」の{KINDS.get(kind, kind)} {n} 件")
        elif kind == "plan":
            # Saved before 撮像計画 became 臨時 requests (2026-11-09): there is no plan store to send it to.
            report.kept.append(f"撮像計画の保存はなくなった（撮像依頼に一本化）ため送らない「{label}」の撮像計画 {n} 件")
        elif label not in known.get(kind, set()):
            report.kept.append(f"設定ファイルにない保存先「{label}」の{KINDS[kind]} {n} 件")
    return report


def _stores_of(work, kind: str, only: Optional[set] = None) -> dict[str, int]:
    out: dict[str, int] = {}
    for p in work.pending(kind):
        if only is None or (p.kind, p.store, p.item_id) in only:
            out[p.store] = out.get(p.store, 0) + 1
    return out
