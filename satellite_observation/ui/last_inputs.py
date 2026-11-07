"""
入力の初期値の記憶: what was last chosen in a form (a satellite, a system,
a condition, a part of a report…) is offered first the next time, in this
QGIS user profile (QgsSettings `satellite_observation/last_inputs/<form>`),
never in the settings file.

`InputMemory(form, {name: widget})` keeps the values of combo boxes (by
their text), line edits, spin boxes and check boxes: `restore()` puts back
what was kept (a choice no longer offered is left as it is), `save()` keeps
what the widgets say now, and `watch()` saves whenever one changes (the
docks, which have no 「OK」). `forget_all()` clears every form's values
(the settings dialog's 「覚えた値を消去」).
"""
from __future__ import annotations

import json
from typing import Mapping, Optional

from qgis.core import QgsSettings
from qgis.PyQt.QtWidgets import QAbstractButton, QAbstractSpinBox, QComboBox, QDoubleSpinBox, QLineEdit, QSpinBox, QWidget

from ..qgis_io.settings import PREFIX

KEY = "last_inputs"


def read(form: str) -> dict:
    """The values kept for `form` ({} when none or unreadable)."""
    value = QgsSettings().value(f"{PREFIX}{KEY}/{form}", "")
    try:
        value = json.loads(value) if isinstance(value, str) and value else {}
    except ValueError:
        return {}
    return value if isinstance(value, dict) else {}


def write(form: str, values: Mapping) -> None:
    QgsSettings().setValue(f"{PREFIX}{KEY}/{form}", json.dumps(dict(values), ensure_ascii=False))


#: The observation form's last kind, activity and 確度, per store (core.obs.book.ObservationBook.remember).
OBSERVATION_LAST = "observations/last_values"


def forget_all() -> None:
    """Every form's kept values cleared (the observation form's too): the forms start from their own defaults again."""
    QgsSettings().remove(f"{PREFIX}{KEY}")
    QgsSettings().remove(f"{PREFIX}{OBSERVATION_LAST}")


def value_of(widget: QWidget):
    """What a widget says, as JSON (None for a widget this does not handle)."""
    if isinstance(widget, QComboBox):
        return widget.currentText()
    if isinstance(widget, QLineEdit):
        return widget.text()
    if isinstance(widget, (QSpinBox, QDoubleSpinBox)):
        return widget.value()
    if isinstance(widget, QAbstractButton) and widget.isCheckable():
        return widget.isChecked()
    return None


def put(widget: QWidget, value) -> bool:
    """Puts a kept value back; False when it no longer fits (an item gone, a wrong type)."""
    if isinstance(widget, QComboBox):
        if not isinstance(value, str):
            return False
        i = widget.findText(value)
        if i >= 0:
            widget.setCurrentIndex(i)
            return True
        if widget.isEditable():
            widget.setEditText(value)
            return True
        return False
    if isinstance(widget, QLineEdit):
        if not isinstance(value, str):
            return False
        widget.setText(value)
        return True
    if isinstance(widget, (QSpinBox, QDoubleSpinBox)):
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            return False
        if not widget.minimum() <= value <= widget.maximum():
            return False
        widget.setValue(int(value) if isinstance(widget, QSpinBox) else float(value))
        return True
    if isinstance(widget, QAbstractButton) and widget.isCheckable():
        if not isinstance(value, bool):
            return False
        widget.setChecked(value)
        return True
    return False


def _changed_signal(widget: QWidget):
    if isinstance(widget, QComboBox):
        return widget.currentIndexChanged if not widget.isEditable() else widget.currentTextChanged
    if isinstance(widget, QLineEdit):
        return widget.editingFinished
    if isinstance(widget, QAbstractSpinBox):
        return widget.valueChanged
    if isinstance(widget, QAbstractButton):
        return widget.toggled
    return None


class InputMemory:
    """The last values of some of a form's widgets, kept per user profile."""

    def __init__(self, form: str, widgets: Mapping[str, QWidget]):
        self.form = form
        self.widgets = dict(widgets)
        self._restoring = False

    def restore(self) -> list[str]:
        """Puts back the kept values; returns the names put back."""
        kept = read(self.form)
        done = []
        self._restoring = True
        try:
            for name, widget in self.widgets.items():
                if name in kept and put(widget, kept[name]):
                    done.append(name)
        finally:
            self._restoring = False
        return done

    def save(self, *_args) -> None:
        if self._restoring:
            return
        kept = read(self.form)
        for name, widget in self.widgets.items():
            value = value_of(widget)
            if value is not None:
                kept[name] = value
        write(self.form, kept)

    def watch(self) -> "InputMemory":
        """Saves whenever one of the widgets changes."""
        for widget in self.widgets.values():
            signal = _changed_signal(widget)
            if signal is not None:
                signal.connect(self.save)
        return self


def checked_satellites(form: str) -> Optional[dict]:
    """The satellites' checks kept for `form` ({key: checked}), None when none were kept."""
    value = read(form).get("satellites")
    if not isinstance(value, dict):
        return None
    return {k: v for k, v in value.items() if isinstance(k, str) and isinstance(v, bool)}


def keep_checked_satellites(form: str, checks: Mapping[str, bool]) -> None:
    """Keeps the satellites' checks ({key: checked}), with those of satellites not listed now kept too."""
    kept = read(form)
    old = kept.get("satellites") if isinstance(kept.get("satellites"), dict) else {}
    kept["satellites"] = {**old, **dict(checks)}
    write(form, kept)
