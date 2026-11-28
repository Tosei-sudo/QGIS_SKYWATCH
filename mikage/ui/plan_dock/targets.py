"""
撮像計画ドックの目標 (ui/plan_dock, docs/architecture.md): the targets: the layer and its features, the point picked or dragged on the map, their labels. A part of ImagingPlanDock
(ui/imaging_plan_dock.py): its methods as they were, on the dock's widgets and state.
"""
from __future__ import annotations

import re
from dataclasses import replace
from typing import Optional

from qgis.core import QgsFeatureRequest, QgsGeometry, QgsMapLayer, QgsPointXY, QgsProject, QgsVectorLayer

from ...core.plan.api import PlanTarget
from ...core.plan.api import (
    js_round, natural_key, percent,
)
from ...core.plan.api import shift_scene, track_place
from ...core.plan.api import MAX_TARGETS, name_of, priority_of, target_of
from ...qgis_io import map_tools
from ...qgis_io.geometry import WGS84, shape_of, to_wgs84
from .common import _plain


class TargetsPart:
    """撮像計画ドックの目標."""

    def _vector_layers(self) -> list[QgsVectorLayer]:
        return [l for l in QgsProject.instance().mapLayers().values()
                if l.type() == QgsMapLayer.LayerType.VectorLayer and l.isSpatial() and l.isValid()]

    def list_targets(self) -> None:
        """Fills the target choice: the selected features, each vector layer, a clicked point."""
        # The choice is kept, unless there was nothing to choose (a layer added then comes first).
        before = self.target.currentData() if self.target.count() > 1 else None
        self.target.blockSignals(True)
        self.target.clear()
        active = self.iface.activeLayer()
        if isinstance(active, QgsVectorLayer) and active.isSpatial() and active.selectedFeatureCount():
            self.target.addItem(self.tr("選択中の地物（{n}）").format(n=active.selectedFeatureCount()), "selection")
        for layer in sorted(self._vector_layers(), key=lambda l: l.name()):
            self.target.addItem(f"{layer.name()}（{layer.featureCount():,}）", f"layer:{layer.id()}")
        if self.moving:
            self.target.addItem(self.tr("移動目標: {label}（{n}）").format(label=self.moving["label"], n=len(self.moving["ships"])), "moving")
        self.target.addItem(self.tr("地図でクリックした地点"), "click")
        values = [self.target.itemData(i) for i in range(self.target.count())]
        self.target.setCurrentIndex(values.index(before) if before in values else 0)
        self.target.blockSignals(False)
        self._target_changed()

    def _layers_changed(self, *_args) -> None:
        if self.isVisible():
            self.list_targets()

    def _layer_changed(self, layer) -> None:
        if self._watched is not None:
            try:
                self._watched.selectionChanged.disconnect(self._selection_changed)
            except (TypeError, RuntimeError):
                pass
        self._watched = layer if isinstance(layer, QgsVectorLayer) else None
        if self._watched is not None:
            self._watched.selectionChanged.connect(self._selection_changed)
        self._selection_changed()

    def _selection_changed(self, *_args) -> None:
        if self.isVisible():
            self.list_targets()

    def _target_layer(self) -> Optional[QgsVectorLayer]:
        choice = self.target.currentData() or ""
        if choice == "selection":
            layer = self.iface.activeLayer()
            return layer if isinstance(layer, QgsVectorLayer) else None
        if choice.startswith("layer:"):
            layer = QgsProject.instance().mapLayer(choice[len("layer:"):])
            return layer if isinstance(layer, QgsVectorLayer) else None
        return None

    def _features(self, limit: Optional[int] = None) -> list:
        layer = self._target_layer()
        if layer is None:
            return []
        request = QgsFeatureRequest()
        if limit:
            request.setLimit(limit)
        if self.target.currentData() == "selection":
            # In feature id order (the selection itself has none).
            return sorted((f for f in layer.getSelectedFeatures(request) if f.hasGeometry()), key=lambda f: f.id())
        return [f for f in layer.getFeatures(request) if f.hasGeometry()]

    def _target_changed(self, *_args) -> None:
        click = self.target.currentData() == "click"
        self.pick.setVisible(click)
        if not click and self.pick.isChecked():
            self.pick.setChecked(False)
        layer = self._target_layer()
        names = [f.name() for f in layer.fields()] if layer is not None else []
        before = self.name_field.currentData()
        self.name_field.blockSignals(True)
        self.name_field.clear()
        self.name_field.addItem(self.tr("自動"), "")
        for i, n in enumerate(names):
            self.name_field.addItem(layer.attributeDisplayName(i), n)
        self.name_field.setCurrentIndex(max(0, self.name_field.findData(before)))
        self.name_field.blockSignals(False)
        self._list_priority_fields(layer)
        self.show_targets()

    def _list_priority_fields(self, layer: Optional[QgsVectorLayer]) -> None:
        """The attributes that hold numbers, offered as the priority when features are planned one by one."""
        before = self.priority.currentData()
        features = self._features(MAX_TARGETS) if layer is not None else []
        numeric: list[str] = []
        if features:
            names = layer.fields().names()
            for i, name in enumerate(names):
                if any(priority_of(_plain(f.attributes()[i])) is not None for f in features):
                    numeric.append(name)
        self.priority.clear()
        self.priority.addItem(self.tr("なし"), "")
        for name in sorted(numeric, key=natural_key):
            self.priority.addItem(layer.attributeDisplayName(layer.fields().indexOf(name)), name)
        index = self.priority.findData(before) if before else -1
        if index < 0:
            guess = next((n for n in numeric if re.search(r"priority|優先", n, re.IGNORECASE)), "")
            index = self.priority.findData(guess)
        self.priority.setCurrentIndex(max(0, index))
        self._priority_shown = self.each.isChecked() and len(features) > 1 and bool(numeric)
        self._hide_rows()

    def _hide_rows(self) -> None:
        """Shows the rows of 「詳細条件」 that apply: the priority with features to rank, the forecast with a forecast service."""
        for w in (self.priority, self.priority_label, self.priority_order, self.priority_order_label):
            w.setVisible(self._priority_shown)
        forecast = self.context.config.cloud_forecast is not None
        self.cloud_row.setVisible(forecast)
        self.cloud_label.setVisible(forecast)

    def targets(self) -> list[PlanTarget] | str:
        """The targets the form asks for (in WGS84), or the reason there are none (as the viewer)."""
        choice = self.target.currentData()
        if choice == "moving":
            ships = self.ships()
            return [PlanTarget(s.name, (s.lon, s.lat)) for s in ships] if ships else self.tr("移動目標なし")
        if choice == "click":
            if not self.clicked:
                return self.tr("地図をクリックして地点を指定")
            lon, lat = self.clicked
            return [PlanTarget(f"{js_round(lat, 5)}, {js_round(lon, 5)}", (lon, lat))]
        layer = self._target_layer()
        if layer is None:
            return self.tr("対象のレイヤーなし")
        name = self.tr("選択中の地物") if choice == "selection" else layer.name()
        each = self.each.isChecked()
        count = layer.selectedFeatureCount() if choice == "selection" else layer.featureCount()
        if each and count > MAX_TARGETS:
            return self.tr("地物 {n} 件。{max} 件以下を選択するか、「詳細条件」の「地物ごとに計算」を外して一括計算").format(n=f"{count:,}", max=MAX_TARGETS)
        features = self._features()
        if not features:
            return self.tr("対象に図形なし")
        transform = to_wgs84(layer.crs())
        field = self.name_field.currentData() or ""
        names = layer.fields().names()

        def attributes(f) -> dict:
            return dict(zip(names, f.attributes()))

        if not each or len(features) == 1:
            shapes = [s for s in (shape_of(f.geometry(), transform) for f in features) if s]
            label = name_of(attributes(features[0]), features[0].id(), 0, field) if len(features) == 1 else name
            t = target_of(shapes, label)
            return [t] if t else self.tr("対象に図形なし")
        priority = self.priority.currentData() if self._priority_shown else ""
        sign = 1 if self.priority_order.currentData() == "large" else -1
        out = []
        for i, f in enumerate(features):
            shape = shape_of(f.geometry(), transform)
            t = target_of([shape], name_of(attributes(f), f.id(), i, field)) if shape else None
            if not t:
                continue
            value = _plain(f[priority]) if priority else None
            v = priority_of(value)
            out.append(t if v is None else replace(t, priority=sign * v, priority_text=str(value)))
        return out or self.tr("対象に図形なし")

    def show_targets(self) -> None:
        """Draws the targets (and the chosen pass) on the map."""
        if self.target.currentData() == "click":
            self.overlay.clear(["target", "target_point"])
            if self.clicked:
                self.overlay.point_target(*self.clicked)
        elif self.target.currentData() == "moving":
            # Each moving target where it was last reported.
            ships = self.ships() or []
            self.overlay.targets([QgsGeometry.fromPointXY(QgsPointXY(s.lon, s.lat)) for s in ships], WGS84)
        else:
            layer = self._target_layer()
            features = self._features(MAX_TARGETS) if layer is not None else []
            self.overlay.targets([f.geometry() for f in features], layer.crs() if layer else WGS84)
        if self.chosen is not None and self.plan:
            self.draw_pass(self.chosen)

    def _move_toggled(self, on: bool) -> None:
        if on:
            if self.pick.isChecked():
                self.pick.setChecked(False)
            map_tools.take(self.canvas, self.drag_tool)
        else:
            map_tools.give_back(self.canvas, self.drag_tool)

    def drag_begin(self, lon: float, lat: float) -> bool:
        """Starts moving the chosen pass's scenes from a place on them."""
        op, plan = self.chosen, self.plan
        if op is None or not plan:
            return False
        start = track_place(plan["sats"][op.satellite], op.time, (lon, lat))
        if start is None:
            return False
        self._drag = (op, replace(op), start)
        return True

    def drag_move(self, lon: float, lat: float) -> None:
        if self._drag is None or not self.plan:
            return
        op, origin, start = self._drag
        plan = self.plan
        sat = plan["sats"][op.satellite]
        at = track_place(sat, op.time, (lon, lat))
        if at is None:
            return
        o = plan["options"]
        moved = shift_scene(origin, sat, plan["targets"][op.target], at[0] - start[0], at[1] - start[1],
                            o.maxOffNadir, o.minIncidence, o.maxIncidence, o.side)
        # The opportunity itself changes, so the list, the CSV and the layer follow.
        for name, value in vars(moved).items():
            setattr(op, name, value)
        self.draw_pass(op)

    def drag_end(self) -> None:
        drag, self._drag = self._drag, None
        if drag is None or not self.plan:
            return
        op = drag[0]
        self.render()
        cover = self.tr("被覆 {p}").format(p=percent(op.coverage)) if op.cells is not None else (self.tr("対象を含む") if op.coverage else self.tr("対象外"))
        self.status.setText(self.tr("撮像範囲を移動: オフナディア角 {a}°・入射角 {i}°・{cover}").format(
            a=js_round(op.offNadir), i=js_round(op.incidence), cover=cover))
        self.chosenChanged.emit()

    def _pick_toggled(self, on: bool) -> None:
        if on:
            if self.move_button.isChecked():
                self.move_button.setChecked(False)
            map_tools.take(self.canvas, self.map_tool)
        else:
            map_tools.give_back(self.canvas, self.map_tool)

    def _map_clicked(self, point: QgsPointXY, _button=None) -> None:
        p = to_wgs84(self.canvas.mapSettings().destinationCrs()).transform(point)
        self.set_point(p.x(), p.y())
        self.pick.setChecked(False)

    def set_point(self, lon: float, lat: float) -> None:
        """Makes a point (WGS84) the target."""
        self.clicked = (lon, lat)
        index = self.target.findData("click")
        if index >= 0 and self.target.currentIndex() != index:
            self.target.setCurrentIndex(index)
        self.show_targets()
        self.status.setText(self.tr("地点 {lat}, {lon} を対象に設定。「計算」で撮像機会を検索").format(lat=js_round(lat, 5), lon=js_round(lon, 5)))

    def plan_at(self, lon: float, lat: float) -> None:
        """Opens the dock with a point (WGS84) as the target and plans it (the map's right-click menu)."""
        self.show()
        self.raise_()
        self.list_targets()
        self.set_point(lon, lat)
        self.run()

    def _relabel(self) -> None:
        """Names the targets of the last plan again by the chosen attribute, when the form still asks for the same targets."""
        plan = self.plan
        if not plan:
            return
        targets = self.targets()
        if isinstance(targets, str) or len(targets) != len(plan["targets"]) or any(t.center != p.center for t, p in zip(targets, plan["targets"])):
            return
        plan["targets"] = [replace(p, label=t.label) for t, p in zip(targets, plan["targets"])]
        self.render()
