"""
「移動目標撮像計画」, as the viewer's moving-target-dialog.ts: targets that
move (ships, vehicles, anything with position reports) from any point layer.
The layer's attributes are mapped to an id, a time, a course, a speed (and
its unit) and a name — found by name first — and each target is carried on
from its last report. 「存在予測を追加」 adds the circles where each may be,
every few hours, as memory layers; 「撮像計画の作成」 hands the targets to the
imaging plan, which aims each pass at the circle of its moment.
"""
from __future__ import annotations

from typing import Callable, Optional

from qgis.core import QgsMapLayer, QgsProject, QgsVectorLayer, QgsWkbTypes
from qgis.PyQt.QtCore import QDate, QDateTime, QTime, QTimeZone
from qgis.PyQt.QtWidgets import (
    QCheckBox, QComboBox, QDialog, QDoubleSpinBox, QFormLayout, QGroupBox, QHBoxLayout, QLabel, QPushButton, QVBoxLayout,
)

from ..core.plan.moving import (
    DEFAULT_UNCERTAINTY, MAX_HORIZON, MAX_SHIPS, SPEED_UNITS, Report, ShipState, UncertaintyModel, guess_speed_unit,
    guess_target_fields, places_layers, targets_of,
)
from ..core.plan.output import local_text
from ..qgis_io.geometry import to_wgs84
from ..qgis_io.plan_layer import memory_layer

#: The attributes of a target: role, label, what it does.
ROLES = [
    ("id", "ID（目標ごとに同じ値）", "同じ値の報告を 1 つの目標の航跡として集約"),
    ("time", "時刻", "なければすべて現在の報告とみなす"),
    ("course", "針路（度）", "北から時計回り。なければ最後の 2 報告から算出"),
    ("speed", "速力", "なければ最後の 2 報告から算出"),
    ("name", "名前", "一覧と計画での目標の名前。なければ ID"),
]


def _value(v):
    """An attribute value as Python: NULL as None, dates as ms since 1970 (UTC)."""
    if v is None or (hasattr(v, "isNull") and v.isNull()):
        return None
    if isinstance(v, QDateTime):
        return v.toMSecsSinceEpoch() if v.isValid() else None
    if isinstance(v, QDate):
        return QDateTime(v, QTime(0, 0), QTimeZone.utc()).toMSecsSinceEpoch() if v.isValid() else None
    return v


class MovingTargetDialog(QDialog):
    """The form: a point layer, its attributes, how far ahead and how the guess grows uncertain."""

    def __init__(self, iface, plan: Callable[[list, str], None], parent=None):
        super().__init__(parent)
        self.iface = iface
        self.plan_targets = plan
        self.setWindowTitle(self.tr("移動目標撮像計画"))
        self.setObjectName("SatObsMovingTargets")
        layout = QVBoxLayout(self)
        hint = QLabel(self.tr("航跡・位置報告のポイントレイヤーから、目標ごとに最後の報告の位置・針路・速力で将来位置を推測し、誤差の円（存在予測）とその撮像計画を作成。"))
        hint.setWordWrap(True)
        layout.addWidget(hint)
        form = QFormLayout()
        self.layer = QComboBox()
        self.layer.currentIndexChanged.connect(self.list_fields)
        form.addRow(self.tr("レイヤー"), self.layer)
        self.selected_only = QCheckBox(self.tr("選択中の地物だけ"))
        self.selected_only.toggled.connect(self.describe)
        form.addRow("", self.selected_only)
        layout.addLayout(form)

        box = QGroupBox(self.tr("属性の対応づけ"))
        fields = QFormLayout(box)
        self.fields: dict[str, QComboBox] = {}
        for role, label, tip in ROLES:
            combo = QComboBox()
            combo.setToolTip(self.tr(tip))
            combo.currentIndexChanged.connect(self.describe)
            fields.addRow(self.tr(label), combo)
            self.fields[role] = combo
        self.unit = QComboBox()
        for key, label in SPEED_UNITS.items():
            self.unit.addItem(self.tr(label), key)
        self.unit.currentIndexChanged.connect(self.describe)
        fields.addRow(self.tr("速力の単位"), self.unit)
        layout.addWidget(box)

        more = QFormLayout()
        self.horizon = self._spin(1, MAX_HORIZON, MAX_HORIZON, 0, self.tr(" 時間"))
        more.addRow(self.tr("予測する長さ"), self.horizon)
        self.step = self._spin(0.25, 72, 6, 2, self.tr(" 時間"))
        more.addRow(self.tr("存在予測の間隔"), self.step)
        layout.addLayout(more)

        spread = QGroupBox(self.tr("誤差の広がり"))
        spread_form = QFormLayout(spread)
        self.base = self._spin(0, 1000, DEFAULT_UNCERTAINTY.base, 2, " km")
        spread_form.addRow(self.tr("報告時の誤差"), self.base)
        self.per_hour = self._spin(0, 1000, DEFAULT_UNCERTAINTY.perHour, 2, " km")
        spread_form.addRow(self.tr("1 時間あたりの増加"), self.per_hour)
        self.share = self._spin(0, 100, DEFAULT_UNCERTAINTY.share * 100, 1, " %")
        spread_form.addRow(self.tr("航行距離に対する割合"), self.share)
        formula = QLabel(self.tr("誤差の円の半径 = 報告時の誤差 + 経過時間 ×（1 時間あたりの増加 + 速力 × 割合）。変針・増減速の多い目標ほど大きく設定。"))
        formula.setWordWrap(True)
        spread_form.addRow(formula)
        layout.addWidget(spread)

        self.note = QLabel()
        self.note.setWordWrap(True)
        layout.addWidget(self.note)
        buttons = QHBoxLayout()
        buttons.addStretch(1)
        close = QPushButton(self.tr("閉じる"))
        close.clicked.connect(self.close)
        self.places_button = QPushButton(self.tr("存在予測を追加"))
        self.places_button.clicked.connect(self.add_places)
        self.plan_button = QPushButton(self.tr("撮像計画の作成"))
        self.plan_button.setDefault(True)
        self.plan_button.clicked.connect(self.plan)
        for b in (close, self.places_button, self.plan_button):
            buttons.addWidget(b)
        layout.addLayout(buttons)

    def _spin(self, low: float, high: float, value: float, decimals: int, suffix: str) -> QDoubleSpinBox:
        box = QDoubleSpinBox()
        box.setRange(low, high)
        box.setDecimals(decimals)
        box.setValue(value)
        box.setSuffix(suffix)
        box.valueChanged.connect(self.describe)
        return box

    # --- The form ---------------------------------------------------------------------------

    def open_for(self, layer: Optional[QgsVectorLayer] = None) -> None:
        """Shows the dialog with the point layers listed (`layer`, the active one or the last chosen first)."""
        before = self.layer.currentData()
        layers = [l for l in QgsProject.instance().mapLayers().values()
                  if l.type() == QgsMapLayer.LayerType.VectorLayer and l.isValid()
                  and l.geometryType() == QgsWkbTypes.GeometryType.PointGeometry]
        self.layer.blockSignals(True)
        self.layer.clear()
        for l in sorted(layers, key=lambda l: l.name()):
            self.layer.addItem(f"{l.name()}（{l.featureCount():,}）", l.id())
        active = layer or self.iface.activeLayer()
        for wanted in (active.id() if isinstance(active, QgsVectorLayer) else None, before):
            index = self.layer.findData(wanted) if wanted else -1
            if index >= 0:
                self.layer.setCurrentIndex(index)
                break
        self.layer.blockSignals(False)
        self.list_fields()
        self.show()
        self.raise_()
        self.activateWindow()

    def chosen_layer(self) -> Optional[QgsVectorLayer]:
        layer = QgsProject.instance().mapLayer(self.layer.currentData() or "")
        return layer if isinstance(layer, QgsVectorLayer) else None

    def list_fields(self, *_args) -> None:
        """Fills the attribute choices from the chosen layer, each set to the attribute found by name."""
        layer = self.chosen_layer()
        names = layer.fields().names() if layer is not None else []
        guess = guess_target_fields(names)
        for role, combo in self.fields.items():
            combo.blockSignals(True)
            combo.clear()
            combo.addItem(self.tr("（なし）"), "")
            for i, name in enumerate(names):
                combo.addItem(layer.attributeDisplayName(i), name)
            combo.setCurrentIndex(max(0, combo.findData(guess.get(role, ""))))
            combo.blockSignals(False)
        self.unit.blockSignals(True)
        self.unit.setCurrentIndex(max(0, self.unit.findData(guess_speed_unit(guess.get("speed")))))
        self.unit.blockSignals(False)
        self.describe()

    def settings(self) -> dict:
        """What the form asks for: the attributes, the unit, how the guess grows uncertain, how far ahead and how often."""
        return {
            "fields": {role: combo.currentData() for role, combo in self.fields.items() if combo.currentData()},
            "unit": self.unit.currentData(),
            "model": UncertaintyModel(self.base.value(), self.share.value() / 100, self.per_hour.value()),
            "horizon": min(MAX_HORIZON, max(1, self.horizon.value())),
            "step": max(0.25, self.step.value()),
        }

    def targets(self) -> list[ShipState] | str:
        """The targets of the chosen layer (or its selected features); the reason when there are none."""
        layer = self.chosen_layer()
        if layer is None:
            return self.tr("航跡のポイントレイヤーを開く必要あり")
        s = self.settings()
        if not s["fields"].get("id"):
            return self.tr("目標ごとの ID になる属性を選択")
        selected = self.selected_only.isChecked()
        transform = to_wgs84(layer.crs())
        names = layer.fields().names()
        reports = []
        for f in (layer.getSelectedFeatures() if selected else layer.getFeatures()):
            g = f.geometry()
            if g is None or g.isNull() or g.isEmpty():
                continue
            points = g.asMultiPoint() if g.isMultipart() else [g.asPoint()]
            if not points:
                continue
            p = transform.transform(points[0])
            reports.append(Report({n: _value(v) for n, v in zip(names, f.attributes())}, (p.x(), p.y())))
        if not reports:
            return self.tr("選択中の地物にポイントなし") if selected else self.tr("このレイヤーにポイントなし")
        targets = targets_of(reports, s["fields"], s["unit"], s["model"])
        return targets or self.tr("ID のある報告なし")

    def describe(self, *_args) -> None:
        """Says how many targets the form gives, and how many have no course or speed."""
        targets = self.targets()
        if isinstance(targets, str):
            self.note.setText(targets)
            return
        still = sum(1 for t in targets if t.course is None or not t.speed)
        latest = max(t.t for t in targets)
        text = self.tr("目標 {n} 件（最新の報告 {t}）").format(n=f"{len(targets):,}", t=local_text(latest))
        if still:
            text += self.tr("。うち {n} 件は針路・速力が不明のため、その場に留まるとみなす").format(n=still)
        if len(targets) > MAX_SHIPS:
            text += self.tr("。撮像計画は最近報告のあった {n} 件で作成").format(n=MAX_SHIPS)
        self.note.setText(text)

    # --- Results ------------------------------------------------------------------------------

    def add_places(self) -> Optional[tuple]:
        """Adds the predicted places (circles with their time) and the predicted tracks as memory layers, in a group."""
        targets = self.targets()
        if isinstance(targets, str):
            self.note.setText(targets)
            return None
        s = self.settings()
        circles, tracks = places_layers(targets, s["horizon"], s["step"])
        layer = self.chosen_layer()
        title = self.tr("存在予測 {layer}（{h} 時間先まで）").format(layer=layer.name() if layer else "", h=f"{s['horizon']:g}")
        project = QgsProject.instance()
        group = project.layerTreeRoot().insertGroup(0, title)
        made = []
        for plan, geometry in ((circles, "MultiPolygon"), (tracks, "MultiLineString")):
            out = memory_layer(plan, self.tr(plan.title), geometry)
            project.addMapLayer(out, False)
            group.addLayer(out)
            made.append(out)
        self.note.setText(self.tr("存在予測 {n} 個の円と予測航跡をレイヤーに追加（時刻つき。時系列コントローラーで時刻を合わせると、その時の存在予測だけを表示可能）").format(n=f"{len(circles.features):,}"))
        return tuple(made)

    def plan(self) -> None:
        """Hands the targets to the imaging plan."""
        targets = self.targets()
        if isinstance(targets, str):
            self.note.setText(targets)
            return
        layer = self.chosen_layer()
        self.close()
        self.plan_targets(targets, layer.name() if layer else self.tr("移動目標"))
