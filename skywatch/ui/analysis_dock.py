"""
「データ分析」 (tsudo, 2026-11-08): for those who analyse — the Esri tables of
the settings file (画像カタログ, 外部 AI の結果, 観測記録, イベント, 通報記録 …)
and the project's own layers and tables, joined and summed up
(core/analysis.py; SQLite of the standard library, reading only).

A dock rather than more of the attribute table: QGIS's attribute table is one
layer, and its joins one-to-one, so 「AI の結果を画像ごとに数える」 (one image,
many results) is not something it does; many of the Esri tables are not
layers of the project (AI results are read per image); and the plugin knows
each table's URL, sign-in and the roles of its attributes, so it offers the
keys that join them. The result goes back to QGIS as a layer (with shapes
when it has the column SHAPE), for the attribute table, the map and
「ダッシュボード」.

- 表: every table a query can name, its columns (type, role, alias) and the
  condition it is read with (Esri: `where`; a project layer: a QGIS
  expression). Double-click puts a name in the SQL.
- 「かんたん集計」: a table, its joins (keys suggested), a condition, the
  columns grouped by and the figures → the SQL. 「SQL」: the query itself.
- `:name` in the SQL: a value asked for below it.
- 分析: kept in the project (保存 / 名前を付けて保存 / 削除); 「例」 are made
  from the settings file (モデルを指定した AI 結果の画像・クラスごとの件数 …).
- The result: a table, CSV, the clipboard, a layer of the project.
"""
from __future__ import annotations

from typing import Callable, Optional

from qgis.core import QgsProject, QgsVectorLayer
from qgis.PyQt import sip
from qgis.PyQt.QtCore import QAbstractTableModel, QModelIndex, Qt
from qgis.PyQt.QtGui import QFontDatabase, QKeySequence
from qgis.PyQt.QtWidgets import (
    QAbstractItemView, QApplication, QCheckBox, QComboBox, QFileDialog, QFormLayout, QHBoxLayout, QHeaderView, QInputDialog, QLabel,
    QLineEdit, QListWidget, QListWidgetItem, QPlainTextEdit, QPushButton, QSpinBox, QSplitter, QTableView, QTabWidget, QToolButton,
    QTreeWidget, QTreeWidgetItem, QVBoxLayout, QWidget,
)

try:
    from qgis.PyQt.QtGui import QShortcut
except ImportError:  # Qt 5
    from qgis.PyQt.QtWidgets import QShortcut

from ..core.analysis import (
    AGGREGATES, DEFAULT_TABLE_LIMIT, JOIN_TYPES, PROJECT_KEY, PROJECT_SCOPE, AnalysisError, Analysis, Builder, Column, Figure, Join,
    Result, Table, aliases, analyses_of, analyses_text, build_sql, cell_text, csv_text, esri_tables, esri_type, examples, parameters,
    quote_name, suggest_join, tsv_text,
)
from ..core.esri import EsriError
from ..qgis_io import analysis_io
from ..qgis_io.tasks import run_task
from .docks import PluginDock, ask_no, plain_tip
from .flow_layout import FlowLayout

TITLE = "データ分析"
#: The rows shown in the dock (all of them go to CSV, the clipboard and the layer).
SHOWN_ROWS = 5000
EXAMPLE = "\u0000example:"


class ResultModel(QAbstractTableModel):
    """The result's rows, for the table view."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.result: Optional[Result] = None

    def set_result(self, result: Optional[Result]) -> None:
        self.beginResetModel()
        self.result = result
        self.endResetModel()

    def rowCount(self, parent=QModelIndex()) -> int:  # noqa: N802 (Qt's name)
        return 0 if self.result is None or parent.isValid() else min(len(self.result.rows), SHOWN_ROWS)

    def columnCount(self, parent=QModelIndex()) -> int:  # noqa: N802
        return 0 if self.result is None or parent.isValid() else len(self.result.columns)

    def data(self, index, role=Qt.ItemDataRole.DisplayRole):
        if self.result is None or not index.isValid():
            return None
        value = self.result.rows[index.row()][index.column()]
        if role == Qt.ItemDataRole.DisplayRole:
            text = cell_text(value)
            return text if len(text) <= 200 else text[:200] + "…"
        if role == Qt.ItemDataRole.ToolTipRole:
            text = cell_text(value)
            return plain_tip(text[:2000]) if len(text) > 30 else None
        if role == Qt.ItemDataRole.TextAlignmentRole and isinstance(value, (int, float)) and not isinstance(value, bool):
            return int(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        return None

    def headerData(self, section, orientation, role=Qt.ItemDataRole.DisplayRole):  # noqa: N802
        if role != Qt.ItemDataRole.DisplayRole or self.result is None:
            return None
        if orientation == Qt.Orientation.Horizontal:
            return self.result.columns[section] if section < len(self.result.columns) else None
        return section + 1


class AnalysisDock(PluginDock):
    """「データ分析」."""

    def __init__(self, iface, context, open_settings: Callable[[], None], parent=None):
        super().__init__("SatObsAnalysis", TITLE, context, open_settings, parent)
        self.iface = iface
        self.project = QgsProject.instance()
        self.tables: list[Table] = []
        self.analyses: list[Analysis] = []
        self.result: Optional[Result] = None
        self.task = None
        self._columns_task = None
        self._columns_read: set = set()
        #: The analysis shown (its name), "" for a new one.
        self.current = ""
        self._example = ""
        self.conditions: dict[str, str] = {}
        self.values: dict[str, str] = {}
        self._building = False

        # ---- 分析 ----
        top = QHBoxLayout()
        self.chooser = QComboBox()
        self.chooser.setToolTip(self.tr("プロジェクトに保存した分析と、設定ファイルから作った例"))
        self.chooser.activated.connect(lambda _i: self._chosen())
        top.addWidget(QLabel(self.tr("分析")))
        top.addWidget(self.chooser, 1)
        self.body.addLayout(top)
        flow = FlowLayout()
        self.buttons: dict[str, QPushButton] = {}
        for key, text, tip, slot in [
            ("new", self.tr("新規"), self.tr("空の分析から始める"), self.new),
            ("save", self.tr("保存"), self.tr("この分析をプロジェクトに保存（名前がなければ尋ねる）"), lambda: self.save()),
            ("save_as", self.tr("名前を付けて保存…"), self.tr("別の名前でプロジェクトに保存"), lambda: self.save(ask=True)),
            ("delete", self.tr("削除"), self.tr("この分析をプロジェクトから削除"), lambda: self.delete()),
        ]:
            b = QPushButton(text)
            b.setToolTip(tip)
            b.clicked.connect(lambda _on=False, slot=slot: slot())
            flow.addWidget(b)
            self.buttons[key] = b
        holder = QWidget()
        holder.setLayout(flow)
        self.body.addWidget(holder)

        split = QSplitter(Qt.Orientation.Vertical)
        split.setChildrenCollapsible(False)

        # ---- 表 ----
        tables = QWidget()
        tl = QVBoxLayout(tables)
        tl.setContentsMargins(0, 0, 0, 0)
        self.tree = QTreeWidget()
        self.tree.setHeaderLabels([self.tr("表・列"), self.tr("内容"), self.tr("読み込み条件")])
        self.tree.setToolTip(self.tr("ダブルクリックで名前を SQL に入れる"))
        self.tree.itemDoubleClicked.connect(self._insert_name)
        self.tree.itemExpanded.connect(lambda _item: self.read_columns())
        self.tree.header().setSectionResizeMode(QHeaderView.ResizeMode.Interactive)
        self.tree.setMinimumHeight(180)
        tl.addWidget(self.tree, 1)
        row = QHBoxLayout()
        self.condition_button = QPushButton(self.tr("読み込み条件…"))
        self.condition_button.setToolTip(self.tr("選んだ表を読むときの条件（Esri: WHERE 句、プロジェクトのレイヤー: QGIS の式）。"
                                                 "大きな表はここで絞ると速い"))
        self.condition_button.clicked.connect(lambda: self.edit_condition())
        self.refresh_button = QPushButton(self.tr("一覧を更新"))
        self.refresh_button.setToolTip(self.tr("プロジェクトのレイヤーと Esri の表の列を読み直す"))
        self.refresh_button.clicked.connect(lambda: self.refresh(read=True))
        self.limit = QSpinBox()
        self.limit.setRange(1000, 5_000_000)
        self.limit.setSingleStep(10_000)
        self.limit.setValue(DEFAULT_TABLE_LIMIT)
        self.limit.setGroupSeparatorShown(True)
        self.limit.setToolTip(self.tr("1 つの表から読む行の上限（超えたら中止: 読み込み条件で絞る）"))
        row.addWidget(self.condition_button)
        row.addWidget(self.refresh_button)
        row.addStretch(1)
        row.addWidget(QLabel(self.tr("表ごとの上限")))
        row.addWidget(self.limit)
        tl.addLayout(row)
        split.addWidget(tables)

        # ---- かんたん集計 / SQL ----
        self.tabs = QTabWidget()
        self.tabs.addTab(self._builder_page(), self.tr("かんたん集計"))
        sql_page = QWidget()
        sl = QVBoxLayout(sql_page)
        self.sql = QPlainTextEdit()
        self.sql.setFont(QFontDatabase.systemFont(QFontDatabase.SystemFont.FixedFont))
        self.sql.setPlaceholderText("SELECT t.IMAGE_ID, COUNT(*) AS 件数\nFROM TBL_AI_RESULT AS t\nWHERE t.MODEL = :model\nGROUP BY t.IMAGE_ID")
        self.sql.textChanged.connect(self._sql_changed)
        sl.addWidget(self.sql, 1)
        hint = QLabel(self.tr("SQLite の SELECT 文（WITH・JOIN・GROUP BY・ウィンドウ関数も可）。:名前 は実行時に入力する値。"
                              "日時は「YYYY-MM-DD HH:MM:SS」（この PC の時刻）の文字。SHAPE で形状（WKT）。"))
        hint.setWordWrap(True)
        sl.addWidget(hint)
        self.tabs.addTab(sql_page, self.tr("SQL"))
        split.addWidget(self.tabs)

        # ---- 値・実行・結果 ----
        results = QWidget()
        rl = QVBoxLayout(results)
        rl.setContentsMargins(0, 0, 0, 0)
        self.params_box = QWidget()
        self.params_form = QFormLayout(self.params_box)
        self.params_form.setContentsMargins(0, 0, 0, 0)
        self.param_edits: dict[str, QLineEdit] = {}
        rl.addWidget(self.params_box)
        run_row = QHBoxLayout()
        self.run_button = QPushButton(self.tr("実行"))
        self.run_button.setToolTip(self.tr("SQL に出てくる表を読み、集計する（Ctrl+Enter）"))
        self.run_button.clicked.connect(lambda: self.run())
        self.cancel_button = QPushButton(self.tr("取消"))
        self.cancel_button.clicked.connect(self.cancel)
        self.cancel_button.setEnabled(False)
        run_row.addWidget(self.run_button)
        run_row.addWidget(self.cancel_button)
        run_row.addStretch(1)
        for key, text, tip, slot in [
            ("csv", self.tr("CSV 保存…"), self.tr("結果をすべて CSV（UTF-8 BOM 付き）で保存"), lambda: self.save_csv()),
            ("copy", self.tr("コピー"), self.tr("結果をすべてタブ区切りでクリップボードへ（Excel に貼り付け可）"), self.copy),
            ("layer", self.tr("レイヤーとして追加"), self.tr("結果をプロジェクトのレイヤーに（SHAPE 列があれば地図に、なければ表）"), lambda: self.add_layer()),
        ]:
            b = QPushButton(text)
            b.setToolTip(tip)
            b.clicked.connect(lambda _on=False, slot=slot: slot())
            run_row.addWidget(b)
            self.buttons[key] = b
        rl.addLayout(run_row)
        self.status = QLabel()
        self.status.setTextFormat(Qt.TextFormat.PlainText)
        self.status.setWordWrap(True)
        self.status.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        rl.addWidget(self.status)
        self.model = ResultModel(self)
        self.view = QTableView()
        self.view.setModel(self.model)
        self.view.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectItems)
        self.view.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.view.setSortingEnabled(False)
        self.view.setMinimumHeight(150)
        rl.addWidget(self.view, 1)
        split.addWidget(results)
        split.setStretchFactor(0, 1)
        split.setStretchFactor(1, 2)
        split.setStretchFactor(2, 2)
        self.body.addWidget(split, 1)

        QShortcut(QKeySequence("Ctrl+Return"), self, activated=lambda: self.run())

        self.visibilityChanged.connect(lambda on: self.refresh(read=True) if on else None)
        self._outside = [(self.project.layersAdded, lambda _l: self._layers_changed()),
                         (self.project.layersRemoved, lambda _l: self._layers_changed()),
                         (self.project.readProject, lambda _d: self._project_read()),
                         (self.project.cleared, self._project_read),
                         (context.configChanged, self._config_changed)]
        for signal, slot in self._outside:
            signal.connect(slot)
        self._project_read()
        self._update_buttons()

    # ---- the builder page ------------------------------------------------------------------------

    def _builder_page(self) -> QWidget:
        page = QWidget()
        form = QFormLayout(page)
        self.base = QComboBox()
        self.base.currentIndexChanged.connect(lambda _i: self._builder_changed(base=True))
        form.addRow(self.tr("基にする表"), self.base)

        self.joins_box = QWidget()
        self.joins_layout = QVBoxLayout(self.joins_box)
        self.joins_layout.setContentsMargins(0, 0, 0, 0)
        self.join_rows: list[dict] = []
        add_join = QPushButton(self.tr("結合を追加"))
        add_join.setToolTip(self.tr("別の表をつなぐ（キーは属性の役割から候補を出す）"))
        add_join.clicked.connect(lambda: self.add_join())
        jl = QVBoxLayout()
        jl.addWidget(self.joins_box)
        jl.addWidget(add_join, 0, Qt.AlignmentFlag.AlignLeft)
        form.addRow(self.tr("結合"), jl)

        self.where = QLineEdit()
        self.where.setPlaceholderText("t0.MODEL = :model AND t0.SCORE >= 0.5")
        self.where.setToolTip(self.tr("WHERE 句（SQL）。t0 が基の表、t1… が結合した表。:名前 は実行時に入力"))
        self.where.textChanged.connect(lambda _t: self._builder_changed())
        form.addRow(self.tr("条件"), self.where)

        self.group = QListWidget()
        self.group.setToolTip(self.tr("チェックした列ごとに集計"))
        self.group.setMaximumHeight(140)
        #: The grouped columns in the order they were checked (the list shows them in the tables' order).
        self.group_order: list[str] = []
        self.group.itemChanged.connect(self._group_changed)
        form.addRow(self.tr("グループ"), self.group)

        self.figures_box = QWidget()
        self.figures_layout = QVBoxLayout(self.figures_box)
        self.figures_layout.setContentsMargins(0, 0, 0, 0)
        self.figure_rows: list[dict] = []
        add_figure = QPushButton(self.tr("集計を追加"))
        add_figure.clicked.connect(lambda: self.add_figure())
        fl = QVBoxLayout()
        fl.addWidget(self.figures_box)
        fl.addWidget(add_figure, 0, Qt.AlignmentFlag.AlignLeft)
        form.addRow(self.tr("集計"), fl)

        order = QHBoxLayout()
        self.descending = QCheckBox(self.tr("多い順"))
        self.descending.setChecked(True)
        self.descending.toggled.connect(lambda _on: self._builder_changed())
        self.row_limit = QSpinBox()
        self.row_limit.setRange(0, 1_000_000)
        self.row_limit.setSpecialValueText(self.tr("すべて"))
        self.row_limit.valueChanged.connect(lambda _v: self._builder_changed())
        order.addWidget(self.descending)
        order.addStretch(1)
        order.addWidget(QLabel(self.tr("上位")))
        order.addWidget(self.row_limit)
        form.addRow(self.tr("並び"), order)
        to_sql = QPushButton(self.tr("SQL を開く"))
        to_sql.setToolTip(self.tr("この集計の SQL を「SQL」で編集する（以後は SQL のほうで実行）"))
        to_sql.clicked.connect(lambda: self.tabs.setCurrentIndex(1))
        form.addRow("", to_sql)
        return page

    def table(self, name: str) -> Optional[Table]:
        return next((t for t in self.tables if t.name == name), None)

    def _columns_of(self, name: str) -> list[str]:
        t = self.table(name)
        if t is None:
            return []
        if t.columns:
            return [c.name for c in t.columns]
        return list(dict.fromkeys(v for v in t.roles.values()))

    def _alias_columns(self, upto: Optional[int] = None) -> list[tuple[str, str]]:
        """("t0.NAME", label) of the base table and the joins before `upto`."""
        b = self.builder(read_sql=False)
        out = []
        for i, (alias, name) in enumerate(aliases(b)):
            if upto is not None and i > upto:
                break
            for c in self._columns_of(name):
                out.append((f"{alias}.{c}", f"{alias}.{c}"))
        return out

    def _combo(self, items: list[tuple[str, str]], value: str = "", editable: bool = False) -> QComboBox:
        box = QComboBox()
        box.setEditable(editable)
        for data, text in items:
            box.addItem(text, data)
        i = box.findData(value)
        if i >= 0:
            box.setCurrentIndex(i)
        elif editable and value:
            box.setEditText(value)
        return box

    def add_join(self, join: Optional[Join] = None) -> None:
        if join is None:
            base = self.table(self.base.currentData() or "")
            if base is None:
                self.say(self.tr("基にする表を先に選ぶ"))
                return
            pick, keys, of = None, None, ""
            used = {n for _a, n in aliases(self.builder(read_sql=False))}
            sources = [("t0", base)] + [(f"t{i + 1}", self.table(r["table"].currentData() or "")) for i, r in enumerate(self.join_rows)]
            for t in self.tables:
                if t.name in used:
                    continue
                for alias, s in sources:
                    if s is not None and (k := suggest_join(s, t)):
                        pick, keys, of = t, k, alias
                        break
                if pick:
                    break
            pick = pick or next((t for t in self.tables if t.name not in used), None)
            if pick is None:
                return
            join = Join(pick.name, keys[0] if keys else "", keys[1] if keys else "", "inner", "" if of == "t0" else of)
        self._building = True
        row: dict = {}
        line = QWidget()
        h = QHBoxLayout(line)
        h.setContentsMargins(0, 0, 0, 0)
        row["widget"] = line
        row["table"] = self._combo([(t.name, t.name) for t in self.tables], join.table)
        row["table"].setToolTip(self.tr("つなぐ表"))
        row["type"] = self._combo([(k, v[0]) for k, v in JOIN_TYPES.items()], join.type)
        index = len(self.join_rows)
        row["left"] = self._combo(self._alias_columns(index), f"{join.of or 't0'}.{join.left}" if join.left else "", editable=True)
        row["left"].setToolTip(self.tr("こちらの列"))
        row["right"] = self._combo([(c, c) for c in self._columns_of(join.table)], join.right, editable=True)
        row["right"].setToolTip(self.tr("つなぐ表の列"))
        remove = QToolButton()
        remove.setText("×")
        remove.setToolTip(self.tr("この結合を外す"))
        remove.clicked.connect(lambda: self.remove_join(row))
        h.addWidget(row["table"], 2)
        h.addWidget(row["type"], 1)
        h.addWidget(QLabel(self.tr("キー")))
        h.addWidget(row["left"], 2)
        h.addWidget(QLabel("="))
        h.addWidget(row["right"], 2)
        h.addWidget(remove)
        self.join_rows.append(row)
        self.joins_layout.addWidget(line)
        row["table"].currentIndexChanged.connect(lambda _i: self._join_table_changed(row))
        for key in ("type", "left", "right"):
            row[key].currentIndexChanged.connect(lambda _i: self._builder_changed(columns=False))
            if row[key].isEditable():
                row[key].editTextChanged.connect(lambda _t: self._builder_changed(columns=False))
        self._building = False
        self._builder_changed()

    def _join_table_changed(self, row: dict) -> None:
        name = row["table"].currentData() or ""
        right = row["right"]
        right.blockSignals(True)
        right.clear()
        for c in self._columns_of(name):
            right.addItem(c, c)
        t = self.table(name)
        index = self.join_rows.index(row)
        sources = [("t0", self.table(self.base.currentData() or ""))] + [
            (f"t{i + 1}", self.table(r["table"].currentData() or "")) for i, r in enumerate(self.join_rows[:index])]
        for alias, s in sources:
            k = suggest_join(s, t) if s is not None and t is not None else None
            if k:
                i = row["left"].findData(f"{alias}.{k[0]}")
                if i >= 0:
                    row["left"].setCurrentIndex(i)
                right.setCurrentIndex(max(0, right.findData(k[1])))
                break
        right.blockSignals(False)
        self._builder_changed()

    def remove_join(self, row: dict) -> None:
        if row in self.join_rows:
            self.join_rows.remove(row)
            row["widget"].deleteLater()
            self._builder_changed()

    def add_figure(self, figure: Optional[Figure] = None) -> None:
        figure = figure or Figure("count")
        self._building = True
        row: dict = {}
        line = QWidget()
        h = QHBoxLayout(line)
        h.setContentsMargins(0, 0, 0, 0)
        row["widget"] = line
        row["function"] = self._combo([(k, v[0]) for k, v in AGGREGATES.items()], figure.function)
        row["column"] = self._combo([("", "")] + self._alias_columns(), figure.column, editable=True)
        row["name"] = QLineEdit(figure.name)
        row["name"].setPlaceholderText(self.tr("列名（空: 自動）"))
        remove = QToolButton()
        remove.setText("×")
        remove.clicked.connect(lambda: self.remove_figure(row))
        h.addWidget(row["function"], 1)
        h.addWidget(row["column"], 2)
        h.addWidget(row["name"], 1)
        h.addWidget(remove)
        self.figure_rows.append(row)
        self.figures_layout.addWidget(line)
        row["function"].currentIndexChanged.connect(lambda _i: self._builder_changed(columns=False))
        row["column"].currentIndexChanged.connect(lambda _i: self._builder_changed(columns=False))
        row["column"].editTextChanged.connect(lambda _t: self._builder_changed(columns=False))
        row["name"].textChanged.connect(lambda _t: self._builder_changed(columns=False))
        self._building = False
        self._builder_changed()

    def remove_figure(self, row: dict) -> None:
        if row in self.figure_rows:
            self.figure_rows.remove(row)
            row["widget"].deleteLater()
            self._builder_changed()

    def _group_changed(self, item: QListWidgetItem) -> None:
        if self._building:
            return
        data = item.data(Qt.ItemDataRole.UserRole)
        if data in self.group_order:
            self.group_order.remove(data)
        if item.checkState() == Qt.CheckState.Checked:
            self.group_order.append(data)
        self._builder_changed(columns=False)

    @staticmethod
    def _combo_value(box: QComboBox) -> str:
        if box.isEditable():
            text = box.currentText().strip()
            i = box.findText(text)
            return (box.itemData(i) or text) if i >= 0 else text
        return box.currentData() or ""

    def builder(self, read_sql: bool = True) -> Builder:
        """The choices of 「かんたん集計」."""
        joins = []
        for r in self.join_rows:
            left = self._combo_value(r["left"])
            of, _, column = left.partition(".") if "." in left else ("t0", "", left)
            joins.append(Join(r["table"].currentData() or "", column, self._combo_value(r["right"]), r["type"].currentData() or "inner",
                              "" if of == "t0" else of))
        group = [self.group.item(i).data(Qt.ItemDataRole.UserRole) for i in range(self.group.count())
                 if self.group.item(i).checkState() == Qt.CheckState.Checked]
        order = {g: i for i, g in enumerate(self.group_order)}
        group.sort(key=lambda g: order.get(g, len(order)))
        figures = [Figure(r["function"].currentData() or "count", self._combo_value(r["column"]), r["name"].text().strip())
                   for r in self.figure_rows]
        return Builder(self.base.currentData() or "", joins, self.where.text().strip(), group, figures,
                       self.descending.isChecked(), self.row_limit.value())

    def _builder_changed(self, base: bool = False, columns: bool = True) -> None:
        """The choices changed: the column lists follow the tables, and the SQL follows the choices."""
        if self._building:
            return
        self._building = True
        try:
            if base or columns:
                self._refill_columns()
            if self.tabs.currentIndex() == 0:
                try:
                    text = build_sql(self.builder())
                except AnalysisError:
                    text = ""
                if text and text != self.sql.toPlainText():
                    self.sql.setPlainText(text)
        finally:
            self._building = False
        self._update_buttons()

    def _refill_columns(self) -> None:
        checked = {self.group.item(i).data(Qt.ItemDataRole.UserRole) for i in range(self.group.count())
                   if self.group.item(i).checkState() == Qt.CheckState.Checked}
        self.group.blockSignals(True)
        self.group.clear()
        for data, text in self._alias_columns():
            item = QListWidgetItem(text)
            item.setData(Qt.ItemDataRole.UserRole, data)
            item.setFlags(item.flags() | Qt.ItemFlag.ItemIsUserCheckable)
            item.setCheckState(Qt.CheckState.Checked if data in checked else Qt.CheckState.Unchecked)
            self.group.addItem(item)
        self.group.blockSignals(False)
        for index, r in enumerate(self.join_rows):
            self._refill(r["left"], self._alias_columns(index))
        for r in self.figure_rows:
            self._refill(r["column"], [("", "")] + self._alias_columns())

    @staticmethod
    def _refill(box: QComboBox, items: list[tuple[str, str]]) -> None:
        value = AnalysisDock._combo_value(box)
        box.blockSignals(True)
        box.clear()
        for data, text in items:
            box.addItem(text, data)
        i = box.findData(value)
        if i >= 0:
            box.setCurrentIndex(i)
        elif box.isEditable():
            box.setEditText(value)
        box.blockSignals(False)

    def set_builder(self, b: Builder) -> None:
        """Shows the choices of an analysis in 「かんたん集計」."""
        self._building = True
        try:
            for r in list(self.join_rows):
                r["widget"].deleteLater()
            for r in list(self.figure_rows):
                r["widget"].deleteLater()
            self.join_rows, self.figure_rows = [], []
            i = self.base.findData(b.table)
            if i < 0 and b.table:
                self.base.addItem(b.table, b.table)
                i = self.base.count() - 1
            self.base.setCurrentIndex(max(i, 0) if self.base.count() else -1)
            self.where.setText(b.where)
            self.group_order = list(b.group)
            self.descending.setChecked(b.descending)
            self.row_limit.setValue(b.limit)
        finally:
            self._building = False
        for j in b.joins:
            self.add_join(j)
        self._building = True
        try:
            self._refill_columns()
            for i in range(self.group.count()):
                item = self.group.item(i)
                item.setCheckState(Qt.CheckState.Checked if item.data(Qt.ItemDataRole.UserRole) in b.group else Qt.CheckState.Unchecked)
            # Grouped columns of tables not read yet: kept as they are.
            listed = {self.group.item(i).data(Qt.ItemDataRole.UserRole) for i in range(self.group.count())}
            for g in b.group:
                if g not in listed:
                    item = QListWidgetItem(g)
                    item.setData(Qt.ItemDataRole.UserRole, g)
                    item.setFlags(item.flags() | Qt.ItemFlag.ItemIsUserCheckable)
                    item.setCheckState(Qt.CheckState.Checked)
                    self.group.addItem(item)
        finally:
            self._building = False
        for f in b.figures:
            self.add_figure(f)
        self._builder_changed(columns=False)

    # ---- tables ----------------------------------------------------------------------------------

    def refresh(self, read: bool = False) -> None:
        """Lists the tables again (the settings file's and the project's); `read`: their Esri columns too."""
        known = {t.name: t for t in self.tables}
        esri = esri_tables(self.context.config)
        for t in esri:
            old = known.get(t.name)
            if old is not None and old.url == t.url and old.columns:
                t.columns, t.spatial = old.columns, old.spatial
        taken = {t.name.lower() for t in esri}
        layers = analysis_io.project_tables(self.project, taken)
        self.tables = esri + layers
        self._render_tables()
        self._refill_bases()
        if read:
            self.read_columns()
        self._fill_chooser()

    def _refill_bases(self) -> None:
        value = self.base.currentData()
        self._building = True
        self.base.clear()
        for t in self.tables:
            self.base.addItem(f"{t.name}（{t.title}）" if t.title != t.name else t.name, t.name)
        i = self.base.findData(value)
        if i < 0 and value:
            self.base.addItem(value, value)
            i = self.base.count() - 1
        self.base.setCurrentIndex(max(i, 0) if self.base.count() else -1)
        for r in self.join_rows:
            self._refill(r["table"], [(t.name, t.name) for t in self.tables])
        self._building = False
        self._builder_changed(columns=True)

    def _render_tables(self) -> None:
        expanded = {self.tree.topLevelItem(i).text(0): self.tree.topLevelItem(i).isExpanded() for i in range(self.tree.topLevelItemCount())}
        open_tables = set()
        for i in range(self.tree.topLevelItemCount()):
            g = self.tree.topLevelItem(i)
            for j in range(g.childCount()):
                if g.child(j).isExpanded():
                    open_tables.add(g.child(j).data(0, Qt.ItemDataRole.UserRole))
        self.tree.clear()
        groups = [(self.tr("Esri（設定ファイル）"), [t for t in self.tables if t.source == "esri"]),
                  (self.tr("プロジェクトのレイヤー・テーブル"), [t for t in self.tables if t.source == "layer"])]
        for label, tables in groups:
            g = QTreeWidgetItem([label, self.tr("{n} 表").format(n=len(tables)), ""])
            g.setFlags(g.flags() & ~Qt.ItemFlag.ItemIsSelectable)
            self.tree.addTopLevelItem(g)
            for t in tables:
                where = " AND ".join(f"({w})" for w in (t.where, self.conditions.get(t.name)) if w)
                item = QTreeWidgetItem([t.name, t.title, where])
                item.setData(0, Qt.ItemDataRole.UserRole, t.name)
                item.setToolTip(1, plain_tip(t.url or t.title))
                if t.where:
                    item.setToolTip(2, plain_tip(self.tr("設定ファイルの where: {w}").format(w=t.where)))
                g.addChild(item)
                for c in t.columns or [Column(n, "", r) for r, n in t.roles.items()]:
                    about = " · ".join(x for x in (c.type, c.role and f"役割 {c.role}", c.alias if c.alias and c.alias != c.name else "") if x)
                    child = QTreeWidgetItem([c.name, about, ""])
                    child.setData(0, Qt.ItemDataRole.UserRole + 1, c.name)
                    item.addChild(child)
                item.setExpanded(t.name in open_tables)
            g.setExpanded(expanded.get(label, True))
        self.tree.resizeColumnToContents(0)

    def read_columns(self) -> None:
        """Reads the columns of the Esri tables not read yet (their layer information, in a task)."""
        if self._columns_task is not None:
            return
        todo = [t for t in self.tables if t.source == "esri" and not t.columns and t.url not in self._columns_read]
        if not todo:
            return
        client = self.context.esri()
        asks = [(t, self.context.token_for(t.url, t.token)) for t in todo]

        def work(_task):
            out = {}
            for t, token in asks:
                try:
                    info = client.layer(t.url, token)
                    fields = [f for f in info.raw.get("fields") or [] if isinstance(f, dict) and isinstance(f.get("name"), str)]
                    columns = [Column(f["name"], esri_type(f.get("type", "")), t.role_of(f["name"]), str(f.get("alias") or ""))
                               for f in fields if str(f.get("type", "")).lower() not in ("esrifieldtypegeometry", "esrifieldtypeblob", "esrifieldtyperaster")]
                    if info.geometry_type:
                        columns.append(Column("SHAPE", "TEXT", "", "形状（WKT）"))
                    out[t.url] = (columns, bool(info.geometry_type), "")
                except EsriError as error:
                    out[t.url] = ([], None, str(error))
            return out

        def done(found) -> None:
            self._columns_task = None
            if sip.isdeleted(self):
                return
            problems = []
            for t in self.tables:
                if t.url in found:
                    columns, spatial, problem = found[t.url]
                    self._columns_read.add(t.url)
                    if columns:
                        t.columns, t.spatial = columns, spatial
                    if problem:
                        problems.append(f"{t.name}: {problem}")
            self._render_tables()
            self._builder_changed(columns=True)
            if problems:
                self.say(self.tr("列を読めない表: {p}").format(p="; ".join(problems)))

        def failed(message: str) -> None:
            self._columns_task = None
            if not sip.isdeleted(self):
                self.say(self.tr("列の読み込みに失敗: {m}").format(m=message))

        self._columns_task = run_task(self.tr("データ分析: 表の列の読み込み"), work, done, failed, hidden=True)

    def selected_table(self) -> Optional[Table]:
        item = self.tree.currentItem()
        while item is not None and item.data(0, Qt.ItemDataRole.UserRole) is None:
            item = item.parent()
        return self.table(item.data(0, Qt.ItemDataRole.UserRole)) if item is not None else None

    def edit_condition(self, name: Optional[str] = None, text: Optional[str] = None) -> None:
        """The condition the table is read with (asked when `text` is not given)."""
        t = self.table(name) if name else self.selected_table()
        if t is None:
            self.say(self.tr("表を選んでから"))
            return
        if text is None:
            how = self.tr("Esri の WHERE 句（例: MODEL = 'yolo-v8' AND SCORE >= 0.5）") if t.source == "esri" else \
                self.tr("QGIS の式（例: \"kind\" = '船舶'）")
            text, ok = QInputDialog.getText(self, self.tr("読み込み条件"), self.tr("{n} を読むときの条件。空: すべて\n{h}").format(n=t.name, h=how),
                                            QLineEdit.EchoMode.Normal, self.conditions.get(t.name, ""))
            if not ok:
                return
        if text.strip():
            self.conditions[t.name] = text.strip()
        else:
            self.conditions.pop(t.name, None)
        self._render_tables()

    def _insert_name(self, item: QTreeWidgetItem, _column: int) -> None:
        name = item.data(0, Qt.ItemDataRole.UserRole) or item.data(0, Qt.ItemDataRole.UserRole + 1)
        if not name:
            return
        plain = name if name.replace("_", "a").isalnum() and not name[0].isdigit() else quote_name(name)
        self.tabs.setCurrentIndex(1)
        self.sql.insertPlainText(plain)
        self.sql.setFocus()

    def _layers_changed(self) -> None:
        if self.isVisible():
            self.refresh()

    def _config_changed(self) -> None:
        self._columns_read.clear()
        self.refresh(read=self.isVisible())

    # ---- SQL and values --------------------------------------------------------------------------

    def _sql_changed(self) -> None:
        if not self._building and self.tabs.currentIndex() == 0 and self.sql.hasFocus():
            self.tabs.setCurrentIndex(1)
        self._render_params()
        self._update_buttons()

    def _render_params(self) -> None:
        names = parameters(self.sql.toPlainText())
        for n, e in self.param_edits.items():
            self.values[n] = e.text()
        if names == list(self.param_edits):
            return
        self._clear_params()
        for n in names:
            e = QLineEdit(self.values.get(n, ""))
            e.setPlaceholderText(self.tr("値（文字。数値の列とは数値で比べる）"))
            e.returnPressed.connect(lambda: self.run())
            self.params_form.addRow(f":{n}", e)
            self.param_edits[n] = e
        self.params_box.setVisible(bool(names))

    def _clear_params(self) -> None:
        self.param_edits = {}
        while self.params_form.rowCount():
            self.params_form.removeRow(0)

    def set_sql(self, text: str) -> None:
        """Puts a query in 「SQL」 (shown)."""
        self.tabs.setCurrentIndex(1)
        self.sql.setPlainText(text)

    def set_value(self, name: str, value: str) -> None:
        self.values[name] = value
        if name in self.param_edits:
            self.param_edits[name].setText(value)

    def current_values(self) -> dict[str, str]:
        out = dict(self.values)
        out.update({n: e.text() for n, e in self.param_edits.items()})
        return out

    # ---- running ---------------------------------------------------------------------------------

    def say(self, text: str) -> None:
        self.status.setText(text)

    def run(self, then: Optional[Callable[[Optional[Result]], None]] = None) -> None:
        """Reads the tables the SQL names and runs it (in a task); `then(result or None)` after."""
        if self.task is not None:
            return
        if self.tabs.currentIndex() == 0:
            try:
                self._building = True
                self.sql.setPlainText(build_sql(self.builder()))
            except AnalysisError as error:
                self.say(str(error))
                if then:
                    then(None)
                return
            finally:
                self._building = False
        sql = self.sql.toPlainText()
        values = {n: v for n, v in self.current_values().items() if n in parameters(sql)}
        empty = [n for n in parameters(sql) if not values.get(n)]
        if empty:
            self.say(self.tr("値を入力: {n}").format(n="、".join(":" + n for n in empty)))
            if then:
                then(None)
            return
        conditions = dict(self.conditions)
        limit = self.limit.value()
        try:
            prepared = analysis_io.prepare(self.tables, sql, conditions, limit, self.project)
        except AnalysisError as error:
            self.say(str(error))
            if then:
                then(None)
            return
        client = self.context.esri()
        tokens = {t.name: self.context.token_for(t.url, t.token) for t, _r in prepared if t.source == "esri"}

        def work(task):
            return analysis_io.run(client, prepared, sql, values, conditions, lambda t: tokens.get(t.name), limit, task)

        def done(answer) -> None:
            self.task = None
            if sip.isdeleted(self):
                return
            result, counts = answer
            self.show_result(result)
            read = "、".join(f"{n} {c:,} 行" for n, c in counts.items())
            more = self.tr("（先頭 {n:,} 行まで）").format(n=len(result.rows)) if result.cut else ""
            shown = self.tr("、表示は先頭 {n:,} 行").format(n=SHOWN_ROWS) if len(result.rows) > SHOWN_ROWS else ""
            self.say(self.tr("結果 {n:,} 行{more}{shown}（読んだ表: {read}）").format(n=len(result.rows), more=more, shown=shown, read=read))
            self._update_buttons()
            if then:
                then(result)

        def failed(message: str) -> None:
            self.task = None
            if sip.isdeleted(self):
                return
            self.say(self.tr("実行できない: {m}").format(m=message))
            self._update_buttons()
            if then:
                then(None)

        self.say(self.tr("読み込み・集計中…"))
        self.task = run_task(self.tr("データ分析"), work, done, failed)
        self._update_buttons()

    def cancel(self) -> None:
        if self.task is not None:
            self.task.cancel()

    def show_result(self, result: Optional[Result]) -> None:
        self.result = result
        self.model.set_result(result)
        self.view.resizeColumnsToContents()
        self._update_buttons()

    # ---- the result ------------------------------------------------------------------------------

    def csv_text(self) -> str:
        return csv_text(self.result) if self.result else ""

    def save_csv(self, path: Optional[str] = None) -> Optional[str]:
        if not self.result:
            return None
        if path is None:
            safe = "".join(c if c.isalnum() or c in "-_" else "_" for c in (self.current or "analysis"))
            path, _f = QFileDialog.getSaveFileName(self, self.tr("分析の結果を保存"), f"{safe}.csv", self.tr("CSV (*.csv)"))
            if not path:
                return None
        try:
            with open(path, "w", encoding="utf-8-sig", newline="") as f:
                f.write(self.csv_text())
        except OSError as error:
            self.say(self.tr("保存できない: {m}").format(m=error))
            return None
        self.say(self.tr("{n:,} 行を保存: {p}").format(n=len(self.result.rows), p=path))
        return path

    def copy(self) -> None:
        if self.result:
            QApplication.clipboard().setText(tsv_text(self.result))
            self.say(self.tr("{n:,} 行をコピー").format(n=len(self.result.rows)))

    def add_layer(self, name: Optional[str] = None) -> Optional[QgsVectorLayer]:
        """The result as a layer of the project (with shapes when it has the column SHAPE)."""
        if not self.result:
            return None
        layer = analysis_io.result_layer(self.result, name or self.tr("分析: {n}").format(n=self.current or self.tr("結果")))
        self.project.addMapLayer(layer)
        self.say(self.tr("レイヤー「{n}」を追加（{c:,} 行）").format(n=layer.name(), c=layer.featureCount()))
        return layer

    # ---- analyses in the project -----------------------------------------------------------------

    def _project_read(self) -> None:
        text, _ok = self.project.readEntry(PROJECT_SCOPE, PROJECT_KEY, "")
        self.analyses = analyses_of(text)
        self.refresh(read=self.isVisible())
        if self.current and not any(a.name == self.current for a in self.analyses):
            self.current = ""
        self._fill_chooser()

    def _write(self) -> None:
        self.project.writeEntry(PROJECT_SCOPE, PROJECT_KEY, analyses_text(self.analyses))
        self.project.setDirty(True)

    def _fill_chooser(self) -> None:
        self.chooser.blockSignals(True)
        self.chooser.clear()
        self.chooser.addItem(self.tr("（新しい分析）"), "")
        for a in self.analyses:
            self.chooser.addItem(a.name, a.name)
        found = examples(self.tables)
        if found:
            self.chooser.insertSeparator(self.chooser.count())
            for a in found:
                self.chooser.addItem(self.tr("例: {n}").format(n=a.name), EXAMPLE + a.name)
        i = self.chooser.findData(self.current if self.current or not self._example else EXAMPLE + self._example)
        self.chooser.setCurrentIndex(max(i, 0))
        self.chooser.blockSignals(False)
        self._update_buttons()

    def analysis(self) -> Analysis:
        """What the dock shows, as an analysis (named as the one shown)."""
        return Analysis(self.current, self.sql.toPlainText(), self.builder(), self.current_values(), dict(self.conditions),
                        "sql" if self.tabs.currentIndex() == 1 else "builder")

    def show_analysis(self, a: Analysis, name: str = "", example: str = "") -> None:
        self.current = name
        #: The example shown (not saved yet), for the chooser.
        self._example = example
        self.conditions = dict(a.conditions)
        self.values = dict(a.values)
        self._clear_params()
        self._render_tables()
        self.tabs.setCurrentIndex(0)
        self.set_builder(a.builder)
        self._building = True
        try:
            self.sql.setPlainText(a.sql or "")
        finally:
            self._building = False
        self._clear_params()
        self._render_params()
        self.tabs.setCurrentIndex(1 if a.mode == "sql" else 0)
        self.show_result(None)
        self.say("")
        self._fill_chooser()

    def load(self, name: str) -> bool:
        """Shows a saved analysis, or an example (`例: …`)."""
        for a in self.analyses:
            if a.name == name:
                self.show_analysis(a, a.name)
                return True
        for a in examples(self.tables):
            if a.name == name or self.tr("例: {n}").format(n=a.name) == name:
                self.show_analysis(a, "", a.name)
                return True
        return False

    def _chosen(self) -> None:
        data = self.chooser.currentData() or ""
        if not data:
            self.new()
        elif data.startswith(EXAMPLE):
            self.load(data[len(EXAMPLE):])
        else:
            self.load(data)

    def new(self) -> None:
        self.show_analysis(Analysis(""), "")

    def save(self, name: Optional[str] = None, ask: bool = False) -> Optional[str]:
        """Keeps the analysis in the project (under `name`, the one shown, or a name asked for)."""
        if name is None and (ask or not self.current):
            name, ok = QInputDialog.getText(self, self.tr("分析の保存"), self.tr("分析の名前（プロジェクトに保存）"), QLineEdit.EchoMode.Normal,
                                            self.current)
            if not ok:
                return None
        name = (name if name is not None else self.current).strip()
        if not name:
            self.say(self.tr("名前を入れる"))
            return None
        if name != self.current and any(a.name == name for a in self.analyses):
            if not ask_no(self, self.tr("分析の保存"), self.tr("「{n}」はすでにある。上書きする？").format(n=name)):
                return None
        a = self.analysis()
        a.name = name
        self.analyses = [x for x in self.analyses if x.name != name] + [a]
        self.current, self._example = name, ""
        self._write()
        self._fill_chooser()
        self.say(self.tr("「{n}」をプロジェクトに保存（プロジェクトの保存で残る）").format(n=name))
        return name

    def delete(self, confirm: bool = True) -> bool:
        if not self.current:
            return False
        if confirm and not ask_no(self, self.tr("分析の削除"), self.tr("「{n}」をプロジェクトから削除する？").format(n=self.current)):
            return False
        self.analyses = [x for x in self.analyses if x.name != self.current]
        self._write()
        self.current = ""
        self._fill_chooser()
        self.say(self.tr("削除した"))
        return True

    def _update_buttons(self) -> None:
        running = self.task is not None
        self.run_button.setEnabled(not running)
        self.cancel_button.setEnabled(running)
        has = bool(self.result and self.result.columns)
        for key in ("csv", "copy", "layer"):
            self.buttons[key].setEnabled(has and not running)
        self.buttons["delete"].setEnabled(bool(self.current))

    def close_all(self) -> None:
        """Lets go of the project's signals and stops a running query (the plugin unloads)."""
        self.cancel()
        for signal, slot in self._outside:
            try:
                signal.disconnect(slot)
            except (TypeError, RuntimeError):
                pass
