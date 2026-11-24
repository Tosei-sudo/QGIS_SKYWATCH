"""組織の関心地域 (2026-11-24): the named area layer's new attributes, 管理番号 given on saving, and the categories."""
import pytest

from mikage.core.config import parse_config
from mikage.core.esri import EsriClient
from mikage.core.named_areas import (
    USE_BOTH, USE_INTEREST, USE_PLACE, AreaCategory, OrgArea, aliases_of, area_color, area_use_of, category_counts,
    category_order_changes, category_tree, check_area, check_category, delete_categories, duplicate_ids, load_areas, next_number,
    number_blanks, rename_category_in_areas, save_area, save_category, section_lines, settings_for,
)
from mikage.core.obs.alerts import DEFAULT_PLACE_FIELDS, named_areas_of_features
from mikage.tests.fake_esri import FakeEsri, FakeLayer

AREAS = "https://x/arcgis/rest/services/Alerts/FeatureServer/3"
CATEGORIES = "https://x/arcgis/rest/services/Alerts/FeatureServer/5"
S, I, T = "esriFieldTypeString", "esriFieldTypeInteger", "esriFieldTypeDate"
FIELDS = {**{v: S for v in DEFAULT_PLACE_FIELDS.values()}, "ACTIVE": "esriFieldTypeSmallInteger", "PRIORITY": I,
          "CREATED_USER": S, "CREATED_DATE": T, "UPDATED_USER": S, "UPDATED_DATE": T}
CATEGORY_FIELDS = {"CATEGORY_ID": S, "CATEGORY_NAME": S, "CATEGORY_NAME_EN": S, "PARENT_ID": S, "SORT_ORDER": I, "COLOR": S,
                   "ACTIVE": "esriFieldTypeSmallInteger", "NOTE": S}


def box(w, s, e, n):
    return {"rings": [[[w, s], [w, n], [e, n], [e, s], [w, s]]]}


def setup(**more):
    fake = FakeEsri()
    areas = fake.add_layer(AREAS, FakeLayer(dict(FIELDS), "esriGeometryPolygon"))
    categories = fake.add_layer(CATEGORIES, FakeLayer(dict(CATEGORY_FIELDS), None))
    areas.add({"NAME": "東京湾", "KIND": "海域", "NAMED_AREA_ID": "NA-000001", "ACTIVE": 1}, box(139.6, 35.0, 140.1, 35.7))
    areas.add({"NAME": "横須賀港", "KIND": "港", "NAMED_AREA_ID": "NA-000101", "AREA_USE": "両方", "NAME_KANA": "よこすかこう",
               "NAME_ALIAS": "Yokosuka、横須賀", "CATEGORY_ID": "PORT", "PARENT_ID": "NA-000001", "PRIORITY": 300, "COUNTRY_CODE": "JP"},
              box(139.70, 35.26, 139.80, 35.34))
    areas.add({"NAME": "長浦港", "AREA_USE": "関心地域", "CATEGORY_ID": "PORT-NAVAL", "ACTIVE": 1}, box(139.735, 35.300, 139.750, 35.312))
    areas.add({"NAME": "旧地名", "ACTIVE": 1}, box(139.0, 35.0, 139.1, 35.1))
    categories.add({"CATEGORY_ID": "PORT", "CATEGORY_NAME": "港湾", "SORT_ORDER": 1, "COLOR": "#0ea5e9", "ACTIVE": 1})
    categories.add({"CATEGORY_ID": "PORT-NAVAL", "CATEGORY_NAME": "軍港", "PARENT_ID": "PORT", "SORT_ORDER": 1, "ACTIVE": 1})
    categories.add({"CATEGORY_ID": "AIRPORT", "CATEGORY_NAME": "空港", "SORT_ORDER": 2, "COLOR": "#a855f7", "ACTIVE": 1})
    config, problems = parse_config({"namedAreas": {"url": AREAS, "categories": CATEGORIES, **more}})
    assert problems == []
    return EsriClient(fake), config.named_areas, areas, categories


def test_settings_and_their_fallback():
    c, problems = parse_config({"namedAreas": AREAS})
    assert problems == [] and c.named_areas.areas.url == AREAS and c.named_areas.categories is None and c.named_areas.own
    assert c.named_areas.id_text(123) == "NA-000123"
    c, problems = parse_config({"namedAreas": {"url": AREAS, "idPrefix": "AOI_", "idDigits": 4, "label": "部の地域", "nope": 1}})
    assert c.named_areas.id_text(7) == "AOI_0007" and c.named_areas.label == "部の地域" and any("nope" in p for p in problems)
    assert c.named_areas.id_number("aoi_0012") == 12 and c.named_areas.id_number("NA-000012") is None
    c, problems = parse_config({"namedAreas": {"url": AREAS, "idDigits": 0, "idPrefix": "名-"}})
    assert c.named_areas.id_digits == 6 and c.named_areas.id_prefix == "NA-" and len(problems) == 2
    # Without namedAreas: the 通報's layer (and its countries), read only for what it is.
    c, _ = parse_config({"alertReports": {"namedAreas": AREAS, "countries": "https://x/arcgis/rest/services/Alerts/MapServer/4",
                                          "formats": [{"name": "f", "fields": ["place"]}]}})
    s = settings_for(c)
    assert s.areas.url == AREAS and not s.own and s.countries.url.endswith("MapServer/4")
    assert settings_for(parse_config({})[0]) is None


def test_use_keeps_areas_of_interest_out_of_the_places():
    assert area_use_of(None) == area_use_of("") == area_use_of("?") == USE_PLACE
    assert area_use_of(" 関心地域 ") == USE_INTEREST and area_use_of("Both") == USE_BOTH
    rows = [{"attributes": {"NAME": "港", "AREA_USE": "両方"}, "geometry": box(0, 0, 1, 1)},
            {"attributes": {"NAME": "関心", "AREA_USE": "関心地域"}, "geometry": box(0, 0, 1, 1)},
            {"attributes": {"NAME": "昔の地名"}, "geometry": box(0, 0, 1, 1)}]
    assert [a.name for a in named_areas_of_features(rows, DEFAULT_PLACE_FIELDS)] == ["港", "昔の地名"]


def test_the_rows_are_read_with_the_shapes_of_the_areas_of_interest_only():
    client, s, _areas, _c = setup()
    got = load_areas(client, s)
    by = {a.name: a for a in got.areas}
    assert set(by) == {"東京湾", "横須賀港", "長浦港", "旧地名"} and got.areas[0].name == "横須賀港"  # by よみがな, else name
    port = by["横須賀港"]
    assert port.is_interest and port.is_place and port.aliases == ("Yokosuka", "横須賀") and port.priority == 300
    assert port.has_shape and by["長浦港"].has_shape and by["東京湾"].rings is None
    assert by["旧地名"].use == USE_PLACE and not by["旧地名"].is_interest and by["長浦港"].key == "#3"
    assert [c.id for c in got.categories] == ["PORT", "PORT-NAVAL", "AIRPORT"] and got.editable
    assert port.matches("yokosuka") and port.matches("こすか") and port.matches("na-000101") and not port.matches("成田")
    # Colours: its own, else its category's, else the parent category's.
    assert area_color(port, got.categories) == "#0ea5e9" and area_color(by["長浦港"], got.categories) == "#0ea5e9"
    assert area_color(OrgArea(color="#123456"), got.categories) == "#123456"
    assert section_lines([by["長浦港"], port], got.categories) == [("横須賀港（港湾）", "#0ea5e9"), ("長浦港（軍港）", "#0ea5e9")]


def test_a_new_area_is_numbered_on_saving(monkeypatch):
    monkeypatch.setenv("USERNAME", "tsudo")
    client, s, areas, _c = setup()
    ring = box(139.74, 35.30, 139.75, 35.31)["rings"]
    saved = save_area(client, s, OrgArea(name="新しい岸壁", rings=ring, category="PORT-NAVAL", aliases=("岸壁A",), priority=50))
    assert saved.id == "NA-000102" and saved.oid == 5
    a = areas.rows[-1]["attributes"]
    assert a["NAMED_AREA_ID"] == "NA-000102" and a["AREA_USE"] == "関心地域" and a["NAME_ALIAS"] == "岸壁A" and a["PRIORITY"] == 50
    assert a["CREATED_USER"] == "tsudo" and areas.rows[-1]["geometry"]["rings"]
    # A number some other row has is refused (case aside); one typed is kept.
    with pytest.raises(ValueError, match="横須賀港"):
        save_area(client, s, OrgArea(name="別", id="na-000101", rings=ring))
    typed = save_area(client, s, OrgArea(name="手で番号", id="PORT-9", rings=ring))
    assert typed.id == "PORT-9"
    # An update without a new shape sends no geometry (a place name's border is never read and sent back).
    sent = []
    real = client.apply_edits
    monkeypatch.setattr(client, "apply_edits", lambda url, **kw: sent.append(kw) or real(url, **kw))
    save_area(client, s, OrgArea(oid=1, id="NA-000001", name="東京湾", use=USE_PLACE, kind="海域"), shape_changed=False)
    assert "geometry" not in sent[0]["updates"][0] and areas.rows[0]["geometry"]["rings"]


def test_two_pcs_numbering_at_once_get_two_numbers(monkeypatch):
    client, s, areas, _c = setup()
    ring = box(0, 0, 1, 1)["rings"]
    theirs = areas.add({"NAME": "他の PC の地域"}, box(0, 0, 1, 1))  # saved a moment before, not numbered yet
    real = client.apply_edits

    def racing(url, **kw):
        result = real(url, **kw)
        # As this PC writes NA-000102, the other PC gives its own row the same number.
        ups = kw.get("updates") or []
        if ups and ups[0]["attributes"].get("NAMED_AREA_ID") == "NA-000102" and ups[0]["attributes"]["OBJECTID"] != theirs:
            next(r for r in areas.rows if r["attributes"]["OBJECTID"] == theirs)["attributes"]["NAMED_AREA_ID"] = "NA-000102"
        return result

    monkeypatch.setattr(client, "apply_edits", racing)
    saved = save_area(client, s, OrgArea(name="同時", rings=ring))
    # The row with the smaller object id (theirs) keeps NA-000102; this one takes the next.
    assert saved.oid == theirs + 1 and saved.id == "NA-000103"
    assert duplicate_ids([(r["attributes"]["OBJECTID"], r["attributes"].get("NAMED_AREA_ID") or "") for r in areas.rows]) == []
    assert duplicate_ids([(5, "NA-000102"), (6, "na-000102"), (1, "X")]) == ["NA-000102"]


def test_every_row_without_a_number_is_numbered():
    client, s, areas, _c = setup()
    assert next_number(s, ["NA-000101", "X-9", "", "NA-7"]) == 102
    done, problems = number_blanks(client, s)
    assert done == 2 and problems == []
    ids = {r["attributes"]["NAME"]: r["attributes"]["NAMED_AREA_ID"] for r in areas.rows}
    assert ids["長浦港"] == "NA-000102" and ids["旧地名"] == "NA-000103"
    assert number_blanks(client, s) == (0, [])


def test_checks_before_saving():
    others = [OrgArea(oid=1, id="NA-1", name="A")]
    assert check_area(OrgArea(name=""), others) == ["名前が必要", "形が必要（地図に描くか、選んだ地物から）"]
    assert check_area(OrgArea(oid=1, id="NA-1", name="A"), others) == []
    assert "管理番号「NA-1」は「A」が使用中" in check_area(OrgArea(oid=2, id="na-1", name="B"), others)
    assert check_area(OrgArea(oid=2, id="X 1", name="B", parent="X 1"), [])[0].startswith("管理番号に空白")
    assert aliases_of("a, b、 c ;A\nd") == ("a", "b", "c", "d")


def test_categories_tree_order_and_delete(monkeypatch):
    client, s, _areas, categories = setup()
    got = load_areas(client, s)
    cats = got.categories
    assert [(d, c.id) for d, c in category_tree(cats)] == [(0, "PORT"), (1, "PORT-NAVAL"), (0, "AIRPORT")]
    counts = category_counts(got.areas)
    assert counts == {"port": 1, "port-naval": 1}
    # Moving AIRPORT up among the tops: numbered again 1, 2.
    changes = category_order_changes(cats, 2, -1)
    assert [(c.id, c.order) for c in changes] == [("AIRPORT", 1.0), ("PORT", 2.0)]
    assert category_order_changes(cats, 1, -1) == []
    # Checks: id form, unique, not under itself.
    assert check_category(AreaCategory(id="port", name="x"), cats)[0] == "ID「port」は使用中"
    assert "上位に自分の下のカテゴリーは選べない" in check_category(AreaCategory(id="PORT", name="港湾", parent="PORT-NAVAL"), cats, cats[0])
    assert check_category(AreaCategory(id="日本", name="x"), cats)[0].startswith("ID は英数字")
    # Saved, renamed (its areas follow), deleted only when unused.
    new = save_category(client, s.categories, AreaCategory(id="NAVAL", name="海軍", parent="PORT"))
    assert new.oid == 4 and categories.rows[-1]["attributes"]["PARENT_ID"] == "PORT"
    renamed = save_category(client, s.categories, AreaCategory(id="PORT-NAVY", name="軍港"), original=cats[1])
    assert renamed.oid == cats[1].oid
    assert rename_category_in_areas(client, s, "PORT-NAVAL", "PORT-NAVY") == 1
    gone, problems = delete_categories(client, s.categories, [cats[0], new], category_counts(load_areas(client, s).areas))
    assert gone == ["NAVAL"] and "港湾: 地域が 1 件ある" in problems[0]
