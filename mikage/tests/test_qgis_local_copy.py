"""
手元にコピーして見る (`localCopy`, qgis_io/local_copy_io.py; tsudo
2026-11-27): a large image on a network folder is copied to this PC in the
background and its layers then read the copy, keeping their style; the
image on the share is only read, its pyramids are built from the copy (and
put beside the image on the share too), and a project saved names the image.
"""
import hashlib
import os
import time

import numpy as np
import pytest

pytest.importorskip("qgis.core")
from qgis.testing import start_app  # noqa: E402

APP = start_app()

from osgeo import gdal, osr  # noqa: E402
from qgis.core import QgsMultiBandColorRenderer, QgsProject, QgsRasterLayer  # noqa: E402

from mikage.core.local_copy import META, LocalCopyOptions, copy_name  # noqa: E402
from mikage.core.overviews import OverviewOptions  # noqa: E402
from mikage.qgis_io import local_copy_io, overviews_io  # noqa: E402
from mikage.qgis_io.local_copy_io import COPIED, NO_ROOM, ORIGINAL, CopyQueue, make_copy  # noqa: E402
from mikage.qgis_io.tasks import Cancelled  # noqa: E402
from mikage.tests.qgis_fakes import wait_for  # noqa: E402


def image(path, size=(1200, 900), bands=3, tiled=True):
    os.makedirs(os.path.dirname(str(path)), exist_ok=True)
    ds = gdal.GetDriverByName("GTiff").Create(str(path), size[0], size[1], bands, gdal.GDT_UInt16,
                                               ["TILED=YES"] if tiled else [])
    ds.SetGeoTransform([139.6, 0.0001, 0, 35.7, 0, -0.0001])
    srs = osr.SpatialReference()
    srs.ImportFromEPSG(4326)
    ds.SetProjection(srs.ExportToWkt())
    for b in range(bands):
        ramp = (np.arange(size[0], dtype=np.uint16)[None, :] + 50 * b).repeat(size[1], 0)
        ds.GetRasterBand(b + 1).WriteArray(ramp)
    ds = None
    return str(path)


def digest(path):
    return hashlib.sha256(open(path, "rb").read()).hexdigest()


@pytest.fixture
def share(tmp_path, monkeypatch):
    """A folder taken as a network folder, and the copies in another (every image copied: minMB 0)."""
    folder = tmp_path / "share"
    folder.mkdir()
    monkeypatch.setattr(local_copy_io, "network_place", lambda path: os.path.abspath(path).startswith(str(folder)))
    monkeypatch.setattr(overviews_io, "cache_folder", lambda: None)
    options = LocalCopyOptions(min_mb=0, folder=str(tmp_path / "copies"))
    local_copy_io.current = options
    overviews_io.current = OverviewOptions()
    local_copy_io._copies.clear()
    local_copy_io._places.clear()
    # (an earlier test's window, deleted, was told the copy's end: 「結果の表示先が閉じられていた」 and nothing else)
    local_copy_io.queue.cancel_all()
    overviews_io.queue.cancel_all()
    yield folder
    QgsProject.instance().removeAllMapLayers()
    local_copy_io.queue.cancel_all()
    overviews_io.queue.finished = None
    local_copy_io.queue.finished = None
    local_copy_io.end_session()
    local_copy_io.current = LocalCopyOptions()
    local_copy_io._copies.clear()
    local_copy_io._places.clear()


def beside(folder):
    """The files in a folder of the share but the .aux.xml QGIS itself writes (the statistics it computed on opening)."""
    return sorted(f for f in os.listdir(folder) if not f.endswith(".aux.xml"))


def copy_folder(path):
    return os.path.join(local_copy_io.base_folder(), copy_name(path))


def test_copied_then_read_here_keeping_the_style(share):
    path = image(share / "scene" / "R1C1.TIF")
    with open(path[:-4] + ".RPB", "w") as f:
        f.write("x")
    before = digest(path)
    layer = QgsRasterLayer(path, "R1C1", "gdal")
    renderer = layer.renderer().clone()
    assert isinstance(renderer, QgsMultiBandColorRenderer)
    renderer.setRedBand(3)
    renderer.setBlueBand(1)
    layer.setRenderer(renderer)
    layer.setCustomProperty("mikage/imageId", "SCENE_1")
    QgsProject.instance().addMapLayer(layer)
    q = CopyQueue()
    ended = []
    q.finished = lambda outcome, error: ended.append((outcome, error))
    assert q.request([layer]) == {local_copy_io.place_key(path)}
    # Asked again while copying: not twice.
    assert q.request([layer]) == {local_copy_io.place_key(path)} and q.pending() == [path]
    assert wait_for(lambda: ended, 30000)
    outcome, error = ended[0]
    assert error is None and outcome.kind == COPIED
    folder = copy_folder(path)
    assert sorted(os.listdir(folder)) == ["R1C1.RPB", "R1C1.TIF", META]
    assert outcome.copy == os.path.join(folder, "R1C1.TIF")
    # The layer reads the copy, as it was shown; the image is remembered.
    assert overviews_io.local_path(layer) == outcome.copy and layer.isValid()
    assert layer.name() == "R1C1" and layer.customProperty("mikage/imageId") == "SCENE_1"
    assert layer.customProperty(ORIGINAL) == path
    assert (layer.renderer().redBand(), layer.renderer().blueBand()) == (3, 1)
    from mikage.qgis_io.obs_images import image_path

    assert image_path(layer) == path
    assert local_copy_io.original_of(outcome.copy) == path and local_copy_io.original_of(path) is None
    # (QGIS itself writes the statistics it computed on opening into the .aux.xml beside the image.)
    assert digest(path) == before and set(os.listdir(share / "scene")) - {"R1C1.TIF.aux.xml"} == {"R1C1.RPB", "R1C1.TIF"}
    assert q.pending() == []
    # A layer of the image added later reads the copy at once.
    again = QgsRasterLayer(path, "again", "gdal")
    QgsProject.instance().addMapLayer(again)
    assert q.request([again]) == set() and overviews_io.local_path(again) == outcome.copy


def test_pyramids_wait_for_the_copy_and_go_beside_the_image_on_the_share_too(share):
    path = image(share / "img.tif", size=(3000, 2000))
    layer = QgsRasterLayer(path, "img", "gdal")
    QgsProject.instance().addMapLayer(layer)
    built = []
    overviews_io.queue.finished = lambda outcome, error: built.append((outcome, error))
    # As the catalog asks for them: the copy first, then its pyramids from it.
    assert overviews_io.queue.request([layer], OverviewOptions()) == []
    assert wait_for(lambda: built, 60000)
    outcome, error = built[0]
    assert error is None and outcome.kind == overviews_io.BUILT and outcome.shared
    copy = os.path.join(copy_folder(path), "img.tif")
    assert outcome.path == copy and os.path.isfile(copy + ".ovr")
    assert os.path.isfile(path + ".ovr") and beside(share) == ["img.tif", "img.tif.ovr"]
    assert layer.dataProvider().hasPyramids()
    ds = gdal.Open(path)
    assert ds.GetRasterBand(1).GetOverviewCount() == 4


def test_pyramids_only_beside_the_copy_where_they_would_not_have_gone_beside_the_image(share):
    path = image(share / "img.tif", size=(3000, 2000))
    layer = QgsRasterLayer(path, "img", "gdal")
    QgsProject.instance().addMapLayer(layer)
    built = []
    overviews_io.queue.finished = lambda outcome, error: built.append((outcome, error))
    options = OverviewOptions(location="temp")
    overviews_io.current = options
    try:
        overviews_io.queue.request([layer], options)
        assert wait_for(lambda: built, 60000)
    finally:
        overviews_io.current = OverviewOptions()
        overviews_io.end_session()
    outcome, error = built[0]
    assert outcome.kind == overviews_io.BUILT and not outcome.shared
    assert os.path.isfile(outcome.path + ".ovr") and beside(share) == ["img.tif"]


def test_no_pyramids_after_the_copy_unless_asked(share):
    path = image(share / "img.tif", size=(3000, 2000))
    layer = QgsRasterLayer(path, "img", "gdal")
    QgsProject.instance().addMapLayer(layer)
    q = CopyQueue()
    ended = []
    q.finished = lambda outcome, error: ended.append(outcome)
    asked = []
    original = overviews_io.queue.request
    overviews_io.queue.request = lambda layers, options, bar=None: asked.append(layers) or []
    try:
        q.request([layer])
        assert wait_for(lambda: ended, 30000)
    finally:
        overviews_io.queue.request = original
    assert ended[0].kind == COPIED and asked == []


def test_a_project_saved_names_the_image_on_the_share(share, tmp_path):
    path = image(share / "img.tif")
    layer = QgsRasterLayer(path, "img", "gdal")
    project = QgsProject.instance()
    project.addMapLayer(layer)
    assert make_copy(path, local_copy_io.current).kind == COPIED
    copy = local_copy_io.copy_of(path)
    assert local_copy_io.switch_layers(path, copy) == [layer]
    seen = []

    def tree(doc):
        seen.append(local_copy_io.project_written(doc))

    def written(layer, element, doc):
        seen.append(local_copy_io.layer_written(layer, element, doc))

    project.writeProject.connect(tree)
    project.writeMapLayer.connect(written)
    target = str(tmp_path / "p.qgs")
    try:
        assert project.write(target)
    finally:
        project.writeProject.disconnect(tree)
        project.writeMapLayer.disconnect(written)
    text = open(target, encoding="utf-8").read()
    assert seen == [1, True]
    assert "copies" not in text and text.count("./share/img.tif") == 2
    # The layer itself still reads the copy.
    assert overviews_io.local_path(layer) == copy


def test_a_copy_kept_is_used_again_unless_the_image_changed(share, tmp_path):
    local_copy_io.current = LocalCopyOptions(min_mb=0, location="cache", folder=str(tmp_path / "kept"))
    path = image(share / "img.tif")
    assert local_copy_io.copy_of(path) is None
    made = make_copy(path, local_copy_io.current)
    assert made.kind == COPIED and local_copy_io.base_folder() == str(tmp_path / "kept")
    assert local_copy_io.copy_of(path) == made.copy
    # The next QGIS: nothing remembered but the folder.
    local_copy_io._copies.clear()
    assert local_copy_io.original_of(made.copy) == path
    assert local_copy_io.copy_of(path) == made.copy
    # Replaced on the share: copied again.
    st = os.stat(path)
    os.utime(path, (st.st_atime, st.st_mtime + 60))
    assert local_copy_io.copy_of(path) is None
    # Kept when QGIS closes.
    assert local_copy_io.end_session() == 0 and os.path.isfile(made.copy)


def test_room_made_from_copies_not_shown(share, tmp_path):
    a, b, c = (image(share / f"{n}.tif", size=(1000, 1000), tiled=False) for n in "abc")
    size = os.path.getsize(a)
    local_copy_io.current = LocalCopyOptions(min_mb=0, folder=str(tmp_path / "c"), max_gb=2.5 * size / 1024 ** 3)
    first = make_copy(a, local_copy_io.current)
    time.sleep(0.01)
    second = make_copy(b, local_copy_io.current)
    # c needs room: a (least recently used) goes, unless it is shown.
    busy = {local_copy_io.place_key(os.path.dirname(first.copy))}
    third = make_copy(c, local_copy_io.current, busy)
    assert third.kind == COPIED and os.path.isfile(first.copy) and not os.path.exists(second.copy)
    # No room even then: read from the share.
    local_copy_io.current = LocalCopyOptions(min_mb=0, folder=str(tmp_path / "c"), max_gb=0.5 * size / 1024 ** 3)
    assert make_copy(b, local_copy_io.current).kind == NO_ROOM
    assert {i.source for i in local_copy_io.copies()} == {a, c}


def test_cancelled_leaves_nothing(share):
    path = image(share / "img.tif")
    with pytest.raises(Cancelled):
        make_copy(path, local_copy_io.current, cancelled=lambda: True)
    assert not os.path.exists(copy_folder(path)) and local_copy_io.copy_of(path) is None


def test_which_images_are_copied(share, tmp_path):
    big = image(share / "big.tif")
    here = image(tmp_path / "here" / "img.tif")
    assert local_copy_io.wanted(big) and not local_copy_io.wanted(here)
    assert not local_copy_io.wanted("/vsicurl/https://example.com/a.tif") and not local_copy_io.wanted("https://x/a.tif")
    # Smaller than minMB.
    assert not local_copy_io.wanted(big, LocalCopyOptions(min_mb=100))
    assert not local_copy_io.wanted(big, LocalCopyOptions(enabled=False, min_mb=0))
    # A rewrite's `localCopy`: wherever they are, or never.
    local_copy_io.mark_place(str(tmp_path / "here"), True)
    local_copy_io.mark_place(str(share / "big.tif"), False)
    assert local_copy_io.wanted(here) and not local_copy_io.wanted(big)
    # A copy is never copied again.
    made = make_copy(here, local_copy_io.current)
    assert not local_copy_io.wanted(made.copy)


def test_the_temporary_copies_go_when_qgis_closes(share, tmp_path):
    path = image(share / "img.tif")
    made = make_copy(path, local_copy_io.current)
    folder = local_copy_io.base_folder()
    assert os.path.basename(folder).startswith("mikage-") and os.path.dirname(folder) == str(tmp_path / "copies")
    assert local_copy_io.end_session() == 0
    assert not os.path.exists(made.copy) and not os.path.exists(folder)
    assert local_copy_io.original_of(made.copy) is None


def test_the_catalog_opens_a_copy_made_before(share):
    from mikage.qgis_io import catalog_io

    path = image(share / "img.tif")
    assert catalog_io._copy_of(path) is None
    made = make_copy(path, local_copy_io.current)
    assert catalog_io._copy_of(path) == made.copy


def test_settings_dialog_shows_the_copies(share):
    from mikage.qgis_io.context import PluginContext
    from mikage.ui.settings_dialog import SettingsDialog

    path = image(share / "img.tif")
    made = make_copy(path, local_copy_io.current)
    layer = QgsRasterLayer(path, "img", "gdal")
    other = image(share / "other.tif")
    unused = make_copy(other, local_copy_io.current)
    QgsProject.instance().addMapLayer(layer)
    local_copy_io.switch_layers(path, made.copy)
    dialog = SettingsDialog(PluginContext())
    try:
        assert dialog.copies.text().startswith(f"{local_copy_io.base_folder()}（2 件、0.0 GB。上限 30 GB、QGIS を閉じると消去）")
        assert dialog.clear_copies.isEnabled()
        dialog.clear_copies.click()
        # The one shown stays.
        assert os.path.isfile(made.copy) and not os.path.exists(unused.copy)
        assert "（1 件、" in dialog.copies.text()
        local_copy_io.current = LocalCopyOptions(enabled=False)
        dialog._show_copies()
        assert dialog.copies.text() == "使わない（組織の設定）" and not dialog.clear_copies.isEnabled()
    finally:
        dialog.reject()


# ---- Windows: files in use, an antivirus reading the file just written (tsudo 2026-11-27) ----

@pytest.fixture
def windows(monkeypatch):
    """
    Files "open" (`locked`): as Windows, they cannot be removed nor replaced,
    and a rename onto or from them is refused (PermissionError, WinError 32);
    `refusals` more renames of any file are refused first (an antivirus).
    """
    locked, refusals, waits = set(), [0], []
    remove, replace = os.remove, os.replace

    def refused(path):
        return PermissionError(13, "プロセスはファイルにアクセスできません。別のプロセスが使用中です。", path)

    def fake_remove(path, *a, **k):
        if os.path.abspath(path) in locked:
            raise refused(path)
        return remove(path, *a, **k)

    def fake_replace(src, dst, *a, **k):
        if os.path.abspath(src) in locked or os.path.abspath(dst) in locked:
            raise refused(dst)
        if refusals[0] > 0:
            refusals[0] -= 1
            raise refused(dst)
        return replace(src, dst, *a, **k)

    monkeypatch.setattr(os, "remove", fake_remove)
    monkeypatch.setattr(os, "replace", fake_replace)
    monkeypatch.setattr(local_copy_io.time, "sleep", lambda s: waits.append(s))
    return locked, refusals, waits


def test_a_rename_refused_for_a_while_is_tried_again(share, windows):
    locked, refusals, waits = windows
    path = image(share / "img.tif")
    refusals[0] = 3
    made = make_copy(path, local_copy_io.current)
    assert made.kind == COPIED and os.path.isfile(made.copy) and waits == list(local_copy_io.REPLACE_WAITS[:3])
    # Refused for good (more than about 10 s): not copied, nothing left.
    refusals[0] = 100
    other = image(share / "other.tif")
    with pytest.raises(local_copy_io.CopyError):
        make_copy(other, local_copy_io.current)
    assert not os.path.exists(copy_folder(other))


def test_a_new_copy_beside_an_older_one_still_open(share, windows):
    locked, refusals, waits = windows
    path = image(share / "img.tif")
    first = make_copy(path, local_copy_io.current)
    # Its copy still open (a layer closed, its GDAL dataset not), and the image replaced on the share since.
    locked.add(os.path.abspath(first.copy))
    st = os.stat(path)
    os.utime(path, (st.st_atime, st.st_mtime + 60))
    assert local_copy_io.copy_of(path) is None
    second = make_copy(path, local_copy_io.current)
    assert second.kind == COPIED and os.path.dirname(second.copy) == copy_folder(path) + "-2"
    assert local_copy_io.copy_of(path) == second.copy and local_copy_io.original_of(second.copy) == path
    # The older one, partly deleted, is never taken as whole; deleted once it is let go.
    locked.clear()
    assert local_copy_io.drop(os.path.dirname(first.copy)) and local_copy_io.copy_of(path) == second.copy


def test_a_copy_partly_deleted_keeps_its_meta_last(share, windows):
    locked, refusals, waits = windows
    path = image(share / "img.tif")
    with open(path[:-4] + ".RPB", "w") as f:
        f.write("x")
    made = make_copy(path, local_copy_io.current)
    folder = os.path.dirname(made.copy)
    locked.add(os.path.abspath(made.copy))
    assert not local_copy_io.drop(folder)
    # The .RPB went, the image (open) and META stay: not a whole copy any more, so not used.
    assert sorted(os.listdir(folder)) == ["img.tif", META]
    assert local_copy_io.copy_of(path) is None


def test_the_copies_shown_stay_whole_when_the_plugin_stops(share):
    """The plugin reloaded or updated: a layer still reading a copy goes on knowing it is one."""
    path, other = image(share / "img.tif"), image(share / "other.tif")
    shown, unused = make_copy(path, local_copy_io.current), make_copy(other, local_copy_io.current)
    layer = QgsRasterLayer(path, "img", "gdal")
    QgsProject.instance().addMapLayer(layer)
    local_copy_io.switch_layers(path, shown.copy)
    assert local_copy_io.end_session() == 1
    assert os.path.isfile(shown.copy) and os.path.isfile(os.path.join(os.path.dirname(shown.copy), META))
    assert not os.path.exists(os.path.dirname(unused.copy))
    # Known again from its META (the plugin's memory is gone).
    assert local_copy_io.original_of(shown.copy) == path
    from mikage.qgis_io.obs_images import image_path

    assert image_path(layer) == path
    QgsProject.instance().removeAllMapLayers()
    assert local_copy_io.end_session() == 0 and not os.path.exists(local_copy_io.base_folder())


def test_no_room_is_tried_again_once_room_is_made(share, monkeypatch):
    path = image(share / "img.tif")
    layer = QgsRasterLayer(path, "img", "gdal")
    QgsProject.instance().addMapLayer(layer)
    tried = []
    monkeypatch.setattr(local_copy_io, "make_copy", lambda p, *a, **k: tried.append(p) or local_copy_io.Outcome(NO_ROOM, p, bytes=1))
    q = local_copy_io.queue
    ended = []
    q.finished = lambda outcome, error: ended.append(outcome.kind)
    q.request([layer], None, OverviewOptions())
    assert wait_for(lambda: ended, 10000)
    wait_for(lambda: False, 300)
    assert tried == [path] and q.given_up(path) == NO_ROOM
    q.request([layer])
    assert tried == [path] and q.pending() == []
    # 「使っていないコピーを消去」: tried again.
    local_copy_io.clear_unused()
    q.request([layer])
    assert wait_for(lambda: len(tried) == 2, 10000)


def test_a_copy_cancelled_is_not_started_again(share, monkeypatch):
    path = image(share / "img.tif", size=(3000, 2000))
    layer = QgsRasterLayer(path, "img", "gdal")
    QgsProject.instance().addMapLayer(layer)
    tried = []

    def slow(p, options, busy, progress, cancelled):
        tried.append(p)
        while not cancelled():
            time.sleep(0.01)
        raise Cancelled()

    monkeypatch.setattr(local_copy_io, "make_copy", slow)
    built = []
    overviews_io.queue.finished = lambda outcome, error: built.append(outcome.kind if outcome else error)
    q = local_copy_io.queue
    assert overviews_io.queue.request([layer], OverviewOptions()) == []
    assert wait_for(lambda: tried, 10000)
    # Cancelled in QGIS's task list: read from the share, its pyramids from there; not copied again.
    q._task.cancel()
    assert wait_for(lambda: built, 60000) and built == [overviews_io.BUILT]
    assert tried == [path] and q.given_up(path) == "取消" and overviews_io.local_path(layer) == path
