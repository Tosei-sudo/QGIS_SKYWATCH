"""
サーバーが遅いとき (tsudo 2026-11-28), in the 観測記録 dock with a shared store:
a record confirmed is listed and drawn at once as 「送信中」 and sent behind the
screen; one not saved comes back in the form; what saves goes before the reads
waiting.
"""
import json
import threading

from mikage.tests.test_qgis_observations import FIELDS, TYPES, URL, _listed, bare  # noqa: F401 (the fixture)
from mikage.core.esri import EsriClient
from mikage.qgis_io import settings
from mikage.tests.fake_esri import FakeEsri, FakeLayer
from mikage.tests.qgis_fakes import wait_for


class SlowEsri(FakeEsri):
    """A fake server whose edits wait until let go (a slow server)."""

    def __init__(self):
        super().__init__()
        self.hold = threading.Event()
        self.hold.set()

    def answer(self, url, form):
        if url.endswith("applyEdits"):
            self.hold.wait(10)
        return super().answer(url, form)


AREAS = "https://example.com/arcgis/rest/services/Obs/FeatureServer/1"
AREA_FIELDS = ("AREA_ID", "IMAGE_ID", "IMAGE_NAME", "IMAGE_TIME", "KIND", "NOTE", "OBSERVER", "CREATED_AT", "UPDATED_AT", "DELETED_AT",
               "UNSEEN_REASON")


def _shared(p, dock, tmp_path, fake, areas=False):
    layer = fake.add_layer(URL, FakeLayer({n: TYPES.get(n, "esriFieldTypeString") for n in FIELDS}))
    store = {"label": "共有", "url": URL}
    if areas:
        fake.add_layer(AREAS, FakeLayer({n: "esriFieldTypeDate" if n.endswith(("_AT", "_TIME")) else "esriFieldTypeString"
                                         for n in AREA_FIELDS}, "esriGeometryPolygon"))
        store["areas"] = AREAS
    config = tmp_path / "config.json"
    config.write_text(json.dumps({"observationStores": [store]}), encoding="utf-8")
    settings.set_config_source(str(config))
    p.context.esri = lambda: EsriClient(fake)
    p.context.reload()
    dock.list_catalog_images([_listed("IMG1", 1)])
    assert wait_for(lambda: dock.book.loaded and not dock._running and not dock._queue)
    return layer


def test_a_record_confirmed_is_listed_at_once_and_sent_behind(bare):
    p, iface, dock, tmp_path = bare
    fake = SlowEsri()
    layer = _shared(p, dock, tmp_path, fake)
    fake.hold.clear()
    dock.set_active(True)
    dock.map_clicked(139.7, 35.6, None)
    dock.object_input.setText("あおば")
    dock.kind_input.setText("車両")
    dock.submit()
    # Listed and drawn now, marked 送信中, while the server has not answered (車両: no individual's name to look up).
    assert not dock.form.isVisible() and not layer.rows
    [o] = dock.book.living()
    assert dock.sending_ids() == {o.id}
    assert dock.table.item(0, 0).text() == "あおば" and dock.table.item(0, 0).toolTip().startswith("送信中")
    assert any(m.label.endswith("（送信中）") for m in dock.scene().marks if m.key == o.id)
    assert dock.offline_note.isVisible() and "送信中の記録 1 件" in dock.offline_note.text()
    # Recording goes on meanwhile: the next one is held too, and goes with the next send.
    dock.map_clicked(139.71, 35.6, None)
    dock.object_input.setText("しらせ")
    dock.kind_input.setText("車両")
    dock.submit()
    assert len(dock.book.living()) == 2 and len(dock.sending_ids()) == 2
    fake.hold.set()
    assert wait_for(lambda: len(layer.rows) == 2 and not dock.sending_ids())
    assert wait_for(lambda: not dock._running and not dock._queue)
    assert not dock.table.item(0, 0).toolTip().startswith("送信中") and not dock.offline_note.isVisible()


def test_a_record_not_saved_comes_back_in_the_form(bare):
    p, iface, dock, tmp_path = bare
    fake = FakeEsri()
    layer = _shared(p, dock, tmp_path, fake)
    dock.set_active(True)
    dock.map_clicked(139.7, 35.6, None)
    dock.object_input.setText("あおば")
    dock.kind_input.setText("車両")
    dock.note_input.setText("入港直後")
    fake.fail_next = {"error": {"code": 500, "message": "busy"}}
    dock.submit()
    assert wait_for(lambda: dock.form.isVisible())
    assert dock.object_input.text() == "あおば" and dock.note_input.text() == "入港直後"
    assert any("送信待ちに保管" in w.text() for w in iface.bar.items())
    assert dock.book.living() == [] and [d.record.object_name for d in dock.book.drafts()] == ["あおば"]
    dock.submit()
    assert wait_for(lambda: len(layer.rows) == 1 and not dock.sending_ids())
    assert wait_for(lambda: dock.book.drafts() == [])


def test_what_saves_goes_before_the_reads_waiting(bare):
    p, iface, dock, tmp_path = bare
    _shared(p, dock, tmp_path, FakeEsri())
    ran = []
    dock._running = True  # (one runs: the others wait in the queue)
    try:
        dock.do("読み 1", lambda b: ran.append("読み 1"), read=True)
        dock.do("読み 2", lambda b: ran.append("読み 2"), read=True)
        dock.do("保存 1", lambda b: ran.append("保存 1"))
        dock.do("保存 2", lambda b: ran.append("保存 2"))
        assert [x[0] for x in dock._queue] == ["保存 1", "保存 2", "読み 1", "読み 2"]
    finally:
        dock._running = False
    dock._next()
    assert wait_for(lambda: ran == ["保存 1", "保存 2", "読み 1", "読み 2"])


def _area(dock, note):
    dock.set_area_mode(True)
    dock.map_clicked(139.69, 35.59, None)
    dock.map_clicked(139.72, 35.62, None)
    dock.area_note.setText(note)
    dock._submit_area()


def test_a_checked_area_is_shown_at_once_and_sent_behind(bare):
    p, iface, dock, tmp_path = bare
    fake = SlowEsri()
    _shared(p, dock, tmp_path, fake, areas=True)
    areas = fake.layers[AREAS]
    dock.set_active(True)
    fake.hold.clear()
    fake.asked.clear()
    _area(dock, "港内")
    # Shown now (送信中) and the mode left, while the server has not answered.
    assert not dock.area_mode and not areas.rows
    [a] = dock.book.image_areas()
    assert dock.book.areas_sending() == {a.id} and a.note == "港内"
    assert any(label.endswith("（送信中）") for _ring, label in dock.scene().areas)
    fake.hold.set()
    assert wait_for(lambda: len(areas.rows) == 1 and not dock.book.areas_sending())
    assert wait_for(lambda: not dock._running and not dock._queue)
    assert not any(label.endswith("（送信中）") for _ring, label in dock.scene().areas)
    # Made just now: added without asking the layer about it first (one request, not two).
    assert not [u for u, _f in fake.asked if u.startswith(AREAS) and u.endswith("/query")]


def test_a_checked_area_not_saved_comes_back_in_its_form(bare):
    p, iface, dock, tmp_path = bare
    fake = FakeEsri()
    _shared(p, dock, tmp_path, fake, areas=True)
    dock.set_active(True)
    fake.fail_next = {"error": {"code": 500, "message": "busy"}}
    _area(dock, "港内")
    assert wait_for(lambda: dock.area_mode)
    assert dock.book.image_areas() == [] and not dock.book.areas_sending()
    assert dock.area_note.text() == "港内" and dock.area_ring[0] == [139.69, 35.59]
    assert any("確認範囲の保存に失敗" in w.text() for w in iface.bar.items())
    dock._submit_area()
    assert wait_for(lambda: len(fake.layers[AREAS].rows) == 1 and not dock.area_mode)
