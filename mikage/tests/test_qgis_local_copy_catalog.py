"""
手元にコピーして見る (`localCopy`) as images are opened in the plugin: from
the catalog, closed and opened again, by their image id (画像 ID で開く, 開い
た画像の履歴; tsudo 2026-11-27: 「手元への画像コピーが失敗するときがあるので、検証
してほしい 多分、同じ画像を閉じて開き直したときの挙動」「画像IDからクイックに開くパター
ンも検証しておいて」), and when a copy fails (「リトライし続けているならそれもやめ
てほしい」).
"""
import os

import pytest

pytest.importorskip("qgis.core")

from qgis.testing import start_app  # noqa: E402

APP = start_app()

from qgis.core import QgsProject  # noqa: E402

from mikage.core.catalog.search import record_of  # noqa: E402
from mikage.core.local_copy import LocalCopyOptions  # noqa: E402
from mikage.features.images import Images  # noqa: E402
from mikage.features.observations import Observations  # noqa: E402
from mikage.features.rendering import Rendering  # noqa: E402
from mikage.qgis_io import local_copy_io, overviews_io  # noqa: E402
from mikage.qgis_io.obs_images import image_path  # noqa: E402
from mikage.tests.qgis_fakes import wait_for  # noqa: E402
from mikage.tests.test_qgis_catalog import plugin, searched  # noqa: E402,F401
from mikage.tests.test_qgis_observations import geotiff  # noqa: E402
from mikage.ui import open_again_dialog  # noqa: E402

#: The plugin as it opens images: the catalog, and 地図の描画 (Rendering: the copies of any image added).
PLUGIN_FEATURES = (Rendering, Images, Observations)
FILES = ["PAN/a_P.tif", "PAN/b_P.tif"]


@pytest.fixture
def copying(tmp_path, monkeypatch):
    """The images under tmp_path/nas taken as on a network folder, all copied (minMB 0); what was copied, and how it ended."""
    share = str(tmp_path / "nas")
    mine = LocalCopyOptions(min_mb=0, folder=str(tmp_path / "copies"))
    monkeypatch.setattr(local_copy_io, "network_place", lambda path: os.path.abspath(path).startswith(share))
    monkeypatch.setattr(local_copy_io, "configure", lambda options: setattr(local_copy_io, "current", mine))
    local_copy_io.current = mine
    local_copy_io.queue.cancel_all()
    local_copy_io.queue.forget()
    local_copy_io._copies.clear()
    made, ended = [], []
    real = local_copy_io.make_copy

    def counted(path, *a, **k):
        made.append(os.path.basename(path))
        return real(path, *a, **k)

    monkeypatch.setattr(local_copy_io, "make_copy", counted)
    local_copy_io.queue.finished = lambda outcome, error: ended.append(outcome.kind if outcome else error)
    yield made, ended
    local_copy_io.queue.finished = None
    local_copy_io.queue.cancel_all()
    local_copy_io.queue.forget()
    QgsProject.instance().removeAllMapLayers()
    local_copy_io.end_session()
    local_copy_io.current = LocalCopyOptions()
    local_copy_io._copies.clear()


def scene(folder, dock):
    """A folder record of 2 images on the share, its record."""
    place = folder / "2026" / "Scene_D"
    for rel in FILES:
        (place / rel).parent.mkdir(parents=True, exist_ok=True)
        geotiff(place / rel)
    record = record_of({"attributes": {"IMG_ID": "D", "SENSOR": "S1", "ACQ": 1_700_000_000_000,
                                       "PATH": "\\\\nas\\images\\2026\\scene_d"}}, dock._shown_catalog.fields)
    return place, record


def open_by_id(dock, monkeypatch):
    """画像 ID で開く of an image opened before (開いた画像の履歴: the files of last time, after one look)."""
    monkeypatch.setattr(open_again_dialog, "ask_open_again", lambda parent, entry: list(FILES))
    opened = []
    dock.open_image_id("D", then=opened.append)
    [layers] = opened
    return layers


def reads_copies(layers, place):
    return all(overviews_io.local_path(layer).startswith(local_copy_io.base_folder()) for layer in layers) and \
        sorted(os.path.normcase(image_path(layer)) for layer in layers) == \
        sorted(os.path.normcase(str(place / rel)) for rel in FILES)


def test_closed_once_copied_and_opened_again_by_its_id(copying, plugin, monkeypatch):
    made, ended = copying
    p, iface, folder, fake, _ = plugin
    dock = p.api.image_catalog
    searched(dock)
    place, record = scene(folder, dock)
    monkeypatch.setattr(dock, "choose_folder", lambda mapping: str(folder))
    monkeypatch.setattr(dock, "choose_images", lambda r, files, more: files)
    layers = dock.open_records([record])
    assert wait_for(lambda: len(ended) == 2, 30000), ended
    assert ended == ["copied", "copied"] and sorted(made) == ["a_P.tif", "b_P.tif"]
    assert reads_copies(layers, place)
    QgsProject.instance().removeAllMapLayers()
    # Opened again by its id: the copies read at once, nothing copied again.
    again = open_by_id(dock, monkeypatch)
    assert reads_copies(again, place)
    wait_for(lambda: False, 500)
    assert len(made) == 2 and local_copy_io.queue.pending() == []
    # The history still says where the image is (the share), not its copy.
    from mikage.qgis_io.catalog_io import opened_images

    assert os.path.normcase(opened_images().find("D").place) == os.path.normcase(str(place))
    # Closed and opened again from the catalog's list: the same.
    QgsProject.instance().removeAllMapLayers()
    assert reads_copies(dock.open_records([record]), place) and len(made) == 2


def test_closed_while_copying_and_opened_again_by_its_id(copying, plugin, monkeypatch):
    made, ended = copying
    p, iface, folder, fake, _ = plugin
    dock = p.api.image_catalog
    searched(dock)
    place, record = scene(folder, dock)
    monkeypatch.setattr(dock, "choose_folder", lambda mapping: str(folder))
    monkeypatch.setattr(dock, "choose_images", lambda r, files, more: files)
    dock.open_records([record])
    QgsProject.instance().removeAllMapLayers()
    again = open_by_id(dock, monkeypatch)
    assert wait_for(lambda: len(ended) == 2, 30000), ended
    # Each image copied once, and the layers opened again read the copies.
    assert sorted(made) == ["a_P.tif", "b_P.tif"] and reads_copies(again, place)


def test_a_copy_that_fails_is_not_tried_again(copying, plugin, monkeypatch):
    """Its pyramids, asked for again from the share, asked for its copy again, which failed again, without end."""
    made, ended = copying
    p, iface, folder, fake, _ = plugin
    dock = p.api.image_catalog
    searched(dock)
    place, record = scene(folder, dock)
    monkeypatch.setattr(dock, "choose_folder", lambda mapping: str(folder))
    monkeypatch.setattr(dock, "choose_images", lambda r, files, more: files)

    def fails(path, *a, **k):
        made.append(os.path.basename(path))
        raise local_copy_io.CopyError(detail="[WinError 32] プロセスはファイルにアクセスできません。別のプロセスが使用中です。")

    monkeypatch.setattr(local_copy_io, "make_copy", fails)
    said = []
    bar = iface.messageBar()
    monkeypatch.setattr(bar, "pushMessage", lambda title, text, *a, **k: said.append(text))
    layers = dock.open_records([record])
    assert wait_for(lambda: len(ended) == 2, 30000), ended
    wait_for(lambda: False, 1500)
    assert sorted(made) == ["a_P.tif", "b_P.tif"] and len(ended) == 2 and local_copy_io.queue.pending() == []
    # Read from the share, said once each.
    assert all(overviews_io.local_path(layer).startswith(str(place)) for layer in layers)
    failed = [text for text in said if "手元にコピーできない" in text]
    assert len(failed) == 2 and "もうコピーしない" in failed[0], said
    # Closed and opened again by its id: not tried again.
    QgsProject.instance().removeAllMapLayers()
    open_by_id(dock, monkeypatch)
    wait_for(lambda: False, 500)
    assert len(made) == 2
    # Replaced on the share: tried again.
    st = os.stat(place / FILES[0])
    os.utime(place / FILES[0], (st.st_atime, st.st_mtime + 60))
    QgsProject.instance().removeAllMapLayers()
    open_by_id(dock, monkeypatch)
    assert wait_for(lambda: len(made) == 3, 10000) and made[-1] == "a_P.tif"
