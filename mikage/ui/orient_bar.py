"""
「地図の向き・補正」 toolbar (tsudo 2026-11-26: 「よく使うと思うので、ドックだけじゃなくて
ツールバー？にも出せないかな。プラグインのまとまりじゃなくて、これ単体のまとまりで。
ついでに補正とDRAのチェックも合わせてあるととても良い」): a QGIS toolbar of its
own (not the MIKAGE one), shown and moved as any toolbar (QGIS keeps where
it is), with the 画像補正 dock's 「地図の向き」 buttons N 撮 衛 進 and its 「補正」
and DRA 「オン」 as two check boxes. It works through the dock (whether the
dock is open or not): the image worked on is the dock's (the active raster,
or the group chosen), the buttons and boxes show what the dock shows.
"""
from __future__ import annotations

from qgis.core import Qgis
from qgis.PyQt.QtCore import QObject
from qgis.PyQt.QtWidgets import QCheckBox, QToolBar

try:  # Qt6
    from qgis.PyQt.QtGui import QAction
except ImportError:  # Qt5
    from qgis.PyQt.QtWidgets import QAction

from ..core.map_view import ORIENTATIONS, ORIENTATION_LABELS
from .orient_icons import orient_icon

TITLE = "地図の向き・補正"


class OrientToolbar(QObject):
    """The toolbar; `dock`: the 画像補正 dock (its `orient_row`, `enabled`, `dra_on`)."""

    def __init__(self, iface, dock, parent=None):
        super().__init__(parent)
        self.iface = iface
        self.dock = dock
        self.row = dock.orient_row
        self.toolbar: QToolBar = iface.addToolBar(self.tr(TITLE))
        self.toolbar.setObjectName("MikageOrientToolbar")
        self.actions: dict[str, QAction] = {}
        for way in ORIENTATIONS:
            action = QAction(orient_icon(way, 32), self.tr(ORIENTATION_LABELS[way]), self.toolbar)
            action.setObjectName(f"MikageOrientAction_{way}")
            action.setCheckable(True)
            action.setToolTip(self.row.buttons[way].toolTip())
            action.triggered.connect(lambda _checked, way=way: self.press(way))
            self.toolbar.addAction(action)
            self.actions[way] = action
        self.toolbar.addSeparator()
        self.enhance_box = QCheckBox(self.tr("補正"))
        self.enhance_box.setObjectName("MikageToolbarEnhance")
        self.enhance_box.setToolTip(self.tr("選んでいる画像（グループでは中の画像すべて）の画像補正をオン・オフ（画像補正ドックの「補正」と同じ）"))
        self.enhance_box.clicked.connect(self._enhance_clicked)
        self.dra_box = QCheckBox(self.tr("DRA"))
        self.dra_box.setObjectName("MikageToolbarDra")
        self.dra_box.setToolTip(self.tr("表示範囲で自動レンジ（画像補正ドックの DRA の「オン」と同じ）。オンにすると補正もオン"))
        self.dra_box.clicked.connect(self._dra_clicked)
        self.toolbar.addWidget(self.enhance_box)
        self.toolbar.addWidget(self.dra_box)
        self.row.changed.connect(self.sync)
        dock.enabled.toggled.connect(self.sync)
        dock.dra_on.toggled.connect(self.sync)
        self.sync()

    def close(self) -> None:
        for signal in (self.row.changed, self.dock.enabled.toggled, self.dock.dra_on.toggled):
            try:
                signal.disconnect(self.sync)
            except (TypeError, RuntimeError):
                pass
        try:
            self.iface.mainWindow().removeToolBar(self.toolbar)
            self.toolbar.deleteLater()
        except RuntimeError:
            pass

    def set_allowed(self, allowed: bool) -> None:
        """Shown only for the roles (担当) that have 画像補正; QGIS's toolbar list offers it only then."""
        self.toolbar.toggleViewAction().setVisible(allowed)
        if not allowed:
            self.toolbar.hide()

    def sync(self, *_args) -> None:
        """The buttons and boxes as the dock shows them (the way the map is turned, the image's correction)."""
        for way, action in self.actions.items():
            button = self.row.buttons[way]
            action.setEnabled(button.isEnabled())
            action.setChecked(button.isChecked())
        has = self.dock.enabled.isEnabled()
        for box, shown in ((self.enhance_box, self.dock.enabled), (self.dra_box, self.dock.dra_on)):
            box.setEnabled(has)
            box.setChecked(shown.isChecked())

    def press(self, way: str) -> None:
        turned, text = self.row.press(way)
        self.sync()
        if not turned:
            # The dock (where it is said) may be closed.
            self.iface.messageBar().pushMessage(self.tr(TITLE), text, Qgis.MessageLevel.Warning, 6)

    def _enhance_clicked(self, on: bool) -> None:
        self.dock.enabled.setChecked(on)
        self.sync()

    def _dra_clicked(self, on: bool) -> None:
        self.dock.dra_on.setChecked(on)
        if on and not self.dock.enabled.isChecked():
            self.dock.enabled.setChecked(True)
        self.sync()
