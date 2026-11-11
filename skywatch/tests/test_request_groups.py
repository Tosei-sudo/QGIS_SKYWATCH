"""依頼グループ (tsudo, 2026-11-11): the groups table, the filter's choices, the ledger and history attributes, and where
the group goes (form memory, bulk edit, layers, the day's filter, the systems' files)."""
from __future__ import annotations

import json
import math
from datetime import date

from skywatch.core.config import parse_config_text
from skywatch.core.request.bulk import RequestChange, changed
from skywatch.core.request.daily import HELD, TAKEN, Candidate
from skywatch.core.request.groups import (
    DEFAULT_GROUP_FIELDS, MINE, NO_GROUP, RequestGroup, filter_text, first_group, groups_of, listed, load_groups, matches, mine,
    offered, read_groups, same, users_of,
)
from skywatch.core.request.last import remembered, with_remembered
from skywatch.core.request.model import ImagingRequest
from skywatch.core.request.rows import field_roles, request_of_row, row_of
from skywatch.core.request.store import (
    DEFAULT_HISTORY_FIELDS, DEFAULT_REQUEST_FIELDS, HistoryRow, attributes_of, history_attributes, history_row_of, request_of,
    request_store_of,
)
from skywatch.core.request.view import CandidateFilter, shown

F = DEFAULT_GROUP_FIELDS
GROUPS = "https://example.com/arcgis/rest/services/Req/FeatureServer/2"


def rows():
    return [
        {"GROUP_NAME": "第2班", "MEMBERS": "sato", "SORT_ORDER": 2, "ACTIVE": 1, "NOTE": "臨時"},
        {"GROUP_NAME": "第1班", "MEMBERS": "tsudo, Yamada\nkato；ito", "SORT_ORDER": 1, "ACTIVE": 1},
        {"GROUP_NAME": "旧体制", "MEMBERS": None, "SORT_ORDER": None, "ACTIVE": 0},
        {"GROUP_NAME": "  ", "MEMBERS": "nobody"},
        # The same name again (any case): one group, its people added.
        {"GROUP_NAME": "第2班 ", "MEMBERS": "SATO, tsudo", "SORT_ORDER": 7, "ACTIVE": 0},
        {"GROUP_NAME": "解析", "MEMBERS": "", "SORT_ORDER": "3", "ACTIVE": "停止"},
    ]


def test_the_table_rows_are_groups_in_their_order():
    groups = groups_of(rows(), F)
    assert [g.name for g in groups] == ["第1班", "第2班", "解析", "旧体制"]
    first, second, analysis, old = groups
    assert first.users == ("tsudo", "Yamada", "kato", "ito")
    assert second.users == ("sato", "tsudo") and second.active and second.note == "臨時" and second.order == 2
    assert not analysis.active and analysis.order == 3
    assert not old.active and old.order is None
    assert users_of(" a,b、 c  a ") == ("a", "b", "c")
    assert same(" 第1班", "第1班 ") and same("Team A", "team a") and not same("A", "B")


def test_mine_and_the_filter():
    groups = groups_of(rows(), F)
    assert mine(groups, "TSUDO") == ["第1班", "第2班"]
    assert mine(groups, "") == [] and mine(groups, "someone") == []
    my = mine(groups, "tsudo")
    assert matches("第1班", None) and matches("", None) and matches("x", "")
    assert matches(" 第1班 ", MINE, my) and matches("第2班", MINE, my) and not matches("解析", MINE, my) and not matches("", MINE, my)
    assert matches("", NO_GROUP) and matches("  ", NO_GROUP) and not matches("第1班", NO_GROUP)
    assert matches("team a", "Team A") and not matches("第1班", "第2班")
    assert [filter_text(v) for v in (None, MINE, NO_GROUP, "第1班")] == ["すべてのグループ", "自分のグループ", "グループなし", "第1班"]


def test_choices_listed_offered_and_the_first_group():
    groups = groups_of(rows(), F)
    # The filter: the table's groups, then names only found on requests (by name, once each).
    assert listed(groups, ["第1班", "", "臨時班", "Z班", "臨時班 ", "第1班"]) == ["第1班", "第2班", "解析", "旧体制", "Z班", "臨時班"]
    # The form: the groups in use, and the request's own one even when no longer used.
    assert offered(groups) == ["第1班", "第2班"]
    assert offered(groups, "旧体制") == ["第1班", "第2班", "旧体制"] and offered(groups, "第2班") == ["第1班", "第2班"]
    # A new request: the group last saved while it is in use, else the user's own.
    assert first_group(groups, "tsudo", "第2班") == "第2班"
    assert first_group(groups, "tsudo", "第2班 ") == "第2班"
    assert first_group(groups, "tsudo", "旧体制") == "第1班"
    assert first_group(groups, "sato", "") == "第2班"
    assert first_group(groups, "nobody", "解析") == ""
    # Without a table, whatever was last typed.
    assert first_group([], "tsudo", "臨時班", table=False) == "臨時班" and first_group([], "tsudo", "", table=False) == ""


class Client:
    """The groups table as EsriClient answers it."""

    def __init__(self, fields=None, fail=None):
        self.fields = fields or {n: "esriFieldTypeString" for n in F.values()}
        self.fail = fail
        self.asked = []

    def layer(self, url, token=None, refresh=False):
        if self.fail:
            raise self.fail
        from types import SimpleNamespace
        return SimpleNamespace(field_types=self.fields)

    def query_all(self, url, where="1=1", token=None, geometry=True, limit=0, cancelled=None, **_):
        self.asked.append((url, where, geometry))
        return [{"attributes": a} for a in rows()]


def test_loading_the_table():
    client = Client()
    assert [g.name for g in load_groups(client, GROUPS, F, "t")] == ["第1班", "第2班", "解析", "旧体制"]
    assert client.asked == [(GROUPS, "1=1", False)]
    assert load_groups(client, None, F, None) == [] and read_groups(client, None, F, None) == ([], "")
    groups, note = read_groups(Client({"NAME": "esriFieldTypeString"}), GROUPS, F, None)
    assert groups is None and "GROUP_NAME" in note
    from skywatch.core.esri import UnreachableError
    groups, note = read_groups(Client(fail=UnreachableError("つながらない")), GROUPS, F, None)
    assert groups is None and note == "依頼グループのテーブルを読めず（つながらない）"


def test_the_settings_name_the_table_and_its_attributes():
    problems = []
    store = request_store_of({"url": "https://g/x/FeatureServer/0", "groups": GROUPS + "?f=json",
                              "groupFields": {"name": "TEAM", "users": "PEOPLE", "order": "bad name!"}}, problems, 0)
    assert store.groups == GROUPS
    assert store.group_fields == {**F, "name": "TEAM", "users": "PEOPLE"}
    assert problems == ["requestStores[0].groupFields.order は属性名で指定"]
    problems = []
    store = request_store_of({"url": "https://g/x/FeatureServer/0", "groups": "https://g/x/MapServer/3"}, problems, 0)
    assert store.groups == "https://g/x/MapServer/3" and problems == []
    store = request_store_of({"url": "https://g/x/FeatureServer/0", "groups": "groups.csv"}, problems, 0)
    assert store.groups is None and "groups は依頼グループのテーブルの URL" in problems[0]
    assert request_store_of({"url": "https://g/x/FeatureServer/0"}, [], 0).groups is None
    config, problems = parse_config_text(json.dumps({"requestStores": [{"url": "https://g/x/FeatureServer/0", "groups": GROUPS}]}))
    assert problems == [] and config.request_stores[0].groups == GROUPS


def test_the_ledger_and_the_history_keep_the_group():
    assert DEFAULT_REQUEST_FIELDS["group"] == "REQUEST_GROUP" and DEFAULT_HISTORY_FIELDS["group"] == "REQUEST_GROUP"
    r = ImagingRequest(id="R-1", name="東京湾", group="第1班", start=0.0, end=1.0)
    a = attributes_of(r, DEFAULT_REQUEST_FIELDS)
    assert a["REQUEST_GROUP"] == "第1班"
    assert attributes_of(ImagingRequest(id="R-2"), DEFAULT_REQUEST_FIELDS)["REQUEST_GROUP"] is None
    back = request_of({**a, "REQUEST_GROUP": " 第1班 ", "OBJECTID": 3}, None, DEFAULT_REQUEST_FIELDS, "OBJECTID")
    assert back.group == "第1班"
    row = HistoryRow("RUN", 0.0, "2026-11-11", "R-1", group="第1班", name="東京湾")
    h = history_attributes(row, DEFAULT_HISTORY_FIELDS)
    assert h["REQUEST_GROUP"] == "第1班"
    assert history_row_of(h, None, DEFAULT_HISTORY_FIELDS, "OBJECTID").group == "第1班"
    assert history_attributes(HistoryRow("RUN", 0.0, "2026-11-11", "R-2"), DEFAULT_HISTORY_FIELDS)["REQUEST_GROUP"] is None


def test_the_written_day_names_the_group():
    from skywatch.core.request.systems import export_system_of
    from skywatch.core.request.workflow import DayPlan, history_of

    system = export_system_of({"system": "SYS-A", "format": "csv", "columns": [{"key": "ID", "source": "id"}]}, [], 0)
    r = ImagingRequest(id="R-1", name="東京湾", group="第1班", start=0.0, end=math.inf)
    taken = Candidate(r, system=system, result=TAKEN)
    held = Candidate(ImagingRequest(id="R-2", name="横浜"), result=HELD, reason="枠なし")
    plan = DayPlan(date(2026, 11, 11), [taken, held], [system])
    out = history_of(plan, 0.0, {"SYS-A": "a.csv"})
    assert [(h.request_id, h.group) for h in out] == [("R-1", "第1班"), ("R-2", "")]


def test_the_day_filtered_by_group():
    a = Candidate(ImagingRequest(id="R-1", name="東京湾", group="第1班"), result=TAKEN)
    b = Candidate(ImagingRequest(id="R-2", name="大阪湾", group="第2班"), result=HELD)
    c = Candidate(ImagingRequest(id="R-3", name="横浜"), result=HELD)
    cands = [a, b, c]
    assert shown(cands, CandidateFilter()) == cands
    assert shown(cands, CandidateFilter(group="第2班")) == [b]
    assert shown(cands, CandidateFilter(group=MINE, mine=("第1班",))) == [a]
    assert shown(cands, CandidateFilter(group=MINE)) == []
    assert shown(cands, CandidateFilter(group=NO_GROUP)) == [c]
    # Words find the group too.
    assert shown(cands, CandidateFilter(text="2班")) == [b]


def test_the_group_is_remembered_bulk_changed_and_goes_through_layers():
    last = remembered(ImagingRequest(id="R-1", group="第1班", requester="運用"))
    assert last["group"] == "第1班"
    assert with_remembered(ImagingRequest(id="new"), last).group == "第1班"
    assert with_remembered(ImagingRequest(id="new"), {"group": 3}).group == ""

    r = ImagingRequest(id="R-1", name="東京湾", group="第1班")
    assert changed(r, RequestChange(group="第2班")).group == "第2班"
    assert changed(r, RequestChange(group=" 第1班 ")) is None
    assert changed(r, RequestChange(group="")).group == ""
    change = RequestChange(group="")
    assert not change.empty and change.text() == "依頼グループ なし" and RequestChange(group="第2班").text() == "依頼グループ 第2班"

    row = row_of(r)
    assert row["REQUEST_GROUP"] == "第1班"
    roles = field_roles(list(row))
    assert roles["group"] == "REQUEST_GROUP"
    assert field_roles(["名称", "依頼グループ"])["group"] == "依頼グループ"
    back = request_of_row(row, roles, [[[0, 0], [1, 0], [1, 1], [0, 0]]], "範囲", r)
    assert back.group == "第1班"
    # A layer without the item (written out before groups) leaves the request's group as it is.
    no_group = {k: v for k, v in row.items() if k != "REQUEST_GROUP"}
    assert request_of_row(no_group, field_roles(list(no_group)), [], "範囲", r).group == "第1班"
    assert request_of_row(no_group, field_roles(list(no_group)), [], "範囲").group == ""
    # The item given empty takes the request out of its group.
    assert request_of_row({**row, "REQUEST_GROUP": None}, roles, [], "範囲", r).group == ""


def test_the_systems_files_can_carry_the_group():
    from skywatch.core.request import export

    assert "group" in export.SOURCES and "part" in export.SOURCES
    assert RequestGroup("x").has("") is False
