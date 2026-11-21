"""
画像カタログドックの検索条件 (ui/catalog_parts, docs/architecture.md): the form: the catalog chosen and what it offers, the
periods, the conditions typed and what is wrong with them. A part of CatalogDock (ui/catalog_dock.py): its methods
as they were, on the dock's widgets and state.
"""
from __future__ import annotations

from dataclasses import replace
from typing import Optional

from qgis.PyQt.QtWidgets import QHBoxLayout, QLabel, QLineEdit, QWidget

from ...core.catalog.api import CatalogConfig, CatalogSearch, sensors_of
from ...core.util import read_day_time, read_number
from ...qgis_io.geometry import to_wgs84
from .common import _number, _is_clock, _period, PERIOD_TIP


class ConditionsPart:
    """画像カタログドックの検索条件."""

    def _period_row(self, a: QLineEdit, b: QLineEdit) -> QWidget:
        """A period's two boxes: a day, or a day and a time (PERIOD_TIP)."""
        for box in (a, b):
            box.setToolTip(self.tr(PERIOD_TIP))
        return self._range(a, b, "YYYY-MM-DD [hh:mm]", "YYYY-MM-DD [hh:mm]")

    def _range(self, a: QLineEdit, b: QLineEdit, pa: str = "YYYY-MM-DD", pb: str = "YYYY-MM-DD") -> QWidget:
        a.setPlaceholderText(pa)
        b.setPlaceholderText(pb)
        w = QWidget()
        h = QHBoxLayout(w)
        h.setContentsMargins(0, 0, 0, 0)
        h.addWidget(a)
        h.addWidget(QLabel("〜"))
        h.addWidget(b)
        return w

    def _catalog_changed(self) -> None:
        self.sensor_box.clear()
        self.sensor_box.addItem("")
        catalog = self.catalog()
        self._show_keyword(catalog)
        self._show_file_buttons()
        if catalog is None or not catalog.fields.get("sensor"):
            return
        client = self.context.esri()
        token = self.context.token_for(catalog.url, catalog.token)
        self._start(self.tr("センサーの取得"), lambda task: sensors_of(client, catalog, token),
                    lambda sensors: self.sensor_box.addItems(sensors) if self.catalog() is catalog else None, lambda m: None)

    def has_files(self) -> bool:
        """Whether the list's catalog (else the one chosen) has image files to open (not `imageFiles: false`)."""
        catalog = self._shown_catalog or self.catalog()
        return catalog is None or catalog.image_files

    def _show_file_buttons(self) -> None:
        """A catalog without image files: 「ファイルなしで記録」 instead of 「開く」, and no paths to copy or folders to open."""
        files = self.has_files()
        for key in ("open", "copy", "folder"):
            self.buttons[key].setVisible(files)
        self.close_previous.setVisible(files)
        self.zoom_opened.setVisible(files)
        self.buttons["record"].setVisible(not files and self.allows("catalog.record"))

    def _show_keyword(self, catalog: Optional[CatalogConfig]) -> None:
        """「フリーワード」 only for a catalog with an image id or `fields.keywords`, its hint naming them."""
        fields = catalog.keyword_fields if catalog is not None else []
        self.keyword_label.setVisible(bool(fields))
        self.keyword.setVisible(bool(fields))
        if not fields:
            return
        labels = "・".join(catalog.keyword_labels())
        self.keyword.setPlaceholderText(self.tr("{labels} に部分一致").format(labels=labels))
        self.keyword.setToolTip(self.tr(
            "{labels} のどれかに入力した語を含む画像を検索。空白で区切ると、すべての語を含む画像。"
            "大文字・小文字、ひらがな・カタカナ、全角・半角は区別しない。Enter で検索").format(labels=labels))

    def conditions(self) -> CatalogSearch:
        """The search the form describes (詳細条件 only while it is open; the free word only when the catalog has keyword fields)."""
        catalog = self.catalog()
        keyword = self.keyword.text().strip() if catalog is not None and catalog.keyword_fields else ""
        acquired = _period(self.acquired_from, self.acquired_to)
        search = CatalogSearch(acquired_from=acquired[0], acquired_to=acquired[1], keyword=keyword)
        if not self.more.isChecked():
            return search
        registered, system = _period(self.registered_from, self.registered_to), _period(self.system_from, self.system_to)
        return replace(search, registered_from=registered[0], registered_to=registered[1],
                       system_registered_from=system[0], system_registered_to=system[1],
                       sensor=self.sensor_box.currentText().strip(), max_angle=_number(self.max_angle),
                       azimuth_from=_number(self.azimuth_from), azimuth_to=_number(self.azimuth_to), where=self.where.text().strip())

    def input_problem(self, days: bool = True) -> Optional[str]:
        """
        Why a typed condition can't be read (a day such as 2026-02-30, a time
        such as 25:00, an angle with letters), or None. The search stops and
        says so, rather than quietly asking without that condition (the whole
        period, every angle).
        """
        periods = [(self.tr("撮像日時"), self.acquired_from, self.acquired_to)] if days else []
        numbers = []
        if self.more.isChecked():
            if days:
                periods += [(self.tr("登録日時"), self.registered_from, self.registered_to),
                            (self.tr("システム登録日時"), self.system_from, self.system_to)]
            numbers += [(self.tr("角度"), self.max_angle)]
            numbers += [(self.tr("方位角"), e) for e in (self.azimuth_from, self.azimuth_to)]

        def unread(label: str, edit: QLineEdit, kind: str) -> str:
            return self.tr("{label}の「{text}」を読めません（{kind}）").format(label=label, text=edit.text().strip(), kind=kind)

        for label, start, end in periods:
            try:
                first = read_day_time(start.text())
            except ValueError:
                return unread(label, start, self.tr("日付は 2026-11-09、時刻もなら 2026-11-09 10:30 の形で"))
            try:
                read_day_time(end.text(), first[:10])
            except ValueError:
                alone = not first and bool(end.text().strip()) and _is_clock(end.text())
                kind = (self.tr("時刻だけなら左に日付を") if alone
                        else self.tr("日付は 2026-11-09、時刻もなら 2026-11-09 10:30 の形で"))
                return unread(label, end, kind)
        for label, edit in numbers:
            try:
                read_number(edit.text())
            except ValueError:
                return unread(label, edit, self.tr("数で"))
        return None

    def extent(self) -> Optional[list[float]]:
        """The map's extent in WGS 84 ([xmin, ymin, xmax, ymax])."""
        box = to_wgs84(self.canvas.mapSettings().destinationCrs()).transformBoundingBox(self.canvas.extent())
        return [box.xMinimum(), box.yMinimum(), box.xMaximum(), box.yMaximum()]
