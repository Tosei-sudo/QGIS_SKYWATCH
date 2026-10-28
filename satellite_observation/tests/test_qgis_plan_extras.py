"""
Stage 4 of the imaging plan in QGIS (the viewer's imaging-plan.spec.ts and
imaging-plan-saved.spec.ts, as far as they go): cloud forecasts, stereo and
repeat pairs, the revisit map, reminders and the calendar file, saving a plan
to a plan store and following it up in 「保存した撮像計画」. Every service is
a fake: nothing leaves the machine.
"""
import json
from urllib.parse import parse_qs, urlsplit

import pytest

pytest.importorskip("qgis.core")
from qgis.testing import start_app  # noqa: E402

APP = start_app()

from qgis.core import QgsProject, QgsRasterLayer, QgsSettings  # noqa: E402

from satellite_observation.core.esri import EsriClient  # noqa: E402
from satellite_observation.core.plan.store import DEFAULT_PLAN_FIELDS  # noqa: E402
from satellite_observation.qgis_io import settings  # noqa: E402
from satellite_observation.qgis_io.context import PluginContext  # noqa: E402
from satellite_observation.tests.fake_esri import FakeEsri, FakeLayer  # noqa: E402
from satellite_observation.tests.qgis_fakes import FakeIface, wait_for  # noqa: E402
from satellite_observation.tests.test_plan import EPOCH, LINE1, LINE2  # noqa: E402
from satellite_observation.tests.test_qgis_imaging_plan import CATALOG, SAR1, SAR2, plan, points_layer, start_at  # noqa: E402

PLANS = "https://example.com/arcgis/rest/services/Plans/FeatureServer/0"
IMAGES = "https://example.com/arcgis/rest/services/Images/FeatureServer/0"
FORECAST = "https://wx.example/v1/forecast"
DAY = 86400000


@pytest.fixture
def plugin(tmp_path, monkeypatch):
    QgsSettings().remove("satellite_observation")
    config = {
        "satelliteCatalogs": [{"label": "テスト衛星", "url": CATALOG, "fields": {
            "name": "NAME", "tle": "TLE", "kind": "KIND", "maxOffNadir": "MAX", "lookSide": "LOOK", "swath": "SWATH", "scenesPerPass": "SCENES"}}],
        "imageCatalogs": [{"label": "画像", "url": IMAGES, "fields": {"id": "IMAGE_ID", "acquired": "ACQ", "sensor": "SENSOR", "source": "URL"}}],
        "planStores": [{"label": "計画レイヤー", "url": PLANS}],
        "cloudForecast": {"url": FORECAST, "label": "社内予報"},
    }
    path = tmp_path / "config.json"
    path.write_text(json.dumps(config, ensure_ascii=False), encoding="utf-8")
    settings.set_config_source(str(path))
    fake = FakeEsri()
    sats = fake.add_layer(CATALOG, FakeLayer({n: "esriFieldTypeString" for n in ("NAME", "TLE", "KIND", "LOOK")} | {n: "esriFieldTypeDouble" for n in ("MAX", "SWATH", "SCENES")}))
    sats.add({"NAME": "OPT-1", "TLE": f"{LINE1}\n{LINE2}", "KIND": "光学", "MAX": 45, "SWATH": 40, "SCENES": 1})
    sats.add({"NAME": "SAR-1", "TLE": f"{SAR1}\n{SAR2}", "KIND": "SAR", "MAX": 45, "LOOK": "R", "SWATH": 50, "SCENES": 1})
    fields = {name: "esriFieldTypeString" for name in DEFAULT_PLAN_FIELDS.values()}
    fields.update(SHOT_TIME="esriFieldTypeDate", CREATED_AT="esriFieldTypeDate", OFF_NADIR="esriFieldTypeDouble", INCIDENCE="esriFieldTypeDouble")
    fake.add_layer(PLANS, FakeLayer(fields, "esriGeometryPolygon"))
    fake.add_layer(IMAGES, FakeLayer({"IMAGE_ID": "esriFieldTypeString", "ACQ": "esriFieldTypeDate", "SENSOR": "esriFieldTypeString", "URL": "esriFieldTypeString"}, "esriGeometryPolygon"))
    from satellite_observation import classFactory

    iface = FakeIface()
    p = classFactory(iface)
    # The docks open at start read their catalogs right away.
    monkeypatch.setattr(PluginContext, "esri", lambda self: EsriClient(fake))
    p.initGui()
    # Each test opens the docks it uses.
    for dock in p.docks.values():
        dock.hide()
    assert p.api.problems == [], p.api.problems
    yield p, iface, tmp_path, fake
    p.unload()
    QgsProject.instance().removeAllMapLayers()
    QgsSettings().remove("satellite_observation")


def planned(p, points, days=14):
    points_layer(points)
    dock = p.api.imaging_plan
    dock.show()
    assert wait_for(lambda: dock.loading_task is None)
    start_at(dock, EPOCH, days)
    plan(dock)
    return dock


def test_cloud_forecast_pairs_and_revisit_map(plugin, monkeypatch):
    p, iface, tmp, fake = plugin
    asked = []

    def fake_get(url, authcfg=None):
        asked.append(url)
        query = parse_qs(urlsplit(url).query)
        n = len(query["latitude"][0].split(","))
        hours = [EPOCH / 1000 + h * 3600 for h in range(16 * 24)]
        one = {"hourly": {"time": hours, "cloud_cover": [(h * 7) % 100 for h in range(len(hours))]}}
        return json.dumps(one if n == 1 else [one] * n).encode()

    monkeypatch.setattr("satellite_observation.ui.imaging_plan_dock.get_bytes", fake_get)
    monkeypatch.setattr("satellite_observation.ui.imaging_plan_dock._now_ms", lambda: EPOCH)
    dock = planned(p, [("東京", 139.7, 35.68)])
    dock.more.setCollapsed(False)
    assert dock.cloud_row.isVisibleTo(dock) and dock.cloud.text() == "雲量予報（社内予報）"
    # Nothing is asked until the forecast is turned on.
    assert asked == []
    dock.cloud.setChecked(True)
    assert wait_for(lambda: dock.cloud_task is None)
    assert len(asked) == 1 and asked[0].startswith(FORECAST + "?")
    assert settings.plan_cloud()
    headers = [c.label for c in dock.model.columns]
    assert "雲量" in headers
    sats = dock.plan["sats"]
    optical = [op for op in dock.shown if not sats[op.satellite].sar]
    assert optical and all(op.cloud is not None for op in optical)
    assert all(op.cloud is None for op in dock.shown if sats[op.satellite].sar)
    every = len(dock.shown)
    dock.max_cloud.setValue(30)
    assert len(dock.shown) < every
    assert all(op.cloud is None or op.cloud <= 30 for op in dock.shown)
    dock.max_cloud.setValue(-1)

    # Stereo pairs: rows carry their second shot, drawn numbered 2.
    dock.view.setCurrentIndex(dock.view.findData("stereo"))
    dock.stereo_max.setValue(90)
    dock.stereo_series.setChecked(False)
    assert dock.stereo_bar.isVisibleTo(dock) and not dock.first.isVisibleTo(dock)
    assert dock.shown and all(op.pair is not None for op in dock.shown), dock.status.text()
    assert "ステレオペア" in dock.status.text()
    assert {"2 回目", "交会角", "間隔"} <= {c.label for c in dock.model.columns}
    assert dock.overlay.extent(["step"]) is not None
    assert "交会角" in dock.csv_text().splitlines()[0]
    # Interferometric pairs of the SAR satellite over two weeks.
    dock.view.setCurrentIndex(dock.view.findData("repeat"))
    assert dock.repeat_bar.isVisibleTo(dock)
    assert "干渉ペア" in dock.status.text()
    assert all(sats[op.satellite].sar for op in dock.shown)

    # The revisit map: planned again over a grid, coloured on the map.
    dock.revisit_grid.setValue(3)
    dock.view.setCurrentIndex(dock.view.findData("revisit"))
    assert wait_for(lambda: dock.revisit is not None, 30000), dock.status.text()
    assert dock.revisit_table.isVisibleTo(dock) and not dock.table.isVisibleTo(dock)
    assert dock.revisit_model.rowCount() == 9
    assert "セル" in dock.status.text()
    assert len(dock.overlay._painted) == 9
    dock.revisit_table.selectRow(0)
    assert dock.revisit["chosen"] is not None
    assert dock.csv_button.isEnabled()
    assert dock.csv_text().startswith("﻿セル,経度,緯度,回数")
    layer = dock.add_layer()
    assert layer.featureCount() == 9 and not layer.temporalProperties().isActive()
    # Back to the list: the cells are gone from the map.
    dock.view.setCurrentIndex(dock.view.findData("all"))
    assert dock.overlay._painted == [] and dock.table.isVisibleTo(dock)


def test_reminders_and_calendar_file(plugin, monkeypatch):
    p, iface, tmp, fake = plugin
    monkeypatch.setattr("satellite_observation.ui.imaging_plan_dock._now_ms", lambda: EPOCH)
    dock = planned(p, [("東京", 139.7, 35.68)], 3)
    assert dock.remind_button.isEnabled()
    dock.remind_button.setChecked(True)
    assert dock.remind_row.isVisibleTo(dock)
    dock.remind_minutes.setValue(15)
    n = dock.set_reminders()
    assert n > 0 and dock.remind_count.text() == f"通知予定 {n} 件"
    kept = settings.plan_reminders()
    assert len(kept) == n and kept[0]["rings"]
    # Set again: replaced, not doubled. Tokens never go there: only the shot.
    dock.set_reminders()
    assert len(settings.plan_reminders()) == n
    assert set(kept[0]) == {"id", "time", "notifyAt", "satellite", "target", "offNadir", "rings"}
    first = min(r["notifyAt"] for r in kept)
    shown_before = len(iface.bar.items())
    assert dock.fire_reminders(first - 1) == 0
    assert dock.fire_reminders(first + 1000) == 1
    assert len(iface.bar.items()) > shown_before
    assert len(settings.plan_reminders()) == n - 1
    path = str(tmp / "plan.ics")
    dock.save_ics(path)
    text = open(path, encoding="utf-8", newline="").read()
    assert text.startswith("BEGIN:VCALENDAR\r\n") and "TRIGGER:-PT15M" in text
    assert text.count("BEGIN:VEVENT") == len(dock.shown)
    dock.clear_reminders()
    assert settings.plan_reminders() == []


def test_save_a_plan_and_follow_it_up(plugin, monkeypatch):
    p, iface, tmp, fake = plugin
    monkeypatch.setattr("satellite_observation.ui.imaging_plan_dock._now_ms", lambda: EPOCH)
    dock = planned(p, [("東京", 139.7, 35.68)], 3)
    assert dock.store_button.isVisibleTo(dock) and dock.saved_button.isVisibleTo(dock)
    dock.store_button.setChecked(True)
    assert dock.plan_name.text().startswith("撮像計画 2026/10/0")
    dock.plan_name.setText("十月の計画")
    ids = []
    dock.save_to_store(ids.append)
    assert wait_for(lambda: ids, 10000), dock.status.text()
    assert ids[0] and "「十月の計画」を 計画レイヤー に保存" in dock.status.text()
    rows = fake.layers[PLANS].rows
    shots = len(rows)
    assert shots == len(dock.shown)
    assert all(r["attributes"]["PLAN_NAME"] == "十月の計画" and r["geometry"]["rings"] for r in rows)
    # Nothing secret in the layer: just the plan.
    assert "token" not in json.dumps([r["attributes"] for r in rows]).lower()

    # The first shot's satellite took an image ten minutes later, over it.
    first = min(rows, key=lambda r: r["attributes"]["SHOT_TIME"])
    t0 = first["attributes"]["SHOT_TIME"]
    xs = [pt[0] for pt in first["geometry"]["rings"][0]]
    ys = [pt[1] for pt in first["geometry"]["rings"][0]]
    ring = {"rings": [[[min(xs), min(ys)], [min(xs), max(ys)], [max(xs), max(ys)], [max(xs), min(ys)], [min(xs), min(ys)]]]}
    fake.layers[IMAGES].add({"IMAGE_ID": "IMG-1", "ACQ": t0 + 600000, "SENSOR": first["attributes"]["SATELLITE"], "URL": "https://example.com/img-1.tif"}, ring)

    monkeypatch.setattr("satellite_observation.ui.saved_plans_dock._now_ms", lambda: EPOCH + 30 * DAY)
    saved = p.api.saved_plans
    done = []
    dock.saved_button.click()
    assert saved.isVisible()
    assert wait_for(lambda: saved.plans and saved.task is None, 10000), saved.status.text()
    assert [pl.name for pl in saved.plans] == ["十月の計画"]
    saved.choose(saved.plans[0], True, lambda: done.append(1))
    assert wait_for(lambda: done, 10000), saved.status.text()
    statuses = saved.counts()
    # Opened long after: the shot with an image is taken, the others missed.
    assert statuses.get("撮像済み") == 1 and statuses.get("未撮像") == shots - 1, statuses
    assert next(s for s in saved.shots if s.status == "撮像済み").imageId == "IMG-1"
    assert saved.plan_model.item(0, 3).text().startswith("撮像済み 1 / ")
    assert len(saved.overlay._painted) == shots

    # The image opens with only the server's authentication configuration in its source (no token), with its id and time.
    opened = []
    settings.set_auth_map({"https://example.com/": "img0001"})
    monkeypatch.setattr("satellite_observation.qgis_io.catalog_io.image_layer",
                        lambda source, name, **kw: opened.append((source, name, kw)) or QgsRasterLayer("", name))
    saved.shot_table.selectRow(next(i for i, s in enumerate(saved.shots) if s.status == "撮像済み"))
    got = []
    saved.open_image(got.append)
    assert wait_for(lambda: got, 10000)
    assert opened == [("https://example.com/img-1.tif", "IMG-1", {"image_id": "IMG-1", "time": t0 + 600000, "authcfg": "img0001"})]
    settings.set_auth_map({})

    # A status set by hand goes to the layer.
    saved.shot_table.selectRow(1)
    assert saved.chosen is saved.shots[1] and saved.status_choice.isEnabled()
    changed = []
    saved.set_status("取消", lambda: changed.append(1))
    assert wait_for(lambda: changed, 10000)
    assert fake.layers[PLANS].rows[[r["attributes"]["OBJECTID"] for r in fake.layers[PLANS].rows].index(saved.shots[1].objectId)]["attributes"]["STATUS"] == "取消"

    # Deleting takes two clicks.
    saved.delete_button.click()
    assert saved.delete_button.text() == "もう一度押すと削除" and fake.layers[PLANS].rows
    gone = []
    saved._disarm()
    saved.delete(lambda: gone.append(1))
    assert wait_for(lambda: gone, 10000)
    assert fake.layers[PLANS].rows == [] and saved.plans == []
