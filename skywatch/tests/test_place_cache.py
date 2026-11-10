"""地名・国の控え (place_cache): squares looked up once, polygons read once, kept on this PC, read again when the layer changes."""
import math
import random

import pytest

from skywatch.core.esri import EsriClient, EsriError
from skywatch.core.obs.alerts import DEFAULT_COUNTRY_FIELDS, DEFAULT_PLACE_FIELDS, EsriSource, NamedArea, inside_rings, places_of
from skywatch.core.obs.place_cache import CHECK_SECONDS, KEEP_SECONDS, PlaceCache, cell_of, runs_of
from skywatch.tests.fake_esri import FakeEsri, FakeLayer

URL = "https://x/arcgis/rest/services/Alerts/FeatureServer/3"
COUNTRY_URL = "https://x/arcgis/rest/services/Alerts/FeatureServer/20"


def box(w, s, e, n):
    return [[[w, s], [w, n], [e, n], [e, s], [w, s]]]


def places_layer():
    fake = FakeEsri()
    layer = fake.add_layer(URL, FakeLayer({"NAME": "esriFieldTypeString", "KIND": "esriFieldTypeString", "ACTIVE": "esriFieldTypeSmallInteger",
                                           "EDITED": "esriFieldTypeDate"}, "esriGeometryPolygon"))
    layer.add({"NAME": "東京湾", "KIND": "湾"}, {"rings": box(139.5, 35.0, 140.1, 35.7)})       # 1: two squares (139 and 140)
    layer.add({"NAME": "横須賀港", "KIND": "港"}, {"rings": box(139.65, 35.25, 139.75, 35.35)})  # 2
    layer.add({"NAME": "横須賀本港", "KIND": "港", "ACTIVE": 0}, {"rings": box(139.66, 35.28, 139.68, 35.30)})  # 3: not used
    layer.add({"NAME": "大阪湾", "KIND": "湾"}, {"rings": box(134.9, 34.3, 135.5, 34.7)})       # 4
    return fake, layer, EsriSource(URL, dict(DEFAULT_PLACE_FIELDS))


def queries(fake, since=0):
    """The queries asked since: ('ids', squares' envelope) / ('read', ids) / ('info', '')."""
    out = []
    for url, form in fake.asked[since:]:
        if not url.endswith("/query"):
            out.append(("info", ""))
        elif "objectIds" in form:
            out.append(("read", form["objectIds"]))
        elif "geometry" in form:
            out.append(("ids", form["geometry"]))
        else:
            out.append(("all", form.get("where")))
    return out


def names(areas, lon, lat):
    return [a.name for a in places_of(lon, lat, areas)]


def test_squares_and_polygons_read_once_then_from_the_cache(tmp_path):
    fake, _layer, source = places_layer()
    t = [1_000_000.0]
    cache = PlaceCache(str(tmp_path / "p.sqlite"), clock=lambda: t[0])
    client = EsriClient(fake)

    areas = cache.areas_at(client, source, [(139.7, 35.3)])
    # Only the square's features (not 大阪湾), the one not active left out; the smallest first.
    assert sorted(a.name for a in areas) == ["東京湾", "横須賀港"]
    assert names(areas, 139.7, 35.3) == ["横須賀港", "東京湾"]
    assert [q[0] for q in queries(fake)] == ["info", "ids", "read"]
    assert queries(fake)[2] == ("read", "1,2,3")
    assert all(q[0] != "all" for q in queries(fake))  # never the whole layer

    # Another point of the square: nothing asked.
    n = len(fake.asked)
    assert names(cache.areas_at(client, source, [(139.9, 35.6)]), 139.9, 35.6) == ["東京湾"]
    assert len(fake.asked) == n
    # A square crossed by a polygon kept: its ids asked, the polygon not read again.
    assert names(cache.areas_at(client, source, [(140.05, 35.5)]), 140.05, 35.5) == ["東京湾"]
    assert [q[0] for q in queries(fake, n)] == ["ids"]
    # A square of nothing (the sea): asked once, then kept empty.
    n = len(fake.asked)
    assert cache.areas_at(client, source, [(150.5, 20.5)]) == []
    assert cache.areas_at(client, source, [(150.2, 20.9)]) == [] and [q[0] for q in queries(fake, n)] == ["ids"]
    # Several points at once: one query for squares side by side.
    n = len(fake.asked)
    found = cache.areas_at(client, source, [(134.95, 34.5), (135.2, 34.6), (136.5, 34.5)])
    assert [a.name for a in found] == ["大阪湾"] and [q[0] for q in queries(fake, n)] == ["ids", "read"]
    assert queries(fake, n)[0][1].startswith('{"xmin": 134.0, "ymin": 34.0, "xmax": 137.0')
    assert cache.info()[:2] == (6, 4)

    # Kept across sessions (the file): nothing asked within the check time.
    again = PlaceCache(str(tmp_path / "p.sqlite"), clock=lambda: t[0])
    n = len(fake.asked)
    assert names(again.areas_at(EsriClient(fake), source, [(139.7, 35.3)]), 139.7, 35.3) == ["横須賀港", "東京湾"]
    assert len(fake.asked) == n
    # Other attribute names: the same features kept, named by the settings' roles.
    other = EsriSource(URL, {**DEFAULT_PLACE_FIELDS, "name": "KIND"})
    assert sorted(a.name for a in again.areas_at(EsriClient(fake), other, [(139.7, 35.3)])) == ["港", "湾"] and len(fake.asked) == n


def test_a_layer_without_edit_times_asks_its_squares_again_after_ten_minutes(tmp_path):
    fake, layer, source = places_layer()
    t = [1_000_000.0]
    cache = PlaceCache(str(tmp_path / "p.sqlite"), clock=lambda: t[0])
    client = EsriClient(fake)
    cache.areas_at(client, source, [(139.7, 35.3)])
    oid = layer.add({"NAME": "観音崎", "KIND": "岬"}, {"rings": box(139.7, 35.24, 139.76, 35.27)})
    assert "観音崎" not in [a.name for a in cache.areas_at(client, source, [(139.72, 35.26)])]  # within 10 minutes: kept as is

    t[0] += CHECK_SECONDS + 1
    n = len(fake.asked)
    assert "観音崎" in names(cache.areas_at(client, source, [(139.72, 35.26)]), 139.72, 35.26)
    assert queries(fake, n) == [("info", ""), queries(fake, n)[1], ("read", str(oid))]  # only the new one read

    # 抽出 (fresh): the squares asked now, whatever the time.
    n = len(fake.asked)
    cache.areas_at(client, source, [(139.72, 35.26)], fresh=True)
    assert [q[0] for q in queries(fake, n)] == ["info", "ids"]
    # Removed from the layer: gone from the square once asked again.
    layer.rows = [r for r in layer.rows if r["attributes"]["NAME"] != "観音崎"]
    assert "観音崎" not in [a.name for a in cache.areas_at(client, source, [(139.72, 35.26)], fresh=True)]

    # A day later the features are read again (their shape or name may have changed).
    layer.rows[1]["attributes"]["NAME"] = "横須賀新港"
    t[0] += KEEP_SECONDS
    n = len(fake.asked)
    assert names(cache.areas_at(client, source, [(139.7, 35.3)]), 139.7, 35.3) == ["横須賀新港", "東京湾"]
    assert [q[0] for q in queries(fake, n)] == ["info", "ids", "read"]


def test_the_layers_last_edit_time_and_its_features_edit_times(tmp_path):
    fake, layer, source = places_layer()
    layer.extra = {"editingInfo": {"lastEditDate": 100}}
    t = [1_000_000.0]
    cache = PlaceCache(str(tmp_path / "p.sqlite"), clock=lambda: t[0])
    client = EsriClient(fake)
    cache.areas_at(client, source, [(139.7, 35.3)])

    # Unchanged: after 10 minutes only the layer's information is asked.
    t[0] += CHECK_SECONDS + 1
    n = len(fake.asked)
    cache.areas_at(client, source, [(139.7, 35.3)])
    assert queries(fake, n) == [("info", "")]
    # Changed: the square asked again and (no edit field) its features read again.
    layer.extra = {"editingInfo": {"lastEditDate": 200, "dataLastEditDate": 150}}
    layer.rows[1]["attributes"]["NAME"] = "横須賀新港"
    n = len(fake.asked)
    assert names(cache.areas_at(client, source, [(139.7, 35.3)], fresh=True), 139.7, 35.3) == ["横須賀新港", "東京湾"]
    assert [q[0] for q in queries(fake, n)] == ["info", "ids", "read"] and queries(fake, n)[2] == ("read", "1,2,3")

    # With editor tracking: only the feature edited is read again.
    layer.extra = {"editingInfo": {"dataLastEditDate": 300}, "editFieldsInfo": {"editDateField": "edited"}}
    for r in layer.rows:
        r["attributes"]["EDITED"] = 1
    cache.areas_at(client, source, [(139.7, 35.3)], fresh=True)
    layer.extra = {"editingInfo": {"dataLastEditDate": 400}, "editFieldsInfo": {"editDateField": "EDITED"}}
    layer.rows[0]["attributes"].update({"NAME": "江戸湾", "EDITED": 2})
    n = len(fake.asked)
    assert names(cache.areas_at(client, source, [(139.7, 35.3)], fresh=True), 139.7, 35.3) == ["横須賀新港", "江戸湾"]
    assert [q for q in queries(fake, n) if q[0] == "read"] == [("read", "1")]
    # The others are known good under the new time: nothing asked for them next.
    n = len(fake.asked)
    cache.areas_at(client, source, [(139.7, 35.3)])
    assert len(fake.asked) == n


def test_kept_squares_when_the_server_cannot_be_asked(tmp_path):
    fake, _layer, source = places_layer()
    t = [1_000_000.0]
    cache = PlaceCache(str(tmp_path / "p.sqlite"), clock=lambda: t[0])
    cache.areas_at(EsriClient(fake), source, [(139.7, 35.3)])
    t[0] += KEEP_SECONDS * 3

    def down(url, form):
        raise EsriError("接続不可")

    assert names(cache.areas_at(EsriClient(down), source, [(139.7, 35.3)], fresh=True), 139.7, 35.3) == ["横須賀港", "東京湾"]
    with pytest.raises(EsriError):
        cache.areas_at(EsriClient(down), source, [(135.0, 34.5)])
    assert cache.areas_at(EsriClient(down), None, [(1, 2)]) == [] and cache.areas_at(EsriClient(down), source, []) == []


def test_memory_and_file_limits_and_clearing(tmp_path):
    fake, _layer, source = places_layer()
    path = str(tmp_path / "p.sqlite")
    cache = PlaceCache(path, memory_points=6)
    client = EsriClient(fake)
    cache.areas_at(client, source, [(139.7, 35.3)])
    cache.areas_at(client, source, [(135.0, 34.5)])
    assert cache._points <= 6
    # Let go of in memory: from the file, no request.
    n = len(fake.asked)
    assert names(cache.areas_at(client, source, [(139.7, 35.3)]), 139.7, 35.3) == ["横須賀港", "東京湾"] and len(fake.asked) == n

    squares, shapes, size = cache.info()
    assert squares == 2 and shapes == 4 and size > 0
    small = PlaceCache(path, disk_bytes=size // 2)
    small._sql("UPDATE shapes SET used_at = oid")
    small._trim()
    assert small.info()[1] < 4
    # Trimmed shapes are read again when needed (by their ids: the square is still known).
    n = len(fake.asked)
    assert names(small.areas_at(client, source, [(139.7, 35.3)]), 139.7, 35.3) == ["横須賀港", "東京湾"]
    assert [q[0] for q in queries(fake, n)] == ["read"]

    small.clear()
    assert small.info() == (0, 0, 0)
    memory = PlaceCache()
    memory.areas_at(client, source, [(139.7, 35.3)])
    assert memory.info()[:2] == (1, 3)
    # A file that cannot be made: memory only.
    (tmp_path / "file").write_text("x")
    broken = PlaceCache(str(tmp_path / "file" / "p.sqlite"))
    assert broken.path is None and broken.areas_at(client, source, [(139.7, 35.3)])


def test_peek_without_asking_and_countries():
    fake, _layer, source = places_layer()
    countries = fake.add_layer(COUNTRY_URL, FakeLayer({n: "esriFieldTypeString" for n in DEFAULT_COUNTRY_FIELDS.values()}, "esriGeometryPolygon"))
    countries.add({"COUNTRY_CODE": "JP", "COUNTRY_NAME": "日本"}, {"rings": box(128.0, 30.0, 146.0, 46.0)})
    countries.add({"COUNTRY_CODE": "KR", "COUNTRY_NAME": "韓国"}, {"rings": box(124.0, 33.0, 130.0, 39.0)})
    t = [1_000_000.0]
    cache = PlaceCache(clock=lambda: t[0])
    client = EsriClient(fake)
    assert cache.peek(source, 139.7, 35.3) == (None, True)
    cache.areas_at(client, source, [(139.7, 35.3)])
    found, again = cache.peek(source, 139.7, 35.3)
    assert [a.name for a in found] == ["横須賀港", "東京湾"] and again is False
    t[0] += CHECK_SECONDS + 1
    assert cache.peek(source, 139.7, 35.3)[1] is True  # still told, but to be looked up again
    assert cache.peek(source, float("nan"), 1.0) == ([], False) and cache.peek(None, 1, 2) == ([], False)

    country = EsriSource(COUNTRY_URL, dict(DEFAULT_COUNTRY_FIELDS))
    found = cache.areas_at(client, country, [(139.7, 35.3), (126.0, 36.0), (129.5, 34.5)])
    assert [c.code for c in places_of(126.0, 36.0, found)] == ["KR"]
    assert [c.code for c in places_of(129.5, 34.5, found)] == ["KR", "JP"]  # in both: the smallest first


def test_squares_and_runs():
    assert cell_of(139.7, 35.3) == (139, 35) and cell_of(-0.5, -0.5) == (-1, -1)
    assert cell_of(180.0, 90.0) == (179, 89) and cell_of(-180.0, -90.0) == (-180, -90)
    assert runs_of([(3, 1), (1, 1), (2, 1), (5, 1), (2, 2)]) == [(1, 3, 1), (5, 5, 1), (2, 2, 2)]
    assert runs_of([(i, 0) for i in range(45)]) == [(0, 19, 0), (20, 39, 0), (40, 44, 0)]


def test_the_fast_inside_test_says_what_the_plain_one_says():
    """A border of many points with islands and a lake: NamedArea.contains (NumPy, far rings skipped) = inside_rings."""
    rnd = random.Random(7)

    def wobbly(cx, cy, r, n, clockwise=True):
        pts = []
        for i in range(n):
            a = 2 * math.pi * i / n * (-1 if clockwise else 1)
            rr = r * (0.7 + 0.3 * rnd.random())
            pts.append([cx + rr * math.cos(a), cy + rr * math.sin(a)])
        return pts + [pts[0]]

    rings = [wobbly(135, 35, 3, 4000), wobbly(135, 35, 1, 300, clockwise=False), wobbly(141, 35, 0.5, 200), wobbly(128, 30, 0.3, 50)]
    area = NamedArea("国", rings, box=(126, 28, 142, 39))
    for _ in range(3000):
        lon, lat = rnd.uniform(126, 142), rnd.uniform(28, 39)
        assert area.contains(lon, lat) == inside_rings(lon, lat, rings), (lon, lat)
    assert not area.contains(150, 35)
