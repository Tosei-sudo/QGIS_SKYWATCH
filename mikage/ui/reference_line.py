"""
「参考情報 n 件」 beside 観測記録's 評価 line (core/reference_info.py): the
infos within `referenceInfo.days` of the image's time, placed in its
footprint or about a named area it shows (namedAreas, asked by the
footprint as 画像の評価 does). Pressed, the 「参考情報」 dock lists them.
Hidden without `referenceInfo` or an image.
"""
from __future__ import annotations

import time
from typing import Callable, Optional

from qgis.PyQt.QtWidgets import QToolButton

from ..core.esri import EsriError
from ..core.named_areas import settings_for
from ..core.reference_info import ReferenceInfo, local_text, near_image
from ..qgis_io.reviews_io import areas_over, footprint_of
from ..qgis_io.tasks import run_task
from .docks import plain_tip

#: Seconds an image's infos read stay fresh.
FRESH_S = 60


class ReferenceLine(QToolButton):
    """「参考情報 n 件」 (a flat button); `references()` gives the 「参考情報」 dock (its port)."""

    def __init__(self, dock, parent=None):
        super().__init__(parent)
        self.dock = dock
        self.image = None
        #: The infos of each image read, and when.
        self.items: dict[str, list[ReferenceInfo]] = {}
        self.read_at: dict[str, float] = {}
        self.error = ""
        self._task = None
        self._again: Optional[tuple] = None
        #: The 「参考情報」 dock (set by the plugin): where the infos are listed.
        self.references: Optional[Callable[[], object]] = None
        self.setAutoRaise(True)
        self.setStyleSheet("QToolButton { color: #1565c0; }")
        self.clicked.connect(lambda _on=False: self.open())
        self.setVisible(False)

    @property
    def settings(self):
        return self.dock.context.config.reference_info

    def reset(self) -> None:
        """The settings changed: forget what was read."""
        self.items, self.read_at, self.error = {}, {}, ""
        self.show_image(self.image)

    def infos(self, image_id: str) -> Optional[list[ReferenceInfo]]:
        """An image's infos as read (None: not read yet)."""
        return self.items.get(image_id) if image_id in self.read_at else None

    def show_image(self, image) -> None:
        """The dock shows another image (or the same again): its infos read again when older than FRESH_S."""
        self.image = image
        if self.settings is not None and image is not None and self.dock.isVisible() and \
                time.monotonic() - self.read_at.get(image.id, -1e9) > FRESH_S:
            self.reload(image)
        self.render()

    def reload(self, image, then: Optional[Callable[[], None]] = None) -> None:
        s = self.settings
        book = getattr(self.dock, "book", None)
        if s is None or image is None:
            return
        if self._task is not None:
            self._again = (image, then)
            return
        context = self.dock.context
        client = context.esri()
        token = context.token_for(s.layer.url, s.layer.token)
        named = settings_for(context.config)
        named_token = context.token_for(named.areas.url, named.areas.token) if named is not None else None
        footprint = footprint_of(image.id, image.box, getattr(book, "listed", ()) if book is not None else ())
        image_id, image_time = image.id, image.time

        def work(_task):
            area_ids: list[str] = []
            if named is not None and footprint:
                try:
                    found, _more = areas_over(client, named, footprint, named_token)
                    area_ids = [a.id for a, _rings in found if a.id]
                except EsriError:
                    area_ids = []  # the named areas not read: the infos in the footprint only
            return near_image(client, s, image_time, footprint, area_ids, token)

        def after() -> None:
            again, self._again = self._again, None
            if again is not None:
                self.reload(*again)

        def done(found) -> None:
            self._task = None
            if s is self.settings:
                self.items[image_id] = found
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

        self._task = run_task(self.tr("画像の参考情報の取得"), work, done, failed, hidden=True)

    def render(self) -> None:
        s = self.settings
        if s is None or self.image is None:
            self.setVisible(False)
            return
        self.setVisible(True)
        found = self.infos(self.image.id)
        if found is None:
            self.setText(self.tr("参考情報: 取得に失敗") if self.error else self.tr("参考情報: …"))
            self.setToolTip(plain_tip(self.error))
            self.setEnabled(False)
            return
        self.setText(self.tr("参考情報 {n} 件").format(n=len(found)))
        lines = [f"{local_text(x.time)}  {x.title}" + (f"（{x.source}）" if x.source else "") for x in found[:10]]
        if len(found) > 10:
            lines.append(self.tr("ほか {n} 件").format(n=len(found) - 10))
        head = self.tr("撮像日時の前後 {d} 日で、画像の範囲か、写っている地名の参考情報").format(d=f"{s.days:g}")
        self.setToolTip(plain_tip("\n".join([head, *lines])))
        self.setEnabled(bool(found))

    def open(self):
        """The image's infos listed in 「参考情報」; the dock, or None."""
        found = self.infos(self.image.id) if self.image is not None else None
        dock = self.references() if self.references else None
        if dock is None or not found:
            return None
        name = self.image.name or self.image.id
        dock.show_infos(found, self.tr("画像「{n}」の前後 {d} 日").format(n=name, d=f"{self.settings.days:g}"))
        return dock
