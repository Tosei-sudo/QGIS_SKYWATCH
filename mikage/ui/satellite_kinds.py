"""
衛星の種別 on the screens (`satelliteKinds`, core/plan/kinds.py): the kind of pasted TLE satellites, and the words
that name the kinds imaging only in daylight (日照条件) or always.
"""
from __future__ import annotations

from typing import Optional

from qgis.PyQt.QtCore import Qt
from qgis.PyQt.QtWidgets import QComboBox, QWidget

from ..core.plan.api import kinds


def kind_combo(parent: Optional[QWidget] = None) -> QComboBox:
    """A choice of the kinds in use (the kind of pasted satellites), the others' kind (光学) at first."""
    combo = QComboBox(parent)
    fill_kinds(combo)
    return combo


def fill_kinds(combo: QComboBox) -> None:
    """Lists the kinds in use again, keeping the one chosen when it is still there (else the others' kind)."""
    chosen = combo.currentData()
    combo.blockSignals(True)
    combo.clear()
    for k in kinds():
        tip = "、".join(t for t, on in (("日中のみ撮像", k.daylight), ("夜間も撮像", not k.daylight),
                                       ("雲を通して撮像", not k.clouds)) if on)
        combo.addItem(k.name, k.name)
        combo.setItemData(combo.count() - 1, tip, Qt.ItemDataRole.ToolTipRole)
    at = combo.findData(chosen) if chosen is not None else -1
    combo.setCurrentIndex(at if at >= 0 else max(0, combo.findData(kinds().other.name)))
    combo.blockSignals(False)
    combo.setToolTip("貼り付けた衛星の種別（設定の satelliteKinds）")


def daylight_names() -> str:
    """The kinds imaging only in daylight: 「光学」 (「光学・ハイパー」)."""
    return "・".join(k.name for k in kinds() if k.daylight) or "光学"


def always_names() -> str:
    """The kinds imaging by night too: 「SAR・IR」."""
    return "・".join(k.name for k in kinds() if not k.daylight) or "SAR"
