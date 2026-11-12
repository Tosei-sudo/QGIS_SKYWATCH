"""
The shots apart from the requests in QGIS (tsudo, 2026-11-11: 「マージした結果、
どの範囲になったかも見たい。他方、要求にどう呼応したかも知りたい」): the
calculation draws both on the map before writing, writing keeps one shot
row a row of the file (the shot layer) with each request's history row
naming it, and 撮像予定 lists each request with what became of it and draws
the shots dashed over the requests' own areas.
"""
import copy
import json

import pytest

pytest.importorskip("qgis.core")

from qgis.core import QgsProject  # noqa: E402
from qgis.PyQt.QtCore import QDate  # noqa: E402

from mikage.core.esri import EsriClient  # noqa: E402
from mikage.core.request.model import ImagingRequest, jst_day, square_rings  # noqa: E402
from mikage.core.request.store import DEFAULT_SHOT_FIELDS, request_store_of, save_request  # noqa: E402
from mikage.tests import test_qgis_requests as base  # noqa: E402
from mikage.tests.fake_esri import FakeLayer  # noqa: E402
from mikage.tests.qgis_fakes import wait_for  # noqa: E402
from mikage.tests.test_plan import BELOW, LINE1, LINE2, T  # noqa: E402
from mikage.ui.schedule_dock import COLUMNS as SCHEDULE_COLUMNS  # noqa: E402

plugin = base.plugin
SHOTS = "https://example.com/arcgis/rest/services/Req/FeatureServer/22"


def _setup(plugin):
    from mikage.core.plan.scene import sub_satellite_point
    from mikage.core.plan.satellites import satellites_from_text

    p, _iface, fake, tmp_path = plugin
    types = {n: "esriFieldTypeString" for n in DEFAULT_SHOT_FIELDS.values()}
    types.update(RUN_DATE="esriFieldTypeDate", TARGET_DATE="esriFieldTypeDate", PASS_TIME="esriFieldTypeDate", OFF_NADIR="esriFieldTypeDouble",
                 INCIDENCE="esriFieldTypeDouble", SCENES="esriFieldTypeInteger", SHOT_COUNT="esriFieldTypeInteger",
                 COVERAGE="esriFieldTypeDouble", REQ_COUNT="esriFieldTypeInteger")
    fake.add_layer(SHOTS, FakeLayer(types, "esriGeometryPolygon"))
    config = copy.deepcopy(base.CONFIG)
    for system in config["requestExports"]:
        system.pop("orbit")
    config["requestStores"][0]["shots"] = SHOTS
    config["satelliteCatalogs"][0]["defaults"] = {"swath": 12, "scenesPerPass": 4, "minInterval": 1}
    (tmp_path / "config.json").write_text(json.dumps(config, ensure_ascii=False), encoding="utf-8")
    p.api.reload_config()
    assert p.api.problems == [], p.api.problems
    assert p.api.config.request_stores[0].shots == SHOTS
    opt = satellites_from_text(f"OPT 1\n{LINE1}\n{LINE2}", 45, 0, 12)[0]
    client = EsriClient(fake)
    store = request_store_of({"url": base.LEDGER, "history": base.HISTORY}, [], 0)
    # 北 and 南 17 km apart on one pass of OPT 1: one shot of 2 scenes (see test_plan_files_on_the_passes).
    for i, (name, where) in enumerate((("北", BELOW), ("南", sub_satellite_point(opt, T + 2500)))):
        save_request(client, store, None, ImagingRequest(id=f"P-{i}", name=name, priority=300 + i, kind="光学", start=T - 6 * 3600000,
                                                         end=T + 6 * 3600000, shape_kind="点", rings=square_rings(*where, 0.1)), T - 86400000)
    return p, fake, tmp_path


def _calculate(p, tmp_path):
    daily = p.api.plan_files
    daily.show()
    day = jst_day(T)
    daily.day.setDate(QDate(day.year, day.month, day.day))
    daily.folder.setText(str(tmp_path / "out"))
    daily.run()
    assert wait_for(lambda: daily.plan is not None and daily.task is None), daily.status.text()
    return daily


def test_calculated_drawn_written_and_scheduled_with_their_shot(plugin):
    p, fake, tmp_path = _setup(plugin)
    daily = _calculate(p, tmp_path)
    # Before writing: both requests with their own areas (blue: put together) and the shot dashed over them.
    polygons = daily.map_polygons()
    areas = [x for x in polygons if len(x) == 4]
    [shot] = [x for x in polygons if len(x) == 5]
    assert [x[1] for x in areas] == ["#2563eb", "#2563eb"] and shot[1] == "#7c3aed" and shot[4] is True
    assert len(daily.overlay._painted) == 3
    # Choosing 南 makes it, 北 (in the same shot) and the shot stand out.
    row = next(i for i, c in enumerate(daily.shown) if c.request.name == "南")
    daily.table.selectRow(row)
    assert daily.chosen.request.name == "南"
    widths = {x[3] or "shot" if len(x) == 5 else x[3]: x[2] for x in daily.map_polygons()}
    assert widths == {"": 2, "南": 3, "shot": 3}
    daily.on_map.setChecked(False)
    assert len(daily.overlay._painted) == 0
    daily.on_map.setChecked(True)

    daily.write(ask=False)
    assert wait_for(lambda: daily.task is None and len(fake.layers[SHOTS].rows) == 1), daily.status.text()
    assert "・撮像の範囲 1 件" in daily.status.text()
    [row] = fake.layers[SHOTS].rows
    a = row["attributes"]
    assert (a["REQ_ID"], a["REQ_IDS"], a["REQ_NAMES"], a["REQ_COUNT"], a["SCENES"], a["SYSTEM"]) == ("P-0", "P-0,P-1", "北、南", 2, 2, "SYS-A")
    assert a["TARGET_DAY"] == jst_day(T).isoformat() and a["OUTPUT_FILE"].endswith(".csv") and row["geometry"]["rings"]
    history = {r["attributes"]["REQ_NAME"]: r["attributes"] for r in fake.layers[base.HISTORY].rows}
    assert history["北"]["SHOT_ID"] == history["南"]["SHOT_ID"] == a["SHOT_ID"]

    # Written again (replacing): the day's shot is replaced, not doubled.
    daily.write(ask=False)
    assert wait_for(lambda: daily.task is None and "置き換え" in daily.status.text()), daily.status.text()
    assert len(fake.layers[SHOTS].rows) == 1 and len(fake.layers[base.HISTORY].rows) == 2
    shot_id = fake.layers[SHOTS].rows[0]["attributes"]["SHOT_ID"]
    assert {r["attributes"]["SHOT_ID"] for r in fake.layers[base.HISTORY].rows} == {shot_id}

    # 撮像予定: a row per request with what became of it, the shot dashed over their areas.
    dock = p.api.schedule
    day = jst_day(T)
    dock.calendar.setSelectedDate(QDate(day.year, day.month, day.day))
    dock.show()
    dock.reload(refresh=True)
    assert wait_for(lambda: dock._loaded and dock.task is None and dock.day == day.isoformat() and dock.shots), dock.status.text()
    texts = {dock.model.item(i, 1).text(): dock.model.item(i, SCHEDULE_COLUMNS.index("まとめ")).text() for i in range(dock.model.rowCount())}
    assert texts == {"北": "22:00 まとめ 2 件（南 と）", "南": "22:00 同乗（「北」の撮像）"}
    assert "1 回の撮像に 2 件: 北・南" in dock.model.item(0, SCHEDULE_COLUMNS.index("まとめ")).toolTip()
    assert len(dock.overlay._painted) == 3
    dock.shots_on_map.setChecked(False)
    assert len(dock.overlay._painted) == 2
    dock.shots_on_map.setChecked(True)
    layer = dock.to_layer()
    assert layer.featureCount() == 2 and dock.shot_layer is not None and dock.shot_layer.featureCount() == 1
    f = next(dock.shot_layer.getFeatures())
    assert f["依頼ID"] == "P-0,P-1" and f["依頼件数"] == "2" and not f.geometry().isEmpty()
    assert {f["まとめ"] for f in layer.getFeatures()} == set(texts.values())
    QgsProject.instance().removeAllMapLayers()


def test_without_the_shot_layer_merged_requests_still_say_so(plugin):
    p, fake, tmp_path = _setup(plugin)
    config = json.loads((tmp_path / "config.json").read_text(encoding="utf-8"))
    del config["requestStores"][0]["shots"]
    (tmp_path / "config.json").write_text(json.dumps(config, ensure_ascii=False), encoding="utf-8")
    p.api.reload_config()
    daily = _calculate(p, tmp_path)
    daily.write(ask=False)
    assert wait_for(lambda: daily.task is None and len(fake.layers[base.HISTORY].rows) == 2), daily.status.text()
    assert fake.layers[SHOTS].rows == [] and "撮像の範囲" not in daily.status.text()
    dock = p.api.schedule
    day = jst_day(T)
    dock.calendar.setSelectedDate(QDate(day.year, day.month, day.day))
    dock.show()
    dock.reload(refresh=True)
    assert wait_for(lambda: dock._loaded and dock.task is None and dock.day == day.isoformat() and dock.rows), dock.status.text()
    assert not dock.shots_on_map.isVisibleTo(dock)
    texts = {dock.model.item(i, 1).text(): dock.model.item(i, SCHEDULE_COLUMNS.index("まとめ")).text() for i in range(dock.model.rowCount())}
    # The history's SHOT_ID is kept without the layer; the time comes only with it.
    assert texts == {"北": "まとめ 2 件（南 と）", "南": "同乗（「北」の撮像）"}
