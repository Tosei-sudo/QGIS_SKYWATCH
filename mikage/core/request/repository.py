"""
撮像依頼・撮像予定のデータ窓口 (docs/architecture.md): the ledger's reads and writes, and those of its
history (撮像予定) and shots, with the store, the client and its token bound once.

A screen makes one where a step starts (on its own thread: the token as it is then) and uses it in
the step's task; a test can give the screen another one. Each method is the store function of the
same name with the three bound (the SQL, the TBL_ names and how times are written stay in those).
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from typing import TYPE_CHECKING, Callable, Optional, Sequence

from ..esri import EsriClient
from ..plan.api import NO_NAMES, SatelliteNames
from .bulk import apply_change
from .covered import read_covered
from .follow import follow_and_save, save_marked, with_settled_times
from .geometry import read_last_looks
from .groups import delete_groups, group_counts, load_groups, read_groups, rename_requests, save_group, save_orders
from .linking import links_of
from .quota import read_quota_data
from .store import (
    RequestStoreConfig, add_history, add_shots, clock_missing, count_history, day_counts, delete_history_rows, delete_request, delete_shots,
    history_has, load_day, load_days, load_history, load_requests, load_shots, load_shots_where, load_system_rows, save_request,
    save_requests, update_history, update_requests, update_shots, waiting_ids,
)
from .use_time import read_other_uses

if TYPE_CHECKING:
    from ..catalog.api import CatalogConfig
    from .bulk import BulkResult, RequestChange
    from .follow import Finding
    from .groups import RequestGroup
    from .model import ImagingRequest
    from .quota import QuotaData
    from .store import HistoryRow, Saved, SavedRequests, ShotRow
    from .use_time import OtherUse


def requests_of(client: EsriClient, store: RequestStoreConfig, token: Optional[str], ids: Sequence[str],
                cancelled: Optional[Callable[[], bool]] = None) -> dict[str, ImagingRequest]:
    """follow_job.requests_of, read when asked (with its urllib and ssl: not at the plugin's start)."""
    from .follow_job import requests_of as read

    return read(client, store, token, ids, cancelled)


@dataclass(frozen=True)
class RequestRepository:
    """撮像依頼の台帳 (the ledger of a request store): read, saved, changed and deleted."""

    client: EsriClient
    store: RequestStoreConfig
    token: Optional[str]

    def read_groups(self, cancelled=None) -> tuple[Optional[list], str]:
        return read_groups(self.client, self.store.groups, self.store.group_fields, self.token, cancelled)

    # 「依頼グループ…」 (ui/request_group_dialog.py): the groups table's rows added, changed, ordered and deleted.

    def load_groups(self, cancelled=None) -> list[RequestGroup]:
        return load_groups(self.client, self.store.groups, self.store.group_fields, self.token, cancelled)

    def group_table_fields(self) -> set[str]:
        """The attributes the groups table has (read again)."""
        return set(self.client.layer(self.store.groups, self.token, refresh=True).field_types)

    def save_group(self, group: RequestGroup) -> RequestGroup:
        return save_group(self.client, self.store.groups, self.store.group_fields, group, self.token)

    def save_group_orders(self, groups: Sequence[RequestGroup]) -> tuple[int, list[str]]:
        return save_orders(self.client, self.store.groups, self.store.group_fields, groups, self.token)

    def delete_groups(self, groups: Sequence[RequestGroup]) -> tuple[list[str], list[str]]:
        return delete_groups(self.client, self.store.groups, groups, self.token)

    def group_counts(self, cancelled=None) -> dict[str, int]:
        return group_counts(self.client, self.store, self.token, cancelled)

    def rename_requests(self, old: str, new: str, now: Callable[[], float], canceled: Callable[[], bool] = lambda: False) -> BulkResult:
        return rename_requests(self.client, self.store, self.token, old, new, now, canceled)

    def waiting_ids(self) -> set[str]:
        return waiting_ids(self.client, self.store)

    def load_requests(self, cancelled: Optional[Callable[[], bool]] = None, where: str = "1=1") -> list[ImagingRequest]:
        return load_requests(self.client, self.store, self.token, cancelled, where)

    def clock_missing(self) -> list[str]:
        return clock_missing(self.client, self.store, self.token)

    def save_request(self, req: ImagingRequest, now: float) -> ImagingRequest:
        return save_request(self.client, self.store, self.token, req, now)

    def save_requests(self, reqs: Sequence[ImagingRequest], now: float, canceled: Callable[[], bool] = lambda: False,
            progress: Callable[[float], None] = lambda _p: None) -> SavedRequests:
        return save_requests(self.client, self.store, self.token, reqs, now, canceled, progress)

    def delete_request(self, req: ImagingRequest) -> int:
        return delete_request(self.client, self.store, self.token, req)

    def update_requests(self, changes: Sequence[tuple[int, dict]], now: Optional[float] = None) -> Saved:
        return update_requests(self.client, self.store, self.token, changes, now)

    def apply_change(self, requests: Sequence[ImagingRequest], change: RequestChange, now: Callable[[], float],
            canceled: Callable[[], bool] = lambda: False, progress: Callable[[float], None] = lambda _p: None) -> BulkResult:
        return apply_change(self.client, self.store, self.token, requests, change, now, canceled, progress)

    def requests_of(self, ids: Sequence[str], cancelled: Optional[Callable[[], bool]] = None) -> dict[str, ImagingRequest]:
        return requests_of(self.client, self.store, self.token, ids, cancelled)


@dataclass(frozen=True)
class ScheduleRepository(RequestRepository):
    """撮像予定: the store's history (the plan of each day) and shots, its quotas and followed images, and its ledger."""

    def load_day(self, day: str, cancelled: Optional[Callable[[], bool]] = None) -> list[HistoryRow]:
        return load_day(self.client, self.store, self.token, day, cancelled)

    def load_days(self, first: str, last: str, cancelled: Optional[Callable[[], bool]] = None) -> list[HistoryRow]:
        return load_days(self.client, self.store, self.token, first, last, cancelled)

    def load_system_rows(self, system: str, first: str, last: str, cancelled: Optional[Callable[[], bool]] = None) -> list[HistoryRow]:
        return load_system_rows(self.client, self.store, self.token, system, first, last, cancelled)

    def day_counts(self, first: str, last: str) -> dict[str, dict]:
        return day_counts(self.client, self.store, self.token, first, last)

    def load_history(self, where: str = "1=1", cancelled: Optional[Callable[[], bool]] = None, shapes: bool = False) -> list[HistoryRow]:
        return load_history(self.client, self.store, self.token, where, cancelled, shapes)

    def count_history(self, where: str) -> int:
        return count_history(self.client, self.store, self.token, where)

    def history_has(self, roles: Sequence[str]) -> list[str]:
        return history_has(self.client, self.store, self.token, roles)

    def add_history(self, rows: Sequence[HistoryRow]) -> Saved:
        return add_history(self.client, self.store, self.token, rows)

    def update_history(self, changes: Sequence[tuple[int, dict]]) -> Saved:
        return update_history(self.client, self.store, self.token, changes)

    def delete_history_rows(self, object_ids: Sequence[int]) -> Saved:
        return delete_history_rows(self.client, self.store, self.token, object_ids)

    def load_shots(self, day: str, cancelled: Optional[Callable[[], bool]] = None) -> list[ShotRow]:
        return load_shots(self.client, self.store, self.token, day, cancelled)

    def load_shots_where(self, where: str, geometry: bool = False, cancelled: Optional[Callable[[], bool]] = None) -> list[ShotRow]:
        return load_shots_where(self.client, self.store, self.token, where, geometry, cancelled)

    def add_shots(self, rows: Sequence[ShotRow]) -> Saved:
        return add_shots(self.client, self.store, self.token, rows)

    def update_shots(self, changes: Sequence[tuple[int, dict]]) -> Saved:
        return update_shots(self.client, self.store, self.token, changes)

    def delete_shots(self, where: str) -> Saved:
        return delete_shots(self.client, self.store, self.token, where)

    def read_quota_data(self, days: Sequence[date], cancelled: Optional[Callable[[], bool]] = None) -> QuotaData:
        return read_quota_data(self.client, self.store, self.token, days, cancelled)

    def read_covered(self, catalog, catalog_token: Optional[str], requests: Sequence[ImagingRequest], day: date,
            history: Sequence, names=None, cancelled: Optional[Callable[[], bool]] = None) -> tuple[dict, list]:
        return read_covered(self.client, self.store, self.token, catalog, catalog_token, requests, day, history, names, cancelled)

    def read_last_looks(self, requests: Sequence[ImagingRequest], day: date, cancelled: Optional[Callable[[], bool]] = None) -> dict:
        return read_last_looks(self.client, self.store, self.token, requests, day, cancelled)

    def read_other_uses(self, day: date, cancelled: Optional[Callable[[], bool]] = None) -> list[OtherUse]:
        return read_other_uses(self.client, self.store, self.token, day, cancelled)

    def with_settled_times(self, history: Sequence[HistoryRow], requests: dict, cancelled: Optional[Callable[[], bool]] = None) -> list[HistoryRow]:
        return with_settled_times(self.client, self.store, self.token, history, requests, cancelled)

    def save_marked(self, findings: Sequence[Finding], requests: dict, history: Sequence[HistoryRow], now: float,
            problems: Optional[list] = None) -> bool:
        return save_marked(self.client, self.store, self.token, findings, requests, history, now, problems)

    def follow_and_save(self, catalog: CatalogConfig, catalog_token: Optional[str], history: Sequence[HistoryRow],
            requests: dict, now: float, names: SatelliteNames = NO_NAMES, cancelled: Optional[Callable[[], bool]] = None,
            problems: Optional[list] = None, unusable: Optional[Callable[[str, Sequence], bool]] = None) -> tuple[list[Finding], bool]:
        return follow_and_save(self.client, self.store, self.token, catalog, catalog_token, history, requests, now, names, cancelled, problems,
                               unusable)

    def links_of(self, image_ids: Sequence[str], cancelled: Optional[Callable[[], bool]] = None) -> dict[str, list[HistoryRow]]:
        return links_of(self.client, self.store, self.token, image_ids, cancelled)
