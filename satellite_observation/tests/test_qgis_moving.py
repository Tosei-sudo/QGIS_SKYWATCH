"""
「移動目標撮像計画」 in QGIS (the viewer's moving-target spec): a point layer
of reports, its attributes found by name, the predicted places as layers,
and the plan aimed at the circle of each pass's moment.
"""

import pytest

pytest.importorskip("qgis.core")
from qgis.testing import start_app  # noqa: E402

APP = start_app()

from qgis.core import QgsFeature, QgsGeometry, QgsPointXY, QgsProject, QgsVectorLayer  # noqa: E402
from qgis.PyQt.QtCore import QDateTime, QTimeZone  # noqa: E402

from satellite_observation.core.plan.moving import destination  # noqa: E402
from satellite_observation.tests.test_plan import BELOW, T  # noqa: E402
from satellite_observation.tests.test_qgis_imaging_plan import _iss_only, plugin, start_at  # noqa: E402,F401
from satellite_observation.tests.qgis_fakes import wait_for  # noqa: E402

H = 3600000


def reports_layer(rows):
    """AIS-like reports: mmsi, a date-time, sog (knots), cog and a name."""
    layer = QgsVectorLayer("Point?crs=EPSG:4326&field=MMSI:string&field=BaseDateTime:datetime&field=SOG:double&field=COG:double&field=VesselName:string",
                           "AIS", "memory")
    features = []
    for mmsi, ms, lon, lat, sog, cog, name in rows:
        f = QgsFeature(layer.fields())
        f.setGeometry(QgsGeometry.fromPointXY(QgsPointXY(lon, lat)))
        f.setAttributes([mmsi, QDateTime.fromMSecsSinceEpoch(int(ms), QTimeZone.utc()), sog, cog, name])
        features.append(f)
    layer.dataProvider().addFeatures(features)
    QgsProject.instance().addMapLayer(layer)
    return layer


def test_reads_reports_and_adds_the_predicted_places(plugin):
    p, iface, tmp = plugin
    layer = reports_layer([
        ("431000001", T - 6 * H, 139.0, 35.0, 10, 90, "MARU"),
        ("431000001", T - 5 * H, 139.2, 35.0, 10, 90, "MARU"),
        ("431000002", T - 5 * H, 140.0, 36.0, None, None, "STILL"),
    ])
    iface.active = layer
    dialog = p.api.moving_targets(layer)
    assert dialog.layer.currentData() == layer.id()
    assert {r: c.currentData() for r, c in dialog.fields.items()} == {"id": "MMSI", "time": "BaseDateTime", "course": "COG", "speed": "SOG", "name": "VesselName"}
    assert dialog.unit.currentData() == "kn"
    targets = dialog.targets()
    assert [t.name for t in targets] == ["MARU", "STILL"]
    assert targets[0].t == T - 5 * H and targets[0].course == 90
    # One report without a course or speed: it stays where it is.
    assert "目標 2 件" in dialog.note.text() and "1 件は針路・速力が不明" in dialog.note.text()
    dialog.step.setValue(24)
    circles, tracks = dialog.add_places()
    # 72 hours every 24: 0, 24, 48, 72 for each target.
    assert circles.featureCount() == 8
    assert tracks.featureCount() == 2
    assert circles.fields().names() == ["kind", "target_id", "name", "time", "hours", "radius_km", "course", "speed_kn"]
    assert circles.temporalProperties().isActive()
    radii = sorted(f["radius_km"] for f in circles.getFeatures() if f["name"] == "MARU")
    assert radii[0] == 0.5 and radii[-1] == pytest.approx(0.5 + 72 * (0.1 * 10 * 1.852 + 0.5), abs=0.01)
    dialog.fields["id"].setCurrentIndex(0)
    assert "ID になる属性" in dialog.note.text()


def test_plans_the_pass_at_where_the_ship_will_be(plugin):
    p, iface, tmp = plugin
    dock = _iss_only(plugin, tmp, swath=10)
    # A ship sailing north at 12 knots that will be 150 km east of the ISS track at T.
    import math

    meet = (BELOW[0] + 150 / (111.32 * math.cos(BELOW[1] * math.pi / 180)), BELOW[1])
    lon, lat = destination(meet, 180, 12 * 1.852 * 5)
    layer = reports_layer([("7", T - 5 * H, lon, lat, 12, 0, "SHIP")])
    start_at(dock, T - 3 * H, 1)
    dialog = p.api.moving_targets(layer)
    dialog.plan()
    assert dock.target.currentData() == "moving"
    assert "移動目標: AIS（1）" == dock.target.currentText()
    assert wait_for(lambda: dock.plan is not None and dock.plan_task is None, 60000), dock.status.text()
    ops = dock.plan["result"].opportunities
    near = [o for o in ops if abs(o.time - T) < 10 * 60000]
    assert len(near) == 1
    target = dock.plan["targets"][near[0].target]
    assert target.uncertainty is not None
    labels = [c.label for c in dock.model.columns]
    assert "位置の誤差" in labels
    assert "被覆" in labels
    # The layer and the CSV carry the error radius.
    assert "位置の誤差" in dock.csv_text().splitlines()[0]
    out = dock.add_layer()
    assert "uncertainty_km" in out.fields().names()
    # Choosing it draws the circle at the moment of the pass.
    dock.choose(near[0])
    assert dock.overlay.extent(["circle"]) is not None
