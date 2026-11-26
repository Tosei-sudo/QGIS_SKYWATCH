"""
組織の関心地域 in QGIS (core/named_areas.py): the named area layer's rows as
read from Esri, kept once for the plugin (`OrgAreaBook`: the 「組織」 tab of
「関心地域・地点」 edits them, the 新着画像 cards name the areas of interest an
image covers), and shown on the map as one memory layer 「組織の関心地域」
(each area in its colour or its category's, labelled).

Only the areas of interest (用途 関心地域・両方) are read with their shapes:
the place names (a country's cities) are listed by their attributes, and a
shape is read when one is shown or edited.
"""
from __future__ import annotations

import time
from dataclasses import replace
from typing import Callable, Optional, Sequence

from qgis.core import (
    QgsFeature, QgsField, QgsFillSymbol, QgsGeometry, QgsPalLayerSettings, QgsProject, QgsProperty, QgsRectangle,
    QgsSingleSymbolRenderer, QgsSymbolLayer, QgsTextBufferSettings, QgsTextFormat, QgsVectorLayer, QgsVectorLayerSimpleLabeling,
)
from qgis.PyQt.QtCore import QObject, QVariant, pyqtSignal
from qgis.PyQt.QtGui import QColor

from ..core.named_areas import (
    DEFAULT_COLOR, AreaCategory, AreaList, NamedAreaSettings, OrgArea, area_color, area_sort_key, category_counts,
    areas_at, category_sort_key, delete_categories, load_areas, number_blanks, read_shapes, rename_category_in_areas, save_area,
    save_category, settings_for,
)
from .catalog_io import footprint
from .pinned_layers import top_index
from .tasks import run_task

#: The custom property that marks the plugin's layer.
LAYER_PROPERTY = "mikage/orgAreas"
LAYER_NAME = "組織の関心地域"
#: A failed read is tried again after this long when asked (the cards ask as they come into view).
RETRY_S = 600


def geometry_of(area: OrgArea) -> Optional[QgsGeometry]:
    """An area's shape as a QGIS geometry (WGS 84); None without one (or not read)."""
    if not area.has_shape:
        return None
    return footprint({"rings": area.rings})


class OrgAreaBook(QObject):
    """The named area layer's rows (`list`: None until read), read and saved in tasks."""

    #: The rows were read or saved, or the settings file changed.
    changed = pyqtSignal()

    def __init__(self, context, parent: Optional[QObject] = None):
        super().__init__(parent)
        self.context = context
        self.list: Optional[AreaList] = None
        self.failure = ""
        self.failed_at = 0.0
        self.loading = False
        self._again = False
        self._then: list[Callable[[], None]] = []
        self._geometries: dict[str, Optional[QgsGeometry]] = {}
        self._tasks: list = []
        context.configChanged.connect(self._config_changed)

    @property
    def settings(self) -> Optional[NamedAreaSettings]:
        return settings_for(self.context.config)

    @property
    def areas(self) -> Optional[list[OrgArea]]:
        return self.list.areas if self.list is not None else None

    @property
    def categories(self) -> list[AreaCategory]:
        return list(self.list.categories or []) if self.list is not None else []

    def _token(self, source) -> Optional[str]:
        return None

    def _tokens(self, s: NamedAreaSettings) -> dict:
        return {"areas": self._token(s.areas), "categories": self._token(s.categories), "countries": self._token(s.countries)}

    def _config_changed(self) -> None:
        self.list, self.failure, self.failed_at = None, "", 0.0
        self._geometries = {}
        if self.loading:
            self._again = True
        self.changed.emit()

    def _start(self, description: str, work, done, failed) -> None:
        task = run_task(description, work, done, failed)
        self._tasks.append(task)
        for signal in (task.taskCompleted, task.taskTerminated):
            signal.connect(lambda task=task: self._tasks.remove(task) if task in self._tasks else None)

    # ---- reading -------------------------------------------------------------------------------

    def want(self) -> None:
        """Read when not read yet (and not failed a moment ago): what the cards and the dock call as they are shown."""
        if self.settings is None or self.list is not None or self.loading:
            return
        if self.failure and time.time() - self.failed_at < RETRY_S:
            return
        self.load()

    def load(self, then: Optional[Callable[[], None]] = None) -> None:
        """Reads the rows again (in a task); `then` once read."""
        s = self.settings
        if s is None:
            return
        if then is not None:
            self._then.append(then)
        if self.loading:
            self._again = True
            return
        self.loading, self._again = True, False
        client, tokens = self.context.esri(), self._tokens(s)

        def work(task):
            return load_areas(client, s, tokens, task.isCanceled)

        def done(result: AreaList) -> None:
            self.loading = False
            if s != self.settings or self._again:
                self.load()
                return
            self.list, self.failure = result, ""
            self._geometries = {}
            waiting, self._then = self._then, []
            self.changed.emit()
            for f in waiting:
                f()

        def failed(message: str) -> None:
            self.loading = False
            if s != self.settings or self._again:
                self.load()
                return
            self.failure, self.failed_at, self._then = message, time.time(), []
            self.changed.emit()

        self.changed.emit()  # 「取得中…」
        self._start("組織の関心地域の取得", work, done, failed)

    def shape(self, area: OrgArea, done: Callable[[OrgArea], None], failed: Callable[[str], None] = lambda _m: None) -> None:
        """The area with its shape: at once when read, else read (a place name's) in a task."""
        if area.rings is not None or area.oid is None:
            done(area)
            return
        s, lst = self.settings, self.list
        if s is None or lst is None:
            failed("地名のレイヤーが未設定")
            return
        client, token, oid_field = self.context.esri(), self._token(s.areas), lst.oid_field

        def work(_task):
            return read_shapes(client, s.areas, [area.oid], token, oid_field).get(area.oid, [])

        def finished(rings) -> None:
            for a in self.areas or []:
                if a.oid == area.oid:
                    a.rings = rings
            done(replace(area, rings=rings))

        self._start("地域の形の取得", work, finished, failed)

    def shapes(self, areas: Sequence[OrgArea], done: Callable[[list], None], failed: Callable[[str], None] = lambda _m: None) -> None:
        """The areas with their shapes (those not read yet read together in one task), in their order."""
        missing = [a for a in areas if a.rings is None and a.oid is not None]
        if not missing:
            done(list(areas))
            return
        s, lst = self.settings, self.list
        if s is None or lst is None:
            failed("地名のレイヤーが未設定")
            return
        client, token, oid_field = self.context.esri(), self._token(s.areas), lst.oid_field

        def work(task):
            return read_shapes(client, s.areas, [a.oid for a in missing], token, oid_field, task.isCanceled)

        def finished(found: dict) -> None:
            for a in self.areas or []:
                if a.oid in found and a.rings is None:
                    a.rings = found[a.oid]
            done([a if a.rings is not None or a.oid is None else replace(a, rings=found.get(a.oid, [])) for a in areas])

        self._start("地域の形の取得", work, finished, failed)

    def at_point(self, lon: float, lat: float, done: Callable[[list], None], failed: Callable[[str], None]) -> bool:
        """The active areas (any 用途) holding a point, with their shapes, the smallest first; False without the layer."""
        s = self.settings
        if s is None:
            return False
        client, token = self.context.esri(), self._token(s.areas)
        self._start("地点の地域の取得", lambda _task: areas_at(client, s, lon, lat, token), done, failed)
        return True

    # ---- writing -------------------------------------------------------------------------------

    def save(self, area: OrgArea, shape_changed: bool, done: Optional[Callable[[OrgArea], None]] = None,
             failed: Optional[Callable[[str], None]] = None) -> None:
        s = self.settings
        if s is None:
            if failed:
                failed("設定ファイルに namedAreas なし")
            return
        client, token = self.context.esri(), self._token(s.areas)

        def work(_task):
            return save_area(client, s, area, token, shape_changed)

        def finished(saved: OrgArea) -> None:
            if self.list is not None and s == self.settings:
                kept = [a for a in self.list.areas if a.oid != saved.oid]
                self.list.areas = sorted(kept + [saved], key=area_sort_key)
                self._geometries.pop(saved.key, None)
                self._geometries.pop(f"#{saved.oid}", None)
                self.changed.emit()
            if done:
                done(saved)

        self._start("組織の関心地域の保存", work, finished, failed or (lambda _m: None))

    def number_blanks(self, done: Callable[[int, list], None], failed: Callable[[str], None]) -> None:
        """Gives every row without a 管理番号 one, then reads the rows again."""
        s = self.settings
        if s is None:
            return
        client, token = self.context.esri(), self._token(s.areas)

        def work(task):
            return number_blanks(client, s, token, task.isCanceled)

        def finished(result) -> None:
            n, problems = result
            self.load(lambda: done(n, problems))

        self._start("管理番号の付番", work, finished, failed)

    def save_category(self, category: AreaCategory, original: Optional[AreaCategory], done: Callable[[AreaCategory, int], None],
                      failed: Callable[[str], None]) -> None:
        """Saves a category (and, its id changed, moves its areas to the new id), then reads the rows again."""
        s = self.settings
        if s is None or s.categories is None:
            failed("カテゴリーの表が未設定（管理者に連絡: namedAreas.categories）［E703］")
            return
        client, token, area_token = self.context.esri(), self._token(s.categories), self._token(s.areas)
        renamed = original is not None and original.id != category.id.strip()

        def work(_task):
            saved = save_category(client, s.categories, category, token, original)
            moved = rename_category_in_areas(client, s, original.id, saved.id, area_token) if renamed else 0
            return saved, moved

        def finished(result) -> None:
            saved, moved = result
            self.load(lambda: done(saved, moved))

        self._start("地域のカテゴリーの保存", work, finished, failed)

    def save_category_orders(self, changes: Sequence[AreaCategory], done: Callable[[], None], failed: Callable[[str], None]) -> None:
        s = self.settings
        if s is None or s.categories is None:
            return
        client, token = self.context.esri(), self._token(s.categories)

        def work(_task):
            for c in changes:
                save_category(client, s.categories, c, token)
            return len(changes)

        self._start("地域のカテゴリーの並べ替え", work, lambda _n: self.load(done), failed)

    def delete_categories(self, chosen: Sequence[AreaCategory], done: Callable[[list, list], None], failed: Callable[[str], None]) -> None:
        s = self.settings
        if s is None or s.categories is None or self.list is None:
            return
        client, token, chosen = self.context.esri(), self._token(s.categories), list(chosen)
        counts = category_counts(self.list.areas)

        def work(_task):
            return delete_categories(client, s.categories, chosen, counts, token)

        def finished(result) -> None:
            gone, problems = result
            self.load(lambda: done(gone, problems))

        self._start("地域のカテゴリーの削除", work, finished, failed)

    # ---- looking -------------------------------------------------------------------------------

    def interest_areas(self) -> list[OrgArea]:
        """The active areas of interest (用途 関心地域・両方)."""
        return [a for a in self.areas or [] if a.is_interest and a.active]

    def find(self, key: str) -> Optional[OrgArea]:
        return next((a for a in self.areas or [] if a.key == key), None)

    def find_id(self, area_id: str) -> Optional[OrgArea]:
        k = area_id.strip().casefold()
        return next((a for a in self.areas or [] if a.id and a.id.casefold() == k), None) if k else None

    def color(self, area: OrgArea) -> str:
        return area_color(area, self.categories, DEFAULT_COLOR)

    def sorted_categories(self) -> list[AreaCategory]:
        return sorted(self.categories, key=category_sort_key)

    def geometry(self, area: OrgArea) -> Optional[QgsGeometry]:
        if area.key not in self._geometries or self._geometries[area.key] is None and area.has_shape:
            self._geometries[area.key] = geometry_of(area)
        return self._geometries[area.key]

    def over(self, esri_geometry: Optional[dict]) -> list[OrgArea]:
        """The active areas of interest an image's footprint (Esri JSON, WGS 84) covers or crosses."""
        areas = self.interest_areas()
        shape = footprint(esri_geometry) if areas else None
        if shape is None or shape.isEmpty():
            return []
        box = shape.boundingBox()
        out = []
        for area in areas:
            g = self.geometry(area)
            if g is not None and box.intersects(g.boundingBox()) and shape.intersects(g):
                out.append(area)
        return out


# ---- the map layer ---------------------------------------------------------------------------------


def layer_of(project: Optional[QgsProject] = None) -> Optional[QgsVectorLayer]:
    """The plugin's 「組織の関心地域」 layer in the project."""
    project = project or QgsProject.instance()
    return next((layer for layer in project.mapLayers().values()
                 if layer.customProperty(LAYER_PROPERTY) and isinstance(layer, QgsVectorLayer)), None)


def _style(layer: QgsVectorLayer) -> None:
    colour = f"coalesce(\"color\", '{DEFAULT_COLOR}')"
    base = QColor(DEFAULT_COLOR)
    rgb = f"{base.red()},{base.green()},{base.blue()}"
    symbol = QgsFillSymbol.createSimple({"color": rgb + ",40", "outline_color": rgb, "outline_width": "0.8", "outline_style": "dash"})
    symbol.symbolLayer(0).setDataDefinedProperty(
        QgsSymbolLayer.PropertyFillColor, QgsProperty.fromExpression(f"set_color_part({colour}, 'alpha', 40)"))
    symbol.symbolLayer(0).setDataDefinedProperty(QgsSymbolLayer.PropertyStrokeColor, QgsProperty.fromExpression(colour))
    layer.setRenderer(QgsSingleSymbolRenderer(symbol))
    text = QgsTextFormat()
    text.setSize(9)
    text.setColor(QColor("#0f172a"))
    buffer = QgsTextBufferSettings()
    buffer.setEnabled(True)
    buffer.setColor(QColor("#ffffff"))
    buffer.setSize(1.0)
    text.setBuffer(buffer)
    labels = QgsPalLayerSettings()
    labels.fieldName = "name"
    labels.setFormat(text)
    layer.setLabeling(QgsVectorLayerSimpleLabeling(labels))
    layer.setLabelsEnabled(True)


FIELDS = (("id", "管理番号"), ("name", "名前"), ("category", "カテゴリー"), ("owner_group", "担当グループ"), ("color", "色"),
          ("note", "メモ"))


def _new_layer() -> QgsVectorLayer:
    layer = QgsVectorLayer("MultiPolygon?crs=EPSG:4326", LAYER_NAME, "memory")
    layer.dataProvider().addAttributes([QgsField(n, QVariant.String) for n, _a in FIELDS] + [QgsField("key", QVariant.String)])
    layer.updateFields()
    for i, (_n, alias) in enumerate(FIELDS):
        layer.setFieldAlias(i, alias)
    layer.setCustomProperty(LAYER_PROPERTY, 1)
    layer.setCustomProperty("skipMemoryLayersCheck", 1)
    layer.setReadOnly(True)
    layer.setAbstract("組織の関心地域（MIKAGE。編集はドック「関心地域・地点」の「組織」で。Esri から読み直す）")
    _style(layer)
    return layer


def show_layer(book: OrgAreaBook, project: Optional[QgsProject] = None, add: bool = True) -> Optional[QgsVectorLayer]:
    """The layer (made and put at the top of the layer list when missing and `add`) holding the areas of interest as they are now."""
    project = project or QgsProject.instance()
    layer = layer_of(project)
    if layer is None:
        if not add:
            return None
        layer = _new_layer()
        project.addMapLayer(layer, False)
        project.layerTreeRoot().insertLayer(top_index(project.layerTreeRoot()), layer)
    fill(layer, book)
    return layer


def fill(layer: QgsVectorLayer, book: OrgAreaBook) -> None:
    provider = layer.dataProvider()
    provider.truncate()
    names = {c.id.casefold(): c.name for c in book.categories}
    features = []
    for area in book.interest_areas():
        g = book.geometry(area)
        if g is None:
            continue
        g = QgsGeometry(g)
        if not g.isMultipart():
            g.convertToMultiType()
        f = QgsFeature(layer.fields())
        f.setAttributes([area.id, area.name, names.get(area.category.casefold(), area.category), area.owner_group, book.color(area),
                         area.note, area.key])
        f.setGeometry(g)
        features.append(f)
    provider.addFeatures(features)
    layer.updateExtents()
    layer.triggerRepaint()


def remove_layer(project: Optional[QgsProject] = None) -> None:
    project = project or QgsProject.instance()
    layer = layer_of(project)
    if layer is not None:
        project.removeMapLayer(layer.id())


def select_on_map(keys: Sequence[str], project: Optional[QgsProject] = None) -> None:
    layer = layer_of(project)
    if layer is not None:
        wanted = set(keys)
        layer.selectByIds([f.id() for f in layer.getFeatures() if f["key"] in wanted])


def box_of(book: OrgAreaBook, areas: Sequence[OrgArea]) -> Optional[QgsRectangle]:
    box: Optional[QgsRectangle] = None
    for area in areas:
        g = book.geometry(area)
        if g is None:
            continue
        b = g.boundingBox()
        if box is None:
            box = QgsRectangle(b)
        else:
            box.combineExtentWith(b)
    return box
