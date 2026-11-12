"""
監査ログ (core/audit.py) on the QGIS side: the log started with the plugin
and told the settings file's `auditLog`, the session's start and end and
each settings file read recorded, a message when the folder cannot be
written to (once per run of failures), and 「監査ログ…」 (plugin menu): what
is recorded where, what was sent, 「今すぐ送信」 and 「送信先を点検」.

Errors (kind `error`, 2026-11-11): the plugin's warnings and errors in
QGIS's log (its tags), Python errors whose traceback goes through the
plugin's files (QGIS logs them as 「Python error」), warnings and errors
shown in the main window's message bar (QGIS's and other plugins' too: what
the person saw), and the settings file's problems. Each is one queue put on
the main thread: what the message holds is worked out by the log's writer.

Nothing here writes or sends: the log's own threads do. The screen only
reads the log's status (a timer every 30 s) and puts lines in its queue.
"""
from __future__ import annotations

import os
import re
import time
from datetime import datetime
from typing import Optional

from qgis.core import Qgis, QgsApplication, QgsMessageLog
from qgis.gui import QgsMessageBarItem
from qgis.PyQt import sip
from qgis.PyQt.QtCore import QCoreApplication, QObject, QTimer, QUrl
from qgis.PyQt.QtGui import QDesktopServices
from qgis.PyQt.QtWidgets import QDialog, QDialogButtonBox, QHBoxLayout, QLabel, QPlainTextEdit, QPushButton, QVBoxLayout

from ..core import audit
from ..qgis_io.tasks import run_task
from .docks import plain_label

LOG_TAG = "MIKAGE"
HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def tr(text: str) -> str:
    return QCoreApplication.translate("SatelliteObservation", text)


def default_folder() -> str:
    """The day's files' folder when the settings file names none: the profile's mikage/audit."""
    return os.path.join(QgsApplication.qgisSettingsDirPath(), "mikage", "audit")


def plugin_version() -> str:
    try:
        with open(os.path.join(HERE, "metadata.txt"), encoding="utf-8") as f:
            m = re.search(r"^version\s*=\s*(.+)$", f.read(), re.MULTILINE)
    except OSError:
        return ""
    return m.group(1).strip() if m else ""


#: The levels recorded as errors (Info and Success are not).
ERROR_LEVELS = {Qgis.MessageLevel.Warning: "warning", Qgis.MessageLevel.Critical: "critical"}


def plugin_tags() -> set:
    """The log's tags that are the plugin's: its own and the docks' (as they are shown, translated or not)."""
    names = (LOG_TAG, "撮像計画", "観測記録")
    return set(names) | {QCoreApplication.translate("SatelliteObservation", n) for n in names}


def _size(n: int) -> str:
    return f"{n} B" if n < 1024 else f"{n / 1024:.1f} KB" if n < 1024 * 1024 else f"{n / 1024 / 1024:.1f} MB"


def _when(t: Optional[float]) -> str:
    return datetime.fromtimestamp(t).strftime("%Y-%m-%d %H:%M:%S") if t else ""


class AuditKeeper(QObject):
    """The audit log of this QGIS run (one per plugin load)."""

    def __init__(self, iface, context, parent: Optional[QObject] = None):
        super().__init__(parent)
        self.iface = iface
        self.context = context
        self.log = audit.AuditLog(default_folder())
        audit.set_log(self.log)
        self.log.start()
        audit.record("session", "start", detail={"plugin": plugin_version(), "qgis": Qgis.version()})
        # First of all who hear it (after the context's own rules): what is recorded and where.
        context.configChanged.connect(self._config_read)
        # Errors: the log (any thread's message reaches this slot on the main thread, queued) and the message bar.
        self._tags = plugin_tags()
        self._bar = iface.messageBar()
        QgsApplication.messageLog().messageReceived[str, str, Qgis.MessageLevel].connect(self._logged)
        self._bar.widgetAdded.connect(self._shown)
        self._told: dict[str, str] = {}
        self.timer = QTimer(self)
        self.timer.setInterval(30_000)
        self.timer.timeout.connect(self.check)
        self.timer.start()
        self.dialog: Optional[AuditDialog] = None

    def _config_read(self) -> None:
        c = self.context
        if c.loading:
            return
        self.log.configure(c.config.audit_log)
        source = c.source or ""
        detail = {"bundled": bool(c.bundled), "locked": bool(c.locked), "problems": len(c.problems)}
        text = getattr(c, "config_text", None)
        local = bool(source) and not re.match(r"^https?://", source, re.IGNORECASE)
        result = "ok" if source and (local or text is not None) else ("cached" if source else "default")
        audit.record("config", "read", source, result=result, detail=detail,
                     file=source if local else None, text=text if not local else None)
        if c.problems:
            audit.error("config", source, tr("設定ファイルの問題 {n} 件").format(n=len(c.problems)), detail={"problems": list(c.problems)[:50]})

    def _logged(self, message: str, tag: str, level) -> None:
        """A message of QGIS's log: the plugin's warnings and errors, and Python errors in the plugin's code."""
        name = ERROR_LEVELS.get(level)
        if name is None:
            return
        if tag in self._tags:
            audit.error("log", tag, message, detail={"level": name})
        elif audit.TRACEBACK in message and HERE in message:
            audit.error("python", tag, message, detail={"level": name})

    def _shown(self, widget) -> None:
        """A message shown in the main window's message bar: its warnings and errors."""
        try:
            item = sip.cast(widget, QgsMessageBarItem)
            name = ERROR_LEVELS.get(item.level())
            # QGIS's own 「Python error」 item: its traceback is in the log (above).
            if name is None or item.property("Error") is not None:
                return
            audit.error("message", item.title(), item.text(), markup=True, detail={"level": name})
        except (TypeError, RuntimeError, AttributeError):
            pass

    def check(self) -> None:
        """A message when sending or writing starts to fail (once per run of failures; the details in the log panel)."""
        s = self.log.status
        for key, error, title, text in (
            ("send", s.send_error, tr("監査ログ"), tr("送信先フォルダに送れません（{e}）。この PC に残し、送れるようになったら送ります。")),
            ("write", s.write_error, tr("監査ログ"), tr("この PC のログに書けません（{e}）。書けるようになるまでメモリに残します。")),
        ):
            if not error:
                if self._told.pop(key, None) is not None:
                    QgsMessageLog.logMessage(tr("監査ログ: {k}が回復").format(k=tr("送信") if key == "send" else tr("書き込み")), LOG_TAG,
                                             Qgis.MessageLevel.Info, notifyUser=False)
                continue
            if self._told.get(key) == error:
                continue
            first = key not in self._told
            self._told[key] = error
            QgsMessageLog.logMessage(f"監査ログ: {error}", LOG_TAG, Qgis.MessageLevel.Warning, notifyUser=False)
            if first:
                self.iface.messageBar().pushMessage(title, text.format(e=error), Qgis.MessageLevel.Warning, 15)

    def open_dialog(self) -> "AuditDialog":
        if self.dialog is None:
            self.dialog = AuditDialog(self, self.iface.mainWindow())
        self.dialog.render()
        self.dialog.show()
        self.dialog.raise_()
        return self.dialog

    def unload(self) -> None:
        self.timer.stop()
        for signal, slot in ((self.context.configChanged, self._config_read),
                             (QgsApplication.messageLog().messageReceived[str, str, Qgis.MessageLevel], self._logged),
                             (self._bar.widgetAdded, self._shown)):
            try:
                signal.disconnect(slot)
            except (TypeError, RuntimeError):
                pass
        if self.dialog is not None:
            self.dialog.close()
            self.dialog.deleteLater()
            self.dialog = None
        audit.record("session", "end")
        audit.set_log(None)
        # The last lines written and sent, waiting a second at most (a folder that does not answer: sent next time).
        self.log.stop()


class AuditDialog(QDialog):
    """「監査ログ…」: what is recorded where, what was sent."""

    def __init__(self, keeper: AuditKeeper, parent=None):
        super().__init__(parent)
        self.keeper = keeper
        self._task = None
        self.setWindowTitle(tr("監査ログ"))
        self.resize(640, 460)
        layout = QVBoxLayout(self)
        intro = QLabel(tr("MIKAGE での操作（Esri への書き込み・画像を開く・ファイル出力・コピーなど）とエラーを 1 行ずつ記録し、"
                          "設定ファイルの送信先フォルダに一定の間隔で送ります。"))
        intro.setWordWrap(True)
        layout.addWidget(intro)
        self.summary = plain_label(QLabel())
        self.summary.setWordWrap(True)
        layout.addWidget(self.summary)
        self.report = QPlainTextEdit()
        self.report.setReadOnly(True)
        self.report.setPlaceholderText(tr("「送信先を点検」で、送信先フォルダのログが欠けたり書き換えられたりしていないかを確かめます。"))
        layout.addWidget(self.report, 1)
        row = QHBoxLayout()
        self.send_button = QPushButton(tr("今すぐ送信"))
        self.send_button.clicked.connect(lambda _on=False: self.send())
        row.addWidget(self.send_button)
        self.check_button = QPushButton(tr("送信先を点検"))
        self.check_button.setToolTip(tr("送信先フォルダのログの行の番号と前の行のハッシュの連なりを確かめる（読むだけ）"))
        self.check_button.clicked.connect(lambda _on=False: self.check_folder())
        row.addWidget(self.check_button)
        self.local_button = QPushButton(tr("この PC のフォルダ"))
        self.local_button.clicked.connect(lambda _on=False: self._open(self.keeper.log.folder))
        row.addWidget(self.local_button)
        row.addStretch(1)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        buttons.rejected.connect(self.reject)
        row.addWidget(buttons)
        layout.addLayout(row)
        self._later = QTimer(self)
        self._later.setSingleShot(True)
        self._later.timeout.connect(self.render)

    def target(self) -> str:
        log = self.keeper.log
        return audit.folder_of(log.settings.folder, time.time(), log.user(), log.computer())

    def render(self) -> None:
        log = self.keeper.log
        s, st = log.settings, log.status
        lines = []
        if not s.enabled:
            lines.append(tr("記録: しない（設定ファイルの auditLog が false）"))
        else:
            lines.append(tr("記録: する（{k}）").format(k="・".join(audit.KINDS[k] for k in s.events)))
        lines.append(tr("この PC の保存先: {f}").format(f=log.folder))
        target = self.target()
        if target:
            lines.append(tr("送信先: {f}（{m:g} 分ごと・{fmt}・{enc}）").format(f=target, m=s.send_minutes, fmt=s.format, enc=s.file_encoding))
            lines.append(tr("最後の送信: {w}").format(w=f"{_when(st.last_sent)}（{_size(st.sent_bytes)}）" if st.last_sent else tr("まだ")))
            lines.append(tr("未送信: {n}").format(n=_size(log.unsent())))
            if st.send_error:
                lines.append(tr("送信のエラー: {e}（{t} から）").format(e=st.send_error, t=_when(st.failing_since)))
        else:
            lines.append(tr("送信先: なし（この PC だけに残す。auditLog.folder で指定）"))
        if st.write_error:
            lines.append(tr("書き込みのエラー: {e}").format(e=st.write_error))
        if st.lost:
            lines.append(tr("書けずに失われた行: {n}").format(n=st.lost))
        self.summary.setText("\n".join(lines))
        self.send_button.setEnabled(bool(target) and s.enabled)
        self.check_button.setEnabled(bool(target) and self._task is None)

    def send(self) -> None:
        """Writes what is queued and sends it (in the log's threads); the status read again a little later."""
        self.keeper.log.send_soon()
        self.summary.setText(self.summary.text() + "\n" + tr("送信中…"))
        self._later.start(1500)

    def check_folder(self) -> None:
        """Checks the chains of the logs in the folder sent to (in a task: the folder may be far)."""
        target = self.target()
        if not target or self._task is not None:
            return
        self.check_button.setEnabled(False)
        self.report.setPlainText(tr("点検中…"))

        def done(value) -> None:
            self._task = None
            self.report.setPlainText(value[0])
            self.render()

        def failed(message: str) -> None:
            self._task = None
            self.report.setPlainText(tr("点検できず: {m}").format(m=message))
            self.render()

        self._task = run_task(tr("監査ログの点検"), lambda task: audit.check_text(target), done, failed)

    def _open(self, folder: str) -> None:
        if folder and os.path.isdir(folder):
            QDesktopServices.openUrl(QUrl.fromLocalFile(folder))

    def closeEvent(self, event) -> None:  # noqa: N802 (Qt's name)
        self._later.stop()
        super().closeEvent(event)
