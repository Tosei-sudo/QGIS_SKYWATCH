"""
The 「衛星パス」 dock in QGIS (the viewer's satellite-track spec): the
imaging plan's satellites, a moment and a period, the list, the colours, the
layers, pasted TLEs.
"""

import pytest

pytest.importorskip("qgis.core")
from qgis.testing import start_app  # noqa: E402

APP = start_app()

from qgis.core import QgsProject  # noqa: E402

from satellite_observation.qgis_io import settings  # noqa: E402
from satellite_observation.tests.qgis_fakes import wait_for  # noqa: E402
from satellite_observation.tests.test_plan import EPOCH, LINE1, LINE2  # noqa: E402
from satellite_observation.tests.test_qgis_imaging_plan import plugin  # noqa: E402,F401


def drawn(dock, ms=30000):
    dock.draw()
    assert wait_for(lambda: dock.task is None, ms), dock.status.text()


def test_the_samples_at_a_moment_and_over_a_period(plugin):
    p, iface, tmp = plugin
    settings.set_config_source("")
    p.context.reload()
    dock = p.api.satellite_pass
    dock.show()
    # Two sample satellites (each with its modes): both chosen, each in its own colour.
    assert wait_for(lambda: len([l for l in dock.tree.items() if l.checked]) == 2)
    assert len(dock.colors) == 2 and len(set(dock.colors.values())) == 2
    dock.set_time(at=EPOCH + 3600000)
    assert wait_for(lambda: dock.task is None and dock.tracks), dock.status.text()
    assert "2 機の軌跡と撮像可能範囲" in dock.status.text() and "前後 15 分" in dock.status.text()
    assert dock.model.rowCount() == 2
    row = [dock.model.item(0, c).text() for c in range(5)]
    assert row[0] == "サンプル光学衛星" and row[2].endswith(" km") and row[3] in ("北行", "南行") and row[4] == "20° 以下・左右"
    sar = [dock.model.item(1, c).text() for c in range(5)]
    assert sar[4] == "20〜45°・右"
    assert dock.overlay.extent() is not None
    dock.zoom_to(1)
    # Over a period: up to a week.
    dock.set_time(start=EPOCH, end=EPOCH + 10 * 86400000)
    assert wait_for(lambda: dock.task is None, 60000)
    assert "最長 7 日" in dock.status.text()
    assert dock.model.headerData(1, 1) == "開始時の位置"
    # Daylight: the optical satellite's reach only in the day.
    dock.set_time(start=EPOCH, end=EPOCH + 200 * 60000)
    assert wait_for(lambda: dock.task is None)
    night = sum(len(b.inner) for b in dock.tracks[0][0].bands)
    dock.daylight.setCurrentIndex(1)
    drawn(dock)
    assert sum(len(b.inner) for b in dock.tracks[0][0].bands) < night
    areas, lines = dock.add_layer()
    assert lines.featureCount() == 2 and areas.featureCount() == 2
    assert sorted(f["kind"] for f in lines.getFeatures()) == ["地上軌跡", "地上軌跡"]
    # Coloured per satellite, as drawn.
    assert sorted(c.value() for c in areas.renderer().categories()) == ["サンプルSAR衛星（ストリップマップ）", "サンプル光学衛星"]
    # Nothing chosen.
    dock.tree.check_all(False)
    drawn(dock)
    assert "表示する衛星を選択" in dock.status.text() and dock.model.rowCount() == 0
    assert not dock.layer_button.isEnabled()
    dock.hide()
    assert dock.overlay.extent() is None


def test_the_catalog_and_pasted_tles(plugin):
    p, iface, tmp = plugin
    dock = p.api.satellite_pass
    dock.show()
    # The imaging plan's catalog (two satellites with TLEs): as few, both start chosen.
    assert wait_for(lambda: len(dock.tree.items()) == 2, 30000), dock.status.text()
    assert all(l.checked for l in dock.tree.items())
    # A pasted satellite is added chosen; the others keep their choice.
    dock.tree.check_all(False)
    dock.tle.setPlainText(f"ISS\n{LINE1}\n{LINE2}")
    assert [l.sat.name for l in dock.tree.items() if l.checked] == ["ISS"]
    dock.set_time(at=EPOCH + 3600000)
    assert wait_for(lambda: dock.task is None and dock.tracks)
    assert [t.sat.name for t, _ in dock.tracks] == ["ISS"]
    assert QgsProject.instance().count() == 0
