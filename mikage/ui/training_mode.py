"""
練習モード's screens (features/training.py starts and ends it): the band
across the top of the map, the mark at the right of the status bar, 【練習】
after the docks' titles and 【練習モード】 before QGIS's window title (a dock
in a window of its own says it too), the question before it starts, and the
mail shown instead of opening the mail program while practising.
"""
from __future__ import annotations

from datetime import datetime
from typing import Callable, Optional, Sequence

from qgis.PyQt.QtCore import QEvent, QObject, QPoint, Qt, QTimer
from qgis.PyQt.QtWidgets import (
    QApplication,
    QCheckBox,
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPlainTextEdit,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from .fit import fit_text

#: The band's colours: amber, as a warning that is not an error.
AMBER = "#d97706"
AMBER_BG = "#fef3c7"
AMBER_TEXT = "#78350f"
#: After each dock's title and before QGIS's window title while practising.
DOCK_MARK = "【練習】"
WINDOW_MARK = "【練習モード】 "
#: The mark's name (the tests and the help's pictures find it by it).
BAND_NAME = "MikageTrainingBand"
STATUS_NAME = "MikageTrainingStatus"


class TrainingBand(QFrame):
    """
    The band across the top of the map while practising, with the practice's
    buttons on its right. QGIS's message bar lies over the top of the map:
    while it shows a message (`cover`), the band is just below it.
    """

    def __init__(self, canvas: QWidget, buttons: Sequence[tuple[str, Callable[[], None]]], text: str,
                 cover: Optional[QWidget] = None):
        super().__init__(canvas)
        self.canvas = canvas
        self.cover = cover
        self._removed = False
        self.setObjectName(BAND_NAME)
        self.setStyleSheet(f"#{BAND_NAME} {{ background: {AMBER_BG}; border-bottom: 2px solid {AMBER}; }}"
                           f" #{BAND_NAME} QLabel {{ color: {AMBER_TEXT}; }}")
        row = QHBoxLayout(self)
        row.setContentsMargins(10, 4, 8, 4)
        mark = QLabel(self.tr("練習モード"))
        mark.setStyleSheet(f"background: {AMBER}; color: white; font-weight: bold; padding: 2px 8px; border-radius: 3px;")
        row.addWidget(mark)
        self.text = QLabel(text)
        self.text.setMinimumWidth(10)
        row.addWidget(self.text, 1)
        self.buttons: dict[str, QPushButton] = {}
        for label, run in buttons:
            b = QPushButton(label)
            b.setStyleSheet(f"QPushButton {{ background: white; border: 1px solid #d6b26b; border-radius: 3px; padding: 2px 10px; color: {AMBER_TEXT}; }}"
                            " QPushButton:hover { background: #fffbeb; }")
            b.clicked.connect(lambda _on=False, run=run: run())
            row.addWidget(b)
            self.buttons[label] = b
        for w in (canvas, cover):
            if w is not None:
                w.installEventFilter(self)
        self.place()

    def eventFilter(self, watched: QObject, event: QEvent) -> bool:  # noqa: N802 (Qt's name)
        if watched is self.canvas and event.type() == QEvent.Type.Resize:
            self.place()
        elif watched is self.cover and event.type() in (QEvent.Type.Show, QEvent.Type.Hide, QEvent.Type.Resize, QEvent.Type.Move):
            # (after the message bar has its size and place)
            QTimer.singleShot(0, self.place)
        return False

    def top(self) -> int:
        """Below the message bar while it shows a message over the map, else 0."""
        cover = self.cover
        try:
            if cover is None or not cover.isVisible():
                return 0
            bottom = self.canvas.mapFromGlobal(cover.mapToGlobal(QPoint(0, 0))).y() + cover.height()
        except RuntimeError:  # (QGIS closing)
            return 0
        return bottom if 0 < bottom < self.canvas.height() // 2 else 0

    def place(self) -> None:
        if self._removed:
            return
        self.setGeometry(0, self.top(), self.canvas.width(), self.sizeHint().height())
        self.raise_()
        self.show()

    def remove(self) -> None:
        self._removed = True
        for w in (self.canvas, self.cover):
            try:
                if w is not None:
                    w.removeEventFilter(self)
            except RuntimeError:
                pass
        self.hide()
        self.deleteLater()


def status_mark() -> QLabel:
    """The mark at the right of the status bar."""
    mark = QLabel(" 練習モード ")
    mark.setObjectName(STATUS_NAME)
    mark.setToolTip("練習用のデータで動いています。本番のサーバーには書き込みません")
    mark.setStyleSheet(f"background: {AMBER}; color: white; font-weight: bold; padding: 1px 6px; border-radius: 3px;")
    return mark


class TitleMarks(QObject):
    """【練習】 after the docks' titles and 【練習モード】 before the window's, kept while QGIS or a dock changes its title."""

    def __init__(self, window: QWidget, docks: Sequence[QWidget]):
        super().__init__(window)
        self.window = window
        self.docks = list(docks)
        for w in [window, *self.docks]:
            w.installEventFilter(self)
            self._mark(w)

    def _mark(self, w: QWidget) -> None:
        title = w.windowTitle()
        if w is self.window:
            if not title.startswith(WINDOW_MARK):
                w.setWindowTitle(WINDOW_MARK + title)
        elif not title.endswith(DOCK_MARK):
            w.setWindowTitle(title + DOCK_MARK)

    def eventFilter(self, watched: QObject, event: QEvent) -> bool:  # noqa: N802 (Qt's name)
        if event.type() == QEvent.Type.WindowTitleChange and isinstance(watched, QWidget):
            self._mark(watched)
        return False

    def remove(self) -> None:
        for w in [self.window, *self.docks]:
            try:
                w.removeEventFilter(self)
                title = w.windowTitle()
                if w is self.window and title.startswith(WINDOW_MARK):
                    w.setWindowTitle(title[len(WINDOW_MARK):])
                elif w is not self.window and title.endswith(DOCK_MARK):
                    w.setWindowTitle(title[:-len(DOCK_MARK)])
            except RuntimeError:  # (a dock deleted meanwhile)
                pass
        self.deleteLater()


def _bullets(lines: Sequence[str]) -> QLabel:
    text = QLabel("<ul style='margin-left:-20px'>" + "".join(f"<li style='margin-bottom:4px'>{s}</li>" for s in lines) + "</ul>")
    text.setTextFormat(Qt.TextFormat.RichText)
    text.setWordWrap(True)
    return text


class StartDialog(QDialog):
    """「練習モードを始める」: what happens, whether to make the sample again with today's dates, and to open チュートリアル after."""

    def __init__(self, parent=None, builtin: bool = True, created: Optional[int] = None, now: Optional[datetime] = None,
                 from_production: str = "", tutorial: bool = False, exercises: bool = False):
        super().__init__(parent)
        fit_text(self)
        self.setWindowTitle(self.tr("練習モードを始める"))
        box = QVBoxLayout(self)
        box.setContentsMargins(18, 16, 18, 14)
        title = QLabel(self.tr("<b>練習モードを始めます</b>"))
        font = title.font()
        font.setPointSizeF(font.pointSizeF() * 1.2)
        title.setFont(font)
        box.addWidget(title)
        data = (self.tr("練習用のデータ（東京湾・横須賀の見本）に切り替えます。本番のサーバーには書き込みません。") if builtin else
                self.tr("組織の練習用の設定ファイル（設定ファイルの training.config）に切り替えます。"))
        read = ([self.tr("{m}は、本番の設定ファイルのもの（組織の実際の分類や衛星）を読みます。読むだけで書き換えません。").format(m=from_production)]
                if from_production else [])
        box.addWidget(_bullets([
            data,
            *read,
            self.tr("地図は新しいプロジェクトになります。いまのプロジェクトに保存していない変更があれば、先に保存するか聞きます。"),
            self.tr("通報のメールはメールソフトを開かずに画面に出し、ファイルは練習用のフォルダーに出します。"),
            self.tr("担当は練習中だけ切り替えられます（終えると元に戻ります）。"),
            self.tr("「練習モードを終える」を押すか QGIS を閉じると、本番に戻ります。練習データは次の練習まで残ります。"),
        ]))
        #: Made again with today's dates (the sample's images are of the days before it was made).
        self.renew = QCheckBox()
        if builtin and created is not None:
            made = datetime.fromtimestamp(created / 1000)
            days = ((now or datetime.now()).date() - made.date()).days
            self.renew.setText(self.tr("練習データを今日の日付で作り直す（{d} に作ったもの。練習で記録したものは消えます）")
                               .format(d=made.strftime("%Y-%m-%d")))
            self.renew.setChecked(days >= 7)
            box.addWidget(self.renew)
        else:
            self.renew.hide()
        self.first = QLabel(self.tr("初めてなので、練習用の画像と背景地図を作ります（数秒〜十数秒）。")) if builtin and created is None else None
        if self.first is not None:
            box.addWidget(self.first)
        #: The courses offered once it has started (ticked by 「チュートリアル…」, and the first time); by 「判読の練習…」
        #: its exercises are, instead.
        self.tutorial = QCheckBox(self.tr("始めたら、チュートリアル（操作を 1 つずつ案内するコース）を選ぶ"))
        self.tutorial.setChecked(not exercises and (tutorial or (builtin and created is None)))
        box.addWidget(self.tutorial)
        if exercises:
            self.tutorial.hide()
            box.addWidget(QLabel(self.tr("始めたら、判読の練習の問題を選びます。")))
        buttons = QDialogButtonBox()
        self.ok = buttons.addButton(self.tr("始める"), QDialogButtonBox.ButtonRole.AcceptRole)
        self.ok.setDefault(True)
        buttons.addButton(self.tr("やめる"), QDialogButtonBox.ButtonRole.RejectRole)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        box.addWidget(buttons)
        self.setMinimumWidth(640)


class PracticeMail(QDialog):
    """A mail made while practising: shown here, the mail program not opened (nothing leaves the computer)."""

    def __init__(self, parent, to: Sequence[str], cc: Sequence[str], bcc: Sequence[str], subject: str, body: str):
        super().__init__(parent)
        fit_text(self)
        self.setWindowTitle(self.tr("通報のメール（練習モード）"))
        box = QVBoxLayout(self)
        note = QLabel(self.tr("練習モードなのでメールソフトは開きません。本番では、この内容のメールをメールソフトで作ります。"))
        note.setWordWrap(True)
        note.setStyleSheet(f"background: {AMBER_BG}; color: {AMBER_TEXT}; padding: 6px; border-radius: 3px;")
        box.addWidget(note)
        form = QFormLayout()
        for label, value in ((self.tr("宛先"), ", ".join(to)), (self.tr("CC"), ", ".join(cc)), (self.tr("BCC"), ", ".join(bcc)),
                             (self.tr("件名"), subject)):
            if not value and label != self.tr("宛先"):
                continue
            field = QLineEdit(value)
            field.setReadOnly(True)
            form.addRow(label, field)
        box.addLayout(form)
        self.body = QPlainTextEdit(body)
        self.body.setReadOnly(True)
        box.addWidget(self.body, 1)
        row = QHBoxLayout()
        copy = QPushButton(self.tr("本文をコピー"))
        copy.clicked.connect(lambda: QApplication.clipboard().setText(body))
        row.addWidget(copy)
        row.addStretch(1)
        close = QPushButton(self.tr("閉じる"))
        close.clicked.connect(self.accept)
        row.addWidget(close)
        box.addLayout(row)
        self.resize(640, 460)
