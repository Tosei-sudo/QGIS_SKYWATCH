"""
The 「保存した計画」 dock (the viewer's imaging-plan-saved.ts): the imaging
plans kept in the Esri feature services of `planStores`. It lists the plans
with how far each has got, and a chosen plan's shots on the map and in a
table, coloured by status. 「撮像を確認」 asks the image catalog whether the
shots whose moment has passed were taken (also done by itself when a plan is
opened); a shot's status can be set by hand, its image opened, and a whole
plan deleted. Every request runs in a QgsTask.
"""
from __future__ import annotations

import math
import time
from typing import Callable, Optional

from qgis.core import QgsProject, QgsRasterLayer, QgsRectangle
from qgis.PyQt.QtCore import QCoreApplication, Qt, QTimer
from qgis.PyQt.QtGui import QBrush, QColor, QStandardItem, QStandardItemModel
from qgis.PyQt.QtWidgets import (
    QAbstractItemView, QComboBox, QHBoxLayout, QHeaderView, QLabel, QPushButton, QSplitter, QTableView, QVBoxLayout, QWidget,
)

from ..core.catalog import CatalogConfig
from ..core.esri import quote
from ..core.plan.output import js_round, local_text
from ..core.plan.store import (
    STATUS_COLORS, STATUSES, PlanStoreConfig, SavedPlan, SavedShot, ShotCheck, delete_plan, list_plans, load_shots,
    progress_text, track_shots, update_shots,
)
from ..qgis_io.geometry import from_wgs84
from ..qgis_io.tasks import run_task
from .docks import PluginDock
from .plan_overlay import PlanOverlay


def _now_ms() -> float:
    return time.time() * 1000


def _table(model: QStandardItemModel) -> QTableView:
    table = QTableView()
    table.setModel(model)
    table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
    table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
    table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
    table.verticalHeader().hide()
    table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.ResizeToContents)
    return table


class SavedPlansDock(PluginDock):
    """The plans saved to the plan stores, and how their shots went."""

    def __init__(self, iface, context, open_settings, parent=None):
        super().__init__("SatObsSavedPlans", QCoreApplication.translate("SavedPlansDock", "保存した撮像計画"), context, open_settings, parent)
        self.iface = iface
        self.canvas = iface.mapCanvas()
        self.overlay = PlanOverlay(self.canvas)
        self.store: Optional[PlanStoreConfig] = None
        self.plans: list[SavedPlan] = []
        self.plan: Optional[SavedPlan] = None
        self.shots: list[SavedShot] = []
        self.chosen: Optional[SavedShot] = None
        self.task = None
        self._build()
        context.configChanged.connect(self._config_changed)
        self.visibilityChanged.connect(self._visibility)
        self._config_changed()

    def _build(self) -> None:
        top = QHBoxLayout()
        self.store_choice = QComboBox()
        self.store_choice.setToolTip(self.tr("保存先"))
        self.store_choice.currentIndexChanged.connect(self._store_chosen)
        top.addWidget(self.store_choice, 1)
        self.body.addLayout(top)
        self.status = QLabel()
        self.status.setWordWrap(True)
        self.status.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        self.body.addWidget(self.status)

        self.plan_model = QStandardItemModel(self)
        self.plan_table = _table(self.plan_model)
        self.plan_table.selectionModel().currentRowChanged.connect(lambda current, _: self._plan_row(current.row()))
        self.shot_model = QStandardItemModel(self)
        self.shot_table = _table(self.shot_model)
        self.shot_table.selectionModel().currentRowChanged.connect(lambda current, _: self._shot_row(current.row()))
        self.shot_table.doubleClicked.connect(lambda _: self.zoom())
        splitter = QSplitter(Qt.Orientation.Vertical)
        splitter.addWidget(self.plan_table)
        shots = QWidget()
        shots_layout = QVBoxLayout(shots)
        shots_layout.setContentsMargins(0, 0, 0, 0)
        shots_layout.addWidget(self.shot_table, 1)
        shot_actions = QHBoxLayout()
        self.status_choice = QComboBox()
        for s in STATUSES:
            self.status_choice.addItem(s)
        self.status_choice.setToolTip(self.tr("選んだ撮像の状態"))
        self.status_choice.activated.connect(lambda i: self.set_status(STATUSES[i]))
        self.image_button = QPushButton(self.tr("画像を開く"))
        self.image_button.setToolTip(self.tr("撮れた画像を画像カタログから探してレイヤーとして追加"))
        self.image_button.clicked.connect(lambda: self.open_image())
        shot_actions.addWidget(QLabel(self.tr("状態")))
        shot_actions.addWidget(self.status_choice)
        shot_actions.addWidget(self.image_button)
        shot_actions.addStretch(1)
        shots_layout.addLayout(shot_actions)
        splitter.addWidget(shots)
        splitter.setChildrenCollapsible(False)
        self.body.addWidget(splitter, 1)

        actions = QHBoxLayout()
        self.reload_button = QPushButton(self.tr("再読み込み"))
        self.reload_button.setToolTip(self.tr("保存先から計画を再読み込み"))
        self.reload_button.clicked.connect(lambda: self.reload())
        self.delete_button = QPushButton(self.tr("計画を削除"))
        self.delete_button.setToolTip(self.tr("選んだ計画の撮像をすべて保存先から削除"))
        self.delete_button.clicked.connect(self._delete_clicked)
        self._armed = False
        self.track_button = QPushButton(self.tr("撮像を確認"))
        self.track_button.setToolTip(self.tr("撮像時刻を過ぎた予定の撮像について、画像カタログへの画像の登録を確認"))
        self.track_button.clicked.connect(lambda: self.track(True))
        self.zoom_button = QPushButton(self.tr("範囲へ移動"))
        self.zoom_button.clicked.connect(self.zoom)
        for b in (self.reload_button, self.delete_button, self.track_button, self.zoom_button):
            actions.addWidget(b)
        self.body.addLayout(actions)
        self._update_buttons()

    # --- Stores and plans ---------------------------------------------------------------------

    def _config_changed(self) -> None:
        stores = self.context.config.plan_stores
        self.store_choice.blockSignals(True)
        self.store_choice.clear()
        for s in stores:
            self.store_choice.addItem(s.label)
        self.store_choice.blockSignals(False)
        self.store_choice.setVisible(len(stores) > 1)
        self.store = stores[0] if stores else None
        self.plans, self.plan, self.shots, self.chosen = [], None, [], None
        self._render_plans()
        self._render_shots()
        if self.isVisible():
            self.reload()

    def _store_chosen(self, index: int) -> None:
        stores = self.context.config.plan_stores
        self.store = stores[index] if 0 <= index < len(stores) else None
        self.plan, self.shots, self.chosen = None, [], None
        self.reload()

    def catalog(self) -> Optional[CatalogConfig]:
        """The catalog the store follows its shots up in (its `catalog`, or the first one)."""
        store, catalogs = self.store, self.context.config.image_catalogs
        if store is None:
            return None
        if store.catalog:
            return next((c for c in catalogs if c.label == store.catalog), None)
        return catalogs[0] if catalogs else None

    def _start(self, what: str, work, done, failed=None) -> bool:
        """Runs one request at a time."""
        if self.task is not None:
            return False

        def finish(result):
            self.task = None
            done(result)

        def fail(message):
            self.task = None
            if failed:
                failed(message)
            else:
                self.status.setText(message)

        self.task = run_task(what, work, finish, fail)
        return True

    def reload(self, then: Optional[Callable[[], None]] = None) -> None:
        """Reads the plans of the store again."""
        store = self.store
        if store is None:
            self.status.setText(self.tr("設定ファイルに計画の保存先（planStores）なし"))
            return
        self.status.setText(self.tr("{label} を読み込み中…").format(label=store.label))
        client = self.context.esri()
        token = self.context.token_for(store.url, store.token)

        def done(plans):
            if store is not self.store:
                return
            self.plans = plans
            chosen = self.plan.id if self.plan else None
            self.plan = next((p for p in plans if p.id == chosen), None)
            self._render_plans()
            self.status.setText(self.tr("{label}: {n} 件の計画。行の選択で撮像を地図に表示し、撮像を確認").format(label=store.label, n=len(plans)) if plans
                                else self.tr("{label} に保存した計画なし（撮像計画の「保存・追跡…」で保存）").format(label=store.label))
            if self.plan:
                self.choose(self.plan, False, then)
            elif then:
                then()

        self._start(self.tr("保存した計画の読み込み"), lambda task: list_plans(client, store, token, task.isCanceled), done,
                    lambda m: self.status.setText(self.tr("読み込み失敗: {m}").format(m=m)))

    def _render_plans(self) -> None:
        self.plan_model.clear()
        self.plan_model.setHorizontalHeaderLabels([self.tr("計画"), self.tr("撮像"), self.tr("期間"), self.tr("状況")])
        chosen_row = -1
        for i, p in enumerate(self.plans):
            span = f"{local_text(p.first)[:10]}〜{local_text(p.last)[5:10]}" if math.isfinite(p.first) else ""
            items = [QStandardItem(t) for t in (p.name, str(p.shots), span, progress_text(p.statuses))]
            if p.created:
                for item in items:
                    item.setToolTip(self.tr("保存 {t}").format(t=local_text(p.created)))
            self.plan_model.appendRow(items)
            if p is self.plan:
                chosen_row = i
        if chosen_row >= 0:
            self.plan_table.selectionModel().blockSignals(True)
            self.plan_table.selectRow(chosen_row)
            self.plan_table.selectionModel().blockSignals(False)
        self._update_buttons()

    def _plan_row(self, row: int) -> None:
        if 0 <= row < len(self.plans) and self.plans[row] is not self.plan:
            self.choose(self.plans[row])

    def choose(self, plan: SavedPlan, track: bool = True, then: Optional[Callable[[], None]] = None) -> None:
        """Shows a plan's shots, and follows them up when some are due (`track`)."""
        store = self.store
        if store is None:
            return
        self.plan, self.chosen = plan, None
        self._update_buttons()
        client = self.context.esri()
        token = self.context.token_for(store.url, store.token)

        def done(shots):
            if self.plan is not plan:
                return
            self.shots = shots
            self._render_shots()
            self.zoom()
            self.status.setText(self.tr("{name}: {n} 回・{p}").format(name=plan.name, n=len(shots), p=progress_text(self.counts())))
            if track and self.catalog() and any(s.status == "予定" and s.time <= _now_ms() for s in shots):
                self.track(False, then)
            elif then:
                then()

        if not self._start(self.tr("撮像の読み込み"), lambda task: load_shots(client, store, token, plan.id, task.isCanceled), done,
                           lambda m: self.status.setText(self.tr("撮像の読み込みに失敗: {m}").format(m=m))):
            # Another request is running: try again once it is through.
            QTimer.singleShot(200, lambda: self.plan is plan and self.choose(plan, track, then))

    def counts(self) -> dict:
        counts: dict = {}
        for s in self.shots:
            counts[s.status] = counts.get(s.status, 0) + 1
        return counts

    # --- Shots ----------------------------------------------------------------------------------

    def _render_shots(self) -> None:
        self.shot_model.clear()
        self.shot_model.setHorizontalHeaderLabels([self.tr("日時"), self.tr("衛星"), self.tr("対象"), self.tr("オフナディア角"), self.tr("状態"), self.tr("画像")])
        for s in self.shots:
            values = (local_text(s.time) if math.isfinite(s.time) else "", s.satellite, s.target,
                      "" if s.offNadir is None else f"{js_round(s.offNadir)}°", s.status, s.imageId)
            items = [QStandardItem(v) for v in values]
            items[4].setForeground(QBrush(QColor(STATUS_COLORS.get(s.status, "#2563eb"))))
            self.shot_model.appendRow(items)
        self.draw()
        self._update_buttons()

    def _shot_row(self, row: int) -> None:
        if 0 <= row < len(self.shots):
            self.chosen = self.shots[row]
            self.status_choice.setCurrentIndex(STATUSES.index(self.chosen.status))
            self.draw()
            self.zoom()
            self._update_buttons()

    def draw(self) -> None:
        """The shots on the map, coloured by status; the chosen one bold and named."""
        polygons = []
        for s in self.shots:
            if not s.rings:
                continue
            chosen = s is self.chosen
            label = f"{s.satellite} {local_text(s.time)[5:]}" if chosen else ""
            polygons.append((s.rings, STATUS_COLORS.get(s.status, "#2563eb"), 3 if chosen else 1, label))
        self.overlay.paint(polygons)
        self.canvas.refresh()

    def zoom(self) -> None:
        """Fits the map to the chosen shot, or the whole plan."""
        box: Optional[QgsRectangle] = None
        for s in self.shots:
            if self.chosen is not None and s is not self.chosen:
                continue
            for ring in s.rings:
                for x, y in ring:
                    if box is None:
                        box = QgsRectangle(x, y, x, y)
                    else:
                        box.combineExtentWith(x, y)
        if box is None:
            return
        box = box.buffered(max(box.width(), box.height()) * 0.1 + 0.01)
        self.canvas.setExtent(from_wgs84(self.canvas.mapSettings().destinationCrs()).transformBoundingBox(box))
        self.canvas.refresh()

    def _update_buttons(self) -> None:
        has = self.plan is not None
        self.delete_button.setEnabled(has)
        self.zoom_button.setEnabled(has)
        self.track_button.setEnabled(has and self.catalog() is not None)
        self.status_choice.setEnabled(self.chosen is not None)
        catalog = self.catalog()
        self.image_button.setEnabled(self.chosen is not None and bool(self.chosen.imageId) and catalog is not None and bool(catalog.fields.get("id")))

    # --- Following up, changing and deleting --------------------------------------------------

    def track(self, say: bool, then: Optional[Callable[[], None]] = None) -> None:
        """Asks the catalog about the shots that are due, and saves what it finds."""
        store, catalog, plan = self.store, self.catalog(), self.plan
        if store is None or plan is None:
            return
        if catalog is None:
            self.status.setText(self.tr("撮像を確認する画像カタログ（設定ファイルの imageCatalogs）なし"))
            return
        self.status.setText(self.tr("{label} で撮像を確認中…").format(label=catalog.label))
        client = self.context.esri()
        token = self.context.token_for(store.url, store.token)
        catalog_token = self.context.token_for(catalog.url, catalog.token)
        shots = list(self.shots)

        def work(task):
            changes = track_shots(client, store, catalog, catalog_token, shots, _now_ms(), task.isCanceled)
            update_shots(client, store, token, changes)
            return changes

        def done(changes: list[ShotCheck]):
            for c in changes:
                shot = next((s for s in self.shots if s.objectId == c.objectId), None)
                if shot:
                    shot.status = c.status
                    if c.imageId is not None:
                        shot.imageId = c.imageId
            plan.statuses = self.counts()
            self._render_plans()
            self._render_shots()
            taken = sum(1 for c in changes if c.status == "撮像済み")
            missed = len(changes) - taken
            text = (self.tr("新たに撮像済み {n} 回").format(n=taken) + (self.tr("・未撮像 {n} 回").format(n=missed) if missed else "")) if changes else self.tr("変わった撮像なし")
            self.status.setText(f"{plan.name}: {text}。{progress_text(plan.statuses)}")
            if say and changes:
                self.iface.messageBar().pushInfo(self.tr("保存した撮像計画"), f"{plan.name}: {text}")
            if then:
                then()

        self._start(self.tr("撮像の確認"), work, done, lambda m: self.status.setText(self.tr("撮像の確認に失敗: {m}").format(m=m)))

    def set_status(self, status: str, then: Optional[Callable[[], None]] = None) -> None:
        """Sets the chosen shot's status by hand."""
        store, shot, plan = self.store, self.chosen, self.plan
        if store is None or shot is None or plan is None or status == shot.status:
            return
        client = self.context.esri()
        token = self.context.token_for(store.url, store.token)

        def done(_n):
            shot.status = status
            plan.statuses = self.counts()
            self._render_plans()
            self._render_shots()
            row = self.shots.index(shot)
            self.shot_table.selectionModel().blockSignals(True)
            self.shot_table.selectRow(row)
            self.shot_table.selectionModel().blockSignals(False)
            if then:
                then()

        self._start(self.tr("撮像の状態の変更"), lambda task: update_shots(client, store, token, [ShotCheck(shot.objectId, status)]), done,
                    lambda m: self.status.setText(self.tr("保存失敗: {m}").format(m=m)))

    def _delete_clicked(self) -> None:
        """Two steps, so one click never deletes a plan."""
        if not self._armed:
            self._armed = True
            self.delete_button.setText(self.tr("もう一度押すと削除"))
            QTimer.singleShot(4000, self._disarm)
            return
        self._disarm()
        self.delete()

    def _disarm(self) -> None:
        self._armed = False
        self.delete_button.setText(self.tr("計画を削除"))

    def delete(self, then: Optional[Callable[[], None]] = None) -> None:
        store, plan = self.store, self.plan
        if store is None or plan is None:
            return
        client = self.context.esri()
        token = self.context.token_for(store.url, store.token)

        def done(_):
            self.plan, self.shots, self.chosen = None, [], None
            self._render_shots()
            self.iface.messageBar().pushInfo(self.tr("保存した撮像計画"), self.tr("計画「{name}」を削除").format(name=plan.name))
            self.reload(then)

        self._start(self.tr("計画の削除"), lambda task: delete_plan(client, store, token, plan.id), done,
                    lambda m: self.status.setText(self.tr("削除失敗: {m}").format(m=m)))

    def open_image(self, then: Optional[Callable[[Optional[QgsRasterLayer]], None]] = None) -> None:
        """Finds the chosen shot's image in the catalog and adds it as a raster layer (a COG over HTTP, or a local file)."""
        shot, catalog = self.chosen, self.catalog()
        if shot is None or not shot.imageId or catalog is None or not catalog.fields.get("id"):
            return
        client = self.context.esri()
        token = self.context.token_for(catalog.url, catalog.token)
        where = f"{catalog.fields['id']} = {quote(shot.imageId)}"

        def work(task):
            return client.ask(f"{catalog.url}/query", {"where": where, "outFields": "*", "returnGeometry": "false"}, token).get("features") or []

        def done(features):
            if not features:
                self.iface.messageBar().pushWarning(self.tr("保存した撮像計画"), self.tr("{id} が画像カタログになし").format(id=shot.imageId))
                return then(None) if then else None
            a = features[0].get("attributes") or {}
            source = next((str(a[n]) for n in catalog.fields.get("source") or [] if a.get(n)), "")
            if not source:
                self.iface.messageBar().pushWarning(self.tr("保存した撮像計画"), self.tr("{id} に画像の場所なし").format(id=shot.imageId))
                return then(None) if then else None
            path = f"/vsicurl/{source}" if source.lower().startswith(("http://", "https://")) else source
            layer = QgsRasterLayer(path, shot.imageId)
            if layer.isValid():
                QgsProject.instance().addMapLayer(layer)
            else:
                self.iface.messageBar().pushWarning(self.tr("保存した撮像計画"), self.tr("{id} の画像を開けず").format(id=shot.imageId))
                layer = None
            if then:
                then(layer)

        self._start(self.tr("画像の検索"), work, done, lambda m: self.status.setText(self.tr("画像の読み込みに失敗: {m}").format(m=m)))

    # --- Showing and hiding ---------------------------------------------------------------------

    def open(self, plan_id: Optional[str] = None) -> None:
        """Shows the dock (on the plan `plan_id` when given)."""
        self.show()
        self.raise_()

        def chosen():
            plan = next((p for p in self.plans if p.id == plan_id), None) if plan_id else None
            if plan is not None:
                self.choose(plan)

        self.reload(chosen)

    def _visibility(self, visible: bool) -> None:
        if visible:
            if not self.plans and self.task is None:
                self.reload()
            self.draw()
        else:
            self.overlay.clear()

    def close_down(self) -> None:
        if self.task is not None:
            self.task.cancel()
            self.task = None
        self.overlay.remove()
