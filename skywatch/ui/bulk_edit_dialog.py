"""
The 「一括編集」 dialog: what to set on every request or record chosen. Each
item has a tick; only the ticked ones are changed, the rest stay as each
request or record has them.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Optional, Sequence

from qgis.PyQt.QtCore import QStringListModel, Qt
from qgis.PyQt.QtWidgets import (
    QCheckBox, QComboBox, QCompleter, QDialog, QDialogButtonBox, QFormLayout, QHBoxLayout, QLabel, QLineEdit, QSpinBox, QVBoxLayout,
    QWidget,
)

from ..core.request.model import PRIORITY_LEVELS, PRIORITY_MAX, PRIORITY_MIN, level_of


@dataclass
class BulkField:
    """One thing the dialog can set: `choices` (a list), `words` (free text with hints) or a priority."""
    key: str
    label: str
    choices: Sequence[str] = ()
    words: Sequence[str] = ()
    priority: bool = False
    #: The value offered at first.
    value: Any = None
    tip: str = ""
    #: A further tick shown under the item (key, label), set only when the item is.
    extra: Optional[tuple] = None


class _Priority(QWidget):
    """A level (最優先 … 最低) and the number it stands for, either typed."""

    def __init__(self, value: int, parent=None):
        super().__init__(parent)
        row = QHBoxLayout(self)
        row.setContentsMargins(0, 0, 0, 0)
        self.level = QComboBox()
        self.level.addItems([name for name, *_ in PRIORITY_LEVELS])
        self.number = QSpinBox()
        self.number.setRange(PRIORITY_MIN, PRIORITY_MAX)
        self.number.setValue(value)
        self.level.setCurrentText(level_of(value))
        self.level.activated.connect(lambda i: self.number.setValue(PRIORITY_LEVELS[i][3]))
        self.number.valueChanged.connect(lambda v: self.level.setCurrentText(level_of(v)))
        row.addWidget(self.level)
        row.addWidget(self.number, 1)

    def value(self) -> int:
        return self.number.value()


class BulkEditDialog(QDialog):
    """Asks what to set on `count` requests or records."""

    def __init__(self, title: str, what: str, fields: Sequence[BulkField], parent=None):
        super().__init__(parent)
        self.setWindowTitle(title)
        self.fields = list(fields)
        lay = QVBoxLayout(self)
        head = QLabel(what)
        head.setWordWrap(True)
        lay.addWidget(head)
        note = QLabel(self.tr("チェックした項目だけを変更（ほかの項目はそれぞれのまま）"))
        note.setWordWrap(True)
        lay.addWidget(note)
        form = QFormLayout()
        self.ticks: dict[str, QCheckBox] = {}
        self.inputs: dict[str, Any] = {}
        self.extras: dict[str, QCheckBox] = {}
        for f in self.fields:
            tick = QCheckBox(f.label)
            tick.setToolTip(f.tip)
            if f.priority:
                widget = _Priority(int(f.value if f.value is not None else 500))
            elif f.choices:
                widget = QComboBox()
                widget.addItems(list(f.choices))
                if f.value in f.choices:
                    widget.setCurrentText(f.value)
            else:
                widget = QLineEdit("" if f.value is None else str(f.value))
                model = QStringListModel(list(dict.fromkeys(w for w in f.words if w)), widget)
                completer = QCompleter(model, widget)
                completer.setFilterMode(Qt.MatchFlag.MatchContains)
                widget.setCompleter(completer)
                widget.textEdited.connect(lambda _t, tick=tick: tick.setChecked(True))
            widget.setEnabled(False)
            tick.toggled.connect(widget.setEnabled)
            if isinstance(widget, QComboBox):
                widget.activated.connect(lambda _i, tick=tick: tick.setChecked(True))
            form.addRow(tick, widget)
            self.ticks[f.key], self.inputs[f.key] = tick, widget
            if f.extra:
                extra = QCheckBox(f.extra[1])
                extra.setEnabled(False)
                tick.toggled.connect(extra.setEnabled)
                form.addRow("", extra)
                self.extras[f.extra[0]] = extra
        lay.addLayout(form)
        self.message = QLabel()
        self.message.setWordWrap(True)
        lay.addWidget(self.message)
        self.buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        self.buttons.button(QDialogButtonBox.StandardButton.Ok).setText(self.tr("変更"))
        self.buttons.accepted.connect(self._ok)
        self.buttons.rejected.connect(self.reject)
        lay.addWidget(self.buttons)

    def tick(self, key: str, value: Any = None) -> None:
        """Ticks an item (and sets its value), as a person would (for tests and scripts)."""
        self.ticks[key].setChecked(True)
        if value is None:
            return
        w = self.inputs[key]
        if isinstance(w, _Priority):
            w.number.setValue(int(value))
        elif isinstance(w, QComboBox):
            w.setCurrentText(str(value))
        else:
            w.setText(str(value))

    def values(self) -> dict[str, Any]:
        """The ticked items' values, by key (and the ticked extras as True)."""
        out: dict[str, Any] = {}
        for key, tick in self.ticks.items():
            if not tick.isChecked():
                continue
            w = self.inputs[key]
            out[key] = w.value() if isinstance(w, _Priority) else w.currentText() if isinstance(w, QComboBox) else w.text().strip()
        for key, extra in self.extras.items():
            if extra.isEnabled() and extra.isChecked():
                out[key] = True
        return out

    def _ok(self) -> None:
        if not any(t.isChecked() for t in self.ticks.values()):
            self.message.setText(self.tr("変更する項目にチェック"))
            return
        self.accept()
