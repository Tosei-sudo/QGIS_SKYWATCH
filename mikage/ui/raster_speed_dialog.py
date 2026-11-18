"""
「地図の描画の速さ」 (設定ダイアログの「地図の描画」の「詳しく…」, and the
message bar's after QGIS's options were changed): what MIKAGE sets for
drawing images faster (qgis_io/raster_speed.py), turning it off and on,
MIKAGE の速い描画（試作, qgis_io/fast_draw.py) on and off, and what only a
person can do (core/raster_speed.MANUAL).
"""
from __future__ import annotations

import html
from typing import Callable, Iterable, Optional

from qgis.PyQt.QtCore import QCoreApplication, Qt
from qgis.PyQt.QtWidgets import (
    QAbstractItemView, QCheckBox, QDialog, QDialogButtonBox, QHeaderView, QLabel, QTableWidget, QTableWidgetItem, QTextBrowser,
    QVBoxLayout,
)

from ..core.raster_speed import MANUAL, RasterSpeedOptions
from ..qgis_io import fast_draw, raster_speed
from .fit import fit_text


def tr(text: str) -> str:
    return QCoreApplication.translate("SatelliteObservation", text)


def manual_html() -> str:
    """MANUAL as the dialog shows it: each title in bold, its text below."""
    return "".join(f"<p><b>{html.escape(title)}</b><br>{html.escape(text)}</p>" for title, text in MANUAL)


class RasterSpeedDialog(QDialog):
    def __init__(self, options: Callable[[], RasterSpeedOptions], canvases: Callable[[], Iterable] = lambda: (),
                 parent=None, fast: Optional[fast_draw.FastDraw] = None):
        """
        `options`: the settings file's `rasterSpeed` now; `canvases`: QGIS's map canvases (changed right away);
        `fast`: the plugin's 速い描画 (its switch is shown with it).
        """
        super().__init__(parent)
        fit_text(self)
        self._options = options
        self._canvases = canvases
        self._fast = fast
        self.setWindowTitle(tr("地図の描画の速さ"))
        self.resize(820, 640)
        layout = QVBoxLayout(self)
        intro = QLabel(tr(
            "大きな画像の地図の描画が速くなるように、MIKAGE は次の設定を使います。GDAL の 2 つはこの QGIS の中だけ"
            "（起動のたびに設定し、どこにも書かない。プラグインを止めると元に戻る）。QGIS のオプションは、QGIS の既定より"
            "遅い値になっているものだけを、このプロファイルで 1 回だけ変えます（QGIS の 設定 > オプション > レンダリング でも変えられる）。"))
        intro.setWordWrap(True)
        layout.addWidget(intro)
        self.use = QCheckBox(tr("描画を速くする設定を使う"))
        self.use.setToolTip(tr("外すと、GDAL の 2 つと MIKAGE が変えた QGIS のオプションを元に戻し、次の起動からも使わない"))
        layout.addWidget(self.use)
        self.table = QTableWidget(0, 3)
        self.table.setHorizontalHeaderLabels([tr("項目"), tr("今の値"), tr("状態")])
        self.table.verticalHeader().setVisible(False)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.setSelectionMode(QAbstractItemView.SelectionMode.NoSelection)
        self.table.setWordWrap(True)
        header = self.table.horizontalHeader()
        header.setSectionsMovable(False)
        header.setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(1, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(2, QHeaderView.ResizeMode.Stretch)
        layout.addWidget(self.table)
        self.status = QLabel()
        self.status.setWordWrap(True)
        layout.addWidget(self.status)
        self.fast = QCheckBox(tr("画像の色付けを MIKAGE が速く描く（試作）"))
        self.fast.setToolTip(tr(
            "QGIS の「マルチバンドカラー」「単バンドグレー」で描く 8・16 ビットの画像を、その設定のまま MIKAGE が同じ色で描く。"
            "外すと QGIS の描画に戻す"))
        self.fast_note = QLabel()
        self.fast_note.setWordWrap(True)
        layout.addWidget(self.fast)
        layout.addWidget(self.fast_note)
        self.fast.setVisible(fast is not None)
        self.fast_note.setVisible(fast is not None)
        layout.addWidget(QLabel(tr("手動で見直す設定（プラグインからは変えられないもの）")))
        self.manual = QTextBrowser()
        self.manual.setOpenExternalLinks(False)
        self.manual.setHtml(manual_html())
        layout.addWidget(self.manual, 1)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)
        self.refresh()
        self.use.toggled.connect(self._toggled)
        self.fast.toggled.connect(self._fast_toggled)

    def refresh(self) -> None:
        options = self._options()
        self.use.blockSignals(True)
        self.use.setChecked(options.enabled and not raster_speed.personal_off())
        self.use.blockSignals(False)
        self.use.setEnabled(options.enabled)
        if not options.enabled:
            self.use.setToolTip(tr("設定ファイルの rasterSpeed で止めている（管理者の設定）"))
        rows = raster_speed.rows(options)
        self.table.setRowCount(len(rows))
        for i, row in enumerate(rows):
            for j, text in enumerate((row.what, row.now, row.state)):
                item = QTableWidgetItem(text)
                item.setFlags(Qt.ItemFlag.ItemIsEnabled)
                self.table.setItem(i, j, item)
        self.table.resizeRowsToContents()
        # As tall as its rows (a handful): the room left goes to the list below.
        height = self.table.horizontalHeader().height() + 2 * self.table.frameWidth()
        height += sum(self.table.rowHeight(r) for r in range(self.table.rowCount()))
        self.table.setFixedHeight(height + 2)
        self.fast.blockSignals(True)
        self.fast.setChecked(fast_draw.on())
        self.fast.blockSignals(False)
        self.fast_note.setText(self._fast_text())

    def _fast_text(self) -> str:
        text = tr(
            "QGIS の色付け（引き伸ばし・no data・不透明度）を MIKAGE が表の引き当てで行います。色は QGIS と画素単位で同じで、"
            "大きな 16 ビット画像の 1 画面が約 0.12 秒から約 0.05 秒になります。QGIS のシンボロジの設定はそのまま使え、"
            "プロジェクトには QGIS の設定として保存されます（MIKAGE のない QGIS でも開ける）。このプロファイルだけの設定です。")
        if self._fast is not None and fast_draw.on():
            text += tr("今 MIKAGE が描いている画像: {n} 件。").format(n=self._fast.count())
        return text

    def _toggled(self, on: bool) -> None:
        canvases = list(self._canvases())
        if on:
            changed = raster_speed.turn_on(self._options(), canvases)
            self.status.setText(tr("使います。QGIS のオプションを {n} 件変えました。").format(n=len(changed)) if changed
                                else tr("使います（QGIS のオプションは変える必要なし）。"))
        else:
            back = raster_speed.turn_off(canvases)
            self.status.setText(tr("使いません。GDAL の設定と QGIS のオプション {n} 件を元に戻しました。").format(n=len(back)))
        self.refresh()

    def _fast_toggled(self, on: bool) -> None:
        fast_draw.set_on(on)
        if self._fast is None:
            return
        if on:
            n = self._fast.start()
            self.status.setText(tr("画像の色付けを MIKAGE が描きます（今の画像 {n} 件。これから開く画像も）。").format(n=n))
        else:
            self._fast.stop()
            self.status.setText(tr("画像の色付けを QGIS の描画に戻しました。"))
        self.fast_note.setText(self._fast_text())

    def set_fast(self, on: bool) -> str:
        """The 速い描画 checkbox as clicked (for scripts and tests); the status line after."""
        self.fast.setChecked(on)
        return self.status.text()

    def set_use(self, on: bool) -> Optional[str]:
        """The checkbox as clicked (for scripts and tests); the status line after."""
        self.use.setChecked(on)
        return self.status.text()
