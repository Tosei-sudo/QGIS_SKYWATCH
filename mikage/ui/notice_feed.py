"""
新着画像 (tsudo, 2026-11-14): the new images the watches found as cards on a
large page in the place of the map (ui/central_views.py), for the daily
work of looking at what came in, one after another.

* One column, in the order they came (the newest at the bottom). A new
  image coming in does not move the page (tsudo: 「新しい画像が来てもスクロールはしないで」):
  「↓ 新着 n 件」 under it goes to them. Opened, the page starts at the
  oldest unread card.
* A card shows everything, as long as it gets (「略さずすべて」): the
  thumbnail (`imageCatalogs[].thumbnail`; the footprint's shape without
  one), the catalog's columns, the watch, when it was found, and 「写っている地物」:
  the features of the catalog's `footprintLayers` over the image, all of
  them, kept with the notice once read.
* 「開く」 opens the image, goes to the map and zooms to it (read);
  「地図で見る」 goes to its footprint; 「既読」「削除」 as in the 通知 dock,
  whose list this is (the same notices).

Only the newest PAGE cards are made at first (「さらに前の n 件」 above them);
thumbnails and features are fetched for the cards in view only.
"""
from __future__ import annotations

import html
import math
import re
from typing import Any, Callable, Optional

from qgis.core import QgsCoordinateReferenceSystem, QgsProject
from qgis.PyQt.QtCore import QPointF, QRectF, Qt, QTimer
from qgis.PyQt.QtGui import QColor, QFont, QPainter, QPainterPath, QPen, QPixmap, QTextDocument
from qgis.PyQt.QtWidgets import (
    QCheckBox, QComboBox, QFrame, QGridLayout, QHBoxLayout, QLabel, QPushButton, QScrollArea, QSizePolicy, QStyle,
    QVBoxLayout, QWidget,
)

from ..core.catalog.config import CatalogConfig
from ..core.catalog.query import time_text
from ..core.catalog.search import cell_text, list_columns, record_of
from ..core.catalog.thumbnail import (
    DEFAULT_HEIGHT, DEFAULT_WIDTH, Contents, contents_lines, contents_of, footprint_of, thumbnail_address,
)
from ..core.catalog.watch import WatchNotice
from ..qgis_io import settings
from ..core.catalog.paths import PathError
from ..qgis_io.catalog_io import RecordImages, close_layers, footprint, layers_extent, record_files
from ..qgis_io.thumbnails import CardWork, cached, fetch, prune

TITLE = "新着画像"
PLAN = "plan"
#: How many cards are made at first (and each time 「さらに前」 is pressed).
PAGE = 100
#: The card's width at most (on a wide screen the column stays readable, centred).
CARD_WIDTH = 1200
#: When the map area is narrow, the thumbnail is made smaller (not below this) so that the card fits without scrolling sideways.
MIN_PICTURE = 160
#: The room the card keeps for its attributes and buttons beside the thumbnail (and its margins).
TEXT_ROOM = 360
#: The room 「写っている地物」 needs as a column of its own (else under the attributes, tsudo 2026-11-14: サムネ、メタ、ファイル、地物).
FEATURES_ROOM = 200
#: The room 「画像ファイル」 needs as a column of its own (else above the features).
FILES_ROOM = 160
#: The card's frame around the grid: the unread mark, the spacing after it and the right margin.
CARD_FRAME = 21
#: Room between the columns.
COLUMN_GAP = 16
#: The attributes' column is as wide as its lines (each on one line), up to this.
ATTRIBUTES_WIDTH = 440
UNREAD = "#2563eb"


def _escape(text: str) -> str:
    return html.escape(text or "", quote=True).replace("\n", "<br>")


def _background(color: Optional[str]) -> Optional[QColor]:
    """A line's background as Qt reads it (None: none or not a colour)."""
    if not color:
        return None
    found = QColor(color.strip())
    return found if found.isValid() else None


def _line_html(text: str, background: Optional[QColor]) -> str:
    if background is None:
        return f"<tr><td>{_escape(text)}</td></tr>"
    # The text black or white, whichever reads on the colour (only Qt's own #rrggbb goes into the markup).
    light = 0.299 * background.red() + 0.587 * background.green() + 0.114 * background.blue() >= 140
    return f"<tr><td bgcolor='{background.name()}' style='color:{'#111111' if light else '#ffffff'}'>{_escape(text)}</td></tr>"


def _rows_html(rows: list[tuple[str, str]]) -> str:
    """Label / value rows as a table (every value escaped: other people's text is never markup)."""
    cells = "".join(
        f"<tr><td style='color:#64748b; padding-right:12px; white-space:nowrap; vertical-align:top'>{_escape(label)}</td>"
        f"<td style='vertical-align:top'>{_escape(value)}</td></tr>"
        for label, value in rows)
    return f"<table cellspacing='0' cellpadding='1'>{cells}</table>"


def _sections_html(sections: list[tuple[str, list[Any]]]) -> str:
    """
    Lists one under another, each under its heading (tsudo 2026-11-14: 見出しは一覧の上に、幅の節約),
    one item to a line; an item may be `(text, background colour)`.
    """
    rows = []
    for i, (heading, items) in enumerate(sections):
        space = " padding-top:6px;" if i else ""
        rows.append(f"<tr><td style='color:#64748b;{space}'>{_escape(heading)}</td></tr>")
        for item in items:
            text, colour = item if isinstance(item, tuple) else (item, None)
            rows.append(_line_html(text, _background(colour)))
    return f"<table cellspacing='0' cellpadding='1' width='100%'>{''.join(rows)}</table>"


#: Where a long file path may go on to the next line (a zero-width space after these).
_BREAK_AFTER = re.compile(r"([/\\_\-])")


def _breakable(text: str) -> str:
    """A path able to wrap after / _ - (a long name never makes the card wider; the extension kept with its name): plain text."""
    return _BREAK_AFTER.sub("\\1\u200b", text)


def file_label(where: RecordImages) -> str:
    """The name of a record's image that is not in a folder: the last part of its path or URL."""
    return re.split(r"[?#]", where.place)[0].replace("\\", "/").rstrip("/").rsplit("/", 1)[-1] or where.place


def shape_picture(geometry: Optional[dict], width: int, height: int, text: str) -> QPixmap:
    """A footprint's shape in a box with a line of text (no thumbnail, or while it is fetched)."""
    pixmap = QPixmap(width, height)
    pixmap.fill(QColor("#eef2f7"))
    painter = QPainter(pixmap)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    place = footprint_of(geometry)
    rings = [r for r in (geometry or {}).get("rings") or [] if isinstance(r, list) and len(r) >= 3] if place else []
    if place is not None and rings:
        x0, y0, x1, y1 = place.box
        k = max(0.01, math.cos(math.radians((y0 + y1) / 2)))
        w, h = max((x1 - x0) * k, 1e-9), max(y1 - y0, 1e-9)
        margin = 16
        scale = min((width - 2 * margin) / w, (height - 2 * margin - 18) / h)
        ox = (width - w * scale) / 2
        oy = (height - 18 - h * scale) / 2
        path = QPainterPath()
        for ring in rings:
            points = [QPointF(ox + (p[0] - x0) * k * scale, oy + (y1 - p[1]) * scale) for p in ring if len(p) >= 2]
            if not points:
                continue
            path.moveTo(points[0])
            for p in points[1:]:
                path.lineTo(p)
            path.closeSubpath()
        painter.setBrush(QColor(37, 99, 235, 50))
        painter.setPen(QPen(QColor(UNREAD), 2))
        painter.drawPath(path)
    painter.setPen(QColor("#475569"))
    painter.drawText(QRectF(4, height - 20, width - 8, 18), int(Qt.AlignmentFlag.AlignCenter), text)
    painter.end()
    return pixmap


class FileRow(QWidget):
    """
    One file of a card's 「画像ファイル」 (tsudo, 2026-11-14): a click checks it or not
    (「開く」 opens the checked), a double click opens it alone, as in 「開く画像の選択」.
    """

    def __init__(self, card: "NoticeCard", name: str, checked: bool):
        super().__init__()
        self.card = card
        self.name = name
        lay = QHBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 2)
        lay.setSpacing(4)
        self.box = QCheckBox()
        self.box.setChecked(checked)
        # The row takes the clicks (on the box too): one click one change, a double click none.
        self.box.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        self.box.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.box.toggled.connect(lambda on: card.file_checked(name, on))
        lay.addWidget(self.box, 0, Qt.AlignmentFlag.AlignTop)
        self.label = QLabel(_breakable(name))
        self.label.setTextFormat(Qt.TextFormat.PlainText)
        self.label.setWordWrap(True)
        lay.addWidget(self.label, 1)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self._double = False

    def mousePressEvent(self, event) -> None:  # noqa: N802 (Qt's name)
        if event.button() == Qt.MouseButton.LeftButton:
            self._double = False
        event.accept()

    def mouseReleaseEvent(self, event) -> None:  # noqa: N802 (Qt's name)
        if event.button() == Qt.MouseButton.LeftButton and not self._double and self.rect().contains(event.pos()):
            self.box.setChecked(not self.box.isChecked())
        event.accept()

    def mouseDoubleClickEvent(self, event) -> None:  # noqa: N802 (Qt's name)
        if event.button() != Qt.MouseButton.LeftButton:
            return
        # The click before it changed the check: put back (a double click only opens).
        if not self._double:
            self._double = True
            self.box.setChecked(not self.box.isChecked())
        self.card.open_only(self.name)
        event.accept()


class NoticeCard(QFrame):
    """One new image's card."""

    def __init__(self, feed: "NoticeFeed", notice: WatchNotice):
        super().__init__()
        self.feed = feed
        self.notice = notice
        #: The thumbnail (or footprint) as drawn at the size the settings file asks, and that size.
        self._source: Optional[QPixmap] = None
        self._full = (DEFAULT_WIDTH, DEFAULT_HEIGHT)
        #: The card's width the page has room for (0: not known yet, the full size).
        self._room = 0
        self._attributes_width = 0
        #: The attributes' column of every card as wide as the widest one's (set by the page): the columns line up.
        self.attributes_column = 0
        self.setObjectName("MikageNoticeCard")
        self.setFrameShape(QFrame.Shape.StyledPanel)
        self.setMaximumWidth(CARD_WIDTH)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Minimum)
        outer = QHBoxLayout(self)
        outer.setContentsMargins(0, 0, 8, 0)
        outer.setSpacing(8)
        self.mark = QFrame()
        self.mark.setFixedWidth(5)
        outer.addWidget(self.mark)
        body = QVBoxLayout()
        body.setContentsMargins(0, 8, 0, 8)
        outer.addLayout(body, 1)

        head = QHBoxLayout()
        self.title = QLabel()
        self.title.setTextFormat(Qt.TextFormat.PlainText)
        self.title.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        self.title.setWordWrap(True)
        font = QFont(self.title.font())
        font.setPointSizeF(font.pointSizeF() * 1.25)
        font.setBold(True)
        self.title.setFont(font)
        head.addWidget(self.title, 1)
        self.badge = QLabel(self.tr("未読"))
        self.badge.setStyleSheet(f"color:white; background:{UNREAD}; border-radius:3px; padding:1px 6px;")
        head.addWidget(self.badge)
        self.found = QLabel()
        self.found.setTextFormat(Qt.TextFormat.PlainText)
        self.found.setStyleSheet("color: palette(mid);")
        head.addWidget(self.found)
        body.addLayout(head)

        # Side by side: the thumbnail, the image's attributes, its files and 「写っている地物」
        # (tsudo, 2026-11-14: サムネ、メタ、ファイル、地物); with less room the files above the features,
        # then all under the attributes. The buttons below.
        self.grid = QGridLayout()
        self.grid.setHorizontalSpacing(16)
        self.grid.setVerticalSpacing(6)
        self.picture = QLabel()
        self.picture.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.picture.setToolTip(self.tr("ダブルクリックで開く"))
        self.attributes = QLabel()
        self.attributes.setTextFormat(Qt.TextFormat.RichText)
        self.attributes.setWordWrap(True)
        self.attributes.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        self.contents_box = QWidget()
        box = QVBoxLayout(self.contents_box)
        box.setContentsMargins(0, 0, 0, 0)
        # No heading of its own (tsudo, 2026-11-14: 「写ってる地物っていう文字はけしていい」): each layer's is enough.
        self.contents = QLabel()
        self.contents.setTextFormat(Qt.TextFormat.RichText)
        self.contents.setWordWrap(True)
        self.contents.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        box.addWidget(self.contents)
        self.files_box = QWidget()
        box = QVBoxLayout(self.files_box)
        box.setContentsMargins(0, 0, 0, 0)
        self.files_title = QLabel(self.tr("画像ファイル"))
        bold = QFont(self.files_title.font())
        bold.setBold(True)
        self.files_title.setFont(bold)
        box.addWidget(self.files_title)
        #: The files, a row each (FileRow).
        self.file_rows: list[FileRow] = []
        self._file_list = QVBoxLayout()
        self._file_list.setContentsMargins(0, 0, 0, 0)
        self._file_list.setSpacing(0)
        box.addLayout(self._file_list)
        #: While they are looked for, why none, or that there are more.
        self.files = QLabel()
        self.files.setTextFormat(Qt.TextFormat.PlainText)
        self.files.setWordWrap(True)
        box.addWidget(self.files)
        self.button_box = QWidget()
        buttons = QHBoxLayout(self.button_box)
        buttons.setContentsMargins(0, 0, 0, 0)
        self.buttons: dict[str, QPushButton] = {}
        for key, text, tip in [
                ("open", self.tr("開く"), self.tr("画像をレイヤーとして開き、地図に切り替えて画像へ移動（既読に）。"
                                                 "画像ファイルの一覧があればチェックしたものを。カードのダブルクリックでも")),
                ("record", self.tr("ファイルなしで記録"), self.tr("画像ファイルなしのカタログの画像を観測記録の画像一覧へ（ID・撮像日時・範囲はカタログから）")),
                ("map", self.tr("地図で見る"), self.tr("地図に切り替えて画像の範囲へ移動（既読に）")),
                ("read", self.tr("既読"), self.tr("既読にする"))]:
            # No 「削除」 on the card (tsudo, 2026-11-14: 用語が混乱する); the 通知 dock still has it.
            button = QPushButton(text)
            button.setToolTip(tip)
            button.clicked.connect(lambda _on=False, k=key: self.feed.act(k, self.notice))
            buttons.addWidget(button)
            self.buttons[key] = button
        buttons.addStretch(1)
        self.grid.addWidget(self.picture, 0, 0, 2, 1, Qt.AlignmentFlag.AlignTop)
        #: The columns beside the thumbnail (filled by `_arrange`).
        self._columns: list[QWidget] = []
        for i in range(3):
            column = QWidget()
            lay = QVBoxLayout(column)
            lay.setContentsMargins(0, 0, 0, 0)
            lay.setSpacing(8)
            self.grid.addWidget(column, 0, 1 + i)
            self._columns.append(column)
        self.grid.addWidget(self.button_box, 1, 1, 1, 3)
        self.grid.setRowStretch(1, 1)
        #: How the boxes are laid out (`_arrange`): 4 a column each, 3 the files above the features, 2 all under the attributes.
        self.mode = 0
        self._arrange(4)
        body.addLayout(self.grid)
        self.update_notice(notice)

    def mouseDoubleClickEvent(self, event) -> None:  # noqa: N802 (Qt's name)
        super().mouseDoubleClickEvent(event)
        key = "open" if self.buttons["open"].isVisible() else "record" if self.buttons["record"].isVisible() else "map"
        self.feed.act(key, self.notice)

    def update_notice(self, notice: WatchNotice) -> None:
        """The card as the notice is now (read, renamed watch, features found)."""
        self.notice = notice
        feed = self.feed
        catalog = feed.catalog_of(notice)
        plan = notice.kind == PLAN
        self.title.setText(notice.image_id or self.tr("（画像ID なし）"))
        self.found.setText(self.tr("発見 {t}").format(t=time_text(notice.found)))
        unread = not notice.read
        self.badge.setVisible(unread)
        self.mark.setStyleSheet(f"background:{UNREAD if unread else 'transparent'};")
        files = catalog is None or catalog.image_files
        self.buttons["open"].setVisible(not plan and files)
        self.buttons["record"].setVisible(not plan and not files)
        self.buttons["read"].setVisible(unread)
        rows = [(self.tr("監視"), notice.watch_name)]
        if plan:
            rows += [(self.tr("予定"), time_text(notice.acquired)), (self.tr("対象"), notice.sensor)]
        elif catalog is not None:
            rows.append((self.tr("カタログ"), catalog.label))
            record = record_of(notice.feature, catalog.fields)
            rows += [(c.label, cell_text(record, c.key)) for c in list_columns(catalog) if c.key != "id"]
        else:
            rows += [(self.tr("カタログ"), notice.catalog_label + self.tr("（設定ファイルになし）")),
                     (self.tr("撮像日時"), time_text(notice.acquired)), (self.tr("センサー"), notice.sensor)]
        text = _rows_html([(label, value) for label, value in rows if value])
        self.attributes.setText(text)
        # Its lines unwrapped (up to ATTRIBUTES_WIDTH) when the features are a column beside.
        doc = QTextDocument()
        doc.setDefaultFont(self.attributes.font())
        doc.setHtml(text)
        self._attributes_width = min(ATTRIBUTES_WIDTH, math.ceil(doc.idealWidth()) + 4)
        self._attributes_min()
        self.show_files()
        self.show_contents()
        self.show_picture()

    def _attributes_min(self) -> None:
        self.attributes.setMinimumWidth(max(self._attributes_width, self.attributes_column) if self.wide else 0)

    def line_up(self, width: int) -> None:
        """The attributes' column as wide as the page's widest (`NoticeFeed._line_up`)."""
        if width != self.attributes_column:
            self.attributes_column = width
            self._attributes_min()
            self._show_source()  # every card laid out alike

    @property
    def wide(self) -> bool:
        """Whether the attributes are a column of their own (the features or files beside them)."""
        return self.mode >= 3

    def column_of(self, widget: QWidget) -> int:
        """Which column beside the thumbnail a box is in (0: the attributes')."""
        return next((i for i, c in enumerate(self._columns) if widget.parent() is c), -1)

    def _set_shown(self, box: QWidget, on: bool) -> None:
        if box.isHidden() == on:
            box.setVisible(on)
            self._show_source()  # another layout may fit now

    def show_contents(self) -> None:
        """「写っている地物」: per layer of the catalog its heading, then its features' names one to a line, or why not yet."""
        catalog = self.feed.catalog_of(self.notice)
        layers = catalog.footprint_layers if catalog is not None and self.notice.kind != PLAN else []
        self._set_shown(self.contents_box, bool(layers))
        if not layers:
            return
        kept = self.notice.extra.get("contents") if isinstance(self.notice.extra.get("contents"), dict) else {}
        sections = []
        for layer in layers:
            found = Contents.from_json(kept.get(layer.key()))
            if found is not None:
                heading = self.tr("{label}（{n} 件）").format(label=layer.label, n=found.count)
                lines: list = contents_lines(found)
                if layer.background is not None and found.texts:
                    lines = [(text, layer.background.color_for(found.value_at(i))) for i, text in enumerate(found.texts)]
                if found.cut:
                    lines.append(self.tr("（先頭の {read} 件まで）").format(read=found.read))
                sections.append((heading, lines))
                continue
            failed = self.feed.work.failure(self.feed.contents_key(self.notice))
            sections.append((layer.label, [self.tr("照合できず: {m}").format(m=failed) if failed else self.tr("照合中…")]))
        self.contents.setText(_sections_html(sections))

    def show_files(self) -> None:
        """「画像ファイル」: the files of the record that can be opened, a checkable row each, or why not yet."""
        feed = self.feed
        catalog = feed.catalog_of(self.notice)
        on = catalog is not None and catalog.image_files and self.notice.kind != PLAN
        self._set_shown(self.files_box, on)
        if not on:
            return
        where = feed.files.get(self.notice.id)
        names = feed.file_names(self.notice)
        if [r.name for r in self.file_rows] != names:
            for row in self.file_rows:
                self._file_list.removeWidget(row)
                row.deleteLater()
            unchecked = feed.unchecked.get(self.notice.id, set())
            self.file_rows = [FileRow(self, name, name not in unchecked) for name in names]
            for row in self.file_rows:
                row.setToolTip(self.tr("{place}\nクリックでチェック（「開く」でチェックしたものを開く）、ダブルクリックでその画像だけ開く")
                               .format(place=where.place if where else ""))
                self._file_list.addWidget(row)
        self.files_title.setText(self.tr("画像ファイル（{n}）").format(n=len(names)) if len(names) > 1 else self.tr("画像ファイル"))
        if where is None:
            failed = feed.work.failure(feed.files_key(self.notice))
            self.files.setText(self.tr("見つからず: {m}").format(m=failed) if failed else self.tr("探しています…"))
        else:
            self.files.setText(self.tr("（ほかにもあり。すべては通知ドックの「開く」で選ぶ）") if where.more else "")
        self.files.setVisible(bool(self.files.text()))

    def file_checked(self, name: str, on: bool) -> None:
        self.feed.set_checked(self.notice.id, name, on)

    def open_only(self, name: str) -> None:
        self.feed.open_files(self.notice, [name] if self.feed.files.get(self.notice.id) and self.feed.files[self.notice.id].files else None)

    def show_picture(self) -> None:
        """The thumbnail, or the footprint's shape (with why there is no thumbnail, or that it is being fetched)."""
        feed = self.feed
        catalog = feed.catalog_of(self.notice)
        pattern = catalog.thumbnail if catalog is not None and self.notice.kind != PLAN else None
        width, height = (pattern.width, pattern.height) if pattern else (DEFAULT_WIDTH, DEFAULT_HEIGHT)
        self._full = (width, height)
        geometry = self.notice.feature.get("geometry")
        address = feed.thumbnail_address(self.notice)
        self.picture.setToolTip(self.tr("ダブルクリックで開く"))
        if address is None:
            text = self.tr("撮像計画") if self.notice.kind == PLAN else self.tr("サムネイルなし")
            self._set_source(shape_picture(geometry, width, height, text))
            return
        path = cached(feed.thumbnail_folder, address)
        if path:
            image = QPixmap(path)
            if not image.isNull():
                self._set_source(image)
                return
        failed = feed.work.failure("thumb|" + address)
        self._set_source(shape_picture(geometry, width, height,
                                       self.tr("サムネイルを取得できず") if failed else self.tr("サムネイルを読み込み中…")))
        if failed:
            self.picture.setToolTip(self.tr("サムネイルを取得できず: {m}").format(m=failed))

    def fit(self, room: int) -> None:
        """The card as wide as the page has room for: the thumbnail smaller when it is narrow."""
        if room != self._room:
            self._room = room
            # Every card as wide as the page has room for: the columns line up from card to card.
            if room > 0:
                self.setFixedWidth(room)
            else:
                self.setMinimumWidth(0)
                self.setMaximumWidth(CARD_WIDTH)
            self._show_source()

    def _set_source(self, picture: QPixmap) -> None:
        self._source = picture
        self._show_source()

    def _arrange(self, mode: int) -> None:
        """4: thumbnail | attributes | files | features; 3: the files above the features; 2: all under the attributes."""
        if mode == self.mode:
            return
        self.mode = mode
        boxes = {4: [[self.attributes], [self.files_box], [self.contents_box]],
                 3: [[self.attributes], [self.files_box, self.contents_box], []],
                 2: [[self.attributes, self.files_box, self.contents_box], [], []]}[mode]
        for column, widgets in zip(self._columns, boxes):
            lay = column.layout()
            while lay.count():
                lay.takeAt(0)  # the widgets are put back below (or in another column), the stretch dropped
            for widget in widgets:
                lay.addWidget(widget)
            lay.addStretch(1)
            column.setVisible(bool(widgets))
        self._attributes_min()
        for i, stretch in enumerate({4: (0, 2, 3), 3: (0, 1, 0), 2: (1, 0, 0)}[mode]):
            self.grid.setColumnStretch(1 + i, stretch)

    def _mode_for(self, room: int, picture: int) -> int:
        """The layout the card has room for, with the thumbnail as asked (the boxes it does not show need none)."""
        beside = [w for box, w in ((self.files_box, FILES_ROOM), (self.contents_box, FEATURES_ROOM)) if not box.isHidden()]
        if room <= 0:
            return 4
        attributes = max(self._attributes_width, self.attributes_column) or 300
        base = picture + COLUMN_GAP + attributes + CARD_FRAME
        if len(beside) == 2 and room >= base + sum(COLUMN_GAP + w for w in beside):
            return 4
        if room >= base + (COLUMN_GAP + max(beside) if beside else 0):
            return 3
        return 2

    def _show_source(self) -> None:
        full_w, full_h = self._full
        width = full_w
        self._arrange(self._mode_for(self._room, full_w))
        if self._room > 0 and not self.wide:
            # What the card needs beside the thumbnail (the buttons in a row at least).
            beside = self.layout().minimumSize().width() - self.picture.width()
            width = max(MIN_PICTURE, min(full_w, self._room - max(TEXT_ROOM, beside)))
        height = max(1, round(full_h * width / full_w))
        self.picture.setFixedSize(width, height)
        if self._source is not None and not self._source.isNull():
            self.picture.setPixmap(self._source.scaled(width, height, Qt.AspectRatioMode.KeepAspectRatio,
                                                       Qt.TransformationMode.SmoothTransformation))


class NoticeFeed(QWidget):
    """The 新着画像 page (`plugin.notice_feed`): the 通知 dock's notices as cards."""

    def __init__(self, iface, context, dock, parent=None):
        super().__init__(parent)
        self.setObjectName("MikageNoticeFeed")
        self.iface = iface
        self.context = context
        self.dock = dock
        #: Shows the map (set by the plugin: the tabs over the map area).
        self.show_map: Callable[[], None] = lambda: None
        self.work = CardWork(self)
        self.thumbnail_folder = settings.thumbnail_folder()
        self.cards: dict[str, NoticeCard] = {}
        #: Every notice, the oldest first, as last told.
        self.notices: list[WatchNotice] = []
        #: How many of the newest notices have cards.
        self.shown = PAGE
        #: Notices that came in since the page last showed them (「↓ 新着 n 件」).
        self.arrived: list[str] = []
        self._known: Optional[set] = None
        self._watches: dict = {}
        #: Notices read while 「未読のみ」 is on: their cards stay until it is turned again.
        self._kept: set[str] = set()
        self._positioned = False
        #: The layers opened last with 「前の画像を閉じる」 on (their ids).
        self.last_opened: list[str] = []
        #: Where each notice's image files are (「画像ファイル」, looked for once a session).
        self.files: dict[str, RecordImages] = {}
        #: The files unchecked on each notice's card (all are checked at first).
        self.unchecked: dict[str, set[str]] = {}
        self._written = 0

        layout = QVBoxLayout(self)
        layout.setContentsMargins(8, 6, 8, 6)
        top = QHBoxLayout()
        heading = QLabel(self.tr(TITLE))
        font = QFont(heading.font())
        font.setPointSizeF(font.pointSizeF() * 1.3)
        font.setBold(True)
        heading.setFont(font)
        top.addWidget(heading)
        self.count = QLabel()
        top.addWidget(self.count)
        top.addStretch(1)
        self._heading = heading
        self._top = top
        #: The filters and buttons: beside the heading, or on a row of their own when the map area is narrow.
        self.controls = QWidget()
        self.controls.setMinimumWidth(1)  # the page never makes QGIS's map area wider
        bar = QHBoxLayout(self.controls)
        bar.setContentsMargins(0, 0, 0, 0)
        self.watch_box = QComboBox()
        self.watch_box.setToolTip(self.tr("監視で絞り込む"))
        self.watch_box.currentIndexChanged.connect(lambda _i: self._filter())
        bar.addWidget(self.watch_box)
        self.unread_only = QCheckBox(self.tr("未読のみ"))
        self.unread_only.setChecked(settings.feed_unread_only())
        self.unread_only.toggled.connect(self._unread_only_toggled)
        bar.addWidget(self.unread_only)
        self.close_previous = QCheckBox(self.tr("前の画像を閉じる"))
        self.close_previous.setToolTip(self.tr("「開く」で画像を開いたら、その前にここから開いた画像のレイヤーをプロジェクトから外す"
                                               "（順に見ていくときに。PC ごとに記憶）"))
        self.close_previous.setChecked(settings.feed_close_previous())
        self.close_previous.toggled.connect(settings.set_feed_close_previous)
        bar.addWidget(self.close_previous)
        self.all_read = QPushButton(self.tr("すべて既読"))
        self.all_read.clicked.connect(lambda: self.dock._mark(None))
        bar.addWidget(self.all_read)
        self.check_now = QPushButton(self.tr("今すぐ確認"))
        self.check_now.setToolTip(self.tr("すべての監視で新着画像を今すぐ確認"))
        self.check_now.clicked.connect(lambda: self.dock.check(force=True))
        bar.addWidget(self.check_now)
        top.addWidget(self.controls)
        layout.addLayout(top)
        self._second = QHBoxLayout()
        layout.addLayout(self._second)

        self.scroll = QScrollArea()
        self.scroll.setObjectName("MikageNoticeFeedScroll")
        self.scroll.setWidgetResizable(True)
        self.scroll.setFrameShape(QFrame.Shape.NoFrame)
        holder = QWidget()
        self.column = QVBoxLayout(holder)
        self.column.setSpacing(10)
        self.older = QPushButton()
        self.older.clicked.connect(self.show_older)
        self.column.addWidget(self.older, 0, Qt.AlignmentFlag.AlignHCenter)
        self.empty = QLabel(self.tr("新着画像はまだなし。監視は画像カタログの「この条件で監視…」か、地図の右クリック「この地点の新着画像を監視…」で追加"))
        self.empty.setWordWrap(True)
        self.empty.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.column.addWidget(self.empty)
        self.column.addStretch(1)
        self.scroll.setWidget(holder)
        layout.addWidget(self.scroll, 1)
        self.arrived_button = QPushButton()
        self.arrived_button.setToolTip(self.tr("来たばかりの新着画像へ（新着が来ても画面は動かさない）"))
        self.arrived_button.clicked.connect(self.go_to_arrived)
        self.arrived_button.hide()
        layout.addWidget(self.arrived_button, 0, Qt.AlignmentFlag.AlignHCenter)

        self._work_timer = QTimer(self)
        self._work_timer.setSingleShot(True)
        self._work_timer.setInterval(150)
        self._work_timer.timeout.connect(self._want_work)
        self.scroll.verticalScrollBar().valueChanged.connect(lambda _v: self._scrolled())
        dock.refreshed.append(self.set_notices)
        context.configChanged.connect(self._config_changed)

    # --- The notices --------------------------------------------------------------------------------

    def catalog_of(self, notice: WatchNotice) -> Optional[CatalogConfig]:
        """The settings file's catalog of a notice: its watch's (same URL), else the one of the same name."""
        watch = self._watches.get(notice.watch_id)
        catalogs = list(self.context.config.image_catalogs)
        if watch is not None:
            found = next((c for c in catalogs if c.url == watch.catalog.url), None)
            if found is not None:
                return found
        return next((c for c in catalogs if c.label == notice.catalog_label), None)

    def thumbnail_address(self, notice: WatchNotice) -> Optional[str]:
        catalog = self.catalog_of(notice)
        if catalog is None or catalog.thumbnail is None or notice.kind == PLAN:
            return None
        return thumbnail_address(catalog.thumbnail, notice.feature, catalog.fields, self.context.token_for(catalog.url, catalog.token))

    def files_key(self, notice: WatchNotice) -> str:
        return "files|" + notice.id

    def file_names(self, notice: WatchNotice) -> list[str]:
        """The names on a card's 「画像ファイル」 (a folder's files, else the image's own name; none until found)."""
        where = self.files.get(notice.id)
        if where is None:
            return []
        return list(where.files) or [file_label(where)]

    def set_checked(self, notice_id: str, name: str, on: bool) -> None:
        unchecked = self.unchecked.setdefault(notice_id, set())
        if on:
            unchecked.discard(name)
        else:
            unchecked.add(name)

    def checked_files(self, notice: WatchNotice) -> list[str]:
        unchecked = self.unchecked.get(notice.id, set())
        return [n for n in self.file_names(notice) if n not in unchecked]

    def contents_key(self, notice: WatchNotice) -> str:
        catalog = self.catalog_of(notice)
        return "contents|" + notice.id + "|" + "\n".join(layer.key() for layer in (catalog.footprint_layers if catalog else []))

    def set_notices(self, notices: list[WatchNotice]) -> None:
        """The notices as the 通知 dock read them (newest first): the cards follow when the page is shown."""
        try:
            self._watches = {w.id: w for w in self.dock.store().watches()}
        except Exception:  # noqa: BLE001 (no GeoPackage: no catalogs by watch)
            self._watches = {}
        ordered = sorted(notices, key=lambda n: (n.found, self._taken(n)))
        ids = {n.id for n in ordered}
        unread = {n.id for n in ordered if not n.read}
        if self._known is not None and self._positioned:
            # 「↓ 新着 n 件」: unread images that came in since the page was first shown (it never moves by itself).
            fresh = [n.id for n in ordered if n.id not in self._known and not n.read]
            self.arrived = [i for i in self.arrived if i in unread] + fresh
        self._known = ids
        self.notices = ordered
        self._show_watches()
        if self.isVisible():
            self._render()
            self._show_arrived()
            QTimer.singleShot(0, self._scrolled)
        self._show_count()

    @staticmethod
    def _taken(n: WatchNotice) -> float:
        from ..core.util import time_of

        t = time_of(n.acquired)
        return 0.0 if math.isnan(t) else t

    def _show_watches(self) -> None:
        current = self.watch_box.currentData()
        names: dict[str, str] = {}
        for n in self.notices:
            names.setdefault(n.watch_id, n.watch_name)
        self.watch_box.blockSignals(True)
        self.watch_box.clear()
        self.watch_box.addItem(self.tr("すべての監視"), "")
        for watch_id, name in sorted(names.items(), key=lambda x: x[1]):
            self.watch_box.addItem(name, watch_id)
        self.watch_box.setCurrentIndex(max(0, self.watch_box.findData(current)))
        self.watch_box.blockSignals(False)

    def _show_count(self) -> None:
        unread = sum(1 for n in self.notices if not n.read)
        shown = sum(1 for c in self.cards.values() if not c.isHidden())
        text = self.tr("未読 {u} 件・全 {n} 件").format(u=unread, n=len(self.notices))
        if self._filtering() and self.cards:
            text += self.tr("（表示 {s} 件）").format(s=shown)
        self.count.setText(text)

    # --- The cards ----------------------------------------------------------------------------------

    def showEvent(self, event) -> None:  # noqa: N802 (Qt's name)
        super().showEvent(event)
        self._render()
        if not self._positioned:
            # Opened the first time: at the oldest unread card (else the newest).
            self._positioned = True
            self.arrived = []
            first = next((n for n in self.notices if not n.read and n.id in self.cards), None)
            QTimer.singleShot(0, lambda: self.go_to(first.id) if first else self._to_bottom())
        self.arrived = [i for i in self.arrived if i in self.cards]
        QTimer.singleShot(0, self._scrolled)
        self._show_arrived()
        self._work_timer.start()

    def resizeEvent(self, event) -> None:  # noqa: N802 (Qt's name)
        super().resizeEvent(event)
        self._lay_out_top()
        room = self._card_room()
        for card in self.cards.values():
            card.fit(room)
        self._work_timer.start()

    def _card_room(self) -> int:
        """How wide a card can be without the page scrolling sideways."""
        margins = self.column.contentsMargins()
        # The vertical scroll bar's width counted always (it comes and goes with the cards).
        bar = max(self.scroll.verticalScrollBar().sizeHint().width(),
                  self.scroll.style().pixelMetric(QStyle.PixelMetric.PM_ScrollBarExtent, None, self.scroll))
        width = self.scroll.width() - 2 * self.scroll.frameWidth() - bar
        return max(0, min(CARD_WIDTH, width - margins.left() - margins.right() - 2))

    def _lay_out_top(self) -> None:
        """The filters and buttons beside the heading when there is room, else on the row below."""
        need = self._heading.sizeHint().width() + self.count.sizeHint().width() + self.controls.sizeHint().width() + 40
        wide = self.width() >= need
        if wide and self._second.indexOf(self.controls) >= 0:
            self._second.removeWidget(self.controls)
            self._top.addWidget(self.controls)
        elif not wide and self._top.indexOf(self.controls) >= 0:
            self._top.removeWidget(self.controls)
            self._second.addWidget(self.controls)

    def _render(self) -> None:
        """Cards for the newest `shown` notices, in order (made, updated or taken away as needed)."""
        if self.cards:
            if self.unread_only.isChecked():
                self._kept |= {n.id for n in self.notices if n.read and n.id in self.cards and not self.cards[n.id].notice.read}
        wanted = self.notices[-self.shown:] if self.shown else []
        wanted_ids = {n.id for n in wanted}
        for notice_id in [i for i in self.cards if i not in wanted_ids]:
            card = self.cards.pop(notice_id)
            self.column.removeWidget(card)
            card.deleteLater()
        index = 1  # after 「さらに前」
        for n in wanted:
            card = self.cards.get(n.id)
            if card is None:
                card = NoticeCard(self, n)
                card.fit(self._card_room())
                self.cards[n.id] = card
                self.column.insertWidget(index, card, 0, Qt.AlignmentFlag.AlignHCenter)
            else:
                if self.column.indexOf(card) != index:
                    self.column.removeWidget(card)
                    self.column.insertWidget(index, card, 0, Qt.AlignmentFlag.AlignHCenter)
                if card.notice.to_json() != n.to_json() or card.notice.read != n.read:
                    card.update_notice(n)
            index += 1
        older = len(self.notices) - len(wanted)
        self.older.setText(self.tr("さらに前の {n} 件を表示").format(n=min(PAGE, older)))
        self.older.setVisible(older > 0)
        self.empty.setVisible(not self.notices)
        self._filter()
        self._line_up()
        self._work_timer.start()

    def _line_up(self) -> None:
        """Every card's attributes' column as wide as the widest: the features start at the same place on each."""
        width = max((c._attributes_width for c in self.cards.values()), default=0)
        for card in self.cards.values():
            card.line_up(width)

    def show_older(self) -> None:
        """「さらに前の n 件」: older cards above, the page kept where it was."""
        bar = self.scroll.verticalScrollBar()
        before = bar.maximum() - bar.value()
        self.shown += PAGE
        self._render()
        QTimer.singleShot(0, lambda: bar.setValue(bar.maximum() - before))

    def _filtering(self) -> bool:
        return self.unread_only.isChecked() or bool(self.watch_box.currentData())

    def _unread_only_toggled(self, on: bool) -> None:
        settings.set_feed_unread_only(on)
        self._kept = set()
        self._filter()

    def _filter(self) -> None:
        """「未読のみ」 and the watch chosen: the other cards hidden (one read just now stays until 未読のみ is turned again)."""
        only, watch = self.unread_only.isChecked(), self.watch_box.currentData()
        for notice_id, card in self.cards.items():
            n = card.notice
            hide = (only and n.read and notice_id not in self._kept) or (bool(watch) and n.watch_id != watch)
            card.setHidden(hide)
        self._show_count()
        self._work_timer.start()

    def _visible_cards(self) -> list[NoticeCard]:
        """The cards in view (and a screen above and below), the lowest (newest) first."""
        viewport = self.scroll.viewport()
        top = self.scroll.verticalScrollBar().value()
        height = viewport.height()
        out = []
        for card in self.cards.values():
            if card.isHidden():
                continue
            y = card.y()
            if y + card.height() >= top - height and y <= top + 2 * height:
                out.append(card)
        return sorted(out, key=lambda c: -c.y())

    def _scrolled(self) -> None:
        """The cards in view: their thumbnails and features fetched, those just come in no longer counted as unseen."""
        self._work_timer.start()
        if self.arrived and self.isVisible():
            seen = {c.notice.id for c in self._in_view()}
            if seen & set(self.arrived):
                self.arrived = [i for i in self.arrived if i not in seen]
                self._show_arrived()

    def _in_view(self) -> list[NoticeCard]:
        self.column.activate()  # a card just put in has its place
        top = self.scroll.verticalScrollBar().value()
        bottom = top + self.scroll.viewport().height()
        return [c for c in self.cards.values() if not c.isHidden() and c.y() < bottom and c.y() + c.height() > top]

    def _show_arrived(self) -> None:
        n = len(self.arrived)
        self.arrived_button.setText(self.tr("↓ 新着 {n} 件").format(n=n))
        self.arrived_button.setVisible(n > 0 and self.isVisible())

    def go_to_arrived(self) -> None:
        """「↓ 新着 n 件」: to the first image that came in since, as asked (the page never moves by itself)."""
        first = next((i for i in self.arrived if i in self.cards and not self.cards[i].isHidden()), None)
        self.arrived = []
        self._show_arrived()
        if first:
            self.go_to(first)
        else:
            self._to_bottom()

    def go_to(self, notice_id: str) -> None:
        """The page scrolled so that a card is at its top."""
        card = self.cards.get(notice_id)
        if card is not None:
            self.column.activate()
            self.scroll.verticalScrollBar().setValue(max(0, card.y() - 8))

    def _to_bottom(self) -> None:
        self.column.activate()
        bar = self.scroll.verticalScrollBar()
        bar.setValue(bar.maximum())

    # --- Thumbnails and features ------------------------------------------------------------------

    def _want_work(self) -> None:
        if not self.isVisible():
            return
        jobs = []
        for card in self._visible_cards():
            n = card.notice
            address = self.thumbnail_address(n)
            if address and not cached(self.thumbnail_folder, address):
                jobs.append(self._thumbnail_job(address))
            catalog = self.catalog_of(n)
            if catalog is not None and catalog.image_files and n.kind != PLAN and n.id not in self.files:
                jobs.append(self._files_job(n, catalog))
            if catalog is not None and catalog.footprint_layers and n.kind != PLAN:
                kept = n.extra.get("contents") if isinstance(n.extra.get("contents"), dict) else {}
                missing = [layer for layer in catalog.footprint_layers if layer.key() not in kept]
                if missing:
                    jobs.append(self._contents_job(n, catalog, missing))
        self.work.want(jobs)

    def _thumbnail_job(self, address: str):
        from ..qgis_io.settings import auth_for

        folder, authcfg = self.thumbnail_folder, auth_for(address)

        def done(_path) -> None:
            self._written += 1
            if self._written % 50 == 0:
                prune(folder)
            for card in self.cards.values():
                if self.thumbnail_address(card.notice) == address:
                    card.show_picture()

        def failed(_message) -> None:
            for card in self.cards.values():
                if self.thumbnail_address(card.notice) == address:
                    card.show_picture()

        return ("thumb|" + address, lambda task: fetch(folder, address, authcfg), done, failed)

    def _files_job(self, notice: WatchNotice, catalog: CatalogConfig):
        record = record_of(notice.feature, catalog.fields)
        mappings = list(self.context.config.path_mappings)
        names = self.context.known_names()
        notice_id = notice.id

        def work(task):
            try:
                return record_files(record, catalog, mappings, names, task.isCanceled)
            except PathError as error:
                raise ValueError(str(error)) from None  # told on the card (looked for again a while later)

        def done(where: RecordImages) -> None:
            self.files[notice_id] = where
            card = self.cards.get(notice_id)
            if card is not None:
                card.show_files()

        def failed(_message: str) -> None:
            card = self.cards.get(notice_id)
            if card is not None:
                card.show_files()

        return (self.files_key(notice), work, done, failed)

    def _contents_job(self, notice: WatchNotice, catalog: CatalogConfig, layers):
        client = self.context.esri()
        geometry = notice.feature.get("geometry")
        tokens = {layer.key(): self.context.token_for(layer.url, layer.token or catalog.token) for layer in layers}
        notice_id = notice.id

        def work(task):
            return {layer.key(): contents_of(client, layer, geometry, tokens[layer.key()], task.isCanceled).to_json() for layer in layers}

        def done(found: dict) -> None:
            card = self.cards.get(notice_id)
            current = card.notice if card is not None else next((n for n in self.notices if n.id == notice_id), None)
            if current is None:
                return
            kept = dict(current.extra.get("contents") or {}) if isinstance(current.extra.get("contents"), dict) else {}
            kept.update(found)
            # Only the layers the settings file has now are kept.
            keys = {layer.key() for layer in catalog.footprint_layers}
            kept = {k: v for k, v in kept.items() if k in keys}
            current.extra = {**current.extra, "contents": kept}
            try:
                self.dock.store().put_extra(notice_id, "contents", kept)
            except Exception:  # noqa: BLE001 (the GeoPackage busy or gone: shown now, asked again next time)
                pass
            if card is not None:
                card.show_contents()

        def failed(_message: str) -> None:
            card = self.cards.get(notice_id)
            if card is not None:
                card.show_contents()

        return (self.contents_key(notice), work, done, failed)

    def _config_changed(self) -> None:
        """Another settings file (or one changed): the cards with the catalogs as they are now."""
        self.work.failures.clear()
        self.files.clear()  # pathMappings or imagePatterns may be others now
        for card in self.cards.values():
            card.update_notice(card.notice)
        self._work_timer.start()

    # --- What the cards do --------------------------------------------------------------------------

    def act(self, key: str, notice: WatchNotice) -> None:
        if key == "open":
            self.open_image(notice)
        elif key == "record":
            self.dock.open_notices([notice])
            self.show_map()
        elif key == "map":
            self.see_on_map(notice)
        elif key == "read":
            self.dock._mark([notice.id])

    def open_image(self, notice: WatchNotice) -> list:
        """
        「開く」: the image opened, the map in front, moved to the image (all its
        files); with 「前の画像を閉じる」 those opened here before close. Not
        opened (the reason in the message bar): the page stays.
        """
        if notice.kind == PLAN:
            self.see_on_map(notice)
            return []
        where = self.files.get(notice.id)
        if where is not None:
            # The files listed on the card: those checked (tsudo, 2026-11-14), without asking.
            chosen = self.checked_files(notice)
            if not chosen:
                self.iface.messageBar().pushInfo(self.tr(TITLE), self.tr("{id}: 開く画像ファイルにチェックなし").format(id=notice.image_id))
                return []
            return self.open_files(notice, chosen if where.files else None)
        layers = self.dock.open_notices([notice])
        # A folder chosen just now: its files can be listed on the card.
        if self.work.failures.pop(self.files_key(notice), None) is not None:
            self._work_timer.start()
        return self._show_opened(layers)

    def open_files(self, notice: WatchNotice, files: Optional[list[str]]) -> list:
        """A file of 「画像ファイル」 pressed: that image alone opened, like 「開く」 (all the record's files when None)."""
        where = self.files.get(notice.id)
        if where is None:
            return []
        return self._show_opened(self.dock.open_notice_files(notice, where, files))

    def _show_opened(self, layers: list) -> list:
        """Images opened from the page: those opened before closed (「前の画像を閉じる」), the map in front at them."""
        if not layers:
            return []
        ids = [layer.id() for layer in layers]
        if self.close_previous.isChecked():
            close_layers([i for i in self.last_opened if i not in ids])
            self.last_opened = ids
        else:
            self.last_opened = []
        self.show_map()
        canvas = self.iface.mapCanvas()
        extent = layers_extent(layers, canvas.mapSettings().destinationCrs())
        if extent is not None:
            if extent.isEmpty():
                extent = extent.buffered(max(canvas.mapUnitsPerPixel() * 200, 1e-6))
            extent.scale(1.05)
            canvas.setExtent(extent)
            canvas.refresh()
        return layers

    def see_on_map(self, notice: WatchNotice) -> None:
        """「地図で見る」: the map in front at the image's footprint, flashed (read)."""
        self.show_map()
        moved = self.dock.zoom_to_notices([notice])
        self.dock._mark([notice.id])
        shape = footprint(notice.feature.get("geometry"))
        if moved and shape is not None:
            canvas = self.iface.mapCanvas()
            QTimer.singleShot(200, lambda: canvas.flashGeometries([shape], QgsCoordinateReferenceSystem("EPSG:4326")))

    def close_down(self) -> None:
        self.work.close_down()
        self._work_timer.stop()
        try:
            self.dock.refreshed.remove(self.set_notices)
        except ValueError:
            pass
