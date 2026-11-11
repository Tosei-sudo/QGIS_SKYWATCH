"""
依頼グループ in QGIS (tsudo, 2026-11-11): the groups table read with the ledger, the filters of the 「撮像依頼」,
「撮像計画ファイル出力」 and 「撮像予定」 docks (自分のグループ by the table's members), the form's group and the bulk edit.
"""
import json
import time

import pytest

pytest.importorskip("qgis.core")
from qgis.testing import start_app  # noqa: E402

APP = start_app()

from qgis.core import QgsPointXY, QgsProject, QgsSettings  # noqa: E402

from skywatch.core.esri import EsriClient  # noqa: E402
from skywatch.core.request.bulk import RequestChange  # noqa: E402
from skywatch.core.request.groups import MINE, NO_GROUP  # noqa: E402
from skywatch.core.request.model import jst_day  # noqa: E402
from skywatch.core.request.store import DEFAULT_HISTORY_FIELDS, DEFAULT_REQUEST_FIELDS, HistoryRow, history_attributes  # noqa: E402
from skywatch.qgis_io import settings  # noqa: E402
from skywatch.qgis_io.context import PluginContext  # noqa: E402
from skywatch.tests.fake_esri import FakeEsri, FakeLayer  # noqa: E402
from skywatch.tests.qgis_fakes import FakeIface, wait_for  # noqa: E402
from skywatch.tests.test_plan import LINE1, LINE2  # noqa: E402

SATS = "https://example.com/arcgis/rest/services/Sats/FeatureServer/0"
LEDGER = "https://example.com/arcgis/rest/services/Req/FeatureServer/0"
HISTORY = "https://example.com/arcgis/rest/services/Req/FeatureServer/1"
GROUPS = "https://example.com/arcgis/rest/services/Req/FeatureServer/2"
TOKYO = [[[139.8, 35.45], [139.9, 35.45], [139.9, 35.55], [139.8, 35.55], [139.8, 35.45]]]

CONFIG = {
    "cloudForecast": False,
    # The user name the plugin goes by (as USERNAME): a member of 第1班.
    "editTracking": {"user": "Tsudo"},
    "satelliteCatalogs": [{"label": "衛星", "url": SATS, "fields": {"name": "NAME", "tle": "TLE", "kind": "KIND", "series": "SERIES"}}],
    "requestStores": [{"label": "撮像依頼", "url": LEDGER, "history": HISTORY, "groups": GROUPS}],
    "requestExports": [{"system": "SYS-A", "label": "A", "kind": "光学", "series": ["OPT"], "format": "csv",
                        "columns": [{"key": "ID", "source": "id"}, {"key": "班", "source": "group"}]}],
}


@pytest.fixture
def plugin(tmp_path, monkeypatch):
    QgsSettings().remove("skywatch")
    config = tmp_path / "config.json"
    config.write_text(json.dumps(CONFIG, ensure_ascii=False), encoding="utf-8")
    settings.set_config_source(str(config))
    fake = FakeEsri()
    sats = fake.add_layer(SATS, FakeLayer({n: "esriFieldTypeString" for n in ("NAME", "TLE", "KIND", "SERIES")}))
    sats.add({"NAME": "OPT 1", "TLE": f"{LINE1}\n{LINE2}", "KIND": "光学", "SERIES": "OPT"})
    types = {n: "esriFieldTypeString" for n in DEFAULT_REQUEST_FIELDS.values()}
    types.update({n: "esriFieldTypeDate" for n in ("START_AT", "END_AT", "CREATED_AT", "UPDATED_AT")})
    types.update({n: "esriFieldTypeDouble" for n in ("PRIORITY", "BOOST")})
    ledger = fake.add_layer(LEDGER, FakeLayer(types, "esriGeometryPolygon"))
    now = time.time() * 1000
    for rid, name, group in (("R-1", "東京湾", "第1班"), ("R-2", "大阪湾", "第2班"), ("R-3", "横浜", None), ("R-4", "呉", "旧体制")):
        ledger.add({"REQ_ID": rid, "REQ_NAME": name, "PRIORITY": 500, "KIND": "光学", "CATEGORY": "臨時", "ACHIEVE": "期間内1回",
                    "STATUS": "有効", "START_AT": now - 86400000, "END_AT": now + 5 * 86400000, "REQUEST_GROUP": group},
                   {"rings": TOKYO, "spatialReference": {"wkid": 4326}})
    htypes = {n: "esriFieldTypeString" for n in DEFAULT_HISTORY_FIELDS.values()}
    htypes.update(RUN_DATE="esriFieldTypeDate", TARGET_DATE="esriFieldTypeDate", PLANNED_AT="esriFieldTypeDate", CLOUD="esriFieldTypeDouble",
                  SCENES="esriFieldTypeInteger", BOOST_DONE="esriFieldTypeInteger")
    history = fake.add_layer(HISTORY, FakeLayer(htypes, "esriGeometryPolygon"))
    groups = fake.add_layer(GROUPS, FakeLayer({"GROUP_NAME": "esriFieldTypeString", "MEMBERS": "esriFieldTypeString",
                                               "SORT_ORDER": "esriFieldTypeInteger", "ACTIVE": "esriFieldTypeSmallInteger",
                                               "NOTE": "esriFieldTypeString"}, "esriGeometryNone"))
    for name, members, order, active in (("第2班", "sato", 2, 1), ("第1班", "tsudo, yamada", 1, 1), ("旧体制", "", 9, 0)):
        groups.add({"GROUP_NAME": name, "MEMBERS": members, "SORT_ORDER": order, "ACTIVE": active, "NOTE": f"{name}の依頼"})
    from skywatch import classFactory

    iface = FakeIface()
    p = classFactory(iface)
    monkeypatch.setattr(PluginContext, "esri", lambda self: EsriClient(fake))
    p.initGui()
    for dock in p.docks.values():
        dock.hide()
    assert p.api.problems == [], p.api.problems
    yield p, fake, history
    p.unload()
    QgsProject.instance().removeAllMapLayers()
    QgsSettings().remove("skywatch")


def names(dock):
    return sorted(r.name for r in dock.shown)


def choices(box):
    return [(box.itemText(i), box.itemData(i)) for i in range(box.count())]


def test_the_requests_dock_filters_and_the_form_chooses_a_group(plugin):
    p, fake, history = plugin
    dock = p.api.requests
    dock.show()
    assert wait_for(lambda: dock._loaded and dock.task is None), dock.status.text()
    assert dock.groups_read and [g.name for g in dock.groups] == ["第1班", "第2班", "旧体制"]
    box = dock.group_filter
    assert box.isVisibleTo(dock) and box.my == ("第1班",)
    assert choices(box) == [("すべてのグループ", None), ("自分のグループ", MINE), ("第1班", "第1班"), ("第2班", "第2班"),
                            ("旧体制（使用停止）", "旧体制"), ("グループなし", NO_GROUP)]
    assert len(dock.shown) == 4
    heads = [dock.model.horizontalHeaderItem(c).text() for c in range(dock.model.columnCount())]
    assert heads[:2] == ["名称", "依頼グループ"]
    row = next(i for i, r in enumerate(dock.shown) if r.name == "大阪湾")
    assert dock.model.item(row, 1).text() == "第2班"

    box.choose(MINE)
    assert names(dock) == ["東京湾"] and settings.group_filter("requests") == MINE
    box.choose("第2班")
    assert names(dock) == ["大阪湾"]
    box.choose(NO_GROUP)
    assert names(dock) == ["横浜"]
    box.choose("旧体制")
    assert names(dock) == ["呉"]
    # Kept in the list's 「ビュー」 too.
    assert dock.views.state()["filters"]["group"] == {"data": "旧体制"}
    box.choose(None)
    assert len(dock.shown) == 4 and settings.group_filter("requests") == ""

    # A new request starts in the user's own group; only the groups in use are offered.
    dock.edit(new=True)
    form = dock.form
    assert [form.group.itemText(i) for i in range(form.group.count())] == ["（グループなし）", "第1班", "第2班"]
    assert not form.group.isEditable() and form.group_value() == "第1班"
    form.name.setText("千葉港")
    form.group.setCurrentIndex(form.group.findData("第2班"))
    form._point_clicked(QgsPointXY(140.08, 35.58))
    form._save_clicked()
    assert wait_for(lambda: dock.task is None and len(dock.requests) == 5), dock.status.text()
    saved = next(r for r in fake.layers[LEDGER].rows if r["attributes"]["REQ_NAME"] == "千葉港")
    assert saved["attributes"]["REQUEST_GROUP"] == "第2班"
    # The next new one starts in the group last saved.
    dock.edit(new=True)
    assert dock.form.group_value() == "第2班"
    dock.form.close()

    # A request of a group no longer used keeps it in the form.
    dock.chosen = next(r for r in dock.requests if r.name == "呉")
    dock.edit()
    form = dock.form
    assert form.group.currentText() == "旧体制（使用停止・テーブルにない）" and form.group_value() == "旧体制"
    assert form.value().group == "旧体制"
    form.close()

    # Bulk edit: the groups to choose from, 「（グループなし）」 taking requests out of theirs.
    dock.select([r.id for r in dock.requests if r.name in ("横浜", "呉")])
    dialog = dock.bulk_edit()
    assert dialog.inputs["group"].itemText(0) == "（グループなし）"
    dialog.reject()
    chosen = dock.selected()
    assert dock.apply_bulk(chosen, RequestChange(group="第1班"))
    assert wait_for(lambda: dock.task is None and "依頼グループ 第1班" not in dock.status.text() and "件を変更" in dock.status.text()), \
        dock.status.text()
    groups = {r["attributes"]["REQ_NAME"]: r["attributes"].get("REQUEST_GROUP") for r in fake.layers[LEDGER].rows}
    assert groups["横浜"] == groups["呉"] == "第1班"
    box.choose(MINE)
    assert names(dock) == ["呉", "東京湾", "横浜"]


def test_the_day_and_the_schedule_filter_by_group(plugin):
    p, fake, history = plugin
    daily = p.api.plan_files
    daily.show()
    daily.group_filter.choose(MINE)
    assert daily.group_filter.value() == MINE and settings.group_filter("plan_files") == MINE
    # A choice kept from last time is there when the dock opens, before the groups are read.
    from skywatch.ui.request_groups import GroupFilter
    again = GroupFilter("plan_files")
    assert again.value() == MINE and not again.isHidden() and again.passes("") is False
    again.deleteLater()
    daily.run()
    assert wait_for(lambda: daily.plan is not None and daily.task is None), daily.status.text()
    assert len(daily.plan.candidates) == 4
    assert sorted(c.request.name for c in daily.shown) == ["東京湾"]
    col = [daily.model.horizontalHeaderItem(c).text() for c in range(daily.model.columnCount())].index("依頼グループ")
    assert daily.model.item(0, col).text() == "第1班"
    daily.group_filter.choose(None)
    assert len(daily.shown) == 4
    # The group filter only narrows what is shown: writing still goes by system.
    assert daily.target() is None

    # The history keeps the group as written; rows written before groups use the ledger's.
    today = jst_day(time.time() * 1000).isoformat()
    now = time.time() * 1000
    for rid, name, group in (("R-1", "東京湾", "第1班"), ("R-2", "大阪湾", ""), ("R-3", "横浜", "")):
        row = HistoryRow("RUN1", now, today, rid, system="SYS-A", result="採用", name=name, group=group, captured="未判定",
                         adoption="不明", rings=TOKYO)
        history.add({k: v for k, v in history_attributes(row, DEFAULT_HISTORY_FIELDS).items() if v is not None},
                    {"rings": TOKYO, "spatialReference": {"wkid": 4326}})
    schedule = p.api.schedule
    schedule.show()
    # (Shown as a tab beside the plan-file dock: read as a click on its tab would.)
    wait_for(lambda: schedule.task is None)
    schedule.reload(refresh=True)
    assert wait_for(lambda: schedule._loaded and schedule.task is None and len(schedule.rows) == 3), schedule.status.text()
    assert schedule.group_of(next(r for r in schedule.rows if r.name == "大阪湾")) == "第2班"
    schedule.group_filter.choose("第2班")
    assert names(schedule) == ["大阪湾"]
    schedule.group_filter.choose(MINE)
    assert names(schedule) == ["東京湾"]
    schedule.group_filter.choose(NO_GROUP)
    assert names(schedule) == ["横浜"]
    assert "（表示 1 件）" in schedule.status.text()
    heads = [schedule.model.horizontalHeaderItem(c).text() for c in range(schedule.model.columnCount())]
    assert heads[:3] == ["状態", "名称", "依頼グループ"]
    layer = schedule.to_layer()
    assert [f["依頼グループ"] for f in layer.getFeatures()] == [""]
    schedule.group_filter.choose("第2班")
    layer = schedule.to_layer()
    assert [f["依頼グループ"] for f in layer.getFeatures()] == ["第2班"]


def test_without_a_table_the_group_is_typed(plugin, tmp_path):
    p, fake, history = plugin
    config = json.loads(json.dumps(CONFIG))
    del config["requestStores"][0]["groups"]
    path = tmp_path / "plain.json"
    path.write_text(json.dumps(config, ensure_ascii=False), encoding="utf-8")
    settings.set_config_source(str(path))
    p.context.reload()
    dock = p.api.requests
    dock.show()
    assert wait_for(lambda: dock._loaded and dock.task is None and dock.requests), dock.status.text()
    # The groups found on the requests are still there to filter by; none is the user's own.
    assert [d for _t, d in choices(dock.group_filter)] == [None, "旧体制", "第1班", "第2班", NO_GROUP]
    dock.edit(new=True)
    form = dock.form
    assert form.group.isEditable() and form.group_value() == ""
    form.group.setEditText(" 臨時班 ")
    assert form.value().group == "臨時班"
    form.close()


def test_a_groups_table_that_cannot_be_read_leaves_the_requests(plugin):
    p, fake, history = plugin
    del fake.layers[GROUPS]
    dock = p.api.requests
    dock.show()
    assert wait_for(lambda: dock._loaded and dock.task is None and dock.requests), dock.status.text()
    assert len(dock.requests) == 4 and "依頼グループのテーブルを読めず" in dock.status.text()
    assert not dock.groups_read and not dock.known()["groupTable"]
    assert [d for _t, d in choices(dock.group_filter)] == [None, "旧体制", "第1班", "第2班", NO_GROUP]
