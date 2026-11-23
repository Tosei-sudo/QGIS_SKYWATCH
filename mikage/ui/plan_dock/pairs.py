"""
撮像計画ドックのペア・再訪 (ui/plan_dock, docs/architecture.md): the pairs (stereo, repeat) and the revisit grid. A part of ImagingPlanDock
(ui/imaging_plan_dock.py): its methods as they were, on the dock's widgets and state.
"""
from __future__ import annotations

import math
from dataclasses import replace
from typing import Optional

from qgis.core import QgsRectangle
from qgis.PyQt.QtCore import Qt
from qgis.PyQt.QtGui import QColor, QStandardItem
from qgis.PyQt.QtWidgets import (
    QCheckBox, QDoubleSpinBox, QHBoxLayout, QLabel, QSpinBox, QWidget,
)

from ...core.plan.api import PlanResult, plan_access
from ...core.plan.api import (
    js_round, local_text, side_text,
)
from ...core.plan.api import RepeatOptions, StereoOptions, repeat_pairs, stereo_pairs
from ...core.plan.api import (
    box_of, cell_targets, grid_cells, hours_text, revisit_color, revisit_stats,
)
from ...core.plan.api import satellite_label
from ...qgis_io.geometry import from_wgs84
from ...qgis_io.tasks import run_task
from .common import MAX_ROWS, Column


class PairsPart:
    """撮像計画ドックのペア・再訪."""

    def _pair_options(self) -> QWidget:
        """The conditions of the pair views and the revisit map's grid, under the view choice."""
        bar = QWidget()
        row = QHBoxLayout(bar)
        row.setContentsMargins(0, 0, 0, 0)

        def spin(low: float, high: float, value: float, decimals: int = 0, suffix: str = "") -> QDoubleSpinBox:
            box = QDoubleSpinBox()
            box.setRange(low, high)
            box.setDecimals(decimals)
            box.setValue(value)
            box.setSuffix(suffix)
            box.valueChanged.connect(self.show_view)
            return box

        def part(tooltip: str, *widgets) -> QWidget:
            w = QWidget()
            layout = QHBoxLayout(w)
            layout.setContentsMargins(0, 0, 0, 0)
            for x in widgets:
                layout.addWidget(QLabel(x) if isinstance(x, str) else x)
            w.setToolTip(tooltip)
            row.addWidget(w)
            w.hide()
            return w

        self.stereo_min = spin(1, 90, 15)
        self.stereo_max = spin(1, 90, 45, suffix="°")
        self.stereo_days = spin(0.01, 60, 7, 2)
        self.stereo_series = QCheckBox(self.tr("同じシリーズ"))
        self.stereo_series.setChecked(True)
        self.stereo_series.toggled.connect(self.show_view)
        self.stereo_bar = part(self.tr("2 回の撮像の視線のなす角（交会角）。15〜45° が DSM・建物の高さに好適"),
                               self.tr("交会角"), self.stereo_min, "〜", self.stereo_max, "・", self.stereo_days, self.tr("日以内"), self.stereo_series)
        self.repeat_incidence = spin(0.01, 10, 1, 2, "°")
        self.repeat_baseline = spin(0, 1e6, 0, 0, " m")
        self.repeat_baseline.setSpecialValueText(self.tr("制限なし"))
        self.repeat_series = QCheckBox(self.tr("同じシリーズ"))
        self.repeat_series.setToolTip(self.tr("同じシリーズの衛星（同じ軌道のコンステレーション）同士もペア化"))
        self.repeat_series.toggled.connect(self.show_view)
        self.repeat_bar = part(self.tr("同じ衛星（シリーズ）が同じ軌道（北行・南行、向き、入射角、方位）で戻ってきた 2 回"),
                               self.tr("入射角の差"), self.repeat_incidence, self.tr("以内・基線"), self.repeat_baseline, self.tr("以下"), self.repeat_series)
        self.revisit_grid = QSpinBox()
        self.revisit_grid.setRange(2, 30)
        self.revisit_grid.setValue(10)
        self.revisit_grid.setPrefix(self.tr("格子 "))
        self.revisit_grid.setSuffix(" ×")
        self.revisit_grid.valueChanged.connect(self.show_view)
        self.revisit_bar = part(self.tr("対象を囲む範囲をこの数 × この数のセルに分け、セルごとに撮像機会の間隔を算出"), self.revisit_grid)
        row.addStretch(1)
        return bar

    def pairing(self) -> Optional[str]:
        """The pair view shown: `stereo` or `repeat`; None in the other views."""
        v = self.view.currentData()
        return v if v in ("stereo", "repeat") else None

    def pairs(self) -> list:
        """The pairs of the last plan for the conditions in the form (found again when they change)."""
        plan, kind = self.plan, self.pairing()
        if not plan or not kind:
            return []
        if kind == "stereo":
            options = StereoOptions(self.stereo_min.value(), self.stereo_max.value(), self.stereo_days.value(), self.stereo_series.isChecked())
        else:
            baseline = self.repeat_baseline.value()
            options = RepeatOptions(self.repeat_incidence.value(), 2, baseline if baseline > 0 else None, self.repeat_series.isChecked())
        key = (kind, options, self.max_cloud_value())
        if self._pairs is None or self._pairs[0] != key:
            find = stereo_pairs if kind == "stereo" else repeat_pairs
            self._pairs = (key, find(self.candidates(), plan["sats"], plan["targets"], options))
        return self._pairs[1]

    def pair_columns(self) -> list[Column]:
        """The columns of a pair view: the second shot, how the two looks relate, and the days between."""
        sats, tr = self.plan["sats"], self.tr
        cols = [Column("second", tr("2 回目"), lambda op: f"{local_text(op.pair.b.time)} {satellite_label(sats[op.pair.b.satellite])}",
                       lambda op: tr("オフナディア角 {a}°・入射角 {i}°・{side}").format(a=js_round(op.pair.b.offNadir), i=js_round(op.pair.b.incidence), side=side_text(op.pair.b)))]
        if self.pairing() == "stereo":
            cols.append(Column("angle", tr("交会角"), lambda op: f"{js_round(op.pair.convergence)}°",
                               lambda op: tr("B/H {bh}（2 回の視線のなす角。大きいほど高さの精度が向上、大きすぎると対応づけが困難）").format(bh=js_round(op.pair.bh, 2))))
        else:
            cols.append(Column("baseline", tr("垂直基線"), lambda op: f"{math.floor(op.pair.baseline + 0.5):,} m",
                               lambda op: tr("入射角の差 {d}°（基線が短いほど干渉しやすく、長いほど地形の高さに敏感）").format(d=js_round(abs(op.pair.b.incidence - op.incidence), 2))))
        cols.append(Column("days", tr("間隔"), lambda op: hours_text(op.pair.days * 24), lambda op: tr("1 回目から 2 回目までの時間")))
        return cols

    def show_revisit(self) -> None:
        """The revisit map of the last plan: planned again over a grid of cells around the targets (in a task), then listed and drawn."""
        plan = self.plan
        size = self.revisit_grid.value()
        self.shown = []
        self.timeline.hide()
        self._update_buttons()
        if self.revisit is not None and self.revisit["size"] == size:
            self._revisit_ready()
            return
        self.revisit = None
        self.revisit_model.clear()
        self.overlay.unpaint()
        if self.revisit_task is not None:
            self.revisit_task.cancel()
        cells = grid_cells(box_of(plan["targets"]), size)
        sats = plan["sats"]
        options = replace(plan["options"], stations=[])
        what = self.tr("再訪間隔を {n} セルで計算中…").format(n=len(cells))
        self.status.setText(what)

        def work(task):
            def progress(done: float) -> None:
                task.check()
                task.progress(done * 100)

            return plan_access(sats, cell_targets(cells), options, progress)

        def done(result: PlanResult):
            if self.revisit_task is not task:
                return
            self.revisit_task = None
            if self.plan is not plan or self.view.currentData() != "revisit":
                return
            self.revisit = {"size": size, "cells": cells, "chosen": None,
                            "stats": revisit_stats(result.opportunities, len(cells), options.start, options.end)}
            self._revisit_ready()

        def failed(message: str):
            if self.revisit_task is not task:
                return
            self.revisit_task = None
            self.status.setText(self.tr("計算の取消") if message == "取消" else self.tr("計算に失敗: {m}").format(m=message))

        task = run_task(what, work, done, failed)
        self.revisit_task = task

    def _revisit_ready(self) -> None:
        revisit, plan = self.revisit, self.plan
        stats = revisit["stats"]
        self.render_revisit()
        self.draw_revisit()
        seen = [s for s in stats if s.count > 0]
        days = (plan["options"].end - plan["options"].start) / 86400000
        if seen:
            per_day = sum(s.count for s in stats) / len(stats) / days
            self.status.setText(self.tr("{n} セルのうち {seen} セルで撮像機会あり。最長の待ちは {worst}、平均で {per} 回/日（緑ほど早く撮像、赤ほど長い待ち）。行の選択でセルを表示").format(
                n=len(stats), seen=len(seen), worst=hours_text(max(s.maxGap for s in stats)), per=js_round(per_day, 2)))
        else:
            self.status.setText(self.tr("この期間、範囲内のどのセルにも撮像機会なし。期間の延長か条件の見直しが必要"))
        self._update_buttons()

    def render_revisit(self) -> None:
        """Lists the revisit map's cells, longest wait first."""
        revisit = self.revisit
        stats, cells = revisit["stats"], revisit["cells"]
        self.revisit_model.clear()
        self.revisit_model.setHorizontalHeaderLabels([self.tr("セル"), self.tr("回数"), self.tr("平均間隔"), self.tr("最長の待ち")])
        order = sorted(range(len(stats)), key=lambda i: (-stats[i].maxGap, i))
        for i in order[:MAX_ROWS]:
            s = stats[i]
            items = [QStandardItem(text) for text in (str(i + 1), str(s.count), hours_text(s.meanGap), hours_text(s.maxGap))]
            for item in items:
                item.setData(i, Qt.ItemDataRole.UserRole)
                item.setToolTip(self.tr("中心 {lat}, {lon}").format(lat=js_round(cells[i].center[1], 4), lon=js_round(cells[i].center[0], 4)))
            self.revisit_model.appendRow(items)

    def draw_revisit(self) -> None:
        """Draws the revisit map's cells, coloured by their longest wait."""
        revisit = self.revisit
        if revisit is None:
            return
        stats = revisit["stats"]
        seen = [s.maxGap for s in stats if s.count > 0]
        shortest, longest = (min(seen), max(seen)) if seen else (0, 0)
        # Every cell named only on a small grid (the viewer names up to 12 × 12; on the canvas the texts would overlap).
        many = len(revisit["cells"]) > 36
        polygons = []
        for i, (cell, s) in enumerate(zip(revisit["cells"], stats)):
            chosen = revisit["chosen"] == i
            label = "" if many and not chosen else (hours_text(s.maxGap) if s.count else self.tr("なし"))
            polygons.append(([cell.ring], QColor(*revisit_color(s, shortest, longest)).name(), 3 if chosen else 1, label))
        self.overlay.paint(polygons)
        self.canvas.refresh()

    def _cell_chosen(self, row: int) -> None:
        if self.revisit is None or row < 0:
            return
        index = self.revisit_model.index(row, 0)
        self.revisit["chosen"] = self.revisit_model.data(index, Qt.ItemDataRole.UserRole)
        self.draw_revisit()

    def zoom_to_cell(self) -> None:
        """Centres the map on the chosen cell."""
        revisit = self.revisit
        if revisit is None or revisit["chosen"] is None:
            return
        x0, y0 = revisit["cells"][revisit["chosen"]].ring[0]
        x1, y1 = revisit["cells"][revisit["chosen"]].ring[2]
        box = QgsRectangle(x0, y0, x1, y1)
        crs = self.canvas.mapSettings().destinationCrs()
        self.canvas.setCenter(from_wgs84(crs).transform(box.center()))
        self.canvas.refresh()

    def revisit_shown(self) -> bool:
        return self.revisit is not None and self.view.currentData() == "revisit"
