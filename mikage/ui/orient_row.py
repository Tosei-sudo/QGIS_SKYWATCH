"""
画像補正ドック「地図の向き」 (tsudo 2026-11-25: 「画像補正ドックの方に、それぞれの
角度(北上、撮像方向、等々)に回転させるボタンがほしい」, 2026-11-26: icons, 「太い↑
の中にNの文字」): four buttons under the dock's 対象, the map turned so the
way of the image worked on is up (ui/map_orient.MapOrienter.turn, the
catalog's 「向き」 shared). The button of the way the map shows now is down; a
line under them says what was done, or why the map was not turned.
"""
from __future__ import annotations

from typing import Callable, Optional

from qgis.PyQt.QtCore import QSize, pyqtSignal
from qgis.PyQt.QtWidgets import QLabel, QToolButton, QVBoxLayout, QWidget

from ..core.map_view import ORIENTATIONS
from .flow_layout import FlowLayout
from .map_orient import release_orienter, shared_orienter
from .orient_icons import orient_icon

ICON_SIZE = 30

TIPS = {
    "north": "北が上（地図の回転を 0° に。投影法はそのまま）",
    "look": "撮像方向が上（衛星が下）: 衛星から地上を見る向きを上に。光学の斜め撮りではビルが上へ倒れて見える",
    "satellite": "衛星が上（撮像方向が下）: 衛星を画面の上に。SAR の影が下へ伸びる",
    "flight": "進行方向が上（SAR）: 衛星の進む向きを上に",
}
#: Said under every way but north.
TIP_MORE = ("\n対象の画像（グループでは一番上の画像）の向きに地図を回す。方位はカタログの方位角、"
            "クイック幾何補正で入力した方角、画像のメタデータの順に探す。緯度経度の地図は画像の UTM に切り替える。"
            "回した画像を閉じると、地図の投影法と向きを元に戻す")


class OrientRow(QWidget):
    """
    「地図の向き」: N 撮 衛 進, each turning the map for `layer_of()`; `found_of`, `typed_of` as MapOrienter.turn takes them.
    `changed`: a button went down or up, or was enabled (the toolbar's copy follows).
    """

    changed = pyqtSignal()

    def __init__(self, canvas, layer_of: Callable[[], Optional[object]],
                 found_of: Callable[[object], Optional[tuple]] = lambda layer: None, parent=None):
        super().__init__(parent)
        self.canvas = canvas
        self.layer_of = layer_of
        self.found_of = found_of
        #: The direction typed in クイック幾何補正 (set by the enhance feature: its keeper).
        self.typed_of: Callable[[object], Optional[float]] = lambda layer: None
        self.orienter = shared_orienter(canvas, self)
        v = QVBoxLayout(self)
        v.setContentsMargins(0, 0, 0, 0)
        v.setSpacing(2)
        row = FlowLayout(spacing=3)
        title = QLabel(self.tr("地図の向き"))
        row.addWidget(title)
        self.buttons: dict[str, QToolButton] = {}
        for way in ORIENTATIONS:
            b = QToolButton()
            b.setObjectName(f"MikageOrient_{way}")
            b.setIcon(orient_icon(way, ICON_SIZE))
            b.setIconSize(QSize(ICON_SIZE, ICON_SIZE))
            b.setCheckable(True)
            b.setAutoRaise(False)
            b.setToolTip(self.tr(TIPS[way]) + ("" if way == "north" else self.tr(TIP_MORE)))
            b.clicked.connect(lambda _checked, way=way: self.press(way))
            row.addWidget(b)
            self.buttons[way] = b
        v.addLayout(row)
        self.note = QLabel()
        self.note.setWordWrap(True)
        self.note.setStyleSheet("color: #475569;")
        self.note.hide()
        v.addWidget(self.note)
        self.orienter.turned.connect(self.show_way)
        self.orienter.said.connect(self.say)
        canvas.rotationChanged.connect(self._rotation_changed)
        self.set_layer(None)
        self.show_way()

    def close_down(self) -> None:
        for signal, slot in ((self.orienter.turned, self.show_way), (self.orienter.said, self.say),
                             (self.canvas.rotationChanged, self._rotation_changed)):
            try:
                signal.disconnect(slot)
            except (TypeError, RuntimeError):
                pass
        release_orienter(self.orienter, self)

    def set_layer(self, layer) -> None:
        """The image the buttons turn the map for (None: only 北が上)."""
        for way, b in self.buttons.items():
            b.setEnabled(way == "north" or layer is not None)
        self.changed.emit()

    def say(self, text: str) -> None:
        self.note.setText(text)
        self.note.setVisible(bool(text))

    def press(self, way: str) -> tuple[bool, str]:
        """The map turned so `way` is up; whether it turned, and what was done or why not (said under the buttons)."""
        turned, text = self.orienter.turn(self.layer_of(), way, found_of=self.found_of, typed_of=self.typed_of)
        self.say("" if way == "north" else text)
        self.show_way()
        return turned, text

    def show_way(self, *_args) -> None:
        """The button of the way the map shows now down (none when turned otherwise)."""
        now = self.orienter.current_way()
        for way, b in self.buttons.items():
            b.setChecked(way == now)
        self.changed.emit()

    def _rotation_changed(self, *_args) -> None:
        if not self.orienter.busy:
            # Turned by hand (QGIS's box, the north arrow): what was said is no longer so.
            self.say("")
        self.show_way()
