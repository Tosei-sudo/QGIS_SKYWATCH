"""
The 「撮像依頼」 dock: the imaging request ledger (`requestStores`). It lists
the requests (filtered by category and status) and shows them on the map;
requests are added and changed in a form, 定点 requests taken in from a
layer or written out to one (docs/imaging-requests.md). The day's files are
written in a dock of its own, 「撮像計画ファイル出力」 (plan_file_dock.py). Every request to the service runs in a QgsTask.
"""
from __future__ import annotations

import math
import time
from typing import Callable, Optional

from qgis.core import QgsProject, QgsRectangle, QgsVectorLayer
from qgis.PyQt.QtCore import QCoreApplication, Qt, QTimer
from qgis.PyQt.QtGui import QBrush, QColor, QStandardItem, QStandardItemModel
from qgis.PyQt.QtWidgets import (
    QAbstractItemView, QComboBox, QDoubleSpinBox, QHBoxLayout, QHeaderView, QLabel, QMessageBox, QPushButton, QTableView,
)

from ..core.request.model import ImagingRequest, jst_day, priority_text
from ..core.request.rows import field_roles, request_of_row, row_problems
from ..core.request.store import RequestStoreConfig, delete_request, load_requests, new_request_id, save_request
from ..qgis_io.geometry import from_wgs84
from ..qgis_io.request_layers import layer_rows, requests_layer
from ..qgis_io.tasks import run_task
from .docks import PluginDock
from .plan_overlay import PlanOverlay

STATUS_COLORS = {"有効": "#2563eb", "停止": "#6b7280", "完了": "#16a34a"}


def _now_ms() -> float:
    return time.time() * 1000


def _period(r: ImagingRequest) -> str:
    if not (math.isfinite(r.start) and math.isfinite(r.end)):
        return ""
    a, b = jst_day(r.start), jst_day(r.end)
    return f"{a.isoformat()}〜{b.isoformat()[5:] if a.year == b.year else b.isoformat()}"


class RequestDock(PluginDock):
    """The imaging requests of the ledger."""

    def __init__(self, iface, context, open_settings, satellites: Callable, parent=None):
        super().__init__("SatObsRequests", QCoreApplication.translate("RequestDock", "撮像依頼"), context, open_settings, parent)
        self.iface = iface
        self.canvas = iface.mapCanvas()
        #: Calls back with the satellites to plan with (the imaging plan's catalog), once they are read.
        self.satellites = satellites
        self.overlay = PlanOverlay(self.canvas)
        self.store: Optional[RequestStoreConfig] = None
        self.requests: list[ImagingRequest] = []
        self.shown: list[ImagingRequest] = []
        self.chosen: Optional[ImagingRequest] = None
        self.task = None
        self.form = None
        self._loaded = False
        self._build()
        context.configChanged.connect(self._config_changed)
        self.visibilityChanged.connect(self._visibility)
        self._config_changed()

    def _build(self) -> None:
        top = QHBoxLayout()
        self.store_choice = QComboBox()
        self.store_choice.setToolTip(self.tr("依頼の保存先"))
        self.store_choice.currentIndexChanged.connect(self._store_chosen)
        top.addWidget(self.store_choice, 1)
        self.category_filter = QComboBox()
        self.category_filter.addItems([self.tr("すべての区分"), "定点", "臨時"])
        self.status_filter = QComboBox()
        self.status_filter.addItems([self.tr("すべての状態"), "有効", "停止", "完了"])
        for c in (self.category_filter, self.status_filter):
            c.currentIndexChanged.connect(lambda _: self.render())
            top.addWidget(c)
        self.body.addLayout(top)
        self.status = QLabel()
        self.status.setWordWrap(True)
        self.status.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        self.body.addWidget(self.status)
        self.model = QStandardItemModel(self)
        self.table = QTableView()
        self.table.setModel(self.model)
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.verticalHeader().hide()
        self.table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.ResizeToContents)
        self.table.selectionModel().currentRowChanged.connect(lambda current, _: self._row(current.row()))
        self.table.doubleClicked.connect(lambda _: self.edit())
        self.body.addWidget(self.table, 1)

        edit = QHBoxLayout()
        self.new_button = QPushButton(self.tr("新規…"))
        self.new_button.clicked.connect(lambda: self.edit(new=True))
        self.edit_button = QPushButton(self.tr("編集…"))
        self.edit_button.clicked.connect(lambda: self.edit())
        self.delete_button = QPushButton(self.tr("削除"))
        self.delete_button.setToolTip(self.tr("選んだ依頼を保存先から削除"))
        self.delete_button.clicked.connect(self._delete_clicked)
        self.reload_button = QPushButton(self.tr("再読み込み"))
        self.reload_button.clicked.connect(lambda: self.reload())
        for b in (self.new_button, self.edit_button, self.delete_button, self.reload_button):
            edit.addWidget(b)
        self.body.addLayout(edit)

        layers = QHBoxLayout()
        self.import_button = QPushButton(self.tr("レイヤーから取り込み"))
        self.import_button.setToolTip(self.tr("作業中のレイヤーの地物（選択があれば選択中のもの）を定点の依頼として取り込み。"
                                              "依頼ID が同じ依頼は上書き"))
        self.import_button.clicked.connect(lambda: self.import_layer())
        self.point_km = QDoubleSpinBox()
        self.point_km.setRange(0.1, 500)
        self.point_km.setValue(10)
        self.point_km.setSuffix(" km")
        self.point_km.setToolTip(self.tr("点の地物を取り込むときの範囲の一辺"))
        self.export_button = QPushButton(self.tr("レイヤーへ書き出し"))
        self.export_button.setToolTip(self.tr("一覧に出ている依頼を一時レイヤーとして追加（区分で「定点」を選ぶと定点のみ）"))
        self.export_button.clicked.connect(lambda: self.export_layer())
        layers.addWidget(self.import_button)
        layers.addWidget(self.point_km)
        layers.addWidget(self.export_button)
        layers.addStretch(1)
        self.body.addLayout(layers)

        self._update_buttons()

    # --- Stores ---------------------------------------------------------------------------------

    def _config_changed(self) -> None:
        stores = self.context.config.request_stores
        self.store_choice.blockSignals(True)
        self.store_choice.clear()
        for s in stores:
            self.store_choice.addItem(s.label)
        self.store_choice.blockSignals(False)
        self.store_choice.setVisible(len(stores) > 1)
        self.store = stores[0] if stores else None
        self.requests, self.chosen, self._loaded = [], None, False
        self.render()
        if self.store is None:
            self.status.setText(self.tr("設定ファイルに撮像依頼の保存先（requestStores）なし"))
        elif self.isVisible():
            self.reload()

    def _visibility(self, visible: bool) -> None:
        if visible and self.store is not None and not self._loaded:
            self.reload()
        if not visible:
            self.overlay.unpaint()
            self.canvas.refresh()
        else:
            self.draw()

    def _store_chosen(self, index: int) -> None:
        stores = self.context.config.request_stores
        self.store = stores[index] if 0 <= index < len(stores) else None
        self.chosen, self._loaded = None, False
        self.reload()

    def _start(self, what: str, work, done, failed=None) -> bool:
        if self.task is not None:
            return False

        def finish(result):
            self.task = None
            self._update_buttons()
            done(result)

        def fail(message):
            self.task = None
            self._update_buttons()
            (failed or self.status.setText)(message)

        self.task = run_task(what, work, finish, fail)
        self._update_buttons()
        return True

    def token(self) -> Optional[str]:
        return self.context.token_for(self.store.url, self.store.token) if self.store else None

    def reload(self, then: Optional[Callable[[], None]] = None) -> None:
        """Reads the ledger again."""
        store = self.store
        if store is None:
            return
        self.status.setText(self.tr("{label} を読み込み中…").format(label=store.label))
        client, token = self.context.esri(), self.token()

        def done(requests):
            if store is not self.store:
                return
            self.requests, self._loaded = requests, True
            self._update_buttons()
            chosen = self.chosen.id if self.chosen else None
            self.chosen = next((r for r in requests if r.id == chosen), None)
            self.render()
            active = sum(1 for r in requests if r.status == "有効")
            self.status.setText(self.tr("{label}: {n} 件（有効 {a} 件）").format(label=store.label, n=len(requests), a=active))
            if then:
                then()

        if not self._start(self.tr("撮像依頼の読み込み"), lambda task: load_requests(client, store, token, task.isCanceled), done,
                           lambda m: self.status.setText(self.tr("読み込み失敗: {m}").format(m=m))):
            QTimer.singleShot(300, lambda: self.reload(then))

    # --- The list -------------------------------------------------------------------------------

    def render(self) -> None:
        cat = self.category_filter.currentText()
        st = self.status_filter.currentText()
        self.shown = [r for r in self.requests if (self.category_filter.currentIndex() == 0 or r.category == cat)
                      and (self.status_filter.currentIndex() == 0 or r.status == st)]
        self.model.clear()
        self.model.setHorizontalHeaderLabels([self.tr(h) for h in ("名称", "区分", "達成条件", "期間", "優先度", "種別", "状態")])
        chosen_row = -1
        for i, r in enumerate(self.shown):
            prio = priority_text(r.effective) + (f"・繰上 {r.boost}" if r.boost else "")
            items = [QStandardItem(t) for t in (r.name, r.category + (f"（{r.repeat}）" if r.category == "定点" else ""),
                                                r.achieve, _period(r), prio, r.kind, r.status)]
            items[6].setForeground(QBrush(QColor(STATUS_COLORS.get(r.status, "#2563eb"))))
            if r.note:
                items[0].setToolTip(r.note)
            self.model.appendRow(items)
            if r is self.chosen:
                chosen_row = i
        if chosen_row >= 0:
            self.table.selectionModel().blockSignals(True)
            self.table.selectRow(chosen_row)
            self.table.selectionModel().blockSignals(False)
        self.draw()
        self._update_buttons()

    def _row(self, row: int) -> None:
        if 0 <= row < len(self.shown):
            self.chosen = self.shown[row]
            self.draw()
            self.zoom()
            self._update_buttons()

    def draw(self) -> None:
        polygons = []
        for r in self.shown:
            if r.rings:
                chosen = r is self.chosen
                polygons.append((r.rings, STATUS_COLORS.get(r.status, "#2563eb"), 3 if chosen else 1, r.name if chosen else ""))
        self.overlay.paint(polygons)
        self.canvas.refresh()

    def zoom(self) -> None:
        box: Optional[QgsRectangle] = None
        for r in ([self.chosen] if self.chosen else self.shown):
            for ring in r.rings:
                for x, y in ring:
                    if box is None:
                        box = QgsRectangle(x, y, x, y)
                    else:
                        box.combineExtentWith(x, y)
        if box is None:
            return
        box = box.buffered(max(box.width(), box.height()) * 0.2 + 0.01)
        self.canvas.setExtent(from_wgs84(self.canvas.mapSettings().destinationCrs()).transformBoundingBox(box))
        self.canvas.refresh()

    def _update_buttons(self) -> None:
        ready = self.store is not None and self.task is None
        for b in (self.new_button, self.reload_button):
            b.setEnabled(ready)
        # Only once the ledger was read: what is new is told by it (#72).
        self.import_button.setEnabled(ready and self._loaded)
        self.edit_button.setEnabled(ready and self.chosen is not None)
        self.delete_button.setEnabled(ready and self.chosen is not None)
        self.export_button.setEnabled(bool(self.shown))

    # --- Adding, changing, deleting -------------------------------------------------------------

    def known(self) -> dict:
        """Names the form offers as hints: the systems, and the series and modes of the requests."""
        return {
            "systems": [s.system for s in self.context.config.request_exports],
            "series": sorted({s for r in self.requests for s in r.series}),
            "modes": sorted({m for r in self.requests for m in r.modes}),
        }

    def edit(self, new: bool = False, request: Optional[ImagingRequest] = None) -> None:
        """Opens the form for a new request, or the chosen one."""
        from .request_form import RequestForm

        if self.store is None:
            return
        if self.form is not None:
            self.form.close()
        if new or (request is None and self.chosen is None):
            base = request or ImagingRequest(id=new_request_id())
        else:
            base = request or self.chosen
        self.form = RequestForm(self.iface, base, self._save, self.known(), self)
        self.form.show()

    def _save(self, request: ImagingRequest) -> None:
        store, client, token = self.store, self.context.esri(), self.token()
        form = self.form

        def done(saved):
            self.chosen = saved
            if form is not None:
                form.accept()
            self.reload()

        if not self._start(self.tr("撮像依頼の保存"), lambda task: save_request(client, store, token, request, _now_ms()), done,
                           lambda m: form.message.setText(self.tr("保存失敗: {m}").format(m=m)) if form else self.status.setText(m)):
            if form is not None:
                form.message.setText(self.tr("ほかの処理の実行中。少し待ってから保存"))

    def _delete_clicked(self) -> None:
        r = self.chosen
        if r is None:
            return
        answer = QMessageBox.question(self, self.tr("撮像依頼の削除"), self.tr("「{name}」を保存先から削除。元に戻せない操作").format(name=r.name))
        if answer != QMessageBox.StandardButton.Yes:
            return
        store, client, token = self.store, self.context.esri(), self.token()

        def done(_):
            self.chosen = None
            self.reload()

        self._start(self.tr("撮像依頼の削除"), lambda task: delete_request(client, store, token, r), done)

    # --- Layers ---------------------------------------------------------------------------------

    def export_layer(self) -> Optional[QgsVectorLayer]:
        """Adds the requests listed as a scratch layer."""
        if not self.shown:
            return None
        cat = self.category_filter.currentText() if self.category_filter.currentIndex() else ""
        layer = requests_layer(self.shown, self.tr("撮像依頼") + (f"（{cat}）" if cat else ""))
        QgsProject.instance().addMapLayer(layer)
        self.status.setText(self.tr("{n} 件をレイヤー「{name}」に書き出し").format(n=len(self.shown), name=layer.name()))
        return layer

    def import_layer(self, layer: Optional[QgsVectorLayer] = None, ask: bool = True) -> None:
        """Takes in the features of a layer (the active one) as 定点 requests: new ones added, those with a known 依頼ID changed."""
        layer = layer or self.iface.activeLayer()
        if not isinstance(layer, QgsVectorLayer) or not layer.isSpatial():
            self.status.setText(self.tr("取り込むレイヤー（ポリゴンか点）を作業中のレイヤーに選択"))
            return
        if not self._loaded or self.store is None:
            self.status.setText(self.tr("台帳を読み込んでから取り込み"))
            return
        selected = layer.selectedFeatureCount() > 0
        rows = layer_rows(layer, selected, self.point_km.value())
        roles = field_roles([f.name() for f in layer.fields()])
        by_id = {r.id: r for r in self.requests}
        requests, skipped, kept = [], [], []
        seen: set = set()
        for i, (values, rings, kind) in enumerate(rows):
            rid = values.get(roles["id"]) if "id" in roles else None
            key = str(rid).strip() if rid not in (None, "") else None
            if key is not None and key in seen:
                # The same id twice in the layer: the first one only (#72).
                skipped.append(f"{i + 1} 行目: 依頼ID {key} が重複")
                continue
            existing = by_id.get(key) if key is not None else None
            r = request_of_row(values, roles, rings, kind, existing)
            problems = row_problems(r)
            if problems:
                skipped.append(f"{i + 1} 行目: {'・'.join(problems)}")
            else:
                if key is not None:
                    seen.add(key)
                requests.append(r)
                kept.append((values, rings, kind, key))
        if not requests:
            self.status.setText(self.tr("取り込める地物なし（{why}）").format(why="、".join(skipped[:3]) or self.tr("地物なし")))
            return
        changed = sum(1 for r in requests if r.object_id is not None)
        if ask:
            text = self.tr("「{layer}」の{which} {n} 件を撮像依頼として取り込み（新規 {new} 件・上書き {old} 件）").format(
                layer=layer.name(), which=self.tr("選択中の地物") if selected else self.tr("地物"), n=len(requests),
                new=len(requests) - changed, old=changed)
            if skipped:
                text += "\n" + self.tr("取り込まないもの: {list}").format(list="、".join(skipped[:5]))
            if QMessageBox.question(self, self.tr("レイヤーから取り込み"), text) != QMessageBox.StandardButton.Yes:
                return
        store, client, token = self.store, self.context.esri(), self.token()

        def work(task):
            # The ledger as it is now (not as the dock last read it): ids already there are changed, not added again (#72).
            fresh = {r.id: r for r in load_requests(client, store, token, task.isCanceled)}
            todo = []
            for values, rings, kind, key in kept:
                existing = fresh.get(key) if key is not None else None
                r = request_of_row(values, roles, rings, kind, existing)
                if existing is not None:
                    r.updated = existing.updated
                todo.append(r)
            saved, failed = 0, []
            for k, r in enumerate(todo):
                if task.isCanceled():
                    break
                try:
                    save_request(client, store, token, r, _now_ms())
                    saved += 1
                except Exception as error:  # noqa: BLE001 (each one reported)
                    failed.append(f"{r.name}: {error}")
                task.progress(100 * (k + 1) / len(todo))
            return saved, failed

        def done(result):
            saved, failed = result
            self.reload(lambda: self.status.setText(
                self.tr("{n} 件を取り込み").format(n=saved) + (self.tr("（失敗 {m} 件: {list}）").format(m=len(failed), list="、".join(failed[:3])) if failed else "")
                + (self.tr("・取り込まなかったもの {k} 件").format(k=len(skipped)) if skipped else "")))

        self._start(self.tr("撮像依頼の取り込み"), work, done)

    def close_all(self) -> None:
        """Closes the form and takes the areas off the map (when the plugin is unloaded)."""
        if self.form is not None:
            self.form.close()
            self.form.deleteLater()
        self.form = None
        self.overlay.unpaint()
