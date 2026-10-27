"""
The observation report (報告書): its period, class, 確度 and parts chosen,
then saved as one HTML file (the map inside it) or printed (a PDF too).

The map is the canvas's layers drawn over the records' extent
(QgsMapRendererParallelJob, off the main thread), with the records' marks,
each thing's track and the checked areas painted on it, as the viewer's.
"""
from __future__ import annotations

import base64
import re
from typing import Callable, Optional

from qgis.core import QgsMapRendererParallelJob, QgsMapSettings, QgsPointXY, QgsRectangle
from qgis.PyQt.QtCore import QBuffer, QByteArray, QDate, QDateTime, QIODevice, QPointF, QSize, Qt, QTime, QUrl
from qgis.PyQt.QtGui import QImage, QPainter, QTextDocument
from qgis.PyQt.QtWidgets import (
    QCheckBox, QComboBox, QDateEdit, QDialog, QFileDialog, QFormLayout, QGroupBox, QHBoxLayout, QLabel, QLineEdit, QPushButton,
    QVBoxLayout,
)

from ..core.obs.areas import CheckedArea, area_label
from ..core.obs.book import ObservationBook
from ..core.obs.flows import flows, now_ms
from ..core.obs.model import Observation
from ..core.obs.report import ReportOptions, ReportSelection, report_file_name
from ..core.obs.taxonomy import kind_of, path_of
from ..qgis_io.geometry import from_wgs84
from .obs_overlay import Mark, Scene, ScenePainter, color_of

#: The map's size in the report (pixels).
MAP_SIZE = QSize(1400, 900)
#: Where the printed page finds the map.
MAP_RESOURCE = "satobs-report-map"


def report_scene(records: list[Observation], areas: list[CheckedArea], kinds: list[str]) -> Scene:
    """The report map's marks: the latest record of each thing named, its earlier ones as steps, its track, the areas."""
    s = Scene(areas=[(a.ring, area_label(a)) for a in areas])
    for flow in flows(records):
        seen = [o for o in flow.observations if o.present]
        if len(seen) > 1:
            s.tracks.append([(o.lon, o.lat) for o in seen])
        last = flow.observations[-1]
        for o in flow.observations:
            if o is last:
                s.marks.append(Mark("obs", o.lon, o.lat, color_of((path_of(o.kind) or [""])[0], kinds), o.object_name, o.present))
            else:
                s.marks.append(Mark("step", o.lon, o.lat, present=o.present))
    return s


def _extent(records: list[Observation], areas: list[CheckedArea]) -> Optional[QgsRectangle]:
    points = [(o.lon, o.lat) for o in records] + [tuple(p[:2]) for a in areas for p in a.ring]
    if not points:
        return None
    lons = [p[0] for p in points]
    lats = [p[1] for p in points]
    return QgsRectangle(min(lons), min(lats), max(lons), max(lats))


class ReportMap:
    """Draws a report's map, then calls `done(data_url, image)` (None, None when nothing to draw or it failed)."""

    def __init__(self, canvas, records: list[Observation], areas: list[CheckedArea], kinds: list[str],
                 done: Callable[[Optional[str], Optional[QImage]], None], say: Callable[[str], None]):
        self.canvas, self.records, self.areas, self.kinds, self.done, self.say = canvas, records, areas, kinds, done, say
        self.job: Optional[QgsMapRendererParallelJob] = None

    def start(self) -> None:
        box = _extent(self.records, self.areas)
        if box is None:
            self.done(None, None)
            return
        source = self.canvas.mapSettings()
        crs = source.destinationCrs()
        try:
            extent = from_wgs84(crs).transformBoundingBox(box)
        except Exception as error:  # noqa: BLE001
            self.say(f"報告書の地図の作成不可: {error}")
            self.done(None, None)
            return
        # As the viewer: a margin of 8 % (at least 200 m).
        metres = crs.mapUnits() == 0  # QgsUnitTypes.DistanceMeters
        least = 200 if metres else 200 / 111_320
        pad = max(least, extent.width() * 0.08, extent.height() * 0.08)
        extent = extent.buffered(pad)
        settings = QgsMapSettings(source)
        settings.setOutputSize(MAP_SIZE)
        settings.setExtent(extent)
        settings.setLayers(source.layers())
        self.settings = settings
        self.job = QgsMapRendererParallelJob(settings)
        self.job.finished.connect(self._finished)
        self.job.start()

    def _finished(self) -> None:
        job, self.job = self.job, None
        if job is None:
            return
        image = job.renderedImage()
        if image.isNull():
            self.say("報告書の地図の作成不可: 描画失敗")
            self.done(None, None)
            return
        settings = self.settings
        to_map = from_wgs84(settings.destinationCrs())
        pixels = settings.mapToPixel()

        def to_pixel(lon: float, lat: float) -> QPointF:
            try:
                p = pixels.transform(to_map.transform(QgsPointXY(lon, lat)))
            except Exception:  # noqa: BLE001
                return QPointF(-1e6, -1e6)
            return QPointF(p.x(), p.y())

        scene = report_scene(self.records, self.areas, self.kinds)
        painter = QPainter(image)
        ScenePainter(to_pixel, scale=1.4).paint(painter, scene)
        painter.end()
        data = QByteArray()
        buffer = QBuffer(data)
        buffer.open(QIODevice.OpenModeFlag.WriteOnly)
        image.save(buffer, "JPEG", 88)
        url = "data:image/jpeg;base64," + base64.b64encode(bytes(data)).decode("ascii")
        self.done(url, image)


class ReportDialog(QDialog):
    """「報告書」: what goes in, then HTML 保存・印刷・PDF 保存."""

    def __init__(self, dock, book: ObservationBook, canvas, parent=None):
        super().__init__(parent)
        self.dock, self.book, self.canvas = dock, book, canvas
        self.setWindowTitle(self.tr("報告書"))
        self._map: Optional[ReportMap] = None
        layout = QVBoxLayout(self)
        form = QFormLayout()
        self.title_input = QLineEdit(ReportOptions().title)
        form.addRow(self.tr("題名"), self.title_input)
        self.start_check, self.start_date = QCheckBox(self.tr("指定")), QDateEdit(QDate.currentDate())
        self.end_check, self.end_date = QCheckBox(self.tr("指定")), QDateEdit(QDate.currentDate())
        for check, date, label in ((self.start_check, self.start_date, self.tr("期間（から）")), (self.end_check, self.end_date, self.tr("期間（まで）"))):
            date.setCalendarPopup(True)
            date.setDisplayFormat("yyyy-MM-dd")
            date.setEnabled(False)
            check.toggled.connect(date.setEnabled)
            check.toggled.connect(lambda _on: self.count())
            date.dateChanged.connect(lambda _d: self.count())
            row = QHBoxLayout()
            row.addWidget(check)
            row.addWidget(date, 1)
            form.addRow(label, row)
        self.kind_select = QComboBox()
        self.kind_select.addItem(self.tr("すべて"), "")
        for t in book.taxonomy():
            self.kind_select.addItem(f"{'　' * (len(t.path) - 1)}{t.path[-1]}", kind_of(t.path))
        self.kind_select.currentIndexChanged.connect(lambda _i: self.count())
        form.addRow(self.tr("分類"), self.kind_select)
        self.confidence_select = QComboBox()
        self.confidence_select.addItem(self.tr("すべて"), "")
        levels = book.confidences()
        for i, c in enumerate(levels):
            self.confidence_select.addItem(c.label if i == len(levels) - 1 else f"{c.label} 以上", c.label)
        self.confidence_select.currentIndexChanged.connect(lambda _i: self.count())
        form.addRow(self.tr("確度"), self.confidence_select)
        layout.addLayout(form)
        parts = QGroupBox(self.tr("内容"))
        row = QHBoxLayout(parts)
        self.map_check = QCheckBox(self.tr("地図"))
        self.map_check.setToolTip(self.tr("記録の範囲の地図（記録の印・流れ・確認範囲）"))
        self.flows_check = QCheckBox(self.tr("モノごとの流れ"))
        self.areas_check = QCheckBox(self.tr("確認範囲"))
        self.records_check = QCheckBox(self.tr("記録一覧"))
        for c, on in ((self.map_check, True), (self.flows_check, True), (self.areas_check, True), (self.records_check, False)):
            c.setChecked(on)
            c.toggled.connect(lambda _on: self.count())
            row.addWidget(c)
        layout.addWidget(parts)
        self.count_label = QLabel()
        layout.addWidget(self.count_label)
        self.status = QLabel()
        self.status.setWordWrap(True)
        layout.addWidget(self.status)
        actions = QHBoxLayout()
        self.save_button = QPushButton(self.tr("HTML 保存"))
        self.save_button.setToolTip(self.tr("報告書の HTML 保存（地図を含む 1 ファイル）"))
        self.save_button.clicked.connect(lambda: self.make(self._save_html))
        self.print_button = QPushButton(self.tr("印刷"))
        self.print_button.setToolTip(self.tr("報告書の印刷（PDF 保存も可）"))
        self.print_button.clicked.connect(lambda: self.make(self._print))
        self.pdf_button = QPushButton(self.tr("PDF 保存"))
        self.pdf_button.clicked.connect(lambda: self.make(self._pdf))
        close = QPushButton(self.tr("閉じる"))
        close.clicked.connect(self.reject)
        for b in (self.save_button, self.print_button, self.pdf_button, close):
            b.setAutoDefault(False)
            actions.addWidget(b)
        layout.addLayout(actions)
        self.count()

    def options(self) -> ReportOptions:
        """What the form says."""
        def day(edit: QDateEdit, end: bool) -> int:
            t = QTime(23, 59, 59, 999) if end else QTime(0, 0)
            return QDateTime(edit.date(), t).toMSecsSinceEpoch()

        return ReportOptions(
            title=self.title_input.text().strip() or ReportOptions().title,
            start=day(self.start_date, False) if self.start_check.isChecked() else None,
            end=day(self.end_date, True) if self.end_check.isChecked() else None,
            kind=self.kind_select.currentData() or "", confidence=self.confidence_select.currentData() or "",
            map=self.map_check.isChecked(), flows=self.flows_check.isChecked(), areas=self.areas_check.isChecked(),
            records=self.records_check.isChecked(),
        )

    def count(self) -> None:
        o = self.options()
        s = self.book.report_selection(o)
        self.count_label.setText(f"モノ {len(s.flows)}・記録 {len(s.records)}・画像 {len(s.images)}{f'・確認範囲 {len(s.areas)}' if o.areas else ''}")

    def _say(self, message: str) -> None:
        self.status.setText(message)

    def make(self, then: Callable[[str, ReportOptions, Optional[QImage]], None]) -> None:
        """Makes the report (its map first, when asked), then saves or prints it."""
        o = self.options()
        selection: ReportSelection = self.book.report_selection(o)
        if not selection.records and not self.book.areas:
            self._say(self.tr("該当する記録なし"))
            return
        self._say(self.tr("報告書を作成中…"))
        for b in (self.save_button, self.print_button, self.pdf_button):
            b.setEnabled(False)

        def done(url: Optional[str], image: Optional[QImage]) -> None:
            for b in (self.save_button, self.print_button, self.pdf_button):
                b.setEnabled(True)
            self._map = None
            try:
                html = self.book.report(o, url)
            except Exception as error:  # noqa: BLE001
                self._say(self.tr("報告書の作成失敗: {e}").format(e=error))
                return
            then(html, o, image)

        if o.map and (selection.records or selection.areas):
            kinds = list(dict.fromkeys([*self.book.vocabulary.kinds, *(t.path[0] for t in self.book.taxonomy())]))
            self._map = ReportMap(self.canvas, selection.records, selection.areas if o.areas else [], kinds, done, self._say)
            self._map.start()
        else:
            done(None, None)

    def _save_html(self, html: str, o: ReportOptions, _image) -> None:
        path, _ = QFileDialog.getSaveFileName(self, self.tr("報告書の保存"), report_file_name(o.title, now_ms()), "HTML (*.html)")
        if not path:
            self._say("")
            return
        with open(path, "w", encoding="utf-8") as f:
            f.write(html)
        self._say(self.tr("報告書を保存: {p}").format(p=path))

    def document(self, html: str, image: Optional[QImage]) -> QTextDocument:
        """The report as a printable document (the map as a resource; Qt's HTML has no CSS grid, so the counts go one under another)."""
        doc = QTextDocument()
        if image is not None:
            doc.addResource(QTextDocument.ResourceType.ImageResource, QUrl(MAP_RESOURCE), image.scaledToWidth(680, Qt.TransformationMode.SmoothTransformation))
            html = re.sub(r'src="data:image/[^"]+"', f'src="{MAP_RESOURCE}"', html)
        doc.setHtml(html)
        return doc

    def _printer(self):
        from qgis.PyQt.QtPrintSupport import QPrinter

        printer = QPrinter(QPrinter.PrinterMode.HighResolution)
        return printer

    def _print(self, html: str, _o: ReportOptions, image: Optional[QImage]) -> None:
        from qgis.PyQt.QtPrintSupport import QPrintDialog

        printer = self._printer()
        dialog = QPrintDialog(printer, self)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            self._say("")
            return
        self.document(html, image).print(printer)
        self._say(self.tr("報告書を印刷"))

    def _pdf(self, html: str, o: ReportOptions, image: Optional[QImage], path: Optional[str] = None) -> None:
        from qgis.PyQt.QtPrintSupport import QPrinter

        if not path:
            path, _ = QFileDialog.getSaveFileName(self, self.tr("報告書の PDF 保存"), re.sub(r"\.html$", ".pdf", report_file_name(o.title, now_ms())), "PDF (*.pdf)")
        if not path:
            self._say("")
            return
        printer = self._printer()
        printer.setOutputFormat(QPrinter.OutputFormat.PdfFormat)
        printer.setOutputFileName(path)
        self.document(html, image).print(printer)
        self._say(self.tr("報告書を保存: {p}").format(p=path))
