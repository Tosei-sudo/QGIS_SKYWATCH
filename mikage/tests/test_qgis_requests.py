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

from qgis.core import QgsFeature, QgsGeometry, QgsPointXY, QgsProject, QgsRectangle, QgsSettings, QgsVectorLayer  # noqa: E402
from qgis.PyQt.QtCore import QDate, Qt  # noqa: E402

from mikage.core.esri import EsriClient  # noqa: E402
from mikage.core.request.model import day_bounds, jst_day  # noqa: E402
from mikage.core.request.store import DEFAULT_HISTORY_FIELDS, DEFAULT_REQUEST_FIELDS  # noqa: E402
from mikage.qgis_io import settings  # noqa: E402
from mikage.qgis_io.context import PluginContext  # noqa: E402
from mikage.tests.fake_esri import FakeEsri, FakeLayer  # noqa: E402
from mikage.tests.qgis_fakes import FakeIface, wait_for  # noqa: E402
from mikage.tests.test_plan import BELOW, LINE1, LINE2, T  # noqa: E402
from mikage.ui.limit_sim_dialog import DAY_COLUMNS  # noqa: E402
from mikage.ui.plan_file_dock import COLUMNS  # noqa: E402

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
    # The tests here run on today's date with satellites of a fixed TLE: fitted by counting (`orbit: false`), as
    # whatever passes there are today would change them. The passes: test_plan_files_on_the_passes, test_request_passes.py.
    "requestExports": [
        {"system": "SYS-A", "label": "A", "kind": "光学", "series": ["OPT"], "limit": {"count": 1}, "orbit": False, "format": "csv", "encoding": "cp932",
         "timeZone": "Asia/Tokyo", "dateFormat": "%Y%m%d",
         "priority": {"table": [{"max": 200, "value": "High"}, {"max": 600, "value": "Middle"}, {"max": 1000, "value": "Low"}]},
         "columns": [{"key": "ID", "source": "id"}, {"key": "名称", "source": "name"}, {"key": "日", "source": "start"},
                     {"key": "優先度", "source": "priority"}]},
        {"system": "SYS-B", "label": "B", "kind": "SAR", "orbit": False, "format": "json", "schema": "sys-b.schema.json", "timeZone": "UTC"},
    ],
}
SCHEMA = {"type": "object", "required": ["tasks"], "properties": {
    "tasks": {"type": "array", "x-source": "requests", "items": {"type": "object", "required": ["id"], "properties": {
        "id": {"type": "string", "x-source": "id"}, "from": {"type": "string", "format": "date-time", "x-source": "start"},
        "priority": {"type": "integer", "x-source": "priorityRaw"}}}}}}


@pytest.fixture
def plugin(tmp_path, monkeypatch):
    QgsSettings().remove("mikage")
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
    htypes.update(RUN_DATE="esriFieldTypeDate", TARGET_DATE="esriFieldTypeDate", PLANNED_AT="esriFieldTypeDate", CLOUD="esriFieldTypeDouble", SCENES="esriFieldTypeInteger",
                  BOOST_DONE="esriFieldTypeInteger")
    fake.add_layer(HISTORY, FakeLayer(htypes, "esriGeometryNone"))
    fake.add_layer(IMAGES, FakeLayer({"IMAGE_ID": "esriFieldTypeString", "ACQ": "esriFieldTypeDate", "SENSOR": "esriFieldTypeString",
                                      "URL": "esriFieldTypeString"}, "esriGeometryPolygon"))
    from mikage import classFactory

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
    QgsSettings().remove("mikage")


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
    form.management_no.setText(" K-2026-0012 ")
    form.identification_no.setText("000123")
    form._save_clicked()
    assert wait_for(lambda: dock.task is None and len(dock.requests) == 1)
    r = dock.requests[0]
    assert (r.name, r.kind, r.priority, r.category, r.shape_kind) == ("大阪湾", "SAR", 150, "臨時", "点")
    assert (r.management_no, r.identification_no) == ("K-2026-0012", "000123")
    assert fake.layers[LEDGER].rows[0]["attributes"]["IDENTIFICATION_NO"] == "000123"

    # 定点 requests from a point layer: the row without a period is left out.
    layer = fixed_layer()
    dock.import_rows(layer, ask=False)
    assert wait_for(lambda: dock.task is None and len(dock.requests) == 3)
    fixed = {x.name: x for x in dock.requests if x.category == "定点"}
    assert set(fixed) == {"横須賀", "呉"}
    assert fixed["横須賀"].priority == 300 and fixed["呉"].priority == 320 and fixed["呉"].achieve == "毎回"
    # Out again as a layer (定点 only), and back in: same ids, so changed rather than added.
    dock.category_filter.setCurrentText("定点")
    out = dock.export_layer()
    assert out.featureCount() == 2 and out.fields().indexOf("REQ_ID") >= 0
    dock.import_rows(out, ask=False)
    assert wait_for(lambda: dock.task is None and dock.status.text().startswith("2 件を取り込み"))
    assert len(dock.requests) == 3

    # 「レイヤーから取り込み…」: a layer chosen with its name attribute, the rest given once in the form.
    zones = QgsVectorLayer("Polygon?crs=EPSG:4326&field=地区:string&field=番号:integer&field=Management_No:string",
                           "監視区域", "memory")
    feats = []
    for name, lon, no, mgmt in (("舞鶴", 135.3, 11, "K-0011"), ("佐世保", 129.7, 12, None)):
        f = QgsFeature(zones.fields())
        f.setAttributes([name, no, mgmt])
        f.setGeometry(QgsGeometry.fromRect(QgsRectangle(lon, 35.0, lon + 0.1, 35.1)))
        feats.append(f)
    zones.dataProvider().addFeatures(feats)
    QgsProject.instance().addMapLayer(zones)
    dialog = dock.import_layer(zones)
    assert dialog.chosen_layer() is zones and dialog.simple.isChecked()
    # 管理番号・識別番号 by attribute (2026-11-09): a field named as the ledger names it is chosen by itself.
    assert dialog.management_field.currentField() == "Management_No" and dialog.identification_field.currentField() == ""
    dialog.name_field.setField("地区")
    dialog.identification_field.setField("番号")
    dialog.full.setChecked(True)
    assert not dialog.management_field.isEnabled()
    dialog.simple.setChecked(True)
    assert dialog.management_field.isEnabled() and dialog.fields() == {
        "name": "地区", "management_no": "Management_No", "identification_no": "番号"}
    dialog.accept()
    form = dock.form
    assert form.batch is not None and len(form.batch["items"]) == 2 and not form.name.isEnabled()
    assert "管理番号: Management_No・識別番号: 番号" in form.area_label.text()
    assert "Management_No" in form.management_no.placeholderText() and "番号" in form.identification_no.placeholderText()
    form.category.setCurrentText("定点")
    form.kind.setCurrentText("SAR")
    form.priority.setValue(700)
    # For the features whose attribute is empty (佐世保 has no 管理番号).
    form.management_no.setText("K-共通")
    form._save_clicked()
    assert wait_for(lambda: dock.task is None and len(dock.requests) == 5), dock.status.text()
    zoned = {x.name: x for x in dock.requests if x.name in ("舞鶴", "佐世保")}
    assert set(zoned) == {"舞鶴", "佐世保"} and len({x.id for x in zoned.values()}) == 2
    assert all(x.kind == "SAR" and x.priority == 700 and x.category == "定点" and x.shape_kind == "範囲" for x in zoned.values())
    assert abs(zoned["舞鶴"].rings[0][0][0] - 135.3) < 1e-6
    assert (zoned["舞鶴"].management_no, zoned["舞鶴"].identification_no) == ("K-0011", "11")
    assert (zoned["佐世保"].management_no, zoned["佐世保"].identification_no) == ("K-共通", "12")
    # Both numbers are columns of the list too.
    heads = [dock.model.headerData(i, Qt.Orientation.Horizontal) for i in range(dock.model.columnCount())]
    assert heads[:4] == ["名称", "依頼グループ", "管理番号", "識別番号"] and heads[-1] == "状態"
    row = next(i for i, x in enumerate(dock.shown) if x.name == "舞鶴")
    assert [dock.model.item(row, c).text() for c in (2, 3)] == ["K-0011", "11"]
    # Out of the day's work below.
    fake.layers[LEDGER].rows[:] = [row for row in fake.layers[LEDGER].rows if row["attributes"]["REQ_NAME"] not in ("舞鶴", "佐世保")]
    dock.reload()
    assert wait_for(lambda: dock.task is None and len(dock.requests) == 3)

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
    # Filters (each system's file is written by its own person, 2026-11-09): a system shows what it took and what it
    # could still take (呉, held back); the system chosen is kept for next time.
    def listed():
        return [daily.model.item(r, COLUMNS.index("依頼")).text() for r in range(daily.model.rowCount())]

    daily.system_filter.setCurrentIndex(daily.system_filter.findData("SYS-A"))
    assert listed() == ["横須賀", "呉"] and daily.room.text() == "表示 2/3 件　A 1/1 件・1 枚"
    assert settings.plan_file_filter()["system"] == "SYS-A"
    daily.system_filter.setCurrentIndex(daily.system_filter.findData("SYS-B"))
    assert listed() == ["大阪湾"]
    daily.system_filter.setCurrentIndex(0)
    daily.result_filter.setCurrentIndex(daily.result_filter.findData("見送り"))
    assert listed() == ["呉"]
    daily.result_filter.setCurrentIndex(0)
    daily.search.setText("大阪")
    assert listed() == ["大阪湾"]
    daily.search.setText("")
    assert len(listed()) == 3 and daily.room.text().startswith("A ") and settings.plan_file_filter()["system"] == ""

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
    assert sim.daily.rowCount() == 2 and "A 2/2 件" in sim.daily.item(0, DAY_COLUMNS.index("使用")).text()
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
    # The day's plan (2026-11-09, 「撮像予定」, dashboards): the history has the day as a date, the names, and
    # what was handed out waits for the system's answer; the ledger is left as it was.
    today = jst_day(time.time() * 1000)
    rows = {r["attributes"]["REQ_NAME"]: r["attributes"] for r in fake.layers[HISTORY].rows}
    assert set(rows) == {"横須賀", "呉", "大阪湾"}
    assert {a["TARGET_DATE"] for a in rows.values()} == {day_bounds(today)[0]}
    assert {n: (a["RESULT"], a["ADOPTION"]) for n, a in rows.items()} == {
        "横須賀": ("見送り", None), "呉": ("採用", "不明"), "大阪湾": ("採用", "不明")}
    assert not any(k.startswith("LAST_") for r in fake.layers[LEDGER].rows for k in r["attributes"])
    live.manual = "見送り"
    daily.plan.refit()

    # Written again the same day (#71): the earlier rows are replaced, not doubled;
    # SYS-A's answer read in meanwhile stays (tsudo, 2026-11-09).
    kure = next(x.id for x in dock.requests if x.name == "呉")
    answered_ = next(r for r in fake.layers[HISTORY].rows if r["attributes"]["REQ_ID"] == kure)
    answered_["attributes"].update({"ADOPTION": "採用", "EXTERNAL_ID": "A-77", "ADOPTION_SOURCE": "sys-a-result.csv"})
    daily.paths = []
    daily.write(ask=False)
    assert wait_for(lambda: daily.task is None and len(daily.paths) == 2 and "置き換え" in daily.status.text()), daily.status.text()
    assert len(fake.layers[HISTORY].rows) == 3 and "採否を引き継いだ行 1 件" in daily.status.text()
    rows = {r["attributes"]["REQ_ID"]: r["attributes"] for r in fake.layers[HISTORY].rows}
    assert (rows[kure]["ADOPTION"], rows[kure]["EXTERNAL_ID"], rows[kure]["ADOPTION_SOURCE"]) == ("採用", "A-77", "sys-a-result.csv")
    assert all(r is not answered_ for r in fake.layers[HISTORY].rows)  # a new row (this run's), with the answer

    # A system whose file cannot be made (#68): its request is 見送り in the history, not 採用・未判定.
    (tmp_path / "sys-b.schema.json").write_text(json.dumps({**SCHEMA, "required": ["tasks", "missing"]}), encoding="utf-8")
    daily.paths = []
    daily.write(ask=False)
    assert wait_for(lambda: daily.task is None and len(daily.paths) == 1 and "置き換え" in daily.status.text()), daily.status.text()
    rows = {r["attributes"]["REQ_ID"]: r["attributes"] for r in fake.layers[HISTORY].rows}
    osaka = next(x.id for x in dock.requests if x.name == "大阪湾")
    assert (rows[osaka]["RESULT"], rows[osaka]["CAPTURED"]) == ("見送り", "")
    assert rows[osaka]["REASON"].startswith("書き出し失敗")

    # Each system by its own person (2026-11-09): with a system chosen, only its file, history rows and raises.
    (tmp_path / "sys-b.schema.json").write_text(json.dumps(SCHEMA), encoding="utf-8")
    kure = next(x.id for x in dock.requests if x.name == "呉")
    a_file = next(x for x in paths if "SYS-A" in x)
    a_text = open(a_file, encoding="cp932").read()
    daily.reset()
    daily.run()
    assert wait_for(lambda: daily.task is None and "候補 3 件" in daily.status.text()), daily.status.text()
    # All systems: nothing pinned, 横須賀 back in SYS-A.
    got = {c.request.name: (c.result, c.system.system if c.system else "", bool(c.pinned)) for c in daily.plan.candidates}
    assert got == {"横須賀": ("採用", "SYS-A", False), "呉": ("見送り", "", False), "大阪湾": ("採用", "SYS-B", False)}
    assert daily.write_button.text() == "書き出し"
    # SYS-B's person: 呉 is in SYS-A's file (written earlier), so it stays there and 横須賀 is held back.
    daily.system_filter.setCurrentIndex(daily.system_filter.findData("SYS-B"))
    got = {c.request.name: (c.result, c.system.system if c.system else "", bool(c.pinned)) for c in daily.plan.candidates}
    assert got == {"横須賀": ("見送り", "", False), "呉": ("採用", "SYS-A", True), "大阪湾": ("採用", "SYS-B", False)}
    assert listed() == ["大阪湾"] and daily.write_button.text() == "B を書き出し" and daily.write_button.isEnabled()
    before = {r["attributes"]["REQ_ID"]: dict(r["attributes"]) for r in fake.layers[HISTORY].rows}
    daily.paths = []
    daily.write(ask=False)
    assert wait_for(lambda: daily.task is None and len(daily.paths) == 1 and "置き換え" in daily.status.text()), daily.status.text()
    assert "SYS-B" in daily.paths[0] and open(a_file, encoding="cp932").read() == a_text
    rows = {r["attributes"]["REQ_ID"]: r["attributes"] for r in fake.layers[HISTORY].rows}
    assert len(fake.layers[HISTORY].rows) == 3 and (rows[osaka]["RESULT"], rows[osaka]["SYSTEM"]) == ("採用", "SYS-B")
    # SYS-A's rows (and the held one without a system) are left as they were.
    assert rows[kure] == before[kure] and rows[yokosuka] == before[yokosuka]
    # Someone writes SYS-A meanwhile: writing SYS-B stops until 計算 is done again.
    fake.layers[HISTORY].add({"RUN_ID": "OTHER", "TARGET_DAY": daily.plan.day.isoformat(), "REQ_ID": yokosuka, "SYSTEM": "SYS-A",
                              "SATELLITE": "OPT 1", "RESULT": "採用", "CAPTURED": "未判定", "BOOST_DONE": 0})
    daily.paths = []
    daily.write(ask=False)
    assert wait_for(lambda: daily.task is None and "計算" in daily.status.text()), daily.status.text()
    assert daily.paths == [] and "A" in daily.status.text() and len(fake.layers[HISTORY].rows) == 4
    daily.run()
    assert wait_for(lambda: daily.task is None and "書き出し済み 2 件" in daily.status.text()), daily.status.text()
    got = {c.request.name: (c.result, c.system.system, bool(c.pinned)) for c in daily.plan.candidates}
    assert got == {"横須賀": ("採用", "SYS-A", True), "呉": ("採用", "SYS-A", True), "大阪湾": ("採用", "SYS-B", False)}
    daily.write(ask=False)
    assert wait_for(lambda: daily.task is None and len(daily.paths) == 1), daily.status.text()
    assert len(fake.layers[HISTORY].rows) == 4
    # 該当システムなし: nothing to write.
    daily.system_filter.setCurrentIndex(daily.system_filter.findData(""))
    assert not daily.write_button.isEnabled()


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

    # 「計算」 does not follow up (tsudo, 2026-11-11: 「その照合、計算時の照合なくして」): the rows stay 未判定 until 照合.
    asked = len(fake.asked)
    daily.run()
    assert wait_for(lambda: daily.plan is not None and daily.task is None), daily.status.text()
    assert not any(url.startswith(IMAGES) for url, _ in fake.asked[asked:])
    assert {f["attributes"]["CAPTURED"] for f in history.rows if f["attributes"]["RESULT"] == "採用"} == {"未判定"}
    assert "撮像済み" not in daily.status.text()

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
    dock.import_rows(layer, ask=False)
    assert wait_for(lambda: dock.task is None and dock.status.text().startswith("2 件を取り込み")), dock.status.text()
    ids = sorted(r["attributes"]["REQ_ID"] for r in fake.layers[LEDGER].rows)
    assert ids == ["R-10", "R-9"]
    assert {r["attributes"]["REQ_ID"]: r["attributes"]["REQ_NAME"] for r in fake.layers[LEDGER].rows}["R-9"] == "横須賀"
    assert "取り込まなかったもの 1 件" in dock.status.text()


def test_a_satellite_catalog_that_cannot_be_read_gives_the_buttons_back(plugin, monkeypatch):
    """#70: 「計算」 and 「試算」 come back with the reason when the satellite catalog cannot be read."""
    from mikage.core.esri import EsriError
    from mikage.ui import imaging_plan_dock

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


def test_the_form_picks_satellites_in_the_imaging_plan_tree(plugin):
    p, iface, fake, tmp_path = plugin
    dock = p.api.requests
    dock.show()
    assert wait_for(lambda: dock._loaded and dock.task is None)
    dock.edit(new=True)
    form = dock.form
    assert wait_for(lambda: len(form.tree.items()) == 2), form.picked_label.text()
    # The same tree as the imaging plan (kind → series → satellite), nothing checked: any satellite.
    assert [form.tree.tree.topLevelItem(i).text(0) for i in range(form.tree.tree.topLevelItemCount())] == ["光学（0 / 1）", "SAR（0 / 1）"]
    assert not form.hand.isVisibleTo(form) and "指定なし" in form.picked_label.text()
    sar = form.tree.tree.topLevelItem(1).child(0)
    sar.setCheckState(0, Qt.CheckState.Checked)
    assert (form.series.text(), form.satellites.text(), form.modes.text()) == ("RADAR", "", "")
    assert "衛星シリーズ RADAR" in form.picked_label.text()
    form.name.setText("大阪湾")
    form._point_clicked(QgsPointXY(135.3, 34.6))
    form._save_clicked()
    assert wait_for(lambda: dock.task is None and len(dock.requests) == 1)
    assert dock.requests[0].series == ["RADAR"]

    # Names typed by hand (or kept in the ledger) show as checks; names the catalog lacks are kept.
    dock.chosen = dock.requests[0]
    dock.edit()
    form = dock.form
    assert wait_for(lambda: len(form.tree.items()) == 2)
    assert [l.sat.name for l in form.tree.items() if l.checked] == ["RADAR 1"]
    form.by_hand.setChecked(True)
    form.satellites.setText("OPT 1,GONE-1")
    form.series.setText("")
    form._names_typed()
    assert [l.sat.name for l in form.tree.items() if l.checked] == ["OPT 1"]
    assert "GONE-1" in form.picked_label.text()
    form.tree.check_all(False)
    assert form.satellites.text() == "GONE-1"
    form.close()


def test_plan_files_on_the_passes(plugin):
    """
    「計算」 on the satellites' passes (tsudo, 2026-11-11): two places 17 km apart on one pass of OPT 1 go in one shot
    of 2 scenes in a row (apart, the minute between shots would hold one back), so SYS-A's one request a day takes
    both; the table says so, the file has one row with the outline of the shot, the history one row per request.
    """
    from mikage.core.plan.scene import sub_satellite_point
    from mikage.core.plan.satellites import satellites_from_text
    from mikage.core.request.model import ImagingRequest, square_rings
    from mikage.core.request.store import request_store_of, save_request

    p, iface, fake, tmp_path = plugin
    config = json.loads(json.dumps(CONFIG))
    for system in config["requestExports"]:
        system.pop("orbit")
    config["requestExports"][0]["columns"] += [{"key": "IDS", "source": "mergedIds"}, {"key": "AREA", "source": "geometry"}]
    config["satelliteCatalogs"][0]["defaults"] = {"swath": 12, "scenesPerPass": 4, "minInterval": 1}
    (tmp_path / "config.json").write_text(json.dumps(config, ensure_ascii=False), encoding="utf-8")
    p.api.reload_config()
    assert p.api.problems == [], p.api.problems
    opt = satellites_from_text(f"OPT 1\n{LINE1}\n{LINE2}", 45, 0, 12)[0]
    client = EsriClient(fake)
    store = request_store_of({"url": LEDGER, "history": HISTORY}, [], 0)
    for i, (name, where) in enumerate((("北", BELOW), ("南", sub_satellite_point(opt, T + 2500)))):
        save_request(client, store, None, ImagingRequest(id=f"P-{i}", name=name, priority=300 + i, kind="光学", start=T - 6 * 3600000,
                                                         end=T + 6 * 3600000, shape_kind="点", rings=square_rings(*where, 0.1)), T - 86400000)
    daily = p.api.plan_files
    daily.show()
    day = jst_day(T)
    daily.day.setDate(QDate(day.year, day.month, day.day))
    daily.folder.setText(str(tmp_path / "out"))
    daily.run()
    assert wait_for(lambda: daily.plan is not None and daily.task is None), daily.status.text()
    got = {c.request.name: (c.result, c.member, c.scenes) for c in daily.plan.candidates}
    assert got == {"北": ("採用", False, 2), "南": ("採用", True, 0)}, [(c.request.name, c.reason) for c in daily.plan.candidates]
    assert "まとめ 1 組（2 件を 1 回の撮像に）" in daily.status.text()
    rows = {daily.model.item(r, COLUMNS.index("依頼")).text(): [daily.model.item(r, c).text() for c in range(len(COLUMNS))]
            for r in range(daily.model.rowCount())}
    assert "まとめ 2 件（南 と 1 回の撮像）" in rows["北"][COLUMNS.index("結果・理由")]
    assert "まとめ（「北」の撮像に同乗）" in rows["南"][COLUMNS.index("結果・理由")]
    assert rows["南"][COLUMNS.index("枚数")] == "同乗" and rows["北"][COLUMNS.index("枚数")] == "2"
    assert rows["北"][COLUMNS.index("撮像時刻")] == rows["南"][COLUMNS.index("撮像時刻")] == "22:00"
    assert daily.room.text().startswith("A 1/1 件・2 枚")

    daily.write(ask=False)
    assert wait_for(lambda: daily.task is None and len(fake.layers[HISTORY].rows) == 2), daily.status.text()
    [path] = daily.paths
    lines = list(csv.reader(io.StringIO(open(path, encoding="cp932").read())))
    assert len(lines) == 2 and lines[1][0] == "P-0" and lines[1][4] == "P-0,P-1" and lines[1][5].startswith("POLYGON ((")
    history = {r["attributes"]["REQ_NAME"]: r["attributes"] for r in fake.layers[HISTORY].rows}
    assert (history["北"]["MERGED_INTO"], history["北"]["SCENES"]) == (None, 2)
    assert (history["南"]["MERGED_INTO"], history["南"]["SCENES"], history["南"]["RESULT"]) == ("P-0", 0, "採用")


def test_a_request_taken_several_times_a_day(plugin):
    # 1 日の回数 (tsudo, 2026-11-11: 「n回以上、m回以下」): in the form, the ledger and the day's list, a row per time.
    p, iface, fake, tmp_path = plugin
    dock = p.api.requests
    dock.show()
    assert wait_for(lambda: dock._loaded and dock.task is None)
    dock.edit(new=True)
    form = dock.form
    assert (form.times_min.value(), form.times_max.text(), form.times_gap.text()) == (1, "下限と同じ", "間隔 1 時間")
    form.name.setText("大阪湾")
    form.kind.setCurrentText("SAR")
    form.category.setCurrentText("臨時")
    form.times_min.setValue(2)
    form.times_max.setValue(3)
    form.times_gap.setValue(2)
    form._point_clicked(QgsPointXY(135.3, 34.6))
    form._save_clicked()
    assert wait_for(lambda: dock.task is None and len(dock.requests) == 1)
    r = dock.requests[0]
    assert (r.times_min, r.times_max, r.times_gap, r.times) == (2, 3, 2, (2, 3))
    a = fake.layers[LEDGER].rows[0]["attributes"]
    assert (a["TIMES_MIN"], a["TIMES_MAX"], a["TIMES_GAP"]) == (2, 3, 2)
    dock.edit(request=r)
    assert (dock.form.times_min.value(), dock.form.times_max.value(), dock.form.times_gap.value()) == (2, 3, 2)
    dock.form.close()

    daily = p.api.plan_files
    daily.show()
    daily.folder.setText(str(tmp_path / "out"))
    daily.run()
    assert wait_for(lambda: daily.plan is not None and daily.task is None), daily.status.text()
    assert [(c.key, c.result) for c in daily.plan.candidates] == [(r.id, "採用"), (f"{r.id}#2", "採用"), (f"{r.id}#3", "採用")]
    names = [daily.model.item(i, COLUMNS.index("依頼")).text() for i in range(daily.model.rowCount())]
    assert names == ["大阪湾（1/2 回目）", "大阪湾（2/2 回目）", "大阪湾（3 回目・追加）"]
