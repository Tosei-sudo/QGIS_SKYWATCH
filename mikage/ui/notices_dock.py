"""
通知: the new images the watches found (and the imaging plan's reminders),
newest first, with 開く・移動・既読・削除, and the watches themselves (on or
off, renamed, their area changed, deleted, when last checked and why a check failed). The
watches are checked when QGIS starts and every `watchInterval` minutes, in
a task; a check finding new images says so in the message bar. The tab
通報 lists the 通報 log table, polled while the dock is shown (alert_feed).
"""
from __future__ import annotations

import time
from dataclasses import replace
from typing import Callable, Optional

from qgis.core import Qgis, QgsRectangle
from qgis.PyQt.QtCore import Qt, QTimer
from qgis.PyQt.QtGui import QFont
from qgis.PyQt.QtWidgets import (
    QAbstractItemView, QDialog, QHBoxLayout, QInputDialog, QLabel, QPushButton, QTabWidget, QTreeWidget, QTreeWidgetItem,
    QVBoxLayout, QWidget,
)

from ..core.catalog.config import CatalogConfig
from ..core.catalog.paths import PathError
from ..core.catalog.query import CatalogSearch, time_text
from ..core.catalog.search import listed_image, read_catalog_layer, record_of
from ..core.catalog.watch import (
    MAX_SEEN, CatalogWatch, WatchNotice, add_areas, areas_of, check_watch, conditions_text, new_watch, replace_areas,
)
from ..core.catalog.watch_store import WatchStore, check_due
from ..qgis_io.overviews_io import queue as overview_queue
from ..qgis_io.catalog_io import Cancelled, add_record_layers, footprint, open_record
from ..qgis_io.geometry import from_wgs84
from ..qgis_io.tasks import run_task
from .alert_feed import AlertFeed
from .docks import PluginDock, ask_no, plain_label, plain_tip
from .list_views import column_menus
from .watch_dialog import PointWatchDialog, WatchAreaDialog, WatchDialog

TITLE = "通知"
PLAN = "plan"


def _now() -> float:
    return time.time() * 1000


class NoticesDock(PluginDock):
    def __init__(self, iface, context, open_settings: Callable[[], None], parent=None):
        super().__init__("SatObsNotices", TITLE, context, open_settings, parent)
        self.iface = iface
        self.canvas = iface.mapCanvas()
        self._store: Optional[WatchStore] = None
        self._checking = False
        #: A check asked for while one runs (None: none; else whether forced), run when that one ends.
        self._again: Optional[bool] = None
        self._closed = False
        #: Told the number of unread notices whenever it may have changed (the plugin puts it on the toolbar button).
        self.unread_changed: Optional[Callable[[int], None]] = None
        #: Opens an image of a notice with the image catalog dock's folder dialog (set by the plugin).
        self.choose_folder = None
        #: Which of a record's images to open (the image catalog dock's); all without it.
        self.choose_images = None
        #: The area drawn in the image catalog dock (its ring, WGS 84; set by the plugin), offered by 「範囲の変更…」.
        self.drawn_area: Optional[Callable[[], Optional[list]]] = None
        #: Told the notices (newest first) whenever the lists are read again (the 新着画像 page beside the map).
        self.refreshed: list[Callable[[list], None]] = []
        #: Shows the 新着画像 page (set by the plugin): the message bar's 「見る」 when new images came.
        self.show_feed: Optional[Callable[[], None]] = None
        self._tasks: list = []

        tabs = QTabWidget()
        self.body.addWidget(tabs, 1)

        page = QWidget()
        v = QVBoxLayout(page)
        self.notices = QTreeWidget()
        self.notices.setHeaderLabels([self.tr("発見"), self.tr("監視"), self.tr("画像ID"), self.tr("撮像日時"), self.tr("センサー")])
        self.notices.setRootIsDecorated(False)
        self.notices.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self.notices.itemDoubleClicked.connect(lambda item, column: self.open_notices([item.data(0, Qt.ItemDataRole.UserRole)]))
        v.addWidget(self.notices, 1)
        # The columns shown, hidden and in order from the header's right-click menu, kept per user.
        self.notices_columns = column_menus("notices", self.notices, self)
        row = QHBoxLayout()
        for text, tip, slot in [
                (self.tr("開く"), self.tr("選んだ新着画像をレイヤーとして開く（ダブルクリックでも）。画像ファイルなしのカタログの画像は、ファイルなしで観測記録へ"), lambda: self.open_notices(self.chosen_notices())),
                (self.tr("移動"), self.tr("選んだ新着画像の範囲へ地図を移動"), self.zoom_to_chosen),
                (self.tr("既読"), self.tr("選んだ新着画像を既読に"), self.mark_chosen_read),
                (self.tr("すべて既読"), self.tr("一覧の新着画像をすべて既読に"), lambda: self._mark(None)),
                (self.tr("削除"), self.tr("選んだ新着画像を一覧から消す（画像そのものは消えない）"), self.delete_chosen_notices)]:
            button = QPushButton(text)
            button.setToolTip(tip)
            button.clicked.connect(slot)
            row.addWidget(button)
        v.addLayout(row)
        tabs.addTab(page, self.tr("新着画像"))

        page = QWidget()
        v = QVBoxLayout(page)
        self.watches = QTreeWidget()
        self.watches.setHeaderLabels([self.tr("名前"), self.tr("条件"), self.tr("最終確認"), self.tr("失敗の理由")])
        self.watches.setRootIsDecorated(False)
        self.watches.itemChanged.connect(self._watch_toggled)
        v.addWidget(self.watches, 1)
        self.watches_columns = column_menus("notice-watches", self.watches, self)
        row = QHBoxLayout()
        for text, tip, slot in [
                (self.tr("名前の変更"), "", self.rename_chosen),
                (self.tr("範囲の変更…"), self.tr("選んだ監視の範囲を、レイヤーの今の形（取り直し）・表示範囲などで置き換える。新しい範囲に今ある画像は通知しない"),
                 self.change_chosen_area),
                (self.tr("削除"), "", self.delete_chosen_watch),
                (self.tr("今すぐ確認"), "", lambda: self.check(force=True))]:
            button = QPushButton(text)
            if tip:
                button.setToolTip(tip)
            button.clicked.connect(slot)
            row.addWidget(button)
        v.addLayout(row)
        self.status = plain_label(QLabel())
        self.status.setWordWrap(True)
        v.addWidget(self.status)
        tabs.addTab(page, self.tr("監視条件"))

        # 通報: the 通報 log table, read again every alertReports.live.seconds while the dock is shown.
        self.alerts = AlertFeed(context)
        self.alerts_tab = tabs.addTab(self.alerts, self.tr("通報"))
        self.tabs = tabs
        self.alerts.unseen_changed.connect(self._alerts_unseen)
        tabs.currentChanged.connect(lambda i: self.alerts.seen() if i == self.alerts_tab else None)
        self.visibilityChanged.connect(self._shown_changed)

        self.timer = QTimer(self)
        self.timer.timeout.connect(lambda: self.check())
        context.configChanged.connect(self._config_changed)

    # --- Store ------------------------------------------------------------------------------------

    def store(self) -> WatchStore:
        if self._store is None:
            self._store = WatchStore(self.context.gpkg())
        return self._store

    def showEvent(self, event) -> None:  # noqa: N802 (Qt's name)
        super().showEvent(event)
        self._shown_changed(True)

    def hideEvent(self, event) -> None:  # noqa: N802 (Qt's name)
        super().hideEvent(event)
        self._shown_changed(False)

    def _shown_changed(self, shown: bool) -> None:
        if shown == self.alerts.active:
            return
        self.alerts.set_active(shown and not self._closed)
        if shown and self.tabs.currentIndex() == self.alerts_tab:
            self.alerts.seen()

    def _alerts_unseen(self, n: int) -> None:
        if n and self.isVisible() and self.tabs.currentIndex() == self.alerts_tab:
            # Looked at as they come in: not counted.
            self.alerts.unseen = set()
            n = 0
        self.tabs.setTabText(self.alerts_tab, self.tr("通報") + (f"（{n}）" if n else ""))

    def _config_changed(self) -> None:
        self.alerts.reset()
        minutes = self.context.config.watch_interval
        self.timer.start(int(minutes * 60 * 1000))
        self.refresh()
        # At start (and after the settings change): the watches due are checked.
        QTimer.singleShot(0, lambda: self.check())

    def _start(self, description: str, work, done, failed=None) -> None:
        task = run_task(description, work, done, failed or (lambda m: self.status.setText(self.tr("{what}に失敗: {m}").format(what=description, m=m))))
        self._tasks.append(task)
        task.taskCompleted.connect(lambda: self._tasks.remove(task) if task in self._tasks else None)
        task.taskTerminated.connect(lambda: self._tasks.remove(task) if task in self._tasks else None)

    def catalog_for(self, watch: CatalogWatch) -> Optional[CatalogConfig]:
        """The settings file's catalog of a watch (same URL): its token and field names as they are now."""
        return next((c for c in self.context.config.image_catalogs if c.url == watch.catalog.url), None)

    def _ask_for(self, client):
        def ask_for(watch: CatalogWatch):
            current = self.catalog_for(watch)
            catalog = current or watch.catalog
            token = self.context.token_for(catalog.url, catalog.token)
            return replace(watch, catalog=catalog), lambda url, params: client.ask(url, params, token)

        return ask_for

    # --- Checking ---------------------------------------------------------------------------------

    def check(self, force: bool = False) -> None:
        """
        Checks the watches due (every enabled one when `force`) in a task. Asked
        while a check runs, it runs once that one ends (a watch added or the
        catalog changed meanwhile is not left until the next round).
        """
        if self._checking:
            self._again = bool(self._again) or force
            return
        if self.context.loading or self._closed:
            return
        try:
            store = self.store()
        except Exception as error:  # noqa: BLE001 (no GeoPackage: nothing to check)
            self.status.setText(str(error))
            return
        if not any(w.enabled for w in store.watches()):
            return
        self._checking = True
        client = self.context.esri()
        ask_for = self._ask_for(client)
        interval = self.context.config.watch_interval * 60 * 1000

        def work(task):
            return check_due(store, ask_for, interval, force, cancelled=task.isCanceled)

        def done(added: int) -> None:
            self._checking = False
            self.refresh()
            if added:
                self._say_new(added)
            self.status.setText(self.tr("確認済み（{t}）").format(t=time_text(_now())))
            self._check_again()

        def failed(message: str) -> None:
            self._checking = False
            self.refresh()
            self.status.setText(self.tr("新着画像の確認に失敗: {m}").format(m=message))
            self._check_again()

        self._start(self.tr("新着画像の確認"), work, done, failed)

    def _say_new(self, n: int) -> None:
        """「新着画像 n 件」 in the message bar, with 「見る」 (the 新着画像 page) when the plugin has it."""
        bar = self.iface.messageBar()
        text = self.tr("新着画像 {n} 件").format(n=n)
        if self.show_feed is None:
            bar.pushInfo(TITLE, text)
            return
        widget = bar.createMessage(TITLE, text)
        button = QPushButton(self.tr("見る"))
        button.setToolTip(self.tr("新着画像を地図の場所の大きな画面（カード）で見る"))

        def look() -> None:
            self.show_feed()
            try:
                bar.popWidget(widget)
            except RuntimeError:  # closed meanwhile
                pass

        button.clicked.connect(look)
        widget.layout().addWidget(button)
        bar.pushWidget(widget, Qgis.MessageLevel.Info, 10)

    def _check_again(self) -> None:
        again, self._again = self._again, None
        if again is not None:
            self.check(force=again)

    # --- Lists ------------------------------------------------------------------------------------

    def refresh(self) -> None:
        try:
            store = self.store()
            notices, watches = store.notices(), store.watches()
        except Exception as error:  # noqa: BLE001
            self.status.setText(str(error))
            return
        self.notices.clear()
        bold = QFont()
        bold.setBold(True)
        for n in notices:
            item = QTreeWidgetItem([time_text(n.found), n.watch_name, n.image_id, time_text(n.acquired), n.sensor])
            item.setData(0, Qt.ItemDataRole.UserRole, n)
            if not n.read:
                for c in range(5):
                    item.setFont(c, bold)
            self.notices.addTopLevelItem(item)
        self.watches.blockSignals(True)
        self.watches.clear()
        for w in watches:
            last = time_text(w.last_checked) if w.last_checked is not None else self.tr("未確認")
            item = QTreeWidgetItem([w.name, conditions_text(w.catalog, w.conditions, w.areas, area_layer=w.area_layer), last, w.last_error or ""])
            item.setData(0, Qt.ItemDataRole.UserRole, w.id)
            item.setFlags(item.flags() | Qt.ItemFlag.ItemIsUserCheckable)
            item.setCheckState(0, Qt.CheckState.Checked if w.enabled else Qt.CheckState.Unchecked)
            # Layer names and SQL conditions shown as they are (a tooltip reads text that looks like HTML as HTML).
            item.setToolTip(1, plain_tip(item.text(1)))
            self.watches.addTopLevelItem(item)
        self.watches.blockSignals(False)
        for c in range(4):
            self.notices.resizeColumnToContents(c)
        if self.unread_changed:
            self.unread_changed(sum(1 for n in notices if not n.read))
        for told in list(self.refreshed):
            told(notices)

    def chosen_notices(self) -> list[WatchNotice]:
        return [i.data(0, Qt.ItemDataRole.UserRole) for i in self.notices.selectedItems()]

    def _mark(self, ids) -> None:
        self.store().mark_read(ids)
        self.refresh()

    def mark_chosen_read(self) -> None:
        ids = [n.id for n in self.chosen_notices()]
        if ids:
            self._mark(ids)

    def delete_chosen_notices(self) -> None:
        ids = [n.id for n in self.chosen_notices()]
        if ids:
            self.store().delete_notices(ids)
            self.refresh()

    def zoom_to_chosen(self) -> None:
        if self.zoom_to_notices(self.chosen_notices()):
            self.mark_chosen_read()

    def zoom_to_notices(self, notices) -> bool:
        """The map moved to the footprints of `notices` (a margin around them); False when none has one."""
        box: Optional[QgsRectangle] = None
        for n in notices:
            g = footprint(n.feature.get("geometry"))
            if g is None:
                continue
            if box is None:
                box = QgsRectangle(g.boundingBox())
            else:
                box.combineExtentWith(g.boundingBox())
        if box is None:
            return False
        extent = from_wgs84(self.canvas.mapSettings().destinationCrs()).transformBoundingBox(box.buffered(max(box.width(), box.height()) * 0.2 + 0.001))
        self.canvas.setExtent(extent)
        self.canvas.refresh()
        return True

    #: The 「観測記録」 dock (plugin.py sets it): the images of a catalog without image files go there.
    observations: Optional[Callable[[], object]] = None

    def open_notices(self, notices) -> list:
        """
        Opens the images of notices as raster layers (the imaging plan's
        notices are moved to); those of a catalog without image files
        (`imageFiles: false`) go to 観測記録 without their files.
        """
        layers, problems, without = [], [], []
        for n in notices:
            if n.kind == PLAN:
                continue
            watch = self.store().watch(n.watch_id)
            catalog = (self.catalog_for(watch) if watch else None) or next(
                (c for c in self.context.config.image_catalogs if c.label == n.catalog_label), None)
            if catalog is None:
                problems.append(self.tr("{id} のカタログが設定ファイルになし").format(id=n.image_id))
                continue
            record = record_of(n.feature, catalog.fields)
            if not catalog.image_files:
                without.append(record)
                continue
            try:
                opened = open_record(record, catalog, self.context.config.path_mappings, self.choose_folder or (lambda m: None),
                                     self.choose_images, problems,
                                     names=self.context.known_names())
            except Cancelled:
                break
            except PathError as error:
                problems.append(str(error))
                continue
            add_record_layers(opened, record)
            overview_queue.request(opened, self.context.config.overviews, self.iface.messageBar())
            layers += opened
        if without:
            listed = [i for i in (listed_image(r) for r in without) if i is not None]
            dock = self.observations() if self.observations else None
            if not listed or dock is None or dock.list_catalog_images(listed) is None:
                problems.append(self.tr("画像ファイルなしのカタログの画像 {n} 件を観測記録へ追加できず（画像 ID か記録の保存先なし）").format(n=len(without)))
        if any(n.kind == PLAN for n in notices):
            self.zoom_to_notices([n for n in notices if n.kind == PLAN])
        self._mark([n.id for n in notices])
        if problems:
            self.iface.messageBar().pushWarning(TITLE, plain_tip(" / ".join(problems)))
        return layers

    def _chosen_watch_id(self) -> Optional[str]:
        item = self.watches.currentItem()
        return item.data(0, Qt.ItemDataRole.UserRole) if item else None

    def _watch_toggled(self, item: QTreeWidgetItem, column: int) -> None:
        watch = self.store().watch(item.data(0, Qt.ItemDataRole.UserRole))
        if watch is None:
            return
        on = item.checkState(0) == Qt.CheckState.Checked
        if on != watch.enabled:
            self.store().put_watch(replace(watch, enabled=on))

    def rename_chosen(self) -> None:
        watch = self.store().watch(self._chosen_watch_id() or "")
        if watch is None:
            return
        name, ok = QInputDialog.getText(self, self.tr("名前の変更"), self.tr("監視の名前"), text=watch.name)
        if ok and name.strip():
            self.store().put_watch(replace(watch, name=name.strip()))
            self.refresh()

    def change_chosen_area(self) -> None:
        """
        「範囲の変更…」: the chosen watch over another area (a layer's shape as
        it is now, the map's extent, …). The images already over it are noted
        first, in a task; the area stays as it was when the catalog cannot be asked.
        """
        watch = self.store().watch(self._chosen_watch_id() or "")
        if watch is None:
            self.status.setText(self.tr("範囲を変える監視を一覧で選択"))
            return
        dialog = WatchAreaDialog(watch, self.canvas, self.drawn_area() if self.drawn_area else None, self)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        areas, origin = dialog.chosen()
        client = self.context.esri()
        asked, ask = self._ask_for(client)(watch)
        store = self.store()

        def work(task):
            changed = replace_areas(asked, areas, ask, area_layer=origin)
            current = store.watch(watch.id)
            if current is None:
                return False
            # A check may have run meanwhile: what it saw is kept too.
            store.put_watch(replace(current, areas=changed.areas, area_layer=changed.area_layer,
                                    seen=list(dict.fromkeys(current.seen + changed.seen))[-MAX_SEEN:]))
            return True

        def done(kept) -> None:
            self.refresh()
            if kept:
                self.status.setText(self.tr("監視「{name}」の範囲を変更").format(name=watch.name))

        self._start(self.tr("監視の範囲の変更"), work, done)

    def delete_chosen_watch(self) -> None:
        watch_id = self._chosen_watch_id()
        watch = self.store().watch(watch_id or "")
        if watch is None:
            return
        if ask_no(self, self.tr("監視の削除"),
                  self.tr("監視「{name}」を削除しますか？条件とこの監視の新着画像の一覧も消え、元に戻せません（画像そのものは消えません）。").format(name=watch.name)):
            self.store().delete_watch(watch_id)
            self.refresh()

    # --- New watches ------------------------------------------------------------------------------

    def add_watch(self, catalog: CatalogConfig, types: dict, conditions: CatalogSearch, areas: list, name: str,
                  area_layer: Optional[dict] = None) -> CatalogWatch:
        """Keeps a new watch (`area_layer`: the layer its areas were copied from) and checks it once (noting the images already there) in a task."""
        watch = new_watch(name, CatalogConfig.from_json(catalog.to_json()), types, conditions, areas, area_layer=area_layer)
        self.store().put_watch(watch)
        self.refresh()
        client = self.context.esri()
        ask_for = self._ask_for(client)
        store = self.store()

        def work(task):
            asked, ask = ask_for(watch)
            result = check_watch(asked, ask)
            current = store.watch(watch.id)
            if current is not None:
                store.put_watch(replace(result.watch, catalog=watch.catalog, name=current.name, enabled=current.enabled))
            return result.watch.last_error

        def done(error) -> None:
            self.refresh()
            self.status.setText(self.tr("監視「{name}」の初回の確認に失敗: {m}").format(name=watch.name, m=error) if error
                                else self.tr("監視「{name}」を追加").format(name=watch.name))

        self._start(self.tr("新着画像の監視の追加"), work, done)
        return watch

    def watch_conditions(self, catalog: CatalogConfig, types: dict, conditions: CatalogSearch, drawn: Optional[list] = None,
                         area: Optional[str] = None, layer=None, selected_only: Optional[bool] = None) -> None:
        """
        「この条件で監視…」 of the image catalog: `drawn` the area drawn there,
        `area` the kind of area searched (offered first; the area drawn when
        not told), `layer` / `selected_only` the layer searched over (the
        active layer and its chosen features otherwise).
        """
        if layer is None:
            layer, selected_only = self.iface.activeLayer(), None
        dialog = WatchDialog(catalog, conditions, self.canvas, layer, self, drawn, area, selected_only)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        areas, origin = dialog.chosen()
        self.add_watch(catalog, types, conditions, areas, dialog.name.text().strip() or catalog.label, area_layer=origin)
        self.show()
        self.raise_()

    def watch_point(self, lon: float, lat: float) -> None:
        """「この地点の新着画像を監視…」 of the map's right-click menu."""
        catalogs = list(self.context.config.image_catalogs)
        dialog = PointWatchDialog(lon, lat, catalogs, self.store().watches(), self)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        area = areas_of([[lon, lat]], [])
        watch_id = dialog.chosen_watch()
        self.show()
        self.raise_()
        if watch_id:
            watch = self.store().watch(watch_id)
            if watch is None:
                return
            client = self.context.esri()
            asked, ask = self._ask_for(client)(watch)
            store = self.store()

            def work(task):
                more = add_areas(asked, area, ask)
                current = store.watch(watch.id) or watch
                store.put_watch(replace(current, areas=more.areas, seen=more.seen))

            self._start(self.tr("監視への地点の追加"), work,
                        lambda r: (self.refresh(), self.status.setText(self.tr("監視「{name}」に地点を追加").format(name=watch.name))))
            return
        catalog = dialog.chosen_catalog()
        if catalog is None:
            return
        name = dialog.name.text().strip()
        client = self.context.esri()
        token = self.context.token_for(catalog.url, catalog.token)
        self._start(self.tr("画像カタログの確認"), lambda task: read_catalog_layer(client, catalog, token),
                    lambda info: self.add_watch(catalog, info.types, CatalogSearch(), area, name))

    # --- The imaging plan's reminders -------------------------------------------------------------

    def add_plan_notice(self, reminder, text: str, now: Optional[float] = None) -> None:
        """A reminder of the imaging plan as a notice (「撮像計画」)."""
        now = _now() if now is None else now
        self.store().add_notices([WatchNotice(
            id=f"{PLAN}|{reminder.id}", watch_id=PLAN, watch_name=self.tr("撮像計画"), catalog_label="", found=now, read=False,
            image_id=text, acquired=reminder.time, sensor=reminder.target,
            feature={"attributes": {}, "geometry": {"rings": reminder.rings} if reminder.rings else None}, kind=PLAN,
        )])
        self.refresh()

    def close_down(self) -> None:
        # Tasks left running (a request waiting on the network) stop with the dock.
        for task in list(self._tasks):
            task.cancel()
        self.timer.stop()
        self.alerts.close_down()
        # A check still waiting to start (the plugin unloaded right after showing the dock) finds the store closed.
        self._closed = True
