"""
The dock 「AI結果」: what another AI system found in the image opened
from the image catalog, read from the settings file's `aiResultSources`
(Esri feature layers of points or polygons, by the image id), and what the
plugin's own 「物体検出（AI）」 found (its layers in the project), listed
and drawn on the map, and the chosen ones recorded as observations (観測記録),
a polygon at its point inside with its long side as the length.

A detection run shows its image's results here when its layer comes; the
layer is the list's own (no copy): choosing there chooses here, and what the
person changes in it (a class corrected, a false one deleted) is what is
recorded.

It follows the image: when an image is opened from the catalog, or a layer
opened from it becomes the active one, its results are read (once per
image; 「再読み込み」 reads them again) and the layers of the image before
are replaced. A result already recorded (its tag in a record's note) is
marked 取込済 and is not recorded again.

Review first what the AI is least sure of (能動学習, core/ai/review): the
list puts first the results neither recorded nor rejected under 「確認不要の
確信度」 (要確認), least sure first. 「正（記録して次へ）」 records the chosen
one, 「誤検出（次へ）」 records a small checked area around it as nothing
there, and both move to the next one to look at. Both are training data for
「AI 学習（物体検出）」 (a labelled object, a window of background around what
the model mistook); 「確認結果で追加学習…」 opens it on the image and the
model, to train it further.

Each result not yet recorded is matched with the things of the images
before and after (継続, core/obs/continuation): a sure one is recorded as
that thing (「継続: 船舶-3」 chosen in the 継続 column), an unsure one offers
the things it may be (recorded as a new thing unless one is chosen).

The viewer has no such dock (its AI only writes results, 「DB へ転記」).
"""
from __future__ import annotations

from typing import Any, Callable, Optional

from qgis.core import QgsCoordinateTransform, QgsFeatureRequest, QgsProject, QgsRasterLayer, QgsRectangle, QgsVectorLayer
from qgis.PyQt.QtCore import QCoreApplication, Qt, QTimer
from qgis.PyQt.QtGui import QBrush, QColor
from qgis.PyQt.QtWidgets import (
    QAbstractItemView, QComboBox, QDoubleSpinBox, QHBoxLayout, QLabel, QMessageBox, QPushButton, QTreeWidget, QTreeWidgetItem, QWidget,
)

from ..core.ai.model_config import DetectModelConfig
from ..core.ai.review import ACCEPTED, REJECTED_STATE, SURE_SCORE, TO_REVIEW, queue, rejected_tags, rejection_note, review_order, state_of
from ..core.ai_results import AiResult, AiResultSource, import_entry, kind_for, recorded_tags, score_text
from ..core.obs.continuation import AUTO, CANDIDATE, Continuation, Found, continuations
from ..core.obs.flows import living
from ..qgis_io.ai_results_io import RESULTS_OF, ROW, fetch, mark_imported, place_of, results_layer
from ..qgis_io.catalog_io import CATALOG_URL
from ..qgis_io.detection_results import detection_layers, detection_results, image_ids_of, is_detection_layer, local_source
from ..qgis_io.obs_images import IMAGE_ID, observed_image
from ..qgis_io.review_io import rejection_ring
from ..qgis_io.tasks import current_feedback, run_task
from .docks import PluginDock, plain_label
from .last_inputs import InputMemory

TITLE = "AI結果"
#: The edits of a detection layer that read its results again.
EDITS = ("featureAdded", "featuresDeleted", "attributeValueChanged", "geometryChanged", "editingStopped")
#: The list's orders: for review (要確認 first, least sure first), as read.
ORDERS = ("確認優先（確信度の低い順）", "読み込み順")
#: 「未取込をすべて取り込み」 asks first above this many.
ASK_OVER = 20
#: The list's column of the review state.
REVIEW_COLUMN = 7


def _tr(text: str) -> str:
    return QCoreApplication.translate("AiResultsDock", text)


def catalog_image_id(layer) -> Optional[str]:
    """The catalog's image id of a layer opened from the catalog; None for other layers."""
    if not isinstance(layer, QgsRasterLayer):
        return None
    image_id = layer.customProperty(IMAGE_ID)
    return str(image_id) if image_id and layer.customProperty(CATALOG_URL) else None


def source_image_id(layer) -> Optional[str]:
    """
    The id to ask the external AI results of: the catalog's for a layer opened
    from the catalog, else the observation book's (its file name) for an image
    file opened by hand; None for other layers (a basemap, a web service).
    """
    found = catalog_image_id(layer)
    if found or not isinstance(layer, QgsRasterLayer) or layer.providerType() != "gdal":
        return found
    return raster_image_id(layer)


def raster_image_id(layer) -> Optional[str]:
    """The id the observation book (and so the plugin's detection) gives an image layer."""
    if not isinstance(layer, QgsRasterLayer):
        return None
    try:
        return observed_image(layer).id
    except Exception:  # noqa: BLE001 (a layer the book cannot read)
        return None


class AiResultsDock(PluginDock):
    """The image's AI results (external, and the plugin's own detection), and what records them as observations."""

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
        #: The results recorded as wrong (誤検出, a checked area around each), by tag.
        self.rejected: set[str] = set()
        #: What each row (not recorded yet) may continue, by row.
        self.continued: dict[int, Continuation] = {}
        #: The thing chosen to continue, by result tag ("" for a new thing), when not the one matched.
        self.picks: dict[str, str] = {}
        #: The rows of the plugin's own detection: the layer and the feature, by row.
        self.local_rows: dict[int, tuple[QgsVectorLayer, int]] = {}
        #: The detection layers followed (their edits read the list again), by layer id.
        self.watched: dict[str, tuple[QgsVectorLayer, list]] = {}
        self._task = None
        self._syncing = False
        self._again = QTimer(self)
        self._again.setSingleShot(True)
        self._again.setInterval(200)
        self._again.timeout.connect(lambda: self.image_id and self.show_results())

        head = QHBoxLayout()
        self.image_label = plain_label(QLabel())
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
        # The lower limit last set is kept (入力の初期値の記憶).
        InputMemory("ai_results", {"min_score": self.min_score}).restore()
        self.min_score.valueChanged.connect(lambda _v: self.show_results())
        self.min_score_memory = InputMemory("ai_results", {"min_score": self.min_score}).watch()
        filters.addWidget(self.min_score)
        filters.addStretch(1)
        self.body.addLayout(filters)

        review = QHBoxLayout()
        review.addWidget(QLabel(_tr("並び")))
        self.order = QComboBox()
        for text in ORDERS:
            self.order.addItem(_tr(text), text)
        self.order.setToolTip(_tr("確認優先: 要確認（未取込・誤検出でもなく確信度が「確認不要」未満）を確信度の低い順に先頭へ"))
        review.addWidget(self.order)
        review.addWidget(QLabel(_tr("確認不要の確信度")))
        self.sure = QDoubleSpinBox()
        self.sure.setRange(0, 1.01)
        self.sure.setSingleStep(0.05)
        self.sure.setDecimals(2)
        self.sure.setValue(SURE_SCORE)
        self.sure.setToolTip(_tr("これ以上の結果は AI を信頼して要確認に含めない（1.01 ですべて要確認）"))
        review.addWidget(self.sure)
        review.addStretch(1)
        self.body.addLayout(review)
        memory = {"order": self.order, "sure": self.sure}
        InputMemory("ai_results_review", memory).restore()
        self.order.currentIndexChanged.connect(lambda _i: self.show_results())
        self.sure.valueChanged.connect(lambda _v: self.show_results())
        self.review_memory = InputMemory("ai_results_review", memory).watch()

        self.list = QTreeWidget()
        self.list.setHeaderLabels([_tr("取込"), _tr("出どころ"), _tr("クラス"), _tr("確信度"), _tr("種別"), _tr("名前"), _tr("継続"), _tr("確認")])
        self.list.setRootIsDecorated(False)
        self.list.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self.list.itemSelectionChanged.connect(self._list_selected)
        self.list.itemDoubleClicked.connect(lambda item, _c: self.zoom_to([self.list.indexOfTopLevelItem(item)]))
        self.body.addWidget(self.list, 1)

        buttons = QHBoxLayout()
        self.import_button = QPushButton(_tr("選択を観測記録へ取り込み"))
        self.import_button.setToolTip(_tr("一覧か地図で選んだ結果を、この画像の観測記録（ポイント）として記録。ポリゴンは内側の点と長辺・短辺（長さ・幅）で記録"))
        self.import_button.clicked.connect(lambda: self.import_rows(self.selected_rows()))
        self.import_all_button = QPushButton(_tr("未取込をすべて取り込み"))
        self.import_all_button.setToolTip(_tr("取込済・誤検出を除く結果をすべて観測記録に（多いときは確認）"))
        self.import_all_button.clicked.connect(lambda: self.import_all())
        for b in (self.import_button, self.import_all_button):
            buttons.addWidget(b)
        buttons.addStretch(1)
        self.body.addLayout(buttons)

        judge = QHBoxLayout()
        self.next_button = QPushButton(_tr("次の要確認へ"))
        self.next_button.setToolTip(_tr("要確認のうち確信度の最も低い結果を選んで表示"))
        self.next_button.clicked.connect(lambda: self.next_to_review())
        self.accept_button = QPushButton(_tr("正（記録して次へ）"))
        self.accept_button.setToolTip(_tr("選んだ結果を観測記録として記録（学習の教師データになる）し、次の要確認へ"))
        self.accept_button.clicked.connect(lambda: self.accept_rows(self.selected_rows()))
        self.reject_button = QPushButton(_tr("誤検出（次へ）"))
        self.reject_button.setToolTip(_tr("選んだ結果を誤検出として、その周りを確認範囲（何もない所）に記録し、次の要確認へ。"
                                          "学習ではその周りを背景として学習。確認範囲を削除すると取り消し"))
        self.reject_button.clicked.connect(lambda: self.reject_rows(self.selected_rows()))
        self.train_button = QPushButton(_tr("確認結果で追加学習…"))
        self.train_button.setToolTip(_tr("この画像と、検出に使った学習済みモデル（あれば追加学習の元に）で「AI 学習（物体検出）」を開く"))
        self.train_button.clicked.connect(lambda: self.open_training())
        for b in (self.next_button, self.accept_button, self.reject_button, self.train_button):
            judge.addWidget(b)
        judge.addStretch(1)
        self.body.addLayout(judge)
        self.status = plain_label(QLabel())
        self.status.setWordWrap(True)
        self.body.addWidget(self.status)

        project = QgsProject.instance()
        project.layersAdded.connect(self._layers_added)
        project.layersWillBeRemoved.connect(self._layers_removed)
        self.iface.currentLayerChanged.connect(self._layer_chosen)
        context.configChanged.connect(self._config_changed)
        for layer in detection_layers(project):
            self._watch(layer)
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
        found = []
        for layer in layers:
            if is_detection_layer(layer):
                self._watch(layer)
                # A detection run: its image's results.
                found += image_ids_of(layer)[:1]
        ids = [i for i in (source_image_id(layer) for layer in layers) if i] if self.sources() else []
        ids += found
        if ids:
            # The last image opened, or detected.
            self.load(ids[-1], reveal=True)

    def _detected_ids(self) -> set[str]:
        out: set[str] = set()
        for layer, _slots in self.watched.values():
            try:
                out.update(image_ids_of(layer))
            except RuntimeError:
                pass
        return out

    def _layer_chosen(self, layer) -> None:
        image_id = source_image_id(layer) if self.sources() else None
        if image_id is None and layer is not None and layer.id() in self.watched:
            ids = image_ids_of(layer)
            image_id = ids[0] if ids and self.image_id not in ids else None
        if image_id is None and self.watched:
            # An image the plugin's detection has results of.
            own = raster_image_id(layer)
            image_id = own if own and own in self._detected_ids() else None
        if image_id and image_id != self.image_id:
            self.load(image_id)

    def _watch(self, layer: QgsVectorLayer) -> None:
        if layer.id() in self.watched:
            return
        slots = []
        for name in EDITS:
            slot = lambda *_a: self._again.start()  # noqa: E731
            getattr(layer, name).connect(slot)
            slots.append((name, slot))
        selected = lambda *_a, lyr=layer: self._map_selected(lyr)  # noqa: E731
        layer.selectionChanged.connect(selected)
        slots.append(("selectionChanged", selected))
        self.watched[layer.id()] = (layer, slots)

    def _unwatch(self, layer_id: str) -> None:
        layer, slots = self.watched.pop(layer_id, (None, []))
        for name, slot in slots:
            try:
                getattr(layer, name).disconnect(slot)
            except (TypeError, RuntimeError):
                pass

    def _layers_removed(self, ids) -> None:
        gone = set(ids)
        self.layers = [layer for layer in self.layers if layer.id() not in gone]
        shown = {layer.id() for layer, _fid in self.local_rows.values()}
        for layer_id in gone & set(self.watched):
            self._unwatch(layer_id)
        if gone & shown:
            self.local_rows = {}
            self._again.start()

    def _render_head(self) -> None:
        if self.image_id:
            self.image_label.setText(_tr("画像: {id}").format(id=self.image_id))
        elif self.sources():
            self.image_label.setText(_tr("画像を開くか「物体検出（AI）」を実行すると、その画像の結果を表示（カタログ外の画像はファイル名で検索）"))
        else:
            self.image_label.setText(_tr("「物体検出（AI）」を実行すると、その画像の結果を表示（外部AIの結果は設定ファイルの aiResultSources）"))
        self.reload_button.setEnabled(bool(self.image_id))

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
            # Only the plugin's own detection, read from the project's layers.
            self.cache[image_id] = []
            self.show_results(reveal=reveal)
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

        self._task = run_task(_tr("AI結果の読み込み"), work, done, failed)

    # ---- the list and the layers ----------------------------------------------

    def _book(self):
        dock = self.observations()
        return getattr(dock, "book", None) if dock is not None else None

    def _refresh_imported(self) -> None:
        book = self._book()
        records = living(book.records) if book is not None else []
        self.imported = recorded_tags(o.note for o in records if o.image_id == self.image_id)
        areas = [a for a in (book.areas if book is not None else []) if a.deleted_at is None and a.image_id == self.image_id]
        self.rejected = rejected_tags(a.note for a in areas)

    def state(self, row: int) -> str:
        """A row's review state (core/ai/review)."""
        return state_of(self.rows[row][1], self.imported, self.rejected, self.sure.value())

    def review_queue(self) -> list[int]:
        """The rows to look at, least sure first."""
        return queue([r for _s, r in self.rows], self.imported, self.rejected, self.sure.value())

    def show_results(self, reveal: bool = False) -> None:
        """The cached results of the image, those at or above the lowest score, listed and drawn."""
        found = self.cache.get(self.image_id or "", [])
        least = self.min_score.value()
        self._refresh_imported()
        # Every result kept: (source, result, its source's index (external) or its layer and feature (own)).
        read: list[tuple[AiResultSource, AiResult, Any]] = []
        problems = []
        for index, (source, results, error) in enumerate(found):
            if error:
                problems.append(f"{source.label}: {error}")
            read += [(source, r, index) for r in results if r.score is None or r.score >= least]
        # The plugin's own detection, from its layers (as they are now).
        local_total = 0
        book = self._book()
        taxonomy = book.kept_taxonomy() if book is not None and book.loaded else []
        for layer, _slots in list(self.watched.values()):
            try:
                found_here = detection_results(layer, self.image_id or "", self._model_kinds, taxonomy)
            except RuntimeError:
                continue
            if not found_here:
                continue
            local_total += len(found_here)
            source = local_source(layer)
            read += [(source, r, (layer, fid)) for r, fid in found_here if r.score is None or r.score >= least]
        if self.order.currentData() == ORDERS[0]:
            # For review: what to look at first, least sure first.
            read = [read[i] for i in review_order([r for _s, r, _w in read], self.imported, self.rejected, self.sure.value())]
        self.rows = [(source, r) for source, r, _w in read]
        self.local_rows = {row: where for row, (_s, _r, where) in enumerate(read) if isinstance(where, tuple)}
        per_source: list[tuple[AiResultSource, list[AiResult], list[int]]] = []
        for index, (source, _results, _error) in enumerate(found):
            rows = [row for row, (_s, _r, where) in enumerate(read) if not isinstance(where, tuple) and where == index]
            per_source.append((source, [self.rows[row][1] for row in rows], rows))
        self._syncing = True
        try:
            self.list.clear()
            for row, (source, r) in enumerate(self.rows):
                item = QTreeWidgetItem(["", source.label, r.cls, score_text(r.score), kind_for(r, source), r.name, "", ""])
                if r.geometry is None:
                    item.setToolTip(1, _tr("位置なし（取り込み不可）"))
                self.list.addTopLevelItem(item)
                self._render_state(row)
            self._match()
            for i in range(self.list.columnCount()):
                self.list.resizeColumnToContents(i)
        finally:
            self._syncing = False
        self._replace_layers(per_source)
        total = sum(len(results) for _s, results, _e in found) + local_total
        text = _tr("{n} 件（全 {total} 件）・取込済 {done} 件").format(
            n=len(self.rows), total=total, done=sum(1 for _s, r in self.rows if r.tag in self.imported))
        text += self._review_text()
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
            entry = import_entry(r, source, place[0], place[1], place[2], place[3])
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

    def _model_kinds(self, model: str) -> dict:
        """The `kinds` of the settings file's detection model of this label."""
        for m in self.context.config.ai_models:
            if isinstance(m, DetectModelConfig) and m.label == model:
                return m.kinds
        return {}

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
        own: dict[str, tuple[QgsVectorLayer, list[int]]] = {}
        for row, (layer, fid) in self.local_rows.items():
            entry = own.setdefault(layer.id(), (layer, []))
            if row in rows:
                entry[1].append(fid)
        yield from own.values()

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
        chosen: dict[str, set[int]] = {}
        for row, (lyr, fid) in self.local_rows.items():
            ids = chosen.setdefault(lyr.id(), set(lyr.selectedFeatureIds()))
            if fid in ids:
                rows.add(row)
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
            if not ids:
                continue
            to = QgsCoordinateTransform(layer.crs(), self.canvas.mapSettings().destinationCrs(), QgsProject.instance())
            for f in layer.getFeatures(QgsFeatureRequest().setFilterFids(ids)):
                if f.hasGeometry():
                    b = to.transformBoundingBox(f.geometry().boundingBox())
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

    def import_all(self, confirm: bool = True) -> list:
        """「未取込をすべて取り込み」: every result not recorded nor rejected; more than ASK_OVER asked first."""
        self._refresh_imported()
        todo = [i for i, (_s, r) in enumerate(self.rows) if r.tag not in self.imported and r.tag not in self.rejected]
        if confirm and len(todo) > ASK_OVER:
            box = QMessageBox(QMessageBox.Icon.Question, _tr("未取込をすべて取り込み"),
                              _tr("未取込 {n} 件を観測記録に取り込みますか？（誤検出は除外）").format(n=len(todo)),
                              QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No, self)
            box.setDefaultButton(QMessageBox.StandardButton.No)
            if box.exec() != QMessageBox.StandardButton.Yes:
                return []
        return self.import_rows(todo)

    def import_rows(self, rows: list[int], then: Optional[Callable[[], None]] = None) -> list:
        """Records the results of these rows (those not recorded yet, with a place) as observations of the image; `then` after."""
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
        entries, done_rows, skipped, wrong = [], [], 0, 0
        for row in rows:
            if not (0 <= row < len(self.rows)):
                continue
            source, r = self.rows[row]
            if r.tag in self.imported:
                continue
            if r.tag in self.rejected:
                # Recorded as wrong (誤検出): not recorded as a thing too (delete its checked area to take that back).
                wrong += 1
                continue
            place = place_of(r)
            if place is None:
                skipped += 1
                continue
            entry = import_entry(r, source, place[0], place[1], place[2], place[3])
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
            self.status.setText(_tr("取り込む結果なし（取込済・誤検出か、位置なし）") if rows else _tr("結果を選択"))
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
                    self._render_state(i)
                    item.setText(6, saved_names.get(i, ""))
            # What the rest may continue, without the things now in this image.
            self._match()
            text = _tr("{n} 件を観測記録に取り込み").format(n=len(saved))
            if continued:
                text += _tr("（継続 {k} 件）").format(k=len(continued))
            if skipped:
                text += _tr("（位置なし {k} 件は除外）").format(k=skipped)
            if wrong:
                text += _tr("（誤検出 {k} 件は除外）").format(k=wrong)
            self.status.setText(text + self._review_text())
            if then is not None:
                then()

        result = dock.do(_tr("AI結果の取り込み"), lambda b: b.add_many(entries, image=image_id), after)
        return result or []

    # ---- the review (能動学習) ---------------------------------------------------

    def _render_state(self, row: int) -> None:
        item = self.list.topLevelItem(row)
        if item is None or not (0 <= row < len(self.rows)):
            return
        state = self.state(row)
        item.setText(0, _tr("済") if state == ACCEPTED else _tr("誤") if state == REJECTED_STATE else "")
        if state == REJECTED_STATE:
            item.setToolTip(0, _tr("誤検出として確認範囲に記録済み（確認範囲を削除すると取り消し）"))
        item.setText(REVIEW_COLUMN, _tr("要確認") if state == TO_REVIEW else "")
        item.setBackground(REVIEW_COLUMN, QBrush(QColor(255, 210, 90, 110)) if state == TO_REVIEW else QBrush())

    def _review_text(self) -> str:
        if not self.rows:
            return ""
        left = len(self.review_queue())
        wrong = sum(1 for _s, r in self.rows if r.tag in self.rejected and r.tag not in self.imported)
        if not left and not wrong:
            return ""
        return _tr("・要確認 {n} 件・誤検出 {k} 件").format(n=left, k=wrong)

    def next_to_review(self) -> Optional[int]:
        """Chooses (and shows) the least sure result still to look at; None when none is left."""
        todo = self.review_queue()
        if not todo:
            self.list.clearSelection()
            self.status.setText(_tr("要確認の結果なし") + self._review_text())
            return None
        row = todo[0]
        self.list.clearSelection()
        item = self.list.topLevelItem(row)
        if item is not None:
            item.setSelected(True)
            self.list.scrollToItem(item)
        self.zoom_to([row])
        return row

    def accept_rows(self, rows: list[int]) -> list:
        """Records the chosen results as right (observations), then goes to the next one to look at."""
        if not rows:
            self.status.setText(_tr("結果を選択"))
            return []
        return self.import_rows(rows, then=self.next_to_review)

    def reject_rows(self, rows: list[int]) -> list:
        """
        Records the chosen results as wrong: a checked area (every kind) around
        each, its note naming the result, then goes to the next one to look at.
        """
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
        specs, tags = [], []
        for row in rows:
            if not (0 <= row < len(self.rows)):
                continue
            _source, r = self.rows[row]
            if r.tag in self.rejected or r.tag in self.imported:
                continue
            ring = rejection_ring(r)
            if ring is None:
                continue
            specs.append((ring, "", rejection_note(r)))
            tags.append(r.tag)
        if not specs:
            self.status.setText(_tr("誤検出にする結果なし（取込済・誤検出済か、位置なし）") if rows else _tr("結果を選択"))
            return []

        def after(saved) -> None:
            if not saved:
                return
            self.rejected |= set(tags)
            for i in range(len(self.rows)):
                self._render_state(i)
            self._match()
            self.status.setText(_tr("{n} 件を誤検出として確認範囲に記録").format(n=len(saved)) + self._review_text())
            self.next_to_review()

        result = dock.do(_tr("AI誤検出の記録"), lambda b: b.add_areas(specs, image=image_id), after)
        return result or []

    def _image_layer(self) -> Optional[QgsRasterLayer]:
        """The image of the list on the map."""
        for layer in QgsProject.instance().mapLayers().values():
            if isinstance(layer, QgsRasterLayer) and self.image_id in (raster_image_id(layer), catalog_image_id(layer)):
                return layer
        return None

    def training_inputs(self) -> dict:
        """What 「AI 学習（物体検出）」 opens with: this image, and the trained model that found its results (to train further)."""
        from ..processing_provider.train_algorithm import TrainAlgorithm
        from ..qgis_io.settings import trained_models

        out: dict = {TrainAlgorithm.SOURCE: 0}
        layer = self._image_layer()
        if layer is not None:
            out[TrainAlgorithm.IMAGES] = [layer.id()]
        used = {r.model for _s, r in self.rows if r.model}
        trained = {_tr("学習済み: {label}").format(label=label): path for label, path in trained_models()}
        trained.update({f"学習済み: {label}": path for label, path in trained_models()})
        base = next((trained[m] for m in used if m in trained), None)
        if base:
            out[TrainAlgorithm.BASE_MODEL] = base
        return out

    def open_training(self) -> None:
        import processing

        processing.execAlgorithmDialog("skywatch:train_detector", self.training_inputs())

    def close_down(self) -> None:
        """Called when the plugin unloads."""
        if self._task is not None:
            try:
                self._task.cancel()
            except RuntimeError:
                pass
        self._again.stop()
        for layer_id in list(self.watched):
            self._unwatch(layer_id)
        project = QgsProject.instance()
        for signal, slot in ((project.layersAdded, self._layers_added), (project.layersWillBeRemoved, self._layers_removed),
                             (self.iface.currentLayerChanged, self._layer_chosen), (self.context.configChanged, self._config_changed)):
            try:
                signal.disconnect(slot)
            except (TypeError, RuntimeError):
                pass

