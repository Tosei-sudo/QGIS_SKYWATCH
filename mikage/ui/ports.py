"""
The ports (窓口) between features (docs/architecture.md): what one feature's dock may ask of
another's, and the notices (PluginEvents) one sends when something changed that others show.

A dock never gets another feature's dock to reach into: it gets a function giving that
feature's port (None while there is none: not made, or not in the roles in use where the
plugin says so), and calls only what the port's Protocol lists. The plugin's features set
the functions in `Ports` (mikage/features); the docks are the ports' implementations today.
"""
from __future__ import annotations

from typing import Any, Callable, Optional, Protocol, Sequence

from qgis.PyQt.QtCore import QObject, pyqtSignal


class ObservationsPort(Protocol):
    """観測記録 (ui/observation_dock.py)."""

    #: The records read (core/obs/model.ObservationBook), None before a store is chosen.
    book: Any

    def list_catalog_images(self, images: list, replacing: Sequence[str] = ()) -> Any:
        """
        Images of a catalog without their files, listed to record on (None: no store or no image id);
        those of `replacing` listed before go off the list (「前の画像を閉じる」).
        """

    def unlist_catalog_images(self, image_ids: Sequence[str]) -> int:
        """Images listed without their files taken off the list, their records kept (「前の画像を閉じる」): how many were on it."""

    def with_records(self, ids: Sequence[str], then: Callable[[], Any]) -> bool:
        """True when those records are read; else read first, `then` once they are."""

    def open_record(self, o: Any) -> None:
        """The record shown in the dock's form."""

    def mark_reported(self, ids) -> None:
        """Records reported (通報) highlighted."""

    def refresh_reported(self) -> None:
        """The highlights of the reported records again (one was taken back)."""

    def open_alerts(self, tab: int = 0) -> Any:
        """「通報文の作成」 (0) or 「通報記録の確認」 (1)."""

    def show(self) -> None: ...

    def raise_(self) -> None: ...


class CatalogPort(Protocol):
    """画像カタログ (ui/catalog_dock.py)."""

    def choose_folder(self, mapping) -> Optional[str]: ...

    def choose_images(self, record, files: list, more: bool) -> Optional[list]: ...

    def drawn_shape(self) -> Optional[list]: ...

    def open_image_id(self, text: str, then=None, missing=None) -> Any:
        """An image opened by its id (開いた画像の履歴, else the catalogs)."""

    def show_event_records(self, catalog, records, text: str) -> None:
        """The records listed, with what they are in the status line, the dock in front."""

    def search_area_period(self, start: str, end: str, corners: Optional[list] = None, anywhere: bool = False,
                           sensor: Optional[str] = None) -> None:
        """
        Searched over a period (its days, "" open) and an area (its corners; `anywhere`: none), the dock in front;
        `sensor`: the センサー box's (several separated by 「、」; "" every sensor; None as it is).
        """

    def sensor_values(self) -> Optional[list]:
        """The sensors the catalog chosen lists (none before they are read); None when it has no sensor attribute."""


class RequestsPort(Protocol):
    """撮像依頼 (ui/request_dock.py)."""

    #: Whether the ledger was read.
    loaded: bool
    #: The requests read (none before).
    requests: list
    #: Whether there is a ledger to save to (`requestStores`).
    store: Any

    def requests_from_areas(self, areas: list) -> Any:
        """Its form opened with a request of each organisation's area (組織の関心地域's 「撮像依頼…」), the dock in front."""

    def follow_area(self, area_id: str, then: Optional[Callable[[Any], Any]] = None, show: bool = True) -> bool:
        """An area saved with a new shape: its requests take it at once, what was done shown (地域の形に合わせる)."""

    def reload(self, then=None, failed=None) -> None: ...

    def select(self, ids) -> None: ...

    def selected(self) -> list: ...

    def from_plan(self, ops: list, sats: list, targets: list) -> Any:
        """Its form opened with 臨時 requests of the imaging plan's opportunities (「臨時依頼を作成」)."""

    def show(self) -> None: ...

    def raise_(self) -> None: ...


class SatellitesPort(Protocol):
    """The satellites of the imaging plan's catalog (撮像計画)."""

    def read(self, then: Callable[[list], Any], failed: Optional[Callable[[str], Any]] = None) -> None:
        """The satellites, read once (`then(list)`), or why not (`failed(text)`)."""

    def known(self) -> list:
        """The satellites as read so far (none before)."""


class OrgAreasPort(Protocol):
    """組織の関心地域 (qgis_io/org_areas_io.OrgAreaBook): the named area layer's rows."""

    #: The settings (core/named_areas.NamedAreaSettings), None without the layer; the rows read (None before).
    settings: Any
    areas: Optional[list]
    loading: bool

    def load(self, then: Optional[Callable[[], Any]] = None) -> None: ...

    def want(self) -> None: ...

    def shapes(self, areas: list, done: Callable[[list], Any], failed: Callable[[str], Any] = ...) -> None:
        """The areas with their shapes (a place name's read when asked)."""

    def at_point(self, lon: float, lat: float, done: Callable[[list], Any], failed: Callable[[str], Any]) -> bool:
        """The areas holding a point, with their shapes, the smallest first."""

    def find_id(self, area_id: str) -> Any: ...


class EventsPort(Protocol):
    """イベント (ui/events_dock.py)."""

    def offer(self, anchor, items: Callable[[], Sequence]) -> Any:
        """「イベントに追加」's menu under `anchor` for the items (images, records, requests)."""


class ReferencesPort(Protocol):
    """参考情報 (ui/reference_dock.py)."""

    def can_link(self) -> bool:
        """Whether infos can be linked (`referenceInfo.links`)."""

    def offer_link(self, targets: Sequence, parent=None, then: Optional[Callable[[], Any]] = None) -> Any:
        """「参考情報に紐付け…」: infos chosen (or a new one) linked to the items (core/reference_info.LinkTarget)."""

    def show_infos(self, infos: Sequence, what: str) -> None:
        """The infos listed on their own (an image's), the dock in front."""

    def show_for_items(self, items: Sequence[tuple[str, str]], what: str) -> None:
        """The infos linked to these items ((type, id)) listed, the dock in front."""

    def infos_for_items(self, items: Sequence[tuple[str, str]], then: Callable[[list], Any],
                        failed: Optional[Callable[[str], Any]] = None) -> bool:
        """The links of these items with their infos ([(link, info)]); False without the link table."""

    def unlink(self, links: Sequence, then: Optional[Callable[[], Any]] = None) -> None: ...


class NoticesPort(Protocol):
    """通知 (ui/notices_dock.py)."""

    def add_plan_notice(self, reminder, text: str, now: Optional[float] = None) -> None: ...


def _none():
    return None


class Ports:
    """Each feature's port as the plugin's features set it (a function: the port, or None)."""

    def __init__(self):
        self.observations: Callable[[], Optional[ObservationsPort]] = _none
        self.catalog: Callable[[], Optional[CatalogPort]] = _none
        self.requests: Callable[[], Optional[RequestsPort]] = _none
        self.events: Callable[[], Optional[EventsPort]] = _none
        self.references: Callable[[], Optional[ReferencesPort]] = _none
        self.notices: Callable[[], Optional[NoticesPort]] = _none
        self.satellites: Callable[[], Optional[SatellitesPort]] = _none
        self.org_areas: Callable[[], Optional[OrgAreasPort]] = _none


class PluginEvents(QObject):
    """What changed, for the features that show it (Qt signals: heard at once, in the order connected)."""

    #: 撮像依頼 written or changed by another dock (撮像計画ファイル出力): the requests as read there, or None (read again).
    requests_changed = pyqtSignal(object)
    #: The 「撮像依頼」 dock read the ledger (組織の関心地域 counts each area's requests again).
    requests_read = pyqtSignal()
