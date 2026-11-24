"""
画像の評価 in QGIS (tsudo, 2026-11-24: 「4は、形状ありで、ネームドエリアごと1行にしようか」):
the 評価 line beside 分担 in 観測記録, the window (the whole image, then the
named areas the image shows cut by its footprint, first ratings from the
不可範囲), saved one row per image × named area, 分担's 完了 opening it, and
the image catalog's 評価 column.
"""
import json

import pytest

pytest.importorskip("qgis.core")
from qgis.testing import start_app  # noqa: E402

APP = start_app()

from qgis.core import QgsGeometry  # noqa: E402

from mikage.core.esri import EsriClient  # noqa: E402
from mikage.core.image_reviews import BAD, GOOD, PART, WHOLE, ImageReview  # noqa: E402
from mikage.qgis_io import settings  # noqa: E402
from mikage.qgis_io.geometry import rings_geometry  # noqa: E402
from mikage.tests.fake_esri import FakeEsri, FakeLayer  # noqa: E402
from mikage.tests.qgis_fakes import wait_for  # noqa: E402
from mikage.tests.test_qgis_alerts import plugin  # noqa: E402,F401

REVIEWS = "https://x/arcgis/rest/services/Work/FeatureServer/2"
WORK = "https://x/arcgis/rest/services/Work/FeatureServer/0"
AREAS = "https://x/arcgis/rest/services/Alerts/FeatureServer/1"
S, D, N = "esriFieldTypeString", "esriFieldTypeDate", "esriFieldTypeDouble"
REVIEW_TYPES = {"REVIEW_ID": S, "IMAGE_ID": S, "IMAGE_NAME": S, "IMAGE_TIME": D, "SATELLITE": S, "NAMED_AREA_ID": S, "NAMED_AREA_NAME": S,
                "RATING": S, "CLOUD_PCT": N, "REASONS": S, "NOTE": S, "REVIEWED_BY": S, "REVIEWED_AT": D}


def box(w, s, e, n):
    return {"rings": [[[w, s], [e, s], [e, n], [w, n], [w, s]]]}


def _setup(p, tmp_path, work=False, named=True):
    """The image of the fixture is 139.6–139.8, 35.5–35.7 (test_qgis_observations.geotiff)."""
    fake = FakeEsri()
    reviews = fake.add_layer(REVIEWS, FakeLayer(dict(REVIEW_TYPES), "esriGeometryPolygon"))
    areas = fake.add_layer(AREAS, FakeLayer({"NAME": S, "KIND": S, "NAMED_AREA_ID": S, "ACTIVE": "esriFieldTypeSmallInteger",
                                             "AREA_USE": S}, "esriGeometryPolygon"))
    # 東京湾 crosses the image's edge (cut by it); 横須賀港 is small and inside; 相模湾 is outside; 古い港 is inactive.
    areas.add({"NAME": "東京湾", "KIND": "海域", "NAMED_AREA_ID": "NA-000001", "ACTIVE": 1}, box(139.65, 35.3, 140.1, 35.65))
    areas.add({"NAME": "横須賀港", "KIND": "港", "NAMED_AREA_ID": "NA-000101", "ACTIVE": 1, "AREA_USE": "両方"},
              box(139.62, 35.62, 139.66, 35.66))
    areas.add({"NAME": "相模湾", "KIND": "海域", "NAMED_AREA_ID": "NA-000002", "ACTIVE": 1}, box(139.1, 34.9, 139.5, 35.3))
    areas.add({"NAME": "古い港", "KIND": "港", "NAMED_AREA_ID": "NA-000900", "ACTIVE": 0}, box(139.7, 35.6, 139.72, 35.62))
    if work:
        fake.add_layer(WORK, FakeLayer({"IMAGE_ID": S, "IMAGE_NAME": S, "IMAGE_TIME": D, "ASSIGNEE": S, "STATUS": S, "NOTE": S,
                                        "TAKEN_AT": D, "DONE_AT": D}, ""))
    config = tmp_path / "config.json"
    config.write_text(json.dumps({"imageReviews": REVIEWS, "editTracking": {"user": "田中"},
                                  **({"namedAreas": AREAS} if named else {}),
                                  **({"assignments": {"url": WORK, "mode": "exclusive"}} if work else {})}), encoding="utf-8")
    settings.set_config_source(str(config))
    p.context.esri = lambda: EsriClient(fake)
    p.context.reload()
    return fake, reviews


def _open(dock):
    bar = dock.review_bar
    assert wait_for(lambda: bar.rows(dock.book.current.id) is not None)
    dialog = bar.open()
    assert dialog is not None
    assert wait_for(lambda: dialog.save_button.isEnabled())
    return bar, dialog


def test_the_line_and_the_window_list_the_named_areas_the_image_shows(plugin):
    p, iface, dock, tmp_path = plugin
    _setup(p, tmp_path)
    bar = dock.review_bar
    assert bar.isVisible() and not dock.assignment_bar.isVisible()  # with or without 分担
    assert wait_for(lambda: bar.label.text() == "評価: なし")
    # A record in 東京湾: listed first, though 横須賀港 is smaller.
    dock.book.add(139.75, 35.55, object="船A", kind="船舶", activity="航行")
    _bar, dialog = _open(dock)
    names = [r.name for r in dialog.rows]
    assert names == [WHOLE, "東京湾", "横須賀港"], names
    assert dialog.table.item(1, 0).text() == "東京湾（記録 1）"
    # 東京湾's row is its part inside the image (cut by the footprint), not the whole bay.
    part = rings_geometry(dialog.rows[1].rings)
    assert part.boundingBox().xMinimum() == pytest.approx(139.65) and part.boundingBox().xMaximum() == pytest.approx(139.8)
    assert part.boundingBox().yMinimum() == pytest.approx(35.5) and part.boundingBox().yMaximum() == pytest.approx(35.65)
    assert "地名 2 件" in dialog.status.text()
    # The search narrows the named areas (the whole image stays).
    dialog.search.setText("横須賀")
    assert [dialog.table.isRowHidden(i) for i in range(3)] == [False, True, False]
    dialog.search.setText("")
    dialog.reject()


def test_saved_one_row_per_named_area_and_written_over(plugin):
    p, iface, dock, tmp_path = plugin
    fake, reviews = _setup(p, tmp_path)
    bar, dialog = _open(dock)
    dialog.set_rating(dialog.row_of("東京湾"), BAD)
    dialog.table.cellWidget(dialog.row_of("東京湾"), 2).setValue(80)
    menu = dialog.table.cellWidget(dialog.row_of("東京湾"), 3).menu()
    next(a for a in menu.actions() if a.text() == "雲").setChecked(True)
    dialog.set_rating(dialog.row_of("横須賀港"), GOOD)
    dialog.save()
    assert wait_for(lambda: bar.dialog is None)
    rows = {r["attributes"]["NAMED_AREA_NAME"]: r for r in reviews.rows}
    assert set(rows) == {"東京湾", "横須賀港"}
    bay = rows["東京湾"]
    assert (bay["attributes"]["RATING"], bay["attributes"]["CLOUD_PCT"], bay["attributes"]["REASONS"]) == (BAD, 80, "雲")
    assert bay["attributes"]["NAMED_AREA_ID"] == "NA-000001" and bay["attributes"]["IMAGE_ID"] == dock.book.current.id
    assert bay["attributes"]["REVIEWED_BY"] == "田中" and bay["geometry"]["rings"]
    assert QgsGeometry.fromWkt("POINT(139.9 35.5)").within(rings_geometry(bay["geometry"]["rings"])) is False
    assert bar.label.text() == "評価: 使える 1・使えない 1" and "東京湾: 使えない（雲 80%・雲）" in bar.label.toolTip()
    assert "画像の評価を保存" in dock.status.text()
    # Opened again: the rows saved; 東京湾 put back to empty and 画像全体 rated → one deleted, one added.
    _bar, again = _open(dock)
    assert again.rating_of(again.row_of("東京湾")) == BAD and "田中" in again.table.item(again.row_of("東京湾"), 5).text()
    again.set_rating(again.row_of("東京湾"), "")
    again.set_rating(0, PART)
    again.save()
    assert wait_for(lambda: bar.dialog is None)
    assert sorted(r["attributes"]["NAMED_AREA_NAME"] or WHOLE for r in reviews.rows) == sorted([WHOLE, "横須賀港"])
    assert bar.label.text().startswith("評価: ")


def test_first_ratings_come_from_the_unseen_areas(plugin):
    p, iface, dock, tmp_path = plugin
    _setup(p, tmp_path)
    if not dock.book.keeps_unseen_areas():
        pytest.skip("この保存先は不可範囲を記録できない")
    # 横須賀港 all under cloud: 使えない (雲) as a first rating, said as such; the whole image only partly: 一部.
    assert dock.book.add_area([[139.61, 35.61], [139.67, 35.61], [139.67, 35.67], [139.61, 35.67], [139.61, 35.61]], unseen="雲")
    _bar, dialog = _open(dock)
    i = dialog.row_of("横須賀港")
    assert dialog.rating_of(i) == BAD and dialog.table.cellWidget(i, 3).text() == "雲"
    assert dialog.rating_of(0) == PART
    assert dialog.rating_of(dialog.row_of("東京湾")) in (PART, "")
    assert "不可範囲から" in dialog.status.text() and "案" in dialog.table.cellWidget(i, 1).toolTip()
    dialog.reject()


def test_without_named_areas_only_the_whole_image(plugin):
    p, iface, dock, tmp_path = plugin
    _setup(p, tmp_path, named=False)
    _bar, dialog = _open(dock)
    assert [r.name for r in dialog.rows] == [WHOLE] and "namedAreas" in dialog.status.text()
    dialog.reject()


def test_done_opens_the_window_when_the_image_has_no_rating(plugin):
    p, iface, dock, tmp_path = plugin
    fake, reviews = _setup(p, tmp_path, work=True)
    work, bar = dock.assignment_bar, dock.review_bar
    assert wait_for(lambda: work.read_at > 0)
    work.take()
    assert wait_for(lambda: work.done_button.isVisible())
    work.set_status("完了")
    assert wait_for(lambda: bar.dialog is not None)
    assert bar.dialog.later_button.text() == "あとで"
    bar.dialog.reject()
    assert wait_for(lambda: bar.dialog is None)
    # Rated already: 完了 again does not open it.
    reviews.add({"REVIEW_ID": "REV-1", "IMAGE_ID": dock.book.current.id, "RATING": GOOD, "REVIEWED_BY": "田中"},
                box(139.6, 35.5, 139.8, 35.7))
    bar.read_at.clear()
    bar.after_done(dock.book.current)
    assert wait_for(lambda: bar.rows(dock.book.current.id))
    assert bar.dialog is None and bar.label.text() == "評価: 画像全体 使える"


def test_the_catalog_column(plugin):
    from mikage.ui.catalog_model import CatalogModel
    from mikage.core.catalog.api import CatalogRecord

    model = CatalogModel()
    records = [CatalogRecord({}, None, i, None, None, None, "", None, None, None) for i in "ABC"]
    model.set_records(None, records, reviews=True)
    # Next to the image's id (here the first column).
    assert [c.label for c in model.columns][:2] == ["場所", "評価"]
    assert model.text(records[0], "review") == "…"
    model.set_reviews({"A": [ImageReview("A", GOOD, area_name="呉港"), ImageReview("A", BAD, area_name="江田島")],
                       "B": [ImageReview("B", BAD)], "C": []})
    assert [model.text(r, "review") for r in records] == ["一部（使えない 1/2）", BAD, ""]
    column = 1
    assert "江田島: 使えない" in model.data(model.index(0, column), 3)  # ToolTipRole
    assert model.data(model.index(1, column), 9).name() == "#c62828"  # ForegroundRole
    model.sort(column)
    assert [r.id for r in model.records] == ["A", "B", "C"]
    model.sort(column, 1)
    assert [r.id for r in model.records] == ["B", "A", "C"]
