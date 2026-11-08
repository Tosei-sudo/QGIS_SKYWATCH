"""
一時的なオフライン作業 on screen (core/offline.py): a button in QGIS's
status bar that says when work is offline and how much waits to be sent, a
note in the message bar when a server stops answering, a check every
`offlineWork.probeSeconds` whether it answers again, and then the question:
send what waits (with how many of each), throw it away, or later.
"""
from __future__ import annotations

import time
from typing import Callable, Optional

from qgis.PyQt.QtCore import QCoreApplication, QObject, QTimer, pyqtSignal
from qgis.PyQt.QtWidgets import QAction, QMenu, QMessageBox, QToolButton, QWidget

from ..core.offline import KINDS, OfflineWork, UploadReport
from ..core.offline_upload import upload_all
from ..qgis_io import settings
from ..qgis_io.network import qgis_transport
from ..qgis_io.tasks import run_task


def tr(text: str) -> str:
    return QCoreApplication.translate("OfflineWork", text)


def _when(ms: Optional[int]) -> str:
    return time.strftime("%m/%d %H:%M", time.localtime(ms / 1000)) if ms else ""


def counts_text(work: OfflineWork) -> str:
    """What waits, a line per kind and store (「観測記録 3 件（共有）」)."""
    return "\n".join(f"・{kind} {n} 件（{store}）" for kind, store, n in work.summary())


#: Asks what to do with the work waiting: "upload", "discard" or "later" (replaced in the tests).
def ask_upload(parent: Optional[QWidget], work: OfflineWork, back: bool) -> str:
    box = QMessageBox(parent)
    box.setIcon(QMessageBox.Icon.Question)
    box.setWindowTitle(tr("オフライン中の作業"))
    head = (tr("サーバーとの接続が戻りました。") if back else tr("オフライン中に作業した内容が送信されずに残っています。"))
    box.setText(head + tr("オフライン中の作業をサーバーに送信しますか？"))
    box.setInformativeText(counts_text(work) + "\n\n" + tr(
        "送信: サーバーに保存します（ほかの人が先に更新していた観測記録は保存せず「未保存の記録」へ移します）。\n"
        "破棄: 送信せずに消します（元に戻せません）。\n後で: 残したまま、ステータスバーの「送信待ち」からいつでも送信できます。"))
    upload = box.addButton(tr("送信"), QMessageBox.ButtonRole.AcceptRole)
    discard = box.addButton(tr("破棄…"), QMessageBox.ButtonRole.DestructiveRole)
    box.addButton(tr("後で"), QMessageBox.ButtonRole.RejectRole)
    box.setDefaultButton(upload)
    box.exec()
    clicked = box.clickedButton()
    return "upload" if clicked is upload else "discard" if clicked is discard else "later"


def ask_discard(parent: Optional[QWidget], work: OfflineWork) -> bool:
    return QMessageBox.warning(
        parent, tr("オフライン中の作業の破棄"),
        tr("次の作業を送信せずに破棄します。元に戻せません。よろしいですか？") + "\n\n" + counts_text(work),
        QMessageBox.StandardButton.Discard | QMessageBox.StandardButton.Cancel,
        QMessageBox.StandardButton.Cancel) == QMessageBox.StandardButton.Discard


class OfflineController(QObject):
    """The status-bar button, the checks while offline and the question when a server is back."""

    #: Something changed (a server down or back, the work waiting); emitted on the main thread.
    changed = pyqtSignal()
    #: The work waiting was sent or thrown away: the docks read again.
    finished = pyqtSignal()

    def __init__(self, iface, context, reload_docks: Callable[[], None], parent: Optional[QObject] = None):
        super().__init__(parent)
        self.iface = iface
        self.context = context
        self.work: OfflineWork = context.offline
        self.reload_docks = reload_docks
        self._was_down = False
        self._probing = False
        self._uploading = False
        self._asked_at_start = False
        #: Whether the question is on screen (not asked twice).
        self._asking = False
        self.work.listeners.append(self._from_any_thread)
        self.changed.connect(self._update)
        self.finished.connect(self.reload_docks)
        self.button = QToolButton()
        self.button.setAutoRaise(True)
        self.button.setPopupMode(QToolButton.ToolButtonPopupMode.InstantPopup)
        self.button.setObjectName("SkywatchOfflineButton")
        self.menu = QMenu(self.button)
        self.act_check = self.menu.addAction(tr("今すぐ接続を確認"), self.probe)
        self.act_upload = self.menu.addAction(tr("オフライン中の作業を送信…"), lambda: self.ask(back=False))
        self.act_discard = self.menu.addAction(tr("オフライン中の作業を破棄…"), self.discard)
        self.menu.addSeparator()
        self.act_forced = QAction(tr("オフラインで作業する（手動）"), self.menu)
        self.act_forced.setCheckable(True)
        self.act_forced.toggled.connect(self._forced)
        self.menu.addAction(self.act_forced)
        self.act_cache = self.menu.addAction("", lambda: None)
        self.act_cache.setEnabled(False)
        self.button.setMenu(self.menu)
        self.menu.aboutToShow.connect(self._menu_shown)
        try:
            iface.mainWindow().statusBar().addPermanentWidget(self.button)
        except AttributeError:
            pass
        self.timer = QTimer(self)
        self.timer.timeout.connect(self.probe)
        context.configChanged.connect(self._config_changed)
        self._update()

    # ---- state ----------------------------------------------------------

    def _from_any_thread(self) -> None:
        try:
            self.changed.emit()  # queued to the main thread when emitted from a task
        except RuntimeError:
            pass

    def _config_changed(self) -> None:
        self.timer.setInterval(int(self.context.config.offline_work.probe_seconds * 1000))
        self._update()
        if not self._asked_at_start:
            # Work left from an earlier session: asked once, a little after the start.
            self._asked_at_start = True
            QTimer.singleShot(3000, self._ask_left_over)

    def _ask_left_over(self) -> None:
        if self.work.enabled and self.work.total() and not self.work.offline:
            self.ask(back=False)

    def _update(self) -> None:
        work = self.work
        down = work.offline
        total = work.total() if work.enabled else 0
        if down:
            self.button.setText(tr("⚠ オフライン作業中") + (tr("（送信待ち {n} 件）").format(n=total) if total else ""))
            self.button.setStyleSheet("QToolButton { color: #b45309; font-weight: bold; }")
            since = _when(work.since())
            self.button.setToolTip(tr("サーバーに接続できないため、最後に読めた内容で作業中{since}。\n変更は送信待ちとして保存し、"
                                      "接続が戻ったら送信するか確認します。\n{why}").format(
                since=tr("（{t} から）").format(t=since) if since else "", why="\n".join(work.reasons())))
        elif total:
            self.button.setText(tr("送信待ち {n} 件").format(n=total))
            self.button.setStyleSheet("QToolButton { color: #b45309; }")
            self.button.setToolTip(tr("オフライン中の作業がサーバーに送信されずに残っています（クリックで送信・破棄）"))
        else:
            self.button.setText("")
        self.button.setVisible(bool(work.enabled and (down or total)))
        if down and not work.forced and not self.timer.isActive():
            self.timer.start(int(self.context.config.offline_work.probe_seconds * 1000))
        elif not down:
            self.timer.stop()
        if down and not self._was_down:
            self.iface.messageBar().pushWarning(
                tr("オフライン作業"),
                tr("サーバーに接続できません。最後に読めた内容で作業を続けます（変更は送信待ちとして保存し、接続が戻ったら確認します）。")
                if not work.forced else tr("手動でオフラインに切り替えました。変更は送信待ちとして保存します。"))
        back = self._was_down and not down
        self._was_down = down
        if back:
            if total:
                self.ask(back=True)
            else:
                self.iface.messageBar().pushSuccess(tr("オフライン作業"), tr("サーバーとの接続が戻りました。"))
                self.finished.emit()

    def _menu_shown(self) -> None:
        total = self.work.total()
        self.act_upload.setEnabled(bool(total) and not self.work.offline and not self._uploading)
        self.act_discard.setEnabled(bool(total) and not self._uploading)
        self.act_check.setEnabled(self.work.offline and not self.work.forced)
        self.act_forced.blockSignals(True)
        self.act_forced.setChecked(self.work.forced)
        self.act_forced.blockSignals(False)
        n, size, at = self.work.cache_info()
        self.act_cache.setText(tr("控え: {n} 件・{mb:.0f} MB（最新 {at}）").format(n=n, mb=size / 1048576, at=_when(at) or "-"))

    def _forced(self, on: bool) -> None:
        self.work.set_forced(on)

    # ---- checking whether servers answer again --------------------------

    def probe(self) -> None:
        if self._probing or not self.work.probes():
            return
        self._probing = True
        send = qgis_transport(settings.auth_for)

        def done(_back) -> None:
            self._probing = False

        def failed(_message: str) -> None:
            self._probing = False

        run_task(tr("サーバーへの接続の確認"), lambda task: self.work.probe(send), done, failed, hidden=True)

    # ---- sending or throwing away ---------------------------------------

    def ask(self, back: bool) -> None:
        if self._asking or self._uploading or not self.work.total():
            return
        self._asking = True
        try:
            choice = ask_upload(self.iface.mainWindow(), self.work, back)
        finally:
            self._asking = False
        if choice == "upload":
            self.upload()
        elif choice == "discard":
            self.discard()

    def discard(self) -> None:
        if not self.work.total() or not ask_discard(self.iface.mainWindow(), self.work):
            return
        n = self.work.discard()
        self.iface.messageBar().pushInfo(tr("オフライン作業"), tr("オフライン中の作業 {n} 件を破棄しました。").format(n=n))
        self.finished.emit()

    def upload(self, then: Optional[Callable[[UploadReport], None]] = None) -> None:
        if self._uploading:
            return
        self._uploading = True
        context = self.context
        config = context.config

        def work(task) -> UploadReport:
            return upload_all(config, context.esri, context.token_for, settings.SettingsPrefs(), context.gpkg)

        def done(report: UploadReport) -> None:
            self._uploading = False
            bar = self.iface.messageBar()
            (bar.pushWarning if report.kept or report.drafts else bar.pushSuccess)(tr("オフライン作業の送信"), report.text())
            self._update()
            self.finished.emit()
            if then is not None:
                then(report)

        def failed(message: str) -> None:
            self._uploading = False
            self.iface.messageBar().pushWarning(tr("オフライン作業の送信"), tr("送信できず（{m}）。送信待ちのまま残しています。").format(m=message))
            self._update()

        run_task(tr("オフライン中の作業の送信"), work, done, failed)

    def unload(self) -> None:
        self.timer.stop()
        try:
            self.work.listeners.remove(self._from_any_thread)
        except ValueError:
            pass
        try:
            self.iface.mainWindow().statusBar().removeWidget(self.button)
        except (AttributeError, RuntimeError):
            pass
        self.button.deleteLater()


__all__ = ["OfflineController", "KINDS", "ask_upload", "ask_discard", "counts_text"]
