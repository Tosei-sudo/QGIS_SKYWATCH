"""
The dock 「ダッシュボード」 in QGIS, as the viewer's dashboard.spec.ts: counts,
the values of an attribute, the distribution of a number and the count over
time of a vector layer, and a heatmap; its charts filter the layer (map and
attribute table), the time chart sets the temporal controller, and the
project keeps the layer's own filter and the dashboard's state.
"""
import json

import pytest

pytest.importorskip("qgis.core")
from qgis.testing import start_app  # noqa: E402

APP = start_app()

from qgis.core import (  # noqa: E402
    QgsProject, QgsRectangle, QgsSettings, QgsVectorLayer, QgsVectorLayerTemporalProperties,
)
from qgis.PyQt.QtCore import QDateTime  # noqa: E402

from satellite_observation.qgis_io import settings  # noqa: E402
from satellite_observation.tests.qgis_fakes import FakeIface, wait_for  # noqa: E402

KINDS = ["公園", "公園", "公園", "学校", "学校", "病院"]


def survey(path):
    path.write_text(json.dumps({"type": "FeatureCollection", "features": [
        {"type": "Feature", "geometry": {"type": "Point", "coordinates": [139.7 + i * 0.01, 35.68]},
         "properties": {"name": f"地点{i + 1}", "kind": kind, "value": (i + 1) * 10, "observed": f"2024-0{i + 1}-15T00:00:00"}}
        for i, kind in enumerate(KINDS)]}, ensure_ascii=False), encoding="utf-8")
    layer = QgsVectorLayer(str(path), "survey", "ogr")
    assert layer.isValid()
    return layer


@pytest.fixture
def plugin(tmp_path):
    from satellite_observation import classFactory

    QgsSettings().remove("satellite_observation")
    settings.set_config_source("")
    iface = FakeIface()
    p = classFactory(iface)
    p.initGui()
    layer = survey(tmp_path / "survey.geojson")
    QgsProject.instance().addMapLayer(layer)
    iface.canvas.setDestinationCrs(layer.crs())
    iface.canvas.setLayers([layer])
    iface.canvas.setExtent(QgsRectangle(139.69, 35.6, 139.76, 35.76))
    yield p, iface, layer, tmp_path
    p.unload()
    QgsProject.instance().clear()
    QgsSettings().remove("satellite_observation")


def shown(layer):
    return sorted(f["name"] for f in layer.getFeatures())


def settle(dock):
    APP.processEvents()
    dock.render()


def test_the_charts_filter_the_layer(plugin):
    p, iface, layer, tmp_path = plugin
    iface.setActiveLayer(layer)
    dock = p.api.dashboard
    dock.toggleViewAction().trigger()
    settle(dock)
    assert dock.layer is layer and dock.figures["地物"] == 6 and dock.figures["表示範囲内"] == 6
    assert dock.group == "kind" and dock.measure == "value"
    assert [(c.value, c.figure) for c in dock.categories] == [("公園", 3), ("学校", 2), ("病院", 1)]
    assert dock.overview.topLevelItem(0).text(2) == "6 件" and "ベクター 1" in dock.overview_head.text()

    # A value: only its features on the map and in the attribute table.
    dock.click_value("学校")
    settle(dock)
    assert shown(layer) == ["地点4", "地点5"] and dock.figures["絞り込み後"] == 2 and dock.clear_button.isVisibleTo(dock)
    assert dock.overview.topLevelItem(0).text(2) == "2 / 6 件"
    # Ctrl: another value too.
    dock.click_value("病院", several=True)
    settle(dock)
    assert shown(layer) == ["地点4", "地点5", "地点6"]

    # 合計 of the number by value (each chart counts what the other filters leave: all values still listed).
    dock.stat_combo.setCurrentIndex(dock.stat_combo.findData("sum"))
    settle(dock)
    assert dict((c.value, c.figure) for c in dock.categories)["公園"] == 60

    # A range of the number (the filter of values stays).
    dock.click_bin(len(dock.hist.counts) - 1)
    settle(dock)
    assert shown(layer) == ["地点6"] and dock.range == (59.0, 60.0)
    dock.clear_filter()
    settle(dock)
    assert len(shown(layer)) == 6 and layer.subsetString() == ""

    # Select what is shown.
    dock.click_value("公園")
    dock.select_shown()
    settle(dock)
    assert dock.figures["選択中"] == 3 and layer.selectedFeatureCount() == 3

    # The heatmap follows the filter.
    dock.set_heatmap(True)
    settle(dock)
    assert wait_for(lambda: dock.heatmap_features() == 3, 3000)
    heat = dock.heat_layer
    assert heat.renderer().type() == "heatmapRenderer" and QgsProject.instance().layerTreeRoot().layerOrder()[0] is heat
    heat_id = heat.id()

    # In the view only.
    iface.canvas.setExtent(QgsRectangle(139.695, 35.6, 139.715, 35.76))
    dock.in_view_box.setChecked(True)
    settle(dock)
    assert dock.figures["表示範囲内"] < 6 and [c.value for c in dock.categories] == ["公園"]
    dock.in_view_box.setChecked(False)

    # Closing: everything again, the heatmap gone.
    dock.toggleViewAction().trigger()
    APP.processEvents()
    assert len(shown(layer)) == 6 and dock.heat_layer is None and QgsProject.instance().mapLayer(heat_id) is None


def test_the_time_chart_sets_the_temporal_controller(plugin):
    p, iface, layer, tmp_path = plugin
    props = layer.temporalProperties()
    props.setIsActive(True)
    props.setMode(QgsVectorLayerTemporalProperties.TemporalMode.ModeFeatureDateTimeInstantFromField)
    props.setStartField("observed")
    iface.setActiveLayer(layer)
    dock = p.api.dashboard
    dock.toggleViewAction().trigger()
    settle(dock)
    assert dock.bins is not None and sum(dock.bins.counts) == 6 and "期間内" not in dock.figures
    dock.click_value("公園")
    dock.click_step(0)
    settle(dock)
    assert iface.canvas.temporalRange().begin() == QDateTime.fromMSecsSinceEpoch(int(dock.bins.starts[0]))
    assert dock.figures["期間内"] == 1 and dock.figures["絞り込み後"] == 1
    # The time chart counts the features of every time (the dashboard's filter only).
    assert sum(dock.bins.counts) == 3


def test_the_project_keeps_the_layers_own_filter_and_the_dashboard(plugin):
    p, iface, layer, tmp_path = plugin
    layer.setSubsetString("\"value\" > 10")
    iface.setActiveLayer(layer)
    dock = p.api.dashboard
    dock.toggleViewAction().trigger()
    settle(dock)
    assert dock.figures["地物"] == 5
    dock.click_value("公園")
    dock.set_heatmap(True)
    settle(dock)
    assert shown(layer) == ["地点2", "地点3"] and layer.subsetString() == "(\"value\" > 10) AND (\"kind\" IN ('公園'))"
    path = str(tmp_path / "layout.qgs")
    assert QgsProject.instance().write(path)
    text = open(path, encoding="utf-8").read()
    assert "公園" in text.split("<SatObsDashboard>")[1] and "kind" not in text.split("<datasource>")[1].split("</datasource>")[0]
    state = dock.get_state()
    assert state["values"] == ["公園"] and state["heatmap"] and state["groupBy"] == "kind"
    # Closing restores the layer's own filter; opening the project again brings the dashboard back.
    dock.toggleViewAction().trigger()
    APP.processEvents()
    assert layer.subsetString() == "\"value\" > 10"
    iface.setActiveLayer(None)  # as QGIS does when the layer goes
    QgsProject.instance().clear()
    assert QgsProject.instance().read(path)
    APP.processEvents()
    APP.processEvents()
    [again] = [l for l in QgsProject.instance().mapLayers().values() if l.name() == "survey"]
    assert wait_for(lambda: dock.layer is again and dock.values == {"公園"}, 3000)
    settle(dock)
    assert dock.isVisible() and dock.heat_box.isChecked() and shown(again) == ["地点2", "地点3"]
    assert [l.name() for l in QgsProject.instance().mapLayers().values()].count("ヒートマップ（survey）") <= 1
