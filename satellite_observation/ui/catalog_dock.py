"""
画像カタログ: searching the image catalogs of the settings file (over the
map's extent or anywhere, by dates, sensor, angle, azimuth and SQL), the list
of images found with their footprints on the map, opening them as raster
layers (a COG through /vsicurl/, a local path through `pathMappings`), the
list as a layer, 「類似画像の検索」 and 「この条件で監視…」 (the watches are in
the 通知 dock).
"""
from __future__ import annotations

from dataclasses import replace
from typing import Callable, Optional

from qgis.core import QgsProject
from qgis.PyQt.QtCore import Qt
from qgis.PyQt.QtWidgets import (
    QAbstractItemView, QApplication, QCheckBox, QComboBox, QDoubleSpinBox, QFileDialog, QFormLayout, QGroupBox, QHBoxLayout,
    QLabel, QLineEdit, QPushButton, QSpinBox, QTableView, QVBoxLayout, QWidget,
)

from ..core.catalog.config import CatalogConfig
from ..core.catalog.paths import PathError, PathMapping
from ..core.catalog.query import CatalogSearch
from ..core.catalog.search import CatalogLayerInfo, CatalogRecord, read_catalog_layer, search_catalog, sensors_of
from ..core.catalog.similar import SimilarTolerance, rank_similar, similar_search
from ..core.catalog.watch import areas_of
from ..qgis_io import settings
from ..qgis_io.catalog_io import Cancelled, footprint, open_record, overlap_measure, record_of_layer, results_layer
from ..qgis_io.geometry import from_wgs84, to_wgs84
from ..qgis_io.tasks import run_task
from .catalog_model import CatalogModel
from .catalog_overlay import DrawTool, FootprintOverlay, FootprintTool
from .docks import PluginDock
from .flow_layout import FlowLayout

TITLE = "画像カタログ"


def _number(edit: QLineEdit) -> Optional[float]:
    try:
        return float(edit.text().strip())
    except ValueError:
        return None


def _day(edit: QLineEdit) -> str:
    return edit.text().strip().replace("/", "-")


class CatalogDock(PluginDock):
    def __init__(self, iface, context, open_settings: Callable[[], None], parent=None):
        super().__init__("SatObsImageCatalog", TITLE, context, open_settings, parent)
        self.iface = iface
        self.canvas = iface.mapCanvas()
        self.layers: dict[str, CatalogLayerInfo] = {}
        #: The image compared with in 「類似画像の検索」 (with its catalog); None for the usual search.
        self.reference: Optional[tuple[CatalogRecord, CatalogConfig]] = None
        #: Opens the watch dialog of the 通知 dock (set by the plugin): (catalog, types, conditions, area drawn).
        self.watch_requested: Optional[Callable[[CatalogConfig, dict, CatalogSearch, Optional[list]], None]] = None
        self._shown_catalog: Optional[CatalogConfig] = None
        self._tasks: list = []

        form = QFormLayout()
        self.catalog_box = QComboBox()
        self.catalog_box.currentIndexChanged.connect(self._catalog_changed)
        form.addRow(self.tr("カタログ"), self.catalog_box)
        self.area_box = QComboBox()
        self.area_box.addItem(self.tr("表示範囲"), "extent")
        self.area_box.addItem(self.tr("描いた範囲"), "drawn")
        self.area_box.addItem(self.tr("指定なし"), "none")
        self.area_box.setCurrentIndex(0)
        self.area_box.activated.connect(self._area_chosen)
        self.draw_button = QPushButton(self.tr("範囲を描く"))
        self.draw_button.setCheckable(True)
        self.draw_button.setToolTip(self.tr("クリックで頂点を追加、ダブルクリックか右クリックで確定、Esc で取消"))
        self.draw_button.toggled.connect(self._draw_toggled)
        area_row = QHBoxLayout()
        area_row.addWidget(self.area_box, 1)
        area_row.addWidget(self.draw_button)
        form.addRow(self.tr("範囲"), area_row)
        #: The area drawn on the map (its corners, longitude / latitude; the ring closed); None before one is drawn.
        self.drawn: Optional[list] = None
        self.acquired_from, self.acquired_to = QLineEdit(), QLineEdit()
        form.addRow(self.tr("撮像日"), self._range(self.acquired_from, self.acquired_to))
        self.body.addLayout(form)

        self.more = QGroupBox(self.tr("詳細条件"))
        self.more.setCheckable(True)
        self.more.setChecked(False)
        # Closed, the group is just its heading.
        more_body = QWidget()
        QVBoxLayout(self.more).addWidget(more_body)
        self.more.layout().setContentsMargins(0, 0, 0, 0)
        more_body.hide()
        self.more.toggled.connect(more_body.setVisible)
        more = QFormLayout(more_body)
        self.registered_from, self.registered_to = QLineEdit(), QLineEdit()
        more.addRow(self.tr("登録日"), self._range(self.registered_from, self.registered_to))
        self.system_from, self.system_to = QLineEdit(), QLineEdit()
        more.addRow(self.tr("システム登録日"), self._range(self.system_from, self.system_to))
        self.sensor_box = QComboBox()
        self.sensor_box.setEditable(True)
        more.addRow(self.tr("センサー"), self.sensor_box)
        self.max_angle = QLineEdit()
        self.max_angle.setPlaceholderText(self.tr("上限（度）"))
        more.addRow(self.tr("角度"), self.max_angle)
        self.azimuth_from, self.azimuth_to = QLineEdit(), QLineEdit()
        more.addRow(self.tr("方位角"), self._range(self.azimuth_from, self.azimuth_to, "0", "360"))
        self.where = QLineEdit()
        self.where.setPlaceholderText(self.tr("SQL の条件"))
        more.addRow(self.tr("条件"), self.where)
        self.body.addWidget(self.more)

        self.similar = QGroupBox(self.tr("類似画像の検索"))
        similar = QFormLayout(self.similar)
        self.reference_label = QLabel()
        self.reference_label.setWordWrap(True)
        similar.addRow(self.reference_label)
        self.same_sensor = QCheckBox(self.tr("同じセンサー"))
        similar.addRow(self.same_sensor)
        self.angle_tolerance = QDoubleSpinBox()
        self.angle_tolerance.setRange(0, 90)
        self.angle_tolerance.setSuffix("°")
        self.angle_tolerance.setPrefix("±")
        similar.addRow(self.tr("角度の差"), self.angle_tolerance)
        self.azimuth_tolerance = QDoubleSpinBox()
        self.azimuth_tolerance.setRange(0, 179)
        self.azimuth_tolerance.setSuffix("°")
        self.azimuth_tolerance.setPrefix("±")
        similar.addRow(self.tr("方位角の差"), self.azimuth_tolerance)
        self.min_overlap = QSpinBox()
        self.min_overlap.setRange(0, 100)
        self.min_overlap.setSuffix("%")
        similar.addRow(self.tr("重なりの下限"), self.min_overlap)
        back = QPushButton(self.tr("通常の検索に復帰"))
        back.clicked.connect(self.normal_search)
        similar.addRow(back)
        self.similar.hide()
        self.body.addWidget(self.similar)

        row = QHBoxLayout()
        self.search_button = QPushButton(self.tr("検索"))
        self.search_button.clicked.connect(self.search)
        row.addWidget(self.search_button)
        self.status = QLabel()
        self.status.setWordWrap(True)
        row.addWidget(self.status, 1)
        self.body.addLayout(row)

        self.model = CatalogModel(self)
        self.table = QTableView()
        self.table.setModel(self.model)
        self.table.setSortingEnabled(True)
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.verticalHeader().hide()
        self.table.doubleClicked.connect(lambda index: self.open_records([self.model.records[index.row()]]))
        self.table.selectionModel().selectionChanged.connect(self._selection_changed)
        self.model.layoutChanged.connect(self._redraw)
        self.body.addWidget(self.table, 1)

        buttons = FlowLayout()
        self.buttons: dict[str, QPushButton] = {}
        for key, text, slot in [
            ("open", self.tr("開く"), lambda: self.open_records(self.chosen())),
            ("zoom", self.tr("範囲へ移動"), self.zoom_to_chosen),
            ("copy", self.tr("パスをコピー"), self.copy_paths),
            ("layer", self.tr("レイヤーとして追加"), self.add_layer),
            ("similar", self.tr("類似画像の検索"), self._similar_of_chosen),
            ("watch", self.tr("この条件で監視…"), self._watch),
            ("pick", self.tr("地図で選択"), None),
            ("folders", self.tr("フォルダの対応を消去"), self.clear_folders),
        ]:
            button = QPushButton(text)
            if slot is None:
                button.setCheckable(True)
                button.toggled.connect(self._pick_toggled)
            else:
                button.clicked.connect(slot)
            buttons.addWidget(button)
            self.buttons[key] = button
        holder = QWidget()
        holder.setLayout(buttons)
        self.body.addWidget(holder)

        self.overlay: Optional[FootprintOverlay] = None
        self.tool = FootprintTool(self.canvas, self._picked)
        self.draw_tool = DrawTool(self.canvas, self.set_drawn)
        # Another map tool chosen: the buttons pop up.
        self.tool.deactivated.connect(lambda: self.buttons["pick"].setChecked(False))
        self.draw_tool.deactivated.connect(lambda: self.draw_button.setChecked(False))
        self._previous_tool = None
        self.visibilityChanged.connect(self._visibility)
        context.configChanged.connect(self._config_changed)
        self._load_tolerance()
        self._config_changed()

    # --- Form -------------------------------------------------------------------------------------

    def _range(self, a: QLineEdit, b: QLineEdit, pa: str = "YYYY-MM-DD", pb: str = "YYYY-MM-DD") -> QWidget:
        a.setPlaceholderText(pa)
        b.setPlaceholderText(pb)
        w = QWidget()
        h = QHBoxLayout(w)
        h.setContentsMargins(0, 0, 0, 0)
        h.addWidget(a)
        h.addWidget(QLabel("〜"))
        h.addWidget(b)
        return w

    def catalogs(self) -> list[CatalogConfig]:
        return list(self.context.config.image_catalogs)

    def catalog(self) -> Optional[CatalogConfig]:
        i = self.catalog_box.currentIndex()
        catalogs = self.catalogs()
        return catalogs[i] if 0 <= i < len(catalogs) else None

    def _config_changed(self) -> None:
        current = self.catalog_box.currentText()
        self.catalog_box.blockSignals(True)
        self.catalog_box.clear()
        for c in self.catalogs():
            self.catalog_box.addItem(c.label)
        i = self.catalog_box.findText(current)
        self.catalog_box.setCurrentIndex(i if i >= 0 else 0)
        self.catalog_box.blockSignals(False)
        self.layers = {}
        self.search_button.setEnabled(bool(self.catalogs()))
        if not self.catalogs():
            self.status.setText(self.tr("設定ファイルに画像カタログなし"))
        self._catalog_changed()

    def _catalog_changed(self) -> None:
        self.sensor_box.clear()
        self.sensor_box.addItem("")
        catalog = self.catalog()
        if catalog is None or not catalog.fields.get("sensor"):
            return
        client = self.context.esri()
        token = self.context.token_for(catalog.url, catalog.token)
        self._start(self.tr("センサーの取得"), lambda task: sensors_of(client, catalog, token),
                    lambda sensors: self.sensor_box.addItems(sensors) if self.catalog() is catalog else None, lambda m: None)

    def conditions(self) -> CatalogSearch:
        """The search the form describes (詳細条件 only while it is open)."""
        search = CatalogSearch(acquired_from=_day(self.acquired_from), acquired_to=_day(self.acquired_to))
        if not self.more.isChecked():
            return search
        return replace(search, registered_from=_day(self.registered_from), registered_to=_day(self.registered_to),
                       system_registered_from=_day(self.system_from), system_registered_to=_day(self.system_to),
                       sensor=self.sensor_box.currentText().strip(), max_angle=_number(self.max_angle),
                       azimuth_from=_number(self.azimuth_from), azimuth_to=_number(self.azimuth_to), where=self.where.text().strip())

    def extent(self) -> Optional[list[float]]:
        """The map's extent in WGS 84 ([xmin, ymin, xmax, ymax])."""
        box = to_wgs84(self.canvas.mapSettings().destinationCrs()).transformBoundingBox(self.canvas.extent())
        return [box.xMinimum(), box.yMinimum(), box.xMaximum(), box.yMaximum()]

    # --- Tasks ------------------------------------------------------------------------------------

    def _start(self, description: str, work, done, failed=None) -> None:
        def fail(message: str) -> None:
            self.search_button.setEnabled(True)
            self.status.setText(self.tr("{what}に失敗: {m}").format(what=description, m=message))

        task = run_task(description, work, done, failed or fail)
        self._tasks.append(task)
        task.taskCompleted.connect(lambda: self._tasks.remove(task) if task in self._tasks else None)
        task.taskTerminated.connect(lambda: self._tasks.remove(task) if task in self._tasks else None)

    def _layer_of(self, client, catalog: CatalogConfig, token) -> CatalogLayerInfo:
        info = self.layers.get(catalog.url)
        if info is None:
            info = read_catalog_layer(client, catalog, token)
            self.layers[catalog.url] = info
        return info

    # --- Searching --------------------------------------------------------------------------------

    def search(self) -> None:
        """Searches the chosen catalog (images like the one compared with, during 「類似画像の検索」)."""
        if self.reference is not None:
            self._search_similar()
            return
        catalog = self.catalog()
        if catalog is None:
            return
        search = self.conditions()
        box = self.area()
        if self.area_box.currentData() == "drawn" and box is None:
            self.status.setText(self.tr("範囲が未作成。「範囲を描く」で地図に描画"))
            return
        client = self.context.esri()
        token = self.context.token_for(catalog.url, catalog.token)

        def work(task):
            return search_catalog(client, catalog, self._layer_of(client, catalog, token), search, box, token)

        def done(result) -> None:
            self.search_button.setEnabled(True)
            self.show_records(catalog, result.records)
            n = len(result.records)
            if result.total is not None and result.total > n:
                self.status.setText(self.tr("{total} 件中 新しい {n} 件").format(total=result.total, n=n))
            else:
                self.status.setText(self.tr("{n} 件").format(n=n))

        self.search_button.setEnabled(False)
        self.status.setText(self.tr("検索中"))
        self._start(self.tr("画像カタログの検索"), work, done)

    def show_records(self, catalog: CatalogConfig, records, overlaps=None) -> None:
        self._shown_catalog = catalog
        self.model.set_records(catalog, records, overlaps)
        # The list comes newest first (類似画像: the most alike first): the heading says so without sorting again.
        header = self.table.horizontalHeader()
        header.blockSignals(True)
        acquired = next((i for i, c in enumerate(self.model.columns) if c.key == "acquired"), -1)
        header.setSortIndicator(acquired if overlaps is None else -1, Qt.SortOrder.DescendingOrder)
        header.blockSignals(False)
        self.table.resizeColumnsToContents()
        self._redraw()

    @property
    def records(self) -> list[CatalogRecord]:
        return list(self.model.records)

    # --- 類似画像の検索 ---------------------------------------------------------------------------

    def _load_tolerance(self) -> None:
        t = SimilarTolerance.from_json(settings.similar_tolerance())
        self.same_sensor.setChecked(t.same_sensor)
        self.angle_tolerance.setValue(t.angle if t.angle is not None else 5)
        self.azimuth_tolerance.setValue(t.azimuth if t.azimuth is not None else 20)
        self.min_overlap.setValue(round(t.min_overlap * 100))

    def tolerance(self) -> SimilarTolerance:
        return SimilarTolerance(self.same_sensor.isChecked(), self.angle_tolerance.value(), self.azimuth_tolerance.value(),
                                self.min_overlap.value() / 100)

    def find_similar(self, record: CatalogRecord, catalog: CatalogConfig) -> None:
        """Lists the images like `record` (an image of `catalog`), the most alike first."""
        self.reference = (record, catalog)
        i = next((n for n, c in enumerate(self.catalogs()) if c.url == catalog.url), -1)
        if i >= 0:
            self.catalog_box.setCurrentIndex(i)
        self.reference_label.setText(self.tr("比較元: {id}").format(id=record.id or self.tr("（画像ID なし）")))
        self.similar.show()
        self.show()
        self.raise_()
        self._search_similar()

    def find_similar_of_layer(self, layer) -> bool:
        """「類似画像の検索…」 of a layer opened from the catalog; False for other layers."""
        found = record_of_layer(layer, self.catalogs())
        if found is None:
            return False
        self.find_similar(*found)
        return True

    def _similar_of_chosen(self) -> None:
        chosen = self.chosen()
        if chosen and self._shown_catalog is not None:
            self.find_similar(chosen[0], self._shown_catalog)

    def _search_similar(self) -> None:
        record, catalog = self.reference
        tolerance = self.tolerance()
        settings.set_similar_tolerance(tolerance.to_json())
        search = similar_search(catalog, record, tolerance, self.conditions())
        reference = footprint(record.geometry)
        box = None
        if reference is not None:
            b = reference.boundingBox()
            box = [b.xMinimum(), b.yMinimum(), b.xMaximum(), b.yMaximum()]
        client = self.context.esri()
        token = self.context.token_for(catalog.url, catalog.token)

        def work(task):
            return search_catalog(client, catalog, self._layer_of(client, catalog, token), search, box, token)

        def done(result) -> None:
            self.search_button.setEnabled(True)
            if self.reference is None:
                return
            ranked = rank_similar(record, result.records, tolerance, overlap_measure(reference))
            self.show_records(catalog, [s.record for s in ranked], {id(s.record): s.overlap for s in ranked})
            self.status.setText(self.tr("類似画像 {n} 件").format(n=len(ranked)))

        self.search_button.setEnabled(False)
        self.status.setText(self.tr("類似画像の検索中"))
        self._start(self.tr("類似画像の検索"), work, done)

    def normal_search(self) -> None:
        """「通常の検索に復帰」: leaves 「類似画像の検索」 and searches with the form."""
        self.reference = None
        self.similar.hide()
        self.search()

    # --- The list and the map ---------------------------------------------------------------------

    def chosen(self) -> list[CatalogRecord]:
        rows = sorted({i.row() for i in self.table.selectionModel().selectedRows()})
        return [self.model.records[r] for r in rows]

    def choose(self, records) -> None:
        """Chooses records of the list (as the map's selection follows)."""
        wanted = {id(r) for r in records}
        selection = self.table.selectionModel()
        selection.clearSelection()
        flags = selection.SelectionFlag.Select | selection.SelectionFlag.Rows
        for row, r in enumerate(self.model.records):
            if id(r) in wanted:
                selection.select(self.model.index(row, 0), flags)

    def _redraw(self) -> None:
        if self.overlay is None:
            return
        reference = footprint(self.reference[0].geometry) if self.reference else None
        self.overlay.show([footprint(r.geometry) for r in self.model.records], reference)
        self._selection_changed()

    def _selection_changed(self, *args) -> None:
        rows = sorted({i.row() for i in self.table.selectionModel().selectedRows()})
        if self.overlay is not None:
            self.overlay.choose(rows, [self.model.records[r].id for r in rows])
        has = bool(rows)
        for key in ("open", "zoom", "copy", "similar"):
            self.buttons[key].setEnabled(has)
        if len(rows) == 1:
            self.table.scrollTo(self.model.index(rows[0], 0))

    def _picked(self, lon: float, lat: float, tolerance: float, add: bool) -> None:
        if self.overlay is None:
            return
        row = self.overlay.at(lon, lat, tolerance)
        selection = self.table.selectionModel()
        if row is None:
            if not add:
                selection.clearSelection()
            return
        index = self.model.index(row, 0)
        flags = selection.SelectionFlag.Rows
        if add:
            selection.select(index, flags | selection.SelectionFlag.Toggle)
        else:
            selection.select(index, flags | selection.SelectionFlag.ClearAndSelect)

    def _pick_toggled(self, on: bool) -> None:
        self._use_tool(self.tool, on)

    def _draw_toggled(self, on: bool) -> None:
        self._use_tool(self.draw_tool, on)

    def _use_tool(self, tool, on: bool) -> None:
        if on:
            current = self.canvas.mapTool()
            if current not in (self.tool, self.draw_tool):
                self._previous_tool = current
            self.canvas.setMapTool(tool)
        elif self.canvas.mapTool() is tool:
            self.canvas.unsetMapTool(tool)
            if self._previous_tool is not None:
                try:
                    self.canvas.setMapTool(self._previous_tool)
                except RuntimeError:
                    pass

    def _area_chosen(self, index: int) -> None:
        if self.area_box.itemData(index) == "drawn" and self.drawn is None:
            self.draw_button.setChecked(True)

    def set_drawn(self, corners: Optional[list]) -> None:
        """The area to search (and watch) drawn on the map: its corners in WGS 84; None forgets it."""
        self.drawn = None
        if corners:
            ring = [list(p) for p in corners]
            if ring[0] != ring[-1]:
                ring.append(list(ring[0]))
            self.drawn = ring
            self.area_box.setCurrentIndex(self.area_box.findData("drawn"))
        elif self.area_box.currentData() == "drawn":
            self.area_box.setCurrentIndex(0)
        self.draw_button.setChecked(False)
        if self.overlay is not None:
            self.overlay.show_area(self.drawn)

    def area(self):
        """Where to search: the map's extent (a box), the area drawn (an Esri polygon) or None for anywhere."""
        kind = self.area_box.currentData()
        if kind == "extent":
            return self.extent()
        if kind == "drawn" and self.drawn:
            return areas_of([], [[self.drawn]])[0]
        return None

    def zoom_to_chosen(self) -> None:
        rows = sorted({i.row() for i in self.table.selectionModel().selectedRows()})
        box = self.overlay.extent(rows) if self.overlay else None
        if box is None:
            return
        extent = from_wgs84(self.canvas.mapSettings().destinationCrs()).transformBoundingBox(box)
        extent.scale(1.2)
        self.canvas.setExtent(extent)
        self.canvas.refresh()

    def _visibility(self, visible: bool) -> None:
        if visible and self.overlay is None:
            self.overlay = FootprintOverlay(self.canvas)
            self.overlay.show_area(self.drawn)
            self._redraw()
        elif not visible and self.overlay is not None:
            self.buttons["pick"].setChecked(False)
            self.draw_button.setChecked(False)
            self.overlay.remove()
            self.overlay = None

    # --- Opening ----------------------------------------------------------------------------------

    def choose_folder(self, mapping: PathMapping) -> Optional[str]:
        return QFileDialog.getExistingDirectory(self, self.tr("「{label}」（{prefix}）のフォルダの選択").format(label=mapping.label, prefix=mapping.prefix)) or None

    def open_records(self, records) -> list:
        """Opens images of the list as raster layers; the reason is shown for those that cannot be opened."""
        catalog = self._shown_catalog
        if catalog is None:
            return []
        layers = []
        problems = []
        for record in records:
            try:
                layer = open_record(record, catalog, self.context.config.path_mappings, self.choose_folder)
            except Cancelled:
                break
            except PathError as error:
                problems.append(str(error))
                continue
            QgsProject.instance().addMapLayer(layer)
            layers.append(layer)
        if problems:
            self.iface.messageBar().pushWarning(TITLE, " / ".join(problems))
            self.status.setText(problems[-1])
        return layers

    def copy_paths(self) -> None:
        """Copies where the chosen images are (their URL or path), one per line."""
        paths = [r.source.value for r in self.chosen() if r.source]
        if not paths:
            self.status.setText(self.tr("画像の場所なし"))
            return
        QApplication.clipboard().setText("\n".join(paths))
        self.status.setText(self.tr("パス {n} 件をコピー").format(n=len(paths)))

    def clear_folders(self) -> None:
        settings.clear_path_folders()
        self.status.setText(self.tr("フォルダの対応を消去"))

    def add_layer(self):
        """The list as a layer of footprints (every attribute; the temporal controller on 撮像日時)."""
        catalog = self._shown_catalog
        if catalog is None or not self.model.records:
            return None
        info = self.layers.get(catalog.url)
        name = f"{TITLE} {catalog.label}" if self.reference is None else f"{TITLE} {catalog.label} 類似画像"
        layer = results_layer(self.model.records, catalog, info.types if info else {}, name)
        QgsProject.instance().addMapLayer(layer)
        return layer

    def _watch(self) -> None:
        catalog = self.catalog()
        if catalog is None or self.watch_requested is None:
            return
        info = self.layers.get(catalog.url)
        if info is None:
            client = self.context.esri()
            token = self.context.token_for(catalog.url, catalog.token)
            self._start(self.tr("画像カタログの確認"), lambda task: self._layer_of(client, catalog, token),
                        lambda info: self.watch_requested(catalog, info.types, self.conditions(), self.drawn))
            return
        self.watch_requested(catalog, info.types, self.conditions(), self.drawn)

    def close_down(self) -> None:
        self.buttons["pick"].setChecked(False)
        self.draw_button.setChecked(False)
        self.draw_tool.remove()
        if self.overlay is not None:
            self.overlay.remove()
            self.overlay = None
