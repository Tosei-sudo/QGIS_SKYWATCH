"""
画像: 画像カタログ, 通知 (and its 新着画像 page beside the map), 関心地域・地点,
「類似画像の検索…」 of the layers opened from the catalog, and 「この地点の新着画像を監視…」.
"""
from __future__ import annotations

from qgis.core import QgsMapLayerType, QgsProject
from qgis.PyQt.QtCore import QRectF, Qt
from qgis.PyQt.QtGui import QColor, QIcon, QPainter

try:  # Qt6
    from qgis.PyQt.QtGui import QAction
except ImportError:  # Qt5
    from qgis.PyQt.QtWidgets import QAction

from ..qgis_io.catalog_io import FEATURE
from ..qgis_io.interests_io import InterestBook
from ..ui.catalog_dock import CatalogDock
from ..ui.central_views import CentralViews
from ..ui.docks import plain_tip
from ..ui.interests_dock import InterestsDock
from ..ui.notice_feed import NoticeFeed
from ..ui.notices_dock import NoticesDock
from .base import DockSpec, Feature, disconnect, icon, tr

#: The 新着画像 page's key beside the map (ui/central_views.py).
NOTICE_FEED = "notices"


def badged(base: QIcon, n: int) -> QIcon:
    """The icon with the number of unread notices in a red circle (the icon as it is for none)."""
    if n <= 0:
        return base
    pixmap = base.pixmap(64, 64)
    painter = QPainter(pixmap)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    painter.setPen(QColor("#ffffff"))
    painter.setBrush(QColor("#dc2626"))
    painter.drawEllipse(QRectF(26, 0, 38, 38))
    font = painter.font()
    font.setBold(True)
    font.setPixelSize(26 if n < 10 else 20)
    painter.setFont(font)
    painter.drawText(QRectF(26, 0, 38, 38), int(Qt.AlignmentFlag.AlignCenter), str(n) if n < 100 else "99+")
    painter.end()
    return QIcon(pixmap)


class Images(Feature):
    COMMANDS = ("show_notice_feed", "toggle_notice_feed")

    def __init__(self, host):
        super().__init__(host)
        #: What the plugin shows of this feature: the 通知 dock's menu entry (its text carries the unread count),
        #: the pages beside the map, the 新着画像 page, the person's 関心地域・地点 and 「類似画像の検索…」.
        host.notices_action = None
        host.central = None
        host.notice_feed = None
        host.interests = None
        host.similar_action = None
        # Its ports: 画像カタログ (to open and list images) and 通知 (the imaging plan's reminders).
        host.ports.catalog = lambda: host.docks.get("SatObsImageCatalog")
        host.ports.notices = lambda: host.docks.get("SatObsNotices")

    def start(self) -> None:
        # 個人の関心地域・地点: the person's own items, for their dock and the 新着画像 cards (qgis_io/interests_io.py).
        self.host.interests = InterestBook(self.context, self.window())

    def dock_specs(self) -> dict:
        iface, context, settings, window = self.iface, self.context, self.host.open_settings, self.window()
        return {
            "SatObsImageCatalog": DockSpec("画像カタログ", "catalog", lambda: CatalogDock(iface, context, settings, window)),
            "SatObsNotices": DockSpec("通知", "notices", lambda: NoticesDock(iface, context, settings, window)),
            "SatObsInterests": DockSpec("関心地域・地点", "interests",
                                        lambda: InterestsDock(iface, context, settings, self.host.interests, window)),
        }

    def connect(self) -> None:
        """The image catalog with the notices, the imaging plan's reminders and the layers' right-click menu."""
        host, docks = self.host, self.docks
        catalog, notices = docks["SatObsImageCatalog"], docks["SatObsNotices"]
        # 「イベントに追加」 of the images goes to 「イベント」; 「ファイルなしで記録」 to 「観測記録」.
        catalog.event_linker = self.ports.events
        catalog.observations = notices.observations = self.observations_if_allowed()
        catalog.watch_requested = notices.watch_conditions
        notices.drawn_area = catalog.drawn_shape
        notices.choose_folder = catalog.choose_folder
        notices.choose_images = catalog.choose_images
        host.notices_action = notices.toggleViewAction()

        bell = host.notices_action.icon()

        def unread(n: int) -> None:
            host.notices_action.setText(tr("通知") + (f"（{n}）" if n else ""))
            host.notices_action.setIcon(badged(bell, n))
            if host.central is not None:
                host.central.set_title(NOTICE_FEED, tr("新着画像") + (f"（{n}）" if n else ""))

        # 「新着画像」 (tsudo, 2026-11-14): the notices as cards on a large page in the place of the map, switched
        # with it by tabs over the map area (ui/central_views.py, ui/notice_feed.py).
        host.central = CentralViews(self.iface)
        host.notice_feed = NoticeFeed(self.iface, self.context, notices)
        host.notice_feed.show_map = host.central.show_map
        host.notice_feed.set_interests(host.interests)
        host.central.add_view(NOTICE_FEED, tr("新着画像"), icon("notice_feed"), host.notice_feed)
        notices.show_feed = self.show_notice_feed
        notices.unread_changed = unread
        # 通知 ▸ 通報: reports polled from the log table are highlighted in 観測記録 and open there.
        observations = self.ports.observations()
        feed = notices.alerts
        feed.on_reported = observations.mark_reported
        feed.on_removed = observations.refresh_reported

        def open_reported(record_id: str) -> None:
            if not observations.with_records([record_id], lambda: open_read(record_id)):
                return  # opened once read (asked of a shared store, or no image chosen in 「観測記録」 yet)
            open_read(record_id)

        def open_read(record_id: str) -> None:
            o = observations.book.record(record_id) if observations.book else None
            if o is None:
                self.iface.messageBar().pushWarning(tr("通報"), plain_tip(tr("観測記録が見つからない（別の保存先か削除済み）: {i}").format(i=record_id)))
                return
            observations.show()
            observations.raise_()
            observations.open_record(o)

        feed.open_record = open_reported

        def open_log() -> None:
            dialog = observations.open_alerts(1)
            if dialog.log_button.isEnabled():
                dialog.read_log()

        feed.open_log = open_log
        # 「類似画像の検索…」 in the right-click menu of layers opened from the catalog.
        host.similar_action = QAction(icon("catalog"), tr("類似画像の検索…"), self.window())
        host.similar_action.triggered.connect(lambda: catalog.find_similar_of_layer(self.iface.activeLayer()))
        self.iface.addCustomActionForLayerType(host.similar_action, "", QgsMapLayerType.RasterLayer, False)
        QgsProject.instance().layersAdded.connect(self._layers_added)
        self._layers_added(QgsProject.instance().mapLayers().values())

    def add_actions(self) -> None:
        feed_action = self.entry("notice_feed", "新着画像（大きな画面）", "toggle_notice_feed", name="SatObsNoticeFeed")
        feed_action.setToolTip(tr("新着画像をカードで並べた大きな画面と地図を切り替え（地図の上のタブでも）"))

    def map_menu(self) -> dict:
        return {"この地点の新着画像を監視…": lambda x, y: self.docks["SatObsNotices"].watch_point(x, y)}

    def _layers_added(self, layers) -> None:
        for layer in layers:
            if layer.customProperty(FEATURE):
                self.iface.addCustomActionForLayer(self.host.similar_action, layer)

    def show_notice_feed(self) -> None:
        """The 新着画像 page in front (opened when it was not); the 通知 dock where QGIS's map area can't hold it."""
        central = self.host.central
        if central is not None and central.open(NOTICE_FEED):
            return
        dock = self.docks.get("SatObsNotices")
        if dock is not None:
            dock.show()
            dock.raise_()

    def toggle_notice_feed(self) -> None:
        """The 新着画像 page and the map in turn (the menu, toolbar and Ctrl+Alt+N)."""
        central = self.host.central
        if central is not None and central.available:
            central.toggle(NOTICE_FEED)
        else:
            self.show_notice_feed()

    def unload(self) -> None:
        host = self.host
        if host.similar_action is not None:
            disconnect(QgsProject.instance().layersAdded, self._layers_added)
            self.iface.removeCustomActionForLayerType(host.similar_action)
            host.similar_action = None
        if host.notice_feed is not None:
            host.notice_feed.close_down()
        if host.central is not None:
            host.central.unload()
            host.central = None
        if host.notice_feed is not None:
            host.notice_feed.setParent(None)
            host.notice_feed.deleteLater()
            host.notice_feed = None

    def unload_after_docks(self) -> None:
        if self.host.interests is not None:
            self.host.interests.deleteLater()
            self.host.interests = None

