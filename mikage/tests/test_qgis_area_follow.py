"""
地域の形に合わせる in QGIS (組織の関心地域, tsudo 2026-11-24): an area saved
with a new shape in the 「組織」 tab moves its requests at once (the results
window, its undo), an area changed elsewhere is marked in the 「撮像依頼」 list
and followed before 「計算」 (only when the layer was edited since), and the
rows handed out with the area before say so in 「撮像予定」.
"""
import json
import time

import pytest

pytest.importorskip("qgis.core")
from qgis.testing import start_app  # noqa: E402

APP = start_app()

from qgis.core import QgsProject, QgsSettings  # noqa: E402

from mikage.qgis_io import qsettings  # noqa: E402
from mikage.core.esri import EsriClient  # noqa: E402
from mikage.core.obs.alerts import DEFAULT_PLACE_FIELDS  # noqa: E402
from mikage.core.request.model import jst_day  # noqa: E402
from mikage.core.request.store import DEFAULT_AREA_CHANGE_FIELDS, DEFAULT_HISTORY_FIELDS, DEFAULT_REQUEST_FIELDS  # noqa: E402
from mikage.features.images import Images  # noqa: E402
from mikage.features.plan import Plan  # noqa: E402
from mikage.features.requests import Requests  # noqa: E402
from mikage.qgis_io import settings  # noqa: E402
from mikage.qgis_io.context import PluginContext  # noqa: E402
from mikage.tests.fake_esri import FakeEsri, FakeLayer  # noqa: E402
from mikage.tests.feature_harness import small_plugin  # noqa: E402
from mikage.tests.qgis_fakes import FakeIface, wait_for  # noqa: E402
from mikage.tests.test_plan import LINE1, LINE2  # noqa: E402
from mikage.tests.test_qgis_area_requests import choose, panel_of  # noqa: E402
from mikage.ui import org_areas_panel  # noqa: E402
from mikage.ui.interests_dock import TAB_KEY  # noqa: E402
from mikage.ui.request_dock import COLUMNS  # noqa: E402
from mikage.ui.schedule_dock import COLUMNS as SCHEDULE_COLUMNS  # noqa: E402

PLACES = "https://example.com/arcgis/rest/services/Alerts/FeatureServer/3"
LEDGER = "https://example.com/arcgis/rest/services/Req/FeatureServer/0"
HISTORY = "https://example.com/arcgis/rest/services/Req/FeatureServer/1"
CHANGES = "https://example.com/arcgis/rest/services/Req/FeatureServer/34"
SATS = "https://example.com/arcgis/rest/services/Sats/FeatureServer/0"
S = "esriFieldTypeString"
RING = [[139.65, 35.55], [139.65, 35.60], [139.70, 35.60], [139.70, 35.55], [139.65, 35.55]]
BIGGER = [[139.65, 35.55], [139.65, 35.60], [139.72, 35.60], [139.72, 35.55], [139.65, 35.55]]
AREA_COLUMN = COLUMNS.index("地域")


@pytest.fixture
def plugin(tmp_path, monkeypatch, request):
    QgsProject.instance().removeAllMapLayers()
    QgsSettings().remove("mikage")
    QgsSettings().remove(qsettings.key(org_areas_panel.ON_MAP_KEY))
    QgsSettings().remove(qsettings.key(TAB_KEY))
    settings._set("local_gpkg", str(tmp_path / "local.gpkg"))
    monkeypatch.setenv("USERNAME", "tsudo")
    fake = FakeEsri()
    places = fake.add_layer(PLACES, FakeLayer({**{v: S for v in DEFAULT_PLACE_FIELDS.values()}, "ACTIVE": "esriFieldTypeSmallInteger",
                                               "PRIORITY": "esriFieldTypeInteger"}, "esriGeometryPolygon"))
    places.extra["editingInfo"] = {"lastEditDate": 1000}
    places.add({"NAME": "横須賀港", "NAMED_AREA_ID": "NA-000101", "AREA_USE": "関心地域"}, {"rings": [RING]})
    places.add({"NAME": "長浦", "NAMED_AREA_ID": "NA-000102", "AREA_USE": "関心地域"},
               {"rings": [[[140.05, 35.5], [140.05, 35.6], [140.1, 35.6], [140.1, 35.5], [140.05, 35.5]]]})
    types = {n: S for n in DEFAULT_REQUEST_FIELDS.values()}
    types.update({n: "esriFieldTypeDate" for n in ("START_AT", "END_AT", "CREATED_AT", "UPDATED_AT")})
    types.update({n: "esriFieldTypeDouble" for n in ("PRIORITY", "BOOST", "OFF_NADIR_MIN", "OFF_NADIR_MAX", "INCIDENCE_MIN",
                                                     "INCIDENCE_MAX", "RESOLUTION_MAX", "CLOUD_NO_GO")})
    types[DEFAULT_REQUEST_FIELDS["areaFollow"]] = "esriFieldTypeSmallInteger"
    ledger = fake.add_layer(LEDGER, FakeLayer(types, "esriGeometryPolygon"))
    htypes = {n: S for n in DEFAULT_HISTORY_FIELDS.values()}
    htypes.update(RUN_DATE="esriFieldTypeDate", TARGET_DATE="esriFieldTypeDate", PLANNED_AT="esriFieldTypeDate", CLOUD="esriFieldTypeDouble",
                  SCENES="esriFieldTypeInteger", BOOST_DONE="esriFieldTypeInteger", PART_NO="esriFieldTypeInteger",
                  OCCURRENCE="esriFieldTypeInteger")
    history = fake.add_layer(HISTORY, FakeLayer(htypes, "esriGeometryPolygon"))
    changes = fake.add_layer(CHANGES, FakeLayer({**{v: S for v in DEFAULT_AREA_CHANGE_FIELDS.values()}, "CREATED_DATE": "esriFieldTypeDate",
                                                 "CREATED_USER": S, "UPDATED_DATE": "esriFieldTypeDate", "UPDATED_USER": S},
                                                "esriGeometryPolygon"))
    sats = fake.add_layer(SATS, FakeLayer({n: S for n in ("NAME", "TLE", "KIND", "SERIES")}))
    sats.add({"NAME": "OPT 1", "TLE": f"{LINE1}\n{LINE2}", "KIND": "光学", "SERIES": "OPT"})
    config = {"cloudForecast": False, "namedAreas": {"url": PLACES},
              "satelliteCatalogs": [{"label": "衛星", "url": SATS, "fields": {"name": "NAME", "tle": "TLE", "kind": "KIND", "series": "SERIES"}}],
              "requestStores": [{"label": "撮像依頼", "url": LEDGER, "history": HISTORY, "areaChanges": CHANGES}],
              "requestExports": [{"system": "SYS-A", "label": "A", "kind": "光学", "limit": {"count": 5}, "orbit": False, "format": "csv",
                                  "columns": [{"key": "ID", "source": "id"}]}]}
    path = tmp_path / "config.json"
    path.write_text(json.dumps(config, ensure_ascii=False), encoding="utf-8")
    settings.set_config_source(str(path))
    monkeypatch.setattr(PluginContext, "esri", lambda self: EsriClient(fake))
    iface = FakeIface()
    iface.window.resize(1300, 900)
    p = small_plugin(iface, Images, Plan, Requests, request=request)
    p.initGui()
    for dock in p.docks.values():
        dock.hide()
    assert p.api.problems == [], p.api.problems
    yield p, places, ledger, history, changes
    p.unload()
    QgsProject.instance().removeAllMapLayers()
    QgsSettings().remove("mikage")
    QgsSettings().remove(qsettings.key(org_areas_panel.ON_MAP_KEY))
    QgsSettings().remove(qsettings.key(TAB_KEY))


def make_request(p, name="横須賀港", follow=True):
    """A request made from an area in the 「組織」 tab (following its shape unless `follow` is False)."""
    requests = p.api.requests
    panel = panel_of(p)
    assert wait_for(lambda: requests.loaded)
    before = len(requests.requests)
    choose(panel, name)
    form = panel.make_requests()
    form.follow_check.setChecked(follow)
    form._save_clicked()
    assert wait_for(lambda: requests.task is None and len(requests.requests) == before + 1), requests.status.text()
    return next(r for r in requests.requests if r.name == name and r.area_follow == follow)


def redraw(panel, name, rings):
    choose(panel, name)
    assert panel.draft is not None and panel.draft.name == name
    panel._tool_for = "redraw"
    panel._drawn(rings[0][:-1])
    assert panel.shape_changed


def test_an_area_saved_with_a_new_shape_moves_its_requests_at_once(plugin):
    p, places, ledger, _history, changes = plugin
    requests = p.api.requests
    following = make_request(p)
    kept = make_request(p, follow=False)
    panel = panel_of(p)
    redraw(panel, "横須賀港", [BIGGER])
    panel.save()
    # Saved, then its requests: the one following takes the shape, the other is only shown.
    assert wait_for(lambda: getattr(requests, "follow_dialog", None) is not None), panel.status.text()
    assert wait_for(lambda: requests.task is None and "地域の形に合わせた依頼 1 件" in requests.status.text()), requests.status.text()
    # Said in the tab too (kept with the counts while the area stays chosen).
    assert wait_for(lambda: panel.status.text().startswith("「横須賀港」を保存。地域の形に合わせた依頼 1 件")), panel.status.text()
    dialog = requests.follow_dialog
    tree = dialog.tree
    shown = sorted((tree.topLevelItem(i).text(1), tree.topLevelItem(i).text(3)) for i in range(tree.topLevelItemCount()))
    assert shown == [("形が違う", "横須賀港"), ("更新", "横須賀港")]
    assert wait_for(lambda: {r.id: r for r in requests.requests}[following.id].rings[0][2][0] == 139.72)
    by_id = {r.id: r for r in requests.requests}
    assert by_id[kept.id].rings[0][2][0] == 139.70 and not by_id[kept.id].area_follow
    # Logged with the area before.
    (row,) = changes.rows
    assert row["attributes"]["CHANGE_ACTION"] == "更新" and row["geometry"]["rings"][0][2][0] == 139.70
    assert row["attributes"]["CREATED_USER"] == "tsudo"
    # The one not following is marked in the list (its area's shape changed).
    assert wait_for(lambda: requests.model.item(next(i for i, r in enumerate(requests.shown) if r.id == kept.id), AREA_COLUMN)
                    .text().startswith("⚠"))
    item = requests.model.item(next(i for i, r in enumerate(requests.shown) if r.id == following.id), AREA_COLUMN)
    assert item.text() == "↻ 横須賀港"

    # Undone from the window: the area before, no longer following.
    tree.setCurrentItem(next(tree.topLevelItem(i) for i in range(tree.topLevelItemCount()) if tree.topLevelItem(i).text(1) == "更新"))
    assert dialog.undo_button.isEnabled()
    assert dialog.undo(ask=False)
    assert wait_for(lambda: dialog.task is None and requests.task is None and "元に戻した" in dialog.status.text()), dialog.status.text()
    assert wait_for(lambda: not {r.id: r for r in requests.requests}[following.id].area_follow)
    back = {r.id: r for r in requests.requests}[following.id]
    assert back.rings[0][2][0] == 139.70
    assert [r["attributes"]["CHANGE_ACTION"] for r in changes.rows] == ["更新", "元に戻す"]
    assert any(tree.topLevelItem(i).text(1) == "更新（元に戻した）" for i in range(tree.topLevelItemCount()))
    dialog.close()

    # 「範囲の変更記録…」: the log, newest first; the undo is the request's last change, so the 更新 is not undone again.
    assert requests.changes_button.isVisibleTo(requests)
    log = requests.area_changes()
    assert wait_for(lambda: log.task is None and log.tree.topLevelItemCount() == 2), log.status.text()
    actions = sorted(log.tree.topLevelItem(i).text(1) for i in range(2))
    assert actions == ["元に戻す", "更新"]
    for i in range(2):
        log.tree.setCurrentItem(log.tree.topLevelItem(i))
        assert not log.undo_button.isEnabled()
    log.search.setText("長浦")
    assert log.tree.topLevelItemCount() == 0
    log.close()
    assert ledger.rows


def test_an_area_changed_elsewhere_is_marked_and_followed_before_the_calculation(plugin):
    p, places, _ledger, _history, changes = plugin
    requests = p.api.requests
    following = make_request(p)
    # Changed in ArcGIS Pro: the layer's last edit time moves.
    places.rows[0]["geometry"] = {"rings": [BIGGER]}
    places.extra["editingInfo"] = {"lastEditDate": 2000}
    book = p.api.interests.org.book
    book.load()
    assert wait_for(lambda: not book.loading)
    requests.reload()
    assert wait_for(lambda: requests.task is None and requests.model.rowCount() == 1)
    assert wait_for(lambda: requests.model.item(0, AREA_COLUMN).text().startswith("⚠")), requests.model.item(0, AREA_COLUMN).text()
    assert "次の「計算」" in requests.model.item(0, AREA_COLUMN).toolTip()

    # 「計算」: the request takes the area's shape first (and is in the day's plan with it).
    daily = p.api.plan_files
    daily.show()
    daily.run()
    assert wait_for(lambda: daily.plan is not None and daily.task is None), daily.status.text()
    assert "地域の形に合わせた依頼 1 件" in daily.status.text()
    assert [r["attributes"]["CHANGE_ACTION"] for r in changes.rows] == ["更新"]
    taken = next(c for c in daily.plan.candidates if c.request.id == following.id)
    assert taken.request.rings[0][2][0] == 139.72
    # The list shows it as read there: no longer marked.
    assert wait_for(lambda: requests.model.item(0, AREA_COLUMN).text() == "↻ 横須賀港")
    stamp, checked = settings.area_follow_stamp(f"{LEDGER}|{PLACES}")
    assert stamp == 2000 and checked is not None

    # Again without an edit of the layer: the areas are not read again.
    asked = []
    query = EsriClient.query_all

    def counting(self, url, *args, **kw):
        if url == PLACES:
            asked.append(url)
        return query(self, url, *args, **kw)

    EsriClient.query_all = counting
    try:
        daily.plan = None
        daily.run()
        assert wait_for(lambda: daily.plan is not None and daily.task is None), daily.status.text()
    finally:
        EsriClient.query_all = query
    assert asked == [] and "地域の形に合わせた" not in daily.status.text()
    assert len(changes.rows) == 1


def test_a_row_handed_out_with_the_area_before_is_a_candidate_to_write_again(plugin):
    p, places, _ledger, history, _changes = plugin
    requests = p.api.requests
    following = make_request(p)
    today = jst_day(time.time() * 1000).isoformat()
    for adoption in ("不明", "採用"):
        history.add({"RUN_ID": "RUN", "TARGET_DAY": today, "REQ_ID": following.id, "REQ_NAME": following.name + adoption,
                     "RESULT": "採用", "CAPTURED": "未判定", "SYSTEM": "SYS-A", "ADOPTION": adoption, "NAMED_AREA_ID": "NA-000101"},
                    {"rings": [RING]})
    # The area changes and its request follows it (saved in the 「組織」 tab).
    panel = panel_of(p)
    redraw(panel, "横須賀港", [BIGGER])
    panel.save()
    assert wait_for(lambda: requests.task is None and "地域の形に合わせた依頼 1 件" in requests.status.text()), requests.status.text()
    schedule = p.api.schedule
    schedule.show()
    schedule.reload(refresh=True)
    assert wait_for(lambda: schedule.loaded and schedule.model.rowCount() == 2), schedule.status.text()
    notes = {schedule.model.item(i, SCHEDULE_COLUMNS.index("名称")).text(): schedule.model.item(i, SCHEDULE_COLUMNS.index("理由")).text()
             for i in range(2)}
    assert notes[following.name + "不明"].startswith("出し直し候補")
    assert notes[following.name + "採用"].startswith("システムには前の形で採用済み")


def test_ticking_follow_again_takes_the_areas_shape(plugin):
    p, places, _ledger, _history, _changes = plugin
    requests = p.api.requests
    kept = make_request(p, follow=False)
    places.rows[0]["geometry"] = {"rings": [BIGGER]}
    book = p.api.interests.org.book
    book.load()
    assert wait_for(lambda: not book.loading)
    requests.edit(request=kept)
    form = requests.form
    assert form.area_id == "NA-000101" and not form.follow_check.isChecked()
    form.follow_check.click()
    assert wait_for(lambda: form.rings and form.rings[0][2][0] == 139.72), form.message.text()
    assert "今の形" in form.message.text()
    form.close()


def test_the_last_look_is_kept_per_store_and_layer(plugin):
    settings.set_area_follow_stamp("a|b", 10.0, 20.0)
    settings.set_area_follow_stamp("c|d", 30.0, 40.0)
    assert settings.area_follow_stamp("a|b") == (10.0, 20.0) and settings.area_follow_stamp("c|d") == (30.0, 40.0)
    assert settings.area_follow_stamp("x|y") == (None, None)
    settings._set("area_follow/stamps", "not json")
    assert settings.area_follow_stamp("a|b") == (None, None)
