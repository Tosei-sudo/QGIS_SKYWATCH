"""
Stage 4 of the imaging plan in QGIS (the viewer's imaging-plan.spec.ts and
imaging-plan-saved.spec.ts, as far as they go): cloud forecasts, stereo and
repeat pairs, the revisit map, reminders and the calendar file, and the
opportunities made into 臨時 requests (「保存・追跡」 went into 撮像依頼,
2026-11-09). Every service is a fake: nothing leaves the machine.
"""
import json
from urllib.parse import parse_qs, urlsplit

import pytest

pytest.importorskip("qgis.core")
from qgis.testing import start_app  # noqa: E402

APP = start_app()

from qgis.core import QgsProject, QgsSettings  # noqa: E402

from mikage.core.esri import EsriClient  # noqa: E402
from mikage.core.request.model import day_bounds, jst_day  # noqa: E402
from mikage.qgis_io import settings  # noqa: E402
from mikage.qgis_io.context import PluginContext  # noqa: E402
from mikage.tests.fake_esri import FakeEsri, FakeLayer  # noqa: E402
from mikage.tests.qgis_fakes import FakeIface, wait_for  # noqa: E402
from mikage.tests.test_plan import EPOCH, LINE1, LINE2  # noqa: E402
from mikage.tests.test_qgis_imaging_plan import CATALOG, SAR1, SAR2, plan, points_layer, start_at  # noqa: E402
from mikage.tests.test_request import HISTORY, LEDGER, ledger_layers  # noqa: E402

IMAGES = "https://example.com/arcgis/rest/services/Images/FeatureServer/0"
FORECAST = "https://wx.example/v1/forecast"
DAY = 86400000


@pytest.fixture
def plugin(tmp_path, monkeypatch):
    QgsSettings().remove("mikage")
    config = {
        "satelliteCatalogs": [{"label": "テスト衛星", "url": CATALOG, "fields": {
            "name": "NAME", "tle": "TLE", "kind": "KIND", "maxOffNadir": "MAX", "lookSide": "LOOK", "swath": "SWATH", "scenesPerPass": "SCENES"}}],
        "imageCatalogs": [{"label": "画像", "url": IMAGES, "fields": {"id": "IMAGE_ID", "acquired": "ACQ", "sensor": "SENSOR", "source": "URL"}}],
        "requestStores": [{"label": "依頼", "url": LEDGER, "history": HISTORY}],
        "cloudForecast": {"url": FORECAST, "label": "社内予報"},
    }
    path = tmp_path / "config.json"
    path.write_text(json.dumps(config, ensure_ascii=False), encoding="utf-8")
    settings.set_config_source(str(path))
    fake = FakeEsri()
    sats = fake.add_layer(CATALOG, FakeLayer({n: "esriFieldTypeString" for n in ("NAME", "TLE", "KIND", "LOOK")} | {n: "esriFieldTypeDouble" for n in ("MAX", "SWATH", "SCENES")}))
    sats.add({"NAME": "OPT-1", "TLE": f"{LINE1}\n{LINE2}", "KIND": "光学", "MAX": 45, "SWATH": 40, "SCENES": 1})
    sats.add({"NAME": "SAR-1", "TLE": f"{SAR1}\n{SAR2}", "KIND": "SAR", "MAX": 45, "LOOK": "R", "SWATH": 50, "SCENES": 1})
    ledger_layers(fake)
    fake.add_layer(IMAGES, FakeLayer({"IMAGE_ID": "esriFieldTypeString", "ACQ": "esriFieldTypeDate", "SENSOR": "esriFieldTypeString", "URL": "esriFieldTypeString"}, "esriGeometryPolygon"))
    from mikage import classFactory

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
    QgsSettings().remove("mikage")


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

    monkeypatch.setattr("mikage.ui.imaging_plan_dock.get_bytes", fake_get)
    monkeypatch.setattr("mikage.ui.imaging_plan_dock._now_ms", lambda: EPOCH)
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
    monkeypatch.setattr("mikage.ui.imaging_plan_dock._now_ms", lambda: EPOCH)
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
    from mikage.ui.docks import QMessageBox as Box
    monkeypatch.setattr(Box, "question", lambda *a: Box.StandardButton.No)
    dock.clear_reminders()
    assert settings.plan_reminders() != []
    monkeypatch.setattr(Box, "question", lambda *a: Box.StandardButton.Yes)
    dock.clear_reminders()
    assert settings.plan_reminders() == []


def test_opportunities_become_temporary_requests(plugin, monkeypatch):
    # 撮像計画と撮像依頼は同じ業務 (tsudo, 2026-11-09): what is to be taken goes to the ledger as 臨時 requests.
    p, iface, tmp, fake = plugin
    monkeypatch.setattr("mikage.ui.imaging_plan_dock._now_ms", lambda: EPOCH)
    dock = planned(p, [("東京", 139.7, 35.68), ("大阪", 135.5, 34.7)], 3)
    assert dock.request_button.isVisibleTo(dock) and dock.request_button.isEnabled()
    assert not hasattr(dock, "store_button") and "SatObsSavedPlans" not in p.docks
    requests = p.api.requests
    assert wait_for(lambda: requests.task is None, 10000)

    # The chosen one: the form with its target, day and satellite, to look over and save.
    op = dock.chosen
    sat, target = dock.plan["sats"][op.satellite], dock.plan["targets"][op.target]
    dock.request_chosen.trigger()
    form = requests.form
    assert requests.isVisible() and form is not None and form.isVisible() and form.batch is None
    r = form.value()
    assert (r.name, r.category, r.achieve, r.status, r.shape_kind) == (target.label, "臨時", "期間内1回", "有効", "点")
    assert jst_day(r.start) == jst_day(op.time) == jst_day(r.end)
    assert r.satellites == [sat.name] and r.note.startswith("撮像計画から: ")
    # (Shown, the dock reads the ledger first.)
    assert wait_for(lambda: requests.task is None, 10000)
    form._save_clicked()
    assert wait_for(lambda: requests.task is None and not form.isVisible(), 10000), form.message.text()
    rows = fake.layers[LEDGER].rows
    assert [(x["attributes"]["REQ_NAME"], x["attributes"]["CATEGORY"]) for x in rows] == [(target.label, "臨時")]

    # Every one listed: one request per target, each its own days and satellites unless the form changes them.
    dock.request_all.trigger()
    form = requests.form
    assert form.batch is not None and form.windowTitle() == "撮像計画から臨時依頼（2 件）"
    form._save_clicked()
    assert wait_for(lambda: requests.task is None and not form.isVisible(), 10000), form.message.text()
    made = [x["attributes"] for x in fake.layers[LEDGER].rows[1:]]
    assert sorted(a["REQ_NAME"] for a in made) == ["大阪", "東京"]
    for a in made:
        ops = [o for o in dock.shown if dock.plan["targets"][o.target].label == a["REQ_NAME"]]
        assert a["START_AT"] == day_bounds(jst_day(min(o.time for o in ops)))[0]
        assert a["END_AT"] == day_bounds(jst_day(max(o.time for o in ops)))[1]
        assert a["NOTE"].startswith("撮像計画から: ")
    # Nothing is saved apart any more: the ledger is all there is.
    assert set(fake.layers) == {CATALOG, IMAGES, LEDGER, HISTORY}


def test_cloud_forecast_from_a_file(plugin, monkeypatch):
    """A GFS cloud file brought in (docs/cloud-forecast-file.md): read from the folder, nothing asked of a service."""
    import numpy as np
    from datetime import datetime, timezone
    from osgeo import gdal
    from mikage.core.plan.weather import CloudForecastConfig

    p, iface, tmp, fake = plugin
    folder = tmp / "gfs"
    folder.mkdir()
    hours = 15 * 24
    ds = gdal.GetDriverByName("GTiff").Create(str(folder / "gfs_tcdc_2026010100.tif"), 8, 8, hours, gdal.GDT_Byte, ["TILED=YES", "INTERLEAVE=BAND"])
    ds.SetGeoTransform((137.875, 0.25, 0, 37.125, 0, -0.25))
    issued = EPOCH - 2 * DAY
    ds.SetMetadata({"INIT_TIME": datetime.fromtimestamp(issued / 1000, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")})
    for h in range(hours):
        band = ds.GetRasterBand(h + 1)
        band.SetNoDataValue(255)
        band.WriteArray(np.full((8, 8), (h * 7) % 100, dtype=np.uint8))
        band.SetMetadata({"GRIB_VALID_TIME": str(int(EPOCH / 1000) + h * 3600)})
    ds = None
    monkeypatch.setattr("mikage.ui.imaging_plan_dock.get_bytes", lambda *a, **k: pytest.fail("no service asked"))
    monkeypatch.setattr("mikage.ui.imaging_plan_dock._now_ms", lambda: EPOCH)
    config = json.loads((tmp / "config.json").read_text(encoding="utf-8"))
    config["cloudForecast"] = {"folder": str(folder), "label": "GFS 雲量"}
    (tmp / "config.json").write_text(json.dumps(config, ensure_ascii=False), encoding="utf-8")
    context = p.api.imaging_plan.context
    context.reload()
    assert context.config.cloud_forecast == CloudForecastConfig("", "GFS 雲量", folder=str(folder))
    dock = planned(p, [("東京", 139.7, 35.68)])
    dock.more.setCollapsed(False)
    assert dock.cloud.text() == "雲量予報（GFS 雲量）" and "送信しない" in dock.cloud.toolTip()
    dock.cloud.setChecked(True)
    assert wait_for(lambda: dock.cloud_task is None)
    assert "GFS 雲量、" in dock.status.text() and "（2 日前）" in dock.status.text(), dock.status.text()
    sats = dock.plan["sats"]
    optical = [op for op in dock.shown if not sats[op.satellite].sar]
    assert optical and all(op.cloud is not None for op in optical)
