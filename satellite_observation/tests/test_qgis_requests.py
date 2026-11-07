"""
The 「撮像依頼」 and 「撮像計画ファイル出力」 docks in QGIS: the ledger in a fake Esri service, the form,
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
IMAGES = "https://example.com/arcgis/rest/services/Images/FeatureServer/0"
SAR1 = "1 90002U 26902A   26280.50000000  .00000100  00000-0  40000-4 0  9992"
SAR2 = "2 90002  98.1800 283.0000 0001300  90.0000 270.0000 14.59200000    16"

CONFIG = {
    "cloudForecast": False,
    "satelliteCatalogs": [{"label": "衛星", "url": SATS, "fields": {"name": "NAME", "tle": "TLE", "kind": "KIND", "series": "SERIES",
                                                                    "resolution": "RES", "maxPerDay": "PERDAY", "system": "SYSTEM"}}],
    "imageCatalogs": [{"label": "画像", "url": IMAGES, "fields": {"id": "IMAGE_ID", "acquired": "ACQ", "sensor": "SENSOR", "source": "URL"}}],
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
    fake.add_layer(IMAGES, FakeLayer({"IMAGE_ID": "esriFieldTypeString", "ACQ": "esriFieldTypeDate", "SENSOR": "esriFieldTypeString",
                                      "URL": "esriFieldTypeString"}, "esriGeometryPolygon"))
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
    # The day's files are a dock of their own (those writing them are not those registering requests).
    assert not hasattr(dock, "open_daily")
    daily = p.api.plan_files
    daily.show()
    daily.folder.setText(str(tmp_path / "out"))
    daily.run()
    assert wait_for(lambda: daily.plan is not None and daily.task is None), daily.status.text()
    got = {c.request.name: (c.result, c.system.system if c.system else c.reason) for c in daily.plan.candidates}
    assert got == {"横須賀": ("採用", "SYS-A"), "呉": ("見送り", "枠超過"), "大阪湾": ("採用", "SYS-B")}

    # 上限シミュレーション: SYS-A at 2 a day takes 呉 too; SYS-B off leaves 大阪湾 without a system. Nothing is written.
    ledger_before = json.dumps(fake.layers[LEDGER].rows, sort_keys=True, default=str)
    history_before = len(fake.layers[HISTORY].rows)
    daily.open_simulation()
    sim = daily.simulation
    sim.days.setValue(2)
    rows = {sim.systems.item(r, 1).data(Qt.ItemDataRole.UserRole): r for r in range(sim.systems.rowCount())}
    assert sim.systems.item(rows["SYS-A"], 2).text() == "1" and sim.systems.item(rows["SYS-B"], 2).text() == ""
    sim.systems.item(rows["SYS-A"], 2).setText("2")
    sim.systems.item(rows["SYS-B"], 0).setCheckState(Qt.CheckState.Unchecked)
    sim.run()
    assert wait_for(lambda: sim.trial is not None and sim.task is None), sim.status.text()
    assert sim.base.total("over") == 2 and sim.base.total("lost") == 2
    assert sim.trial.total("over") == 0 and sim.trial.days[0].used["SYS-A"][0] == 2
    assert sim.trial.days[0].no_system == 1 and "SYS-B" not in sim.trial.days[0].used
    summary = {sim.summary.item(r, 0).text(): (sim.summary.item(r, 1).text(), sim.summary.item(r, 2).text(), sim.summary.item(r, 3).text())
               for r in range(sim.summary.rowCount())}
    assert summary["枠超過で見送り"] == ("2", "0", "-2")
    assert sim.daily.rowCount() == 2 and "A 2/2 件" in sim.daily.item(0, 11).text()
    # Back to the settings' values: the same as the base, without reading the ledger again.
    sim.fill_systems()
    sim.run()
    # The run is a task of its own (#83): the dialog stays live meanwhile.
    assert sim.task is not None and not sim.run_button.isEnabled()
    assert wait_for(lambda: sim.task is None), sim.status.text()
    assert sim.trial.total("over") == sim.base.total("over") == 2 and sim.run_button.isEnabled()
    assert json.dumps(fake.layers[LEDGER].rows, sort_keys=True, default=str) == ledger_before
    assert len(fake.layers[HISTORY].rows) == history_before
    sim.close()
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
    # While the files are made (#84): a tick in the table is put back, and the plan is not changed.
    assert daily.task is not None
    names = [c.request.name for c in daily.plan.candidates]
    daily.model.item(names.index("横須賀"), 0).setCheckState(Qt.CheckState.Checked)
    assert daily.model.item(names.index("横須賀"), 0).checkState() == Qt.CheckState.Unchecked
    daily.move(-1)
    daily.reset()
    assert {c.request.name: c.result for c in daily.plan.candidates} == got
    # Even a change made to the plan meanwhile does not reach the files and history (they are of a copy).
    live = next(c for c in daily.plan.candidates if c.request.name == "横須賀")
    live.manual = "採用"
    daily.plan.refit()
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
    yokosuka = next(x.id for x in dock.requests if x.name == "横須賀")
    assert {r["attributes"]["REQ_ID"]: r["attributes"]["RESULT"] for r in fake.layers[HISTORY].rows}[yokosuka] == "見送り"
    live.manual = "見送り"
    daily.plan.refit()

    # Written again the same day (#71): the earlier rows are replaced, not doubled.
    daily.paths = []
    daily.write(ask=False)
    assert wait_for(lambda: daily.task is None and len(daily.paths) == 2 and "置き換え" in daily.status.text()), daily.status.text()
    assert len(fake.layers[HISTORY].rows) == 3

    # A system whose file cannot be made (#68): its request is 見送り in the history, not 採用・未判定.
    (tmp_path / "sys-b.schema.json").write_text(json.dumps({**SCHEMA, "required": ["tasks", "missing"]}), encoding="utf-8")
    daily.paths = []
    daily.write(ask=False)
    assert wait_for(lambda: daily.task is None and len(daily.paths) == 1 and "置き換え" in daily.status.text()), daily.status.text()
    rows = {r["attributes"]["REQ_ID"]: r["attributes"] for r in fake.layers[HISTORY].rows}
    osaka = next(x.id for x in dock.requests if x.name == "大阪湾")
    assert (rows[osaka]["RESULT"], rows[osaka]["CAPTURED"]) == ("見送り", "")
    assert rows[osaka]["REASON"].startswith("書き出し失敗")


def test_plan_results_against_the_catalog(plugin):
    p, iface, fake, tmp_path = plugin
    daily = p.api.plan_files
    # Two requests handed out five days ago (and one held back): 横須賀 imaged, 呉 not.
    day = jst_day(time.time() * 1000 - 5 * 86400000)
    first, last = day_bounds(day)
    square = lambda lon: {"rings": [[[lon - 0.05, 34.95], [lon + 0.05, 34.95], [lon + 0.05, 35.05], [lon - 0.05, 35.05], [lon - 0.05, 34.95]]],
                          "spatialReference": {"wkid": 4326}}
    ledger = fake.layers[LEDGER]
    for rid, name, lon, sats in (("R-1", "横須賀", 139.67, ""), ("R-2", "呉", 132.55, "OPT 1")):
        ledger.add({"REQ_ID": rid, "REQ_NAME": name, "CATEGORY": "臨時", "ACHIEVE": "期間内1回", "STATUS": "有効", "PRIORITY": 500,
                    "BOOST": 100, "KIND": "光学", "SATELLITES": sats, "START_AT": first - 86400000, "END_AT": last + 30 * 86400000},
                   square(lon))
    history = fake.layers[HISTORY]
    for rid, result in (("R-1", "採用"), ("R-2", "採用"), ("R-3", "見送り")):
        history.add({"RUN_ID": "RUN", "TARGET_DAY": day.isoformat(), "REQ_ID": rid, "SYSTEM": "SYS-A", "SATELLITE": "OPT 1",
                     "RESULT": result, "CAPTURED": "未判定" if result == "採用" else "", "BOOST_DONE": 0})
    images = fake.layers[IMAGES]
    images.add({"IMAGE_ID": "IMG-Y", "ACQ": first + 3 * 3600000, "SENSOR": "OTHER 9"}, square(139.67))
    images.add({"IMAGE_ID": "IMG-K", "ACQ": first + 3 * 3600000, "SENSOR": "OTHER 9"}, square(132.55))

    dialog = daily.show_results()
    assert wait_for(lambda: dialog.task is None and dialog.history), dialog.status.text()
    # 横須賀 takes any satellite; 呉 named OPT 1, so another satellite's image does not count.
    got = {r.request_id: (r.captured, r.image_id) for r in dialog.history if r.result == "採用"}
    assert got == {"R-1": ("撮像済み", "IMG-Y"), "R-2": ("未撮像", "")}, dialog.status.text()
    assert dialog.status.text().startswith("撮像済み 1 件・未撮像 1 件を確認")
    assert dialog.model.rowCount() == 2
    assert "SYS-A: 撮像済み 1・未撮像 1・未判定 0（撮像率 50%）" in dialog.summary.text()
    steps = {dialog.model.item(i, 3).text(): dialog.model.item(i, 7).text() for i in range(dialog.model.rowCount())}
    assert steps == {"R-1": "完了", "R-2": "次回の計算で持ち越し・繰り上げ候補"}
    saved = {f["attributes"]["REQ_ID"]: f["attributes"] for f in ledger.rows}
    assert (saved["R-1"]["STATUS"], saved["R-1"]["BOOST"]) == ("完了", 0)
    dialog.shown.setCurrentText("未撮像")
    assert dialog.model.rowCount() == 1

    # By hand: 呉 was in fact imaged (the catalog is late).
    dialog.table.selectRow(0)
    dialog.mark("撮像済み", "IMG-HAND")
    assert wait_for(lambda: dialog.task is None and dialog.status.text().startswith("1 件を撮像済みに変更")), dialog.status.text()
    rows = {f["attributes"]["REQ_ID"]: f["attributes"] for f in history.rows}
    assert (rows["R-2"]["CAPTURED"], rows["R-2"]["IMAGE_ID"]) == ("撮像済み", "IMG-HAND")
    assert {f["attributes"]["REQ_ID"]: f["attributes"]["STATUS"] for f in ledger.rows}["R-2"] == "完了"
    assert dialog.model.rowCount() == 0  # no longer 未撮像


def test_import_reads_the_ledger_first_and_skips_repeated_ids(plugin):
    """#72: ids already in the ledger (added since the dock read it) are changed, an id twice in the layer is taken once."""
    p, iface, fake, tmp_path = plugin
    dock = p.api.requests
    assert not dock.import_button.isEnabled()  # not read yet
    dock.show()
    assert wait_for(lambda: dock._loaded and dock.task is None)
    assert dock.import_button.isEnabled()
    # Someone else adds R-9 after the dock read the ledger.
    fake.layers[LEDGER].add({"REQ_ID": "R-9", "REQ_NAME": "先に追加", "CATEGORY": "定点", "STATUS": "有効", "PRIORITY": 500},
                            {"rings": [[[139.6, 35.0], [139.7, 35.0], [139.7, 35.1], [139.6, 35.1], [139.6, 35.0]]], "spatialReference": {"wkid": 4326}})
    layer = QgsVectorLayer("Point?crs=EPSG:4326&field=REQ_ID:string&field=名称:string&field=開始:string&field=終了:string", "定点", "memory")
    today = jst_day(time.time() * 1000).isoformat()
    feats = []
    for rid, name, lon in (("R-9", "横須賀", 139.67), ("R-10", "呉", 132.55), ("R-10", "呉（重複）", 132.6)):
        f = QgsFeature(layer.fields())
        f.setAttributes([rid, name, today, "2099-12-31"])
        f.setGeometry(QgsGeometry.fromPointXY(QgsPointXY(lon, 35.0)))
        feats.append(f)
    layer.dataProvider().addFeatures(feats)
    QgsProject.instance().addMapLayer(layer)
    dock.import_layer(layer, ask=False)
    assert wait_for(lambda: dock.task is None and dock.status.text().startswith("2 件を取り込み")), dock.status.text()
    ids = sorted(r["attributes"]["REQ_ID"] for r in fake.layers[LEDGER].rows)
    assert ids == ["R-10", "R-9"]
    assert {r["attributes"]["REQ_ID"]: r["attributes"]["REQ_NAME"] for r in fake.layers[LEDGER].rows}["R-9"] == "横須賀"
    assert "取り込まなかったもの 1 件" in dock.status.text()


def test_a_satellite_catalog_that_cannot_be_read_gives_the_buttons_back(plugin, monkeypatch):
    """#70: 「計算」 and 「試算」 come back with the reason when the satellite catalog cannot be read."""
    from satellite_observation.core.esri import EsriError
    from satellite_observation.ui import imaging_plan_dock

    p, iface, fake, tmp_path = plugin

    def down(*args, **kwargs):
        raise EsriError("接続不可（テスト）")

    plan_dock = p.docks["SatObsImagingPlan"]
    assert wait_for(lambda: plan_dock.loading_task is None)
    monkeypatch.setattr(imaging_plan_dock, "load_satellites", down)
    plan_dock._loaded = False
    daily = p.api.plan_files
    daily.show()
    daily.run()
    assert wait_for(lambda: daily.run_button.isEnabled() and "衛星カタログの読み込みに失敗" in daily.status.text()), daily.status.text()
    daily.open_simulation()
    sim = daily.simulation
    sim.run()
    assert wait_for(lambda: sim.run_button.isEnabled() and "衛星カタログの読み込みに失敗" in sim.status.text()), sim.status.text()
    sim.close()
