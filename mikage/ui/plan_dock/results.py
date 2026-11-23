"""
撮像計画ドックの結果の一覧 (ui/plan_dock, docs/architecture.md): the opportunities found: the view, the columns, the rows, the timeline and the pass drawn. A part of ImagingPlanDock
(ui/imaging_plan_dock.py): its methods as they were, on the dock's widgets and state.
"""
from __future__ import annotations

import math
from dataclasses import replace

from qgis.PyQt.QtCore import Qt

from ...core.plan.api import Opportunity
from ...core.plan.api import cover_plan
from ...core.plan.api import (
    NO_OPPORTUNITY, cover_text, cover_title, describe_problems, first_only, is_old_tle, js_round, local_text, mode_facts,
    percent, side_text, sorted_ops, summary_all, summary_cover,
)
from ...core.plan.api import (
    hours_text,
)
from ...core.plan.api import satellite_label
from ...core.plan.api import pass_shapes
from ...qgis_io.geometry import from_wgs84
from .common import Column


class ResultsPart:
    """撮像計画ドックの結果の一覧."""

    def combining(self) -> bool:
        return self.view.currentData() == "cover"

    def cover_plans(self) -> list:
        plan = self.plan
        goal = min(1.0, max(0.1, self.goal.value() / 100))
        share = self.share.isChecked()
        top = self.max_cloud_value()
        if self.cover is None or self.cover[:3] != (goal, share, top):
            self.cover = (goal, share, top, cover_plan(self.candidates(), plan["targets"], plan["sats"], goal, 0.02, share))
        return self.cover[3]

    def show_view(self) -> None:
        """Lists the last plan in the chosen view, with what it found in the status line."""
        plan = self.plan
        if not plan:
            return
        combining = self.combining()
        pairing = self.pairing()
        revisit = self.view.currentData() == "revisit"
        self.goal.setVisible(combining)
        self.share.setVisible(combining and sum(1 for t in plan["targets"] if not t.cells) >= 2)
        self.first.setVisible(not combining and not pairing and not revisit)
        self.timeline_box.setVisible(not revisit)
        self.stereo_bar.setVisible(pairing == "stereo")
        self.repeat_bar.setVisible(pairing == "repeat")
        self.revisit_bar.setVisible(revisit)
        self.table.setVisible(not revisit)
        self.revisit_table.setVisible(revisit)
        self.chosen = None
        if revisit:
            self.overlay.clear(["track", "reach", "scene", "step", "label"])
            self.show_targets()
            self.show_revisit()
            return
        self.overlay.unpaint()
        if not any(c.key == self.sort[0] for c in self.columns()):
            self.sort = ("time", False)
        self.render()
        result, sats, targets = plan["result"], plan["sats"], plan["targets"]
        problems = describe_problems(result.problems, result.tooSoon, result.outaged)
        if not result.opportunities:
            self.status.setText(self.tr(NO_OPPORTUNITY) + problems)
        elif pairing:
            pairs = self.pairs()
            what = self.tr("ステレオペア") if pairing == "stereo" else self.tr("干渉ペア")
            if pairs:
                first = pairs[0].b if pairing == "stereo" else pairs[0].a
                self.status.setText(self.tr("{what} {n} 組。最初の 2 回目は {time}。行を選ぶと 1 回目（青）と 2 回目（番号 2）の撮像範囲を地図に表示").format(
                    what=what, n=f"{len(pairs):,}", time=local_text(first.time)) + problems)
            else:
                hint = (self.tr("交会角・日数の条件の緩和、またはオフナディア角の上限の引き上げが必要") if pairing == "stereo"
                        else self.tr("SAR 衛星を選び、期間を回帰日数より長くするか、入射角の差の条件の緩和が必要"))
                self.status.setText(self.tr("条件に合う{what}なし。{hint}").format(what=what, hint=hint) + problems)
        elif not combining:
            self.status.setText(summary_all(result.opportunities, sats, problems))
        else:
            self.status.setText(summary_cover(self.cover_plans(), targets, problems))
        self.show_targets()
        if self.shown:
            self.choose(self.shown[0])
        else:
            self.overlay.clear(["track", "reach", "scene", "step", "label"])

    def columns(self) -> list[Column]:
        plan = self.plan
        sats, targets, options = plan["sats"], plan["targets"], plan["options"]
        many = len(targets) > 1
        tr = self.tr

        def time_title(op: Opportunity) -> str:
            sat = sats[op.satellite]
            parts = [tr("発注期限 {t}").format(t=local_text(op.time - sat.leadTime * 3600000)) if sat.leadTime > 0 else "",
                     tr("撮像範囲を地図で移動済み（✎）") if op.adjusted else ""]
            return "・".join(x for x in parts if x)

        def satellite_title(op: Opportunity) -> str:
            sat = sats[op.satellite]
            text = tr("{source}{mode}・TLE {days} 日").format(source=sat.source, mode=mode_facts(sat), days=js_round(op.tleAge, 0))
            return text + (tr("（古い TLE。予測がずれる場合あり）") if is_old_tle(op, sat) else "")

        def downlink_title(op: Opportunity) -> str:
            if not op.downlink:
                return tr("撮像から 24 時間以内の地上局上空の通過なし")
            return tr("{station}・撮像から {minutes} 分（衛星が地上局の上に来る時刻。データの処理・配信の時間は含まず）").format(
                station=options.stations[op.downlink[1]].name, minutes=math.floor((op.downlink[0] - op.time) / 60000 + 0.5))

        cols = [
            Column("time", tr("日時"), lambda op: local_text(op.time) + (" ✎" if op.adjusted else ""), time_title),
            Column("satellite", tr("衛星"), lambda op: satellite_label(sats[op.satellite]), satellite_title),
        ]
        if many:
            cols.append(Column("target", tr("対象"), lambda op: targets[op.target].label))
        if many and any(t.priority is not None for t in targets):
            cols.append(Column("priority", tr("優先度"), lambda op: targets[op.target].priority_text or "",
                               lambda op: tr("対象の優先度（属性の値）。並べ替えると優先度の高い順")))
        if any(t.uncertainty is not None for t in targets):
            cols.append(Column(
                "uncertainty", tr("位置の誤差"), lambda op: f"±{js_round(targets[op.target].uncertainty or 0)} km",
                lambda op: tr("最後の報告から {h} 後の推測位置（針路・速力から）。誤差の円のうち撮像範囲に入る割合が被覆").format(
                    h=hours_text((targets[op.target].guessed or 0) / 3600000))))
        cols += [
            Column("offNadir", tr("オフナディア角"), lambda op: f"{js_round(op.offNadir)}°",
                   lambda op: tr("入射角 {i}°・距離 {d} km").format(i=js_round(op.incidence), d=js_round(op.range, 0))),
            Column("side", tr("方向"), side_text, lambda op: tr("衛星の進行方向に対して対象が右か左か、北行（昇交）か南行（降交）か")),
            Column("sun", tr("太陽高度"), lambda op: f"{js_round(op.sunElevation, 0)}°"),
        ]
        if any(op.cloud is not None for op in plan["result"].opportunities):
            label = self.context.config.cloud_forecast.label if self.context.config.cloud_forecast else ""
            cols.append(Column("cloud", tr("雲量"), lambda op: "—" if op.cloud is None else f"{js_round(op.cloud, 0)}%",
                               lambda op: (tr("SAR は雲を通して撮像") if sats[op.satellite].sar else tr("予報の範囲外（16 日先まで）")) if op.cloud is None
                               else tr("{label} の雲量予報").format(label=label)))
        if options.stations:
            cols.append(Column("downlink", tr("受信見込み"), lambda op: local_text(op.downlink[0]) if op.downlink else tr("1 日以内なし"), downlink_title))
        if self.pairing():
            cols += self.pair_columns()
        elif self.combining():
            cols += [
                Column("gain", tr("追加"), lambda op: f"+{percent(op.gain or 0)}", lambda op: tr("この回で新たに撮像できる対象の割合")),
                Column("cumulative", tr("累計"), lambda op: percent(op.cumulative or 0), lambda op: tr("この回までに撮像できた対象の割合")),
            ]
            if many:
                cols.append(Column(
                    "shot", tr("シーン"),
                    lambda op: f"#{'' if op.shot is None else op.shot}" + (tr("（{n} 地点）").format(n=op.shotTargets) if (op.shotTargets or 1) > 1 else ""),
                    lambda op: tr("この {n} 地点は 1 回の撮像（1 シーン）でまとめて撮像できる").format(n=op.shotTargets) if (op.shotTargets or 1) > 1 else ""))
        else:
            cols.append(Column("cover", tr("被覆"), cover_text, lambda op: cover_title(op, sats[op.satellite])))
        return cols

    def sorted(self) -> list[Opportunity]:
        plan = self.plan
        if self.pairing():
            ops = [replace(pair.a, pair=pair) for pair in self.pairs()]
        elif self.combining():
            ops = [s for p in self.cover_plans() for s in p.steps]
        else:
            ops = self.candidates()
        if not self.combining() and not self.pairing() and self.first.isChecked():
            ops = first_only(ops)
        key, descending = self.sort
        return sorted_ops(ops, key, descending, plan["sats"], plan["targets"])

    def render(self) -> None:
        if not self.plan:
            return
        columns = self.columns()
        self.shown = self.sorted()
        self.model.set(columns, self.shown, self.plan["sats"])
        key, descending = self.sort
        index = next((i for i, c in enumerate(columns) if c.key == key), -1)
        self.table.horizontalHeader().setSortIndicator(index, Qt.SortOrder.DescendingOrder if descending else Qt.SortOrder.AscendingOrder)
        self._select_chosen()
        self._update_buttons()
        self.draw_timeline()

    def draw_timeline(self) -> None:
        """Draws the timeline of the listed shots (when it is asked for)."""
        plan = self.plan
        on = self.timeline_box.isChecked() and bool(plan) and bool(self.shown)
        self.timeline.setVisible(on)
        if not on:
            return
        many = len(plan["targets"]) > 1

        def title(op: Opportunity) -> str:
            target = f" → {plan['targets'][op.target].label}" if many else ""
            return self.tr("{time} {name}{target}・オフナディア角 {angle}°").format(
                time=local_text(op.time), name=plan["sats"][op.satellite].name, target=target, angle=js_round(op.offNadir))

        self.timeline.set(self.shown, plan["sats"], plan["options"].start, plan["options"].end, self.chosen, title)

    def _sort_by(self, section: int) -> None:
        columns = self.model.columns
        if section >= len(columns):
            return
        key = columns[section].key
        self.sort = (key, not self.sort[1] if self.sort[0] == key else False)
        self.render()

    def _row_chosen(self, row: int) -> None:
        op = self.model.op_at(row)
        if op is not None and op is not self.chosen:
            self.choose(op)

    def _select_chosen(self) -> None:
        if self.chosen is None:
            return
        row = next((i for i, op in enumerate(self.model.ops) if op is self.chosen), -1)
        if row >= 0:
            self.table.selectRow(row)
            self.table.scrollTo(self.model.index(row, 0))

    def choose(self, op: Opportunity) -> None:
        """Chooses an opportunity: its row, and its pass on the map."""
        self.chosen = op
        self._select_chosen()
        self.draw_pass(op)
        self._update_buttons()
        if self.timeline.isVisible():
            self.timeline.choose(op)
        self.chosenChanged.emit()

    def draw_pass(self, op: Opportunity) -> None:
        plan = self.plan
        sat = plan["sats"][op.satellite]
        shapes = pass_shapes(sat, plan["targets"][op.target], op)
        self._scenes = shapes.scenes if shapes else []
        if shapes is None:
            return
        steps = []
        if op.pair is not None:
            # The pair's second shot, numbered 2.
            second = pass_shapes(plan["sats"][op.pair.b.satellite], plan["targets"][op.pair.b.target], op.pair.b)
            steps.append((2, second.scenes if second else []))
        elif self.combining():
            for i, step in enumerate(self.cover_plans()[op.target].steps):
                if step.time == op.time and step.satellite == op.satellite:
                    continue
                other = pass_shapes(plan["sats"][step.satellite], plan["targets"][step.target], step)
                steps.append((i + 1, other.scenes if other else []))
        target = plan["targets"][op.target]
        # A moving target: the circle its guessed place may be off by, at the moment of the pass.
        circle = [*target.points, target.points[0]] if target.uncertainty is not None and len(target.points) > 2 else None
        self.overlay.chosen(shapes, f"{satellite_label(sat)} {local_text(op.time)[5:]}", steps, circle)
        self.canvas.refresh()

    def zoom_to_chosen(self) -> None:
        """Fits the map to the chosen opportunity's target, scenes and satellite (or the combination's passes)."""
        op = self.chosen
        if op is None or not self.plan:
            return
        # A combination shows the target and all its passes; one pass, also where the satellite is.
        if self.combining():
            box = self.overlay.extent(["scene", "step"])
        else:
            # A pair shows its second shot too.
            box = self.overlay.extent(["scene", "satellite", "step"])
        if box is None:
            return
        t = self.plan["targets"][op.target]
        for x, y in [t.center, *t.points]:
            box.combineExtentWith(x, y)
        box = box.buffered(max(box.width(), box.height()) * 0.1 + 0.01)
        canvas_crs = self.canvas.mapSettings().destinationCrs()
        self.canvas.setExtent(from_wgs84(canvas_crs).transformBoundingBox(box))
        self.canvas.refresh()

    def _rings_of(self, op: Opportunity) -> list:
        plan = self.plan
        shapes = pass_shapes(plan["sats"][op.satellite], plan["targets"][op.target], op)
        return [[list(p) for p in ring] for ring in shapes.scenes] if shapes else []
