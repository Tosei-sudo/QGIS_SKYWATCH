"""
「クイック幾何補正」 (qgis_io/geo_fix_io.py does the correction): the active image
is put where it really is on the ground, for the terrain with the DEM
(`dem`) and for what is left with landmarks: drag from a landmark in the
image to where it really is on the background map (the image goes faint
while dragging, so the map shows through), and the image is corrected at
once. 1 point: a shift; 2: shift, rotation and scale; 3 or more: an affine.
Ctrl+Z takes the last point back, Esc puts the tool away.

The panel shows what corrects the image (the terrain: the satellite's
direction and incidence angle, from the file or typed; the points and their
residuals), keeps the correction for the image (「保存」: shared in the Esri
table `imageCorrections`, else on this PC; anyone opening the image gets it)
and, after a save, offers to move the image's records made before so that
they lie where the corrected image shows them (by their pixels; one without
pixels from where the file showed it).
"""
from __future__ import annotations

import math
from dataclasses import replace
from typing import Callable, Optional

from qgis.core import QgsCoordinateReferenceSystem, QgsCoordinateTransform, QgsPointXY, QgsProject, QgsRasterLayer, QgsWkbTypes
from qgis.gui import QgsMapTool, QgsRubberBand, QgsVertexMarker
from qgis.PyQt import sip
from qgis.PyQt.QtCore import QCoreApplication, QObject, Qt
from qgis.PyQt.QtGui import QColor
from qgis.PyQt.QtWidgets import (
    QCheckBox, QDialog, QDoubleSpinBox, QFormLayout, QHBoxLayout, QHeaderView, QLabel, QMessageBox, QPushButton, QTableWidget,
    QTableWidgetItem, QVBoxLayout, QWidget,
)

from ..core.geo_fix import MAX_POINTS, GeoFix, TiePoint, compass, kind_name
from ..qgis_io import geo_fix_io, map_tools
from ..qgis_io.geo_fix_keeper import FixKeeper, image_key
from ..qgis_io.pixel_place import cached_placement, original_placement
from ..qgis_io.shift_io import can_shift, original_source
from .fit import fit_text

ENTRY = "クイック幾何補正"
TIP = ("画像を地形（DEM）と目印の点で正しい位置に合わせる。画像の目印から背景地図の正しい位置へドラッグ（1 点で平行移動、"
       "2 点で回転・縮尺、3 点以上でアフィン）。ファイルは変えない。保存すると、誰が画像を開いても補正される")
WGS84 = QgsCoordinateReferenceSystem("EPSG:4326")
#: The image's opacity while a point is dragged (the map shows through).
FAINT = 0.4


def tr(text: str) -> str:
    return QCoreApplication.translate("Mikage", text)


class FixPointTool(QgsMapTool):
    """Drag from a landmark in the image to where it really is; Ctrl+Z the last point back, Esc away."""

    def __init__(self, canvas, controller: "LayerFix"):
        super().__init__(canvas)
        self.controller = controller
        self._start: Optional[QgsPointXY] = None
        self._line = QgsRubberBand(canvas, QgsWkbTypes.GeometryType.LineGeometry)
        self._line.setColor(QColor("#dc2626"))
        self._line.setWidth(2)
        self.setCursor(Qt.CursorShape.CrossCursor)

    def canvasPressEvent(self, event) -> None:  # noqa: N802 (QGIS's name)
        if event.button() != Qt.MouseButton.LeftButton or self.controller.layer is None:
            return
        self._start = self.toMapCoordinates(event.pos())
        self.controller.faint(True)
        self._line.reset(QgsWkbTypes.GeometryType.LineGeometry)
        self._line.addPoint(self._start)
        self._line.addPoint(self._start)

    def canvasMoveEvent(self, event) -> None:  # noqa: N802
        if self._start is not None:
            self._line.movePoint(self.toMapCoordinates(event.pos()))

    def canvasReleaseEvent(self, event) -> None:  # noqa: N802
        if self._start is None:
            return
        start, self._start = self._start, None
        end = self.toMapCoordinates(event.pos())
        self._line.reset(QgsWkbTypes.GeometryType.LineGeometry)
        self.controller.faint(False)
        if start != end:
            self.controller.add_point(start, end)

    def keyPressEvent(self, event) -> None:  # noqa: N802
        key = event.key()
        if key == Qt.Key.Key_Escape:
            self.controller.set_active(False)
        elif key == Qt.Key.Key_Z and event.modifiers() & Qt.KeyboardModifier.ControlModifier:
            self.controller.undo()
        else:
            event.ignore()
            return
        event.accept()

    def deactivate(self) -> None:
        self._line.reset(QgsWkbTypes.GeometryType.LineGeometry)
        if self._start is not None:
            self._start = None
            self.controller.faint(False)
        super().deactivate()

    def close(self) -> None:
        scene = self.canvas().scene()
        if scene is not None and not sip.isdeleted(self._line):
            scene.removeItem(self._line)


class GeoFixPanel(QDialog):
    """What corrects the image, its points, saving and taking off."""

    def __init__(self, controller: "LayerFix", parent: Optional[QWidget] = None):
        super().__init__(parent)
        self.controller = controller
        self.setObjectName("SatObsGeoFixPanel")
        self.setWindowTitle(tr(ENTRY))
        self.setWindowFlag(Qt.WindowType.Tool, True)
        self.setModal(False)
        fit_text(self)
        layout = QVBoxLayout(self)
        self.image = QLabel()
        self.image.setWordWrap(True)
        layout.addWidget(self.image)
        self.saved = QLabel()
        self.saved.setWordWrap(True)
        self.saved.setStyleSheet("color: #475569")
        layout.addWidget(self.saved)
        # The terrain.
        self.terrain = QCheckBox(tr("地形（DEM）で補正"))
        self.terrain.toggled.connect(lambda on: controller.set_terrain(on))
        layout.addWidget(self.terrain)
        self.dem = QLabel()
        self.dem.setWordWrap(True)
        self.dem.setStyleSheet("color: #475569")
        layout.addWidget(self.dem)
        self.look = QLabel()
        self.look.setWordWrap(True)
        layout.addWidget(self.look)
        self.typed = QCheckBox(tr("撮像方向を入力する"))
        self.typed.toggled.connect(self._typed_toggled)
        layout.addWidget(self.typed)
        form = QFormLayout()
        self.toward = QDoubleSpinBox()
        self.toward.setRange(0, 359.9)
        self.toward.setDecimals(1)
        self.toward.setSuffix("°")
        self.toward.setToolTip(tr("地上から見た衛星の方角（北から時計回り）。右向き観測の上昇軌道なら西寄り（約 260°）、下降軌道なら東寄り（約 100°）"))
        self.incidence = QDoubleSpinBox()
        self.incidence.setRange(5, 85)
        self.incidence.setDecimals(1)
        self.incidence.setSuffix("°")
        self.incidence.setValue(35)
        self.incidence.setToolTip(tr("入射角（鉛直から測る）"))
        form.addRow(tr("衛星の方角"), self.toward)
        form.addRow(tr("入射角"), self.incidence)
        self.typed_box = QWidget()
        self.typed_box.setLayout(form)
        layout.addWidget(self.typed_box)
        for box in (self.toward, self.incidence):
            box.editingFinished.connect(self._typed_changed)
        # The points.
        hint = QLabel(tr("地図で、画像の目印（岸壁の角・橋など）から正しい位置へドラッグ。Ctrl+Z で 1 点戻す、Esc で道具を終了"))
        hint.setWordWrap(True)
        layout.addWidget(hint)
        self.table = QTableWidget(0, 3)
        self.table.setHorizontalHeaderLabels([tr("点"), tr("残差"), ""])
        self.table.verticalHeader().setVisible(False)
        self.table.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        self.table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self.table.setMinimumHeight(110)
        layout.addWidget(self.table)
        self.report = QLabel()
        self.report.setWordWrap(True)
        layout.addWidget(self.report)
        buttons = QHBoxLayout()
        self.save = QPushButton(tr("保存"))
        self.save.clicked.connect(lambda _on=False: controller.save())
        self.off = QPushButton(tr("補正を外す"))
        self.off.setToolTip(tr("この画面だけ、ファイルの位置に戻す（保存した補正は残る）"))
        self.off.clicked.connect(lambda _on=False: controller.take_off())
        self.records = QPushButton(tr("記録を合わせる…"))
        self.records.setToolTip(tr("この画像の記録を、補正した画像が示す位置へ動かす"))
        self.records.clicked.connect(lambda _on=False: controller.offer_records(asked=True))
        self.forget = QPushButton(tr("保存した補正を削除"))
        self.forget.clicked.connect(lambda _on=False: controller.delete_saved())
        for b in (self.save, self.off, self.records, self.forget):
            buttons.addWidget(b)
        layout.addLayout(buttons)
        self.resize(420, 520)

    def _typed_toggled(self, on: bool) -> None:
        self.typed_box.setEnabled(on)
        self.controller.set_look(self.toward.value(), self.incidence.value()) if on else self.controller.set_look(None, None)

    def _typed_changed(self) -> None:
        if self.typed.isChecked():
            self.controller.set_look(self.toward.value(), self.incidence.value())

    def show_state(self, layer, fix: Optional[GeoFix], report: dict, saved: Optional[GeoFix], dem_text: str, look_text: str,
                   dirty: bool, where: str, hint_toward: Optional[float]) -> None:
        """Everything shown, from the controller's state."""
        self.image.setText(tr("画像: {name}").format(name=layer.name()) if layer is not None else
                           tr("ファイルの画像を選ぶと補正できる"))
        if saved is not None:
            when = ""
            if saved.at:
                from datetime import datetime

                when = datetime.fromtimestamp(saved.at / 1000).strftime("%m/%d %H:%M")
            text = tr("保存済み（{where}、{by} {when}）").format(where=where, by=saved.by or tr("不明"), when=when)
            if dirty:
                text += tr(" — 変更あり（未保存）")
        else:
            text = tr("未保存（保存先: {where}）").format(where=where) if fix is not None and not fix.is_empty() else ""
        self.saved.setText(text)
        enabled = layer is not None
        for w in (self.terrain, self.typed, self.table, self.save, self.off, self.records):
            w.setEnabled(enabled)
        self.forget.setEnabled(enabled and saved is not None)
        self.terrain.blockSignals(True)
        self.terrain.setChecked(bool(fix.terrain) if fix is not None else False)
        self.terrain.blockSignals(False)
        self.dem.setText(dem_text)
        self.look.setText(look_text)
        typed = fix is not None and fix.typed_look
        self.typed.blockSignals(True)
        self.typed.setChecked(typed)
        self.typed.blockSignals(False)
        self.typed_box.setEnabled(typed)
        if typed:
            self.toward.setValue(float(fix.toward))
            self.incidence.setValue(float(fix.incidence))
        elif hint_toward is not None:
            self.toward.setValue(round(hint_toward, 1))
        points = list(fix.points) if fix is not None else []
        residuals = list(report.get("residuals") or [])
        self.table.setRowCount(len(points))
        for i, _p in enumerate(points):
            self.table.setItem(i, 0, QTableWidgetItem(str(i + 1)))
            value = residuals[i] if i < len(residuals) else None
            self.table.setItem(i, 1, QTableWidgetItem(f"{value:.1f} m" if value is not None and len(points) >= 3 else "—"))
            remove = QPushButton(tr("削除"))
            remove.clicked.connect(lambda _on=False, i=i: self.controller.remove_point(i))
            self.table.setCellWidget(i, 2, remove)
        lines = [report.get("text") or ""] + list(report.get("notes") or [])
        if points:
            lines.append(tr("方式: {kind}").format(kind=kind_name(len(points))))
        self.report.setText("\n".join(line for line in lines if line))

    def closeEvent(self, event) -> None:  # noqa: N802 (Qt's name)
        self.controller.set_active(False)
        super().closeEvent(event)


class LayerFix(QObject):
    """The entry's work: the active image, its correction being made, the tool, the panel and the markers."""

    def __init__(self, iface, action, keeper: FixKeeper, observations: Callable[[], object] = lambda: None,
                 parent: Optional[QObject] = None):
        super().__init__(parent)
        self.iface = iface
        self.canvas = iface.mapCanvas()
        self.action = action
        self.keeper = keeper
        self.observations = observations
        self.active = False
        self.layer: Optional[QgsRasterLayer] = None
        #: The correction being made for the layer, and whether it differs from the one kept.
        self.fix: Optional[GeoFix] = None
        self.dirty = False
        self._opacity: Optional[float] = None
        self._markers: list = []
        self.tool = FixPointTool(self.canvas, self)
        self.panel: Optional[GeoFixPanel] = None
        if action is not None:
            action.setCheckable(True)
            action.setToolTip(tr(TIP))
            action.setStatusTip(tr(TIP))
            action.toggled.connect(self.set_active)
        self.canvas.mapToolSet.connect(self._tool_set)
        iface.currentLayerChanged.connect(self._current_changed)
        keeper.changed.connect(self._layer_changed)
        keeper.read.connect(self._kept_changed)
        # The image worked on removed (closed, or the catalog opening the next in its place): forgotten.
        QgsProject.instance().layersWillBeRemoved.connect(self._removing)

    # ---- on and off ----

    def set_active(self, active: bool) -> None:
        if active == self.active:
            return
        self.active = active
        if self.action is not None and self.action.isChecked() != active:
            self.action.blockSignals(True)
            self.action.setChecked(active)
            self.action.blockSignals(False)
        if active:
            map_tools.take(self.canvas, self.tool)
            if self.panel is None:
                self.panel = GeoFixPanel(self, self.iface.mainWindow())
            self.take(self.iface.activeLayer())
            self.panel.show()
            self.panel.raise_()
        else:
            self.faint(False)
            self._clear_markers()
            map_tools.give_back(self.canvas, self.tool)
            if self.panel is not None and self.panel.isVisible():
                self.panel.blockSignals(True)
                self.panel.hide()
                self.panel.blockSignals(False)
            if self.dirty and self.layer is not None and not sip.isdeleted(self.layer):
                self.say(tr("{name} の補正は未保存（表示は補正したまま。「保存」で残る）").format(name=self.layer.name()))

    def _tool_set(self, new, old=None) -> None:
        if self.active and new is not self.tool:
            self.set_active(False)

    def _current_changed(self, layer) -> None:
        if self.active and isinstance(layer, QgsRasterLayer) and layer is not self.layer:
            self.take(layer)

    def _layer_changed(self, layer_id: str) -> None:
        if self.layer is not None and not sip.isdeleted(self.layer) and self.layer.id() == layer_id:
            self.show_state()

    def _kept_changed(self, _ids) -> None:
        self.show_state()

    def _removing(self, layer_ids) -> None:
        layer = self.layer
        if layer is not None and (sip.isdeleted(layer) or layer.id() in set(layer_ids)):
            self._forget_layer()
            self.show_state()

    def _forget_layer(self) -> None:
        self._clear_markers()
        self.layer, self.fix, self.dirty = None, None, False
        self._opacity = None

    def _alive(self) -> Optional[QgsRasterLayer]:
        """The layer worked on; None once it is gone (removed from the project), which is then forgotten."""
        layer = self.layer
        if layer is not None and sip.isdeleted(layer):
            self._forget_layer()
            return None
        return layer

    # ---- the layer and its correction ----

    def take(self, layer) -> None:
        """Works on a layer (the active one): its correction shown, else the one kept, else a new one."""
        self._clear_markers()
        if not isinstance(layer, QgsRasterLayer) or not can_shift(layer):
            self.layer, self.fix, self.dirty = None, None, False
            self.show_state()
            return
        self.layer = layer
        image_id, image_file = image_key(layer)
        shown = geo_fix_io.fix_of(layer)
        kept = self.keeper.known(image_id, image_file)
        if shown is not None:
            self.fix = replace(shown, image_id=image_id, image_file=image_file)
            self.dirty = kept is None or kept.to_json() != shown.to_json()
        elif kept is not None:
            self.fix, self.dirty = kept, False
            self._show()
        else:
            dem = self.keeper.dem
            self.fix = GeoFix(image_id=image_id, image_file=image_file, terrain=bool(dem is not None and dem.configured))
            self.dirty = False
        if not self.keeper.is_read(image_id):
            self.keeper.load([image_id], lambda _f: self._kept_read(layer))
        self._markers_for()
        self.show_state()

    def _kept_read(self, layer) -> None:
        """The kept corrections read: a layer not corrected yet and not changed here gets its own."""
        if layer is not self.layer or self.layer is None or sip.isdeleted(layer):
            return
        if not self.dirty and not geo_fix_io.is_fixed(layer):
            kept = self.keeper.known(*image_key(layer))
            if kept is not None:
                self.fix = kept
                self._show()
        self.show_state()

    def saved(self) -> Optional[GeoFix]:
        layer = self._alive()
        return self.keeper.known(*image_key(layer)) if layer is not None else None

    def _changed(self, fix: GeoFix) -> None:
        self.fix = fix
        self.dirty = True
        self._show()

    def _show(self) -> None:
        """The layer shown with the correction being made (the DEM read first when needed)."""
        layer, fix = self._alive(), self.fix
        if layer is None or fix is None:
            return
        if fix.is_empty():
            self.keeper.take_off(layer)
            self._markers_for()
            self.show_state()
            return
        record = geo_fix_io.record_of(layer) or {}
        dem = str(record.get("dem") or "")
        if fix.terrain and not dem and self.keeper.dem is not None and self.keeper.dem.configured:
            self.say(tr("標高（DEM）を取得中…"))
            self.keeper.prepare(layer, fix, lambda _r: self._after_show())
            return
        try:
            geo_fix_io.apply(layer, fix, dem if fix.terrain else "", self.keeper.dem.geoid_height if self.keeper.dem else 0.0)
        except (geo_fix_io.GeoFixError, OSError, RuntimeError, ValueError) as error:
            self.warn(str(error))
        self.keeper.changed.emit(layer.id())
        self._after_show()

    def _after_show(self) -> None:
        self._markers_for()
        self.show_state()

    # ---- what the panel asks ----

    def set_terrain(self, on: bool) -> None:
        if self.fix is not None and self.fix.terrain != on:
            self._changed(replace(self.fix, terrain=on))

    def set_look(self, toward: Optional[float], incidence: Optional[float]) -> None:
        if self.fix is None:
            return
        if (self.fix.toward, self.fix.incidence) != (toward, incidence):
            self._changed(replace(self.fix, toward=toward, incidence=incidence))

    def add_point(self, start: QgsPointXY, end: QgsPointXY) -> None:
        """A landmark shown at `start` (map CRS) is really at `end`."""
        layer, fix = self._alive(), self.fix
        if layer is None or fix is None:
            return
        if len(fix.points) >= MAX_POINTS:
            self.warn(tr("目印の点は {n} 点まで").format(n=MAX_POINTS))
            return
        to_wgs = QgsCoordinateTransform(self.canvas.mapSettings().destinationCrs(), WGS84, QgsProject.instance())
        try:
            a, b = to_wgs.transform(start), to_wgs.transform(end)
        except Exception:  # noqa: BLE001 (QgsCsException)
            self.warn(tr("地図の座標を経緯度に変換できない"))
            return
        placement = cached_placement(layer)
        pixel = placement.pixel(a.x(), a.y()) if placement is not None else None
        if pixel is None:
            self.warn(tr("ドラッグを始めた所が画像の上にない"))
            return
        x, y = pixel
        if not (-0.05 * layer.width() <= x <= 1.05 * layer.width() and -0.05 * layer.height() <= y <= 1.05 * layer.height()):
            self.warn(tr("ドラッグは画像の上の目印から始める"))
            return
        self._changed(replace(fix, points=fix.points + (TiePoint(x, y, b.x(), b.y()),)))

    def undo(self) -> None:
        if self.fix is not None and self.fix.points:
            self._changed(replace(self.fix, points=self.fix.points[:-1]))

    def remove_point(self, i: int) -> None:
        if self.fix is not None and 0 <= i < len(self.fix.points):
            self._changed(replace(self.fix, points=self.fix.points[:i] + self.fix.points[i + 1:]))

    def take_off(self) -> None:
        """This screen only: the image where its file places it (the kept correction stays)."""
        layer = self._alive()
        if layer is None:
            return
        self.keeper.take_off(layer)
        image_id, image_file = image_key(layer)
        self.fix = GeoFix(image_id=image_id, image_file=image_file, terrain=False)
        self.dirty = False
        self._markers_for()
        self.show_state()

    def save(self) -> None:
        layer, fix = self._alive(), self.fix
        if layer is None or fix is None:
            return
        name = layer.name()
        report = geo_fix_io.report_of(layer)
        fix = replace(fix, residual=report.get("residual"))

        def done(saved: GeoFix) -> None:
            if self._alive() is not layer:
                # The image closed while it was saved: kept, nothing more to show.
                self.say(tr("{name} の幾何補正を保存（{where}）").format(name=name, where=self.keeper.where()))
                return
            self.fix, self.dirty = saved, False
            if geo_fix_io.is_fixed(layer):
                # The layer's record with who and when.
                record = geo_fix_io.record_of(layer) or {}
                import json

                record["fix"] = json.loads(saved.to_json())
                layer.setCustomProperty(geo_fix_io.FIX, json.dumps(record, ensure_ascii=False))
            self.say(tr("{name} の幾何補正を保存（{where}）").format(name=name, where=self.keeper.where()))
            self.show_state()
            self.offer_records()

        self.keeper.save(fix, done, self.warn)

    def delete_saved(self) -> None:
        saved, layer = self.saved(), self._alive()
        if saved is None or layer is None:
            return
        where = self.keeper.where()
        if QMessageBox.question(self.panel, tr(ENTRY), tr("{name} の保存した補正を削除する？（{where}。この画面の表示はそのまま）").format(
                name=layer.name(), where=where)) != QMessageBox.StandardButton.Yes:
            return
        self.keeper.delete(saved, lambda _n: (self.say(tr("保存した補正を削除")), self._deleted()), self.warn)

    def _deleted(self) -> None:
        self.dirty = self.fix is not None and not self.fix.is_empty()
        self.show_state()

    # ---- the records made on the image ----

    def records_to_move(self) -> tuple[dict, int, int]:
        """({record id: (lon, lat)} of the image's records not where the corrected image shows them, own count, others' count)."""
        dock = self.observations()
        book = getattr(dock, "book", None)
        layer = self._alive()
        if book is None or layer is None:
            return {}, 0, 0
        from ..core import edit_stamp

        image_id, image_file = image_key(layer)
        now = cached_placement(layer)
        before = original_placement(layer)
        if now is None:
            return {}, 0, 0
        me = edit_stamp.user_name().strip().casefold()
        moves, own, others = {}, 0, 0
        for o in list(book.records):
            if o.deleted_at is not None or o.image_id != image_id:
                continue
            if o.image_file and image_file and o.image_file.casefold() != image_file.casefold():
                continue
            if o.pixel_x is not None and o.pixel_y is not None:
                pixel = (o.pixel_x, o.pixel_y)
            else:
                pixel = before.pixel(o.lon, o.lat) if before is not None else None
            place = now.place(*pixel) if pixel is not None else None
            if place is None:
                continue
            east = (place[0] - o.lon) * 111_320 * math.cos(math.radians(o.lat))
            north = (place[1] - o.lat) * 110_574
            if math.hypot(east, north) < 0.05:
                continue
            moves[o.id] = place
            if (o.recorded_by or "").strip().casefold() == me:
                own += 1
            else:
                others += 1
        return moves, own, others

    def offer_records(self, asked: bool = False) -> None:
        """Offers to move the image's records to where the corrected image shows them (own by default)."""
        moves, own, others = self.records_to_move()
        if not moves:
            if asked:
                self.say(tr("動かす記録なし（観測記録ドックでこの画像を選ぶと、その記録を調べる）"))
            return
        box = QMessageBox(self.panel or self.iface.mainWindow())
        box.setWindowTitle(tr(ENTRY))
        box.setText(tr("この画像の記録 {n} 件が、補正した画像の示す位置からずれている（自分 {own} 件、ほかの人 {others} 件）。"
                       "補正に合わせて動かす？").format(n=len(moves), own=own, others=others))
        include = QCheckBox(tr("ほかの人の記録も動かす"))
        include.setEnabled(others > 0)
        box.setCheckBox(include)
        box.setStandardButtons(QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No)
        if box.exec() != QMessageBox.StandardButton.Yes:
            return
        self.move_records(moves, include.isChecked())

    def move_records(self, moves: dict, others: bool = False) -> None:
        from ..core import edit_stamp

        dock = self.observations()
        book = getattr(dock, "book", None)
        if book is None:
            return
        me = edit_stamp.user_name().strip().casefold()
        if not others:
            mine = {o.id for o in book.records if (o.recorded_by or "").strip().casefold() == me}
            moves = {k: v for k, v in moves.items() if k in mine}
        if not moves:
            return

        def after(outcome) -> None:
            if outcome is None:
                return
            text = tr("{n} 件の記録を補正に合わせて移動").format(n=len(outcome.saved))
            if outcome.failed:
                text += tr("・保存できず {n} 件（{why}）").format(n=outcome.failed, why=outcome.error)
            self.say(text)

        dock.do(tr("記録を補正に合わせる"), lambda b: b.move_many(moves), after)

    # ---- the map ----

    def faint(self, on: bool) -> None:
        """The image faint while a point is dragged (the map shows through), as it was after."""
        layer = self.layer
        if layer is None or sip.isdeleted(layer):
            self._opacity = None
            return
        if on and self._opacity is None:
            self._opacity = layer.opacity()
            layer.setOpacity(min(self._opacity, FAINT))
            layer.triggerRepaint()
        elif not on and self._opacity is not None:
            layer.setOpacity(self._opacity)
            self._opacity = None
            layer.triggerRepaint()

    def _clear_markers(self) -> None:
        scene = self.canvas.scene()
        for m in self._markers:
            if not sip.isdeleted(m) and scene is not None:
                scene.removeItem(m)
        self._markers = []

    def _markers_for(self) -> None:
        """A cross where each landmark really is."""
        self._clear_markers()
        if not self.active or self.fix is None:
            return
        to_map = QgsCoordinateTransform(WGS84, self.canvas.mapSettings().destinationCrs(), QgsProject.instance())
        for p in self.fix.points:
            try:
                at = to_map.transform(QgsPointXY(p.lon, p.lat))
            except Exception:  # noqa: BLE001
                continue
            m = QgsVertexMarker(self.canvas)
            m.setCenter(at)
            m.setIconType(QgsVertexMarker.IconType.ICON_CROSS)
            m.setColor(QColor("#dc2626"))
            m.setIconSize(14)
            m.setPenWidth(2)
            self._markers.append(m)

    # ---- the panel ----

    def show_state(self) -> None:
        # (Closed: nothing shown; opened again, take() shows the active image.)
        if self.panel is None or not self.active:
            return
        layer = self._alive()
        dem = self.keeper.dem
        if dem is None or not dem.configured:
            dem_text = tr("DEM の設定（dem）なし: 地形は補正しない")
        else:
            dem_text = tr("DEM: {src}{geoid}").format(src=dem.label, geoid=tr("、ジオイド高 {n:.1f} m").format(n=dem.geoid_height)
                                                       if dem.geoid_height else "")
        look_text, hint = "", None
        if layer is not None:
            try:
                base = geo_fix_io.open_base(original_source(layer).split("|")[0])
                look = self.fix.look() if self.fix is not None and self.fix.typed_look else geo_fix_io.file_look(base)
                hint = geo_fix_io.guessed_toward(base)
                if base.how == "rpc":
                    look_text = tr("撮像モデル（RPC）あり: 方向の入力は不要")
                elif look is not None:
                    look_text = tr("撮像方向（{src}）: {text}").format(src=tr("入力") if look.source == "typed" else tr("ファイル"),
                                                                 text=look.text())
                else:
                    look_text = tr("撮像方向がファイルから分からない。地形の補正には「撮像方向を入力する」で入力"
                                   "（衛星の方角の初期値は{hint}）").format(hint=tr("画像の向きから {d}（{a:.0f}°）").format(
                                       d=compass(hint), a=hint) if hint is not None else tr("なし"))
            except geo_fix_io.GeoFixError as error:
                look_text = str(error)
        report = geo_fix_io.report_of(layer) if layer is not None else {}
        if layer is not None and geo_fix_io.is_fixed(layer):
            # The residuals of the points as the model fits them now.
            report = dict(report)
            report["residuals"] = self._residuals(layer)
        self.panel.show_state(layer, self.fix, report, self.saved(), dem_text, look_text, self.dirty, self.keeper.where(), hint)

    def _residuals(self, layer) -> list:
        """Each landmark's miss (m): where the corrected image shows its pixel against where it is."""
        placement = cached_placement(layer)
        out = []
        for p in self.fix.points if self.fix is not None else ():
            shown = placement.place(p.x, p.y) if placement is not None else None
            if shown is None:
                out.append(None)
                continue
            out.append(math.hypot((shown[0] - p.lon) * 111_320 * math.cos(math.radians(p.lat)), (shown[1] - p.lat) * 110_574))
        return out

    # ---- telling ----

    def say(self, text: str) -> None:
        self.iface.messageBar().pushInfo(tr(ENTRY), text)

    def warn(self, text: str) -> None:
        self.iface.messageBar().pushWarning(tr(ENTRY), text)

    def close(self) -> None:
        self.set_active(False)
        for signal, slot in ((self.canvas.mapToolSet, self._tool_set), (self.iface.currentLayerChanged, self._current_changed),
                             (self.keeper.changed, self._layer_changed), (self.keeper.read, self._kept_changed),
                             (QgsProject.instance().layersWillBeRemoved, self._removing)):
            try:
                signal.disconnect(slot)
            except (TypeError, RuntimeError):
                pass
        self.tool.close()
        if self.panel is not None:
            self.panel.deleteLater()
            self.panel = None

    def deleteLater(self) -> None:  # noqa: N802 (Qt's name; features.base.close calls it)
        self.close()
        super().deleteLater()
