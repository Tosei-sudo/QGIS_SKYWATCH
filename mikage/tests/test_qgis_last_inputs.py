"""
入力の初期値の記憶 in QGIS: what was last chosen in a form is offered first
the next time (QgsSettings, per user profile), never in the settings file.
"""
import pytest

pytest.importorskip("qgis.core")
from qgis.testing import start_app  # noqa: E402

APP = start_app()

from qgis.core import QgsPointXY, QgsSettings  # noqa: E402
from qgis.PyQt.QtWidgets import QCheckBox, QComboBox, QDoubleSpinBox, QLineEdit, QSpinBox  # noqa: E402

from mikage.core.plan.satellites import satellites_from_text  # noqa: E402
from mikage.tests.qgis_fakes import wait_for  # noqa: E402
from mikage.tests.test_plan import LINE1, LINE2  # noqa: E402
from mikage.tests.test_qgis_requests import SAR1, SAR2, plugin  # noqa: E402,F401
from mikage.ui import last_inputs  # noqa: E402
from mikage.ui.last_inputs import InputMemory  # noqa: E402
from mikage.ui.satellite_tree import SatelliteTree  # noqa: E402


@pytest.fixture
def clean():
    QgsSettings().remove("mikage")
    yield
    QgsSettings().remove("mikage")


def widgets():
    combo = QComboBox()
    combo.addItems(["光学", "SAR"])
    line, spin, dspin, check = QLineEdit(), QSpinBox(), QDoubleSpinBox(), QCheckBox()
    spin.setRange(0, 10)
    dspin.setRange(-1, 80)
    return {"combo": combo, "line": line, "spin": spin, "dspin": dspin, "check": check}


def test_widgets_get_back_what_was_last_kept(clean):
    first = widgets()
    memory = InputMemory("test", first).watch()
    first["combo"].setCurrentText("SAR")
    first["spin"].setValue(7)
    first["dspin"].setValue(32.5)
    first["check"].setChecked(True)
    first["line"].setText("山田")
    first["line"].editingFinished.emit()
    assert last_inputs.read("test") == {"combo": "SAR", "line": "山田", "spin": 7, "dspin": 32.5, "check": True}
    again = widgets()
    assert sorted(InputMemory("test", again).restore()) == ["check", "combo", "dspin", "line", "spin"]
    assert (again["combo"].currentText(), again["line"].text(), again["spin"].value(), again["dspin"].value(),
            again["check"].isChecked()) == ("SAR", "山田", 7, 32.5, True)
    # A choice no longer offered, or a value out of range, is left as the form has it.
    last_inputs.write("test", {"combo": "赤外", "spin": 50, "check": "yes"})
    third = widgets()
    assert InputMemory("test", third).restore() == []
    assert (third["combo"].currentText(), third["spin"].value(), third["check"].isChecked()) == ("光学", 0, False)
    # Forgetting clears every form's values; the settings file is never touched.
    memory.save()
    last_inputs.forget_all()
    assert last_inputs.read("test") == {}


def test_the_satellites_checked_last_time_are_checked_again(clean):
    sats = satellites_from_text(f"OPT 1\n{LINE1}\n{LINE2}\nRADAR 1\n{SAR1}\n{SAR2}", maxOffNadir=30, minOffNadir=0, swath=10)
    assert len(sats) == 2
    tree = SatelliteTree(remember_as="plan_test")
    tree.set(sats)
    assert len(tree.checked()) == 2
    tree.choice.listed[1].checked = False
    tree.changed.emit()
    again = SatelliteTree(remember_as="plan_test")
    again.set(sats)
    assert [s.name for s in again.checked()] == [sats[0].name]
    # Without a form name nothing is kept or put back.
    plain = SatelliteTree()
    plain.set(sats)
    assert len(plain.checked()) == 2


def test_a_new_request_starts_from_the_last_one_saved(plugin):
    p, iface, fake, tmp_path = plugin
    dock = p.api.requests
    dock.show()
    assert wait_for(lambda: dock._loaded and dock.task is None)
    dock.edit(new=True)
    form = dock.form
    form.name.setText("大阪湾")
    form.kind.setCurrentText("SAR")
    form.detail.setChecked(True)
    form.priority.setValue(150)
    form.systems.setText("SYS-B")
    form.satellites.setText("RADAR 1")
    form.requester.setText("山田")
    form.size.setValue(25)
    form._point_clicked(QgsPointXY(135.3, 34.6))
    form._save_clicked()
    assert wait_for(lambda: dock.task is None and len(dock.requests) == 1)

    dock.edit(new=True)
    form = dock.form
    assert form.name.text() == "" and not form.rings
    assert (form.kind.currentText(), form.priority.value(), form.systems.text(), form.satellites.text(), form.requester.text()) == (
        "SAR", 150, "SYS-B", "RADAR 1", "山田")
    assert form.size.value() == 25
    form.close()


def test_the_settings_dialog_forgets_the_kept_values(plugin):
    from mikage.ui.settings_dialog import SettingsDialog

    p, iface, fake, tmp_path = plugin
    last_inputs.write("request", {"kind": "SAR"})
    QgsSettings().setValue("mikage/observations/last_values/ローカル", '{"kind": "船舶"}')
    QgsSettings().setValue("mikage/observations/points", "2")
    dialog = SettingsDialog(p.context)
    dialog.forget_inputs.click()
    assert last_inputs.read("request") == {}
    assert QgsSettings().value("mikage/observations/last_values/ローカル", "") == ""
    # Other settings stay.
    assert QgsSettings().value("mikage/observations/points") == "2"
    assert "消去済み" in dialog.last_inputs.text()
    dialog.close()
