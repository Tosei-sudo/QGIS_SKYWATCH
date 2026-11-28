"""
ミカゲと話す's 画像検索 (文で操作's until 2026-11-28) with the satellite kind (tsudo 2026-11-25: 「画像検索の文から生成AIで
やるけん、衛星の種別も拾えないかな」): the card's 衛星・種別 read from the sentence by code, the catalog's sensors it names
beside it, and the run putting them in 画像カタログ's センサー (several: any of them), or clearing it when the sentence
names none. Images of a place asked for (a point, a named area, the map's extent) are searched behind the screen and
answered in the chat (how many, the newest with 「開く」), and a follow-up opens them (「いちばん新しいのを開いて」).
"""
import time

import pytest

pytest.importorskip("qgis.core")

from mikage.features.chat import Chat  # noqa: E402
from mikage.features.images import Images  # noqa: E402
from mikage.features.plan import Plan  # noqa: E402
from mikage.tests.fake_esri import FakeLayer  # noqa: E402
from mikage.tests.qgis_fakes import wait_for  # noqa: E402
from mikage.tests.test_qgis_requests import IMAGES, plugin  # noqa: E402,F401 (the fixture)
from mikage.tests.test_qgis_chat_operation import _setup, records, send  # noqa: E402,F401 (a fixture)
from mikage.ui.chat_dock import ImagesCard, NoteCard  # noqa: E402

PLUGIN_FEATURES = (Plan, Images, Chat)
DAY = 86_400_000


def ring(x0, y0, x1, y1):
    return {"rings": [[[x0, y0], [x0, y1], [x1, y1], [x1, y0], [x0, y0]]]}


def _ready(p, fake, tmp_path):
    """The image catalog with sensors of both kinds and one of none known, the satellites (OPT 1 光学, RADAR 1 SAR) read."""
    images = fake.layers[IMAGES]
    now = time.time() * 1000
    for i, sensor in enumerate(["RADAR 1", "RADAR 2", "OPT 1", "OTHER"]):
        images.add({"IMAGE_ID": f"I{i}", "ACQ": now - DAY, "SENSOR": sensor, "URL": f"https://x.example/{i}.tif"},
                   ring(139.6, 35.2, 139.8, 35.4))
    chat, _loaded = _setup(p, tmp_path, [])
    dock = p.docks["SatObsImageCatalog"]
    feature = p.feature(Chat)
    assert wait_for(lambda: dock.sensor_values() == ["OPT 1", "OTHER", "RADAR 1", "RADAR 2"])
    feature.satellites()
    assert wait_for(lambda: len(feature.satellites()) == 2)
    return chat, dock


def asked_where(fake):
    """The where of the last search of the image catalog (not the list of its sensors)."""
    forms = [f for url, f in fake.asked if url.startswith(IMAGES) and f.get("returnDistinctValues") != "true" and "where" in f]
    return forms[-1]["where"]


def test_the_kind_said_searches_the_sensors_of_that_kind(plugin, records):
    p, iface, fake, tmp_path = plugin
    chat, dock = _ready(p, fake, tmp_path)
    card = send(chat, "昨日のSAR画像を探して")
    r = card.chosen()
    assert r.operation.key == "catalog" and r.sensors == ["SAR"] and card.sensors.text() == "SAR"
    assert card.sensors_row.isVisibleTo(card) and card.run_button.isEnabled()
    # RADAR 2 is not in the satellite catalog: of the series RADAR. OTHER: no satellite, kind not known.
    assert card.sensors_note.text() == "→ RADAR 1・RADAR 2（2 センサー）。種別のわからない 1 は含めない"
    assert "OTHER" in card.sensors_note.toolTip()
    card.run_button.click()
    assert card.finished and dock.isVisible() and dock.more.is_open()
    assert dock.sensor_box.currentText() == "RADAR 1、RADAR 2"
    assert wait_for(lambda: dock.search_button.isEnabled() and "SENSOR IN ('RADAR 1', 'RADAR 2')" in asked_where(fake))
    assert card.message.endswith("・SAR（2 センサー） を検索")
    assert records[-1][3]["detail"]["sensors"] == ["SAR"]


def test_a_satellite_said_and_the_value_changed_by_hand(plugin):
    p, iface, fake, tmp_path = plugin
    chat, dock = _ready(p, fake, tmp_path)
    card = send(chat, "OPT 1 の昨日の画像を探して")
    assert card.chosen().sensors == ["OPT 1"] and card.sensors_note.text() == "→ OPT 1（1 センサー）"
    # A name of no sensor of the catalog: said, the button off until it is put right or cleared.
    card.sensors.setText("NOPE")
    assert card.sensors_note.text() == "当たるセンサーが画像カタログにない" and not card.run_button.isEnabled()
    card.sensors.setText("光学")
    assert card.sensors_note.text().startswith("→ OPT 1（1 センサー）") and card.run_button.isEnabled()
    card.run_button.click()
    assert dock.sensor_box.currentText() == "OPT 1"
    assert wait_for(lambda: dock.search_button.isEnabled() and "SENSOR = 'OPT 1'" in asked_where(fake))
    # A sentence naming none: every sensor (the box cleared of what it had).
    card = send(chat, "昨日の画像を探して")
    assert card.chosen().sensors == [] and card.sensors_note.text() == "（なし: すべてのセンサー）"
    card.run_button.click()
    assert dock.sensor_box.currentText() == ""
    assert wait_for(lambda: dock.search_button.isEnabled() and "SENSOR" not in asked_where(fake))


def test_ir_said_searches_the_ir_sensors(plugin):
    """衛星の種別 (`satelliteKinds`, 2026-11-26): 「赤外」 is IR, the sensors of the satellites whose kind attribute says so."""
    p, iface, fake, tmp_path = plugin
    from mikage.tests.test_plan import LINE1, LINE2
    from mikage.tests.test_qgis_requests import SATS

    fake.layers[SATS].add({"NAME": "THERM 1", "TLE": f"{LINE1}\n{LINE2}", "KIND": "熱赤外", "SERIES": "THERM", "RES": 3})
    images = fake.layers[IMAGES]
    images.add({"IMAGE_ID": "I9", "ACQ": time.time() * 1000 - DAY, "SENSOR": "THERM 1", "URL": "https://x.example/9.tif"},
               ring(139.6, 35.2, 139.8, 35.4))
    chat, _loaded = _setup(p, tmp_path, [])
    dock = p.docks["SatObsImageCatalog"]
    feature = p.feature(Chat)
    assert wait_for(lambda: "THERM 1" in (dock.sensor_values() or []))
    feature.satellites()
    assert wait_for(lambda: len(feature.satellites()) == 3)
    assert [s.kind_name for s in feature.satellites()] == ["光学", "SAR", "IR"]
    card = send(chat, "昨日の赤外画像を探して")
    assert card.chosen().operation.key == "catalog" and card.sensors.text() == "IR"
    assert card.sensors_note.text().startswith("→ THERM 1（1 センサー）")
    card.run_button.click()
    assert dock.sensor_box.currentText() == "THERM 1"


def test_images_of_a_place_answered_in_the_chat_and_opened_by_a_follow_up(plugin, records):
    """「35.3, 139.7 の昨日のSAR画像を見せて」: searched behind the screen (no card, no model), the newest listed, then opened."""
    p, iface, fake, tmp_path = plugin
    chat, dock = _ready(p, fake, tmp_path)
    images = fake.layers[IMAGES]
    now = time.time() * 1000
    images.add({"IMAGE_ID": "I8", "ACQ": now - DAY + 60_000, "SENSOR": "RADAR 1", "URL": "https://x.example/8.tif"},
               ring(139.6, 35.2, 139.8, 35.4))
    opened = []
    dock.open_catalog_records = lambda catalog, records: opened.append([r.id for r in records]) or []
    card = send(chat, "35.3, 139.7 の昨日のSAR画像を見せて", ImagesCard)
    assert not dock.isVisible() and chat.last(NoteCard) is None
    # The point's square searched, the SAR sensors only; the newest first.
    where = asked_where(fake)
    assert "SENSOR IN ('RADAR 1', 'RADAR 2')" in where
    assert [r.id for r in card.found.records] == ["I8", "I0", "I1"]
    assert card.said.text().endswith("・SAR（2 センサー）: 3 枚") and len(card.open_buttons) == 3
    # A follow-up: the newest opened as from the catalog's list.
    assert chat.send("いちばん新しいのを開いて")
    assert opened == [["I8"]] and chat.last(NoteCard).text() == "画像を開いた: I8"
    # A card's button, and 「カタログで見る」 (the dock in front, the records listed).
    card.open_buttons[1].click()
    assert opened[-1] == [card.found.records[1].id]
    card.catalog_button.click()
    assert dock.isVisible() and [r.id for r in dock.records] == [r.id for r in card.found.records]
    # Nothing of 画像検索 recorded as an operation (no card was pressed).
    assert not [r for r in records if r[0] == "command"]


def test_images_searched_without_the_roles_for_the_catalog_are_not(plugin, monkeypatch):
    p, iface, fake, tmp_path = plugin
    chat, dock = _ready(p, fake, tmp_path)
    monkeypatch.setattr(p, "allows", lambda feature: feature != "画像カタログ")
    assert chat.send("35.3, 139.7 の昨日の画像を見せて")
    assert wait_for(lambda: not chat.busy)
    assert not chat.cards(ImagesCard)


def test_images_searched_in_every_catalog_or_the_one_named(plugin):
    """
    Two catalogs (tsudo 2026-11-28 「複数画像カタログがある場合はどうなる？」): both searched at once, each one's SAR
    sensors, the images together newest first with their catalog, each count said, a 「…で見る」 for each, and one
    opened with its own catalog; a sentence naming one searches that one only.
    """
    p, iface, fake, tmp_path = plugin
    radar = "https://example.com/arcgis/rest/services/Radar/FeatureServer/0"
    fake.add_layer(radar, FakeLayer({"IMAGE_ID": "esriFieldTypeString", "ACQ": "esriFieldTypeDate", "SENSOR": "esriFieldTypeString",
                                     "URL": "esriFieldTypeString"}, "esriGeometryPolygon"))
    now = time.time() * 1000
    for i, (sensor, age) in enumerate([("RADAR 1", 3), ("RADAR 2", 1), ("OPT 1", 2)]):
        fake.layers[IMAGES].add({"IMAGE_ID": f"A{i}", "ACQ": now - age * 3_600_000, "SENSOR": sensor, "URL": f"https://x.example/a{i}.tif"},
                                ring(139.6, 35.2, 139.8, 35.4))
    fake.layers[radar].add({"IMAGE_ID": "B0", "ACQ": now - 2 * 3_600_000, "SENSOR": "RADAR 1", "URL": "https://x.example/b0.tif"},
                           ring(139.6, 35.2, 139.8, 35.4))
    fields = {"id": "IMAGE_ID", "acquired": "ACQ", "sensor": "SENSOR", "source": "URL"}
    catalogs = [{"label": "本番", "url": IMAGES, "fields": fields}, {"label": "レーダー", "url": radar, "fields": fields}]
    chat, _loaded = _setup(p, tmp_path, [], extra={"imageCatalogs": catalogs})
    dock = p.docks["SatObsImageCatalog"]
    feature = p.feature(Chat)
    feature.satellites()
    assert wait_for(lambda: len(feature.satellites()) == 2)
    opened = []
    dock.open_catalog_records = lambda catalog, records: opened.append((catalog.label, [r.id for r in records])) or []
    card = send(chat, "35.3, 139.7 の今日のSAR画像を見せて", ImagesCard)
    # Both, newest first, each with its catalog; the 光学 one left out by the sensors.
    assert [r.id for r in card.found.records] == ["A1", "B0", "A0"]
    assert [c.label for c in card.found.catalogs] == ["本番", "レーダー", "本番"]
    assert card.said.text().endswith(": 3 枚（本番 2 枚・レーダー 1 枚）")
    assert [b.text() for b in card.catalog_buttons] == ["本番で見る", "レーダーで見る"]
    assert not dock.isVisible()
    # The second one opened with its own catalog; 「レーダーで見る」 lists its images in 画像カタログ.
    assert chat.send("2枚目を開いて")
    assert opened == [("レーダー", ["B0"])]
    card.catalog_buttons[1].click()
    # (Listed as イベント's images are: the list's catalog said in the status line, the form's as it was.)
    assert dock.isVisible() and [r.id for r in dock.records] == ["B0"] and dock.status.text().startswith("「レーダー」・")
    # A sentence naming a catalog: that one only, named in the answer.
    card = send(chat, "レーダーで 35.3, 139.7 の今日の画像ある？", ImagesCard)
    assert [r.id for r in card.found.records] == ["B0"] and card.said.text().startswith("「レーダー」・")
