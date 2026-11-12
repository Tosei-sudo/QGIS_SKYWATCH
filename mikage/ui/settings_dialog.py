"""The plugin's settings: the settings file, who records, the authentication of each service, and what was wrong in the file."""
from __future__ import annotations

from qgis.gui import QgsAuthConfigSelect
from qgis.PyQt.QtCore import Qt
from qgis.PyQt.QtWidgets import (
    QCheckBox,
    QDialog,
    QDialogButtonBox,
    QFileDialog,
    QFormLayout,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QListWidget,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from ..core.obs import place_cache
from ..qgis_io import overviews_io, settings
from ..qgis_io.context import PluginContext
from . import last_inputs


class SettingsDialog(QDialog):
    def __init__(self, context: PluginContext, parent: QWidget | None = None):
        super().__init__(parent)
        self.context = context
        self.setWindowTitle(self.tr("MIKAGE の設定"))
        self.resize(760, 560)

        self.source = QLineEdit(settings.config_source())
        shipped = settings.bundled_config()
        self.source.setPlaceholderText(self.tr("空欄なら同梱の config.json を使用") if shipped
                                       else self.tr("config.json のパスか URL（https://…/config.json）"))
        if shipped:
            self.source.setToolTip(self.tr("同梱の設定ファイル: {path}。ここに指定するとそちらを優先").format(path=shipped))
        browse = QPushButton(self.tr("参照…"))
        browse.clicked.connect(self._browse)
        load = QPushButton(self.tr("読み込み"))
        load.clicked.connect(self._load)
        #: The zip locks the plugin to its shipped settings file: no other can be chosen.
        self.locked = settings.config_locked()
        if self.locked:
            self.source.setText(shipped)
            self.source.setReadOnly(True)
            self.source.setToolTip(self.tr("同梱の設定ファイルに固定（ほかの設定ファイルは指定不可）: {path}").format(path=shipped))
            browse.setEnabled(False)
            load.setToolTip(self.tr("同梱の設定ファイルを読み直す"))
        row = QHBoxLayout()
        row.addWidget(self.source, 1)
        row.addWidget(browse)
        row.addWidget(load)

        self.recorder = QLineEdit(settings.recorder_setting())
        self.recorder.setPlaceholderText(self.tr("空欄なら認証設定のユーザー名"))

        form = QFormLayout()
        form.addRow(self.tr("設定ファイル"), row)
        form.addRow(self.tr("記録者"), self.recorder)
        self.startup_layers = QCheckBox(self.tr("起動時と新規プロジェクトで、設定ファイルのレイヤー（layers）を追加"))
        self.startup_layers.setChecked(settings.startup_layers_on())
        form.addRow("", self.startup_layers)
        self.cache = QLabel()
        self.cache.setWordWrap(True)
        self.clear_cache = QPushButton(self.tr("キャッシュを消去"))
        self.clear_cache.setToolTip(self.tr("書き込めないフォルダの画像のピラミッド（.ovr）を消す。使用中のことがあるので、次に QGIS を起動したときに消去"))
        self.clear_cache.clicked.connect(self._clear_cache)
        cache_row = QHBoxLayout()
        cache_row.addWidget(self.cache, 1)
        cache_row.addWidget(self.clear_cache)
        form.addRow(self.tr("ピラミッドのキャッシュ"), cache_row)
        self._show_cache()
        self.places = QLabel()
        self.places.setWordWrap(True)
        self.clear_places = QPushButton(self.tr("控えを消去"))
        self.clear_places.setToolTip(self.tr("通報・エクスポート・式の関数で読んだ地名・国のポリゴンの控えを消す（次に使うときサーバーから読み直す）"))
        self.clear_places.clicked.connect(self._clear_places)
        places_row = QHBoxLayout()
        places_row.addWidget(self.places, 1)
        places_row.addWidget(self.clear_places)
        form.addRow(self.tr("地名・国の控え"), places_row)
        self._show_places()
        self.last_inputs = QLabel(self.tr("前回の入力を次の入力の初期値に使用（このユーザープロファイルに保存）"))
        self.last_inputs.setWordWrap(True)
        self.forget_inputs = QPushButton(self.tr("覚えた値を消去"))
        self.forget_inputs.setToolTip(self.tr("撮像依頼・撮像計画の衛星と条件・観測記録の種別などで覚えた前回の入力を消し、各フォームの既定値に戻す"))
        self.forget_inputs.clicked.connect(self._forget_inputs)
        inputs_row = QHBoxLayout()
        inputs_row.addWidget(self.last_inputs, 1)
        inputs_row.addWidget(self.forget_inputs)
        form.addRow(self.tr("入力の初期値"), inputs_row)

        self.services = QTableWidget(0, 4)
        self.services.setHorizontalHeaderLabels([self.tr("用途"), self.tr("名前"), self.tr("URL"), self.tr("認証設定")])
        self.services.horizontalHeader().setSectionResizeMode(2, QHeaderView.ResizeMode.Stretch)
        self.services.verticalHeader().setVisible(False)
        self.problems = QListWidget()
        self.status = QLabel()
        self.status.setWordWrap(True)

        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)

        layout = QVBoxLayout(self)
        layout.addLayout(form)
        layout.addWidget(QLabel(self.tr("接続の認証（QGIS の認証設定。OAuth2・ESRI token など）")))
        layout.addWidget(self.services, 2)
        layout.addWidget(QLabel(self.tr("設定ファイルの問題")))
        layout.addWidget(self.problems, 1)
        layout.addWidget(self.status)
        layout.addWidget(buttons)

        self._selects: dict[str, QgsAuthConfigSelect] = {}
        context.configChanged.connect(self._fill)
        self._fill()

    def done(self, result: int) -> None:
        """Closed: no longer fills itself again on each reading of the settings (#87)."""
        try:
            self.context.configChanged.disconnect(self._fill)
        except (TypeError, RuntimeError):
            pass
        super().done(result)

    def _show_cache(self) -> None:
        folder = overviews_io.cache_folder()
        if not folder:
            self.cache.setText(self.tr("なし（書き込めないフォルダの画像にはピラミッドを作らない）"))
            self.clear_cache.setEnabled(False)
            return
        n, size = overviews_io.cache_size(folder)
        text = self.tr("{folder}（{n} 件、{mb:.1f} MB）").format(folder=folder, n=n, mb=size / 1048576)
        if settings.overview_cache_to_clear():
            text += self.tr("。次回起動時に消去")
        self.cache.setText(text)
        self.clear_cache.setEnabled(n > 0 and not settings.overview_cache_to_clear())

    def _clear_cache(self) -> None:
        overviews_io.clear_cache_next_start()
        self._show_cache()

    def _show_places(self) -> None:
        squares, shapes, size = place_cache.PLACES.info()
        self.places.setText(self.tr("1 度四方 {s} マス、ポリゴン {n} 件、{mb:.1f} MB（使った場所だけ。上限 {top} MB、古いものから消す）").format(
            s=squares, n=shapes, mb=size / 1048576, top=place_cache.DISK_BYTES // 1048576))
        self.clear_places.setEnabled(squares > 0 or shapes > 0)

    def _clear_places(self) -> None:
        place_cache.PLACES.clear()
        self._show_places()

    def _forget_inputs(self) -> None:
        last_inputs.forget_all()
        self.last_inputs.setText(self.tr("消去済み。次に開くフォームから既定値（開いているフォームは今の値のまま）"))

    def _browse(self) -> None:
        path, _ = QFileDialog.getOpenFileName(self, self.tr("設定ファイルの選択"), self.source.text(), self.tr("設定ファイル (*.json *.jsonc);;すべてのファイル (*)"))
        if path:
            self.source.setText(path)
            self._load()

    def _load(self) -> None:
        self._keep_auth()
        if not self.locked:
            settings.set_config_source(self.source.text())
        self.status.setText(self.tr("読み込み中…"))
        self.context.reload()

    def _keep_auth(self) -> None:
        mapping = settings.auth_map()
        for url, select in self._selects.items():
            mapping[url] = select.configId()
        settings.set_auth_map(mapping)

    def _fill(self) -> None:
        mapping = settings.auth_map()
        services = self.context.services()
        self._selects = {}
        self.services.setRowCount(len(services))
        for i, s in enumerate(services):
            for j, text in enumerate((s.role, s.label, s.url)):
                item = QTableWidgetItem(text)
                item.setFlags(item.flags() & ~Qt.ItemFlag.ItemIsEditable)
                self.services.setItem(i, j, item)
            select = QgsAuthConfigSelect(self.services)
            select.setConfigId(mapping.get(s.url, ""))
            if s.has_token:
                select.setToolTip(self.tr("設定ファイルに token あり（認証設定を選ぶと token は不使用）"))
            self.services.setCellWidget(i, 3, select)
            self._selects[s.url] = select
        self.services.resizeRowsToContents()
        self.problems.clear()
        self.problems.addItems(self.context.problems)
        if not self.context.source:
            self.status.setText(self.tr("設定ファイルなし（サンプルの衛星とローカル保存で動作）"))
        elif self.context.locked and not self.context.problems:
            self.status.setText(self.tr("同梱の設定ファイルに固定。問題なし（接続 {n} 件）").format(n=len(services)))
        elif self.context.bundled and not self.context.problems:
            self.status.setText(self.tr("同梱の設定ファイルを使用。問題なし（接続 {n} 件）").format(n=len(services)))
        elif self.context.problems:
            self.status.setText(self.tr("問題 {n} 件").format(n=len(self.context.problems)))
        else:
            self.status.setText(self.tr("問題なし（接続 {n} 件）").format(n=len(services)))

    def accept(self) -> None:
        self._keep_auth()
        settings.set_recorder(self.recorder.text())
        settings.set_startup_layers_on(self.startup_layers.isChecked())
        if not self.locked and self.source.text().strip() != settings.config_source():
            settings.set_config_source(self.source.text())
            self.context.reload()
        super().accept()
