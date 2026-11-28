"""
What the observation dock's parts share (ui/obs_dock): the hints, the rows' colours and the small
widgets.
"""
from __future__ import annotations

from typing import Any, Optional

from qgis.PyQt.QtCore import Qt
from qgis.PyQt.QtGui import QBrush, QColor, QPainter
from qgis.PyQt.QtWidgets import QDateTimeEdit, QSizePolicy, QWidget

from ...core.obs.api import local_time, ObjectFlow, time_key, unseen_label
from ..docks import SENDING_ROW  # noqa: F401 (a record confirmed, not taken by the server yet)
from ..obs_overlay import UNSEEN_COLOR

HINT = "地図クリックで記録（白抜きの印は継続）"
HINT_POINTS = {
    2: "後端 → 前端（船首・機首）の 2 点クリックで記録（白抜きの印は継続）",
    3: "後端 → 前端 → 側面の 3 点クリックで長さ・幅・向きを記録（白抜きの印は継続）",
    4: "後端 → 前端 → 両側面の 4 点クリックで長さ・幅・向きを記録（白抜きの印は継続）",
}
#: The choices of 「点数」.
POINT_CHOICES = [(1, "1 点"), (2, "2 点（長さ・向き）"), (3, "3 点（＋片側面）"), (4, "4 点（＋両側面）")]
HINT_AREA = "確認範囲: 地図で 2 点クリック（対角）、または「表示範囲」「画像の範囲」"
HINT_UNSEEN = "不可範囲（雲など）: 地図で 2 点クリック（対角）、または「表示範囲」「画像の範囲」。中の前後の画像のモノは「不可」として記録"
NO_IMAGE = "画像なし。画像を開くか、＋ でファイルのない画像を追加"
#: The background of the rows of records already reported (通報済み).
REPORTED_ROW = "#fde68a"
#: Records saved while their server could not be reached, waiting to be sent (core/offline.py).
WAITING_ROW = "#dbeafe"
ACTIVITY_COLORS = ["#2563eb", "#16a34a", "#d97706", "#7c3aed", "#0891b2", "#db2777", "#65a30d", "#ea580c", "#4f46e5", "#e11d48"]


def _ms_of(edit: QDateTimeEdit) -> int:
    return edit.dateTime().toMSecsSinceEpoch()


class FlowStrip(QWidget):
    """A thing's flow along time: a bar per step, coloured by activity, the images as ticks."""

    def __init__(self, parent: Optional[QWidget] = None):
        super().__init__(parent)
        self.flow: Optional[ObjectFlow] = None
        self.setMinimumHeight(34)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)

    def set_flow(self, flow: Optional[ObjectFlow]) -> None:
        self.flow = flow
        if flow:
            tips = [f"{unseen_label(s.unseen) if s.unseen else (s.activity or '—') if s.present else '消失'}: "
                    f"{local_time(s.start)}{f' 〜 {local_time(s.end)}' if s.end != s.start else ''}" for s in flow.steps]
            self.setToolTip("\n".join(tips))
        self.update()

    def paintEvent(self, event) -> None:  # noqa: N802 (Qt's name)
        flow = self.flow
        if not flow:
            return
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        width = self.width()
        n = len(flow.observations)
        gap = (flow.last - flow.first) / (n - 1) if n > 1 else 0
        span = max(1, flow.last + gap - flow.first)

        def x(t: float) -> float:
            return width / 2 if flow.first == flow.last else 0.01 * width + (t - flow.first) / span * 0.98 * width

        activities = list(dict.fromkeys(s.activity for s in flow.steps))
        for i, s in enumerate(flow.steps):
            nxt = flow.steps[i + 1] if i + 1 < len(flow.steps) else None
            start = x(s.start)
            end = x(nxt.start) if nxt else max(x(s.end + gap), start + 6)
            color = QColor(ACTIVITY_COLORS[activities.index(s.activity) % len(ACTIVITY_COLORS)]) if s.present else QColor("#d4d4d8")
            if s.unseen:
                # 不可: grey and hatched (nothing known), not the red of 消失.
                p.setPen(QColor(UNSEEN_COLOR))
                p.setBrush(QColor("#f3f4f6"))
                p.drawRoundedRect(int(start), 6, int(max(6, end - start)), 14, 3, 3)
                p.setBrush(QBrush(QColor("#9ca3af"), Qt.BrushStyle.BDiagPattern))
                p.drawRoundedRect(int(start), 6, int(max(6, end - start)), 14, 3, 3)
                continue
            p.setPen(QColor("#dc2626") if not s.present else Qt.PenStyle.NoPen)
            p.setBrush(color)
            p.drawRoundedRect(int(start), 6, int(max(6, end - start)), 14, 3, 3)
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(QColor("#111111"))
        for o in flow.observations:
            p.drawRect(int(x(time_key(o)) - 1), 22, 2, 8)
        p.end()


class _Saved:
    """A change's result and the records it added or changed (for the 通報 check)."""

    def __init__(self, result: Any, changed: list):
        self.result, self.changed = result, changed
