"""
The imaging plan dock in QGIS (the viewer's imaging-plan.spec.ts, as far as
stage 2 goes): a satellite catalog from a fake Esri layer, targets from a
layer, the selection or a clicked point, the list, the map, CSV and a layer.
"""

import pytest

pytest.importorskip("qgis.core")
from qgis.testing import start_app  # noqa: E402

APP = start_app()
import sys, traceback  # noqa: E401
sys.excepthook = lambda *a: traceback.print_exception(*a)

from qgis.core import QgsFeature, QgsGeometry, QgsPointXY, QgsProject, QgsSettings, QgsVectorLayer  # noqa: E402
from qgis.PyQt.QtCore import QDateTime, QTimeZone  # noqa: E402

from satellite_observation.core.esri import EsriClient  # noqa: E402
from satellite_observation.qgis_io import settings  # noqa: E402
from satellite_observation.qgis_io.context import PluginContext  # noqa: E402
from satellite_observation.tests.fake_esri import FakeEsri, FakeLayer  # noqa: E402
from satellite_observation.tests.qgis_fakes import FakeIface, wait_for  # noqa: E402
from satellite_observation.tests.test_plan import EPOCH, LINE1, LINE2  # noqa: E402

CATALOG = "https://example.com/arcgis/rest/services/Sats/FeatureServer/0"
SAR2 = "2 90002  98.1800 283.0000 0001300  90.0000 270.0000 14.59200000    16"
SAR1 = "1 90002U 26902A   26280.50000000  .00000100  00000-0  40000-4 0  9992"


@pytest.fixture
def plugin(tmp_path, monkeypatch):
    QgsSettings().remove("satellite_observation")
    config = tmp_path / "config.json"
    config.write_text('{"satelliteCatalogs": [{"label": "テスト衛星", "url": "%s", '
                      '"fields": {"name": "NAME", "tle": "TLE", "kind": "KIND", "maxOffNadir": "MAX", "lookSide": "LOOK", "minInterval": "GAP", "swath": "SWATH", "scenesPerPass": "SCENES"}}]}' % CATALOG,
                      encoding="utf-8")
    settings.set_config_source(str(config))
    fake = FakeEsri()
    layer = fake.add_layer(CATALOG, FakeLayer({n: "esriFieldTypeString" for n in ("NAME", "TLE", "KIND", "LOOK")} | {n: "esriFieldTypeDouble" for n in ("MAX", "GAP", "SWATH", "SCENES")}))
    layer.add({"NAME": "OPT-1", "TLE": f"{LINE1}\n{LINE2}", "KIND": "光学", "MAX": 30, "SWATH": 40, "SCENES": 4})
    layer.add({"NAME": "SAR-1", "TLE": f"{SAR1}\n{SAR2}", "KIND": "SAR", "MAX": 45, "LOOK": "R", "GAP": 10, "SWATH": 50, "SCENES": 3})
    layer.add({"NAME": "NO-TLE"})
    from satellite_observation import classFactory

    iface = FakeIface()
    p = classFactory(iface)
    # The docks open at start read their catalogs right away.
    monkeypatch.setattr(PluginContext, "esri", lambda self: EsriClient(fake))
    p.initGui()
    # Each test opens the docks it uses.
    for dock in p.docks.values():
        dock.hide()
    assert p.api.problems == [] and p.api.config.satellite_catalogs, p.api.problems
    yield p, iface, tmp_path
    p.unload()
    QgsProject.instance().removeAllMapLayers()
    QgsSettings().remove("satellite_observation")


def points_layer(points):
    layer = QgsVectorLayer("Point?crs=EPSG:4326&field=name:string&field=priority:integer", "地点", "memory")
    features = []
    for name, lon, lat in points:
        f = QgsFeature(layer.fields())
        f.setGeometry(QgsGeometry.fromPointXY(QgsPointXY(lon, lat)))
        f.setAttributes([name, 1])
        features.append(f)
    layer.dataProvider().addFeatures(features)
    QgsProject.instance().addMapLayer(layer)
    return layer


def start_at(dock, ms, days=14):
    dock.start.setDateTime(QDateTime.fromMSecsSinceEpoch(int(ms), QTimeZone.utc()))
    dock.days.setCurrentIndex(dock.days.findData(days))


def plan(dock, ms=30000):
    dock.run()
    assert wait_for(lambda: dock.plan_task is None and dock.loading_task is None, ms), dock.status.text()


def test_plans_the_passes_over_a_layer_from_a_catalog(plugin):
    p, iface, tmp = plugin
    layer = points_layer([("東京", 139.7, 35.68), ("大阪", 135.5, 34.7)])
    dock = p.api.imaging_plan
    dock.show()
    assert wait_for(lambda: dock.loading_task is None)
    assert "テスト衛星: 2 機" in dock.status.text() and "NO-TLE: TLE なし" in dock.status.text()
    assert dock.target.currentData() == f"layer:{layer.id()}"
    start_at(dock, EPOCH)
    plan(dock)
    assert "最短は" in dock.status.text()
    ops = dock.shown
    assert len(ops) > 2
    assert [dock.model.headerData(i, 1) for i in range(dock.model.columnCount())][:3] == ["日時", "衛星", "対象"]
    assert all(b.time >= a.time for a, b in zip(ops, ops[1:]))
    sats = dock.plan["sats"]
    for op in ops:
        if sats[op.satellite].name == "OPT-1":
            assert op.offNadir <= 30 and op.sunElevation >= 10
        else:
            assert op.side == "right"
    # The first row is chosen and its pass drawn.
    assert dock.chosen is ops[0]
    assert dock.overlay.extent(["scene"]) is not None and dock.overlay.extent(["track"]) is not None
    dock.table.selectRow(1)
    assert dock.chosen is ops[1]
    # Sorting by the angle.
    dock._sort_by(next(i for i, c in enumerate(dock.model.columns) if c.key == "offNadir"))
    angles = [o.offNadir for o in dock.shown]
    assert angles == sorted(angles)
    dock.first.setChecked(True)
    assert len(dock.shown) <= 4
    # CSV and layer.
    assert dock.default_file_name() == "imaging-plan_20261007-20261021.csv" or dock.default_file_name().startswith("imaging-plan_2026100")
    path = str(tmp / "plan.csv")
    dock.save_csv(path)
    text = open(path, encoding="utf-8").read()
    assert text.startswith("﻿日時（UTC）") and text.count("\n") == len(dock.shown) + 1
    added = dock.add_layer()
    assert added.featureCount() == len(dock.shown)
    assert added.name().startswith("撮像計画 ")
    assert added.temporalProperties().isActive()
    assert added.fields().field("time").alias() == "撮像日時"
    assert QgsProject.instance().mapLayer(added.id()) is added


def test_without_a_catalog_plans_the_samples_and_pasted_tles_over_a_clicked_point(plugin):
    p, iface, tmp = plugin
    settings.set_config_source("")
    p.context.reload()
    dock = p.api.imaging_plan
    dock.show()
    assert "サンプル衛星 2 機" in dock.status.text()
    assert dock.tree.count.text() == "衛星（2 / 2）"
    assert dock.target.currentData() == "click"
    dock.tle.setPlainText(f"ISS\n{LINE1}\n{LINE2}")
    assert dock.tree.count.text() == "衛星（3 / 3）"
    dock.run()
    assert "地図をクリック" in dock.status.text()
    dock.set_point(139.7, 35.68)
    assert "を対象に設定" in dock.status.text()
    start_at(dock, EPOCH, 7)
    plan(dock)
    assert "最短は" in dock.status.text()
    assert "対象" not in [c.label for c in dock.model.columns]


def test_combines_passes_to_cover_a_wide_polygon(plugin):
    p, iface, tmp = plugin
    layer = QgsVectorLayer("Polygon?crs=EPSG:4326&field=name:string", "範囲", "memory")
    f = QgsFeature(layer.fields())
    f.setGeometry(QgsGeometry.fromWkt("POLYGON((139 35, 140.6 35, 140.6 36.2, 139 36.2, 139 35))"))
    f.setAttributes(["関東"])
    layer.dataProvider().addFeatures([f])
    QgsProject.instance().addMapLayer(layer)
    dock = p.api.imaging_plan
    dock.show()
    assert wait_for(lambda: dock.loading_task is None)
    dock.daylight.setCurrentIndex(dock.daylight.findData("none"))
    start_at(dock, EPOCH, 30)
    plan(dock, 60000)
    assert dock.view.currentData() == "cover"
    assert dock.status.text().startswith("関東:") and "パスで" in dock.status.text()
    labels = [c.label for c in dock.model.columns]
    assert "追加" in labels and "累計" in labels
    steps = dock.shown
    assert len(steps) > 1, (dock.status.text(), [(o.coverage, o.satellite) for o in dock.plan["result"].opportunities][:20])
    assert all(s.gain > 0 for s in steps)
    # The other passes are numbered on the map.
    assert dock.overlay.extent(["step"]) is not None
    dock.view.setCurrentIndex(dock.view.findData("all"))
    assert "被覆" in [c.label for c in dock.model.columns]


def test_the_selection_and_cancelling(plugin):
    p, iface, tmp = plugin
    layer = points_layer([("a", 139.7, 35.68), ("b", 135.5, 34.7), ("c", 141.35, 43.06)])
    layer.selectByIds([1, 3])
    iface.setActiveLayer(layer)
    dock = p.api.imaging_plan
    dock.show()
    assert dock.target.currentData() == "selection"
    assert dock.target.currentText() == "選択中の地物（2）"
    assert [t.label for t in dock.targets()] == ["a", "c"]
    dock.each.setChecked(False)
    [one] = dock.targets()
    assert one.label == "選択中の地物"
    dock.each.setChecked(True)
    dock.name_field.setCurrentIndex(dock.name_field.findData("priority"))
    assert [t.label for t in dock.targets()] == ["1", "1"]
    start_at(dock, EPOCH, 30)
    assert wait_for(lambda: dock.loading_task is None)
    dock.run()
    dock.cancel()
    assert dock.status.text() == "計算の取消"
    assert dock.run_button.isEnabled()


def test_plans_a_point_from_the_maps_right_click_menu(plugin):
    from qgis.core import QgsCoordinateReferenceSystem, QgsRectangle
    from qgis.gui import QgsMapMouseEvent
    from qgis.PyQt.QtCore import QEvent, QPoint, Qt
    from qgis.PyQt.QtWidgets import QMenu

    p, iface, tmp = plugin
    settings.set_config_source("")
    p.context.reload()
    canvas = iface.canvas
    canvas.setDestinationCrs(QgsCoordinateReferenceSystem("EPSG:3857"))
    canvas.setExtent(QgsRectangle(15540000, 4250000, 15560000, 4270000))
    canvas.refresh()
    dock = p.api.imaging_plan
    start_at(dock, EPOCH, 7)
    # The middle of the canvas, whatever its size.
    event = QgsMapMouseEvent(canvas, QEvent.Type.MouseButtonPress, QPoint(canvas.width() // 2, canvas.height() // 2), Qt.MouseButton.RightButton)
    clicked = event.mapPoint()
    menu = QMenu()
    canvas.contextMenuAboutToShow.emit(menu, event)
    [action] = [a for a in menu.actions() if a.text() == "ここを撮像計画"]
    action.trigger()
    assert not dock.isHidden()
    assert dock.target.currentData() == "click"
    assert wait_for(lambda: dock.plan is not None and dock.plan_task is None, 30000), dock.status.text()
    assert "最短は" in dock.status.text() or "機会なし" in dock.status.text()
    [target] = dock.plan["targets"]
    from satellite_observation.qgis_io.geometry import to_wgs84

    expected = to_wgs84(canvas.mapSettings().destinationCrs()).transform(clicked)
    assert abs(target.center[0] - expected.x()) < 1e-6 and abs(target.center[1] - expected.y()) < 1e-6
    assert 139 < target.center[0] < 141 and 35 < target.center[1] < 36


def _pass_points():
    """Points along an ISS pass a minute apart (as the viewer's tests) and one 4 km beside the first."""
    from satellite_observation.core.plan.scene import sub_satellite_point
    from satellite_observation.tests.test_plan import BELOW, T, east, iss

    return T, [("a", *BELOW), ("b", *sub_satellite_point(iss(), T + 50000)), ("near", *east(BELOW, 4))]


def _iss_only(plugin, tmp, gap=10, swath=10):
    p, iface, _ = plugin
    config = tmp / "iss.json"
    config.write_text("{}", encoding="utf-8")
    settings.set_config_source(str(config))
    p.context.reload()
    dock = p.api.imaging_plan
    # The dock open at start reads the (new) catalogs again: the choice below comes after.
    assert wait_for(lambda: dock.loading_task is None)
    dock.tree.check_all(False)
    dock.paste_gap.setValue(gap)
    dock.paste_swath.setValue(swath)
    dock.paste_max.setValue(45)
    dock.tle.setPlainText(f"ISS\n{LINE1}\n{LINE2}")
    dock.tree.check_only(["ISS"])
    assert [s.name for s in dock.tree.checked()] == ["ISS"]
    return dock


def test_priorities_and_points_in_one_scene(plugin):
    p, iface, tmp = plugin
    T, points = _pass_points()
    layer = QgsVectorLayer("Point?crs=EPSG:4326&field=name:string&field=rank:integer&field=note:string", "地点", "memory")
    features = []
    for (name, lon, lat), rank in zip(points, [2, 1, 2]):
        f = QgsFeature(layer.fields())
        f.setGeometry(QgsGeometry.fromPointXY(QgsPointXY(lon, lat)))
        f.setAttributes([name, rank, "x"])
        features.append(f)
    layer.dataProvider().addFeatures(features)
    QgsProject.instance().addMapLayer(layer)
    dock = _iss_only(plugin, tmp)
    dock.show()
    dock.list_targets()
    # Only attributes holding numbers are offered; "rank" is not guessed (no priority in its name).
    assert [dock.priority.itemData(i) for i in range(dock.priority.count())] == ["", "rank"]
    dock.more.setCollapsed(False)
    assert dock.priority.isVisibleTo(dock)
    # Hidden again by collapsing 「詳細条件」, the priority still applies.
    dock.more.setCollapsed(True)
    dock.priority.setCurrentIndex(1)
    dock.priority_order.setCurrentIndex(dock.priority_order.findData("large"))
    targets = dock.targets()
    assert [(t.label, t.priority, t.priority_text) for t in targets] == [("a", 2, "2"), ("b", 1, "1"), ("near", 2, "2")]
    start_at(dock, T - 60000, 3)
    dock.daylight.setCurrentIndex(dock.daylight.findData("none"))
    plan(dock)
    dock.view.setCurrentIndex(dock.view.findData("cover"))
    assert "優先度の高い対象から撮像を割り当て" in dock.status.text()
    assert "優先度" in [c.label for c in dock.model.columns]
    # The more important first: "a" and "near" (4 km apart) share one scene of the pass; "b", a minute on, waits for the interval.
    plans = dock.cover_plans()
    a_step, b_step, near_step = (pl.steps[0] for pl in plans)
    assert a_step.shot == near_step.shot and near_step.shotTargets == 2
    assert b_step.time - a_step.time >= 10 * 60000 and plans[1].blocked > 0
    assert "全 2 シーン（うち 1 シーンで 2 地点をまとめて撮像）" in dock.status.text()
    # Without sharing, each point its own scene.
    dock.share.setChecked(False)
    assert "全 3 シーン" in dock.status.text()


def test_moving_the_chosen_scenes_on_the_map(plugin):
    from qgis.core import QgsCoordinateReferenceSystem
    from qgis.gui import QgsMapMouseEvent
    from qgis.PyQt.QtCore import QEvent, QPoint, Qt

    from satellite_observation.qgis_io.geometry import from_wgs84

    p, iface, tmp = plugin
    T, points = _pass_points()
    dock = _iss_only(plugin, tmp, gap=0, swath=10)
    dock.show()
    start_at(dock, T - 30 * 60000, 1)
    dock.daylight.setCurrentIndex(dock.daylight.findData("none"))
    dock.set_point(points[0][1], points[0][2])
    plan(dock)
    op = dock.chosen
    assert abs(op.time - T) < 60000 and op.coverage == 1 and op.offNadir < 1
    canvas = iface.canvas
    canvas.setDestinationCrs(QgsCoordinateReferenceSystem("EPSG:3857"))
    dock.zoom_to_chosen()
    canvas.refresh()
    dock.move_button.setChecked(True)
    assert canvas.mapTool() is dock.drag_tool
    ring = dock._scenes[0]
    cx, cy = sum(x for x, _ in ring[:-1]) / (len(ring) - 1), sum(y for _, y in ring[:-1]) / (len(ring) - 1)
    to_map = from_wgs84(canvas.mapSettings().destinationCrs())

    def pixel(lon, lat):
        q = canvas.getCoordinateTransform().transform(to_map.transform(QgsPointXY(lon, lat)))
        return QPoint(int(q.x()), int(q.y()))

    def mouse(kind, at):
        return QgsMapMouseEvent(canvas, kind, at, Qt.MouseButton.LeftButton, Qt.MouseButton.LeftButton)

    start = pixel(cx, cy)
    # 30 km east: off the point, the angle grows.
    end = pixel(cx + 30 / (111.32 * 0.8), cy)
    dock.drag_tool.canvasPressEvent(mouse(QEvent.Type.MouseButtonPress, start))
    dock.drag_tool.canvasMoveEvent(mouse(QEvent.Type.MouseMove, end))
    dock.drag_tool.canvasReleaseEvent(mouse(QEvent.Type.MouseButtonRelease, end))
    assert op.adjusted and op.offNadir > 2 and op.coverage == 0
    assert dock.status.text().startswith("撮像範囲を移動: オフナディア角") and dock.status.text().endswith("対象外")
    assert dock.model.data(dock.model.index(0, 0)).endswith("✎")
    # The CSV and the layer follow the moved scene.
    assert "✎" not in dock.csv_text()
    assert dock.csv_text().splitlines()[1].split(",")[9] == js_round_text(op.offNadir)
    dock.move_button.setChecked(False)
    assert canvas.mapTool() is not dock.drag_tool


def js_round_text(v):
    from satellite_observation.core.plan.output import js_round

    return js_round(v, 2)


def test_the_timeline(plugin):
    p, iface, tmp = plugin
    settings.set_config_source("")
    p.context.reload()
    dock = p.api.imaging_plan
    dock.show()
    dock.set_point(139.7, 35.68)
    start_at(dock, EPOCH, 7)
    plan(dock)
    dock.timeline_box.setChecked(True)
    assert dock.timeline.isVisible()
    # The sample SAR satellite's modes share its row.
    assert dock.timeline.rows() == len({dock.plan["sats"][o.satellite].name for o in dock.shown})
    last = dock.shown[-1]
    dock.timeline.picked.emit(last)
    assert dock.chosen is last
    assert dock.table.currentIndex().row() == len(dock.shown) - 1
    dock.timeline_box.setChecked(False)
    assert not dock.timeline.isVisible()
