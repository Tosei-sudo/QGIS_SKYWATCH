"""
The 「撮像依頼」 dock in QGIS: the ledger in a fake Esri service, the form,
taking 定点 requests in from a layer and out to one, and the day's work
written to files with the history and raises saved.
"""
import csv
import io
import json
import time

import pytest

pytest.importorskip("qgis.core")
from qgis.testing import start_app  # noqa: E402

APP = start_app()

from qgis.core import QgsFeature, QgsGeometry, QgsPointXY, QgsProject, QgsSettings, QgsVectorLayer  # noqa: E402
from qgis.PyQt.QtCore import QDate, Qt  # noqa: E402

from satellite_observation.core.esri import EsriClient  # noqa: E402
from satellite_observation.core.request.model import day_bounds, jst_day  # noqa: E402
from satellite_observation.core.request.store import DEFAULT_HISTORY_FIELDS, DEFAULT_REQUEST_FIELDS  # noqa: E402
from satellite_observation.qgis_io import settings  # noqa: E402
from satellite_observation.qgis_io.context import PluginContext  # noqa: E402
from satellite_observation.tests.fake_esri import FakeEsri, FakeLayer  # noqa: E402
from satellite_observation.tests.qgis_fakes import FakeIface, wait_for  # noqa: E402
from satellite_observation.tests.test_plan import LINE1, LINE2  # noqa: E402

SATS = "https://example.com/arcgis/rest/services/Sats/FeatureServer/0"
LEDGER = "https://example.com/arcgis/rest/services/Req/FeatureServer/0"
HISTORY = "https://example.com/arcgis/rest/services/Req/FeatureServer/1"
SAR1 = "1 90002U 26902A   26280.50000000  .00000100  00000-0  40000-4 0  9992"
SAR2 = "2 90002  98.1800 283.0000 0001300  90.0000 270.0000 14.59200000    16"

CONFIG = {
    "cloudForecast": False,
    "satelliteCatalogs": [{"label": "衛星", "url": SATS, "fields": {"name": "NAME", "tle": "TLE", "kind": "KIND", "series": "SERIES",
                                                                    "resolution": "RES", "maxPerDay": "PERDAY", "system": "SYSTEM"}}],
    "requestStores": [{"label": "撮像依頼", "url": LEDGER, "history": HISTORY}],
    "requestExports": [
        {"system": "SYS-A", "label": "A", "kind": "光学", "series": ["OPT"], "limit": {"count": 1}, "format": "csv", "encoding": "cp932",
         "timeZone": "Asia/Tokyo", "dateFormat": "%Y%m%d",
         "priority": {"table": [{"max": 200, "value": "High"}, {"max": 600, "value": "Middle"}, {"max": 1000, "value": "Low"}]},
         "columns": [{"key": "ID", "source": "id"}, {"key": "名称", "source": "name"}, {"key": "日", "source": "start"},
                     {"key": "優先度", "source": "priority"}]},
        {"system": "SYS-B", "label": "B", "kind": "SAR", "format": "json", "schema": "sys-b.schema.json", "timeZone": "UTC"},
    ],
}
SCHEMA = {"type": "object", "required": ["tasks"], "properties": {
    "tasks": {"type": "array", "x-source": "requests", "items": {"type": "object", "required": ["id"], "properties": {
        "id": {"type": "string", "x-source": "id"}, "from": {"type": "string", "format": "date-time", "x-source": "start"},
        "priority": {"type": "integer", "x-source": "priorityRaw"}}}}}}


@pytest.fixture
def plugin(tmp_path, monkeypatch):
    QgsSettings().remove("satellite_observation")
    config = tmp_path / "config.json"
    config.write_text(json.dumps(CONFIG, ensure_ascii=False), encoding="utf-8")
    (tmp_path / "sys-b.schema.json").write_text(json.dumps(SCHEMA), encoding="utf-8")
    settings.set_config_source(str(config))
    fake = FakeEsri()
    sats = fake.add_layer(SATS, FakeLayer({n: "esriFieldTypeString" for n in ("NAME", "TLE", "KIND", "SERIES", "SYSTEM")} | {"RES": "esriFieldTypeDouble", "PERDAY": "esriFieldTypeDouble"}))
    sats.add({"NAME": "OPT 1", "TLE": f"{LINE1}\n{LINE2}", "KIND": "光学", "SERIES": "OPT", "RES": 0.5})
    # The catalog says which system this one is ordered through.
    sats.add({"NAME": "RADAR 1", "TLE": f"{SAR1}\n{SAR2}", "KIND": "SAR", "SERIES": "RADAR", "RES": 1, "SYSTEM": "SYS-B"})
    types = {n: "esriFieldTypeString" for n in DEFAULT_REQUEST_FIELDS.values()}
    types.update({n: "esriFieldTypeDate" for n in ("START_AT", "END_AT", "CREATED_AT", "UPDATED_AT")})
    types.update({n: "esriFieldTypeDouble" for n in ("PRIORITY", "BOOST", "OFF_NADIR_MIN", "OFF_NADIR_MAX", "INCIDENCE_MIN",
                                                     "INCIDENCE_MAX", "RESOLUTION_MAX", "CLOUD_NO_GO")})
    fake.add_layer(LEDGER, FakeLayer(types, "esriGeometryPolygon"))
    htypes = {n: "esriFieldTypeString" for n in DEFAULT_HISTORY_FIELDS.values()}
    htypes.update(RUN_DATE="esriFieldTypeDate", CLOUD="esriFieldTypeDouble", SCENES="esriFieldTypeInteger", BOOST_DONE="esriFieldTypeInteger")
    fake.add_layer(HISTORY, FakeLayer(htypes, "esriGeometryNone"))
    from satellite_observation import classFactory

    iface = FakeIface()
    p = classFactory(iface)
    monkeypatch.setattr(PluginContext, "esri", lambda self: EsriClient(fake))
    p.initGui()
    for dock in p.docks.values():
        dock.hide()
    assert p.api.problems == [], p.api.problems
    yield p, iface, fake, tmp_path
    p.unload()
    QgsProject.instance().removeAllMapLayers()
    QgsSettings().remove("satellite_observation")


def fixed_layer():
    layer = QgsVectorLayer("Point?crs=EPSG:4326&field=名称:string&field=開始:string&field=終了:string&field=優先度:string&field=繰り返し:string&field=種別:string", "定点一覧", "memory")
    today = jst_day(time.time() * 1000)
    feats = []
    for name, lon, prio in (("横須賀", 139.67, "高"), ("呉", 132.55, "320")):
        f = QgsFeature(layer.fields())
        f.setAttributes([name, today.isoformat(), "2099-12-31", prio, "毎日", "光学"])
        f.setGeometry(QgsGeometry.fromPointXY(QgsPointXY(lon, 35.0)))
        feats.append(f)
    bad = QgsFeature(layer.fields())
    bad.setAttributes(["期間なし", None, None, None, None, None])
    bad.setGeometry(QgsGeometry.fromPointXY(QgsPointXY(135, 34)))
    feats.append(bad)
    layer.dataProvider().addFeatures(feats)
    QgsProject.instance().addMapLayer(layer)
    return layer


def test_requests_dock_form_layers_and_the_day(plugin):
    p, iface, fake, tmp_path = plugin
    dock = p.api.requests
    dock.show()
    assert wait_for(lambda: dock._loaded and dock.task is None)
    assert dock.requests == []

    # A 臨時 SAR request through the form (a point clicked on the map).
    dock.edit(new=True)
    form = dock.form
    form.name.setText("大阪湾")
    form.kind.setCurrentText("SAR")
    form.category.setCurrentText("臨時")
    form.detail.setChecked(True)
    form.priority.setValue(150)
    form._point_clicked(QgsPointXY(135.3, 34.6))
    assert form.shape_kind == "点" and form.rings
    form._save_clicked()
    assert wait_for(lambda: dock.task is None and len(dock.requests) == 1)
    r = dock.requests[0]
    assert (r.name, r.kind, r.priority, r.category, r.shape_kind) == ("大阪湾", "SAR", 150, "臨時", "点")

    # 定点 requests from a point layer: the row without a period is left out.
    layer = fixed_layer()
    dock.import_layer(layer, ask=False)
    assert wait_for(lambda: dock.task is None and len(dock.requests) == 3)
    fixed = {x.name: x for x in dock.requests if x.category == "定点"}
    assert set(fixed) == {"横須賀", "呉"}
    assert fixed["横須賀"].priority == 300 and fixed["呉"].priority == 320 and fixed["呉"].achieve == "毎回"
    # Out again as a layer (定点 only), and back in: same ids, so changed rather than added.
    dock.category_filter.setCurrentText("定点")
    out = dock.export_layer()
    assert out.featureCount() == 2 and out.fields().indexOf("REQ_ID") >= 0
    dock.import_layer(out, ask=False)
    assert wait_for(lambda: dock.task is None and dock.status.text().startswith("2 件を取り込み"))
    assert len(dock.requests) == 3

    # The day: SYS-A takes one a day (横須賀, higher), 呉 is held back; the SAR one goes to SYS-B by the catalog's system.
    daily = dock.open_daily()
    daily.folder.setText(str(tmp_path / "out"))
    daily.run()
    assert wait_for(lambda: daily.plan is not None and daily.task is None), daily.status.text()
    got = {c.request.name: (c.result, c.system.system if c.system else c.reason) for c in daily.plan.candidates}
    assert got == {"横須賀": ("採用", "SYS-A"), "呉": ("見送り", "枠超過"), "大阪湾": ("採用", "SYS-B")}
    # By hand: 呉 in (over the limit), 横須賀 out.
    model = daily.model
    names = [c.request.name for c in daily.plan.candidates]
    model.item(names.index("呉"), 0).setCheckState(Qt.CheckState.Checked)
    assert wait_for(lambda: next(c for c in daily.plan.candidates if c.request.name == "呉").result == "採用")
    names = [c.request.name for c in daily.plan.candidates]
    daily.model.item(names.index("横須賀"), 0).setCheckState(Qt.CheckState.Unchecked)
    assert wait_for(lambda: next(c for c in daily.plan.candidates if c.request.name == "横須賀").result == "見送り")
    got = {c.request.name: c.result for c in daily.plan.candidates}
    assert got == {"横須賀": "見送り", "呉": "採用", "大阪湾": "採用"}

    daily.write(ask=False)
    assert wait_for(lambda: len(daily.paths) == 2), daily.status.text()
    paths = daily.paths
    a = next(x for x in paths if "SYS-A" in x)
    rows = list(csv.reader(io.StringIO(open(a, encoding="cp932").read())))
    assert rows[0] == ["ID", "名称", "日", "優先度"]
    assert rows[1][1:] == ["呉", jst_day(time.time() * 1000).strftime("%Y%m%d"), "Middle"]
    b = json.load(open(next(x for x in paths if "SYS-B" in x), encoding="utf-8"))
    assert b["tasks"][0]["priority"] == 150 and b["tasks"][0]["from"].endswith("Z")
    assert wait_for(lambda: daily.task is None and len(fake.layers[HISTORY].rows) == 3), daily.status.text()
    assert settings.request_export_folder() == str(tmp_path / "out")
    assert {r["attributes"]["RESULT"] for r in fake.layers[HISTORY].rows} == {"採用", "見送り"}
