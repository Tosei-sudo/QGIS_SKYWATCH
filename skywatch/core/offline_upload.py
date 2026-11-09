"""
Sending the work kept while servers could not be reached (core/offline.py),
store by store as the settings file names them: observation records and
checked areas, imaging requests, imaging plans. What a store no longer in
the settings file kept stays waiting (to be thrown away by the person).
"""
from __future__ import annotations

from typing import Any, Callable, Optional

from .esri import EsriClient
from .gpkg import GeoPackage
from .obs.drafts import keep_draft_in
from .obs.offline_stores import OfflineAreaStore, OfflineObservationStore
from .obs.setup import store_sets
from .offline import KINDS, UploadReport, now_ms, offline_of
from .request.store import upload_requests


def upload_all(config: Any, client: Callable[[], EsriClient], token_for: Callable[[str, Optional[str]], Optional[str]],
               prefs: Any, gpkg: Callable[[], GeoPackage], now: Callable[[], int] = now_ms) -> UploadReport:
    report = UploadReport()
    esri = client()
    work = offline_of(esri)
    if work is None:
        return report
    known: dict[str, set] = {k: set() for k in KINDS}
    for s in store_sets(config.observation_stores, config.observation_vocabulary, gpkg, client, token_for, local=False):
        known["observation"].add(s.label)
        if isinstance(s.records, OfflineObservationStore):
            s.records.upload(report, lambda d, label=s.label: keep_draft_in(prefs, label, d), now)
        if isinstance(s.areas, OfflineAreaStore):
            known["area"].add(s.areas.label)
            s.areas.upload(report, now)
    for r in config.request_stores:
        known["request"].add(r.label)
        upload_requests(esri, r, token_for(r.url, r.token), report, now())
    for kind, label, n in [(k, s, n) for k in KINDS for s, n in _stores_of(work, k).items()]:
        if kind == "plan":
            # Saved before 撮像計画 became 臨時 requests (2026-11-09): there is no plan store to send it to.
            report.kept.append(f"撮像計画の保存はなくなった（撮像依頼に一本化）ため送らない「{label}」の撮像計画 {n} 件")
        elif label not in known.get(kind, set()):
            report.kept.append(f"設定ファイルにない保存先「{label}」の{KINDS[kind]} {n} 件")
    return report


def _stores_of(work, kind: str) -> dict[str, int]:
    out: dict[str, int] = {}
    for p in work.pending(kind):
        out[p.store] = out.get(p.store, 0) + 1
    return out
