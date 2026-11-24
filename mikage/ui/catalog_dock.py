"""
画像カタログ: searching the image catalogs of the settings file (over the
map's extent, an area drawn, an area saved before, a vector layer's geometry as it is, or anywhere, by dates, a free word, sensor, angle,
azimuth and SQL), the list
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

「保存した範囲」 (tsudo, 2026-11-19): an area drawn is kept under a name
(「保存…」 beside 「範囲を描く」) and chosen again later from 「範囲 ▸ 保存した
範囲」, newest first (core/catalog/saved_areas.py; per user, as the views).
"""
from __future__ import annotations

from typing import Callable, Optional

from qgis.gui import QgsMapLayerComboBox
from qgis.PyQt.QtCore import QStringListModel, Qt, QTimer
from qgis.PyQt.QtWidgets import (
    QAbstractItemView, QCheckBox, QComboBox, QCompleter, QDoubleSpinBox, QFormLayout, QGroupBox, QHBoxLayout, QLabel,
    QLineEdit, QMenu, QPushButton, QSpinBox, QTableView, QToolButton, QVBoxLayout, QWidget,
)

from ..core import roles
from ..core.catalog.api import CatalogConfig, CatalogLayerInfo, CatalogRecord
from ..qgis_io import settings
from ..qgis_io.file_manager import show_folder
from .catalog_model import CatalogModel
from .catalog_overlay import DrawTool, FootprintOverlay, FootprintTool
from .docks import PluginDock, plain_label
from .flow_layout import FlowLayout
from .list_views import ListViews, MenuToolButton
from .catalog_parts.conditions import ConditionsPart
from .catalog_parts.search import SearchPart
from .catalog_parts.similar import SimilarPart
from .catalog_parts.footprints import FootprintsPart
from .catalog_parts.areas import AreasPart
from .catalog_parts.saved_areas import SavedAreasPart
from .catalog_parts.opening import OpeningPart
from .catalog_parts.outputs import OutputsPart
from .catalog_parts.common import (  # noqa: F401 (read from here too)
    TITLE, _number, _Listed, _key, _when, local_now, _is_clock, _period, PERIOD_TIP,
)


#: The layers a search can be over: vector layers with geometries.
try:
    from qgis.core import QgsMapLayerProxyModel

    _GEOMETRY_LAYERS = QgsMapLayerProxyModel.Filter.HasGeometry
except AttributeError:  # older bindings: the enum unscoped
    from qgis.core import QgsMapLayerProxyModel

    _GEOMETRY_LAYERS = QgsMapLayerProxyModel.HasGeometry


class CatalogDock(
        ConditionsPart, SearchPart, SimilarPart, FootprintsPart, AreasPart, SavedAreasPart, OpeningPart, OutputsPart,
        PluginDock):
    def __init__(self, iface, context, open_settings: Callable[[], None], parent=None):
        super().__init__("SatObsImageCatalog", TITLE, context, open_settings, parent)
        self.iface = iface
        self.canvas = iface.mapCanvas()
        self.layers: dict[str, CatalogLayerInfo] = {}
        #: The image compared with in 「類似画像の検索」 (with its catalog); None for the usual search.
        self.reference: Optional[tuple[CatalogRecord, CatalogConfig]] = None
        #: Opens the watch dialog of the 通知 dock (set by the plugin): (catalog, types, conditions, area drawn), with the
        #: keywords `area` (the kind of area searched), `layer` and `selected_only` (the layer searched over, for 「レイヤー」).
        self.watch_requested: Optional[Callable[..., None]] = None
        self._shown_catalog: Optional[CatalogConfig] = None
        #: The search listed (「続きを読む」); None for a list that is not a search (an event's images).
        self._listed: Optional[_Listed] = None
        #: Searches (and 続き) being read.
        self._reads = 0
        self._tasks: list = []

        # 「画像 ID で開く」 and 「最近開いた画像」 (開いた画像の履歴, core/catalog/opened.py; tsudo 2026-11-18).
        quick = QHBoxLayout()
        self.open_id = QLineEdit()
        self.open_id.setClearButtonEnabled(True)
        self.open_id.setPlaceholderText(self.tr("画像 ID で開く"))
        self.open_id.setToolTip(self.tr(
            "画像 ID を入力し Enter で画像を表示（前に開いた画像は前回のファイル、それ以外は画像カタログで検索）"))
        self.open_id.returnPressed.connect(lambda: self.open_image_id(self.open_id.text()))
        self._opened_model = QStringListModel()
        completer = QCompleter(self._opened_model, self.open_id)
        completer.setCaseSensitivity(Qt.CaseSensitivity.CaseInsensitive)
        completer.setFilterMode(Qt.MatchFlag.MatchContains)
        self.open_id.setCompleter(completer)
        self.open_id.textEdited.connect(lambda text: self._refresh_opened() if len(text) <= 1 else None)
        self.recent_button = QToolButton()
        self.recent_button.setText(self.tr("最近開いた画像"))
        self.recent_button.setToolTip(self.tr("この PC で最近開いた画像を、前回と同じファイルで表示（確認あり）"))
        self.recent_button.setPopupMode(QToolButton.ToolButtonPopupMode.InstantPopup)
        self.recent_menu = QMenu(self.recent_button)
        self.recent_menu.aboutToShow.connect(self._fill_recent)
        self.recent_button.setMenu(self.recent_menu)
        quick.addWidget(self.open_id, 1)
        quick.addWidget(self.recent_button)
        self.body.addLayout(quick)
        self._refresh_opened()

        form = QFormLayout()
        self.catalog_box = QComboBox()
        self.catalog_box.currentIndexChanged.connect(self._catalog_changed)
        form.addRow(self.tr("カタログ"), self.catalog_box)
        self.area_box = QComboBox()
        self.area_box.addItem(self.tr("表示範囲"), "extent")
        self.area_box.addItem(self.tr("描いた範囲"), "drawn")
        self.area_box.addItem(self.tr("保存した範囲"), "saved")
        self.area_box.addItem(self.tr("レイヤー"), "layer")
        self.area_box.addItem(self.tr("指定なし"), "none")
        self.area_box.setCurrentIndex(0)
        self.area_box.activated.connect(self._area_chosen)
        self.area_box.currentIndexChanged.connect(lambda *_: self._show_layer_row())
        self.draw_button = QPushButton(self.tr("範囲を描く"))
        self.draw_button.setCheckable(True)
        self.draw_button.setToolTip(self.tr("クリックで頂点を追加（描いている形は地図に表示）、ダブルクリックか右クリックで確定、Esc で取消"))
        self.draw_button.toggled.connect(self._draw_toggled)
        # 「保存…」: the area drawn kept under a name, chosen again later from 「保存した範囲」.
        self.save_area_button = QPushButton(self.tr("保存…"))
        self.save_area_button.setToolTip(self.tr("描いた範囲に名前を付けて保存。あとで「保存した範囲」から選べる（自分の PC に保存）"))
        self.save_area_button.clicked.connect(self.ask_save_area)
        area_row = QHBoxLayout()
        area_row.addWidget(self.area_box, 1)
        area_row.addWidget(self.draw_button)
        area_row.addWidget(self.save_area_button)
        # 「保存した範囲」: the areas saved before (newest first) and what can be done with the one chosen.
        self.saved_box = QComboBox()
        self.saved_box.setToolTip(self.tr("前に保存した範囲（新しい順）。選ぶと地図に表示（見えない場所ならそこへ移動）"))
        self.saved_box.activated.connect(lambda *_: self._saved_chosen(move=True))
        self.saved_box.currentIndexChanged.connect(lambda *_: self._show_area())
        self.saved_menu_button = MenuToolButton()
        self.saved_menu_button.setText(self.tr("編集"))
        self.saved_menu_button.setToolTip(self.tr("選んだ保存範囲の移動・名前の変更・削除"))
        self.saved_menu_button.setPopupMode(QToolButton.ToolButtonPopupMode.InstantPopup)
        self.saved_menu = QMenu(self.saved_menu_button)
        self.saved_menu.addAction(self.tr("範囲へ移動"), self.zoom_to_saved_area)
        self.saved_menu.addAction(self.tr("名前の変更…"), self.ask_rename_saved_area)
        self.saved_menu.addAction(self.tr("削除…"), self.ask_delete_saved_area)
        self.saved_menu_button.setMenu(self.saved_menu)
        self.saved_row = QWidget()
        saved_row = QHBoxLayout(self.saved_row)
        saved_row.setContentsMargins(0, 0, 0, 0)
        saved_row.addWidget(self.saved_box, 1)
        saved_row.addWidget(self.saved_menu_button)
        self.saved_row.hide()
        self._fill_saved_areas()
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
        area_column.addWidget(self.saved_row)
        area_column.addWidget(self.layer_row)
        form.addRow(self.tr("範囲"), area_column)
        #: The area drawn on the map (its corners, longitude / latitude; the ring closed); None before one is drawn.
        self.drawn: Optional[list] = None
        self._show_layer_row()
        self.acquired_from, self.acquired_to = QLineEdit(), QLineEdit()
        form.addRow(self.tr("撮像日時"), self._period_row(self.acquired_from, self.acquired_to))
        # 「フリーワード」: words looked for in the catalog's image id and `fields.keywords` (shown only for a catalog that has either).
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
        self.more_button.setToolTip(self.tr("この検索の続き（さらに古い画像）を一覧に追加。範囲と条件は検索したときのまま"))
        self.more_button.clicked.connect(self.read_more)
        self.more_button.hide()
        self.body.addWidget(self.more_button)
        # 「ビュー」: the columns, the sort and the search conditions kept under a name (per user).
        self.views = ListViews("catalog", self.table, {
            "catalog": self.catalog_box, "area": self.area_box, "savedArea": self.saved_box, "acquiredFrom": self.acquired_from, "acquiredTo": self.acquired_to,
            "more": self.more, "registeredFrom": self.registered_from, "registeredTo": self.registered_to, "systemFrom": self.system_from,
            "systemTo": self.system_to, "sensor": self.sensor_box, "maxAngle": self.max_angle, "azimuthFrom": self.azimuth_from,
            "azimuthTo": self.azimuth_to, "where": self.where, "unworkedOnly": self.unworked_only, "keyword": self.keyword,
        }, self._view_applied, self)
        row.addWidget(self.views.button)
        #: The 分担 of the listed images read again every minute while the dock is shown.
        self._work_asked = 0
        self._work_timer = QTimer(self)
        # Every 3 minutes (2026-11-19: every minute, with 200 people, was 2 requests a second for this column alone).
        self._work_timer.setInterval(180_000)
        self._work_timer.timeout.connect(self.load_work)

        # 「前の画像を閉じる」 (tsudo, 2026-11-10): looking through images one after another, the one opened before closes.
        self.close_previous = QCheckBox(self.tr("前の画像を閉じる"))
        self.close_previous.setToolTip(self.tr("画像を開くとき、前にここから開いた画像を地図から外す（PC ごとに記憶）"))
        self.close_previous.setChecked(settings.close_previous())
        self.close_previous.toggled.connect(settings.set_close_previous)
        #: The layers opened last with 「前の画像を閉じる」 on (their ids), which close when the next images open.
        self.last_opened: list[str] = []
        # 「開いた画像へ移動」 (tsudo, 2026-11-10): the map goes to the images opened (all of a record's files, or of several records).
        self.zoom_opened = QCheckBox(self.tr("開いた画像へ移動"))
        self.zoom_opened.setToolTip(self.tr("画像を開くとき、その画像の範囲へ地図を移動（PC ごとに記憶）"))
        self.zoom_opened.setChecked(settings.zoom_to_opened())
        self.zoom_opened.toggled.connect(settings.set_zoom_to_opened)

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
            "folder": self.tr("選んだ画像のフォルダをエクスプローラーで開く（パスの読み替えがあれば、読み替えた先にあるときはそちら。"
                              "画像ファイルはそのファイルを選んだ状態で）"),
            "event": self.tr("選んだ画像をイベントに追加"),
            "record": self.tr("選んだ画像を、画像ファイルを開かずに観測記録の画像一覧へ（ID・撮像日時・範囲はカタログから。"
                              "生データが別システムにしかない画像に。ダブルクリックでも）。背景地図の上をクリックして記録"),
        }
        for key, text, slot in [
            ("open", self.tr("開く"), lambda: self.open_records(self.chosen())),
            ("record", self.tr("ファイルなしで記録"), lambda: self.record_without_files()),
            ("zoom", self.tr("範囲へ移動"), self.zoom_to_chosen),
            ("copy", self.tr("パスをコピー"), self.copy_paths),
            ("folder", self.tr("フォルダを開く"), lambda: self.open_folders()),
            ("layer", self.tr("レイヤーとして追加"), self.add_layer),
            ("similar", self.tr("類似画像の検索"), self._similar_of_chosen),
            ("watch", self.tr("この条件で監視…"), self._watch),
            ("pick", self.tr("地図で選択"), None),
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
                buttons.addWidget(self.zoom_opened)
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
        self.visibilityChanged.connect(self._visibility)
        context.configChanged.connect(self._config_changed)
        self._load_tolerance()
        self._config_changed()

    def _view_applied(self) -> None:
        """A view chosen: the list searched again with its conditions (when it was searched before)."""
        if self.model.records:
            self.search()

    # --- Form -------------------------------------------------------------------------------------

    def catalogs(self) -> list[CatalogConfig]:
        """
        The settings file's catalogs shown now; for 判読練習の指導者 also those shown while practising only
        (imageCatalogs[].mode "training"), after them (the exercises' images: their exercises are made in production).
        """
        config = self.context.config
        practice = config.practice_catalogs if self.allows(roles.PRACTICE_CATALOGS) else []
        return [*config.image_catalogs, *practice]

    def catalog_label(self, catalog: CatalogConfig) -> str:
        """A catalog's name in the list: 「（練習用）」 after a practice-only one's (shown in production to 判読練習の指導者)."""
        if any(catalog is c for c in self.context.config.practice_catalogs):
            return self.tr("{label}（練習用）").format(label=catalog.label)
        return catalog.label

    def catalog(self) -> Optional[CatalogConfig]:
        i = self.catalog_box.currentIndex()
        catalogs = self.catalogs()
        return catalogs[i] if 0 <= i < len(catalogs) else None

    def _config_changed(self) -> None:
        current = self.catalog_box.currentText()
        self.catalog_box.blockSignals(True)
        self.catalog_box.clear()
        for c in self.catalogs():
            self.catalog_box.addItem(self.catalog_label(c))
        i = self.catalog_box.findText(current)
        self.catalog_box.setCurrentIndex(i if i >= 0 else 0)
        self.catalog_box.blockSignals(False)
        self.layers = {}
        self.buttons["event"].setVisible(self.context.config.events is not None)
        self.unworked_only.setVisible(self.context.config.assignments is not None)
        if self.context.config.assignments is None:
            self.unworked_only.setChecked(False)
        self.search_button.setEnabled(bool(self.catalogs()))
        none = self.tr("設定ファイルに画像カタログなし")
        if not self.catalogs():
            self.status.setText(none)
        elif self.status.text() == none:
            # Said while the settings file was not read yet (the dock is made before it is): no longer so.
            self.status.setText("")
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

    def roles_changed(self) -> None:
        """
        「ファイルなしで記録」: for those who record observations; the catalogs shown while practising only:
        for 判読練習の指導者 (core/roles.py).
        """
        self._show_file_buttons()
        if [self.catalog_box.itemText(i) for i in range(self.catalog_box.count())] != [self.catalog_label(c) for c in self.catalogs()]:
            self._config_changed()


    # --- Tasks ------------------------------------------------------------------------------------


    # --- Searching --------------------------------------------------------------------------------


    # --- 分担 -------------------------------------------------------------------------------------


    # --- 類似画像の検索 ---------------------------------------------------------------------------


    # --- The list and the map ---------------------------------------------------------------------


    # --- 「保存した範囲」 ---------------------------------------------------------------------------


    # --- Opening ----------------------------------------------------------------------------------


    #: The 「観測記録」 dock (plugin.py sets it).
    observations: Optional[Callable[[], object]] = None


    # --- 開いた画像の履歴・画像 ID で開く (tsudo 2026-11-18) ---------------------------------------


    #: The 「イベント」 dock (plugin.py sets it).
    event_linker: Optional[Callable[[], object]] = None


    #: Shows a folder in Explorer (qgis_io/file_manager.py; tests replace it).
    show_folder = staticmethod(show_folder)
    #: 「フォルダを開く」 opens at most this many windows (several images chosen).
    MAX_FOLDERS = 5

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
