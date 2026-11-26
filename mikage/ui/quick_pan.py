"""
「クイックパンシャープン」's switch (qgis_io/quick_pan.py does the drawing):
two images chosen in the layer panel (Ctrl+click), then the entry of the
menu (解析・処理) or of the layer panel's right-click, and the multispectral
one is drawn pan-sharpened where it shows. The same entry again, with either
layer chosen, ends it (画像補正's 「解除」 too). The menu entry is ticked while
a chosen layer is in it.

`QuickPanBox` is 画像補正's box for a layer in it: the pan, the method, the
strength and 「解除」.
"""
from __future__ import annotations

from typing import Optional

from qgis.core import QgsProject, QgsRasterLayer
from qgis.PyQt import sip
from qgis.PyQt.QtCore import QCoreApplication, QObject, Qt, QTimer
from qgis.PyQt.QtGui import QIcon
from qgis.PyQt.QtWidgets import QComboBox, QFormLayout, QGroupBox, QHBoxLayout, QLabel, QPushButton, QSlider

from ..qgis_io import quick_pan as qp

try:  # Qt6
    from qgis.PyQt.QtGui import QAction
except ImportError:  # Qt5
    from qgis.PyQt.QtWidgets import QAction

ENTRY = "クイックパンシャープン"
TIP = ("レイヤーパネルで選んだ 2 つの画像（パンクロとマルチスペクトル）を、地図に見えている範囲だけその場でパンシャープンして表示。"
       "ファイルは作らない。もう一度選ぶと解除")


def tr(text: str) -> str:
    return QCoreApplication.translate("Mikage", text)


class QuickPanSwitch(QObject):
    """The entry's work: start or end クイックパンシャープン for the layers chosen in the layer panel."""

    def __init__(self, iface, action: Optional[QAction] = None, parent: Optional[QObject] = None):
        super().__init__(parent)
        self.iface = iface
        self.action = action
        self.view = iface.layerTreeView() if hasattr(iface, "layerTreeView") else None
        self._connections: list = []
        if action is not None:
            action.setCheckable(True)
            action.setToolTip(tr(TIP))
            action.setStatusTip(tr(TIP))
            self._connect(action.triggered, lambda _on=False: self.toggle())
        if self.view is not None:
            self._connect(self.view.contextMenuAboutToShow, self._layer_menu)
            selection = self.view.selectionModel()
            if selection is not None:
                self._connect(selection.selectionChanged, lambda *_a: self.sync())
        self._connect(iface.currentLayerChanged, lambda *_a: self.sync())
        self._connect(qp.signals().changed, lambda *_a: self.sync())
        self.sync()

    def _connect(self, signal, slot) -> None:
        signal.connect(slot)
        self._connections.append((signal, slot))

    def close(self) -> None:
        for signal, slot in self._connections:
            try:
                signal.disconnect(slot)
            except (TypeError, RuntimeError):
                pass
        self._connections = []

    def deleteLater(self) -> None:  # noqa: N802 (Qt's name; features.base.close calls it)
        self.close()
        super().deleteLater()

    # ---- what is chosen ----

    def chosen(self) -> list:
        """The raster layers chosen in the layer panel (else the active one)."""
        layers = []
        if self.view is not None and not sip.isdeleted(self.view):
            layers = [layer for layer in self.view.selectedLayers() if isinstance(layer, QgsRasterLayer)]
        if not layers:
            active = self.iface.activeLayer()
            layers = [active] if isinstance(active, QgsRasterLayer) else []
        return layers

    @staticmethod
    def links_of(layers) -> list:
        """The links the layers are in (as the multispectral image or the pan), each once."""
        out = []
        for layer in layers:
            for link in (qp.link_of(layer), qp.used_as_pan(layer)):
                if link is not None and link not in out:
                    out.append(link)
        return out

    def sync(self) -> None:
        if self.action is not None and not sip.isdeleted(self.action):
            self.action.setChecked(bool(self.links_of(self.chosen())))

    # ---- turning it on and off ----

    def toggle(self, layers=None) -> bool:
        """Ends the links of the layers chosen, else starts one for the two chosen; True when one started."""
        layers = list(layers) if layers is not None else self.chosen()
        bar = self.iface.messageBar()
        links = self.links_of(layers)
        if links:
            for link in links:
                qp.end(link)
            bar.pushInfo(tr(ENTRY), tr("解除"))
            self.sync()
            return False
        if len(layers) != 2:
            bar.pushWarning(tr(ENTRY), tr("レイヤーパネルでパンクロとマルチスペクトルの画像を 2 つ選択（Ctrl+クリック）"))
            self.sync()
            return False
        try:
            pan, ms = qp.choose(*layers)
            qp.start(ms, pan)
        except qp.QuickPanError as error:
            bar.pushWarning(tr(ENTRY), str(error))
            self.sync()
            return False
        bar.pushInfo(tr(ENTRY), tr("「{ms}」を「{pan}」の細部で表示（地図に見えている範囲だけ・ファイルは作らない）。パンクロは非表示に")
                     .format(ms=ms.name(), pan=pan.name()))
        self.sync()
        return True

    def _layer_menu(self, menu) -> None:
        """The layer panel's right-click: the entry when two images are chosen, or a chosen one is in it (ticked)."""
        layers = self.chosen() if self.view is not None else []
        on = bool(self.links_of(layers))
        if not on and len(layers) != 2:
            return
        action = QAction(self.action.icon() if self.action is not None else QIcon(), tr(ENTRY), menu)
        action.setObjectName("mikageQuickPan")
        action.setCheckable(True)
        action.setChecked(on)
        action.setToolTip(tr(TIP))
        action.triggered.connect(lambda _on=False, chosen=layers: self.toggle(chosen))
        menu.addSeparator()
        menu.addAction(action)


#: The methods (short: the dock is narrow) and what the Processing algorithm says of them.
METHOD_LABELS = (("gram-schmidt", "Gram-Schmidt（推奨）", "色を最もよく維持"), ("ihs", "IHS", "最もくっきり。色が変わることも"),
                 ("brovey", "Brovey", "コントラスト強め"))
#: The pause after the strength is moved before drawing, ms.
STRENGTH_DELAY = 250


class QuickPanBox(QGroupBox):
    """
    画像補正's box for the layer it shows while that layer is in クイックパンシャープン:
    the pan, the method and strength, 「解除」. Hidden otherwise. The dock shows
    it again (`show_layer`) when the layer's link starts, changes or ends.
    """

    def __init__(self, parent=None):
        super().__init__(tr(ENTRY), parent)
        self.setObjectName("mikageQuickPanBox")
        self.layer: Optional[QgsRasterLayer] = None
        form = QFormLayout(self)
        self.label = QLabel()
        self.label.setWordWrap(True)
        self.label.setToolTip(tr("地図に見えている範囲だけを表示のたびに計算（ファイルは作らない）。"
                                 "マルチスペクトルの画素より粗い縮小表示では合成しない（違いが出ないため）"))
        form.addRow(self.label)
        self.method = QComboBox()
        self.method.setSizeAdjustPolicy(QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon)
        self.method.setMinimumContentsLength(8)
        for i, (key, label, tip) in enumerate(METHOD_LABELS):
            self.method.addItem(tr(label), key)
            self.method.setItemData(i, tr(tip), Qt.ItemDataRole.ToolTipRole)
        self.method.activated.connect(self._method_chosen)
        form.addRow(tr("方式"), self.method)
        row = QHBoxLayout()
        self.strength = QSlider(Qt.Orientation.Horizontal)
        self.strength.setRange(0, 200)
        self.strength.setSingleStep(5)
        self.strength.setPageStep(25)
        self.strength.setToolTip(tr("100 % で標準、0 % でマルチスペクトルの再サンプリングのみ"))
        self.strength_text = QLabel()
        self.strength_text.setMinimumWidth(QLabel("200 %").sizeHint().width())
        self.strength.setMinimumWidth(60)
        row.addWidget(self.strength, 1)
        row.addWidget(self.strength_text)
        form.addRow(tr("強さ"), row)
        self.off = QPushButton(tr("解除"))
        self.off.setToolTip(tr("マルチスペクトルの画像を元の表示に戻し、パンクロの画像を表示に戻す"))
        self.off.clicked.connect(self._off)
        form.addRow(self.off)
        self._timer = QTimer(self)
        self._timer.setSingleShot(True)
        self._timer.setInterval(STRENGTH_DELAY)
        self._timer.timeout.connect(self._strength_chosen)
        self.strength.valueChanged.connect(self._strength_moved)
        self._loading = False
        self.hide()

    def close_down(self) -> None:
        self._timer.stop()

    def link(self):
        layer = self.layer
        if layer is None or sip.isdeleted(layer):
            return None
        return qp.link_of(layer) or qp.used_as_pan(layer)

    def show_layer(self, layer) -> None:
        """Shows the box for `layer` (None: a group, or nothing chosen)."""
        if self._timer.isActive():
            self._strength_chosen()
        self.layer = layer if isinstance(layer, QgsRasterLayer) else None
        self._show()

    def _show(self) -> None:
        link = self.link()
        self.setVisible(link is not None)
        if link is None:
            return
        project_layer = QgsProject.instance().mapLayer
        ms, pan = project_layer(link.ms_id), project_layer(link.pan_id)
        is_pan = self.layer.id() == link.pan_id
        if is_pan:
            lines = [tr("「{ms}」のパンクロとして使用中（非表示）").format(ms=ms.name() if ms is not None else "?")]
        else:
            lines = [tr("パンクロ: {pan}").format(pan=pan.name() if pan is not None else "?")]
        if link.fitting and link.model is not None:
            lines.append(tr("色の関係を調べ直し中…（終わるまで前の結果で表示）"))
        elif link.fitting:
            lines.append(tr("色の関係を調査中…（終わるまで元の画像のまま）"))
        elif link.failed:
            lines.append(tr("色の関係の調査に失敗: {reason}").format(reason=link.failed))
        self.label.setText("\n".join(lines))
        self._loading = True
        try:
            index = self.method.findData(link.method)
            self.method.setCurrentIndex(max(0, index))
            self.strength.setValue(round(link.strength * 100))
            self.strength_text.setText(f"{self.strength.value()} %")
        finally:
            self._loading = False
        for w in (self.method, self.strength):
            w.setEnabled(not is_pan)

    def _method_chosen(self, _index: int) -> None:
        link = self.link()
        if link is not None and not self._loading:
            qp.set_options(QgsProject.instance().mapLayer(link.ms_id), method=self.method.currentData())

    def _strength_moved(self, value: int) -> None:
        self.strength_text.setText(f"{value} %")
        if not self._loading:
            self._timer.start()

    def _strength_chosen(self) -> None:
        self._timer.stop()
        link = self.link()
        if link is not None:
            qp.set_options(QgsProject.instance().mapLayer(link.ms_id), strength=self.strength.value() / 100)

    def _off(self) -> None:
        link = self.link()
        if link is not None:
            qp.end(link)
