"""
「見本で記録」 in the 観測記録 dock (ui/obs_many.py, tsudo 2026-11-09): the
last record saved from the form is the 見本; 「連続」 records one per double
click as the 見本 lies and one per press and drag turned to the drag (a
single click waits to be told from a double click, and opens a record as
before), a double click on a hollow mark continues that thing, Backspace
takes the last back; 「並べる」 lays a row or a grid out by its clicks, a
place clicked is left out, Enter records them in one save; on an Esri store
what is put down while a save runs goes in the next one, and what cannot be
saved waits in 送信待ち; 「似たものを探す」 finds the cars like the 見本 on an
image and the dock 「AI結果」 records them with the 見本's class, 活動, 確度,
size and their own heading.
"""
import json
import math

import numpy as np
import pytest

pytest.importorskip("qgis.core")
from qgis.testing import start_app  # noqa: E402

APP = start_app()

from osgeo import gdal, osr  # noqa: E402
from qgis.core import QgsProject, QgsRasterLayer, QgsVectorLayer  # noqa: E402
from qgis.gui import QgsMapMouseEvent  # noqa: E402
from qgis.PyQt.QtCore import QEvent, QPoint, Qt  # noqa: E402
from qgis.PyQt.QtGui import QKeyEvent  # noqa: E402

from mikage.core.esri import EsriClient  # noqa: E402
from mikage.core.obs.flows import living  # noqa: E402
from mikage.core.obs.geo import bearing, get_distance  # noqa: E402
from mikage.qgis_io import settings  # noqa: E402
from mikage.tests.fake_esri import FakeEsri, FakeLayer  # noqa: E402
from mikage.tests.qgis_fakes import wait_for  # noqa: E402
from mikage.tests.test_qgis_observations import FIELDS, TYPES, URL, plugin  # noqa: E402,F401 (the fixture)

LEFT = Qt.MouseButton.LeftButton


def mouse(dock, kind, lon, lat, dx=0):
    at = dock.overlay.to_pixel(lon, lat)
    return QgsMapMouseEvent(dock.canvas, kind, QPoint(round(at.x()) + dx, round(at.y())), LEFT, LEFT)


def double_click(dock, lon, lat):
    tool = dock.tool
    tool.canvasPressEvent(mouse(dock, QEvent.Type.MouseButtonPress, lon, lat))
    tool.canvasReleaseEvent(mouse(dock, QEvent.Type.MouseButtonRelease, lon, lat))
    tool.canvasDoubleClickEvent(mouse(dock, QEvent.Type.MouseButtonDblClick, lon, lat))
    tool.canvasReleaseEvent(mouse(dock, QEvent.Type.MouseButtonRelease, lon, lat))


def click(dock, lon, lat):
    dock.tool.canvasPressEvent(mouse(dock, QEvent.Type.MouseButtonPress, lon, lat))
    dock.tool.canvasReleaseEvent(mouse(dock, QEvent.Type.MouseButtonRelease, lon, lat))


def pixel_m(dock):
    """Two of the map's pixels in metres (a click lands on a whole pixel)."""
    return 2 * dock.canvas.mapUnitsPerPixel() * 111_320


def key(dock, k):
    dock.tool.keyPressEvent(QKeyEvent(QEvent.Type.KeyPress, k, Qt.KeyboardModifier.NoModifier))


def measured_sample(dock):
    """A car measured by three points (rear, front, side) and saved from the form: the 見本."""
    dock.set_active(True)
    dock.set_points(3)
    dock.map_clicked(139.70, 35.600, None)
    dock.map_clicked(139.70, 35.6005, None)
    dock.map_clicked(139.7001, 35.60025, None)
    dock.kind_input.setText("車両 > 乗用車")
    dock._pick_activity("駐車")
    dock.confidence_select.setCurrentIndex(dock.confidence_select.findData("推定"))
    dock.submit()
    [car] = dock.book.living()
    assert dock.many.sample.record_id == car.id
    return car


def test_stamps_by_double_click_turns_by_drag_and_takes_back(plugin):
    p, iface, dock, layers, _ = plugin
    book = dock.book
    car = measured_sample(dock)
    assert car.object_name == "乗用車-1" and car.heading == pytest.approx(0, abs=0.01) and car.width > 10
    dock.stamp_button.setChecked(True)
    assert dock.many.mode == "stamp" and dock.many.isVisibleTo(dock) and dock.active
    assert dock.many.sample_label.text().startswith("乗用車-1（乗用車・駐車・推定・")
    assert dock.status.text().startswith("ダブルクリック: 見本と同じ向きで記録")
    # A double click: one more as the 見本 lies.
    double_click(dock, 139.75, 35.65)
    rows = living(book.records)
    assert len(rows) == 2
    two = next(o for o in rows if o.id != car.id)
    assert (two.object_name, two.kind, two.activity, two.confidence, two.length, two.width, two.heading) == (
        "乗用車-2", car.kind, "駐車", "推定", car.length, car.width, car.heading)
    assert get_distance((two.lon, two.lat), (139.75, 35.65)) < pixel_m(dock)
    assert dock.status.text() == "1 件を見本で記録"
    # A single click on nothing waits for a second one, then only says how.
    click(dock, 139.65, 35.62)
    assert dock.tool._waiting is not None and len(living(book.records)) == 2
    assert wait_for(lambda: dock.tool._waiting is None, 3000)
    assert len(living(book.records)) == 2 and dock.status.text().startswith("ダブルクリック:")
    # A single click on a record opens it, as before.
    click(dock, two.lon, two.lat)
    dock.tool._click_waited()
    assert dock.form.isVisible() and dock.pending[2].id == two.id and dock.many.pick_button.isVisibleTo(dock)
    dock.many.use_open_record()
    assert dock.many.sample.record_id == two.id
    dock.escape()
    assert not dock.many.pick_button.isVisibleTo(dock)
    assert not dock.form.isVisible() and dock.many.mode == "stamp"
    # Pressed and dragged east: turned to 090°.
    tool = dock.tool
    tool.canvasPressEvent(mouse(dock, QEvent.Type.MouseButtonPress, 139.66, 35.66))
    tool.canvasMoveEvent(mouse(dock, QEvent.Type.MouseMove, 139.66, 35.66, dx=40))
    assert dock.many.turning is not None and dock.many.turning[2] == pytest.approx(90, abs=1)
    assert any(m.role == "slot" and m.outline for m in dock.scene().marks)
    tool.canvasReleaseEvent(mouse(dock, QEvent.Type.MouseButtonRelease, 139.66, 35.66, dx=40))
    three = max(living(book.records), key=lambda o: o.created_at)
    assert three.object_name == "乗用車-3" and three.heading == pytest.approx(90, abs=1)
    assert get_distance((three.lon, three.lat), (139.66, 35.66)) < pixel_m(dock)
    # Backspace on the map: the last one taken back.
    key(dock, Qt.Key.Key_Backspace)
    assert [o.object_name for o in living(book.records)] == ["乗用車-1", "乗用車-2"]
    # A double click on a record is not recorded over it.
    double_click(dock, two.lon, two.lat)
    assert len(living(book.records)) == 2 and dock.status.text().startswith("記録の上")
    # The next image: a double click on a hollow mark continues that thing.
    dock.step(1)
    # (Clicks hit what the map has painted.)
    assert wait_for(lambda: any(m.role == "ghost" for _at, m in dock.overlay.painted), 3000)
    ghost = next(m for m in dock.scene().marks if m.role == "ghost" and m.label == "乗用車-2")
    double_click(dock, ghost.lon, ghost.lat)
    [again] = book.here()
    assert again.object_id == two.object_id and again.image_id == "d2.tif"
    key(dock, Qt.Key.Key_Backspace)
    assert book.here() == []
    # Esc ends 連続 (recording goes on).
    dock.escape()
    assert dock.many.mode == "" and not dock.stamp_button.isChecked() and not dock.many.isVisibleTo(dock) and dock.active


def test_records_deleted_elsewhere_are_not_the_sample_nor_taken_back_again(plugin):
    """tsudo 2026-11-09: 「消したとき連続作成の方に反映されない…もうないやつが親になるのも変」."""
    p, iface, dock, layers, _ = plugin
    book = dock.book
    car = measured_sample(dock)
    dock.stamp_button.setChecked(True)
    for lon in (139.72, 139.74, 139.76):
        double_click(dock, lon, 35.65)
    one, two, three = sorted((o for o in living(book.records) if o.id != car.id), key=lambda o: o.created_at)
    assert [o.object_name for o in (one, two, three)] == ["乗用車-2", "乗用車-3", "乗用車-4"]
    # The 見本 follows its record as it is now.
    dock.do("変更", lambda b: b.update(car.id, length=7.5))
    assert dock.many.sample.length == 7.5 and "7.5 × " in dock.many.sample_label.text()
    # The 見本 deleted from the list: no longer the 見本; this image's newest record is.
    dock.remove_selected([book.record(car.id)], ask=False)
    assert dock.many.sample.record_id == three.id and dock.many.sample_label.text().startswith("乗用車-4（")
    double_click(dock, 139.78, 35.65)
    four = max(living(book.records), key=lambda o: o.created_at)
    assert (four.object_name, four.length) == ("乗用車-5", three.length)
    # The last ones deleted from the list: Backspace takes back the last still there (乗用車-3), not nothing.
    dock.remove_selected([book.record(four.id), book.record(three.id)], ask=False)
    assert dock.many.sample.record_id == two.id
    key(dock, Qt.Key.Key_Backspace)
    assert [o.object_name for o in living(book.records)] == ["乗用車-2"]
    assert dock.many.sample.record_id == one.id
    # Deleted from the list too: nothing left to take back, and the button says so.
    dock.remove_selected([book.record(one.id)], ask=False)
    assert not dock.many.undo_button.isEnabled() and dock.many.made == []
    assert dock.many.sample is None and dock.many.sample_label.text().startswith("なし")
    key(dock, Qt.Key.Key_Backspace)
    assert dock.status.text().startswith("取り消せる記録なし")


def test_rows_and_grids_are_laid_out_left_out_and_recorded_in_one(plugin):
    p, iface, dock, layers, _ = plugin
    book = dock.book
    car = measured_sample(dock)
    dock.row_button.setChecked(True)
    assert dock.many.mode == "row" and dock.many.row_box.isVisibleTo(dock)
    assert dock.status.text() == "列の始め（最初の 1 つの中心）をクリック"
    a, b = (139.62, 35.62), (139.66, 35.62)
    dock.map_clicked(*a, None)
    dock.map_hovered(*b)
    assert len([m for m in dock.scene().marks if m.role == "slot" and m.key]) == 2
    dock.map_clicked(*b, None)
    # How many fit: the 見本 lies across the row (heading 0, the row east), as wide as it is.
    expected = round(get_distance(a, b) / (car.width * 1.1)) + 1
    assert abs(dock.many.columns_input.value() - expected) <= 1
    dock.many.columns_input.setValue(5)
    slots = [m for m in dock.scene().marks if m.role == "slot" and m.key]
    assert [m.key for m in slots] == [f"slot:{i}" for i in range(5)]
    assert dock.many.spacing_label.text().startswith("間隔 ")
    # The third left out (a gap in the row), and ＋ on the map: none more.
    dock.map_clicked(slots[2].lon, slots[2].lat, slots[2])
    assert dock.many.layout_.skipped == {2}
    assert not next(m for m in dock.scene().marks if m.key == "slot:2").present
    assert dock.map_entered()
    made = [o for o in living(book.records) if o.id != car.id]
    assert len(made) == 4 and {o.heading for o in made} == {car.heading} and dock.status.text() == "4 件を見本で記録"
    assert sorted(o.object_name for o in made) == ["乗用車-2", "乗用車-3", "乗用車-4", "乗用車-5"]
    assert min(get_distance((o.lon, o.lat), a) for o in made) < 1
    # Taken back together.
    assert dock.many.undo()
    assert [o.id for o in living(book.records)] == [car.id]
    # Along the row; a grid of 3 × 2 by a third click.
    dock.many.how_select.setCurrentIndex(dock.many.how_select.findData("along"))
    dock.many.shape_select.setCurrentIndex(dock.many.shape_select.findData("grid"))
    c = (139.62, 35.60)
    for place in (a, b):
        dock.map_clicked(*place, None)
    assert not dock.many.layout_.ready and dock.status.text() == "最後の列の始め（最初の 1 つの中心）をクリック"
    dock.map_clicked(*c, None)
    dock.many.columns_input.setValue(3)
    dock.many.rows_input.setValue(2)
    assert dock.many.layout_.ready
    dock.many.submit_row()
    made = [o for o in living(book.records) if o.id != car.id]
    assert len(made) == 6
    # Along the row (east), of its two ways the one nearer the 見本's (north): 090°.
    assert {round(o.heading) for o in made} == {90}
    far = max(made, key=lambda o: get_distance((o.lon, o.lat), a))
    assert get_distance((far.lon, far.lat), (b[0], c[1])) < 1
    # Esc takes the last corner back, then ends 並べる.
    dock.map_clicked(*a, None)
    dock.escape()
    assert dock.many.layout_.corners == () and dock.many.mode == "row"
    dock.escape()
    assert dock.many.mode == "" and not dock.row_button.isChecked()
    # Measuring by points (L) ends them too.
    dock.row_button.setChecked(True)
    dock.set_points(2)
    assert dock.many.mode == "" and not dock.row_button.isChecked()


def painted_slot_at(dock, lon, lat):
    """Whether the map has painted a place of the row under that point (what a click there hits)."""
    at = dock.overlay.to_pixel(lon, lat)
    return wait_for(lambda: dock.overlay.hit(at.x(), at.y(), ("slot",), keyed=True) is not None, 3000)


def test_the_last_corner_is_put_down_by_a_click_on_the_map(plugin):
    # tsudo 2026-11-11: 「列作成のほうが、最後のポイントを打てない」. Before the last corner the last place
    # follows the pointer, so the click that puts the corner down is on it: it must not leave it out.
    p, iface, dock, layers, _ = plugin
    measured_sample(dock)
    dock.row_button.setChecked(True)
    a, b, c = (139.62, 35.62), (139.66, 35.62), (139.62, 35.60)
    click(dock, *a)
    dock.tool.canvasMoveEvent(mouse(dock, QEvent.Type.MouseMove, *b))
    assert painted_slot_at(dock, *b)
    click(dock, *b)
    L = dock.many.layout_
    assert L.ready and len(L.corners) == 2 and L.skipped == frozenset()
    assert dock.status.text().startswith("数を合わせ")
    # Once laid out, a click on a place still leaves it out: the two ends too (under the corners' marks).
    dock.many.columns_input.setValue(5)
    assert painted_slot_at(dock, *b)
    click(dock, *b)
    assert L.skipped == {4} and L.ready
    click(dock, *a)
    assert L.skipped == {0, 4} and L.ready
    assert dock.map_entered()
    # A grid: the third corner too.
    dock.many.shape_select.setCurrentIndex(dock.many.shape_select.findData("grid"))
    click(dock, *a)
    click(dock, *b)
    assert not L.ready
    dock.tool.canvasMoveEvent(mouse(dock, QEvent.Type.MouseMove, *c))
    assert painted_slot_at(dock, *c)
    click(dock, *c)
    assert L.ready and len(L.corners) == 3 and L.skipped == frozenset()


def test_turning_a_mode_on_lets_a_new_record_being_made_go(plugin):
    # tsudo 2026-11-11: 「元が作成中だとどの状態がわからなくなるので、連続ボタン押したら、作成中は外してほしい」.
    p, iface, dock, layers, _ = plugin
    car = measured_sample(dock)
    dock.set_points(1)
    for button in (dock.stamp_button, dock.row_button):
        dock.map_clicked(139.65, 35.63, None)
        assert dock.form.isVisible() and dock.pending[2] is None
        dock.object_input.setText("作りかけ")
        button.setChecked(True)
        assert not dock.form.isVisible() and dock.pending is None and dock.many.mode
        dock.set_many_mode("")
    # Points clicked for a measured one are let go too.
    dock.set_points(2)
    dock.map_clicked(139.65, 35.63, None)
    assert dock.rear is not None
    dock.row_button.setChecked(True)
    assert dock.rear is None and dock.many.mode == "row"
    dock.set_many_mode("")
    # A record open in the form stays (it can be made the 見本).
    dock.set_points(1)
    dock.open_form(car.lon, car.lat, editing=car)
    dock.stamp_button.setChecked(True)
    assert dock.form.isVisible() and dock.pending[2].id == car.id and dock.many.pick_button.isVisibleTo(dock)
    assert [o.id for o in living(dock.book.records)] == [car.id]


def esri_store(p, tmp_path, fake):
    layer = fake.add_layer(URL, FakeLayer({n: TYPES.get(n, "esriFieldTypeString") for n in FIELDS + ["WIDTH_M"]}))
    config = tmp_path / "config.json"
    config.write_text(json.dumps({"observationStores": [{"label": "共有", "url": URL}], "localObservations": False}), encoding="utf-8")
    settings.set_config_source(str(config))
    p.context.esri = lambda: EsriClient(fake)
    p.context.reload()
    return layer


def test_on_an_esri_store_what_is_put_down_meanwhile_goes_together_and_waits_when_refused(plugin, tmp_path):
    p, iface, dock, layers, _ = plugin
    fake = FakeEsri()
    layer = esri_store(p, tmp_path, fake)
    assert wait_for(lambda: dock.book is not None and dock.book.loaded)
    dock.set_active(True)
    dock.stamp_button.setChecked(True)
    for i in range(6):
        double_click(dock, 139.61 + i * 0.01, 35.61)
    assert wait_for(lambda: len(layer.rows) == 6 and not dock.many._sending and not dock.many.waiting)
    edits = [f for u, f in fake.asked if u.endswith("/applyEdits")]
    assert 1 <= len(edits) <= 2
    assert sorted(r["attributes"]["OBJECT_NAME"] for r in layer.rows) == sorted(f"船舶-{i}" for i in range(1, 7))
    # Not saved: kept in 送信待ち, nothing lost.
    fake.layers.pop(URL)
    double_click(dock, 139.70, 35.65)
    assert wait_for(lambda: len(dock.book.drafts()) == 1 and not dock.many._sending)
    assert dock.book.drafts()[0].record.object_name == "船舶-7"


def cars_image(path):
    """
    A car park on a GeoTIFF (WGS 84, pixels about 0.55 m): dark ground with a
    row of light cars 12 × 5 pixels facing north and three more turned.
    """
    dx, dy = 0.000006, 0.000005
    x0, y0 = 139.70, 35.60
    rng = np.random.default_rng(7)
    img = rng.normal(70, 4, (240, 320))
    yy, xx = np.mgrid[0:240, 0:320]
    places = []

    def car(cx, cy, heading, value):
        p = math.radians(heading)
        ddx, ddy = xx + 0.5 - cx, yy + 0.5 - cy
        v = ddx * math.sin(p) - ddy * math.cos(p)
        u = ddx * math.cos(p) + ddy * math.sin(p)
        img[(np.abs(v) <= 6) & (np.abs(u) <= 2.5)] = value
        img[(v > 2.5) & (v <= 4.5) & (np.abs(u) <= 1.8)] = 30
        places.append((x0 + cx * dx, y0 - cy * dy, heading))

    for i in range(7):
        car(30 + i * 9, 40, 0, 200)
    for i, h in enumerate((90, 135, 270)):
        car(80 + i * 60, 160, h, 170)
    ds = gdal.GetDriverByName("GTiff").Create(str(path), 320, 240, 1, gdal.GDT_Byte)
    ds.SetGeoTransform([x0, dx, 0, y0, 0, -dy])
    srs = osr.SpatialReference()
    srs.ImportFromEPSG(4326)
    ds.SetProjection(srs.ExportToWkt())
    ds.SetMetadataItem("ACQUISITION_START_TIME", "2026-10-05T01:00:00Z")
    ds.GetRasterBand(1).WriteArray(np.clip(img, 0, 255).astype(np.uint8))
    ds = None
    return places


def test_finds_cars_like_the_sample_and_records_them_from_ai_results(plugin, tmp_path, monkeypatch):
    p, iface, dock, layers, _ = plugin
    # (What an earlier test left unsent is not asked about meanwhile.)
    from mikage.ui import offline_status
    monkeypatch.setattr(offline_status, "ask_upload", lambda parent, w, back: "later")
    places = cars_image(tmp_path / "park.tif")
    park = QgsRasterLayer(str(tmp_path / "park.tif"), "park")
    assert park.isValid()
    QgsProject.instance().addMapLayer(park)
    iface.setActiveLayer(park)
    assert dock.book.current.name == "park.tif"
    iface.canvas.setExtent(park.extent())
    book = dock.book
    # The first car recorded by hand (12 × 5 pixels, facing north).
    lon, lat, _h = places[0]
    sample = book.add(lon, lat, kind="車両 > 乗用車", activity="駐車", confidence="推定", length=6.6, width=2.75, heading=0)
    dock.many.use_record(sample)
    dock.stamp_button.setChecked(True)
    dock.many.threshold_input.setValue(0.7)
    dock.many.find_similar()
    assert dock.many.searching and not dock.many.find_button.isEnabled()
    assert wait_for(lambda: not dock.many.searching, 60000)
    found = [x for x in QgsProject.instance().mapLayers().values() if isinstance(x, QgsVectorLayer)]
    assert len(found) == 1, dock.status.text()
    [layer] = found
    first_id = layer.id()
    assert layer.name() == "似たもの（乗用車・park.tif）"
    assert dock.status.text().startswith(f"見本に似たもの {len(places) - 1} 件（見本 1 件から。「AI結果」で確認して取り込み")
    features = list(layer.getFeatures())
    # Every other car (the 見本 itself not), each at its place, turned as it is.
    assert len(features) == len(places) - 1
    for f in features:
        c = f.geometry().centroid().asPoint()
        nearest = min(places[1:], key=lambda q: get_distance((q[0], q[1]), (c.x(), c.y())))
        assert get_distance((nearest[0], nearest[1]), (c.x(), c.y())) < 2.5
        # Its axis (alike cars may be found front to back).
        assert abs((f["heading"] - nearest[2] + 90) % 180 - 90) <= 20
        assert f["class"] == "車両 > 乗用車" and f["model"] == "見本照合" and f["score"] >= 0.7
    # 「AI結果」 lists them; four checked right are recorded as the 見本, one is checked wrong.
    results = p.api.ai_results
    assert wait_for(lambda: len(results.rows) == len(features))
    saved = results.import_rows([0, 1, 2, 3])
    assert len(saved) == 4
    assert {(o.kind, o.activity, o.confidence, o.length, o.width) for o in saved} == {("車両 > 乗用車", "駐車", "推定", 6.6, 2.75)}
    assert all("見本照合" in o.note for o in saved)
    assert len(results.reject_rows([4])) == 1
    wrong = results.rows[4][1]
    # Searched again: learnt from the five 見本 and the one wrong; those checked are not found again (the cars left
    # come first), and the list takes the last one's place.
    dock.many.find_similar()
    assert wait_for(lambda: not dock.many.searching, 60000)
    assert dock.status.text().startswith("見本に似たもの ") and "件（見本 5 件・誤検出 1 件から。" in dock.status.text()
    [again] = [x for x in QgsProject.instance().mapLayers().values() if isinstance(x, QgsVectorLayer)]
    assert again.id() != first_id and again.name() == "似たもの（乗用車・park.tif）"
    c0 = wrong.geometry.centroid().asPoint()
    checked = [(o.lon, o.lat) for o in [sample, *saved]] + [(c0.x(), c0.y())]
    left = [q for q in places if all(get_distance((q[0], q[1]), c) > 1 for c in checked)]
    assert len(left) == len(places) - 6
    ranked = sorted(again.getFeatures(), key=lambda f: -f["score"])
    for f in ranked[:len(left)]:
        c = f.geometry().centroid().asPoint()
        assert min(get_distance((q[0], q[1]), (c.x(), c.y())) for q in left) < 1.5
    for f in ranked:
        c = f.geometry().centroid().asPoint()
        assert all(get_distance(q, (c.x(), c.y())) > 1.5 for q in checked)
    # The cars left recorded too: none of them found again (at most a stray place of the noisy ground).
    assert wait_for(lambda: len(results.rows) == again.featureCount())
    rows = sorted(range(len(results.rows)), key=lambda i: -(results.rows[i][1].score or 0))[:len(left)]
    rest = results.import_rows(rows)
    assert len(rest) == len(left)
    turned = sorted(round(o.heading) % 360 for o in [*saved, *rest] if round(o.heading) % 360 != 0)
    assert len(turned) >= 2
    dock.many.threshold_input.setValue(0.9)
    dock.many.find_similar()
    assert wait_for(lambda: not dock.many.searching, 60000)
    assert "（見本 9 件・誤検出 1 件から" in dock.status.text() or dock.status.text().startswith("見本に似たものは見つかりませんでした")
    for lay in QgsProject.instance().mapLayers().values():
        if isinstance(lay, QgsVectorLayer):
            assert lay.featureCount() <= 2
            for f in lay.getFeatures():
                c = f.geometry().centroid().asPoint()
                assert all(get_distance((q[0], q[1]), (c.x(), c.y())) > 1.5 for q in places)


def test_the_search_says_what_it_needs(plugin, tmp_path):
    p, iface, dock, layers, _ = plugin
    dock.set_active(True)
    dock.stamp_button.setChecked(True)
    dock.many.find_similar()
    assert dock.status.text().startswith("見本なし: この画像で 1 つを 2〜4 点")
    # A 見本 far too small for the image's pixels (25 m here).
    dock.book.add(139.7, 35.6, kind="車両", length=4.5, width=1.8, heading=0)
    dock.many.find_similar()
    assert dock.status.text().startswith("見本が小さすぎます")
    assert bearing((0, 0), (1, 0)) == pytest.approx(90)
