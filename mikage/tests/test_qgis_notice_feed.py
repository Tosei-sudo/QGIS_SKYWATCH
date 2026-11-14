"""
新着画像（大きな画面） (tsudo, 2026-11-14): the 通知 dock's notices as cards in
QGIS's map area, switched with the map by tabs over it; the newest at the
bottom, the page never moving by itself, thumbnails from the catalog's
`thumbnail` pattern and 「写っている地物」 from its `footprintLayers`.
"""
import json
import os
import time

import pytest

pytest.importorskip("qgis.core")
from qgis.testing import start_app  # noqa: E402

APP = start_app()

from qgis.core import QgsProject, QgsSettings  # noqa: E402
from qgis.PyQt.QtCore import Qt, pyqtSignal  # noqa: E402
from qgis.PyQt.QtTest import QTest  # noqa: E402
from qgis.PyQt.QtGui import QColor, QImage  # noqa: E402
from qgis.PyQt.QtWidgets import QPushButton, QTabBar  # noqa: E402

from mikage.core.catalog.watch import WatchNotice  # noqa: E402
from mikage.core.esri import EsriClient  # noqa: E402
from mikage.qgis_io import settings  # noqa: E402
from mikage.qgis_io.context import PluginContext  # noqa: E402
from mikage.tests.fake_esri import FakeEsri, FakeLayer  # noqa: E402
from mikage.tests.qgis_fakes import FakeIface, wait_for  # noqa: E402
from mikage.tests.test_qgis_observations import geotiff  # noqa: E402
from mikage.ui import notice_feed  # noqa: E402
from mikage.ui.central_views import MAP  # noqa: E402

IMAGES = "https://example.com/arcgis/rest/services/Images/FeatureServer/0"
PLACES = "https://example.com/arcgis/rest/services/Places/FeatureServer/0"
TYPES = {"IMG_ID": "esriFieldTypeString", "ACQ": "esriFieldTypeDate", "SENSOR": "esriFieldTypeString",
         "PATH": "esriFieldTypeString", "CLOUD": "esriFieldTypeDouble"}
FEED = "notices"
DAY = 86400000


def ring(x0, y0, x1, y1):
    return {"rings": [[[x0, y0], [x0, y1], [x1, y1], [x1, y0], [x0, y0]]]}


def config_of(tmp_path, **catalog):
    return {"imageCatalogs": [{
        "label": "衛星画像", "url": IMAGES,
        "fields": {"id": "IMG_ID", "acquired": "ACQ", "sensor": "SENSOR", "source": "PATH"},
        "columns": [{"field": "CLOUD", "label": "雲量"}],
        "thumbnail": {"url": str(tmp_path / "ql" / "{id}.png"), "width": 200, "height": 150},
        "footprintLayers": [{"label": "施設", "url": PLACES, "fields": ["NAME", "NAME_EN"], "format": "{NAME}（{NAME_EN}）"}],
        **catalog}]}


def write_config(tmp_path, config):
    path = tmp_path / "config.json"
    path.write_text(json.dumps(config, ensure_ascii=False), encoding="utf-8")
    settings.set_config_source(str(path))


def notice(tmp_path, image_id, minutes_ago, geometry, read=False, watch="w", watch_name="横須賀の新着"):
    now = time.time() * 1000
    acquired = now - DAY - minutes_ago * 60000
    return WatchNotice(id=f"{watch}|{image_id}", watch_id=watch, watch_name=watch_name, catalog_label="衛星画像",
                       found=now - minutes_ago * 60000, read=read, image_id=image_id, acquired=acquired, sensor="S1",
                       feature={"attributes": {"IMG_ID": image_id, "ACQ": acquired, "SENSOR": "S1", "CLOUD": 10,
                                               "PATH": str(tmp_path / "img" / f"{image_id}.tif")}, "geometry": geometry})


def start(iface):
    from mikage import classFactory

    p = classFactory(iface)
    p.initGui()
    for dock in p.docks.values():
        dock.hide()
    p.notice_feed.thumbnail_folder = str(iface.thumbs)
    return p


@pytest.fixture
def plugin(tmp_path, monkeypatch):
    QgsProject.instance().removeAllMapLayers()
    QgsSettings().remove("mikage")
    settings._set("local_gpkg", str(tmp_path / "local.gpkg"))
    write_config(tmp_path, config_of(tmp_path))
    (tmp_path / "ql").mkdir()
    (tmp_path / "img").mkdir()
    for image_id, color in (("A", "#ff0000"), ("B", "#00ff00")):
        picture = QImage(400, 300, QImage.Format.Format_RGB32)
        picture.fill(QColor(color))
        picture.save(str(tmp_path / "ql" / f"{image_id}.png"))
    geotiff(tmp_path / "img" / "A.tif")
    geotiff(tmp_path / "img" / "B.tif", west=140.0)
    fake = FakeEsri()
    fake.add_layer(IMAGES, FakeLayer(TYPES, "esriGeometryPolygon"))
    places = fake.add_layer(PLACES, FakeLayer({"NAME": "esriFieldTypeString", "NAME_EN": "esriFieldTypeString"}, "esriGeometryPoint"))
    places.add({"NAME": "横須賀本港", "NAME_EN": "Yokosuka Port"}, {"x": 139.66, "y": 35.6})
    places.add({"NAME": "長浦港", "NAME_EN": None}, {"x": 139.64, "y": 35.62})
    places.add({"NAME": "館山", "NAME_EN": "Tateyama"}, {"x": 140.1, "y": 35.6})
    monkeypatch.setattr(PluginContext, "esri", lambda self: EsriClient(fake))
    iface = FakeIface()
    iface.thumbs = tmp_path / "thumbs"
    iface.window.resize(1300, 900)
    iface.canvas.setDestinationCrs(QgsProject.instance().crs())
    p = start(iface)
    assert p.api.problems == [], p.api.problems
    store = p.api.notices.store()
    store.add_notices([
        notice(tmp_path, "A", 30, ring(139.6, 35.5, 139.8, 35.7), read=True),
        notice(tmp_path, "B", 20, ring(140.0, 35.5, 140.2, 35.7)),
        notice(tmp_path, "C", 10, ring(139.5, 35.0, 139.55, 35.05)),
    ])
    p.api.notices.refresh()
    yield p, iface, fake, tmp_path
    p.unload()
    QgsProject.instance().removeAllMapLayers()
    QgsSettings().remove("mikage")


def tabs(p):
    bar = p.central.bar
    return [bar.tabText(i) for i in range(bar.count())] if bar is not None else None


def order(feed):
    out = []
    for i in range(feed.column.count()):
        widget = feed.column.itemAt(i).widget()
        if widget in feed.cards.values():
            out.append(widget.notice.image_id)
    return out


def loaded(feed):
    """Every card's thumbnail fetched (or failed) and its features found."""
    def done(card):
        text = card.contents.text()
        return "照合中" not in text and "読み込み中" not in card.picture.toolTip() + text
    return wait_for(lambda: feed.cards and all(done(c) for c in feed.cards.values()) and not feed.work._running and not feed.work._waiting)


def test_the_page_and_the_map_switch_by_tabs(plugin):
    p, iface, *_ = plugin
    grid = iface.window.centralWidget().layout()
    positions = {grid.itemAt(i).widget(): grid.getItemPosition(i) for i in range(grid.count())}
    assert p.central.available and p.central.bar is None
    p.toggle_notice_feed()
    assert p.central.current() == FEED and iface.stack.currentWidget() is p.notice_feed
    # The tabs over the map area: QGIS's widgets a row down.
    assert tabs(p) == ["地図", "新着画像（2）"]
    assert p.central.bar.objectName() == "MikageCentralTabs"
    assert grid.getItemPosition(grid.indexOf(p.central.bar)) == (0, 0, 1, 1)
    assert grid.getItemPosition(grid.indexOf(iface.stack)) == (1, 0, 2, 1)
    # The map has no ×; the page has.
    assert p.central.bar.tabButton(0, QTabBar.ButtonPosition.RightSide) is None
    # The same key (or the map tab) back to the map.
    p.toggle_notice_feed()
    assert p.central.current() == MAP and iface.stack.currentWidget() is iface.canvas
    assert p.central.bar.currentIndex() == 0
    p.central.bar.setCurrentIndex(1)
    assert p.central.current() == FEED
    # QGIS goes back to the map by itself (a layer added): the tabs follow.
    iface.stack.setCurrentIndex(0)
    assert p.central.bar.currentIndex() == 0
    # The unread count in the tab's text.
    p.api.notices._mark(["w|B"])
    assert tabs(p) == ["地図", "新着画像（1）"]
    # Closed: the tabs go, QGIS's widgets back where they were.
    p.central.close(FEED)
    assert p.central.bar is None and iface.stack.indexOf(p.notice_feed) < 0
    assert {grid.itemAt(i).widget(): grid.getItemPosition(i) for i in range(grid.count())} == positions
    assert settings.central_views() == []


def test_back_from_the_welcome_page_and_open_again_after_restart(plugin):
    p, iface, fake, tmp_path = plugin
    iface.stack.setCurrentWidget(iface.welcome)
    p.toggle_notice_feed()
    p.toggle_notice_feed()
    # QGIS's own page that was in front before.
    assert iface.stack.currentWidget() is iface.welcome
    assert settings.central_views() == [FEED]
    p.unload()
    assert iface.stack.indexOf(p.notice_feed) < 0 and iface.window.findChild(QTabBar, "MikageCentralTabs") is None
    again = start(iface)
    try:
        # Open again, behind the map.
        assert again.central.is_open(FEED) and again.central.current() == MAP
        assert tabs(again) == ["地図", "新着画像（2）"]
    finally:
        again.unload()


def test_open_again_only_once_qgis_has_started(plugin):
    # Changing QGIS's map area while QGIS is still being built (plugins load before its window shows) made it crash.
    p, iface, fake, tmp_path = plugin
    p.toggle_notice_feed()
    p.unload()

    class Starting(FakeIface):
        initializationCompleted = pyqtSignal()

    starting = Starting()
    starting.window.hide()
    starting.thumbs = tmp_path / "thumbs"
    grid = starting.window.centralWidget().layout()
    again = start(starting)
    try:
        assert not again.central.is_open(FEED) and again.central.bar is None
        assert grid.getItemPosition(grid.indexOf(starting.stack)) == (0, 0, 2, 1)
        # The plugin's tasks wait while QGIS starts (another plugin being imported while one ran crashed QGIS).
        from mikage.qgis_io import tasks

        assert tasks._holding
        starting.window.show()
        starting.initializationCompleted.emit()
        assert not tasks._holding
        assert wait_for(lambda: again.central.is_open(FEED), 2000)
        assert again.central.current() == MAP and tabs(again) == ["地図", "新着画像（2）"]
    finally:
        again.unload()


def test_cards_newest_at_the_bottom_with_everything_on_them(plugin):
    p, iface, fake, tmp_path = plugin
    feed = p.notice_feed
    p.toggle_notice_feed()
    assert order(feed) == ["A", "B", "C"]
    assert feed.count.text() == "未読 2 件・全 3 件"
    assert loaded(feed)
    a, b, c = (feed.cards[f"w|{i}"] for i in "ABC")
    assert not a.badge.isVisibleTo(a) and b.badge.isVisibleTo(b)
    assert "横須賀の新着" in b.attributes.text() and "雲量" in b.attributes.text() and "衛星画像" in b.attributes.text()
    # 写っている地物: every feature over the footprint, in Japanese and English (empty brackets dropped).
    assert "施設（2 件）" in a.contents.text() and "<tr><td>横須賀本港（Yokosuka Port）</td></tr><tr><td>長浦港</td></tr>" in a.contents.text()
    assert "施設（1 件）" in b.contents.text() and "館山（Tateyama）" in b.contents.text()
    assert "施設（0 件）" in c.contents.text() and "なし" in c.contents.text()
    # Kept with the notice: not asked again next time.
    kept = {n.id: n.extra for n in p.api.notices.store().notices()}
    layer_key = p.api.config.image_catalogs[0].footprint_layers[0].key()
    assert kept["w|A"]["contents"][layer_key]["count"] == 2
    # The thumbnails: from the file pattern, kept in the folder, shown at the size asked.
    assert a.picture.width() == 200 and a.picture.height() == 150
    assert a.picture.pixmap().toImage().pixelColor(100, 75) == QColor("#ff0000")
    assert len([f for f in os.listdir(iface.thumbs) if f.endswith(".png")]) == 2
    # C has no file: the footprint drawn, why in its tip.
    assert "ファイルなし" in c.picture.toolTip()
    asked = len(fake.asked)
    feed._config_changed()  # the cards made again (the settings file read again)
    assert loaded(feed)
    assert [u for u, _ in fake.asked[asked:] if "Places" in u] == []


def test_a_narrow_map_area(plugin):
    # The docks on both sides leave the map area narrow: the thumbnail smaller, the filters on a row of their own,
    # nothing to scroll sideways, and the page never makes QGIS's map area wider.
    p, iface, fake, tmp_path = plugin
    feed = p.notice_feed
    p.toggle_notice_feed()
    card = feed.cards["w|B"]  # unread: its 既読 button too
    assert wait_for(lambda: card.picture.width() == 200)
    assert feed._top.indexOf(feed.controls) >= 0
    # Wide: the thumbnail, the attributes, the files and the features side by side (サムネ、メタ、ファイル、地物).
    assert card.mode == 4 and (card.column_of(card.files_box), card.column_of(card.contents_box)) == (1, 2)
    iface.window.resize(850, 800)
    # Less room: the files above the features.
    assert wait_for(lambda: card.mode == 3, 2000), card.mode
    assert (card.column_of(card.files_box), card.column_of(card.contents_box)) == (1, 1)
    iface.window.resize(600, 800)
    assert wait_for(lambda: feed._second.indexOf(feed.controls) >= 0, 2000)
    # Narrow: all under the attributes.
    assert not card.wide and (card.column_of(card.files_box), card.column_of(card.contents_box)) == (0, 0)
    assert notice_feed.MIN_PICTURE <= card.picture.width() < 200
    assert card.picture.height() == round(150 * card.picture.width() / 200)
    assert wait_for(lambda: feed.scroll.horizontalScrollBar().maximum() == 0, 2000), (card.width(), feed.scroll.viewport().width())
    assert feed.minimumSizeHint().width() < 300
    iface.window.resize(1300, 900)
    assert wait_for(lambda: card.picture.width() == 200 and feed._top.indexOf(feed.controls) >= 0 and card.mode == 4, 2000)


def test_the_features_are_asked_again_when_the_layers_change(plugin):
    p, iface, fake, tmp_path = plugin
    feed = p.notice_feed
    p.toggle_notice_feed()
    assert loaded(feed)
    asked = len(fake.asked)
    write_config(tmp_path, config_of(tmp_path, footprintLayers=[
        {"label": "港", "url": PLACES, "fields": ["NAME"]}]))
    p.api.reload_config()
    assert wait_for(lambda: "港（2 件）" in feed.cards["w|A"].contents.text())
    assert "<tr><td>横須賀本港</td></tr><tr><td>長浦港</td></tr>" in feed.cards["w|A"].contents.text()
    assert [u for u, _ in fake.asked[asked:] if "Places" in u]
    # Only the layers the settings file has now are kept.
    kept = {n.id: n.extra for n in p.api.notices.store().notices()}["w|A"]["contents"]
    assert list(kept) == [p.api.config.image_catalogs[0].footprint_layers[0].key()]


def test_one_feature_a_line_with_a_background_from_an_attribute(plugin):
    # tsudo 2026-11-14: 「箇条書きみたいな（点はいらない）」 and 「その行の背景色を属性から変えたい」.
    p, iface, fake, tmp_path = plugin
    feed = p.notice_feed
    write_config(tmp_path, config_of(tmp_path, footprintLayers=[
        {"label": "施設", "url": PLACES, "fields": ["NAME"], "background": {"field": "NAME", "colors": {"横須賀本港": "#1e3a8a"}, "default": "pink"}},
        {"label": "英名", "url": PLACES, "fields": ["NAME"], "background": "NAME_EN"}]))
    p.api.reload_config()
    assert p.api.problems == []
    p.toggle_notice_feed()
    assert loaded(feed)
    text = feed.cards["w|A"].contents.text()
    # A row each, coloured: dark → white text, light → black; the default for the rest.
    assert "<td bgcolor='#1e3a8a' style='color:#ffffff'>横須賀本港</td>" in text
    assert "<td bgcolor='#ffc0cb' style='color:#111111'>長浦港</td>" in text
    # The value as the colour: 「Yokosuka Port」 is not one, so no background (and no markup from the attribute).
    assert "英名（2 件）" in text and "Yokosuka Port" not in text
    assert text.count("bgcolor") == 2
    assert "<tr><td>横須賀本港</td></tr><tr><td>長浦港</td></tr>" in text.split("英名")[1]


def test_the_page_never_moves_by_itself(plugin):
    p, iface, fake, tmp_path = plugin
    feed = p.notice_feed
    store = p.api.notices.store()
    # Enough cards to scroll, all read: the page opens at the bottom (the newest).
    store.delete_notices()
    store.add_notices([notice(tmp_path, f"R{i}", 100 - i, ring(139.6, 35.5, 139.8, 35.7), read=True) for i in range(8)])
    p.api.notices.refresh()
    p.toggle_notice_feed()
    bar = feed.scroll.verticalScrollBar()
    assert wait_for(lambda: bar.maximum() > 0 and bar.value() == bar.maximum(), 3000)
    bar.setValue(0)
    store.add_notices([notice(tmp_path, "N1", 5, ring(139.6, 35.5, 139.8, 35.7))])
    p.api.notices.refresh()
    assert order(feed)[-1] == "N1"
    # Not moved; 「↓ 新着 1 件」 instead.
    assert wait_for(lambda: feed.arrived_button.isVisible(), 2000)
    assert bar.value() == 0 and feed.arrived_button.text() == "↓ 新着 1 件"
    feed.arrived_button.click()
    assert not feed.arrived_button.isVisible()
    card = feed.cards["w|N1"]
    assert wait_for(lambda: bar.value() > 0, 2000)
    assert card.y() >= bar.value() and card.y() < bar.value() + feed.scroll.viewport().height()


def test_the_page_opens_at_the_oldest_unread(plugin):
    p, iface, fake, tmp_path = plugin
    feed = p.notice_feed
    store = p.api.notices.store()
    store.delete_notices()
    store.add_notices([notice(tmp_path, f"R{i}", 100 - i, ring(139.6, 35.5, 139.8, 35.7), read=i < 4) for i in range(8)])
    p.api.notices.refresh()
    p.toggle_notice_feed()
    bar = feed.scroll.verticalScrollBar()
    first = feed.cards["w|R4"]
    assert wait_for(lambda: bar.value() > 0 and abs(bar.value() - (first.y() - 8)) <= 1, 3000)
    # Nothing said to have come in (they were there before).
    assert not feed.arrived_button.isVisible()


def test_open_goes_to_the_image_on_the_map(plugin):
    p, iface, fake, tmp_path = plugin
    feed = p.notice_feed
    p.toggle_notice_feed()
    b = feed.cards["w|B"]
    b.buttons["open"].click()
    assert p.central.current() == MAP
    opened = [layer for layer in QgsProject.instance().mapLayers().values() if layer.source().endswith("B.tif")]
    assert len(opened) == 1
    # At the image (west edge 140.0, a little around it).
    extent = iface.canvas.extent()
    assert extent.contains(opened[0].extent()) and extent.width() < 0.5
    assert {n.id: n.read for n in p.api.notices.store().notices()}["w|B"]
    # 「前の画像を閉じる」: the one opened here before closes.
    feed.close_previous.setChecked(True)
    before = set(QgsProject.instance().mapLayers())
    feed.cards["w|B"].buttons["open"].click()
    second = set(QgsProject.instance().mapLayers()) - before
    assert len(second) == 1
    feed.cards["w|A"].buttons["open"].click()
    left = set(QgsProject.instance().mapLayers())
    # The one opened before 「前の画像を閉じる」 was on stays.
    assert not second & left and before <= left
    assert any(layer.source().endswith("A.tif") for layer in QgsProject.instance().mapLayers().values())
    assert settings.feed_close_previous()


def test_image_files_on_the_card_checked_or_opened_alone(plugin):
    # tsudo 2026-11-14: 「その画像カタログレコードから開ける画像ファイルの一覧リスト出して、そこから簡単に開けるように」,
    # then 「チェックボックスにして、クリックでチェック、ダブルクリックでそれだけ開く」.
    p, iface, fake, tmp_path = plugin
    feed = p.notice_feed
    store = p.api.notices.store()
    folder = tmp_path / "img" / "D_PRODUCT"
    (folder / "PAN").mkdir(parents=True)
    geotiff(folder / "PAN" / "D_pan.tif")
    geotiff(folder / "D_ms.tif", west=139.9)
    geotiff(folder / "D_psh.tif", west=139.7)
    (folder / "D_meta.xml").write_text("<x/>", encoding="utf-8")
    d = notice(tmp_path, "D", 5, ring(139.6, 35.5, 140.1, 35.7))
    d.feature["attributes"]["PATH"] = str(folder)
    store.add_notices([d])
    p.api.notices.refresh()
    p.toggle_notice_feed()
    assert loaded(feed) and wait_for(lambda: {"w|A", "w|D"} <= set(feed.files) and feed.work.failure("files|w|C"), 5000)
    # A file record: its file; a folder record: each image below it (the patterns of the catalog), all checked.
    a, c, card = feed.cards["w|A"], feed.cards["w|C"], feed.cards["w|D"]
    assert [r.name for r in a.file_rows] == ["A.tif"] and a.files_title.text() == "画像ファイル"
    assert card.files_title.text() == "画像ファイル（3）"
    assert sorted(r.name for r in card.file_rows) == ["D_ms.tif", "D_psh.tif", "PAN/D_pan.tif"]
    assert all(r.box.isChecked() for r in card.file_rows)
    pan = next(r for r in card.file_rows if r.name == "PAN/D_pan.tif")
    assert pan.label.text() == "PAN/\u200bD_\u200bpan.tif"  # may wrap, never widens the card
    assert str(folder) in pan.toolTip()
    # No file: why, on the card (not a fault).
    assert not c.file_rows and "見つからず" in c.files.text()
    # A click: unchecked (on the box or the name), again: checked; 「開く」 opens those checked, no window.
    ms = next(r for r in card.file_rows if r.name == "D_ms.tif")
    psh = next(r for r in card.file_rows if r.name == "D_psh.tif")
    QTest.mouseClick(ms, Qt.MouseButton.LeftButton, pos=ms.box.geometry().center())  # on the box
    QTest.mouseClick(psh.label, Qt.MouseButton.LeftButton)
    assert not ms.box.isChecked() and not psh.box.isChecked() and pan.box.isChecked()
    before = set(QgsProject.instance().mapLayers())
    card.buttons["open"].click()
    opened = [QgsProject.instance().mapLayer(i) for i in set(QgsProject.instance().mapLayers()) - before]
    assert [layer.source().replace("\\", "/").endswith("PAN/D_pan.tif") for layer in opened] == [True], [(x.name(), x.source()) for x in opened]
    assert p.central.current() == MAP and iface.canvas.extent().contains(opened[0].extent())
    assert {n.id: n.read for n in store.notices()}["w|D"]
    # The checks stay when the card is made again.
    p.toggle_notice_feed()
    feed._config_changed()
    assert wait_for(lambda: "w|D" in feed.files and len(feed.cards["w|D"].file_rows) == 3, 5000)
    card = feed.cards["w|D"]
    assert {r.name: r.box.isChecked() for r in card.file_rows} == {"D_ms.tif": False, "D_psh.tif": False, "PAN/D_pan.tif": True}
    # A double click: that one alone, whatever is checked (its check as it was).
    ms = next(r for r in card.file_rows if r.name == "D_ms.tif")
    before = set(QgsProject.instance().mapLayers())
    QTest.mouseClick(ms.label, Qt.MouseButton.LeftButton)  # a real double click starts with a click
    QTest.mouseDClick(ms.label, Qt.MouseButton.LeftButton)
    opened = [QgsProject.instance().mapLayer(i) for i in set(QgsProject.instance().mapLayers()) - before]
    assert [layer.source().endswith("D_ms.tif") for layer in opened] == [True]
    assert not ms.box.isChecked()
    # Nothing checked: nothing opened, said so.
    p.toggle_notice_feed()
    for row in feed.cards["w|D"].file_rows:
        if row.box.isChecked():
            QTest.mouseClick(row.label, Qt.MouseButton.LeftButton)
    before = set(QgsProject.instance().mapLayers())
    feed.cards["w|D"].buttons["open"].click()
    assert set(QgsProject.instance().mapLayers()) == before and p.central.current() == FEED
    # A file record's double click: the record (as 「開く」). Its files looked for again once in view.
    feed.go_to("w|A")
    assert wait_for(lambda: feed.cards["w|A"].file_rows, 5000)
    before = set(QgsProject.instance().mapLayers())
    row = feed.cards["w|A"].file_rows[0]
    QTest.mouseClick(row.label, Qt.MouseButton.LeftButton)
    QTest.mouseDClick(row.label, Qt.MouseButton.LeftButton)
    assert [QgsProject.instance().mapLayer(i).source().endswith("A.tif") for i in set(QgsProject.instance().mapLayers()) - before] == [True]
    assert row.box.isChecked()


def test_see_on_the_map_and_the_buttons(plugin):
    p, iface, fake, tmp_path = plugin
    feed = p.notice_feed
    p.toggle_notice_feed()
    c = feed.cards["w|C"]
    c.buttons["map"].click()
    assert p.central.current() == MAP
    assert iface.canvas.extent().contains(139.52, 35.02)
    assert {n.id: n.read for n in p.api.notices.store().notices()}["w|C"]
    p.toggle_notice_feed()
    b = feed.cards["w|B"]
    assert b.buttons["read"].isVisibleTo(b)
    b.buttons["read"].click()
    assert not feed.cards["w|B"].badge.isVisibleTo(b)
    assert feed.count.text() == "未読 0 件・全 3 件"
    # No 「削除」 on the cards (tsudo: 用語が混乱する).
    assert "delete" not in feed.cards["w|A"].buttons
    assert not [b for b in feed.cards["w|A"].findChildren(QPushButton) if b.text() == "削除"]


def test_filters(plugin):
    p, iface, fake, tmp_path = plugin
    feed = p.notice_feed
    p.api.notices.store().add_notices([notice(tmp_path, "D", 5, ring(139.6, 35.5, 139.8, 35.7), watch="v", watch_name="東京湾")])
    p.api.notices.refresh()
    p.toggle_notice_feed()
    shown = lambda: [i for i in order(feed) if not feed.cards[f"{'v' if i == 'D' else 'w'}|{i}"].isHidden()]  # noqa: E731
    feed.unread_only.setChecked(True)
    assert shown() == ["B", "C", "D"] and feed.count.text() == "未読 3 件・全 4 件（表示 3 件）"
    # Read while 未読のみ is on: stays until it is turned again.
    feed.cards["w|B"].buttons["read"].click()
    assert shown() == ["B", "C", "D"]
    feed.unread_only.setChecked(False)
    feed.unread_only.setChecked(True)
    assert shown() == ["C", "D"]
    assert settings.feed_unread_only()
    feed.unread_only.setChecked(False)
    feed.watch_box.setCurrentIndex(feed.watch_box.findData("v"))
    assert shown() == ["D"]
    assert [feed.watch_box.itemText(i) for i in range(feed.watch_box.count())] == ["すべての監視", "東京湾", "横須賀の新着"]


def test_older_cards_a_page_at_a_time(plugin, monkeypatch):
    p, iface, fake, tmp_path = plugin
    monkeypatch.setattr(notice_feed, "PAGE", 2)
    feed = p.notice_feed
    feed.shown = 2
    p.toggle_notice_feed()
    assert order(feed) == ["B", "C"]
    assert feed.older.isVisible() and feed.older.text() == "さらに前の 1 件を表示"
    feed.older.click()
    assert order(feed) == ["A", "B", "C"] and not feed.older.isVisible()


def test_new_images_in_the_message_bar_open_the_page(plugin):
    p, iface, fake, tmp_path = plugin
    p.api.notices._say_new(2)
    item = iface.bar.currentItem()
    assert item is not None and "新着画像 2 件" in item.text()
    look = [b for b in item.findChildren(QPushButton) if b.text() == "見る"]
    assert look
    look[0].click()
    assert p.central.current() == FEED


def test_settings_file_problems(plugin):
    p, iface, fake, tmp_path = plugin
    write_config(tmp_path, config_of(tmp_path, thumbnail="https://q/{nope}.jpg",
                                     footprintLayers=[{"url": PLACES, "fields": ["NAME"], "format": "{NAME_EN}"}]))
    p.api.reload_config()
    problems = " / ".join(p.api.problems)
    assert "imageCatalogs[0].thumbnail: {nope} は使えない" in problems
    assert "imageCatalogs[0].footprintLayers[0].format の {NAME_EN} は fields にない属性" in problems
    # Without a thumbnail: the footprint drawn.
    p.toggle_notice_feed()
    card = p.notice_feed.cards["w|A"]
    assert (card.picture.width(), card.picture.height()) == (270, 200) and not card.picture.pixmap().isNull()


def test_menu_shortcut_and_toolbar(plugin):
    p, iface, *_ = plugin
    from mikage.ui.shortcuts import GLOBAL

    assert ("ToggleNoticeFeed", "新着画像（大きな画面）と地図の切り替え", "Ctrl+Alt+N") in GLOBAL
    action = iface.window.findChild(type(p.actions[0]), "SatObsNoticeFeed")
    assert action is not None and action.text() == "新着画像（大きな画面）"
    action.trigger()
    assert p.central.current() == FEED
    action.trigger()
    assert p.central.current() == MAP
