"""
観測記録ドックの書き出しと連携 (ui/obs_dock, docs/architecture.md 段階7): the classification file, CSV and layers, and
the windows opened from the dock (alerts, export, import, handover, events, report). A part of
ObservationDock (ui/observation_dock.py): its methods as they were, on the dock's widgets and state.
"""
from __future__ import annotations

import json
from typing import Any, Callable, Optional

from qgis.core import QgsProject
from qgis.PyQt.QtWidgets import QFileDialog

from ...core.obs.api import csv_file_name, observations_csv, now_ms, Observation, observation_layers, nest
from ...qgis_io.obs_layers import observation_memory_layer
from ...qgis_io.tasks import error_text
from ..docks import save_text


class OutputsPart:
    """観測記録ドックの書き出しと連携."""

    def save_taxonomy_file(self) -> None:
        """The classification as `config.json`'s `observationVocabulary.taxonomy` writes it."""
        if self.book is None:
            return
        path, _ = QFileDialog.getSaveFileName(self, self.tr("分類体系の JSON 保存"), "observation-taxonomy.json", "JSON (*.json)")
        if not path:
            return
        failed = save_text(path, json.dumps(nest(self.book.taxonomy()), ensure_ascii=False, indent=2) + "\n", what="taxonomy-json")
        if failed:
            self.say(self.tr("分類体系の保存に失敗: {m}").format(m=failed))
            return
        self.say(self.tr("分類体系を保存: {p}").format(p=path))

    def save_taxonomy_table_file(self, path: Optional[str] = None) -> Optional[str]:
        """The classification as a table (CSV, one row per class; 「分類体系の取り込み」 reads it back). Where it was saved."""
        if self.book is None:
            return None
        from ...core.obs.api import taxonomy_import

        if not path:
            path, _ = QFileDialog.getSaveFileName(self, self.tr("分類体系の表に書き出し"), taxonomy_import.taxonomy_csv_name(now_ms()), "CSV (*.csv)")
        if not path:
            return None
        taxa = self.book.taxonomy()
        failed = save_text(path, taxonomy_import.taxonomy_csv(taxa, self.book.spec_fields), newline="", what="taxonomy-csv", count=len(taxa))
        if failed:
            self.say(self.tr("分類体系の書き出しに失敗: {m}").format(m=failed))
            return None
        self.say(self.tr("分類体系を表に書き出し（{n} 分類）: {p}").format(n=len(taxa), p=path))
        return path

    def open_taxonomy_import(self):
        """Opens 「分類体系の取り込み」 (a table into the chosen store's classification; one kept while the dock lives)."""
        from ..taxonomy_import_dialog import TaxonomyImportDialog

        if getattr(self, "_taxonomy_import", None) is None:
            self._taxonomy_import = TaxonomyImportDialog(self, self)
        else:
            self._taxonomy_import.reset()
        self._taxonomy_import.show()
        self._taxonomy_import.raise_()
        self._taxonomy_import.activateWindow()
        return self._taxonomy_import

    def load_taxonomy_file(self, path: Optional[str] = None) -> None:
        if self.book is None:
            return
        if not path:
            path, _ = QFileDialog.getOpenFileName(self, self.tr("分類体系の JSON 読み込み"), "", "JSON (*.json)")
        if not path:
            return
        try:
            with open(path, encoding="utf-8-sig") as f:
                value = json.load(f)
            items = value if isinstance(value, list) else value.get("taxonomy") if isinstance(value, dict) else None
            if not isinstance(items, list):
                raise ValueError("分類の配列なし")
        except (OSError, ValueError) as error:
            self.say(self.tr("分類の読み込みに失敗: {e}").format(e=error_text(error)))
            return
        self.do(self.tr("分類の読み込み"), lambda b: b.set_taxonomy(items))

    def save_csv(self, path: Optional[str] = None) -> Optional[str]:
        """Saves the records as CSV (the deleted ones too when shown)."""
        if self.read_now() is None:
            return None
        rows = self.book.records if self.deleted_check.isChecked() else self.book.living()
        if not rows:
            self.say(self.tr("記録なし"))
            return None
        if not path:
            path, _ = QFileDialog.getSaveFileName(self, self.tr("CSV 保存"), csv_file_name(now_ms()), "CSV (*.csv)")
        if not path:
            return None
        failed = save_text(path, observations_csv(rows), newline="", what="observations-csv", count=len(rows))
        if failed:
            self.say(self.tr("CSV の保存に失敗: {m}").format(m=failed))
            return None
        self.say(self.tr("CSV を保存: {p}").format(p=path))
        return path

    def to_layers(self) -> list:
        """Adds the records as a point layer, the checked areas as polygons and the flows as lines."""
        if self.read_now() is None:
            return []
        made = observation_layers(self.book.records, self.book.areas)
        if not made:
            self.say(self.tr("記録なし"))
            return []
        layers = [observation_memory_layer(m) for m in made]
        QgsProject.instance().addMapLayers(layers)
        self.say(self.tr("{n} をレイヤーとして追加").format(n="・".join(m.title for m in made)))
        return layers

    def open_alerts(self, tab: int = 0):
        """Opens 「通報」 (its tab 0 通報文の作成, 1 通報記録); one kept while the dock lives."""
        from ..alert_dialog import AlertDialog

        if self._alerts is None:
            self._alerts = AlertDialog(self, self)
            self.context.configChanged.connect(self._alerts.reset)
        self._alerts.tabs.setCurrentIndex(tab)
        self._alerts.show()
        self._alerts.raise_()
        self._alerts.activateWindow()
        return self._alerts

    def open_export(self):
        """Opens 「観測記録のエクスポート」 (one kept while the dock lives; not modal, so the map can be drawn on)."""
        from ..obs_export_dialog import ExportDialog

        if self._export is None:
            self._export = ExportDialog(self, self)
            self.context.configChanged.connect(self._export.reset)
        else:
            self._export.reset()
        self._export.count()
        self._export.show()
        self._export.raise_()
        self._export.activateWindow()
        return self._export

    def open_import(self):
        """Opens 「観測記録の一括取込み」 (a CSV or a layer into the chosen store; one kept while the dock lives)."""
        from ..record_import_dialog import RecordImportDialog

        if getattr(self, "_import", None) is None:
            self._import = RecordImportDialog(self, self)
        else:
            self._import.reset()
        self._import.show()
        self._import.raise_()
        self._import.activateWindow()
        return self._import

    def open_handover(self, tab: int = 0):
        """Opens 「分担と引き継ぎ」 (its tab 0 分担, 1 引き継ぎメモ); one kept while the dock lives."""
        from ..handover_dialog import HandoverDialog

        if self._handover is None:
            self._handover = HandoverDialog(self, self)
            self.context.configChanged.connect(self._handover.reset)
        if tab == 0 and self.assignment_bar.source is None:
            tab = 1
        self._handover.tabs.setCurrentIndex(tab)
        if tab == 0:
            self.assignment_bar.reload()
        self._handover.show()
        self._handover.raise_()
        self._handover.activateWindow()
        return self._handover

    #: The 「イベント」 dock (plugin.py sets it).
    event_linker: Optional[Callable[[], Any]] = None

    def event_items(self, records: Optional[list[Observation]] = None) -> list:
        """The chosen records as items of an event."""
        from ...core.events import LinkItem

        url = self.store_set.config.url if self.store_set and self.store_set.config else "local"
        out = []
        for o in records if records is not None else self.selected_records():
            kind = o.kind.split(">")[-1].strip() if o.kind else ""
            label = " / ".join(x for x in (o.object_name or o.object_id, kind, o.activity, o.image_name or o.image_id) if x)
            out.append(LinkItem("observation", o.id, label=label, time=o.image_time, source=url))
        return out

    def image_event_items(self) -> list:
        """The image shown as an item of an event: from the catalog by its id, else by its path (`listed` without its file)."""
        from ...core.events import LISTED, LinkItem
        from ...qgis_io.catalog_io import CATALOG_URL
        from ...qgis_io.obs_images import image_path

        image = self.book.current if self.book else None
        if image is None:
            return []
        layer = QgsProject.instance().mapLayer(image.layer_id) if image.layer_id else None
        if layer is not None and layer.customProperty(CATALOG_URL):
            source = str(layer.customProperty(CATALOG_URL))
        elif layer is not None:
            source = image_path(layer) or layer.name()
        else:
            source = LISTED
        return [LinkItem("image", image.id, label=image.name or image.id, time=image.time, source=source)]

    def add_image_to_event(self):
        """「⚑」: the image shown into an open event (a menu of them)."""
        events = self.event_linker() if self.event_linker else None
        return events.offer(self.image_event_button, self.image_event_items) if events and self.image_event_items() else None

    def add_to_event(self):
        """「イベントに追加」: the chosen records into an open event (a menu of them)."""
        events = self.event_linker() if self.event_linker else None
        return events.offer(self.event_add_button, self.event_items) if events and self.selected_records() else None

    #: The 「参考情報」 dock (plugin.py sets it): records and things linked to infos, and their infos listed.
    references: Optional[Callable[[], Any]] = None

    def reference_targets(self, type_: str, records: Optional[list[Observation]] = None) -> list:
        """The chosen records (`observation`), or their things each once (`thing`), as items to link infos to."""
        from ...core.reference_info import OBSERVATION, LinkTarget

        out: dict[str, Any] = {}
        for o in records if records is not None else self.selected_records():
            kind = o.kind.split(">")[-1].strip() if o.kind else ""
            if type_ == OBSERVATION:
                label = " / ".join(x for x in (o.object_name or o.object_id, kind, o.activity, o.image_name or o.image_id) if x)
                out.setdefault(o.id, LinkTarget(OBSERVATION, o.id, label))
            elif o.object_id:
                out.setdefault(o.object_id, LinkTarget(type_, o.object_id, " / ".join(x for x in (o.object_name or o.object_id, kind) if x)))
        return list(out.values())

    def reference_menu(self, at=None):
        """「参考情報 ▾」 (and the list's right-click): link the records or their things to infos, or list theirs."""
        from qgis.PyQt.QtCore import QPoint
        from qgis.PyQt.QtWidgets import QMenu

        from ...core.reference_info import OBSERVATION, THING

        dock = self.references() if self.references else None
        records = self.selected_records()
        if dock is None or not records or not dock.can_link():
            return None
        menu = QMenu(self)
        notes = self.reference_targets(OBSERVATION, records)
        things = self.reference_targets(THING, records)
        menu.addAction(self.tr("記録を参考情報に紐付け…（{n} 件）").format(n=len(notes))).triggered.connect(
            lambda: dock.offer_link(notes, self))
        thing = menu.addAction(self.tr("モノを参考情報に紐付け…（{n} 件）").format(n=len(things)))
        thing.triggered.connect(lambda: dock.offer_link(things, self))
        thing.setEnabled(bool(things))
        menu.addSeparator()
        items = [(OBSERVATION, t.item_id) for t in notes] + [(THING, t.item_id) for t in things]
        what = self.tr("記録「{n}」・そのモノ").format(n=notes[0].label) if len(notes) == 1 else self.tr("記録 {n} 件・そのモノ").format(n=len(notes))
        menu.addAction(self.tr("この記録・モノの参考情報")).triggered.connect(lambda: dock.show_for_items(items, what))
        anchor = self.reference_button
        menu.popup(at if at is not None else anchor.mapToGlobal(QPoint(0, anchor.height())))
        self._reference_menu = menu
        return menu

    def add_to_alerts(self, records: Optional[list[Observation]] = None):
        """「通報に追加」: the chosen records (or `records`) into 「通報」, whatever the conditions say."""
        records = records if records is not None else self.selected_records()
        dialog = self.open_alerts(0)
        dialog.add_records(records)
        return dialog

    def open_report(self) -> None:
        if self.book is None:
            return
        from ..obs_report_dialog import ReportDialog

        ReportDialog(self, self.book, self.canvas, self).exec()
