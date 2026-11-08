"""
The 分担 line of the observation dock, under the image: who holds the image
shown (core/obs/assignments.py), and a button to take it (「この画像を担当」),
or, when it is yours, to finish it or give it back. An image someone else
holds says so in orange, so that nobody records it twice.

The bar keeps the assignments read (the 「分担と引き継ぎ」 dialog shares
them) and reads them again when they are a minute old and another image is shown.
"""
from __future__ import annotations

import time
from typing import Callable, Optional

from qgis.PyQt.QtCore import pyqtSignal
from qgis.PyQt.QtWidgets import QHBoxLayout, QLabel, QMessageBox, QSizePolicy, QToolButton, QWidget

from ..core import edit_stamp
from ..core.obs.assignments import DONE, Assignment, holder_of, release, same_person, take, update, with_status
from ..qgis_io.tasks import run_task

#: Seconds the assignments read stay fresh.
FRESH_S = 60
#: Days the 完了 rows are read back.
DONE_DAYS = 7


class AssignmentBar(QWidget):
    """「分担: …」 and its buttons (hidden without `assignments` in the settings file)."""

    #: The assignments were read again or changed.
    changed = pyqtSignal()

    def __init__(self, dock, parent=None):
        super().__init__(parent)
        self.dock = dock
        self.items: list[Assignment] = []
        self.read_at = 0.0
        self.error = ""
        self._task = None
        self._again = False
        self._writing = None
        #: The image being taken because 「クリック記録」 started on it (claim()).
        self.claiming = None
        self.image = None
        row = QHBoxLayout(self)
        row.setContentsMargins(0, 0, 0, 0)
        self.label = QLabel()
        self.label.setWordWrap(True)
        self.label.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)
        row.addWidget(self.label, 1)
        self.take_button = self._button(self.tr("この画像を担当"), self.tr("この画像を自分の担当に（ほかの人が担当中なら不可）"), self.take)
        self.done_button = self._button(self.tr("完了"), self.tr("この画像の担当を完了に"), lambda: self.set_status(DONE))
        self.release_button = self._button(self.tr("手放す"), self.tr("この画像の担当をやめる（ほかの人が担当できる）"), self.give_back)
        self.list_button = self._button(self.tr("分担…"), self.tr("全員の分担と引き継ぎメモ"), lambda: self.dock.open_handover(0))
        self.setVisible(False)

    def _button(self, text: str, tip: str, slot: Callable) -> QToolButton:
        b = QToolButton()
        b.setText(text)
        b.setToolTip(tip)
        b.clicked.connect(lambda _on=False: slot())
        self.layout().addWidget(b)
        return b

    # ---- the settings, the user ----------------------------------------------------

    @property
    def source(self):
        return self.dock.context.config.assignments

    def token(self) -> Optional[str]:
        s = self.source
        return self.dock.context.token_for(s.url, s.token) if s else None

    @staticmethod
    def user() -> str:
        return edit_stamp.user_name()

    def reset(self) -> None:
        """The settings changed: forget what was read."""
        self.items, self.read_at, self.error = [], 0.0, ""
        self.setVisible(self.source is not None)
        if self.source is not None and self.dock.isVisible():
            self.reload()
        self.render()

    # ---- reading ------------------------------------------------------------

    def reload(self, then: Optional[Callable[[], None]] = None) -> None:
        source = self.source
        if source is None:
            return
        if self._task is not None:
            # Read again once the running task ends (it may have read before a change).
            self._again = True
            return
        self._again = False
        from ..core.obs.assignments import load_assignments

        client, token = self.dock.context.esri(), self.token()

        def done(items) -> None:
            self._task = None
            if source is not self.source:
                return
            if self._again:
                self.reload(then)
                return
            self.items, self.read_at, self.error = items, time.monotonic(), ""
            self.render()
            self.changed.emit()
            if then:
                then()

        def failed(message: str) -> None:
            self._task = None
            self.error = message
            self.render()
            if self._again:
                self.reload(then)

        # The rows done long ago are left: they only grow (a week back, as the dialog's 「完了も表示」 shows).
        since = time.time() * 1000 - DONE_DAYS * 86_400_000
        self._task = run_task(self.tr("分担の読み込み"), lambda _t: load_assignments(client, source, token, done_since=since), done, failed)

    def show_image(self, image) -> None:
        """The dock shows another image (or the same again)."""
        self.image = image
        if self.source is not None and self.dock.isVisible() and time.monotonic() - self.read_at > FRESH_S:
            self.reload()
        self.render()

    def needs_claim(self, image) -> bool:
        """
        Whether starting 「クリック記録」 on `image` has to take it first: the
        table is set (and `claimOnRecord` not false) and, as last read, the
        image is not one's own.
        """
        source = self.source
        if source is None or image is None or not getattr(source, "claim_on_record", True):
            return False
        x = holder_of(self.items, image.id)
        return x is None or not same_person(x.assignee, self.user())

    def claim(self, image, done: Callable[[Assignment], None], failed: Callable[[str], None]) -> None:
        """
        Takes `image` for 「クリック記録」, checking again in the table (take():
        before and after adding). `failed`: someone else holds it, or the
        table could not be read or written (offline): recording is refused,
        as nobody can tell whether the work would be double.
        """
        source, user = self.source, self.user()
        if source is None or image is None:
            return
        if self.claiming is not None:
            return
        if not user:
            failed(self.tr("ユーザー名なし（環境変数 USERNAME か editTracking.user）: 分担を取得できない"))
            return
        client, token = self.dock.context.esri(), self.token()
        now = int(time.time() * 1000)
        self.claiming = image

        def work(_task):
            return take(client, source, image.id, user, now, image.name, image.time, token=token)

        def taken(x: Assignment) -> None:
            self.claiming = None
            if source is not self.source:
                failed(self.tr("設定が変わったため中止"))
                return
            # Known as one's own at once (the reading below may still be on its way).
            self.items = [x, *(i for i in self.items if i.oid is None or i.oid != x.oid)]
            self.render()
            self.changed.emit()
            self.read_at = 0.0
            self.reload()
            done(x)

        def refused(message: str) -> None:
            self.claiming = None
            self.read_at = 0.0
            self.reload()
            failed(message)

        run_task(self.tr("分担の取得"), work, taken, refused)
        self.render()

    def holder(self) -> Optional[Assignment]:
        return holder_of(self.items, self.image.id) if self.image else None

    def render(self) -> None:
        if self.source is None:
            self.setVisible(False)
            return
        self.setVisible(True)
        image, x = self.image, self.holder()
        mine = x is not None and same_person(x.assignee, self.user())
        style = ""
        if image is None:
            text = self.tr("分担: 画像なし")
        elif self.error and not self.read_at:
            text = self.tr("分担: 読み込みの失敗（{m}）").format(m=self.error)
        elif x is None:
            done = [d for d in self.items if d.image_id == image.id and d.done]
            text = self.tr("分担: 担当者なし") + (self.tr("（{who} さんが完了済み）").format(who="・".join(d.assignee for d in done)) if done else "")
        elif mine:
            text = self.tr("分担: 自分が担当中（{s}）").format(s=x.status)
            style = "color: #2e7d32;"
        else:
            text = self.tr("分担: {who} さんが担当中（{s}）。重複作業に注意").format(who=x.assignee or self.tr("（名前なし）"), s=x.status)
            style = "color: #e65100; font-weight: bold;"
        self.label.setText(text)
        self.label.setStyleSheet(style)
        busy = self._writing is not None or self.claiming is not None
        self.take_button.setVisible(image is not None and x is None)
        self.done_button.setVisible(mine)
        self.release_button.setVisible(mine)
        for b in (self.take_button, self.done_button, self.release_button):
            b.setEnabled(not busy)

    # ---- changing ------------------------------------------------------------

    def _run(self, what: str, work, done_text: str) -> None:
        if self._writing is not None:
            return

        def done(_result) -> None:
            self._writing = None
            self.dock.status.setText(done_text)
            self.read_at = 0.0
            self.reload()

        def failed(message: str) -> None:
            self._writing = None
            self.dock.status.setText(message)
            self.read_at = 0.0
            self.reload()

        self._writing = run_task(what, work, done, failed)
        self.render()

    def take(self, image=None) -> None:
        """Takes the image shown (or `image`) for oneself; refused when someone else holds it."""
        image = image or self.image
        source, user = self.source, self.user()
        if source is None or image is None:
            return
        if not user:
            self.dock.status.setText(self.tr("ユーザー名なし（環境変数 USERNAME か editTracking.user）: 分担は取得不可"))
            return
        client, token = self.dock.context.esri(), self.token()
        now = int(time.time() * 1000)

        def work(_task):
            return take(client, source, image.id, user, now, image.name, image.time, token=token)

        self._run(self.tr("分担の取得"), work, self.tr("{name} を担当に").format(name=image.name or image.id))

    def set_status(self, status: str, x: Optional[Assignment] = None) -> None:
        x = x or self.holder()
        source = self.source
        if source is None or x is None:
            return
        client, token = self.dock.context.esri(), self.token()
        new = with_status(x, status, int(time.time() * 1000))
        self._run(self.tr("分担の更新"), lambda _t: update(client, source, new, x, token),
                  self.tr("{name}: {s}").format(name=x.label, s=status))

    def give_back(self, x: Optional[Assignment] = None, confirm: bool = True) -> None:
        x = x or self.holder()
        source = self.source
        if source is None or x is None:
            return
        if confirm and QMessageBox.question(self, self.tr("分担を手放す"), self.tr("{name} の担当をやめます（記録は残る）。").format(name=x.label)) \
                != QMessageBox.StandardButton.Yes:
            return
        client, token = self.dock.context.esri(), self.token()
        self._run(self.tr("分担の解除"), lambda _t: release(client, source, x, token), self.tr("{name} の担当を解除").format(name=x.label))
