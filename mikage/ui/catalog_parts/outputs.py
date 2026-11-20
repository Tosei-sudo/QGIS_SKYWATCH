"""
画像カタログドックの書き出しと受け渡し (ui/catalog_parts, docs/architecture.md): the chosen images to an event, their paths copied,
their folders, the list as a layer, a watch. A part of CatalogDock (ui/catalog_dock.py): its methods as they were,
on the dock's widgets and state.
"""
from __future__ import annotations

import os

from qgis.core import QgsProject
from qgis.PyQt.QtWidgets import QApplication

from ...core import audit
from ...core.catalog.api import PathError
from ...qgis_io.catalog_io import Cancelled, RecordFolder, record_folder, results_layer
from .common import TITLE


class OutputsPart:
    """画像カタログドックの書き出しと受け渡し."""

    def event_items(self, records=None) -> list:
        """The chosen images as items of an event."""
        from ...core.events import LinkItem
        from ...core.util import ms_of

        catalog = self._shown_catalog
        if catalog is None:
            return []
        return [LinkItem("image", r.id, label=" ".join(x for x in (r.id, r.sensor) if x), time=ms_of(r.acquired), source=catalog.url)
                for r in (records if records is not None else self.chosen()) if r.id]

    def add_to_event(self):
        """「イベントに追加」: the chosen images into an open event (a menu of them)."""
        if not self.event_items():
            self.status.setText(self.tr("一覧で画像を選択（Ctrl・Shift + クリックで複数）"))
            return None
        events = self.event_linker() if self.event_linker else None
        return events.offer(self.buttons["event"], self.event_items) if events else None

    def copy_paths(self) -> None:
        """Copies where the chosen images are (their URL or path), one per line."""
        paths = [r.source.value for r in self.chosen() if r.source]
        if not paths:
            self.status.setText(self.tr("画像の場所なし"))
            return
        QApplication.clipboard().setText("\n".join(paths))
        audit.record("copy", "image-paths", count=len(paths), ids=[r.id for r in self.chosen() if r.source])
        self.status.setText(self.tr("パス {n} 件をコピー").format(n=len(paths)))

    def open_folders(self, records=None) -> list[RecordFolder]:
        """
        「フォルダを開く」 (tsudo, 2026-11-19, instead of 「フォルダの対応を消去」): the
        folders of the chosen images (`records`) in Explorer, after the rewrites
        of `pathMappings` when the rewritten place is there (else the path as
        written; catalog_io.record_folder), an image file selected in its
        folder. Each folder once, at most MAX_FOLDERS. The folders shown.
        """
        records = self.chosen() if records is None else list(records)
        if not records:
            self.status.setText(self.tr("一覧で画像を選択（Ctrl・Shift + クリックで複数）"))
            return []
        shown: list[RecordFolder] = []
        ids: list[str] = []
        problems: list[str] = []
        more = False
        for record in records:
            try:
                where = record_folder(record, self.context.config.path_mappings, self.choose_folder)
            except Cancelled:
                break
            except PathError as error:
                problems.append(str(error))
                continue
            if any(os.path.normcase(s.folder) == os.path.normcase(where.folder) for s in shown):
                continue
            if len(shown) == self.MAX_FOLDERS:
                more = True
                break
            if not self.show_folder(where.folder, where.file):
                problems.append(self.tr("フォルダ「{f}」を開けず").format(f=where.folder))
                continue
            shown.append(where)
            ids.append(record.id)
        if shown:
            audit.record("open", "image-folder", count=len(shown), ids=ids, detail={"folders": [s.folder for s in shown]})
        if len(shown) == 1:
            text = self.tr("フォルダを開く: {f}").format(f=shown[0].folder) + (self.tr("（読み替え先）") if shown[0].rewritten else "")
        elif shown:
            text = self.tr("フォルダ {n} 件を開く").format(n=len(shown)) + (self.tr("（先頭の {m} 件）").format(m=self.MAX_FOLDERS) if more else "")
        else:
            text = ""
        if problems:
            text = "　".join(t for t in (text, problems[-1] if len(problems) == 1 else
                                          self.tr("{n} 件は開けず（{p}）").format(n=len(problems), p=problems[-1])) if t)
        if text:
            self.status.setText(text)
        return shown

    def add_layer(self):
        """The list as a layer of footprints (every attribute; the temporal controller on 撮像日時)."""
        catalog = self._shown_catalog
        if catalog is None or not self.model.records:
            return None
        info = self.layers.get(catalog.url)
        name = f"{TITLE} {catalog.label}" if self.reference is None else f"{TITLE} {catalog.label} 類似画像"
        layer = results_layer(self.model.records, catalog, info.types if info else {}, name)
        QgsProject.instance().addMapLayer(layer)
        return layer

    def _watch(self) -> None:
        catalog = self.catalog()
        if catalog is None or self.watch_requested is None:
            return
        # A watch keeps the sensor, angle, azimuth, SQL and free word (not the days).
        problem = self.input_problem(days=False)
        if problem:
            self.status.setText(problem)
            return
        # The area searched is offered first (a layer's shape is copied into the watch when it is set).
        kind = self.area_box.currentData()
        # A saved area: watched as an area drawn (the watch keeps its own copy).
        drawn = self.drawn_shape()
        kind = "drawn" if kind == "saved" and drawn else kind
        where = {"area": kind, **({"layer": self.area_layer.currentLayer(), "selected_only": self.selected_only.isChecked()}
                                  if kind == "layer" and self.area_layer.currentLayer() is not None else {})}
        info = self.layers.get(catalog.url)
        if info is None:
            client = self.context.esri()
            token = self.context.token_for(catalog.url, catalog.token)
            self._start(self.tr("画像カタログの確認"), lambda task: self._layer_of(client, catalog, token),
                        lambda info: self.watch_requested(catalog, info.types, self.conditions(), drawn, **where))
            return
        self.watch_requested(catalog, info.types, self.conditions(), drawn, **where)
