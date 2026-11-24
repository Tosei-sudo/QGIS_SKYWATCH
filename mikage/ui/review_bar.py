"""
画像の評価 under the observation dock's 分担 line (core/image_reviews.py): the
image's ratings in short (「評価: 使える 2・使えない 1」) and 「評価…」, the
window of its ratings (ui/image_review_dialog.py). Shown when the settings
file has `imageReviews` and an image is chosen, with or without 分担.
分担's 完了 opens the window when the image has no rating yet (「あとで」
closes it).
"""
from __future__ import annotations

import time
from typing import Callable, Optional

from qgis.PyQt.QtCore import pyqtSignal
from qgis.PyQt.QtWidgets import QHBoxLayout, QLabel, QToolButton, QWidget

from ..core.image_reviews import BAD, COLORS, GOOD, PART, ImageReview, counts_of, lines_of, load_reviews, summary_text
from ..qgis_io.reviews_io import footprint_of
from ..qgis_io.tasks import run_task
from .docks import plain_label, plain_tip

#: Seconds an image's ratings read stay fresh.
FRESH_S = 60


class ReviewBar(QWidget):
    """「評価: …」 and 「評価…」 (hidden without `imageReviews`)."""

    #: An image's ratings saved here: its id (the catalog's 評価 column reads it again).
    written = pyqtSignal(str)

    def __init__(self, dock, parent=None):
        super().__init__(parent)
        self.dock = dock
        self.image = None
        #: The rows of each image read, and when.
        self.items: dict[str, list[ImageReview]] = {}
        self.read_at: dict[str, float] = {}
        self.error = ""
        self._task = None
        #: Asked while a reading ran: (image id, then), read after it.
        self._again: Optional[tuple] = None
        self.dialog = None
        row = QHBoxLayout(self)
        row.setContentsMargins(0, 0, 0, 0)
        self.label = plain_label(QLabel())
        row.addWidget(self.label)
        self.button = QToolButton()
        self.button.setText(self.tr("評価…"))
        self.button.setToolTip(self.tr("この画像が判読に使えるかを、地名ごとに付ける（画像の評価）"))
        self.button.clicked.connect(lambda _on=False: self.open())
        row.addWidget(self.button)
        self.setVisible(False)

    @property
    def source(self):
        return self.dock.context.config.image_reviews

    def reset(self) -> None:
        """The settings changed: forget what was read."""
        self.items, self.read_at, self.error = {}, {}, ""
        self.show_image(self.image)

    def rows(self, image_id: str) -> Optional[list[ImageReview]]:
        """An image's rows as read (None: not read yet)."""
        return self.items.get(image_id) if image_id in self.read_at else None

    # ---- reading ------------------------------------------------------------------------------------

    def show_image(self, image) -> None:
        """The dock shows another image (or the same again): its rows read again when older than FRESH_S."""
        self.image = image
        if self.source is not None and image is not None and self.dock.isVisible() and \
                time.monotonic() - self.read_at.get(image.id, -1e9) > FRESH_S:
            self.reload(image.id)
        self.render()

    def reload(self, image_id: str, then: Optional[Callable[[], None]] = None) -> None:
        source = self.source
        if source is None or not image_id:
            return
        if self._task is not None:
            self._again = (image_id, then)
            return
        client = self.dock.context.esri()
        token = self.dock.context.token_for(source.url, source.token)

        def after() -> None:
            again, self._again = self._again, None
            if again is not None:
                self.reload(*again)

        def done(found) -> None:
            self._task = None
            if source is self.source:
                self.items[image_id] = found.get(image_id, [])
                self.read_at[image_id] = time.monotonic()
                self.error = ""
                self.render()
                if then:
                    then()
            after()

        def failed(message: str) -> None:
            self._task = None
            self.error = message
            self.render()
            after()

        self._task = run_task(self.tr("画像の評価の取得"), lambda _t: load_reviews(client, source, [image_id], token), done, failed,
                              hidden=True)

    def render(self) -> None:
        if self.source is None or self.image is None:
            self.setVisible(False)
            return
        self.setVisible(True)
        rows = self.rows(self.image.id)
        if rows is None:
            text, tip, style = (self.tr("評価: 取得に失敗") if self.error else self.tr("評価: …")), self.error, ""
        else:
            text, tip = summary_text(rows), "\n".join(lines_of(rows))
            counts = counts_of(rows)
            worst = BAD if BAD in counts else PART if PART in counts else GOOD if counts else ""
            style = f"color: {COLORS[worst]};" if worst else "color: #666;"
        self.label.setText(text)
        self.label.setStyleSheet(style)
        self.label.setToolTip(plain_tip(tip))
        self.button.setEnabled(self.dialog is None)

    # ---- the window ----------------------------------------------------------------------------------

    def open(self, later: bool = False):
        """The window of the image's ratings; None when it cannot be opened (no table, no image, one open)."""
        image, book = self.image, getattr(self.dock, "book", None)
        if self.source is None or image is None or book is None or self.dialog is not None:
            return None
        from .image_review_dialog import ImageReviewDialog

        footprint = footprint_of(image.id, image.box, getattr(book, "listed", ()))
        points = [(o.lon, o.lat) for o in book.records if o.image_id == image.id and o.deleted_at is None]
        unseen = [([a.ring], a.unseen) for a in book.areas if a.image_id == image.id and a.unseen and a.deleted_at is None]
        reasons = list(getattr(book.vocabulary, "unseen_reasons", ()) or ())
        dialog = ImageReviewDialog(self.dock.context, image, footprint, self.dock.canvas, points, unseen, reasons, later, self.dock)
        dialog.saved.connect(self._saved)
        dialog.finished.connect(lambda _r: self._closed(dialog))
        self.dialog = dialog
        dialog.show()
        dialog.load()
        self.render()
        return dialog

    def _closed(self, dialog) -> None:
        if self.dialog is dialog:
            self.dialog = None
            dialog.deleteLater()
        self.render()

    def _saved(self, image_id: str, rows: list) -> None:
        self.items[image_id] = list(rows)
        self.read_at[image_id] = time.monotonic()
        self.render()
        self.dock.status.setText(self.tr("画像の評価を保存（{s}）").format(s=summary_text(rows)))
        self.written.emit(image_id)

    def after_done(self, image) -> None:
        """
        分担's 完了 of `image`: the window opened when the image has no rating
        yet (its rows read first when not read lately).
        """
        if self.source is None or image is None or self.image is None or image.id != self.image.id:
            return

        def check() -> None:
            rows = self.rows(image.id)
            if rows is not None and not rows and self.image is not None and self.image.id == image.id:
                self.open(later=True)

        if self.rows(image.id) is None or time.monotonic() - self.read_at.get(image.id, -1e9) > FRESH_S:
            self.reload(image.id, check)
        else:
            check()
