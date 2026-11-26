"""
撮像依頼 from the organisation's areas (組織の関心地域, tsudo 2026-11-24:
「撮像依頼をネームドエリアから簡単に出せるように」): the 「組織」 tab's
「撮像依頼…」 (one area, or several in one form), the map's 「この地域で撮像依頼…」
(the smallest area first where they overlap) and the form's 「地域から…」; the
request keeps the area's 管理番号 and follows its shape until changed by hand.
"""
import json

import pytest

pytest.importorskip("qgis.core")
from qgis.testing import start_app  # noqa: E402

APP = start_app()

from qgis.core import QgsPointXY, QgsProject, QgsSettings  # noqa: E402

from mikage.qgis_io import qsettings  # noqa: E402
from mikage.core.esri import EsriClient  # noqa: E402
from mikage.core.obs.alerts import DEFAULT_PLACE_FIELDS  # noqa: E402
from mikage.core.request.store import DEFAULT_HISTORY_FIELDS, DEFAULT_REQUEST_FIELDS  # noqa: E402
from mikage.features.images import Images  # noqa: E402
from mikage.features.requests import Requests  # noqa: E402
from mikage.qgis_io import settings  # noqa: E402
from mikage.qgis_io.context import PluginContext  # noqa: E402
from mikage.qgis_io.org_areas_io import layer_of  # noqa: E402
from mikage.tests.fake_esri import FakeEsri, FakeLayer  # noqa: E402
from mikage.tests.feature_harness import small_plugin  # noqa: E402
from mikage.tests.qgis_fakes import FakeIface, wait_for  # noqa: E402
from mikage.ui import org_areas_panel  # noqa: E402
from mikage.ui.interests_dock import TAB_KEY  # noqa: E402
from mikage.ui.request_dock import COLUMNS  # noqa: E402

PLACES = "https://example.com/arcgis/rest/services/Alerts/FeatureServer/3"
CATEGORIES = "https://example.com/arcgis/rest/services/Alerts/FeatureServer/5"
LEDGER = "https://example.com/arcgis/rest/services/Req/FeatureServer/0"
HISTORY = "https://example.com/arcgis/rest/services/Req/FeatureServer/1"
S, I = "esriFieldTypeString", "esriFieldTypeInteger"
FIELDS = {**{v: S for v in DEFAULT_PLACE_FIELDS.values()}, "ACTIVE": "esriFieldTypeSmallInteger", "PRIORITY": I}
CATEGORY_FIELDS = {"CATEGORY_ID": S, "CATEGORY_NAME": S, "PARENT_ID": S, "SORT_ORDER": I, "COLOR": S,
                   "ACTIVE": "esriFieldTypeSmallInteger", "NOTE": S}
AREA = DEFAULT_REQUEST_FIELDS["namedAreaId"]
FOLLOW = DEFAULT_REQUEST_FIELDS["areaFollow"]


def box(w, s, e, n):
    return {"rings": [[[w, s], [w, n], [e, n], [e, s], [w, s]]]}


@pytest.fixture
def plugin(tmp_path, monkeypatch):
    QgsProject.instance().removeAllMapLayers()
    QgsSettings().remove("mikage")
    QgsSettings().remove(qsettings.key(org_areas_panel.ON_MAP_KEY))
    QgsSettings().remove(qsettings.key(TAB_KEY))
    settings._set("local_gpkg", str(tmp_path / "local.gpkg"))
    monkeypatch.setenv("USERNAME", "tsudo")
    fake = FakeEsri()
    places = fake.add_layer(PLACES, FakeLayer(dict(FIELDS), "esriGeometryPolygon"))
    categories = fake.add_layer(CATEGORIES, FakeLayer(dict(CATEGORY_FIELDS), None))
    # 横須賀港 inside 東京湾 (a place name only); 長浦 apart; 旧港 stopped.
    places.add({"NAME": "東京湾", "KIND": "海域", "NAMED_AREA_ID": "NA-000001"}, box(139.0, 34.5, 140.5, 36.0))
    places.add({"NAME": "横須賀港", "NAMED_AREA_ID": "NA-000101", "AREA_USE": "両方", "CATEGORY_ID": "PORT", "NAME_KANA": "よこすかこう",
                "OWNER_GROUP": "第1班", "PRIORITY": 300}, box(139.65, 35.55, 139.70, 35.60))
    places.add({"NAME": "長浦", "NAMED_AREA_ID": "NA-000102", "AREA_USE": "関心地域", "CATEGORY_ID": "PORT"},
               box(140.05, 35.5, 140.1, 35.6))
    places.add({"NAME": "旧港", "NAMED_AREA_ID": "NA-000103", "AREA_USE": "関心地域", "CATEGORY_ID": "PORT", "ACTIVE": 0},
               box(141.05, 35.5, 141.1, 35.6))
    categories.add({"CATEGORY_ID": "PORT", "CATEGORY_NAME": "港湾", "SORT_ORDER": 1, "ACTIVE": 1})
    types = {n: S for n in DEFAULT_REQUEST_FIELDS.values()}
    types.update({n: "esriFieldTypeDate" for n in ("START_AT", "END_AT", "CREATED_AT", "UPDATED_AT")})
    types.update({n: "esriFieldTypeDouble" for n in ("PRIORITY", "BOOST", "OFF_NADIR_MIN", "OFF_NADIR_MAX", "INCIDENCE_MIN",
                                                     "INCIDENCE_MAX", "RESOLUTION_MAX", "CLOUD_NO_GO")})
    types[FOLLOW] = "esriFieldTypeSmallInteger"
    ledger = fake.add_layer(LEDGER, FakeLayer(types, "esriGeometryPolygon"))
    htypes = {n: S for n in DEFAULT_HISTORY_FIELDS.values()}
    htypes.update(RUN_DATE="esriFieldTypeDate", TARGET_DATE="esriFieldTypeDate", PLANNED_AT="esriFieldTypeDate")
    fake.add_layer(HISTORY, FakeLayer(htypes, "esriGeometryNone"))
    config = {"cloudForecast": False, "namedAreas": {"url": PLACES, "categories": CATEGORIES},
              "requestStores": [{"label": "撮像依頼", "url": LEDGER, "history": HISTORY}]}
    path = tmp_path / "config.json"
    path.write_text(json.dumps(config, ensure_ascii=False), encoding="utf-8")
    settings.set_config_source(str(path))
    monkeypatch.setattr(PluginContext, "esri", lambda self: EsriClient(fake))
    iface = FakeIface()
    iface.window.resize(1300, 900)
    p = small_plugin(iface, Images, Requests)
    p.initGui()
    for dock in p.docks.values():
        dock.hide()
    assert p.api.problems == [], p.api.problems
    yield p, iface, places, ledger
    p.unload()
    QgsProject.instance().removeAllMapLayers()
    QgsSettings().remove("mikage")
    QgsSettings().remove(qsettings.key(org_areas_panel.ON_MAP_KEY))
    QgsSettings().remove(qsettings.key(TAB_KEY))


def panel_of(p):
    dock = p.api.interests
    dock.show()
    dock.tabs.setCurrentWidget(dock.org)
    panel = dock.org
    assert wait_for(lambda: panel.book.list is not None and not panel.book.loading)
    return panel


def choose(panel, *names):
    panel.show_select.setCurrentIndex(panel.show_select.findData(org_areas_panel.SHOW_ALL))
    panel.way_select.setCurrentIndex(panel.way_select.findData(org_areas_panel.FLAT))
    panel.tree.clearSelection()
    tree = panel.tree
    items = {tree.topLevelItem(i).text(0): tree.topLevelItem(i) for i in range(tree.topLevelItemCount())}
    for n in names:
        items[n].setSelected(True)
    return items


def row_of(dock, name):
    return next(i for i in range(dock.model.rowCount()) if dock.model.item(i, 0).text() == name)


def test_the_org_tab_makes_requests_linked_to_its_areas(plugin):
    p, _iface, _places, ledger = plugin
    requests = p.api.requests
    panel = panel_of(p)
    # The ledger is read for the 「依頼」 column (none yet).
    assert wait_for(lambda: requests.loaded and not panel.tree.isColumnHidden(org_areas_panel.COLUMNS.index("依頼")))
    assert panel.buttons["request"].isVisibleTo(panel) and not panel.buttons["request"].isEnabled()

    # One area: the form with its name, shape, 担当グループ and 重要度, linked and following it.
    choose(panel, "横須賀港")
    assert panel.buttons["request"].isEnabled()
    form = panel.make_requests()
    assert form is requests.form and requests.isVisible()
    assert (form.name.text(), form.group_value(), form.priority.value()) == ("横須賀港", "第1班", 300)
    assert form.area_id == "NA-000101" and form.follow_check.isChecked() and "横須賀港" in form.link_label.text()
    assert abs(form.rings[0][0][0] - 139.65) < 1e-9
    form._save_clicked()
    assert wait_for(lambda: requests.task is None and len(requests.requests) == 1), requests.status.text()
    r = requests.requests[0]
    assert (r.named_area_id, r.area_follow, r.shape_kind, r.group) == ("NA-000101", True, "範囲", "第1班")
    assert ledger.rows[0]["attributes"][AREA] == "NA-000101" and ledger.rows[0]["attributes"][FOLLOW] == 1
    # The list: 「地域」 with ↻, and the area filter.
    assert requests.model.item(0, COLUMNS.index("地域")).text() == "↻ 横須賀港"
    assert requests.area_filter.isVisibleTo(requests) and requests.area_filter.findText("横須賀港") >= 0

    # The org tab counts the area's requests, and its 管理番号 is locked.
    assert wait_for(lambda: panel._items and panel.tree.findItems("横須賀港", org_areas_panel.Qt.MatchFlag.MatchExactly)[0]
                    .text(org_areas_panel.COLUMNS.index("依頼")) == "1")
    choose(panel, "横須賀港")
    assert panel.draft is not None and panel.draft.id == "NA-000101" and not panel.area_id.isEnabled()
    panel.area_id.setText("NA-000999")
    panel._form_edited()
    panel.save()
    assert "管理番号は変えられない" in panel.problem.text()

    # Several areas: one form, one request each; each keeps its own group and priority (長浦 has none: the form's).
    choose(panel, "横須賀港", "長浦")
    form = panel.make_requests()
    assert form.batch is not None and len(form.batch["items"]) == 2 and not form.link_row.isVisibleTo(form)
    form.priority.setValue(450)
    form._save_clicked()
    assert wait_for(lambda: requests.task is None and len(requests.requests) == 3), requests.status.text()
    made = {}
    for x in requests.requests:
        made.setdefault(x.named_area_id, []).append(x)
    assert len(made["NA-000101"]) == 2 and len(made["NA-000102"]) == 1
    nagaura = made["NA-000102"][0]
    assert nagaura.name == "長浦" and nagaura.area_follow and nagaura.priority == 450
    # 横須賀港's own (the form's value changed applies to all: 450 typed over the template's).
    assert all(x.group == "第1班" for x in made["NA-000101"])
    # Filtered by area.
    requests.area_filter.setCurrentIndex(requests.area_filter.findText("長浦"))
    assert requests.model.rowCount() == 1 and requests.model.item(0, 0).text() == "長浦"
    requests.area_filter.setCurrentIndex(0)

    # A stopped area makes none.
    choose(panel, "旧港")
    assert panel.make_requests() is None and "停止中" in panel.status.text()


def test_the_map_and_the_form_choose_an_area(plugin):
    p, iface, _places, _ledger = plugin
    requests = p.api.requests
    requests.show()
    assert wait_for(lambda: requests.loaded and requests.task is None)
    entries = p.feature(Requests).map_menu()
    assert "この地域で撮像依頼…" in entries

    # 横須賀港 inside 東京湾: both offered, the smaller first.
    requests.request_at(139.67, 35.57)
    assert wait_for(lambda: getattr(requests, "area_menu", None) is not None)
    labels = [a.text() for a in requests.area_menu.actions()]
    assert labels[0].startswith("横須賀港") and labels[1].startswith("東京湾") and "地名" in labels[1]
    requests.area_menu.actions()[1].trigger()
    assert wait_for(lambda: requests.form is not None and requests.form.name.text() == "東京湾")
    assert requests.form.area_id == "NA-000001"
    requests.form.close()
    # Nothing there: said.
    requests.request_at(150.0, 10.0)
    assert wait_for(lambda: any("地名・組織の関心地域なし" in w.text() for w in iface.bar.items()))

    # The form's 「地域から…」: found by よみがな, its name and shape taken.
    requests.edit(new=True)
    form = requests.form
    form.kind.setCurrentText("SAR")
    form.choose_area()
    dialog = form.area_dialog
    assert wait_for(lambda: dialog.tree.topLevelItemCount() > 0)
    dialog.search.setText("よこすか")
    assert dialog.tree.topLevelItemCount() == 1 and dialog.chosen() is not None
    dialog.accept()
    assert wait_for(lambda: form.area_id == "NA-000101")
    assert form.name.text() == "横須賀港" and form.follow_check.isChecked() and form.kind.currentText() == "SAR"
    # The area changed by hand: no longer following it (still linked).
    form._point_clicked(QgsPointXY(139.8, 35.4))
    assert not form.follow_check.isChecked() and form.area_id == "NA-000101" and form.shape_kind == "点"
    form._save_clicked()
    assert wait_for(lambda: requests.task is None and len(requests.requests) == 1)
    first = requests.requests[0]
    assert (first.named_area_id, first.area_follow, first.kind) == ("NA-000101", False, "SAR")

    # 「前回と同じ条件」: the area's last request's conditions (but the area's own group and priority).
    requests.edit(new=True)
    form = requests.form
    form.kind.setCurrentText("光学")
    form.choose_area()
    form.area_dialog.search.setText("NA-000101")
    form.area_dialog.accept()
    assert wait_for(lambda: form.area_id == "NA-000101")
    assert form.previous_button.isEnabled()
    form.same_as_before()
    assert form.kind.currentText() == "SAR" and form.group_value() == "第1班" and form.priority.value() == 300
    # 「つながりを外す」: the shape kept, no area.
    form.unlink_button.click()
    assert form.area_id == "" and not form.follow_check.isVisibleTo(form) and form.rings
    form.close()


def test_the_area_layer_imports_linked(plugin):
    p, _iface, _places, _ledger = plugin
    requests = p.api.requests
    panel = panel_of(p)
    panel.buttons["map"].click()
    layer = layer_of()
    assert layer is not None and wait_for(lambda: layer.featureCount() >= 2)
    requests.show()
    assert wait_for(lambda: requests.loaded and requests.task is None)
    form = requests.take_layer(layer, "name")
    assert form is not None and form.batch is not None
    form._save_clicked()
    assert wait_for(lambda: requests.task is None and len(requests.requests) == layer.featureCount()), requests.status.text()
    assert {r.named_area_id for r in requests.requests} >= {"NA-000101", "NA-000102"}
    assert all(r.area_follow for r in requests.requests if r.named_area_id)
