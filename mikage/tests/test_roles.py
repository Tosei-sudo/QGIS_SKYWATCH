"""担当と使える機能: the roles, what each lets a person see, and the settings file's table of the people's roles."""
import pytest

from mikage.core import roles
from mikage.core.config import parse_config
from mikage.core.esri import EsriClient, EsriError
from mikage.tests.fake_esri import FakeEsri, FakeLayer

URL = "https://x/arcgis/rest/services/MIKAGE/FeatureServer/30"
S, T = "esriFieldTypeString", "esriFieldTypeDate"
FIELDS = {"USER_NAME": S, "ROLE_NAME": S, "CREATED_USER": S, "CREATED_DATE": T, "UPDATED_USER": S, "UPDATED_DATE": T}


def setup(value=URL):
    fake = FakeEsri()
    table = fake.add_layer(URL, FakeLayer(dict(FIELDS), ""))
    config, problems = parse_config({"userRoles": value})
    assert problems == []
    return EsriClient(fake), config.user_roles, table


def test_the_roles_and_their_codes():
    assert [r.code for r in roles.ROLES] == ["PLANNER", "REQUESTER", "OBSERVER", "INTERPRETER", "TRAINER", "ADMIN"]
    assert roles.clean_codes(["admin", " observer ", "NOPE", 3, "OBSERVER"]) == ["OBSERVER", "ADMIN"]
    assert roles.codes_of(["管理者", "画像判読", "夜間当番"]) == ["INTERPRETER", "ADMIN"]
    assert roles.names_of(["ADMIN", "PLANNER"]) == ["撮像計画の担当", "管理者"]
    # Each role has a line saying what it is for; every permission some role gives.
    assert all(r.hint for r in roles.ROLES)
    assert set().union(*(r.permissions for r in roles.ROLES)) == set(roles.PERMISSION_LABELS)


def test_what_each_role_sees():
    # Not listed: for everyone.
    assert roles.allows("画像カタログ", []) and roles.allows("画像カタログ", ["REQUESTER"])
    # No role: everything but the administrators'.
    assert roles.allows("撮像計画ファイル出力", []) and roles.allows("観測記録", [])
    assert not roles.allows("接続と表の点検…", []) and not roles.allows("observations.clear", [])
    # The person who asks for images does not plan, record or check the tables.
    asker = ["REQUESTER"]
    assert roles.allows("requests.edit", asker)
    assert not any(roles.allows(f, asker) for f in ("撮像計画ファイル出力", "schedule.answer", "観測記録", "接続と表の点検…"))
    # The observer does not write the plan files (tsudo: 「観測結果の担当が計画のシステム入力とかやらない」).
    observer = ["OBSERVER"]
    assert roles.allows("観測記録", observer) and roles.allows("分類体系の編集", observer)
    assert not roles.allows("撮像計画ファイル出力", observer) and not roles.allows("requests.edit", observer)
    # AI結果: the observers' and the trainers'; taking them into the records only the observers'.
    assert roles.allows("AI結果", ["TRAINER"]) and not roles.allows("ai_results.import", ["TRAINER"])
    # As many as the person likes: what any of them has.
    both = ["PLANNER", "ADMIN"]
    assert roles.allows("撮像計画ファイル出力", both) and roles.allows("監査ログ…", both) and not roles.allows("観測記録", both)
    # Unknown codes count as none.
    assert roles.allows("観測記録", ["NIGHT"]) and not roles.allows("監査ログ…", ["NIGHT"])
    # Every Processing tool that is a feature is listed.
    assert set(roles.ALGORITHM_FEATURES.values()) <= set(roles.FEATURES)


def test_a_feature_not_offered_says_which_roles_have_it():
    assert roles.refusal("撮像計画ファイル出力") == "担当「撮像計画の担当」の機能です（メニュー「担当と個人設定…」で担当を選ぶと使えます）"
    assert "「撮像計画の担当」「撮像を依頼する人」" in roles.refusal("requests.edit")
    assert roles.refusal("画像カタログ") == ""


def test_settings_of_the_table():
    c, problems = parse_config({"userRoles": URL})
    assert problems == [] and c.user_roles.url == URL and c.user_roles.fields == {"user": "USER_NAME", "role": "ROLE_NAME"}
    c, problems = parse_config({"userRoles": {"url": URL, "fields": {"user": "WHO"}, "where": "SYSTEM = 'A'"}})
    assert problems == [] and c.user_roles.fields["user"] == "WHO" and c.user_roles.fields["role"] == "ROLE_NAME"
    assert roles.user_where(c.user_roles, "tsudo") == "(SYSTEM = 'A') AND (UPPER(WHO) = 'TSUDO')"
    c, problems = parse_config({"userRoles": "https://x/arcgis/rest/services/M/MapServer/0"})
    assert c.user_roles is None and any("userRoles" in p for p in problems)
    c, problems = parse_config({})
    assert c.user_roles is None and problems == []


def test_each_person_s_roles_are_their_rows(monkeypatch):
    monkeypatch.setenv("USERNAME", "tsudo")
    client, source, table = setup()
    assert roles.load_user_roles(client, source, "tsudo") == []
    assert roles.save_user_roles(client, source, "tsudo", ["ADMIN", "PLANNER"]) == ["PLANNER", "ADMIN"]
    rows = sorted((r["attributes"]["USER_NAME"], r["attributes"]["ROLE_NAME"]) for r in table.rows)
    assert rows == [("tsudo", "ADMIN"), ("tsudo", "PLANNER")]
    assert table.rows[0]["attributes"]["CREATED_USER"] == "tsudo"  # editTracking
    # Another person's rows are not touched; the user name in any case.
    table.add({"USER_NAME": "SATO", "ROLE_NAME": "OBSERVER"})
    assert roles.load_user_roles(client, source, "TSUDO") == ["PLANNER", "ADMIN"]
    # Chosen again: what is no longer chosen goes, the new one comes; nothing sent when the same.
    assert roles.save_user_roles(client, source, "tsudo", ["PLANNER", "OBSERVER"]) == ["PLANNER", "OBSERVER"]
    assert roles.load_user_roles(client, source, "tsudo") == ["PLANNER", "OBSERVER"]
    count = len(table.rows)
    roles.save_user_roles(client, source, "tsudo", ["OBSERVER", "PLANNER"])
    assert len(table.rows) == count
    assert roles.load_user_roles(client, source, "sato") == ["OBSERVER"]
    # A row an administrator added twice, and one of a value this version does not know.
    table.add({"USER_NAME": "tsudo", "ROLE_NAME": "planner"})
    table.add({"USER_NAME": "tsudo", "ROLE_NAME": "NIGHT_SHIFT"})
    assert roles.load_user_roles(client, source, "tsudo") == ["PLANNER", "OBSERVER"]
    roles.save_user_roles(client, source, "tsudo", ["PLANNER"])
    mine = sorted(r["attributes"]["ROLE_NAME"] for r in table.rows if r["attributes"]["USER_NAME"].upper() == "TSUDO")
    assert mine == ["NIGHT_SHIFT", "PLANNER"] or mine == ["NIGHT_SHIFT", "planner"]
    # None: every known row of the person goes.
    roles.save_user_roles(client, source, "tsudo", [])
    assert roles.load_user_roles(client, source, "tsudo") == []


def test_the_table_without_its_attributes_or_without_a_user_name():
    fake = FakeEsri()
    fake.add_layer(URL, FakeLayer({"WHO": S}, ""))
    config, _ = parse_config({"userRoles": URL})
    client = EsriClient(fake)
    with pytest.raises(EsriError, match="USER_NAME・ROLE_NAME"):
        roles.load_user_roles(client, config.user_roles, "tsudo")
    with pytest.raises(EsriError, match="USERNAME"):
        roles.save_user_roles(client, config.user_roles, " ", ["ADMIN"])
