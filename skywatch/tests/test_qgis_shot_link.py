"""
「画像とショットの関連付け」 in QGIS (tsudo, 2026-11-11: 「手の照合は、今の方法に
加えて、この画像カタログのレコードがこのショットという本当の手動関連付け画面が
ほしい」): opened from 「計画と実績の照合」 on a row's shot, the catalog's images
over it, one linked by hand (the rows, the ledger and the shot follow), and
the link taken off again.
"""
import copy
import json
import time

import pytest

pytest.importorskip("qgis.core")

from qgis.PyQt.QtCore import QDate  # noqa: E402

from skywatch.core.request.model import day_bounds, jst_day  # noqa: E402
from skywatch.core.request.store import DEFAULT_SHOT_FIELDS  # noqa: E402
from skywatch.tests import test_qgis_requests as base  # noqa: E402
from skywatch.tests.fake_esri import FakeLayer  # noqa: E402
from skywatch.tests.qgis_fakes import wait_for  # noqa: E402
from skywatch.ui import shot_link_dialog  # noqa: E402

plugin = base.plugin
SHOTS = "https://example.com/arcgis/rest/services/Req/FeatureServer/22"
H = 3600000


def _square(lon, lat=35.0, d=0.05):
    return {"rings": [[[lon - d, lat - d], [lon + d, lat - d], [lon + d, lat + d], [lon - d, lat + d], [lon - d, lat - d]]],
            "spatialReference": {"wkid": 4326}}


def _setup(plugin):
    p, _iface, fake, tmp_path = plugin
    types = {n: "esriFieldTypeString" for n in DEFAULT_SHOT_FIELDS.values()}
    types.update(RUN_DATE="esriFieldTypeDate", TARGET_DATE="esriFieldTypeDate", PASS_TIME="esriFieldTypeDate", SCENES="esriFieldTypeInteger",
                 SHOT_COUNT="esriFieldTypeInteger", REQ_COUNT="esriFieldTypeInteger")
    shots = fake.add_layer(SHOTS, FakeLayer(types, "esriGeometryPolygon"))
    config = copy.deepcopy(base.CONFIG)
    config["requestStores"][0]["shots"] = SHOTS
    (tmp_path / "config.json").write_text(json.dumps(config, ensure_ascii=False), encoding="utf-8")
    p.api.reload_config()
    assert p.api.problems == [], p.api.problems
    day = jst_day(time.time() * 1000 - 86400000)
    first, last = day_bounds(day)
    ledger, history = fake.layers[base.LEDGER], fake.layers[base.HISTORY]
    for rid, name, lon in (("R-1", "横須賀", 139.67), ("R-2", "久里浜", 139.70), ("R-3", "呉", 132.55)):
        ledger.add({"REQ_ID": rid, "REQ_NAME": name, "CATEGORY": "臨時", "ACHIEVE": "期間内1回", "STATUS": "有効", "PRIORITY": 500,
                    "BOOST": 100, "KIND": "光学", "SATELLITES": "OPT 1", "START_AT": first - 86400000, "END_AT": last + 30 * 86400000},
                   _square(lon))
    # 横須賀 and 久里浜 in one shot (X-1); 呉 in its own, already linked to IMG-B.
    for rid, name, shot, extra in (("R-1", "横須賀", "X-1", {}), ("R-2", "久里浜", "X-1", {"MERGED_INTO": "R-1"}),
                                   ("R-3", "呉", "X-2", {"CAPTURED": "撮像済み", "IMAGE_ID": "IMG-B"})):
        history.add({"RUN_ID": "RUN", "TARGET_DAY": day.isoformat(), "REQ_ID": rid, "REQ_NAME": name, "SYSTEM": "SYS-A", "SATELLITE": "OPT 1",
                     "RESULT": "採用", "CAPTURED": "未判定", "BOOST_DONE": 0, "SHOT_ID": shot, **extra})
    shots.add({"SHOT_ID": "X-1", "RUN_ID": "RUN", "TARGET_DAY": day.isoformat(), "SYSTEM": "SYS-A", "SATELLITE": "OPT 1",
               "PASS_TIME": first + 10 * H, "REQ_IDS": "R-1,R-2", "CAPTURED": "未判定"}, _square(139.685, d=0.08))
    shots.add({"SHOT_ID": "X-2", "RUN_ID": "RUN", "TARGET_DAY": day.isoformat(), "SYSTEM": "SYS-A", "SATELLITE": "OPT 1",
               "PASS_TIME": first + 11 * H, "REQ_IDS": "R-3", "CAPTURED": "撮像済み", "IMAGE_ID": "IMG-B"}, _square(132.55))
    images = fake.layers[base.IMAGES]
    images.add({"IMAGE_ID": "IMG-A", "ACQ": first + 10 * H + 5 * 60000, "SENSOR": "OPT 1"}, _square(139.68))
    images.add({"IMAGE_ID": "IMG-B", "ACQ": first + 12 * H, "SENSOR": "OPT 1"}, _square(139.69))
    images.add({"IMAGE_ID": "IMG-SAR", "ACQ": first + 10 * H, "SENSOR": "RADAR 1"}, _square(139.68))
    images.add({"IMAGE_ID": "IMG-FAR", "ACQ": first + 10 * H, "SENSOR": "OPT 1"}, _square(130.0))
    return p, fake, day


def _attrs(layer, key, value):
    return next(r["attributes"] for r in layer.rows if r["attributes"].get(key) == value)


def test_link_an_image_to_a_shot_by_hand(plugin, monkeypatch):
    p, fake, day = _setup(plugin)
    daily = p.api.plan_files
    results = daily.show_results(run=False)
    results.first.setDate(QDate(day.year, day.month, day.day))
    results.run(follow=False)
    assert wait_for(lambda: results.task is None and results.history), results.status.text()
    row = next(i for i, r in enumerate(results.rows) if r.request_id == "R-2")
    results.table.selectRow(row)
    linking = results.link_images()
    assert wait_for(lambda: linking.task is None and linking.searched is not None), linking.status.text()
    # The day's shots; the row's shot chosen; its images over it, OPT 1 only, the nearest to the pass first.
    assert [u.key for u in linking.shown_units] == ["X-1", "X-2"]
    unit = linking.chosen_unit()
    assert unit.key == "X-1" and unit.names == ["横須賀", "久里浜"]
    shown = [linking.image_model.item(i, 2).text() for i in range(linking.image_model.rowCount())]
    assert shown == ["IMG-A", "IMG-B"], linking.status.text()
    assert linking.image_model.item(0, 3).text() == "+5分"
    assert "呉" in linking.image_model.item(1, 4).text()  # linked to 呉's shot already
    linking.only_fitting.setChecked(False)
    assert [linking.image_model.item(i, 2).text() for i in range(linking.image_model.rowCount())] == ["IMG-A", "IMG-B", "IMG-SAR"]
    assert linking.overlay.painted()

    # Linked: both requests of the shot 撮像済み with IMG-A, the ledger 完了, the shot too.
    linking.image_table.selectRow(0)
    linking.link()
    assert wait_for(lambda: linking.task is None and linking.status.text().startswith("画像 IMG-A を")), linking.status.text()
    history, ledger, shots = fake.layers[base.HISTORY], fake.layers[base.LEDGER], fake.layers[SHOTS]
    for rid in ("R-1", "R-2"):
        a = _attrs(history, "REQ_ID", rid)
        assert (a["CAPTURED"], a["IMAGE_ID"]) == ("撮像済み", "IMG-A")
        assert (_attrs(ledger, "REQ_ID", rid)["STATUS"], _attrs(ledger, "REQ_ID", rid)["BOOST"]) == ("完了", 0)
    assert (_attrs(shots, "SHOT_ID", "X-1")["CAPTURED"], _attrs(shots, "SHOT_ID", "X-1")["IMAGE_ID"]) == ("撮像済み", "IMG-A")
    assert linking.shot_model.item(0, 5).text() == "撮像済み" and linking.shot_model.item(0, 6).text() == "IMG-A"
    assert linking.image_model.item(0, 4).text() == "このショット"
    # 「計画と実績の照合」 reads the rows again.
    assert wait_for(lambda: results.task is None and {r.request_id: r.captured for r in results.history}.get("R-1") == "撮像済み")

    # IMG-B instead: asked first (another shot's image, and this shot's image replaced); いいえ keeps things as they were.
    asked = []
    monkeypatch.setattr(shot_link_dialog, "ask_no", lambda parent, title, text: asked.append(text) or False)
    linking.image_table.selectRow(1)
    linking.link()
    assert len(asked) == 1 and "IMG-A" in asked[0] and "呉" in asked[0]
    assert linking.task is None and _attrs(history, "REQ_ID", "R-1")["IMAGE_ID"] == "IMG-A"
    monkeypatch.setattr(shot_link_dialog, "ask_no", lambda parent, title, text: True)
    linking.link()
    assert wait_for(lambda: linking.task is None and linking.status.text().startswith("画像 IMG-B を")), linking.status.text()
    assert _attrs(history, "REQ_ID", "R-1")["IMAGE_ID"] == "IMG-B" and _attrs(shots, "SHOT_ID", "X-1")["IMAGE_ID"] == "IMG-B"

    # Taken off again: 未判定 (the follow-up looks again), the requests 有効, the shot 未判定.
    assert linking.unlink_button.isEnabled()
    linking.unlink("未判定")
    assert wait_for(lambda: linking.task is None and "未判定に" in linking.status.text()), linking.status.text()
    for rid in ("R-1", "R-2"):
        a = _attrs(history, "REQ_ID", rid)
        assert (a["CAPTURED"], a.get("IMAGE_ID") or "") == ("未判定", "")
        assert _attrs(ledger, "REQ_ID", rid)["STATUS"] == "有効"
    assert _attrs(shots, "SHOT_ID", "X-1")["CAPTURED"] == "未判定"
    assert not linking.unlink_button.isEnabled()
    # 呉 kept its own image.
    assert _attrs(history, "REQ_ID", "R-3")["IMAGE_ID"] == "IMG-B"

    # Other days, and closing takes the shapes off the map.
    linking.open_at(day.replace(day=1) if day.day != 1 else day.replace(day=2))
    assert wait_for(lambda: linking.task is None and linking.loaded_day != day.isoformat()), linking.status.text()
    assert linking.shown_units == [] and "出力した依頼なし" in linking.status.text()
    results.close()
    assert not linking.overlay.painted() and not linking.isVisible()
