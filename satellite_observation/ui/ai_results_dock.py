"""
The dock 「外部AI結果」: what another AI system found in the image opened
from the image catalog, read from the settings file's `aiResultSources`
(Esri feature layers of points or polygons, by the image id), listed and
drawn on the map, and the chosen ones recorded as observations (観測記録),
a polygon at its point inside with its long side as the length.

It follows the image: when an image is opened from the catalog, or a layer
opened from it becomes the active one, its results are read (once per
image; 「再読み込み」 reads them again) and the layers of the image before
are replaced. A result already recorded (its tag in a record's note) is
marked 取込済 and is not recorded again.

Each result not yet recorded is matched with the things of the images
before and after (継続, core/obs/continuation): a sure one is recorded as
that thing (「継続: 船舶-3」 chosen in the 継続 column), an unsure one offers
the things it may be (recorded as a new thing unless one is chosen).

The viewer has no such dock (its AI only writes results, 「DB へ転記」).
"""
from __future__ import annotations

from typing import Any, Callable, Optional

from qgis.core import QgsFeatureRequest, QgsProject, QgsRasterLayer, QgsRectangle, QgsVectorLayer
from qgis.PyQt.QtCore import QCoreApplication, Qt
from qgis.PyQt.QtWidgets import (
    QAbstractItemView, QComboBox, QDoubleSpinBox, QHBoxLayout, QLabel, QPushButton, QTreeWidget, QTreeWidgetItem, QWidget,
)

from ..core.ai_results import AiResult, AiResultSource, import_entry, kind_for, recorded_tags, score_text
from ..core.obs.continuation import AUTO, CANDIDATE, Continuation, Found, continuations
from ..core.obs.flows import living
from ..qgis_io.ai_results_io import RESULTS_OF, ROW, fetch, mark_imported, place_of, results_layer
from ..qgis_io.catalog_io import CATALOG_URL
from ..qgis_io.geometry import from_wgs84
from ..qgis_io.obs_images import IMAGE_ID
from ..qgis_io.tasks import current_feedback, run_task
from .docks import PluginDock

TITLE = "外部AI結果"


def _tr(text: str) -> str:
    return QCoreApplication.translate("AiResultsDock", text)


def catalog_image_id(layer) -> Optional[str]:
    """The catalog's image id of a layer opened from the catalog; None for other layers."""
    if not isinstance(layer, QgsRasterLayer):
        return None
    image_id = layer.customProperty(IMAGE_ID)
    return str(image_id) if image_id and layer.customProperty(CATALOG_URL) else None


class AiResultsDock(PluginDock):
    """The image's external AI results, and what records them as observations."""

    def __init__(self, iface, context, open_settings: Callable[[], None], observations: Callable[[], Any],
                 parent: Optional[QWidget] = None):
        super().__init__("SatObsAiResults", _tr(TITLE), context, open_settings, parent)
        self.iface = iface
        self.canvas = iface.mapCanvas()
        #: The observations dock (its book and `do`).
        self.observations = observations
        self.image_id: Optional[str] = None
        #: Results by image id, read once: each source's results (or why it could not be read).
        self.cache: dict[str, list[tuple[AiResultSource, list[AiResult], Optional[str]]]] = {}
        #: The rows of the list: (source, result).
        self.rows: list[tuple[AiResultSource, AiResult]] = []
        self.layers: list[QgsVectorLayer] = []
        self.imported: set[str] = set()
        #: What each row (not recorded yet) may continue, by row.
        self.continued: dict[int, Continuation] = {}
        #: The thing chosen to continue, by result tag ("" for a new thing), when not the one matched.
        self.picks: dict[str, str] = {}
        self._task = None
        self._syncing = False

        head = QHBoxLayout()
        self.image_label = QLabel()
        self.image_label.setWordWrap(True)
        self.reload_button = QPushButton(_tr("再読み込み"))
        self.reload_button.setToolTip(_tr("この画像の結果を読み直し"))
        self.reload_button.clicked.connect(lambda: self.load(self.image_id, again=True))
        head.addWidget(self.image_label, 1)
        head.addWidget(self.reload_button)
        self.body.addLayout(head)

        filters = QHBoxLayout()
        filters.addWidget(QLabel(_tr("確信度の下限")))
        self.min_score = QDoubleSpinBox()
        self.min_score.setRange(0, 1)
        self.min_score.setSingleStep(0.05)
        self.min_score.setDecimals(2)
        self.min_score.setToolTip(_tr("これより低い結果は一覧と地図から除外（確信度のない結果は常に表示）"))
        self.min_score.valueChanged.connect(lambda _v: self.show_results())
        filters.addWidget(self.min_score)
        filters.addStretch(1)
        self.body.addLayout(filters)

        self.list = QTreeWidget()
        self.list.setHeaderLabels([_tr("取込"), _tr("出どころ"), _tr("クラス"), _tr("確信度"), _tr("種別"), _tr("名前"), _tr("継続")])
        self.list.setRootIsDecorated(False)
        self.list.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self.list.itemSelectionChanged.connect(self._list_selected)
        self.list.itemDoubleClicked.connect(lambda item, _c: self.zoom_to([self.list.indexOfTopLevelItem(item)]))
        self.body.addWidget(self.list, 1)

        buttons = QHBoxLayout()
        self.import_button = QPushButton(_tr("選択を観測記録へ取り込み"))
        self.import_button.setToolTip(_tr("一覧か地図で選んだ結果を、この画像の観測記録（ポイント）として記録。ポリゴンは内側の点と長辺の長さで記録"))
        self.import_button.clicked.connect(lambda: self.import_rows(self.selected_rows()))
        self.import_all_button = QPushButton(_tr("未取込をすべて取り込み"))
        self.import_all_button.clicked.connect(lambda: self.import_rows(list(range(len(self.rows)))))
        for b in (self.import_button, self.import_all_button):
            buttons.addWidget(b)
        buttons.addStretch(1)
        self.body.addLayout(buttons)
        self.status = QLabel()
        self.status.setWordWrap(True)
        self.body.addWidget(self.status)

        project = QgsProject.instance()
        project.layersAdded.connect(self._layers_added)
        project.layersWillBeRemoved.connect(self._layers_removed)
        self.iface.currentLayerChanged.connect(self._layer_chosen)
        context.configChanged.connect(self._config_changed)
        self._render_head()

    # ---- the image ---------------------------------------------------------

    def sources(self) -> list[AiResultSource]:
        return list(self.context.config.ai_result_sources)

    def _config_changed(self) -> None:
        self.cache.clear()
        self._render_head()
        if self.image_id:
            self.load(self.image_id)

    def _layers_added(self, layers) -> None:
        ids = [i for i in (catalog_image_id(layer) for layer in layers) if i]
        if ids and self.sources():
            # The last image opened.
            self.load(ids[-1], reveal=True)

    def _layer_chosen(self, layer) -> None:
        image_id = catalog_image_id(layer)
        if image_id and image_id != self.image_id and self.sources():
            self.load(image_id)

    def _layers_removed(self, ids) -> None:
        gone = set(ids)
        self.layers = [layer for layer in self.layers if layer.id() not in gone]

    def _render_head(self) -> None:
        if not self.sources():
            self.image_label.setText(_tr("設定ファイルに aiResultSources（外部AI結果のフィーチャーサービス）なし"))
        elif not self.image_id:
            self.image_label.setText(_tr("画像カタログから画像を開くと、その画像の結果を表示"))
        else:
            self.image_label.setText(_tr("画像: {id}").format(id=self.image_id))
        has = bool(self.sources() and self.image_id)
        self.reload_button.setEnabled(has)

    def load(self, image_id: Optional[str], again: bool = False, reveal: bool = False) -> None:
        """Shows the results of an image (read from every source, once unless `again`)."""
        if not image_id:
            return
        self.image_id = image_id
        self._render_head()
        if image_id in self.cache and not again:
            self.show_results(reveal=reveal)
            return
        sources = self.sources()
        if not sources:
            return
        if self._task is not None:
            try:
                self._task.cancel()
            except RuntimeError:
                pass
        client = self.context.esri()
        tokens = {s.url: self.context.token_for(s.url, s.token) for s in sources}
        self.status.setText(_tr("{id} の結果を読み込み中").format(id=image_id))

        def work(task):
            feedback = current_feedback()
            out = []
            for s in sources:
                try:
                    out.append((s, fetch(client, s, image_id, tokens[s.url], cancelled=feedback.isCanceled if feedback else None), None))
                except Exception as error:  # noqa: BLE001 (said per source)
                    out.append((s, [], str(error)))
            return out

        def done(found) -> None:
            self._task = None
            self.cache[image_id] = found
            if image_id == self.image_id:
                self.show_results(reveal=reveal)

        def failed(message: str) -> None:
            self._task = None
            if image_id == self.image_id:
                self.status.setText(_tr("読み込み失敗: {m}").format(m=message))

        self._task = run_task(_tr("外部AI結果の読み込み"), work, done, failed)

    # ---- the list and the layers ----------------------------------------------

    def _book(self):
        dock = self.observations()
        return getattr(dock, "book", None) if dock is not None else None

    def _refresh_imported(self) -> None:
        book = self._book()
        records = living(book.records) if book is not None else []
        self.imported = recorded_tags(o.note for o in records if o.image_id == self.image_id)

    def show_results(self, reveal: bool = False) -> None:
        """The cached results of the image, those at or above the lowest score, listed and drawn."""
        found = self.cache.get(self.image_id or "", [])
        least = self.min_score.value()
        self._refresh_imported()
        self.rows = []
        per_source: list[tuple[AiResultSource, list[AiResult], list[int]]] = []
        problems = []
        for source, results, error in found:
            if error:
                problems.append(f"{source.label}: {error}")
            kept = [r for r in results if r.score is None or r.score >= least]
            rows = list(range(len(self.rows), len(self.rows) + len(kept)))
            self.rows += [(source, r) for r in kept]
            per_source.append((source, kept, rows))
        self._syncing = True
        try:
            self.list.clear()
            for source, r in self.rows:
                done = r.tag in self.imported
                item = QTreeWidgetItem([_tr("済") if done else "", source.label, r.cls, score_text(r.score), kind_for(r, source), r.name])
                if r.geometry is None:
                    item.setToolTip(1, _tr("位置なし（取り込み不可）"))
                self.list.addTopLevelItem(item)
            self._match()
            for i in range(self.list.columnCount()):
                self.list.resizeColumnToContents(i)
        finally:
            self._syncing = False
        self._replace_layers(per_source)
        total = sum(len(results) for _s, results, _e in found)
        text = _tr("{n} 件（全 {total} 件）・取込済 {done} 件").format(
            n=len(self.rows), total=total, done=sum(1 for _s, r in self.rows if r.tag in self.imported))
        auto = sum(1 for c in self.continued.values() if c.tier == AUTO)
        maybe = len(self.continued) - auto
        if auto or maybe:
            text += _tr("・継続 {auto} 件・継続の候補 {maybe} 件").format(auto=auto, maybe=maybe)
        if problems:
            text += " / " + " / ".join(problems)
        self.status.setText(text)
        if reveal and self.rows and not self.isVisible():
            self.show()
            self.raise_()

    def _match(self) -> None:
        """What the rows not recorded yet continue (the things of the images before and after), shown in the 継続 column."""
        self.continued = {}
        for i in range(self.list.topLevelItemCount()):
            item = self.list.topLevelItem(i)
            if self.list.itemWidget(item, 6) is not None:
                self.list.removeItemWidget(item, 6)
        book = self._book()
        image_id = self.image_id
        if book is None or not book.loaded or not image_id or not any(i.id == image_id for i in book.images):
            return
        rows, found = [], []
        for row, (source, r) in enumerate(self.rows):
            if r.tag in self.imported or source.candidate_radius <= 0:
                continue
            place = place_of(r)
            if place is None:
                continue
            entry = import_entry(r, source, place[0], place[1], place[2])
            rows.append(row)
            found.append(Found(entry.lon, entry.lat, entry.kind, entry.length, entry.heading,
                               source.continue_radius, source.candidate_radius))
        if not found:
            return
        for row, c in zip(rows, continuations(found, book.carried_for(image_id), book.kept_taxonomy())):
            if c.options:
                self.continued[row] = c
        for row, c in self.continued.items():
            item = self.list.topLevelItem(row)
            if item is None:
                continue
            box = QComboBox()
            box.addItem(_tr("新規（候補あり）") if c.tier == CANDIDATE else _tr("新規"), "")
            for o in c.options:
                box.addItem(_tr("継続: {t}").format(t=o.text), o.object_id)
            tag = self.rows[row][1].tag
            chosen = self.picks.get(tag, c.pick.object_id if c.pick else "")
            box.setCurrentIndex(max(0, box.findData(chosen)))
            box.setToolTip(_tr("自動で継続（近く、分類と長さが合い、紛らわしいものがない）") if c.tier == AUTO
                           else _tr("継続の候補あり（近いが確かでない）: 選ぶと継続として記録"))
            box.currentIndexChanged.connect(lambda _i, b=box, t=tag: self.picks.__setitem__(t, b.currentData() or ""))
            self.list.setItemWidget(item, 6, box)

    def chosen_carry(self, row: int):
        """The record of the thing a row is recorded as continuing (継続), None for a new thing."""
        c = self.continued.get(row)
        if c is None:
            return None
        tag = self.rows[row][1].tag
        chosen = self.picks.get(tag, c.pick.object_id if c.pick else "")
        return next((o.source for o in c.options if o.object_id == chosen), None)

    def _replace_layers(self, per_source) -> None:
        project = QgsProject.instance()
        stale = [layer.id() for layer in project.mapLayers().values() if layer.customProperty(RESULTS_OF) is not None]
        for layer in self.layers:
            try:
                if layer.id() not in stale and project.mapLayer(layer.id()) is not None:
                    stale.append(layer.id())
            except RuntimeError:
                pass
        self.layers = []
        if stale:
            project.removeMapLayers(stale)
        for source, results, rows in per_source:
            if not results:
                continue
            layer = results_layer(results, rows, source, self.image_id or "", self.imported)
            project.addMapLayer(layer)
            layer.selectionChanged.connect(lambda *_a, lyr=layer: self._map_selected(lyr))
            self.layers.append(layer)

    def _features_of(self, rows: set[int]):
        for layer in self.layers:
            index = layer.fields().indexOf(ROW)
            ids = [f.id() for f in layer.getFeatures() if f[index] in rows]
            yield layer, ids

    def _list_selected(self) -> None:
        if self._syncing:
            return
        rows = set(self.selected_rows())
        self._syncing = True
        try:
            for layer, ids in self._features_of(rows):
                layer.selectByIds(ids)
        finally:
            self._syncing = False

    def _map_selected(self, layer: QgsVectorLayer) -> None:
        if self._syncing:
            return
        rows: set[int] = set()
        for lyr in self.layers:
            index = lyr.fields().indexOf(ROW)
            rows |= {f[index] for f in lyr.getSelectedFeatures()}
        self._syncing = True
        try:
            for i in range(self.list.topLevelItemCount()):
                self.list.topLevelItem(i).setSelected(i in rows)
        finally:
            self._syncing = False

    def selected_rows(self) -> list[int]:
        return sorted(self.list.indexOfTopLevelItem(item) for item in self.list.selectedItems())

    def zoom_to(self, rows: list[int]) -> None:
        box: Optional[QgsRectangle] = None
        for layer, ids in self._features_of(set(rows)):
            for f in layer.getFeatures(QgsFeatureRequest().setFilterFids(ids)):
                if f.hasGeometry():
                    b = from_wgs84(self.canvas.mapSettings().destinationCrs()).transformBoundingBox(f.geometry().boundingBox())
                    if box is None:
                        box = b
                    else:
                        box.combineExtentWith(b)
        if box is None:
            return
        if box.width() < 1e-9 or box.height() < 1e-9:
            self.canvas.setCenter(box.center())
        else:
            box.scale(3)
            self.canvas.setExtent(box)
        self.canvas.refresh()

    # ---- recording -----------------------------------------------------------

    def import_rows(self, rows: list[int]) -> list:
        """Records the results of these rows (those not recorded yet, with a place) as observations of the image."""
        dock = self.observations()
        book = self._book()
        if dock is None or book is None:
            self.status.setText(_tr("観測記録は使用不可"))
            return []
        if not book.loaded:
            self.status.setText(_tr("観測記録の読み込み待ち"))
            return []
        image_id = self.image_id
        if not image_id or not any(i.id == image_id for i in book.images):
            self.status.setText(_tr("画像 {id} が観測記録の画像一覧になし（画像を開いて）").format(id=image_id or ""))
            return []
        self._refresh_imported()
        levels = {c.label for c in book.confidences()}
        entries, done_rows, skipped = [], [], 0
        for row in rows:
            if not (0 <= row < len(self.rows)):
                continue
            source, r = self.rows[row]
            if r.tag in self.imported:
                continue
            place = place_of(r)
            if place is None:
                skipped += 1
                continue
            entry = import_entry(r, source, place[0], place[1], place[2])
            if entry.confidence and entry.confidence not in levels:
                entry.confidence = ""
            entry.carry = self.chosen_carry(row)
            if entry.carry is not None:
                c = self.continued[row]
                how = "自動継続" if c.pick is not None and c.pick.object_id == entry.carry.object_id else "選択継続"
                entry.note = f"{entry.note} [{how}]"
            entries.append(entry)
            done_rows.append(row)
        if not entries:
            self.status.setText(_tr("取り込む結果なし（取込済か、位置なし）") if rows else _tr("結果を選択"))
            return []

        carried_ids = {e.carry.object_id for e in entries if e.carry is not None}

        def after(saved) -> None:
            if not saved:
                return
            continued = {o.object_id for o in saved if o.object_id in carried_ids}
            saved_names = {i: _tr("継続: {n}").format(n=e.carry.object_name) for i, e in zip(done_rows, entries)
                           if e.carry is not None and e.carry.object_id in continued}
            self.imported |= {self.rows[i][1].tag for i in done_rows if i < len(self.rows)}
            for layer in self.layers:
                mark_imported(layer, set(done_rows))
            for i in done_rows:
                item = self.list.topLevelItem(i)
                if item is not None:
                    item.setText(0, _tr("済"))
                    item.setText(6, saved_names.get(i, ""))
            # What the rest may continue, without the things now in this image.
            self._match()
            text = _tr("{n} 件を観測記録に取り込み").format(n=len(saved))
            if continued:
                text += _tr("（継続 {k} 件）").format(k=len(continued))
            if skipped:
                text += _tr("（位置なし {k} 件は除外）").format(k=skipped)
            self.status.setText(text)

        result = dock.do(_tr("外部AI結果の取り込み"), lambda b: b.add_many(entries, image=image_id), after)
        return result or []

    def close_down(self) -> None:
        """Called when the plugin unloads."""
        if self._task is not None:
            try:
                self._task.cancel()
            except RuntimeError:
                pass
        project = QgsProject.instance()
        for signal, slot in ((project.layersAdded, self._layers_added), (project.layersWillBeRemoved, self._layers_removed),
                             (self.iface.currentLayerChanged, self._layer_chosen), (self.context.configChanged, self._config_changed)):
            try:
                signal.disconnect(slot)
            except (TypeError, RuntimeError):
                pass

