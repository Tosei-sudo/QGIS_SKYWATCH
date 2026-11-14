"""
The 「地点ごとの撮像予定」 dock in QGIS (2026-11-14): points clicked and a
layer's features, each with the plans ahead (history rows of the coming
days, their shots' times) and the days requests will be planned on, on a
timeline under the map; read only.
"""
import json
import time
from datetime import datetime, timedelta

import pytest

pytest.importorskip("qgis.core")
from qgis.testing import start_app  # noqa: E402

APP = start_app()

from qgis.core import QgsFeature, QgsGeometry, QgsPointXY, QgsProject, QgsSettings, QgsVectorLayer  # noqa: E402
from qgis.PyQt.QtCore import Qt  # noqa: E402
from qgis.PyQt.QtWidgets import QMenu  # noqa: E402

from mikage.core.esri import EsriClient  # noqa: E402
from mikage.core.request.model import JST, jst_day  # noqa: E402
from mikage.core.request.place_plans import HEAD, ROW, Place, share_of  # noqa: E402
from mikage.core.request.store import DEFAULT_HISTORY_FIELDS, DEFAULT_REQUEST_FIELDS, DEFAULT_SHOT_FIELDS, HistoryRow, history_attributes  # noqa: E402
from mikage.qgis_io import settings  # noqa: E402
from mikage.qgis_io.context import PluginContext  # noqa: E402
from mikage.tests.fake_esri import FakeEsri, FakeLayer  # noqa: E402
from mikage.tests.qgis_fakes import FakeIface, wait_for  # noqa: E402
from mikage.ui.place_schedule_dock import GeometryShares  # noqa: E402

LEDGER = "https://example.com/arcgis/rest/services/Req/FeatureServer/0"
HISTORY = "https://example.com/arcgis/rest/services/Req/FeatureServer/1"
SHOTS = "https://example.com/arcgis/rest/services/Req/FeatureServer/22"
TOKYO = [[[139.8, 35.45], [139.9, 35.45], [139.9, 35.55], [139.8, 35.55], [139.8, 35.45]]]
OSAKA = [[[135.4, 34.6], [135.5, 34.6], [135.5, 34.7], [135.4, 34.7], [135.4, 34.6]]]
OSAKA_SHOT = [[[135.3, 34.5], [135.6, 34.5], [135.6, 34.8], [135.3, 34.8], [135.3, 34.5]]]

CONFIG = {"cloudForecast": False, "requestStores": [{"label": "撮像依頼", "url": LEDGER, "history": HISTORY, "shots": SHOTS}]}


def at(day, hh, mm=0) -> float:
    return datetime(day.year, day.month, day.day, hh, mm, tzinfo=JST).timestamp() * 1000


@pytest.fixture
def plugin(tmp_path):
    QgsSettings().remove("mikage")
    config = tmp_path / "config.json"
    config.write_text(json.dumps(CONFIG, ensure_ascii=False), encoding="utf-8")
    settings.set_config_source(str(config))
    today = jst_day(time.time() * 1000)
    fake = FakeEsri()
    types = {n: "esriFieldTypeString" for n in DEFAULT_REQUEST_FIELDS.values()}
    types.update({n: "esriFieldTypeDate" for n in ("START_AT", "END_AT", "CREATED_AT", "UPDATED_AT")})
    types.update({n: "esriFieldTypeDouble" for n in ("PRIORITY", "BOOST")})
    ledger = fake.add_layer(LEDGER, FakeLayer(types, "esriGeometryPolygon"))
    start, end = at(today, 0), at(today + timedelta(days=10), 23)
    for rid, name, rings, extra in (("R-1", "東京湾", TOKYO, {}), ("R-2", "大阪湾", OSAKA, {}),
                                    ("R-3", "東京 月曜", TOKYO, {"CATEGORY": "定点", "ACHIEVE": "毎回", "REPEAT": "曜日:月"}),
                                    ("R-4", "東京 停止", TOKYO, {"STATUS": "停止"})):
        ledger.add({"REQ_ID": rid, "REQ_NAME": name, "PRIORITY": 500, "KIND": "光学", "CATEGORY": "臨時", "ACHIEVE": "期間内1回",
                    "STATUS": "有効", "START_AT": start, "END_AT": end, **extra}, {"rings": rings, "spatialReference": {"wkid": 4326}})
    htypes = {n: "esriFieldTypeString" for n in DEFAULT_HISTORY_FIELDS.values()}
    htypes.update(RUN_DATE="esriFieldTypeDate", TARGET_DATE="esriFieldTypeDate", PLANNED_AT="esriFieldTypeDate", CLOUD="esriFieldTypeDouble",
                  SCENES="esriFieldTypeInteger", BOOST_DONE="esriFieldTypeInteger")
    history = fake.add_layer(HISTORY, FakeLayer(htypes, "esriGeometryPolygon"))
    stypes = {n: "esriFieldTypeString" for n in DEFAULT_SHOT_FIELDS.values()}
    stypes.update(RUN_DATE="esriFieldTypeDate", TARGET_DATE="esriFieldTypeDate", PASS_TIME="esriFieldTypeDate")
    shots = fake.add_layer(SHOTS, FakeLayer(stypes, "esriGeometryPolygon"))
    now = time.time() * 1000
    d1, d2, d3, d4 = (today + timedelta(days=k) for k in (1, 2, 3, 4))
    for row in (
            HistoryRow("RUN1", now, d1.isoformat(), "R-1", system="SYS-A", satellite="ASNARO-2", name="東京湾", adoption="採用",
                       planned_at=at(d1, 10, 30), rings=TOKYO),
            HistoryRow("RUN1", now, d2.isoformat(), "R-2", system="SYS-B", name="大阪湾", adoption="不明", shot_id="S-1", rings=OSAKA),
            HistoryRow("RUN1", now, d3.isoformat(), "", system="SYS-A", name="定常 東京", result="要求外", adoption="採用", rings=TOKYO),
            HistoryRow("RUN1", now, d4.isoformat(), "R-1", system="SYS-B", name="東京湾", adoption="不採用", rings=TOKYO),
            # Long past: not read.
            HistoryRow("RUN0", now, (today - timedelta(days=5)).isoformat(), "R-1", system="SYS-A", name="東京湾", adoption="採用", rings=TOKYO)):
        history.add({k: v for k, v in history_attributes(row, DEFAULT_HISTORY_FIELDS).items() if v is not None},
                    {"rings": row.rings, "spatialReference": {"wkid": 4326}})
    shots.add({"SHOT_ID": "S-1", "RUN_ID": "RUN1", "TARGET_DAY": d2.isoformat(), "SYSTEM": "SYS-B", "SATELLITE": "SAR-1",
               "PASS_TIME": at(d2, 22, 15), "REQ_ID": "R-2"}, {"rings": OSAKA_SHOT, "spatialReference": {"wkid": 4326}})
    from mikage import classFactory

    iface = FakeIface()
    p = classFactory(iface)
    original = PluginContext.esri
    PluginContext.esri = lambda self: EsriClient(fake)
    p.initGui()
    for dock in p.docks.values():
        dock.hide()
    assert p.api.problems == [], p.api.problems
    yield p, iface, today
    p.unload()
    PluginContext.esri = original
    QgsProject.instance().removeAllMapLayers()
    QgsSettings().remove("mikage")


def facilities() -> QgsVectorLayer:
    layer = QgsVectorLayer("Polygon?crs=EPSG:4326&field=名称:string", "施設", "memory")
    for name, wkt in (("東京の港", "POLYGON((139.82 35.47, 139.86 35.47, 139.86 35.51, 139.82 35.51, 139.82 35.47))"),
                      ("大阪の港", "POLYGON((135.42 34.62, 135.46 34.62, 135.46 34.66, 135.42 34.66, 135.42 34.62))"),
                      ("沖", "POLYGON((150 40, 150.1 40, 150.1 40.1, 150 40.1, 150 40))")):
        f = QgsFeature(layer.fields())
        f.setAttribute("名称", name)
        f.setGeometry(QgsGeometry.fromWkt(wkt))
        layer.dataProvider().addFeature(f)
    QgsProject.instance().addMapLayer(layer)
    return layer


def names(dock):
    return [lane.place.name for lane in dock.lanes]


def test_the_plans_ahead_of_points_clicked_and_a_layer_s_features(plugin):
    p, iface, today = plugin
    dock = p.api.place_schedule
    assert dock.windowTitle() == "地点ごとの撮像予定"
    # Under the map, as wide as it.
    assert iface.window.dockWidgetArea(dock) == Qt.DockWidgetArea.BottomDockWidgetArea
    dock.show()
    assert wait_for(lambda: dock._loaded and dock.task is None), dock.status.text()
    # Low the first time, so that the map keeps most of the window.
    assert wait_for(lambda: settings._get("placeSchedule/sized") == "1")
    # Today's and the 13 days after: the old row is not read.
    assert dock.read_days == 14 and {r.day for r in dock.rows} == {(today + timedelta(days=k)).isoformat() for k in (1, 2, 3, 4)}
    assert dock.lanes == [] and "地図で地点を追加" in dock.timeline.empty_text

    # A point clicked in Tokyo: the adopted plan tomorrow at 10:30 is next.
    dock.add_point(139.85, 35.5)
    [lane] = dock.lanes
    assert lane.next.start == at(today + timedelta(days=1), 10, 30)
    assert lane.place.name == "地点 1" and "緯度 35.50000・経度 139.85000" in dock.timeline.tip_at(20, HEAD + 5)
    assert lane.next.row.name == "東京湾" and dock.timeline.lay.labels[0][2].endswith("10:30 採用・ASNARO-2")
    plans = [(m.state, m.row.name) for m in lane.marks if m.is_plan]
    assert plans == [("採用", "東京湾"), ("要求外", "定常 東京")]
    # 不採用 only when asked.
    dock.not_taken.setChecked(True)
    assert ("不採用", "東京湾") in [(m.state, m.row.name) for m in dock.lanes[0].marks if m.is_plan]
    dock.not_taken.setChecked(False)
    # The requests' days: R-1 (no row today) and R-3 on Mondays; R-4 is stopped.
    asked = {m.day: sorted(q.id for q in m.requests) for m in dock.lanes[0].marks if not m.is_plan}
    assert asked[today.isoformat()][0] == "R-1" and "R-4" not in {i for ids in asked.values() for i in ids}
    monday = next(today + timedelta(days=k) for k in range(7) if (today + timedelta(days=k)).weekday() == 0)
    assert "R-3" in asked[monday.isoformat()]
    # The point is on the map, numbered.
    assert len(dock.points_overlay._items) == 2

    # A layer's features: a line each after the point, named by the attribute guessed.
    layer = facilities()
    dock.layer_choice.setLayer(layer)
    assert dock.name_field.currentField() == "名称"
    assert names(dock)[1:] == ["東京の港", "大阪の港", "沖"]
    osaka = dock.lanes[2]
    # 大阪湾's plan at its shot's pass (22:15), not answered yet.
    d2 = today + timedelta(days=2)
    assert osaka.next.row.name == "大阪湾" and osaka.next.timed and osaka.next.start == at(d2, 22, 15)
    assert dock.lanes[3].next is None and dock.timeline.lay.labels[3][2] == "予定なし"
    assert dock.status.text().endswith("地点 4・計画あり 3・予定なし 1")
    # The soonest first: Tokyo's (tomorrow) before Osaka's (the day after), 沖 last.
    dock.by_next.setChecked(True)
    assert names(dock)[2:] == ["大阪の港", "沖"]
    dock.by_next.setChecked(False)
    # Only the selected features.
    layer.selectByExpression("\"名称\" = '沖'")
    dock.selected_only.setChecked(True)
    assert names(dock)[1:] == ["沖"]
    layer.selectByExpression("\"名称\" = '大阪の港'")
    assert names(dock)[1:] == ["大阪の港"]
    dock.selected_only.setChecked(False)

    # Fewer days: worked out again without reading (3 days: Osaka's plan the day after tomorrow still in, 定常 not).
    reads = dock.read_days
    dock.days_choice.setCurrentIndex(dock.days_choice.findData(3))
    assert dock.task is None and dock.read_days == reads
    assert [(m.row.name) for m in dock.lanes[0].marks if m.is_plan] == ["東京湾"]
    assert settings._get("placeSchedule/days") == "3"

    # The timeline: the tooltip of the plan, a click draws it on the map.
    lay = dock.timeline.lay
    x0, x1, y0, y1, mark = next(b for b in lay.marks if b[4].is_plan and b[4].row.name == "東京湾" and HEAD <= b[2] < HEAD + ROW)
    tip = dock.timeline.tip_at((x0 + x1) / 2, (y0 + y1) / 2)
    assert tip.startswith("東京湾: 採用\n") and "SYS-A / ASNARO-2" in tip
    dock.timeline.click((x0 + x1) / 2, (y0 + y1) / 2)
    assert dock.timeline.chosen is mark and dock.overlay.painted()
    assert dock.chosen_label.text().startswith("地点 1")
    # A click on Osaka's name: the map goes there.
    dock.timeline.click(20, HEAD + 2 * ROW + 5)
    assert dock.canvas.extent().contains(QgsPointXY(135.44, 34.64))

    # From the map's right-click menu: one more point, the dock shown.
    dock.hide()
    menu = QMenu()

    class Event:
        def mapPoint(self):  # noqa: N802
            return QgsPointXY(135.45, 34.65)

    p._canvas_menu(menu, Event())
    next(a for a in menu.actions() if a.text() == "この地点の撮像予定").trigger()
    assert dock.isVisible() and names(dock)[1].startswith("地点 2")
    assert dock.lanes[1].next.row.name == "大阪湾"
    dock.clear_points()
    assert names(dock) == ["東京の港", "大阪の港", "沖"] and dock.points_overlay._items == []
    dock.hide()
    assert not dock.overlay.painted()


def test_qgis_shares_agree_with_the_core():
    shares = GeometryShares()
    half = Place("半分", rings=[[[139.85, 35.45], [139.95, 35.45], [139.95, 35.55], [139.85, 35.55], [139.85, 35.45]]])
    assert abs(shares(half, TOKYO) - 0.5) < 0.01 and abs(share_of(half, TOKYO) - 0.5) < 0.1
    point = Place("点", points=[[139.85, 35.5]])
    assert shares(point, TOKYO) == share_of(point, TOKYO) == 1
    assert shares(point, OSAKA) is None and share_of(point, OSAKA) is None
    line = Place("線", lines=[[[139.7, 35.5], [140.0, 35.5]]])
    assert shares(line, TOKYO) is not None and share_of(line, TOKYO) is not None
    # A ring crossing itself is mended, not an error.
    bow = [[[139.8, 35.45], [139.9, 35.55], [139.9, 35.45], [139.8, 35.55], [139.8, 35.45]]]
    assert shares(point, bow) is not None
