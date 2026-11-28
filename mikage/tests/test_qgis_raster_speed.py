"""
地図の描画を速くする設定 (qgis_io/raster_speed.py): GDAL's threads and block cache in this QGIS (and back), someone
else's values left alone, QGIS's slower options changed once with 「元に戻す」, the dialog's switch, the settings file's `rasterSpeed`.
"""
import json
import os

import pytest

pytest.importorskip("qgis.core")
from qgis.testing import start_app  # noqa: E402

APP = start_app()

from osgeo import gdal  # noqa: E402
from qgis.core import QgsApplication, QgsSettings  # noqa: E402
from qgis.PyQt.QtCore import QThread, QThreadPool  # noqa: E402
from qgis.PyQt.QtWidgets import QPushButton  # noqa: E402

from mikage.core.raster_speed import (  # noqa: E402
    DEFAULT_RASTER_SPEED, EARLY_RESAMPLING, MAX_THREADS, PARALLEL, QGIS_KEYS, RENDER_CACHE, ZOOMED_OUT, RasterSpeedOptions,
)
from mikage.qgis_io import raster_speed, settings  # noqa: E402
from mikage.qgis_io.raster_speed import CACHE, MB, THREADS  # noqa: E402
from mikage.tests.qgis_fakes import FakeIface, wait_for  # noqa: E402
from mikage.features.rendering import Rendering  # noqa: E402
from mikage.tests.feature_harness import small_plugin  # noqa: E402

CORES = raster_speed.cores()
needs_cores = pytest.mark.skipif(CORES < 3 or raster_speed.gdal_version() < 3060000, reason="コアが 3 つ以上・GDAL 3.6 以降")


@pytest.fixture(autouse=True)
def clean(monkeypatch):
    """Each test from a profile the plugin never ran in, QGIS's options at their defaults, GDAL as the test run has it."""
    monkeypatch.setattr(raster_speed, "ENABLED", True)
    monkeypatch.delenv(THREADS, raising=False)
    monkeypatch.delenv(CACHE, raising=False)
    cache, threads = gdal.GetCacheMax(), gdal.GetConfigOption(THREADS)
    s = QgsSettings()
    s.remove("mikage")
    for key in QGIS_KEYS:
        s.remove(key)
    yield
    raster_speed.revert_gdal()
    raster_speed._theirs.clear()
    gdal.SetCacheMax(cache)
    gdal.SetConfigOption(THREADS, threads)
    s.remove("mikage")
    for key in QGIS_KEYS:
        s.remove(key)
    QgsApplication.setMaxThreads(-1)


def start(config_path=None):
    if config_path:
        settings.set_config_source(config_path)
    iface = FakeIface()
    plugin = small_plugin(iface, Rendering)
    plugin.initGui()
    return plugin, iface


def messages(iface):
    return [w for w in iface.messageBar().items()]


@needs_cores
def test_gdal_is_set_in_this_qgis_only_and_put_back():
    before = gdal.GetCacheMax()
    raster_speed.apply_gdal(DEFAULT_RASTER_SPEED)
    assert gdal.GetConfigOption(THREADS) == str(CORES - 1)
    assert os.environ.get(THREADS) is None  # nothing in the environment (processes started from QGIS keep theirs)
    ram = raster_speed.ram_mb()
    expected = min(ram // 10, 2048)
    assert gdal.GetCacheMax() == (expected * MB if expected > before // MB else before)
    # Read again with other values: changed, still put back to what GDAL had first.
    raster_speed.apply_gdal(RasterSpeedOptions(gdal_threads=2, gdal_cache_mb=300))
    assert gdal.GetConfigOption(THREADS) == "2" and gdal.GetCacheMax() == 300 * MB
    assert raster_speed.gdal_set() == {THREADS: "2", CACHE: 300, "before_" + CACHE: before // MB}
    raster_speed.revert_gdal()
    assert gdal.GetConfigOption(THREADS) is None and gdal.GetCacheMax() == before
    # Off (the settings file or the person): nothing set.
    raster_speed.apply_gdal(RasterSpeedOptions(enabled=False))
    assert gdal.GetConfigOption(THREADS) is None and gdal.GetCacheMax() == before


@needs_cores
def test_values_someone_else_set_are_left(monkeypatch):
    monkeypatch.setenv(THREADS, "2")
    monkeypatch.setenv(CACHE, "256")
    before = gdal.GetCacheMax()
    raster_speed.apply_gdal(DEFAULT_RASTER_SPEED)
    assert gdal.GetConfigOption(THREADS) == "2" and gdal.GetCacheMax() == before
    assert raster_speed.gdal_set() == {}
    rows = {row.what: row for row in raster_speed.rows()}
    assert rows["GDAL の読み込みのスレッド（GDAL_NUM_THREADS）"].state == "環境変数などの値のまま"
    assert rows["GDAL のブロックキャッシュ（GDAL_CACHEMAX）"].state == "環境変数などの値のまま"


def test_qgis_options_slower_than_its_defaults_are_changed_once_and_can_be_put_back():
    s = QgsSettings()
    s.setValue(PARALLEL, False)
    s.setValue(RENDER_CACHE, False)
    s.setValue(ZOOMED_OUT, "bilinear")
    plugin, iface = start()
    canvas = iface.mapCanvas()
    wait_for(lambda: any("速く描くため QGIS のオプション" in w.text() for w in messages(iface)), ms=5000)
    item = next(w for w in messages(iface) if "速く描くため QGIS のオプション" in w.text())
    assert "3 件" in item.text() and "並列描画・描画キャッシュ・早期リサンプリング" in item.text()
    assert "複数の CPU コアで並列にレイヤーを描画: オフ → オン" in item.toolTip()
    # In QGIS's options and on the map canvas right away; early resampling for the layers added from now on.
    assert raster_speed.as_bool(s.value(PARALLEL), False) and raster_speed.as_bool(s.value(RENDER_CACHE), False)
    assert raster_speed.as_bool(s.value(EARLY_RESAMPLING), False)
    assert canvas.isParallelRenderingEnabled() and canvas.isCachingEnabled()
    assert [c.key for c in raster_speed.qgis_changed()] == [PARALLEL, RENDER_CACHE, EARLY_RESAMPLING]
    # 「元に戻す」: as they were (a key never set before is removed again), on the canvas too.
    undo = next(b for b in item.findChildren(QPushButton) if b.text() == "元に戻す")
    undo.click()
    assert not raster_speed.as_bool(s.value(PARALLEL), True) and not raster_speed.as_bool(s.value(RENDER_CACHE), True)
    assert s.value(EARLY_RESAMPLING) is None
    assert not canvas.isParallelRenderingEnabled() and not canvas.isCachingEnabled()
    assert any("QGIS のオプション 3 件を復元" in w.text() for w in messages(iface))
    plugin.unload()
    # Not again at the next start: the person chose them so.
    plugin, iface = start()
    APP.processEvents()
    assert not any("速く描くため QGIS のオプション" in w.text() for w in messages(iface))
    assert not raster_speed.as_bool(s.value(PARALLEL), True)
    plugin.unload()


def test_a_value_changed_again_since_is_not_put_back():
    s = QgsSettings()
    s.setValue(MAX_THREADS, 1)
    changes = raster_speed.apply_qgis()
    if CORES < 2:
        assert changes == []
        return
    assert [(c.key, c.after) for c in changes] == [(MAX_THREADS, -1)]
    # All the cores again for QGIS's own threads (-1: as many as Qt says the PC has).
    assert QgsApplication.maxThreads() == -1 and QThreadPool.globalInstance().maxThreadCount() == QThread.idealThreadCount()
    s.setValue(MAX_THREADS, 2)  # the person, in QGIS's options
    assert raster_speed.revert_qgis() == []
    assert raster_speed.as_int(s.value(MAX_THREADS), 0) == 2


def test_the_dialog_turns_it_off_and_on(monkeypatch):
    s = QgsSettings()
    s.setValue(PARALLEL, False)
    plugin, iface = start()
    wait_for(lambda: raster_speed.qgis_changed() != [], ms=5000)
    dialog = plugin.open_raster_speed()
    assert dialog.windowTitle() == "地図の描画の速さ" and dialog.use.isChecked() and dialog.use.isEnabled()
    assert dialog.table.rowCount() == 6
    texts = [dialog.table.item(r, 2).text() for r in range(dialog.table.rowCount())]
    assert "MIKAGE が変更（元: オフ）" in texts
    assert "ウイルス対策" in dialog.manual.toPlainText() and "COG" in dialog.manual.toPlainText()
    before_cache = raster_speed.gdal_set().get("before_" + CACHE)
    status = dialog.set_use(False)
    assert "を復元" in status and raster_speed.personal_off()
    assert not raster_speed.as_bool(s.value(PARALLEL), True) and not iface.mapCanvas().isParallelRenderingEnabled()
    assert raster_speed.gdal_set() == {} and gdal.GetConfigOption(THREADS) is None
    if before_cache is not None:
        assert gdal.GetCacheMax() // MB == before_cache
    plugin.unload()
    # Off stays off at the next start (GDAL included); on again sets everything again.
    plugin, iface = start()
    APP.processEvents()
    assert raster_speed.gdal_set() == {} and not raster_speed.as_bool(s.value(PARALLEL), True)
    dialog = plugin.open_raster_speed()
    assert not dialog.use.isChecked()
    status = dialog.set_use(True)
    assert "1 件" in status and raster_speed.as_bool(s.value(PARALLEL), False) and not raster_speed.personal_off()
    if CORES >= 3 and raster_speed.gdal_version() >= 3060000:
        assert gdal.GetConfigOption(THREADS) == str(CORES - 1)
    # The settings dialog's row says what is in use and opens the same window.
    from mikage.ui.settings_dialog import SettingsDialog

    opened = []
    page = SettingsDialog(plugin.context, iface.mainWindow(), raster_speed=lambda parent: opened.append(parent))
    assert "QGIS の設定の変更 1 件" in page.raster.text()
    page.raster_more.click()
    assert opened == [page]
    page.deleteLater()
    plugin.unload()
    assert raster_speed.gdal_set() == {}  # unloaded: GDAL as it was


def test_the_settings_file_can_stop_it(tmp_path):
    s = QgsSettings()
    s.setValue(PARALLEL, False)
    path = tmp_path / "config.json"
    path.write_text(json.dumps({"rasterSpeed": False}), encoding="utf-8")
    # Set by the last read's options (the defaults) as the plugin loads, then the file says no: back as it was.
    plugin, iface = start(str(path))
    APP.processEvents()
    assert raster_speed.gdal_set() == {} and gdal.GetConfigOption(THREADS) is None
    assert not raster_speed.as_bool(s.value(PARALLEL), True) and raster_speed.qgis_changed() == []
    assert raster_speed.last_options() == RasterSpeedOptions(enabled=False)  # for the next start, before the file is read
    dialog = plugin.open_raster_speed()
    assert not dialog.use.isEnabled() and "組織の設定" in dialog.use.toolTip()
    plugin.unload()
    # Only QGIS's options left alone.
    path.write_text(json.dumps({"rasterSpeed": {"qgisSettings": False}}), encoding="utf-8")
    plugin, iface = start(str(path))
    APP.processEvents()
    assert not raster_speed.as_bool(s.value(PARALLEL), True)
    if CORES >= 3 and raster_speed.gdal_version() >= 3060000:
        assert gdal.GetConfigOption(THREADS) == str(CORES - 1)
    plugin.unload()
