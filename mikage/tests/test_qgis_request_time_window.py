"""
時間帯を時刻で指定 in QGIS (tsudo, 2026-11-26): the request form's 時間帯 「時刻」 with
its 「時刻（日本時間） 10:00 〜 14:00」 row, saved to the ledger and shown in the
list's 「時間帯」 column (hidden until shown, as メモ).
"""
import pytest

pytest.importorskip("qgis.core")
from qgis.testing import start_app  # noqa: E402

APP = start_app()

from qgis.core import QgsPointXY  # noqa: E402
from qgis.PyQt.QtCore import QTime  # noqa: E402

from mikage.core.request.model import BY_CLOCK, ImagingRequest  # noqa: E402
from mikage.tests.qgis_fakes import FakeIface, wait_for  # noqa: E402
from mikage.tests.test_qgis_requests import LEDGER, plugin  # noqa: E402,F401 (the fixture)
from mikage.ui.request_dock import COLUMNS  # noqa: E402
from mikage.ui.request_form import RequestForm  # noqa: E402


@pytest.fixture
def form():
    f = RequestForm(FakeIface(), ImagingRequest(id=""), lambda _r: None)
    yield f
    f.close()


def test_the_times_show_only_by_the_clock(form):
    assert form.clock_row.isHidden()
    form.time_of_day.setCurrentText(BY_CLOCK)
    assert not form.clock_row.isHidden()
    assert (form.clock_from.time(), form.clock_to.time()) == (QTime(10, 0), QTime(14, 0))
    r = form.value()
    assert r.time_of_day == BY_CLOCK and (r.time_from, r.time_to) == ("10:00", "14:00") and r.window == (600, 840)
    form.time_of_day.setCurrentText("昼")
    assert form.clock_row.isHidden() and form.value().time_from == "" and form.value().window is None


def test_past_midnight_is_said_and_the_same_times_are_not_saved(form):
    form.time_of_day.setCurrentText(BY_CLOCK)
    form.clock_from.setTime(QTime(22, 0))
    form.clock_to.setTime(QTime(2, 0))
    assert "日をまたぐ" in form.clock_note.text()
    assert form.value().window == (1320, 120)
    form.clock_to.setTime(QTime(22, 0))
    assert form.clock_note.text() == ""
    assert "時刻の始まりと終わりが同じ" in form.problems(form.value())


def test_a_request_by_the_clock_opens_with_its_times():
    r = ImagingRequest(id="R", name="港", time_of_day=BY_CLOCK, time_from="09:30", time_to="24:00")
    f = RequestForm(FakeIface(), r, lambda _r: None)
    try:
        assert f.time_of_day.currentText() == BY_CLOCK and not f.clock_row.isHidden()
        # 24:00 (the day's end) as its last minute.
        assert (f.clock_from.time(), f.clock_to.time()) == (QTime(9, 30), QTime(23, 59))
    finally:
        f.close()


def test_saved_to_the_ledger_and_shown_in_the_list(plugin):  # noqa: F811
    p, _iface, fake, _tmp = plugin
    dock = p.api.requests
    dock.show()
    assert wait_for(lambda: dock._loaded and dock.task is None)
    dock.edit(new=True)
    form = dock.form
    form.name.setText("横須賀港 昼前")
    form._point_clicked(QgsPointXY(139.67, 35.28))
    form.time_of_day.setCurrentText(BY_CLOCK)
    form.clock_from.setTime(QTime(9, 0))
    form.clock_to.setTime(QTime(11, 30))
    form._save_clicked()
    assert wait_for(lambda: dock.requests and dock.task is None)
    [row] = fake.layers[LEDGER].rows
    assert (row["attributes"]["TIME_OF_DAY"], row["attributes"]["TIME_FROM"], row["attributes"]["TIME_TO"]) == ("時刻", "09:00", "11:30")
    column = COLUMNS.index("時間帯")
    assert dock.table.isColumnHidden(column)
    assert dock.model.item(0, column).text() == "09:00〜11:30"
