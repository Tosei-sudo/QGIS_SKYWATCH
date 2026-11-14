"""個人の関心地域・地点: the settings file's `interests`, each person's own rows in the point and area layers."""
from mikage.core.config import parse_config
from mikage.core.esri import EsriClient, EsriError
from mikage.core.interests import (
    AREA, DEFAULT_COLOR, DEFAULT_LABEL, POINT, delete_interests, load_interests, matches, new_interest, owner_where,
    save_interest, section_lines,
)
from mikage.tests.fake_esri import FakeEsri, FakeLayer

import pytest

POINTS = "https://x/arcgis/rest/services/MIKAGE/FeatureServer/25"
AREAS = "https://x/arcgis/rest/services/MIKAGE/FeatureServer/26"
S, T = "esriFieldTypeString", "esriFieldTypeDate"
FIELDS = {"INTEREST_ID": S, "INTEREST_NAME": S, "OWNER_USER": S, "COLOR": S, "NOTE": S,
          "CREATED_USER": S, "CREATED_DATE": T, "UPDATED_USER": S, "UPDATED_DATE": T}
RING = [[139.6, 35.2], [139.6, 35.4], [139.8, 35.4], [139.8, 35.2], [139.6, 35.2]]


def setup(**settings):
    fake = FakeEsri()
    points = fake.add_layer(POINTS, FakeLayer(dict(FIELDS), "esriGeometryPoint"))
    areas = fake.add_layer(AREAS, FakeLayer(dict(FIELDS), "esriGeometryPolygon"))
    config, problems = parse_config({"interests": {"points": POINTS, "areas": {"url": AREAS}, **settings}})
    assert problems == []
    return EsriClient(fake), config.interests, points, areas


def test_settings_are_written_their_own_way():
    c, problems = parse_config({"interests": {"points": POINTS}})
    assert problems == [] and c.interests.kinds == (POINT,) and c.interests.label == DEFAULT_LABEL
    assert c.interests.color == DEFAULT_COLOR and c.interests.points.fields["owner"] == "OWNER_USER"
    c, problems = parse_config({"interests": {"areas": {"url": AREAS, "fields": {"owner": "WHO", "bogus": "X"}},
                                              "label": "私の関心", "color": "teal", "fields": ["NAME"]}})
    assert c.interests.kinds == (AREA,) and c.interests.areas.fields["owner"] == "WHO" and c.interests.label == "私の関心"
    assert c.interests.color == "teal"
    assert any("bogus" in p for p in problems) and any("interests.fields" in p for p in problems)
    c, problems = parse_config({"interests": {}})
    assert c.interests is None and any("points" in p and "areas" in p for p in problems)
    c, problems = parse_config({"interests": {"points": "https://x/arcgis/rest/services/M/MapServer/0", "color": "#12"}})
    assert c.interests is None and any("interests.points" in p for p in problems)
    c, problems = parse_config({"interests": [POINTS]})
    assert c.interests is None and problems


def test_owner_condition_is_in_capitals_and_quoted():
    _client, s, _p, _a = setup()
    assert owner_where(s.points, "tsudo") == "UPPER(OWNER_USER) = 'TSUDO'"
    assert owner_where(s.points, "o'neil") == "UPPER(OWNER_USER) = 'O''NEIL'"


def test_each_person_reads_and_writes_only_their_own(monkeypatch):
    monkeypatch.setenv("USERNAME", "tsudo")
    client, s, points, areas = setup()
    port = save_interest(client, s, new_interest(POINT, s, name="横須賀本港", x=139.67, y=35.29, note="毎朝"), "tsudo")
    assert port.oid == 1 and port.owner == "tsudo"
    a = points.rows[0]["attributes"]
    assert a["INTEREST_NAME"] == "横須賀本港" and a["OWNER_USER"] == "tsudo" and a["COLOR"] == DEFAULT_COLOR and a["CREATED_USER"] == "tsudo"
    assert points.rows[0]["geometry"]["x"] == 139.67
    bay = save_interest(client, s, new_interest(AREA, s, name="東京湾口", rings=[RING], color="#2563eb"), "tsudo")
    assert areas.rows[0]["geometry"]["rings"] and areas.rows[0]["attributes"]["COLOR"] == "#2563eb"
    # Someone else's row in the same layer: never listed.
    points.add({"INTEREST_ID": "other", "INTEREST_NAME": "佐藤さんの港", "OWNER_USER": "sato"}, {"x": 139.0, "y": 35.0})
    mine = load_interests(client, s, "TSUDO")
    assert [(i.name, i.kind) for i in mine] == [("東京湾口", AREA), ("横須賀本港", POINT)]
    assert mine[1].note == "毎朝" and mine[1].x == 139.67 and mine[0].rings and mine[0].color == "#2563eb"
    # Updated: the same row (also when saved again after an answer was lost).
    save_interest(client, s, type(port)(**{**port.__dict__, "name": "横須賀本港（Y）"}), "tsudo")
    lost = type(bay)(**{**bay.__dict__, "oid": None, "note": "再送"})
    save_interest(client, s, lost, "tsudo")
    assert len(points.rows) == 2 and len(areas.rows) == 1 and areas.rows[0]["attributes"]["NOTE"] == "再送"
    assert points.rows[0]["attributes"]["INTEREST_NAME"] == "横須賀本港（Y）"
    done, failed = delete_interests(client, s, load_interests(client, s, "tsudo"))
    assert len(done) == 2 and failed == [] and areas.rows == []
    assert [r["attributes"]["OWNER_USER"] for r in points.rows] == ["sato"]


def test_what_cannot_be_saved_or_read():
    client, s, points, _areas = setup()
    with pytest.raises(ValueError):
        save_interest(client, s, new_interest(POINT, s, name=""), "tsudo")
    with pytest.raises(ValueError):
        save_interest(client, s, new_interest(AREA, s, name="形なし"), "tsudo")
    with pytest.raises(EsriError):
        load_interests(client, s, "")
    # The point layer made as polygons: told, not read wrong.
    fake = FakeEsri()
    fake.add_layer(POINTS, FakeLayer(dict(FIELDS), "esriGeometryPolygon"))
    c, _ = parse_config({"interests": {"points": POINTS}})
    with pytest.raises(EsriError, match="ポイント"):
        load_interests(EsriClient(fake), c.interests, "tsudo")
    fake = FakeEsri()
    fake.add_layer(POINTS, FakeLayer({"INTEREST_ID": S, "INTEREST_NAME": S}, "esriGeometryPoint"))
    with pytest.raises(EsriError, match="OWNER_USER"):
        load_interests(EsriClient(fake), c.interests, "tsudo")


def test_shapes_names_and_lines():
    _client, s, _p, _a = setup()
    p = new_interest(POINT, s, name="港", x=139.5, y=35.25)
    a = new_interest(AREA, s, name="湾", rings=[RING])
    assert p.shape_text() == "地点 139.50000, 35.25000" and a.shape_text() == "地域（頂点 4）"
    assert new_interest(AREA, s, name="x").shape_text() == "形なし"
    assert matches(p, "") and matches(p, "港") and not matches(p, "湾")
    assert matches(type(p)(**{**p.__dict__, "note": "Night Shift"}), "night")
    assert section_lines([p, a]) == [("港", DEFAULT_COLOR), ("湾", DEFAULT_COLOR)]
    # Outer rings go to Esri clockwise.
    anticlockwise = [[0, 0], [1, 0], [1, 1], [0, 1], [0, 0]]
    rings = new_interest(AREA, s, name="x", rings=[anticlockwise]).esri_geometry()["rings"]
    assert rings[0] == list(reversed(anticlockwise))
