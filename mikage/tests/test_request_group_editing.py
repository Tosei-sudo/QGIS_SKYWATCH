"""
依頼グループの追加・編集 (tsudo, 2026-11-23: 「撮像依頼のグループをミカゲから追加、編集できる機能が欲しい」):
the checks, the rows written, adding and saving over (a name given twice, one deleted on another PC), the order,
deleting, the requests in each group, a new name carried to the ledger, and the user names offered as members.
"""
from __future__ import annotations

import time

import pytest

from mikage.core import edit_stamp
from mikage.core.esri import EsriClient, EsriError
from mikage.core.request.groups import (
    DEFAULT_GROUP_FIELDS, RequestGroup, check_group, delete_groups, group_attributes, group_counts, load_groups, members_text,
    order_changes, rename_requests, save_group, save_orders,
)
from mikage.core.request.store import DEFAULT_REQUEST_FIELDS, RequestStoreConfig
from mikage.core.roles import DEFAULT_USER_ROLE_FIELDS, known_users
from mikage.core.sources import EsriSource
from mikage.tests.fake_esri import FakeEsri, FakeLayer

F = DEFAULT_GROUP_FIELDS
GROUPS = "https://example.com/arcgis/rest/services/Req/FeatureServer/21"
LEDGER = "https://example.com/arcgis/rest/services/Req/FeatureServer/0"
ROLES = "https://example.com/arcgis/rest/services/Req/FeatureServer/30"
TYPES = {"GROUP_NAME": "esriFieldTypeString", "MEMBERS": "esriFieldTypeString", "SORT_ORDER": "esriFieldTypeInteger",
         "ACTIVE": "esriFieldTypeSmallInteger", "NOTE": "esriFieldTypeString", "CREATED_USER": "esriFieldTypeString",
         "UPDATED_USER": "esriFieldTypeString"}
RING = [[[139.8, 35.4], [139.9, 35.4], [139.9, 35.5], [139.8, 35.4]]]


@pytest.fixture
def esri(monkeypatch):
    monkeypatch.setattr(edit_stamp, "user_name", lambda: "tsudo")
    fake = FakeEsri()
    groups = fake.add_layer(GROUPS, FakeLayer(dict(TYPES), "esriGeometryNone"))
    for name, members, order, active in (("第1班", "tsudo, yamada", 1, 1), ("第2班", "sato", 2, 1), ("旧体制", "", 9, 0)):
        groups.add({"GROUP_NAME": name, "MEMBERS": members, "SORT_ORDER": order, "ACTIVE": active, "NOTE": None})
    types = {n: "esriFieldTypeString" for n in DEFAULT_REQUEST_FIELDS.values()}
    types.update({n: "esriFieldTypeDate" for n in ("START_AT", "END_AT", "CREATED_AT", "UPDATED_AT")})
    types.update({n: "esriFieldTypeDouble" for n in ("PRIORITY", "BOOST")})
    ledger = fake.add_layer(LEDGER, FakeLayer(types, "esriGeometryPolygon"))
    now = time.time() * 1000
    for rid, group in (("R-1", "第1班"), ("R-2", "第1班 "), ("R-3", "第2班"), ("R-4", None), ("R-5", "第１班")):
        ledger.add({"REQ_ID": rid, "REQ_NAME": rid, "PRIORITY": 500, "KIND": "光学", "CATEGORY": "臨時", "ACHIEVE": "期間内1回",
                    "STATUS": "有効", "START_AT": now, "END_AT": now + 86400000, "REQUEST_GROUP": group},
                   {"rings": RING, "spatialReference": {"wkid": 4326}})
    return fake, EsriClient(fake)


def table(fake):
    return [r["attributes"] for r in fake.layers[GROUPS].rows]


def test_the_checks():
    groups = [RequestGroup("第1班", object_ids=(1,)), RequestGroup("Team A", object_ids=(2,))]
    assert check_group(RequestGroup(" "), groups) == ["グループ名を入れる"]
    assert check_group(RequestGroup("x" * 256), groups) == ["グループ名は 255 文字まで"]
    # Another group's name, case and surrounding spaces aside; its own name is fine.
    assert check_group(RequestGroup(" team a "), groups)[0].startswith("同じ名前のグループ「Team A」がある")
    assert check_group(RequestGroup("TEAM A", object_ids=(2,)), groups) == []
    assert check_group(RequestGroup("第3班", users=("u" * 2000, "v" * 2000)), groups) == ["メンバーが長すぎる（カンマ区切りで 4000 文字まで）"]
    assert check_group(RequestGroup("第3班", note="n" * 1001), groups) == ["メモは 1000 文字まで"]
    assert members_text(("tsudo", "", "yamada")) == "tsudo, yamada"


def test_the_row_as_the_table_takes_it():
    g = RequestGroup(" 第3班 ", ("tsudo", "sato"), 4.0, False, " 港湾 ")
    assert group_attributes(g, F, TYPES, {}) == {"GROUP_NAME": "第3班", "MEMBERS": "tsudo, sato", "SORT_ORDER": 4, "ACTIVE": 0,
                                                 "NOTE": "港湾"}
    # 有効 kept in text, a table without some attributes, the table's lengths.
    text = {"GROUP_NAME": "esriFieldTypeString", "ACTIVE": "esriFieldTypeString"}
    assert group_attributes(RequestGroup("第3班 長い名前"), F, text, {"GROUP_NAME": 3}) == {"GROUP_NAME": "第3班", "ACTIVE": "1"}
    assert group_attributes(RequestGroup("x", order=2.5), F, {"SORT_ORDER": "esriFieldTypeDouble"}, {}) == {"SORT_ORDER": 2.5}


def test_added_saved_over_and_refused(esri):
    fake, client = esri
    new = save_group(client, GROUPS, F, RequestGroup("第3班", ("tsudo",), 10.0, True, "港湾"))
    assert new.object_ids == (4,)
    row = table(fake)[-1]
    assert (row["GROUP_NAME"], row["MEMBERS"], row["SORT_ORDER"], row["ACTIVE"], row["NOTE"]) == ("第3班", "tsudo", 10, 1, "港湾")
    assert row["CREATED_USER"] == "tsudo"
    # Saved over: changed in place (no new row).
    groups = load_groups(client, GROUPS, F, None)
    second = next(g for g in groups if g.name == "第2班")
    saved = save_group(client, GROUPS, F, RequestGroup("第二班", ("sato", "kato"), second.order, False, "", second.object_ids))
    assert len(table(fake)) == 4 and saved.object_ids == second.object_ids
    row = next(a for a in table(fake) if a["OBJECTID"] == second.object_ids[0])
    assert (row["GROUP_NAME"], row["MEMBERS"], row["ACTIVE"], row["NOTE"], row["UPDATED_USER"]) == ("第二班", "sato, kato", 0, None, "tsudo")
    # A name another group has (someone else may have just given it): refused, the table read again first.
    fake.layers[GROUPS].add({"GROUP_NAME": "第4班"})
    with pytest.raises(ValueError, match="同じ名前のグループ「第4班」"):
        save_group(client, GROUPS, F, RequestGroup(" 第4班"))
    # A group deleted on another PC is told, not added again.
    gone = RequestGroup("第3班", object_ids=new.object_ids)
    fake.layers[GROUPS].rows = [r for r in fake.layers[GROUPS].rows if r["attributes"]["OBJECTID"] != new.object_ids[0]]
    with pytest.raises(EsriError) as caught:
        save_group(client, GROUPS, F, gone)
    assert caught.value.code == "E302"
    # A table without the name's attribute.
    fake.add_layer(GROUPS + "0", FakeLayer({"NAME": "esriFieldTypeString"}, "esriGeometryNone"))
    with pytest.raises(EsriError) as caught:
        save_group(client, GROUPS + "0", F, RequestGroup("x"))
    assert caught.value.code == "E704"


def test_a_name_given_twice_is_saved_on_one_row(esri):
    fake, client = esri
    fake.layers[GROUPS].add({"GROUP_NAME": "第1班 ", "MEMBERS": "kato", "SORT_ORDER": 5, "ACTIVE": 1})
    first = next(g for g in load_groups(client, GROUPS, F, None) if g.name == "第1班")
    assert first.object_ids == (1, 4) and first.users == ("tsudo", "yamada", "kato")
    saved = save_group(client, GROUPS, F, RequestGroup("第1班", ("tsudo", "kato"), 1.0, True, "", first.object_ids))
    assert saved.object_ids == (1,)
    assert [a["GROUP_NAME"] for a in table(fake)] == ["第1班", "第2班", "旧体制"]
    assert table(fake)[0]["MEMBERS"] == "tsudo, kato"


def test_put_in_order():
    a, b, c = RequestGroup("A", order=1.0), RequestGroup("B", order=2.0), RequestGroup("C")
    # C (no number) up: numbered 1, 2, 3 in the new order; only those that change.
    assert [(g.name, g.order) for g in order_changes([a, b, c], 2, -1)] == [("C", 2.0), ("B", 3.0)]
    assert [(g.name, g.order) for g in order_changes([a, b, c], 0, 1)] == [("B", 1.0), ("A", 2.0), ("C", 3.0)]
    assert order_changes([a, b, c], 0, -1) == [] and order_changes([a, b, c], 2, 1) == [] and order_changes([], 0, 1) == []


def test_the_order_saved(esri):
    fake, client = esri
    groups = load_groups(client, GROUPS, F, None)
    changes = order_changes(groups, 1, -1)
    assert [(g.name, g.order) for g in changes] == [("第2班", 1.0), ("第1班", 2.0), ("旧体制", 3.0)]
    assert save_orders(client, GROUPS, F, changes + [RequestGroup("未保存", order=3.0)], None) == (3, ["未保存: 未保存"])
    assert [g.name for g in load_groups(client, GROUPS, F, None)] == ["第2班", "第1班", "旧体制"]
    without = {k: v for k, v in TYPES.items() if k != "SORT_ORDER"}
    fake.add_layer(GROUPS + "0", FakeLayer(without, "esriGeometryNone"))
    assert save_orders(client, GROUPS + "0", F, changes, None) == (0, ["表示順の項目がない（管理者に連絡: SORT_ORDER）"])


def test_deleted(esri):
    fake, client = esri
    groups = load_groups(client, GROUPS, F, None)
    old = next(g for g in groups if g.name == "旧体制")
    assert delete_groups(client, GROUPS, [old, RequestGroup("未保存")], None) == (["旧体制"], ["未保存: 未保存"])
    assert [a["GROUP_NAME"] for a in table(fake)] == ["第1班", "第2班"]
    # Deleted on another PC already.
    assert delete_groups(client, GROUPS, [old], None) == ([], ["旧体制: 削除失敗"])


def test_the_requests_in_each_group_and_a_new_name(esri):
    fake, client = esri
    store = RequestStoreConfig(label="撮像依頼", url=LEDGER, groups=GROUPS)
    # Only the group is read; names as the filters compare them (case and surrounding spaces aside).
    assert group_counts(client, store, None) == {"第1班": 2, "第2班": 1, "": 1, "第１班": 1}
    query = next(form for url, form in reversed(fake.asked) if url == LEDGER + "/query")
    assert query["outFields"] == "OBJECTID,REQUEST_GROUP" and query["returnGeometry"] == "false"
    # Renamed: the ledger's requests of it in the new group (the other groups' left as they are).
    result = rename_requests(client, store, None, "第1班", " 第一班 ", lambda: time.time() * 1000)
    assert len(result.saved) == 2 and result.failed == []
    groups = {r["attributes"]["REQ_ID"]: r["attributes"]["REQUEST_GROUP"] for r in fake.layers[LEDGER].rows}
    assert groups == {"R-1": "第一班", "R-2": "第一班", "R-3": "第2班", "R-4": None, "R-5": "第１班"}
    # The same name in another case: nothing to carry.
    assert rename_requests(client, store, None, "第2班", "第2班 ", lambda: 0).saved == []


def test_the_user_names_offered_as_members():
    fake = FakeEsri()
    layer = fake.add_layer(ROLES, FakeLayer({"USER_NAME": "esriFieldTypeString", "ROLE_NAME": "esriFieldTypeString"}, "esriGeometryNone"))
    for user, role in (("tsudo", "PLANNER"), ("Tsudo", "ADMIN"), ("yamada", "REQUESTER"), ("", "OBSERVER"), ("ito", "OBSERVER")):
        layer.add({"USER_NAME": user, "ROLE_NAME": role})
    source = EsriSource(url=ROLES, fields=dict(DEFAULT_USER_ROLE_FIELDS))
    assert known_users(EsriClient(fake), source) == ["ito", "tsudo", "yamada"]
    other = EsriSource(url=ROLES, fields={"user": "PERSON", "role": "ROLE_NAME"})
    assert known_users(EsriClient(fake), other) == []
