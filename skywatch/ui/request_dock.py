"""
The 「撮像依頼」 dock: the imaging request ledger (`requestStores`). It lists
the requests (filtered by category, status and 依頼グループ) and shows them on the map;
requests are added and changed in a form; 「レイヤーから取り込み…」 takes the
areas and names of a layer's features and asks the rest once in the form
(or reads every item from a layer written out by 「レイヤーへ書き出し」) (docs/imaging-requests.md). The day's files are
written in a dock of its own, 「撮像計画ファイル出力」 (plan_file_dock.py). Every request to the service runs in a QgsTask.
"""
from __future__ import annotations

import math
import time
from dataclasses import replace
from typing import Callable, Optional

from qgis.core import QgsProject, QgsRectangle, QgsVectorLayer
from qgis.PyQt.QtCore import QCoreApplication, Qt, QTimer
from qgis.PyQt.QtGui import QBrush, QColor, QStandardItem, QStandardItemModel
from qgis.PyQt.QtWidgets import (
    QAbstractItemView, QComboBox, QHBoxLayout, QHeaderView, QLabel, QMessageBox, QPushButton, QTableView,
)

from ..core import edit_stamp
from ..core.request.bulk import RequestChange, apply_change
from ..core.request.from_plan import requests_of_plan
from ..core.request.groups import first_group, offered, read_groups
from ..core.request.last import remembered, with_remembered
from ..core.request.model import KINDS, STATUSES, ImagingRequest, jst_day, priority_text
from ..core.request.rows import code_text, field_roles, request_of_row, row_problems
from ..core.request.store import RequestStoreConfig, delete_request, load_requests, new_request_id, save_request, waiting_ids
from ..qgis_io.geometry import from_wgs84
from ..qgis_io.request_layers import layer_rows, requests_layer
from ..qgis_io.tasks import run_task
from . import last_inputs
from .docks import PluginDock, plain_label, plain_tip
from .list_views import ListViews
from .plan_overlay import PlanOverlay
from .request_groups import NO_GROUP_TEXT, GroupFilter

STATUS_COLORS = {"有効": "#2563eb", "停止": "#6b7280", "完了": "#16a34a"}


def _now_ms() -> float:
    return time.time() * 1000


def _period(r: ImagingRequest) -> str:
    if not (math.isfinite(r.start) and math.isfinite(r.end)):
        return ""
    a, b = jst_day(r.start), jst_day(r.end)
    return f"{a.isoformat()}〜{b.isoformat()[5:] if a.year == b.year else b.isoformat()}"


def group_value(values: dict) -> dict:
    """The bulk edit's values, 「（グループなし）」 as no group."""
    if values.get("group") == NO_GROUP_TEXT:
        values = {**values, "group": ""}
    return values


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
        #: The 依頼グループ of the store's table (core/request/groups.py), and whether the table was read.
        self.groups: list = []
        self.groups_read = False
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
        self.group_filter = GroupFilter("requests")
        for c in (self.category_filter, self.status_filter, self.group_filter):
            c.currentIndexChanged.connect(lambda _: self.render())
            top.addWidget(c)
        self.body.addLayout(top)
        # Request names (others' text) and the services' messages: shown as written, never as HTML.
        self.status = plain_label(QLabel())
        self.status.setWordWrap(True)
        self.status.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        self.body.addWidget(self.status)
        self.model = QStandardItemModel(self)
        self.table = QTableView()
        self.table.setModel(self.model)
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        # Ctrl / Shift + click chooses several, for 「一括編集」.
        self.table.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.verticalHeader().hide()
        self.table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.ResizeToContents)
        self.table.selectionModel().currentRowChanged.connect(lambda current, _: self._row(current.row()))
        self.table.selectionModel().selectionChanged.connect(lambda *_: self._update_buttons())
        self.table.doubleClicked.connect(lambda _: self.edit())
        self.body.addWidget(self.table, 1)
        # 「ビュー」: the columns and the filters kept under a name (per user).
        self.views = ListViews("requests", self.table, {"category": self.category_filter, "status": self.status_filter,
                                                        "group": self.group_filter}, None, self)
        top.addWidget(self.views.button)

        edit = QHBoxLayout()
        self.new_button = QPushButton(self.tr("新規…"))
        self.new_button.clicked.connect(lambda: self.edit(new=True))
        self.edit_button = QPushButton(self.tr("編集…"))
        self.edit_button.clicked.connect(lambda: self.edit())
        self.delete_button = QPushButton(self.tr("削除"))
        self.delete_button.setToolTip(self.tr("選んだ依頼を保存先から削除"))
        self.delete_button.clicked.connect(self._delete_clicked)
        self.bulk_button = QPushButton(self.tr("一括編集…"))
        self.bulk_button.setToolTip(self.tr("選んだ依頼（Ctrl・Shift + クリックで複数）の優先度・状態・種別をまとめて変更"))
        self.bulk_button.clicked.connect(lambda: self.bulk_edit())
        self.reload_button = QPushButton(self.tr("再読み込み"))
        self.reload_button.clicked.connect(lambda: self.reload())
        self.event_button = QPushButton(self.tr("イベントに追加"))
        self.event_button.setToolTip(self.tr("選んだ依頼を対応中のイベント（地震対応など）に入れる"))
        self.event_button.clicked.connect(lambda: self.add_to_event())
        for b in (self.new_button, self.edit_button, self.bulk_button, self.delete_button, self.event_button, self.reload_button):
            edit.addWidget(b)
        self.body.addLayout(edit)

        layers = QHBoxLayout()
        self.import_button = QPushButton(self.tr("レイヤーから取り込み…"))
        self.import_button.setToolTip(self.tr("レイヤーと名称・管理番号・識別番号の属性を選び、地物の範囲と名称・番号で依頼を登録。"
                                              "期間・優先度・衛星などは入力画面でまとめて指定"))
        self.import_button.clicked.connect(lambda: self.import_layer())
        #: The side of a point feature's square last used.
        self.point_km = 10.0
        self.export_button = QPushButton(self.tr("レイヤーへ書き出し"))
        self.export_button.setToolTip(self.tr("一覧に出ている依頼を一時レイヤーとして追加（区分で「定点」を選ぶと定点のみ）"))
        self.export_button.clicked.connect(lambda: self.export_layer())
        layers.addWidget(self.import_button)
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
        self.groups, self.groups_read = [], False
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
            # (isVisible() may still say no while the dock's tab is being brought up.)
            self.draw(shown=True)

    def _store_chosen(self, index: int) -> None:
        stores = self.context.config.request_stores
        self.store = stores[index] if 0 <= index < len(stores) else None
        self.chosen, self._loaded = None, False
        self.groups, self.groups_read = [], False
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

    def reload(self, then: Optional[Callable[[], None]] = None, failed: Optional[Callable[[str], None]] = None) -> None:
        """Reads the ledger again (`then` once read, `failed` with the reason when it cannot be)."""
        store = self.store
        if store is None:
            return
        self.status.setText(self.tr("{label} を読み込み中…").format(label=store.label))
        client, token = self.context.esri(), self.token()

        def work(task):
            requests = load_requests(client, store, token, task.isCanceled)
            groups, note = read_groups(client, store.groups, store.group_fields, token, task.isCanceled)
            return requests, groups, note

        def done(result):
            if store is not self.store:
                return
            requests, groups, note = result
            self.requests, self._loaded = requests, True
            if groups is not None:
                self.groups, self.groups_read = groups, True
            self.group_filter.set_groups(self.groups, (r.group for r in requests))
            self._update_buttons()
            chosen = self.chosen.id if self.chosen else None
            self.chosen = next((r for r in requests if r.id == chosen), None)
            self.render()
            active = sum(1 for r in requests if r.status == "有効")
            text = self.tr("{label}: {n} 件（有効 {a} 件）").format(label=store.label, n=len(requests), a=active)
            self.status.setText(text + (f"。{note}" if note else ""))
            if then:
                then()

        def not_read(m):
            self.status.setText(self.tr("読み込み失敗: {m}").format(m=m))
            if failed:
                failed(m)

        if not self._start(self.tr("撮像依頼の読み込み"), work, done, not_read):
            QTimer.singleShot(300, lambda: self.reload(then, failed))

    # --- The list -------------------------------------------------------------------------------

    def render(self) -> None:
        # Requests put in from elsewhere (the 「撮像計画ファイル出力」 dock's read): their groups listed too.
        self.group_filter.set_groups(self.groups, (r.group for r in self.requests), force=False)
        cat = self.category_filter.currentText()
        st = self.status_filter.currentText()
        self.shown = [r for r in self.requests if (self.category_filter.currentIndex() == 0 or r.category == cat)
                      and (self.status_filter.currentIndex() == 0 or r.status == st) and self.group_filter.passes(r.group)]
        self.model.clear()
        self.model.setHorizontalHeaderLabels([self.tr(h) for h in ("名称", "依頼グループ", "管理番号", "識別番号", "区分", "達成条件", "期間",
                                                                   "優先度", "種別", "状態")])
        chosen_row = -1
        try:
            # Saved while the ledger could not be reached, waiting to be sent (core/offline.py): marked blue.
            waiting = waiting_ids(self.context.esri(), self.store) if self.store is not None else set()
        except Exception:  # noqa: BLE001
            waiting = set()
        for i, r in enumerate(self.shown):
            prio = priority_text(r.effective) + (f"・繰上 {r.boost}" if r.boost else "")
            items = [QStandardItem(t) for t in (r.name + (self.tr("（送信待ち）") if r.id in waiting else ""),
                                                r.group, r.management_no, r.identification_no,
                                                r.category + (f"（{r.repeat}）" if r.category == "定点" else ""),
                                                r.achieve, _period(r), prio, r.kind, r.status)]
            items[-1].setForeground(QBrush(QColor(STATUS_COLORS.get(r.status, "#2563eb"))))
            if r.note:
                items[0].setToolTip(plain_tip(r.note))
            if r.id in waiting:
                for item in items:
                    item.setBackground(QBrush(QColor("#dbeafe")))
                items[0].setToolTip(self.tr("送信待ち（オフライン中に保存。台帳にはまだない）"))
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

    def draw(self, shown: Optional[bool] = None) -> None:
        """The listed requests' areas on the map (the chosen one thicker, with its name), while the dock is shown."""
        if not (self.isVisible() if shown is None else shown):
            # Read while closed (「先読み」): the areas are drawn when the dock is opened, not on the map meanwhile.
            if self.overlay.painted():
                self.overlay.unpaint()
                self.canvas.refresh()
            return
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
        self.bulk_button.setEnabled(ready and bool(self.selected()))
        self.event_button.setVisible(self.context.config.events is not None)
        self.event_button.setEnabled(bool(self.selected()))
        self.export_button.setEnabled(bool(self.shown))

    # --- Adding, changing, deleting -------------------------------------------------------------

    def known(self) -> dict:
        """
        Names the form offers as hints: the systems, and the series and modes
        of the requests; the 依頼グループ to choose from (those of the table in
        use: `groupTable`), or else those of the requests (typed freely).
        """
        table = self.groups_read and self.store is not None and self.store.groups is not None
        return {
            "systems": [s.system for s in self.context.config.request_exports],
            "series": sorted({s for r in self.requests for s in r.series}),
            "modes": sorted({m for r in self.requests for m in r.modes}),
            "groups": offered(self.groups) if table else sorted({r.group for r in self.requests if r.group}, key=str.casefold),
            "groupTable": table,
        }

    def new_request(self) -> ImagingRequest:
        """
        A new request started from the conditions last saved (入力の初期値の記憶);
        its 依頼グループ the one last saved while it is in use, else the user's
        own group.
        """
        base = with_remembered(ImagingRequest(id=new_request_id()), last_inputs.read("request"))
        table = self.groups_read and self.store is not None and self.store.groups is not None
        base.group = first_group(self.groups, edit_stamp.user_name(), base.group, table)
        return base

    def edit(self, new: bool = False, request: Optional[ImagingRequest] = None) -> None:
        """Opens the form for a new request, or the chosen one."""
        from .request_form import RequestForm

        if self.store is None:
            return
        if self.form is not None:
            self.form.close()
        if new or (request is None and self.chosen is None):
            # A new request starts from the conditions last saved (入力の初期値の記憶).
            base = request or self.new_request()
        else:
            base = request or self.chosen
        self.form = RequestForm(self.iface, base, self._save, self.known(), self, self.satellites)
        self.form.show()

    def _save(self, request: ImagingRequest) -> None:
        store, client, token = self.store, self.context.esri(), self.token()
        form = self.form

        def done(saved):
            last_inputs.write("request", remembered(request))
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
        answer = QMessageBox.question(self, self.tr("撮像依頼の削除"), plain_tip(self.tr("「{name}」を保存先から削除しますか？ほかの人の一覧からも消え、元に戻せません。").format(name=r.name)),
                                      QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No, QMessageBox.StandardButton.No)
        if answer != QMessageBox.StandardButton.Yes:
            return
        store, client, token = self.store, self.context.esri(), self.token()

        def done(count):
            self.chosen = None
            gone = self.tr("「{name}」を削除").format(name=r.name) if count else self.tr("「{name}」は台帳になし（ほかの人が削除済み）").format(name=r.name)
            self.reload(lambda: self.status.setText(gone))

        self._start(self.tr("撮像依頼の削除"), lambda task: delete_request(client, store, token, r), done,
                    lambda m: self.status.setText(self.tr("削除失敗: {m}").format(m=m)))

    def selected(self) -> list[ImagingRequest]:
        """The requests chosen in the list (several with Ctrl / Shift)."""
        rows = sorted({i.row() for i in self.table.selectionModel().selectedRows()})
        return [self.shown[r] for r in rows if 0 <= r < len(self.shown)]

    #: The 「イベント」 dock (plugin.py sets it).
    event_linker: Optional[Callable[[], object]] = None

    def event_items(self) -> list:
        """The chosen requests as items of an event."""
        from ..core.events import LinkItem

        url = self.store.url if self.store else ""
        return [LinkItem("request", r.id, label=r.name or r.id, time=int(r.start) if math.isfinite(r.start) else None, source=url)
                for r in self.selected()]

    def add_to_event(self):
        events = self.event_linker() if self.event_linker else None
        return events.offer(self.event_button, self.event_items) if events and self.selected() else None

    def select(self, ids) -> None:
        """Chooses the listed requests of these ids."""
        ids = set(ids)
        sel = self.table.selectionModel()
        sel.clearSelection()
        for i, r in enumerate(self.shown):
            if r.id in ids:
                sel.select(self.model.index(i, 0), sel.SelectionFlag.Select | sel.SelectionFlag.Rows)
        self._update_buttons()

    def bulk_edit(self):
        """Asks what to set on the chosen requests, then saves it on each."""
        from .bulk_edit_dialog import BulkEditDialog, BulkField

        chosen = self.selected()
        if not chosen or self.store is None:
            return None
        first = chosen[0]
        fields = [
            BulkField("priority", self.tr("優先度"), priority=True, value=first.priority,
                      tip=self.tr("1 が最も高い（最優先 1〜200・高・中・低・最低）"), extra=("reset_boost", self.tr("繰り上げも解除"))),
            BulkField("status", self.tr("状態"), choices=STATUSES, value=first.status),
            BulkField("kind", self.tr("種別"), choices=KINDS, value=first.kind),
        ]
        known = self.known()
        if known["groupTable"]:
            fields.append(BulkField("group", self.tr("依頼グループ"), choices=(NO_GROUP_TEXT, *offered(self.groups, first.group)),
                                    value=first.group or NO_GROUP_TEXT))
        else:
            fields.append(BulkField("group", self.tr("依頼グループ"), words=known["groups"], value=first.group,
                                    tip=self.tr("空欄でグループなし")))
        dialog = BulkEditDialog(self.tr("撮像依頼の一括編集"), self.tr("選んだ依頼 {n} 件").format(n=len(chosen)), fields, self)
        dialog.accepted.connect(lambda: self.apply_bulk(chosen, RequestChange(**group_value(dialog.values()))))
        dialog.finished.connect(lambda _r: dialog.deleteLater())
        dialog.open()
        return dialog

    def apply_bulk(self, chosen: list[ImagingRequest], change: RequestChange) -> bool:
        """Saves a change on each request (in a task): each one checked against the ledger, the failures told by name."""
        problems = change.problems()
        if change.empty or problems:
            self.status.setText("、".join(problems) or self.tr("変更する項目なし"))
            return False
        store, client, token = self.store, self.context.esri(), self.token()
        ids = [r.id for r in chosen]
        self.status.setText(self.tr("{n} 件を変更中（{what}）…").format(n=len(chosen), what=change.text()))

        def done(result):
            self.reload(lambda: (self.select(ids), self.status.setText(result.text())))

        started = self._start(self.tr("撮像依頼の一括編集"),
                              lambda task: apply_change(client, store, token, chosen, change, _now_ms, task.isCanceled, task.progress),
                              done, lambda m: self.status.setText(self.tr("一括編集の失敗: {m}").format(m=m)))
        if not started:
            self.status.setText(self.tr("ほかの処理の実行中。少し待ってから変更"))
        return started

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

    def import_layer(self, layer: Optional[QgsVectorLayer] = None):
        """Asks which layer and which attribute names the requests (「レイヤーから取り込み…」), then opens the form for the rest."""
        from .layer_import_dialog import LayerImportDialog

        if not self._loaded or self.store is None:
            self.status.setText(self.tr("台帳を読み込んでから取り込み"))
            return None
        active = layer or self.iface.activeLayer()
        dialog = LayerImportDialog(active if isinstance(active, QgsVectorLayer) and active.isSpatial() else None, self.point_km, self)

        def chosen():
            layer = dialog.chosen_layer()
            self.point_km = dialog.point_km.value()
            if layer is None:
                return
            selected = dialog.selected_only.isChecked()
            if dialog.full.isChecked():
                self.import_rows(layer, selected=selected)
            else:
                fields = dialog.fields()
                self.take_layer(layer, fields["name"], selected, fields["management_no"], fields["identification_no"])

        dialog.accepted.connect(chosen)
        dialog.finished.connect(lambda _r: dialog.deleteLater())
        dialog.open()
        return dialog

    def take_layer(self, layer: QgsVectorLayer, name_field: str = "", selected: bool = False,
                   management_field: str = "", identification_field: str = ""):
        """Opens the form for the features of a layer: their areas, names (`name_field`) and 管理番号・識別番号 (the
        attributes named) as they are, the rest asked once (a number a feature lacks: the one typed in the form)."""
        from .request_form import RequestForm

        if self.store is None:
            return None
        rows = layer_rows(layer, selected and layer.selectedFeatureCount() > 0, self.point_km)
        items, skipped = [], 0
        for i, (values, rings, kind) in enumerate(rows):
            if not rings:
                skipped += 1
                continue
            name = values.get(name_field) if name_field else None
            name = str(name).strip() if name not in (None, "") else ""
            numbers = {"management_no": code_text(values.get(management_field)) if management_field else "",
                       "identification_no": code_text(values.get(identification_field)) if identification_field else ""}
            items.append((name or f"{layer.name()} {i + 1}", rings, kind, numbers))
        if not items:
            self.status.setText(self.tr("取り込める地物なし（範囲のない地物のみ）"))
            return None
        if self.form is not None:
            self.form.close()
        base = self.new_request()
        base.category, base.achieve = "定点", "毎回"
        names = "、".join(n for n, *_rest in items[:3]) + ("…" if len(items) > 3 else "")
        text = self.tr("レイヤー「{layer}」の地物 {n} 件（名称: {field}").format(
            layer=layer.name(), n=len(items), field=name_field or self.tr("レイヤー名と連番"))
        if management_field:
            text += self.tr("・管理番号: {field}").format(field=management_field)
        if identification_field:
            text += self.tr("・識別番号: {field}").format(field=identification_field)
        text += self.tr("）")
        if skipped:
            text += self.tr("・範囲のない地物 {k} 件は除く").format(k=skipped)
        batch = {"items": items, "text": text, "names": names,
                 "fields": {"management_no": management_field, "identification_no": identification_field}}
        self.form = RequestForm(self.iface, base, lambda template: self._save_batch(template, items), self.known(), self,
                                satellites=self.satellites, batch=batch)
        self.form.show()
        return self.form

    def from_plan(self, ops: list, sats: list, targets: list):
        """
        臨時 requests from the 「撮像計画」 dock's opportunities (one per target:
        its area, the days and satellites of those chosen), opened in the form
        to look over and save; several targets in one form, saved one each.
        """
        from .request_form import RequestForm

        if self.store is None:
            self.status.setText(self.tr("設定ファイルに撮像依頼の保存先（requestStores）なし"))
            return None
        base = self.new_request()
        made = requests_of_plan(ops, sats, targets, new_request_id, self.point_km, base)
        if not made:
            return None
        if len(made) == 1:
            self.edit(new=True, request=made[0])
            return self.form
        if self.form is not None:
            self.form.close()
        # One form for all of them: the days from the first to the last, the satellites of all. What the form
        # leaves as it was is each target's own (its days, satellites, modes, kind); the shots go in each note.
        kinds = {r.kind for r in made}
        template = replace(made[0], start=min(r.start for r in made), end=max(r.end for r in made),
                           satellites=list(dict.fromkeys(s for r in made for s in r.satellites)),
                           modes=list(dict.fromkeys(m for r in made for m in r.modes)),
                           kind=made[0].kind if len(kinds) == 1 else "任意", note="")
        if template.kind != "光学":
            template.cloud_no_go = None
        own = ("start", "end", "satellites", "modes", "kind", "cloud_no_go")
        items = [(r.name, r.rings, r.shape_kind, {"own": {k: getattr(r, k) for k in own}, "note": r.note}) for r in made]
        names = "、".join(r.name for r in made[:3]) + ("…" if len(made) > 3 else "")
        batch = {"items": items, "names": names, "title": self.tr("撮像計画から臨時依頼（{n} 件）").format(n=len(made)),
                 "text": self.tr("撮像計画の対象 {n} 件（対象ごとに 1 件。範囲は対象のもの。期間・衛星・モード・種別は、"
                                 "ここで変えなければ対象ごとの撮像機会のもの。撮像の一覧は各件の備考に）").format(n=len(made))}
        self.form = RequestForm(self.iface, template, lambda t: self._save_batch(t, items, template), self.known(), self,
                                satellites=self.satellites, batch=batch)
        self.form.show()
        return self.form

    def _save_batch(self, template: ImagingRequest, items: list, first: Optional[ImagingRequest] = None) -> None:
        """
        Saves one request per feature: the form's items, the feature's name and
        area. An item's `own` values stand where the form still has what it
        opened with (`first`); its `note` follows the form's.
        """
        store, client, token = self.store, self.context.esri(), self.token()
        form = self.form
        todo = []
        for name, rings, kind, numbers in items:
            r = ImagingRequest(**{**template.__dict__})
            r.id, r.object_id = new_request_id(), None
            r.name, r.rings, r.shape_kind = name, [list(x) for x in rings], kind
            # The feature's own numbers; one it lacks is the form's.
            r.management_no = numbers.get("management_no") or template.management_no
            r.identification_no = numbers.get("identification_no") or template.identification_no
            for key, value in (numbers.get("own") or {}).items():
                if first is not None and getattr(template, key) == getattr(first, key):
                    setattr(r, key, value)
            r.note = "\n".join(x for x in (template.note, numbers.get("note") or "") if x)
            todo.append(r)

        def work(task):
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
            last_inputs.write("request", remembered(template))
            if form is not None:
                form.accept()
            self.reload(lambda: self.status.setText(
                self.tr("{n} 件を取り込み").format(n=saved)
                + (self.tr("（失敗 {m} 件: {list}）").format(m=len(failed), list="、".join(failed[:3])) if failed else "")))

        if not self._start(self.tr("撮像依頼の取り込み"), work, done,
                           lambda m: form.message.setText(self.tr("取り込み失敗: {m}").format(m=m)) if form else self.status.setText(m)):
            if form is not None:
                form.message.setText(self.tr("ほかの処理の実行中。少し待ってから保存"))

    def import_rows(self, layer: Optional[QgsVectorLayer] = None, ask: bool = True, selected: Optional[bool] = None) -> None:
        """Takes in the features of a layer with every item from its attributes (as 「レイヤーへ書き出し」 writes them):
        new ones added, those with a known 依頼ID changed."""
        layer = layer or self.iface.activeLayer()
        if not isinstance(layer, QgsVectorLayer) or not layer.isSpatial():
            self.status.setText(self.tr("取り込むレイヤー（ポリゴンか点）を作業中のレイヤーに選択"))
            return
        if not self._loaded or self.store is None:
            self.status.setText(self.tr("台帳を読み込んでから取り込み"))
            return
        selected = layer.selectedFeatureCount() > 0 if selected is None else selected and layer.selectedFeatureCount() > 0
        rows = layer_rows(layer, selected, self.point_km)
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
