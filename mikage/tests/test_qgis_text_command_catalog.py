"""
文で操作's 画像検索 with the satellite kind (tsudo 2026-11-25: 「画像検索の文から生成AIでやるけん、衛星の種別も
拾えないかな」): the card's 衛星・種別 read from the sentence by code, the catalog's sensors it names beside it, and
the run putting them in 画像カタログ's センサー (several: any of them), or clearing it when the sentence names none.
"""
import time

import pytest

pytest.importorskip("qgis.core")

from mikage.features.command import Command  # noqa: E402
from mikage.features.images import Images  # noqa: E402
from mikage.features.plan import Plan  # noqa: E402
from mikage.tests.qgis_fakes import wait_for  # noqa: E402
from mikage.tests.test_qgis_requests import IMAGES, plugin  # noqa: E402,F401 (the fixture)
from mikage.tests.test_qgis_text_command import _setup, records  # noqa: E402,F401 (a fixture)

PLUGIN_FEATURES = (Plan, Images, Command)
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
    box, _loaded = _setup(p, tmp_path, [])
    dock = p.docks["SatObsImageCatalog"]
    command = p.feature(Command)
    assert wait_for(lambda: dock.sensor_values() == ["OPT 1", "OTHER", "RADAR 1", "RADAR 2"])
    command.satellites()
    assert wait_for(lambda: len(command.satellites()) == 2)
    return box, dock


def asked_where(fake):
    """The where of the last search of the image catalog (not the list of its sensors)."""
    forms = [f for url, f in fake.asked if url.startswith(IMAGES) and f.get("returnDistinctValues") != "true" and "where" in f]
    return forms[-1]["where"]


def test_the_kind_said_searches_the_sensors_of_that_kind(plugin, records):
    p, iface, fake, tmp_path = plugin
    box, dock = _ready(p, fake, tmp_path)
    box.submit("昨日のSAR画像を探して")
    card = box.card
    r = card.chosen()
    assert r.operation.key == "catalog" and r.sensors == ["SAR"] and card.sensors.text() == "SAR"
    assert card.sensors_row.isVisibleTo(card) and card.run_button.isEnabled()
    # RADAR 2 is not in the satellite catalog: of the series RADAR. OTHER: no satellite, kind not known.
    assert card.sensors_note.text() == "→ RADAR 1・RADAR 2（2 センサー）。種別のわからない 1 は含めない"
    assert "OTHER" in card.sensors_note.toolTip()
    card.run_button.click()
    assert not card.isVisible() and dock.isVisible() and dock.more.isChecked()
    assert dock.sensor_box.currentText() == "RADAR 1、RADAR 2"
    assert wait_for(lambda: dock.search_button.isEnabled() and "SENSOR IN ('RADAR 1', 'RADAR 2')" in asked_where(fake))
    assert box.message.endswith("・SAR（2 センサー） を検索")
    assert records[-1][3]["detail"]["sensors"] == ["SAR"]


def test_a_satellite_said_and_the_value_changed_by_hand(plugin):
    p, iface, fake, tmp_path = plugin
    box, dock = _ready(p, fake, tmp_path)
    card = box.card
    box.submit("OPT 1 の昨日の画像を探して")
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
    box.submit("昨日の画像を探して")
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
    box, _loaded = _setup(p, tmp_path, [])
    dock = p.docks["SatObsImageCatalog"]
    command = p.feature(Command)
    assert wait_for(lambda: "THERM 1" in (dock.sensor_values() or []))
    command.satellites()
    assert wait_for(lambda: len(command.satellites()) == 3)
    assert [s.kind_name for s in command.satellites()] == ["光学", "SAR", "IR"]
    box.submit("昨日の赤外画像を探して")
    card = box.card
    assert card.chosen().operation.key == "catalog" and card.sensors.text() == "IR"
    assert card.sensors_note.text().startswith("→ THERM 1（1 センサー）")
    card.run_button.click()
    assert dock.sensor_box.currentText() == "THERM 1"
