"""
画像カタログ: searching the image catalogs of the settings file (over the
map's extent, an area drawn, a vector layer's geometry as it is, or anywhere, by dates, a free word, sensor, angle, azimuth and SQL), the list
of images found with their footprints on the map, opening them as raster
layers (a COG through /vsicurl/, a local path through `pathMappings`), the
list as a layer, 「類似画像の検索」 and 「この条件で監視…」 (the watches are in
the 通知 dock).

「前の画像を閉じる」 (per user): looking through images one after another,
opening the next closes the layers opened here before.

A search lists the newest `maxResults` images; 「続きを読む」 under the list
adds the next `maxResults` (older ones) of the same search (its area and
conditions as they were when searched, not as the form says now).

「ファイルなしで記録」: the images whose raw data is in another system only
go to 観測記録 as images without their files (the catalog's id, time and
footprint, so the records are the ones the image would have if opened). A
catalog of such images only (`imageFiles: false`) has this button instead
of 「開く」 (a double click too); elsewhere it is offered when an image
cannot be opened.
"""
from __future__ import annotations

from dataclasses import dataclass, field, replace
from typing import Any, Callable, Optional

from qgis.core import Qgis, QgsMapLayer, QgsProject
from qgis.gui import QgsMapLayerComboBox
from qgis.PyQt.QtCore import Qt, QTimer
from qgis.PyQt.QtWidgets import (
    QAbstractItemView, QApplication, QCheckBox, QComboBox, QDoubleSpinBox, QFileDialog, QFormLayout, QGroupBox, QHBoxLayout,
    QLabel, QLineEdit, QPushButton, QSpinBox, QTableView, QVBoxLayout, QWidget,
)

from ..core.catalog.config import CatalogConfig
from ..core.catalog.paths import PathError, PathMapping
from ..core.catalog.query import CatalogSearch
from ..core.catalog.search import CatalogLayerInfo, CatalogRecord, listed_image, read_catalog_layer, search_catalog, sensors_of
from ..core.catalog.similar import SimilarTolerance, rank_similar, similar_search
from ..core.catalog.watch import areas_of
from ..core.util import read_clock, read_day_time, read_number
from ..qgis_io import settings
from ..qgis_io.overviews_io import queue as overview_queue
from ..qgis_io.catalog_io import (
    Cancelled, add_record_layers, close_layers, footprint, layer_area, open_record, overlap_measure, record_of_layer, results_layer,
)
from ..qgis_io.geometry import from_wgs84, to_wgs84
from ..qgis_io.tasks import run_task
from .catalog_model import CatalogModel
from .catalog_overlay import DrawTool, FootprintOverlay, FootprintTool
from .docks import PluginDock, ask_no, plain_label, plain_tip
from .flow_layout import FlowLayout
from .list_views import ListViews
from .image_files_dialog import choose_image_files

TITLE = "画像カタログ"


def _number(edit: QLineEdit) -> Optional[float]:
    """The number typed (full-width digits and a ° too); None when empty or unreadable (`input_problem` tells which)."""
    try:
        return read_number(edit.text())
    except ValueError:
        return None


#: The layers a search can be over: vector layers with geometries.
try:
    from qgis.core import QgsMapLayerProxyModel

    _GEOMETRY_LAYERS = QgsMapLayerProxyModel.Filter.HasGeometry
except AttributeError:  # older bindings: the enum unscoped
    from qgis.core import QgsMapLayerProxyModel

    _GEOMETRY_LAYERS = QgsMapLayerProxyModel.HasGeometry


@dataclass
class _Listed:
    """The search the list shows (or is being read), to read on from with 「続きを読む」."""
    catalog: CatalogConfig
    search: CatalogSearch
    box: Any
    #: 「類似画像の検索」: the images read → those listed (the most alike first) and their overlaps; None for the usual search.
    rank: Optional[Callable[[list], tuple[list, dict]]] = None
    #: Every image read so far, in the service's order (newest first).
    read: list = field(default_factory=list)
    total: Optional[int] = None
    #: Where the next images start (the service's offset); None when there are no more.
    next_offset: Optional[int] = None


def _key(record: CatalogRecord) -> Any:
    """What tells an image of the catalog from the others (an image read twice, the list having moved on since, is listed once)."""
    return ("oid", record.object_id) if record.object_id is not None else ("id", record.id, record.acquired)


def _when(edit: QLineEdit, day: str = "") -> str:
    """
    The day, or day and time, typed as `yyyy-mm-dd` or `yyyy-mm-dd hh:mm`
    (2026/11/9 10:30, 20261109, full-width digits too; a time alone on
    `day`, the start's day); '' when empty or unreadable (`input_problem`
    tells which).
    """
    try:
        return read_day_time(edit.text(), day)
    except ValueError:
        return ""


def _is_clock(text: str) -> bool:
    """Whether `text` is a time of day alone (12:00)."""
    try:
        return bool(read_clock(text))
    except ValueError:
        return False


def _period(start: QLineEdit, end: QLineEdit) -> tuple[str, str]:
    """A period typed (`_when`): the end may be a time alone (〜 12:00), on the start's day."""
    a = _when(start)
    return a, _when(end, a[:10])


#: The hint of the period boxes.
PERIOD_TIP = ("日付だけなら、その日の 0 時から（〜の左）・その日の終わりまで（〜の右）。"
              "時刻をつけると分（秒も書けば秒）の単位で絞る（例 2026-11-09 10:30。10時30分・1030 も可）。"
              "右に時刻だけ（12:00）なら左と同じ日。時刻はこの PC の時刻で、一覧の日時と同じ")


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
        #: The search listed (「続きを読む」); None for a list that is not a search (an event's images).
        self._listed: Optional[_Listed] = None
        #: Searches (and 続き) being read.
        self._reads = 0
        self._tasks: list = []

        form = QFormLayout()
        self.catalog_box = QComboBox()
        self.catalog_box.currentIndexChanged.connect(self._catalog_changed)
        form.addRow(self.tr("カタログ"), self.catalog_box)
        self.area_box = QComboBox()
        self.area_box.addItem(self.tr("表示範囲"), "extent")
        self.area_box.addItem(self.tr("描いた範囲"), "drawn")
        self.area_box.addItem(self.tr("レイヤー"), "layer")
        self.area_box.addItem(self.tr("指定なし"), "none")
        self.area_box.setCurrentIndex(0)
        self.area_box.activated.connect(self._area_chosen)
        self.area_box.currentIndexChanged.connect(lambda *_: self._show_layer_row())
        self.draw_button = QPushButton(self.tr("範囲を描く"))
        self.draw_button.setCheckable(True)
        self.draw_button.setToolTip(self.tr("クリックで頂点を追加、ダブルクリックか右クリックで確定、Esc で取消"))
        self.draw_button.toggled.connect(self._draw_toggled)
        area_row = QHBoxLayout()
        area_row.addWidget(self.area_box, 1)
        area_row.addWidget(self.draw_button)
        # 「レイヤー」: the features of a vector layer, searched over as they are (not their extent).
        self.area_layer = QgsMapLayerComboBox()
        self.area_layer.setFilters(_GEOMETRY_LAYERS)
        self.area_layer.setAllowEmptyLayer(True)
        self.area_layer.setToolTip(self.tr("地物の形そのもの（外接矩形ではなく）と重なる画像を検索"))
        self.selected_only = QCheckBox(self.tr("選択地物のみ"))
        self.layer_row = QWidget()
        layer_row = QHBoxLayout(self.layer_row)
        layer_row.setContentsMargins(0, 0, 0, 0)
        layer_row.addWidget(self.area_layer, 1)
        layer_row.addWidget(self.selected_only)
        self.layer_row.hide()
        area_column = QVBoxLayout()
        area_column.addLayout(area_row)
        area_column.addWidget(self.layer_row)
        form.addRow(self.tr("範囲"), area_column)
        #: The area drawn on the map (its corners, longitude / latitude; the ring closed); None before one is drawn.
        self.drawn: Optional[list] = None
        self.acquired_from, self.acquired_to = QLineEdit(), QLineEdit()
        form.addRow(self.tr("撮像日時"), self._period_row(self.acquired_from, self.acquired_to))
        # 「フリーワード」: words looked for in the catalog's `fields.keywords` (shown only for a catalog that has them).
        self.keyword = QLineEdit()
        self.keyword.setClearButtonEnabled(True)
        self.keyword.returnPressed.connect(self.search)
        self.keyword_label = QLabel(self.tr("フリーワード"))
        form.addRow(self.keyword_label, self.keyword)
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
        more.addRow(self.tr("登録日時"), self._period_row(self.registered_from, self.registered_to))
        self.system_from, self.system_to = QLineEdit(), QLineEdit()
        more.addRow(self.tr("システム登録日時"), self._period_row(self.system_from, self.system_to))
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
        self.reference_label = plain_label(QLabel())
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
        self.status = plain_label(QLabel())
        self.status.setWordWrap(True)
        row.addWidget(self.status, 1)
        # 分担 (`assignments`): who works which image, and only those nobody has taken.
        self.unworked_only = QCheckBox(self.tr("未着手のみ"))
        self.unworked_only.setToolTip(self.tr("まだ誰も担当にしていない画像だけを一覧に出す（分担）"))
        self.unworked_only.toggled.connect(lambda _on: self._apply_work_filter())
        row.addWidget(self.unworked_only)
        self.body.addLayout(row)

        self.model = CatalogModel(self)
        self.table = QTableView()
        self.table.setModel(self.model)
        self.table.setSortingEnabled(True)
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.verticalHeader().hide()
        self.table.doubleClicked.connect(lambda index: self._activated(self.model.records[index.row()]))
        self.table.selectionModel().selectionChanged.connect(self._selection_changed)
        self.model.layoutChanged.connect(self._redraw)
        self.model.layoutChanged.connect(self._apply_work_filter)
        self.body.addWidget(self.table, 1)
        # 「続きを読む」: the next `maxResults` (older) images of the search listed, under the list.
        self.more_button = QPushButton()
        self.more_button.setToolTip(self.tr("この検索の続き（さらに古い画像）を一覧に足す。"
                                            "範囲と条件は検索したときのまま（地図を動かしても変わらない）"))
        self.more_button.clicked.connect(self.read_more)
        self.more_button.hide()
        self.body.addWidget(self.more_button)
        # 「ビュー」: the columns, the sort and the search conditions kept under a name (per user).
        self.views = ListViews("catalog", self.table, {
            "catalog": self.catalog_box, "area": self.area_box, "acquiredFrom": self.acquired_from, "acquiredTo": self.acquired_to,
            "more": self.more, "registeredFrom": self.registered_from, "registeredTo": self.registered_to, "systemFrom": self.system_from,
            "systemTo": self.system_to, "sensor": self.sensor_box, "maxAngle": self.max_angle, "azimuthFrom": self.azimuth_from,
            "azimuthTo": self.azimuth_to, "where": self.where, "unworkedOnly": self.unworked_only, "keyword": self.keyword,
        }, self._view_applied, self)
        row.addWidget(self.views.button)
        #: The 分担 of the listed images read again every minute while the dock is shown.
        self._work_asked = 0
        self._work_timer = QTimer(self)
        self._work_timer.setInterval(60_000)
        self._work_timer.timeout.connect(self.load_work)

        # 「前の画像を閉じる」 (tsudo, 2026-11-10): looking through images one after another, the one opened before closes.
        self.close_previous = QCheckBox(self.tr("前の画像を閉じる"))
        self.close_previous.setToolTip(self.tr("「開く」・ダブルクリックで画像を開いたら、その前にここから開いた画像のレイヤーを"
                                               "プロジェクトから外す（連続して画像を見るときに。開けなかったときは外さない。PC ごとに記憶）"))
        self.close_previous.setChecked(settings.close_previous())
        self.close_previous.toggled.connect(settings.set_close_previous)
        #: The layers opened last with 「前の画像を閉じる」 on (their ids), which close when the next images open.
        self.last_opened: list[str] = []

        buttons = FlowLayout()
        self.buttons: dict[str, QPushButton] = {}
        tips = {
            "open": self.tr("選んだ画像をラスターレイヤーとして開く（ダブルクリックでも）"),
            "zoom": self.tr("選んだ画像の範囲へ地図を移動"),
            "copy": self.tr("選んだ画像のファイルのパスをクリップボードへ"),
            "layer": self.tr("一覧の画像の範囲を地物レイヤーとして追加（属性つき）"),
            "similar": self.tr("選んだ画像と同じ場所を写した画像を探す"),
            "watch": self.tr("今の検索条件で新しい画像を見張る（新着画像の通知）"),
            "pick": self.tr("地図上の範囲をクリックして一覧の画像を選ぶ"),
            "folders": self.tr("この PC で画像のパスの読み替え（pathMappings）に選んだフォルダを忘れる"),
            "event": self.tr("選んだ画像をイベントに紐付ける"),
            "record": self.tr("選んだ画像を、画像ファイルを開かずに観測記録の画像一覧へ（ID・撮像日時・範囲はカタログから。"
                              "生データが別システムにしかない画像に。ダブルクリックでも）。背景地図の上をクリックして記録"),
        }
        for key, text, slot in [
            ("open", self.tr("開く"), lambda: self.open_records(self.chosen())),
            ("record", self.tr("ファイルなしで記録"), lambda: self.record_without_files()),
            ("zoom", self.tr("範囲へ移動"), self.zoom_to_chosen),
            ("copy", self.tr("パスをコピー"), self.copy_paths),
            ("layer", self.tr("レイヤーとして追加"), self.add_layer),
            ("similar", self.tr("類似画像の検索"), self._similar_of_chosen),
            ("watch", self.tr("この条件で監視…"), self._watch),
            ("pick", self.tr("地図で選択"), None),
            ("folders", self.tr("フォルダの対応を消去"), self.clear_folders),
            ("event", self.tr("イベントに追加"), self.add_to_event),
        ]:
            button = QPushButton(text)
            button.setToolTip(tips[key])
            if slot is None:
                button.setCheckable(True)
                button.toggled.connect(self._pick_toggled)
            else:
                button.clicked.connect(slot)
            buttons.addWidget(button)
            self.buttons[key] = button
            if key == "open":
                buttons.addWidget(self.close_previous)
        # 「フットプリント」: how the listed images are drawn on the map (filled ones hid the images opened under them).
        self.footprint_box = QComboBox()
        for text, style in ((self.tr("塗りつぶし"), "fill"), (self.tr("枠だけ"), "outline"), (self.tr("非表示"), "hidden")):
            self.footprint_box.addItem(text, style)
        self.footprint_box.setCurrentIndex(max(0, self.footprint_box.findData(settings.footprint_style())))
        self.footprint_box.setToolTip(self.tr("地図上の画像の範囲（フットプリント）の表示\n"
                                              "塗りつぶし: 範囲を薄く塗る\n"
                                              "枠だけ: 塗らずに枠線だけ（画像 ID は範囲の上端に）\n"
                                              "非表示: 地図に出さない（「地図で選択」のクリックは効く）"))
        self.footprint_box.currentIndexChanged.connect(lambda _i: self.set_footprint_style(self.footprint_box.currentData()))
        footprints = QWidget()
        footprint_row = QHBoxLayout(footprints)
        footprint_row.setContentsMargins(0, 0, 0, 0)
        footprint_row.addWidget(QLabel(self.tr("フットプリント")))
        footprint_row.addWidget(self.footprint_box)
        buttons.addWidget(footprints)
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

    def _view_applied(self) -> None:
        """A view chosen: the list searched again with its conditions (when it was searched before)."""
        if self.model.records:
            self.search()

    # --- Form -------------------------------------------------------------------------------------

    def _period_row(self, a: QLineEdit, b: QLineEdit) -> QWidget:
        """A period's two boxes: a day, or a day and a time (PERIOD_TIP)."""
        for box in (a, b):
            box.setToolTip(self.tr(PERIOD_TIP))
        return self._range(a, b, "YYYY-MM-DD [hh:mm]", "YYYY-MM-DD [hh:mm]")

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
        self.buttons["event"].setVisible(self.context.config.events is not None)
        self.unworked_only.setVisible(self.context.config.assignments is not None)
        if self.context.config.assignments is None:
            self.unworked_only.setChecked(False)
        self.search_button.setEnabled(bool(self.catalogs()))
        if not self.catalogs():
            self.status.setText(self.tr("設定ファイルに画像カタログなし"))
        if self._shown_catalog is not None:
            # The list's catalog as the settings file says now (`imageFiles` changed, say).
            self._shown_catalog = next((c for c in self.catalogs() if c.url == self._shown_catalog.url and c.label == self._shown_catalog.label),
                                       self._shown_catalog)
        if self._listed is not None:
            # 「続きを読む」 asks the catalog as it is now (`maxResults`, the token); one no longer there has no 続き.
            now = next((c for c in self.catalogs() if c.url == self._listed.catalog.url and c.label == self._listed.catalog.label), None)
            if now is None:
                self._listed.next_offset = None
            else:
                self._listed.catalog = now
            self._show_more()
        self._catalog_changed()

    def _catalog_changed(self) -> None:
        self.sensor_box.clear()
        self.sensor_box.addItem("")
        catalog = self.catalog()
        self._show_keyword(catalog)
        self._show_file_buttons()
        if catalog is None or not catalog.fields.get("sensor"):
            return
        client = self.context.esri()
        token = self.context.token_for(catalog.url, catalog.token)
        self._start(self.tr("センサーの取得"), lambda task: sensors_of(client, catalog, token),
                    lambda sensors: self.sensor_box.addItems(sensors) if self.catalog() is catalog else None, lambda m: None)

    def has_files(self) -> bool:
        """Whether the list's catalog (else the one chosen) has image files to open (not `imageFiles: false`)."""
        catalog = self._shown_catalog or self.catalog()
        return catalog is None or catalog.image_files

    def _show_file_buttons(self) -> None:
        """A catalog without image files: 「ファイルなしで記録」 instead of 「開く」, and no paths to copy or folders to forget."""
        files = self.has_files()
        for key in ("open", "copy", "folders"):
            self.buttons[key].setVisible(files)
        self.close_previous.setVisible(files)
        self.buttons["record"].setVisible(not files)

    def _show_keyword(self, catalog: Optional[CatalogConfig]) -> None:
        """「フリーワード」 only for a catalog with `fields.keywords`, its hint naming them."""
        fields = catalog.keyword_fields if catalog is not None else []
        self.keyword_label.setVisible(bool(fields))
        self.keyword.setVisible(bool(fields))
        if not fields:
            return
        labels = "・".join(catalog.keyword_labels())
        self.keyword.setPlaceholderText(self.tr("{labels} に部分一致").format(labels=labels))
        self.keyword.setToolTip(self.tr(
            "{labels} のどれかに入力した語を含む画像を検索。空白で区切ると、すべての語を含む画像。"
            "大文字・小文字、ひらがな・カタカナ、全角・半角は区別しない。Enter で検索").format(labels=labels))

    def conditions(self) -> CatalogSearch:
        """The search the form describes (詳細条件 only while it is open; the free word only when the catalog has keyword fields)."""
        catalog = self.catalog()
        keyword = self.keyword.text().strip() if catalog is not None and catalog.keyword_fields else ""
        acquired = _period(self.acquired_from, self.acquired_to)
        search = CatalogSearch(acquired_from=acquired[0], acquired_to=acquired[1], keyword=keyword)
        if not self.more.isChecked():
            return search
        registered, system = _period(self.registered_from, self.registered_to), _period(self.system_from, self.system_to)
        return replace(search, registered_from=registered[0], registered_to=registered[1],
                       system_registered_from=system[0], system_registered_to=system[1],
                       sensor=self.sensor_box.currentText().strip(), max_angle=_number(self.max_angle),
                       azimuth_from=_number(self.azimuth_from), azimuth_to=_number(self.azimuth_to), where=self.where.text().strip())

    def input_problem(self, days: bool = True) -> Optional[str]:
        """
        Why a typed condition can't be read (a day such as 2026-02-30, a time
        such as 25:00, an angle with letters), or None. The search stops and
        says so, rather than quietly asking without that condition (the whole
        period, every angle).
        """
        periods = [(self.tr("撮像日時"), self.acquired_from, self.acquired_to)] if days else []
        numbers = []
        if self.more.isChecked():
            if days:
                periods += [(self.tr("登録日時"), self.registered_from, self.registered_to),
                            (self.tr("システム登録日時"), self.system_from, self.system_to)]
            numbers += [(self.tr("角度"), self.max_angle)]
            numbers += [(self.tr("方位角"), e) for e in (self.azimuth_from, self.azimuth_to)]

        def unread(label: str, edit: QLineEdit, kind: str) -> str:
            return self.tr("{label}の「{text}」を読めません（{kind}）").format(label=label, text=edit.text().strip(), kind=kind)

        for label, start, end in periods:
            try:
                first = read_day_time(start.text())
            except ValueError:
                return unread(label, start, self.tr("日付は 2026-11-09、時刻もなら 2026-11-09 10:30 の形で"))
            try:
                read_day_time(end.text(), first[:10])
            except ValueError:
                alone = not first and bool(end.text().strip()) and _is_clock(end.text())
                kind = (self.tr("時刻だけなら左に日付を") if alone
                        else self.tr("日付は 2026-11-09、時刻もなら 2026-11-09 10:30 の形で"))
                return unread(label, end, kind)
        for label, edit in numbers:
            try:
                read_number(edit.text())
            except ValueError:
                return unread(label, edit, self.tr("数で"))
        return None

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
        problem = self.input_problem()
        if problem:
            self.status.setText(problem)
            return
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
        if self.area_box.currentData() == "layer" and box is None:
            layer = self.area_layer.currentLayer()
            if layer is None:
                self.status.setText(self.tr("範囲のレイヤーが未選択"))
            elif self.selected_only.isChecked():
                self.status.setText(self.tr("「{name}」に選択地物なし").format(name=layer.name()))
            else:
                self.status.setText(self.tr("「{name}」に形のある地物なし").format(name=layer.name()))
            return
        self._read(_Listed(catalog, search, box))

    def read_more(self) -> None:
        """「続きを読む」: the next `maxResults` images of the search listed added to the list."""
        listed = self._listed
        if listed is None or listed.next_offset is None:
            return
        self._read(listed, more=True)

    def _read(self, listed: _Listed, more: bool = False) -> None:
        """Reads the newest images of `listed` (a new search; in a task), or with `more` the next ones."""
        catalog = listed.catalog
        client = self.context.esri()
        token = self.context.token_for(catalog.url, catalog.token)
        offset = (listed.next_offset or 0) if more else 0
        similar = listed.rank is not None
        if not more:
            self._listed = listed
            self.more_button.hide()

        def work(task):
            # Read here (once) so that opening a record's folder finds the imagePatterns of a sensor's other names.
            self.context.satellite_names(client, task.isCanceled)
            return search_catalog(client, catalog, self._layer_of(client, catalog, token), listed.search, listed.box, token,
                                  offset, task.isCanceled)

        def done(result) -> None:
            self._reading(False)
            if listed is not self._listed or (similar and self.reference is None):
                return  # searched again since (another list shown, 「類似画像の検索」 left)
            listed.total, listed.next_offset = result.total, result.next_offset
            known = {_key(r) for r in listed.read}
            new = [r for r in result.records if _key(r) not in known]
            listed.read += new
            if similar:
                chosen = self.chosen() if more else []
                records, overlaps = listed.rank(listed.read)
                self.show_records(catalog, records, overlaps, listed)
                self.choose(chosen)
                self._show_count()
            elif more:
                self._add_records(new)
            else:
                self.show_records(catalog, new, listed=listed)
                self._show_count()

        def failed(message: str) -> None:
            self._reading(False)
            what = self.tr("続きの読み込み") if more else self.tr("類似画像の検索") if similar else self.tr("画像カタログの検索")
            self.status.setText(self.tr("{what}に失敗: {m}").format(what=what, m=message))

        self._reading(True)
        self.status.setText(self.tr("続きを読み込み中") if more else self.tr("類似画像の検索中") if similar else self.tr("検索中"))
        self._start(self.tr("画像カタログの続き") if more else self.tr("類似画像の検索") if similar else self.tr("画像カタログの検索"),
                    work, done, failed)

    def _reading(self, on: bool) -> None:
        """A search (or its 続き) started (`on`) or ended: while any is read, neither button asks again."""
        self._reads = max(0, self._reads + (1 if on else -1))
        idle = self._reads == 0
        self.search_button.setEnabled(idle and bool(self.catalogs()))
        self.more_button.setEnabled(idle)

    def _show_count(self) -> None:
        """How many images the list has of how many match, and 「続きを読む」 when there are more."""
        listed = self._listed
        n = len(self.model.records)
        if listed is not None and listed.rank is not None:
            text = self.tr("類似画像 {n} 件").format(n=n)
            if listed.next_offset is not None:
                text += (self.tr("（候補 {total} 件中 新しい {read} 件から）").format(total=listed.total, read=len(listed.read))
                         if listed.total is not None else self.tr("（候補の新しい {read} 件から）").format(read=len(listed.read)))
        elif listed is not None and listed.total is not None and listed.total > n:
            text = self.tr("{total} 件中 新しい {n} 件").format(total=listed.total, n=n)
        elif listed is not None and listed.next_offset is not None:
            text = self.tr("新しい {n} 件").format(n=n)
        else:
            text = self.tr("{n} 件").format(n=n)
        self.status.setText(text)
        self._show_more()

    def _show_more(self) -> None:
        """「続きを読む（次の n 件）」 while the search listed has more images."""
        listed = self._listed
        if listed is None or listed.next_offset is None:
            self.more_button.hide()
            return
        n = listed.catalog.max_results
        if listed.total is not None:
            n = max(1, min(n, listed.total - listed.next_offset))
        self.more_button.setText(self.tr("続きを読む（次の {n} 件）").format(n=n))
        self.more_button.show()

    def _add_records(self, records) -> None:
        """
        The images read on added to the list in its order (newest first, or by
        the heading clicked), the rows chosen and where the list is scrolled to kept.
        """
        bar = self.table.verticalScrollBar()
        scrolled = bar.value()
        self.model.add_records(records)
        self._show_count()
        header = self.table.horizontalHeader()
        section = header.sortIndicatorSection()
        if 0 <= section < self.model.columnCount():
            # The rows follow their images (the selection too); the map is drawn again and 「未着手のみ」 applied.
            self.model.sort(section, header.sortIndicatorOrder())
        else:
            self._redraw()
            self._apply_work_filter()
        bar.setValue(scrolled)
        self.load_work()

    def show_records(self, catalog: CatalogConfig, records, overlaps=None, listed: Optional[_Listed] = None) -> None:
        """Lists `records` of `catalog`; `listed`: the search they are of (「続きを読む」), None for other lists (an event's images)."""
        self._shown_catalog = catalog
        self._listed = listed
        self._show_more()
        self._show_file_buttons()
        self.model.set_records(catalog, records, overlaps, work=self.context.config.assignments is not None)
        # The list comes newest first (類似画像: the most alike first): the heading says so without sorting again.
        header = self.table.horizontalHeader()
        header.blockSignals(True)
        acquired = next((i for i, c in enumerate(self.model.columns) if c.key == "acquired"), -1)
        header.setSortIndicator(acquired if overlaps is None else -1, Qt.SortOrder.DescendingOrder)
        header.blockSignals(False)
        self.table.resizeColumnsToContents()
        self._redraw()
        self.load_work()

    @property
    def records(self) -> list[CatalogRecord]:
        return list(self.model.records)

    # --- 分担 -------------------------------------------------------------------------------------

    def load_work(self) -> None:
        """Reads who works the listed images (`assignments`; in a task): the 分担 column and 「未着手のみ」."""
        source = self.context.config.assignments
        if source is None or self.model.work is None or not self.model.records:
            self._work_timer.stop()
            return
        if self.isVisible() and not self._work_timer.isActive():
            self._work_timer.start()
        from ..core.obs.assignments import assignments_of_images, work_state

        self._work_asked += 1
        asked = self._work_asked
        ids = [r.id for r in self.model.records if r.id]
        client, token = self.context.esri(), self.context.token_for(source.url, source.token)

        def work(_task):
            rows = assignments_of_images(client, source, ids, token)
            return {i: work_state(rows.get(i, [])) for i in ids}

        def done(states) -> None:
            if asked != self._work_asked:
                return  # listed again since
            self.model.set_work(states)
            self._apply_work_filter()

        def failed(message: str) -> None:
            if asked == self._work_asked:
                self.status.setText(self.tr("分担の読み込みに失敗: {m}").format(m=message))

        self._start(self.tr("分担の読み込み"), work, done, failed)

    def _apply_work_filter(self) -> None:
        """「未着手のみ」: the rows of images someone took (or not read yet) hidden."""
        from ..core.obs.assignments import UNWORKED

        only = self.unworked_only.isChecked() and self.model.work is not None
        shown = 0
        for row, r in enumerate(self.model.records):
            state = self.model.work_state(r)
            hide = only and (state is None or state[0] != UNWORKED)
            self.table.setRowHidden(row, hide)
            shown += not hide
        base = self.status.text().split(self.tr("（表示: 未着手"))[0]
        self.status.setText(base + (self.tr("（表示: 未着手 {n} 件）").format(n=shown) if only and self.model.records else ""))

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
        measure = overlap_measure(reference)

        def rank(records):
            ranked = rank_similar(record, records, tolerance, measure)
            return [s.record for s in ranked], {id(s.record): s.overlap for s in ranked}

        self._read(_Listed(catalog, search, box, rank))

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

    def set_footprint_style(self, style: str) -> None:
        """「フットプリント」: 'fill', 'outline' or 'hidden', kept for next time (per user)."""
        settings.set_footprint_style(style)
        if self.footprint_box.currentData() != style:
            self.footprint_box.setCurrentIndex(max(0, self.footprint_box.findData(style)))
        if self.overlay is not None:
            self.overlay.set_style(style)

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
        for key in ("open", "record", "zoom", "copy", "similar"):
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
        kind = self.area_box.itemData(index)
        if kind == "drawn" and self.drawn is None:
            self.draw_button.setChecked(True)
        if kind == "layer" and self.area_layer.currentLayer() is None:
            active = self.iface.activeLayer()
            if active is not None and layer_area(active) is not None:
                self.area_layer.setLayer(active)
        self._show_layer_row()

    def _show_layer_row(self) -> None:
        self.layer_row.setVisible(self.area_box.currentData() == "layer")

    def use_layer(self, layer: Optional[QgsMapLayer], selected_only: bool = False) -> None:
        """Searches over a vector layer's geometry (its chosen features only, when asked)."""
        self.area_layer.setLayer(layer)
        self.selected_only.setChecked(selected_only)
        self.area_box.setCurrentIndex(self.area_box.findData("layer"))

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
        """
        Where to search: the map's extent (a box), the area drawn (an Esri
        polygon), a layer's geometry (an Esri polygon, polyline or multipoint)
        or None for anywhere.
        """
        kind = self.area_box.currentData()
        if kind == "extent":
            return self.extent()
        if kind == "drawn" and self.drawn:
            return areas_of([], [[self.drawn]])[0]
        if kind == "layer":
            return layer_area(self.area_layer.currentLayer(), self.selected_only.isChecked())
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
        if not visible:
            self._work_timer.stop()
        elif self.model.work is not None and self.model.records:
            self.load_work()
        if visible and self.overlay is None:
            self.overlay = FootprintOverlay(self.canvas, self.footprint_box.currentData())
            self.overlay.show_area(self.drawn)
            self._redraw()
        elif not visible and self.overlay is not None:
            self.buttons["pick"].setChecked(False)
            self.draw_button.setChecked(False)
            self.overlay.remove()
            self.overlay = None

    # --- Opening ----------------------------------------------------------------------------------

    def choose_images(self, record, files: list[str], more: bool) -> Optional[list[str]]:
        """Which of a record's images (its folder's files matching `imagePatterns`) to open."""
        return choose_image_files(self, record, files, more)

    def choose_folder(self, mapping: PathMapping) -> Optional[str]:
        return QFileDialog.getExistingDirectory(self, self.tr("「{label}」（{prefix}）のフォルダの選択").format(label=mapping.label, prefix=mapping.prefix)) or None

    def _activated(self, record: CatalogRecord) -> None:
        """A double click: the image opened, or recorded without its file in a catalog without image files."""
        if self.has_files():
            self.open_records([record])
        else:
            self.record_without_files([record])

    #: The 「観測記録」 dock (plugin.py sets it).
    observations: Optional[Callable[[], object]] = None

    def record_without_files(self, records=None):
        """
        「ファイルなしで記録」: the chosen images (`records`) listed in 観測記録
        without their files, with the catalog's id, time and footprint; the
        first one is recorded on (the map moved to it). Its entry, None when
        none could be.
        """
        records = self.chosen() if records is None else list(records)
        images = [i for i in (listed_image(r) for r in records) if i is not None]
        if not images:
            self.status.setText(self.tr("画像 ID のある画像を一覧で選択（Ctrl・Shift + クリックで複数）"))
            return None
        dock = self.observations() if self.observations else None
        entry = dock.list_catalog_images(images) if dock is not None else None
        if entry is None:
            self.status.setText(self.tr("観測記録は使用不可（記録の保存先なし）"))
            return None
        if len(images) == 1:
            self.status.setText(self.tr("{id} を観測記録へ（ファイルなし）").format(id=entry.name))
        else:
            self.status.setText(self.tr("{n} 件を観測記録へ（ファイルなし）。◀ ▶ で次の画像").format(n=len(images)))
        return entry

    def open_records(self, records) -> list:
        """
        Opens images of the list as raster layers (a record whose path is a
        folder: the images chosen among its files, in a group); the reason is
        shown for those that cannot be opened, with 「ファイルなしで記録」 for
        them. With 「前の画像を閉じる」, the images opened here before close
        once these are open (not when none could be). In a catalog without
        image files, they are recorded without their files instead.
        """
        catalog = self._shown_catalog
        if catalog is None:
            return []
        if not catalog.image_files:
            self.record_without_files(records)
            return []
        layers = []
        problems = []
        unopened = []
        for record in records:
            try:
                opened = open_record(record, catalog, self.context.config.path_mappings, self.choose_folder,
                                     lambda r, files, more: self.choose_images(r, files, more), problems,
                                     names=self.context.known_names())
            except Cancelled:
                break
            except PathError as error:
                problems.append(str(error))
                unopened.append(record)
                continue
            add_record_layers(opened, record)
            overview_queue.request(opened, self.context.config.overviews, self.iface.messageBar())
            layers += opened
        if layers:
            # Only those opened with it on close (an image opened before it was turned on stays).
            opened_ids = [layer.id() for layer in layers]
            if self.close_previous.isChecked():
                close_layers([i for i in self.last_opened if i not in opened_ids])
                self.last_opened = opened_ids
            else:
                self.last_opened = []
        if problems:
            self._say_unopened(problems, [r for r in unopened if r.id])
            self.status.setText(problems[-1])
        return layers

    def _say_unopened(self, problems: list[str], unopened: list[CatalogRecord]) -> None:
        """Why images could not be opened, in the message bar; with 「ファイルなしで記録」 for those (their raw data elsewhere, say)."""
        bar = self.iface.messageBar()
        dock = self.observations() if self.observations else None
        # The ids and paths are the catalog's (others' text): shown as written, never as HTML.
        text = plain_tip(" / ".join(problems))
        if not unopened or dock is None:
            bar.pushWarning(TITLE, text)
            return
        widget = bar.createMessage(TITLE, text)
        button = QPushButton(self.tr("ファイルなしで記録"))
        button.setToolTip(self.tr("開けなかった画像 {n} 件を、ファイルなしで観測記録の画像一覧へ（ID・撮像日時・範囲はカタログから）").format(n=len(unopened)))

        def record() -> None:
            self.record_without_files(unopened)
            try:
                bar.popWidget(widget)
            except RuntimeError:  # closed meanwhile
                pass

        button.clicked.connect(record)
        widget.layout().addWidget(button)
        # One at a time (opening again does not stack them).
        before = getattr(self, "unopened_message", None)
        if before is not None and any(w is before for w in bar.items()):
            bar.popWidget(before)
        bar.pushWidget(widget, Qgis.MessageLevel.Warning, 0)
        #: The last such message (for scripts and tests).
        self.unopened_message = widget

    #: The 「イベント」 dock (plugin.py sets it).
    event_linker: Optional[Callable[[], object]] = None

    def event_items(self, records=None) -> list:
        """The chosen images as items of an event."""
        from ..core.events import LinkItem
        from ..core.util import ms_of

        catalog = self._shown_catalog
        if catalog is None:
            return []
        return [LinkItem("image", r.id, label=" ".join(x for x in (r.id, r.sensor) if x), time=ms_of(r.acquired), source=catalog.url)
                for r in (records if records is not None else self.chosen()) if r.id]

    def add_to_event(self):
        """「イベントに追加」: the chosen images into an open event (a menu of them)."""
        if not self.event_items():
            self.status.setText(self.tr("一覧で画像を選択（Ctrl・Shift + クリックで複数）"))
            return None
        events = self.event_linker() if self.event_linker else None
        return events.offer(self.buttons["event"], self.event_items) if events else None

    def copy_paths(self) -> None:
        """Copies where the chosen images are (their URL or path), one per line."""
        paths = [r.source.value for r in self.chosen() if r.source]
        if not paths:
            self.status.setText(self.tr("画像の場所なし"))
            return
        QApplication.clipboard().setText("\n".join(paths))
        self.status.setText(self.tr("パス {n} 件をコピー").format(n=len(paths)))

    def clear_folders(self) -> None:
        if not ask_no(self, self.tr("フォルダの対応を消去"),
                      self.tr("この PC で画像のパスの読み替えに選んだフォルダをすべて忘れますか？次に画像を開くとき、フォルダをまた選ぶことになります。")):
            return
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
        # A watch keeps the sensor, angle, azimuth, SQL and free word (not the days).
        problem = self.input_problem(days=False)
        if problem:
            self.status.setText(problem)
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
        # Tasks left running (a request waiting on the network) stop with the dock.
        for task in list(self._tasks):
            task.cancel()
        self.buttons["pick"].setChecked(False)
        self.draw_button.setChecked(False)
        self.draw_tool.remove()
        if self.overlay is not None:
            self.overlay.remove()
            self.overlay = None
