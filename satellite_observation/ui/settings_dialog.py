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

from ..qgis_io import settings
from ..qgis_io.context import PluginContext


class SettingsDialog(QDialog):
    def __init__(self, context: PluginContext, parent: QWidget | None = None):
        super().__init__(parent)
        self.context = context
        self.setWindowTitle(self.tr("衛星撮像計画・観測記録の設定"))
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

    def _browse(self) -> None:
        path, _ = QFileDialog.getOpenFileName(self, self.tr("設定ファイルの選択"), self.source.text(), self.tr("設定ファイル (*.json *.jsonc);;すべてのファイル (*)"))
        if path:
            self.source.setText(path)
            self._load()

    def _load(self) -> None:
        self._keep_auth()
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
        if self.source.text().strip() != settings.config_source():
            settings.set_config_source(self.source.text())
            self.context.reload()
        super().accept()
