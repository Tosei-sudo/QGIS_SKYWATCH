"""
画像カタログドックの画像を開く (ui/catalog_parts, docs/architecture.md): opening the images (files, folders, by id, again), and
「ファイルなしで記録」. A part of CatalogDock (ui/catalog_dock.py): its methods as they were, on the dock's widgets and state.
"""
from __future__ import annotations

from typing import Callable, Optional

from qgis.core import Qgis
from qgis.PyQt.QtWidgets import QFileDialog, QPushButton

from ...core.catalog.api import (
    CatalogConfig, PathError, PathMapping, OpenedImage, CatalogRecord, listed_image, record_of, records_by_ids,
)
from ...core.obs.api import local_time
from ...qgis_io.overviews_io import queue as overview_queue
from ...qgis_io.catalog_io import (
    Cancelled, add_or_choose, close_layers, located_again, open_located, open_record,
    opened_images, record_of_layer,
)
from ..docks import plain_tip
from ..image_files_dialog import choose_image_files
from .. import open_again_dialog
from .common import TITLE


class OpeningPart:
    """画像カタログドックの画像を開く."""

    def choose_images(self, record, files: list[str], more: bool) -> Optional[list[str]]:
        """Which of a record's images (its folder's files matching `imagePatterns`) to open."""
        return choose_image_files(self, record, files, more)

    def choose_folder(self, mapping: PathMapping) -> Optional[str]:
        return QFileDialog.getExistingDirectory(self, self.tr("「{label}」（{prefix}）のフォルダの選択").format(label=mapping.label, prefix=mapping.prefix)) or None

    def _activated(self, record: CatalogRecord) -> None:
        """A double click: the image opened, or recorded without its file in a catalog without image files."""
        if self.has_files():
            self.open_records([record])
        else:
            self.record_without_files([record])

    def record_without_files(self, records=None):
        """
        「ファイルなしで記録」: the chosen images (`records`) listed in 観測記録
        without their files, with the catalog's id, time and footprint; the
        first one is recorded on (the map moved to it). Its entry, None when
        none could be.
        """
        if self.denied("catalog.record"):
            return None
        records = self.chosen() if records is None else list(records)
        images = [i for i in (listed_image(r) for r in records) if i is not None]
        if not images:
            self.status.setText(self.tr("画像 ID のある画像を一覧で選択（Ctrl・Shift + クリックで複数）"))
            return None
        dock = self.observations() if self.observations else None
        entry = dock.list_catalog_images(images) if dock is not None else None
        if entry is None:
            self.status.setText(self.tr("観測記録は使えない（記録の保存先が未設定）"))
            return None
        if len(images) == 1:
            self.status.setText(self.tr("{id} を観測記録へ（ファイルなし）").format(id=entry.name))
        else:
            self.status.setText(self.tr("{n} 件を観測記録へ（ファイルなし）。◀ ▶ で次の画像").format(n=len(images)))
        return entry

    def open_records(self, records) -> list:
        """
        Opens images of the list as raster layers (a record whose path is a
        folder: the images chosen among its files, in a group); the reason is
        shown for those that cannot be opened, with 「ファイルなしで記録」 for
        them. With 「前の画像を閉じる」, the images opened here before close
        once these are open (not when none could be); with 「開いた画像へ移動」
        the map goes to those opened. In a catalog without image files, they
        are recorded without their files instead.
        """
        catalog = self._shown_catalog
        if catalog is None:
            return []
        return self.open_catalog_records(catalog, records)

    def open_catalog_records(self, catalog: CatalogConfig, records) -> list:
        """open_records for the records of any catalog (not only the one listed): an image looked up by its id."""
        if not catalog.image_files:
            self.record_without_files(records)
            return []
        layers = []
        problems = []
        unopened = []
        already = []
        for record in records:
            try:
                opened = open_record(record, catalog, self.context.config.path_mappings, self.choose_folder,
                                     lambda r, files, more: self.choose_images(r, files, more), problems,
                                     names=self.context.known_names())
            except Cancelled:
                break
            except PathError as error:
                problems.append(str(error))
                unopened.append(record)
                continue
            # Open already: chosen (and gone to) rather than added twice.
            opened, there = add_or_choose(opened, record)
            if there and not opened:
                already.append(record.id)
            overview_queue.request(opened, self.context.config.overviews, self.iface.messageBar())
            layers += opened + there
        note = self._opened(layers)
        if already:
            self.status.setText(self.tr("{id} は開いている画像。そのレイヤーを選択").format(id="、".join(already)) +
                                ("\n" + note if note else ""))
        if problems:
            self._say_unopened(problems, [r for r in unopened if r.id])
            self.status.setText(problems[-1])
        return layers

    def _opened(self, layers: list) -> str:
        """
        After images opened: 「投影法を画像に合わせる」・「向き」 (before the
        previous ones close: the map is put back only when none is left),
        「前の画像を閉じる」, 「開いた画像へ移動」, and the ids offered by
        「画像 ID で開く」. What 「向き」 did ("" for nothing), for the status.
        """
        note = ""
        if layers:
            note = self.orient_map(layers)
            # Only those opened with it on close (an image opened before it was turned on stays).
            opened_ids = [layer.id() for layer in layers]
            if self.close_previous.isChecked():
                close_layers([i for i in self.last_opened if i not in opened_ids])
                self.last_opened = opened_ids
            else:
                self.last_opened = []
            if self.zoom_opened.isChecked():
                self.zoom_to_layers(layers)
        self._refresh_opened()
        #: What 「向き」 did at the last opening (kept for the status lines set after).
        self.orient_note = note
        return note

    def orient_map(self, layers: list) -> str:
        """「投影法を画像に合わせる」・「向き」 for the images opened (the first of them); what was done."""
        catalogs = self.catalogs()
        typed = self.typed_toward
        return self.orienter.orient(
            layers, self.match_crs.isChecked(), self.orientation_box.currentData() or "north",
            found_of=lambda layer: record_of_layer(layer, catalogs),
            typed_of=(lambda layer: typed(layer)) if typed is not None else (lambda layer: None))

    def _orienter_said(self, text: str) -> None:
        if text:
            self.status.setText(text)

    def _refresh_opened(self) -> None:
        """The ids 「画像 ID で開く」 offers: the images opened on this computer."""
        try:
            ids = list(dict.fromkeys(e.image_id for e in opened_images().items()))
        except Exception:  # noqa: BLE001 (the list unreadable: nothing offered)
            ids = []
        self._opened_model.setStringList(ids)

    def _catalog_of_url(self, url: str) -> Optional[CatalogConfig]:
        return next((c for c in self.catalogs() if c.url == url), None)

    def _fill_recent(self) -> None:
        """「最近開いた画像」: the last images opened here (of the catalogs of the settings file)."""
        self.recent_menu.clear()
        entries = [e for e in opened_images().recent(30) if self._catalog_of_url(e.catalog_url) is not None][:20]
        if not entries:
            action = self.recent_menu.addAction(self.tr("まだなし（画像を開くとここに表示）"))
            action.setEnabled(False)
            return
        for e in entries:
            when = local_time(e.acquired)
            files = f"（{len(e.opened)} ファイル）" if len(e.opened) > 1 else ""
            action = self.recent_menu.addAction(" ".join(x for x in (e.image_id, when, e.sensor) if x) + files)
            action.setToolTip(e.place)
            action.triggered.connect(lambda _on=False, e=e: self.open_again(e))

    def open_image_id(self, text: str, then: Optional[Callable[[list], None]] = None,
                      missing: Optional[Callable[[], None]] = None) -> None:
        """
        「画像 ID で開く」: an image opened before opens with the files of last
        time (after a look, open_again); else the catalogs are asked for the id
        (in a task) and it opens as from the list. `then` gets the layers
        opened ([] for an image recorded without its file); `missing` is called
        when no catalog has it.
        """
        words = [w for w in (text or "").replace("、", " ").replace(",", " ").split() if w]
        if not words:
            return
        image_id = words[0]
        if len(words) > 1:
            self.status.setText(self.tr("画像 ID は 1 つずつ（{id} を表示）").format(id=image_id))
        entry = next((e for e in opened_images().items() if e.image_id == image_id and self._catalog_of_url(e.catalog_url)), None)
        if entry is not None:
            self.open_again(entry, then, missing)
            return
        self.open_from_catalogs(image_id, then, missing)

    def open_again(self, entry: OpenedImage, then: Optional[Callable[[list], None]] = None,
                   missing: Optional[Callable[[], None]] = None) -> None:
        """An image of 開いた画像の履歴 opened with the files of last time, after asking (or looked up again in the catalog)."""
        catalog = self._catalog_of_url(entry.catalog_url)
        if catalog is None:
            self.open_from_catalogs(entry.image_id, then, missing)
            return
        answer = open_again_dialog.ask_open_again(self, entry)
        if answer is None:
            return
        if answer == open_again_dialog.FROM_CATALOG:
            self.open_from_catalogs(entry.image_id, then, missing)
            return
        files = list(answer)
        if entry.missing(files if entry.files else None):
            self.status.setText(self.tr("{id} の前回のファイルが見つからないので、画像カタログで検索").format(id=entry.image_id))
            self.open_from_catalogs(entry.image_id, then, missing)
            return
        record = record_of(entry.feature, catalog.fields)
        if not record.id:
            record.id = entry.image_id
        problems: list[str] = []
        try:
            layers = open_located(record, catalog, located_again(entry), files or None, problems)
        except PathError as error:
            self.status.setText(str(error))
            self.iface.messageBar().pushWarning(TITLE, plain_tip(str(error)))
            return
        layers, there = add_or_choose(layers, record)
        overview_queue.request(layers, self.context.config.overviews, self.iface.messageBar())
        layers += there
        note = self._opened(layers)
        if there and len(there) == len(layers):
            text = self.tr("{id} は開いている画像。そのレイヤーを選択").format(id=entry.image_id)
        else:
            text = problems[-1] if problems else self.tr("{id} を前回のファイルで表示（{n} ファイル）").format(id=entry.image_id, n=len(layers))
        self.status.setText(text + ("\n" + note if note and not problems else ""))
        if then is not None:
            then(layers)

    def open_from_catalogs(self, image_id: str, then: Optional[Callable[[list], None]] = None,
                           missing: Optional[Callable[[], None]] = None) -> None:
        """The catalogs (the one chosen first) asked for an image id in a task, the image opened as from the list."""
        chosen = self.catalog()
        catalogs = sorted((c for c in self.catalogs() if c.fields.get("id")), key=lambda c: c is not chosen)
        if not catalogs:
            self.status.setText(self.tr("画像 ID で探せる画像カタログがない（管理者に連絡: fields.id）［E703］"))
            if missing is not None:
                missing()
            return
        client = self.context.esri()
        asks = [(c, self.context.token_for(c.url, c.token)) for c in catalogs]
        self.status.setText(self.tr("画像カタログで {id} を検索中…").format(id=image_id))

        def work(_task):
            for c, token in asks:
                found = records_by_ids(client, c, [image_id], token)
                if found:
                    return c, found[0]
            return None

        def done(result) -> None:
            if result is None:
                self.status.setText(self.tr("画像カタログに画像 ID「{id}」の画像なし").format(id=image_id))
                if missing is not None:
                    missing()
                return
            catalog, record = result
            if not catalog.image_files:
                entry = self.record_without_files([record])
                if then is not None and entry is not None:
                    then([])
                return
            layers = self.open_catalog_records(catalog, [record])
            if layers:
                note = getattr(self, "orient_note", "")
                self.status.setText(self.tr("{id} を表示（{n} ファイル）").format(id=image_id, n=len(layers)) + ("\n" + note if note else ""))
                if then is not None:
                    then(layers)

        def failed(message: str) -> None:
            self.status.setText(self.tr("画像カタログの検索に失敗: {m}").format(m=message))

        self._start(self.tr("画像 ID の検索"), work, done, failed)

    def show_image_ids(self, ids: list[str], title: str = "") -> None:
        """「画像カタログで表示」: the images of these ids listed (from the first catalog that has any of them; in a task)."""
        ids = [i for i in dict.fromkeys(ids) if i]
        chosen = self.catalog()
        catalogs = sorted((c for c in self.catalogs() if c.fields.get("id")), key=lambda c: c is not chosen)
        if not ids or not catalogs:
            self.status.setText(self.tr("画像 ID で探せる画像カタログがない（管理者に連絡: fields.id）［E703］") if ids else "")
            return
        client = self.context.esri()
        asks = [(c, self.context.token_for(c.url, c.token)) for c in catalogs]

        def work(_task):
            best = None
            for c, token in asks:
                found = records_by_ids(client, c, ids, token)
                if found and (best is None or len(found) > len(best[1])):
                    best = (c, found)
                if best is not None and len(best[1]) == len(ids):
                    break
            return best

        def done(result) -> None:
            if result is None:
                self.status.setText(self.tr("画像カタログに該当する画像なし（{n} 件の画像 ID）").format(n=len(ids)))
                return
            catalog, records = result
            self.show_records(catalog, records)
            rest = len(ids) - len(records)
            self.status.setText((title or self.tr("指定した画像")) + self.tr(" {n} 件").format(n=len(records)) +
                                (self.tr("（{m} 件はカタログになし）").format(m=rest) if rest else ""))
            self.show()
            self.raise_()

        self._start(self.tr("画像 ID の検索"), work, done)

    def _say_unopened(self, problems: list[str], unopened: list[CatalogRecord]) -> None:
        """Why images could not be opened, in the message bar; with 「ファイルなしで記録」 for those (their raw data elsewhere, say)."""
        bar = self.iface.messageBar()
        dock = self.observations() if self.observations else None
        # The ids and paths are the catalog's (others' text): shown as written, never as HTML.
        text = plain_tip(" / ".join(problems))
        if not unopened or dock is None:
            bar.pushWarning(TITLE, text)
            return
        widget = bar.createMessage(TITLE, text)
        button = QPushButton(self.tr("ファイルなしで記録"))
        button.setToolTip(self.tr("開けなかった画像 {n} 件を、ファイルなしで観測記録の画像一覧へ（ID・撮像日時・範囲はカタログから）").format(n=len(unopened)))

        def record() -> None:
            self.record_without_files(unopened)
            try:
                bar.popWidget(widget)
            except RuntimeError:  # closed meanwhile
                pass

        button.clicked.connect(record)
        widget.layout().addWidget(button)
        # One at a time (opening again does not stack them).
        before = getattr(self, "unopened_message", None)
        if before is not None and any(w is before for w in bar.items()):
            bar.popWidget(before)
        bar.pushWidget(widget, Qgis.MessageLevel.Warning, 0)
        #: The last such message (for scripts and tests).
        self.unopened_message = widget
