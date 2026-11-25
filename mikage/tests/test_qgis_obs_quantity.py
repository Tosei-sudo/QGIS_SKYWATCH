"""
数量 in QGIS (tsudo 2026-11-24): the 数量 row of the 観測記録 form (a group
recorded as one record, 車両 ×30), its 「×30」 in the list and the count's
「数量 計」, the flow's 数量 as it went, the bulk edit's check, and training
leaving a group (and the checked area holding it) out.
"""
import pytest

pytest.importorskip("qgis.core")
from qgis.testing import start_app  # noqa: E402

APP = start_app()

from mikage.core.obs.areas import CheckedArea  # noqa: E402
from mikage.core.obs.model import Observation, default_taxonomy  # noqa: E402
from mikage.qgis_io.train_io import labels_from_book  # noqa: E402
from mikage.tests.test_qgis_bulk_edit import _choose  # noqa: E402
from mikage.tests.test_qgis_observations import plugin  # noqa: E402,F401 (the fixture)


def test_a_group_typed_in_the_form_is_one_record_of_its_number(plugin):
    p, iface, dock, layers, _ = plugin
    book = dock.book
    assert book.keeps_quantity()
    dock.set_active(True)
    dock.map_clicked(139.7, 35.6, None)
    assert dock.quantity_input.isVisibleTo(dock.form) and dock.quantity_input.text() == ""
    assert dock.quantity_input.placeholderText() == "1"
    dock.object_input.setText("駐車場の車両")
    dock.kind_input.setText("車両")
    # Typed with the IME on and a counter word: read as 30.
    dock.quantity_input.setText("３０台")
    dock.submit()
    [group] = book.living()
    assert group.quantity == 30
    dock.map_clicked(139.65, 35.6, None)
    assert dock.quantity_input.text() == ""
    dock.object_input.setText("トラック")
    dock.submit()
    assert [o.quantity for o in book.living() if o.object_name == "トラック"] == [None]
    dock.render()
    names = [dock.table.item(r, 0).text() for r in range(dock.table.rowCount())]
    assert sorted(names) == ["トラック", "駐車場の車両×30"]
    assert dock.count_label.text() == "この画像 2 件（数量 計 31）"
    # Changed to what is not a whole number: the form stays open and says so, the record keeps its 30.
    dock.open_form(group.lon, group.lat, editing=group)
    assert dock.quantity_input.text() == "30"
    dock.quantity_input.setText("2.5")
    dock.submit()
    assert dock.form.isVisible() and dock.pending is not None
    assert dock.measure_note.isVisibleTo(dock.form)
    assert dock.measure_note.text() == "数量は 1 以上の整数で入力（「2.5」は読めない）"
    assert [o.quantity for o in book.living() if o.object_name == "駐車場の車両"] == [30]
    # 1 (or empty): one thing again, the 「数量 計」 gone.
    dock.quantity_input.setText("1")
    dock.submit()
    assert not dock.form.isVisible()
    assert [o.quantity for o in book.living() if o.object_name == "駐車場の車両"] == [None]
    dock.render()
    assert dock.count_label.text() == "この画像 2 件"


def test_a_group_carried_over_keeps_its_number_and_the_flow_shows_it(plugin):
    p, iface, dock, layers, _ = plugin
    book = dock.book
    first = book.add(139.7, 35.6, object="駐車場の車両", kind="車両", activity="駐車", quantity=30)
    dock.step(1)
    [c] = book.carried()
    assert c.source.id == first.id
    # 継続 opens the form with the number it had, changed here to 22.
    dock.open_form(c.source.lon, c.source.lat, source=c.source)
    assert dock.quantity_input.text() == "30" and dock.submit_button.text() == "継続（Enter）"
    dock.quantity_input.setText("22")
    dock.submit()
    second = [o for o in book.living() if o.image_id == book.current.id]
    assert [o.quantity for o in second] == [22]
    dock.chosen = first.object_id
    dock.render()
    assert dock.flow_box.isVisible()
    assert "・数量 30 → 22・" in dock.flow_summary.text()
    assert [dock.steps_table.item(r, 1).text() for r in range(dock.steps_table.rowCount())] == ["駐車 ×30", "駐車 ×22"]
    # 消失: no number.
    dock.step(1)
    dock.carry(first.object_id, present=False)
    gone = [o for o in book.living() if o.image_id == book.current.id]
    assert [(o.present, o.quantity) for o in gone] == [(False, None)]


def test_the_bulk_edit_sets_a_number_and_refuses_one_that_is_not(plugin):
    p, iface, dock, layers, _ = plugin
    book = dock.book
    for name, lon in (("あ", 139.65), ("い", 139.7), ("う", 139.75)):
        book.add(lon, 35.6, object=name, kind="車両", activity="駐車")
    dock.render()
    _choose(dock.table, [0, 2])
    dialog = dock.bulk_edit()
    assert "quantity" in dialog.inputs
    dialog.tick("quantity", "0")
    dialog._ok()
    assert dialog.isVisible() and dialog.message.text() == "数量は 1 以上の整数で入力（「0」は読めない）"
    dialog.tick("quantity", "１２")
    dialog._ok()
    assert not dialog.isVisible()
    got = {o.object_name: o.quantity for o in book.living()}
    assert got == {"あ": 12, "い": None, "う": 12}


def test_training_leaves_a_group_and_the_checked_area_holding_it_out():
    ring = [[139.0, 35.0], [139.01, 35.0], [139.01, 35.01], [139.0, 35.01], [139.0, 35.0]]
    far = [[139.1, 35.1], [139.11, 35.1], [139.11, 35.11], [139.1, 35.11], [139.1, 35.1]]
    areas = [CheckedArea("a1", "img", "img", None, ring, "", "駐車場", 0, 0),
             CheckedArea("a2", "img", "img", None, far, "", "港", 0, 0)]
    one = Observation("r1", "img", "img", None, "o1", "", "船舶", "", True, "", 139.105, 35.105, 0, 0, length=20.0)
    group = Observation("r2", "img", "img", None, "o2", "", "船舶", "", True, "", 139.005, 35.005, 0, 0, quantity=30)
    gathered = labels_from_book([one, group], areas, default_taxonomy())
    assert [l.centre for l in gathered.labels] == [(139.105, 35.105)]
    assert len(gathered.areas) == 1 and gathered.areas[0].geometry.contains(gathered.labels[0].geometry.centroid())
    assert gathered.skipped["数量 2 以上の記録（まとめて記録）"] == 1
    assert gathered.skipped["数量 2 以上の記録がある確認範囲"] == 1
