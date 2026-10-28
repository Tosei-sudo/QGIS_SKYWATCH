"""
「サーバ処理…」, as the viewer's gp-dialog.ts: a service (the settings
file's `gpServices`, or a typed URL of a GPServer, a GP task, an OGC API
landing page, its `/processes` or one process) read, its tasks listed, and
the one chosen opened as a Processing algorithm of the 「サーバ処理」 group,
where its inputs are filled in and it runs.

The service's sign-in is a QGIS authentication configuration, kept for the
service like the others' in the plugin settings (a token in the settings
file is still used, for its own service only).
"""
from __future__ import annotations

from typing import Callable, Optional

from qgis.core import QgsSettings
from qgis.gui import QgsAuthConfigSelect
from qgis.PyQt.QtCore import QCoreApplication, Qt
from qgis.PyQt.QtWidgets import QComboBox, QDialog, QFormLayout, QHBoxLayout, QLabel, QListWidget, QListWidgetItem, QPushButton, QVBoxLayout

from ..core.gp import GpClient, GpError, GpService, GpTask, safe_url, service_root
from ..processing_provider import gp_algorithm
from ..qgis_io import gp_io, settings
from ..qgis_io.tasks import run_task

RECENT_KEY = "satellite_observation/gp_recent"
MAX_RECENT = 10


def _tr(text: str) -> str:
    return QCoreApplication.translate("RemoteProcessingDialog", text)


def _open_dialog(algorithm_id: str) -> None:
    import processing  # QGIS's Processing plugin (loaded with QGIS)

    processing.execAlgorithmDialog(algorithm_id, {})


class RemoteProcessingDialog(QDialog):
    def __init__(self, context, parent=None, open_algorithm: Callable[[str], None] = _open_dialog):
        super().__init__(parent)
        self.context = context
        self.open_algorithm = open_algorithm
        self.task = None
        #: The service read last: its tasks, and the token of the settings file for it.
        self.service: Optional[GpService] = None
        self.token = ""
        self.setWindowTitle(_tr("サーバ処理"))
        self.resize(520, 420)
        layout = QVBoxLayout(self)
        form = QFormLayout()
        self.url = QComboBox()
        self.url.setEditable(True)
        self.url.setMinimumContentsLength(36)
        self.url.lineEdit().setPlaceholderText(_tr("GPServer・GP タスク・OGC API – Processes の URL"))
        self.url.currentTextChanged.connect(self._url_changed)
        self.url.lineEdit().returnPressed.connect(self.load)
        form.addRow(_tr("サービス"), self.url)
        self.auth = QgsAuthConfigSelect(self)
        form.addRow(_tr("認証設定"), self.auth)
        layout.addLayout(form)
        row = QHBoxLayout()
        self.load_button = QPushButton(_tr("読み込み"))
        self.load_button.clicked.connect(self.load)
        row.addWidget(self.load_button)
        row.addStretch(1)
        layout.addLayout(row)
        self.status = QLabel()
        self.status.setWordWrap(True)
        self.status.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        layout.addWidget(self.status)
        self.tasks = QListWidget()
        self.tasks.itemDoubleClicked.connect(lambda _: self.open_selected())
        self.tasks.currentRowChanged.connect(lambda row: self.open_button.setEnabled(row >= 0))
        layout.addWidget(self.tasks, 1)
        buttons = QHBoxLayout()
        buttons.addStretch(1)
        self.open_button = QPushButton(_tr("開く"))
        self.open_button.setEnabled(False)
        self.open_button.clicked.connect(self.open_selected)
        buttons.addWidget(self.open_button)
        close = QPushButton(_tr("閉じる"))
        close.clicked.connect(self.close)
        buttons.addWidget(close)
        layout.addLayout(buttons)

    def prepare(self) -> None:
        """The services of the settings file and the URLs read lately, the last one chosen."""
        text = self.url.currentText()
        recent = [u for u in (QgsSettings().value(RECENT_KEY, []) or []) if isinstance(u, str)]
        self.url.blockSignals(True)
        self.url.clear()
        for s in self.context.config.gp_services:
            self.url.addItem(s.label if s.label != s.url else s.url, s.url)
            self.url.setItemData(self.url.count() - 1, s.url, Qt.ItemDataRole.ToolTipRole)
        known = {s.url for s in self.context.config.gp_services}
        for u in recent:
            if u not in known:
                self.url.addItem(u, u)
        self.url.setEditText(text or (self.url.itemText(0) if self.url.count() else ""))
        self.url.blockSignals(False)
        self._url_changed(self.url.currentText())

    def chosen_url(self) -> str:
        text = self.url.currentText().strip()
        index = self.url.findText(text)
        return (self.url.itemData(index) if index >= 0 else None) or text

    def _url_changed(self, _text: str) -> None:
        url = self.chosen_url()
        self.auth.setConfigId(settings.auth_for(url) or "" if safe_url(url) else "")

    def _keep(self, url: str) -> None:
        """Remembers the URL and the sign-in chosen for its service."""
        root = service_root(url)
        chosen = self.auth.configId() or None
        mapping = settings.auth_map()
        if chosen and mapping.get(root) != chosen:
            settings.set_auth_map({**mapping, root: chosen})
        recent = [u for u in (QgsSettings().value(RECENT_KEY, []) or []) if isinstance(u, str) and u != url]
        QgsSettings().setValue(RECENT_KEY, [url, *recent][:MAX_RECENT])

    def _token_for(self, url: str) -> str:
        root = service_root(url)
        return next((s.token for s in self.context.config.gp_services if s.token and service_root(s.url) == root), "")

    def busy(self, on: bool, text: str = "") -> None:
        self.load_button.setEnabled(not on)
        self.open_button.setEnabled(not on and self.tasks.currentRow() >= 0)
        self.status.setText(text)

    def _run(self, description: str, read: Callable[[GpClient], object], done: Callable[[object], None], token: str) -> None:
        if self.task is not None:
            self.task.cancel()

        def work(task):
            client = GpClient(gp_io.http, token or None, cancelled=task.isCanceled)
            try:
                return True, read(client)
            except GpError as e:
                return False, str(e)

        def finished(result) -> None:
            self.task = None
            ok, value = result
            if ok:
                done(value)
            else:
                self.busy(False, _tr("読み込み失敗: {e}").format(e=_tr(value)))

        def failed(message: str) -> None:
            self.task = None
            self.busy(False, _tr("読み込み失敗: {e}").format(e=_tr(message)))

        self.busy(True, _tr("読み込み中…"))
        self.task = run_task(description, work, finished, failed)

    def load(self) -> None:
        url = self.chosen_url()
        if not safe_url(url):
            self.status.setText(_tr("http(s) の URL を入力"))
            return
        self._keep(url)
        token = self._token_for(url)
        self.tasks.clear()
        self.service = None
        self._run(_tr("サーバ処理の読み込み"), lambda client: client.read(url), lambda value: self._loaded(value, token), token)

    def _loaded(self, value, token: str) -> None:
        if isinstance(value, GpTask):
            self.busy(False, _tr("「{title}」を開きます").format(title=value.title))
            self._open(value, token)
            return
        self.service, self.token = value, token
        for task_id, title, url in value.tasks:
            item = QListWidgetItem(title if title == task_id else f"{title}（{task_id}）")
            item.setData(Qt.ItemDataRole.UserRole, url)
            item.setToolTip(url)
            self.tasks.addItem(item)
        if value.tasks:
            self.tasks.setCurrentRow(0)
        kind = _tr("ジオプロセシング") if value.kind == "esri" else "OGC API – Processes"
        self.busy(False, _tr("{title}（{kind}）: {n} 件").format(title=value.title, kind=kind, n=len(value.tasks)) if value.tasks
                  else _tr("{title}: 処理なし").format(title=value.title))

    def open_selected(self) -> None:
        item = self.tasks.currentItem()
        if item is None or self.service is None:
            return
        url, kind, token = item.data(Qt.ItemDataRole.UserRole), self.service.kind, self.token
        self._run(_tr("サーバ処理の読み込み"), lambda client: client.read_task(kind, url), lambda task: self._opened(task, token), token)

    def _opened(self, task: GpTask, token: str) -> None:
        self.busy(False, _tr("「{title}」を開きます").format(title=task.title))
        self._open(task, token)

    def _open(self, task: GpTask, token: str) -> None:
        self.open_algorithm(gp_algorithm.register(task, token))

    def closeEvent(self, event) -> None:  # noqa: N802 (Qt's name)
        if self.task is not None:
            self.task.cancel()
            self.task = None
        super().closeEvent(event)
