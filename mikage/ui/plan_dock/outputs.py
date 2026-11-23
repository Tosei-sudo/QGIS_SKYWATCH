"""
撮像計画ドックの書き出し (ui/plan_dock, docs/architecture.md): the opportunities as requests, CSV and a layer, and the buttons for them. A part of ImagingPlanDock
(ui/imaging_plan_dock.py): its methods as they were, on the dock's widgets and state.
"""
from __future__ import annotations

import math
from datetime import datetime
from typing import Optional

from qgis.core import QgsProject, QgsVectorLayer
from qgis.PyQt.QtWidgets import (
    QFileDialog, QHBoxLayout, QLabel, QMenu, QPushButton, QSpinBox, QWidget,
)

from ...core.plan.api import (
    local_text, plan_csv, plan_features,
)
from ...core.plan.api import (
    revisit_csv, revisit_layer,
)
from ...qgis_io.plan_layer import memory_layer
from ..docks import save_text


class OutputsPart:
    """撮像計画ドックの書き出し."""

    def _later_actions(self) -> QHBoxLayout:
        """「予定・通知…」 and 「臨時依頼を作成」, and the row the first opens."""
        actions = QHBoxLayout()
        self.remind_button = QPushButton(self.tr("予定・通知…"))
        self.remind_button.setCheckable(True)
        self.remind_button.setToolTip(self.tr("撮像前の通知を設定、予定表（.ics）に保存"))
        # What is to be taken goes to the ledger as a 臨時 request (tsudo, 2026-11-09: 撮像計画と撮像依頼は同じ業務):
        # from there the day's files, the systems' answers and whether it was taken, as every request.
        self.request_button = QPushButton(self.tr("臨時依頼を作成"))
        self.request_button.setToolTip(self.tr("撮像機会の対象から臨時の撮像依頼を作成（範囲・日・衛星を引き継ぎ、依頼のフォームで確認して保存）"))
        menu = QMenu(self.request_button)
        self.request_chosen = menu.addAction(self.tr("選んだ撮像機会から"))
        self.request_chosen.triggered.connect(lambda: self.to_requests(all_shown=False))
        self.request_all = menu.addAction(self.tr("一覧のすべての撮像機会から（対象ごとに 1 件）"))
        self.request_all.triggered.connect(lambda: self.to_requests(all_shown=True))
        self.request_button.setMenu(menu)
        for b in (self.remind_button, self.request_button):
            actions.addWidget(b)
        actions.addStretch(1)

        self.remind_row = QWidget()
        remind = QHBoxLayout(self.remind_row)
        remind.setContentsMargins(0, 0, 0, 0)
        self.remind_minutes = QSpinBox()
        self.remind_minutes.setRange(0, 1440)
        self.remind_minutes.setValue(30)
        self.remind_minutes.setPrefix(self.tr("撮像の "))
        self.remind_minutes.setSuffix(self.tr(" 分前"))
        notify = QPushButton(self.tr("通知を設定"))
        notify.setToolTip(self.tr("一覧の撮像の前に、QGIS のメッセージバーで通知（QGIS の起動中のみ）"))
        notify.clicked.connect(self.set_reminders)
        ics = QPushButton(self.tr("予定表 (.ics)"))
        ics.setToolTip(self.tr("一覧の撮像を予定表（.ics）に保存。Outlook などに取り込める（設定した分前のアラーム付き）"))
        ics.clicked.connect(lambda: self.save_ics())
        clear = QPushButton(self.tr("通知をすべて解除"))
        clear.setToolTip(self.tr("設定した撮像の通知をすべて取り消す（ほかの計画で設定したものも）"))
        clear.clicked.connect(self.clear_reminders)
        self.remind_count = QLabel()
        for w in (self.remind_minutes, notify, ics, clear, self.remind_count):
            remind.addWidget(w)
        remind.addStretch(1)
        self.remind_row.hide()
        self.remind_button.toggled.connect(lambda on: (self.remind_row.setVisible(on), self.count_reminders()))

        return actions

    def to_requests(self, all_shown: bool = False):
        """
        Opens the 「撮像依頼」 form with 臨時 requests of the chosen opportunity
        (or of every one listed, one request per target); the form, or None.
        """
        plan = self.plan
        if not plan or self.make_requests is None or self.denied("requests.edit"):
            return None
        ops = list(self.shown) if all_shown or self.chosen is None else [self.chosen]
        if not ops:
            return None
        return self.make_requests(ops, plan["sats"], plan["targets"])

    def csv_text(self) -> str:
        plan = self.plan
        if self.revisit_shown():
            return revisit_csv(self.revisit["cells"], self.revisit["stats"])
        return plan_csv(self.shown, plan["sats"], plan["targets"], plan["options"].stations)

    def default_file_name(self) -> str:
        options = self.plan["options"]

        def day(ms: float) -> str:
            return datetime.fromtimestamp(ms / 1000).strftime("%Y%m%d")

        if self.revisit_shown():
            return "revisit.csv"
        return f"imaging-plan_{day(options.start)}-{day(options.end)}.csv"

    def save_csv(self, path: Optional[str] = None) -> None:
        if not self.plan or not (self.shown or self.revisit_shown()):
            return
        if not path:
            path, _ = QFileDialog.getSaveFileName(self, self.tr("CSV 保存"), self.default_file_name(), self.tr("CSV (*.csv)"))
        if not path:
            return
        failed = save_text(path, self.csv_text(), newline="", what="imaging-plan-csv")
        if failed:
            self.iface.messageBar().pushWarning(self.tr("撮像計画"), self.tr("CSV を保存できない: {m}").format(m=failed))
            return
        if self.revisit_shown():
            self.iface.messageBar().pushSuccess(self.tr("撮像計画"), self.tr("再訪間隔 {n} セルを CSV で保存").format(n=len(self.revisit["cells"])))
        else:
            self.iface.messageBar().pushSuccess(self.tr("撮像計画"), self.tr("撮像機会 {n} 件を CSV で保存").format(n=len(self.shown)))

    def layer_name(self) -> str:
        options = self.plan["options"]
        days = math.floor((options.end - options.start) / 86400000 + 0.5)
        return self.tr("撮像計画 {start} から {days} 日（{n} 回）").format(start=local_text(options.start), days=days, n=len(self.shown))

    def add_layer(self) -> Optional[QgsVectorLayer]:
        if self.plan and self.revisit_shown():
            options = self.plan["options"]
            days = math.floor((options.end - options.start) / 86400000 + 0.5)
            layer = memory_layer(revisit_layer(self.revisit["cells"], self.revisit["stats"]),
                                 self.tr("再訪間隔 {start} から {days} 日").format(start=local_text(options.start), days=days))
            QgsProject.instance().addMapLayer(layer)
            return layer
        if not self.plan or not self.shown:
            return None
        plan = self.plan
        layer = memory_layer(plan_features(self.shown, plan["sats"], plan["targets"], self.combining(), plan["options"].stations), self.layer_name())
        QgsProject.instance().addMapLayer(layer)
        return layer
