"""
画像の評価 (tsudo, 2026-11-24: 「4は、形状ありで、ネームドエリアごと1行にしようか」):
one row per image × named area (or the whole image) in TBL_IMAGE_REVIEW,
written over when rated again, deleted when cleared; the texts of the dock,
the catalog and the follow-up of requests.
"""
from __future__ import annotations

from dataclasses import replace

from mikage.core.config import parse_config
from mikage.core.esri import EsriClient
from mikage.core.image_reviews import (
    BAD, DEFAULT_REVIEW_FIELDS, GOOD, PART, WHOLE, ImageReview, attributes_of, catalog_rating, catalog_text, from_unseen, lines_of,
    load_reviews, rating_of, reasons_of, review_of, save_reviews, summary_text, unusable_over, unusable_text,
)
from mikage.core.shapes import rings_overlap
from mikage.tests.fake_esri import FakeEsri, FakeLayer

import pytest

URL = "https://x/arcgis/rest/services/MIKAGE/FeatureServer/30"
TYPES = {"REVIEW_ID": "esriFieldTypeString", "IMAGE_ID": "esriFieldTypeString", "IMAGE_NAME": "esriFieldTypeString",
         "IMAGE_TIME": "esriFieldTypeDate", "SATELLITE": "esriFieldTypeString", "NAMED_AREA_ID": "esriFieldTypeString",
         "NAMED_AREA_NAME": "esriFieldTypeString", "RATING": "esriFieldTypeString", "CLOUD_PCT": "esriFieldTypeDouble",
         "REASONS": "esriFieldTypeString", "NOTE": "esriFieldTypeString", "REVIEWED_BY": "esriFieldTypeString",
         "REVIEWED_AT": "esriFieldTypeDate"}
T = 1_790_000_000_000


def box(w, s, e, n):
    return [[[w, s], [e, s], [e, n], [w, n], [w, s]]]


def setup():
    fake = FakeEsri()
    layer = fake.add_layer(URL, FakeLayer(dict(TYPES), "esriGeometryPolygon"))
    config, problems = parse_config({"imageReviews": URL})
    assert problems == []
    return EsriClient(fake), config.image_reviews, layer


def test_settings_and_words():
    config, problems = parse_config({"imageReviews": {"url": URL, "fields": {"rating": "HYOKA"}}})
    assert problems == [] and config.image_reviews.fields["rating"] == "HYOKA"
    assert config.image_reviews.fields["areaId"] == DEFAULT_REVIEW_FIELDS["areaId"]
    assert parse_config({})[0].image_reviews is None
    assert parse_config({"imageReviews": "https://x/arcgis/rest/services/M/MapServer/3"})[1]
    assert [rating_of(x) for x in ("使える", "△", "×", "bad", "たぶん")] == [GOOD, PART, BAD, BAD, ""]
    assert reasons_of("雲, 霞、雲") == ("雲", "霞")


def test_the_row():
    a = {"OBJECTID": 4, "REVIEW_ID": "REV-1", "IMAGE_ID": "IMG-1", "RATING": "使えない", "CLOUD_PCT": 80, "REASONS": "雲,霞",
         "NAMED_AREA_ID": "NA-000012", "NAMED_AREA_NAME": "呉港", "REVIEWED_BY": "tsudo", "REVIEWED_AT": T}
    r = review_of(a, DEFAULT_REVIEW_FIELDS)
    assert (r.key, r.label, r.whole, r.oid, r.cloud, r.reasons) == ("NA-000012", "呉港", False, 4, 80.0, ("雲", "霞"))
    assert r.detail_text() == "使えない（雲 80%・雲・霞）"
    assert review_of({**a, "RATING": "?"}, DEFAULT_REVIEW_FIELDS) is None and review_of({**a, "IMAGE_ID": ""}, DEFAULT_REVIEW_FIELDS) is None
    whole = review_of({**a, "NAMED_AREA_ID": None, "NAMED_AREA_NAME": None}, DEFAULT_REVIEW_FIELDS)
    assert whole.whole and whole.key == "" and whole.label == WHOLE
    named = review_of({**a, "NAMED_AREA_ID": None}, DEFAULT_REVIEW_FIELDS)
    assert named.key == "名前:呉港"
    written = attributes_of(r, DEFAULT_REVIEW_FIELDS, TYPES)
    assert written["REASONS"] == "雲,霞" and written["NAMED_AREA_ID"] == "NA-000012" and "OBJECTID" not in written


def test_saved_written_over_and_cleared():
    client, source, layer = setup()
    area = box(132.5, 34.2, 132.6, 34.3)
    kure = ImageReview("IMG-1", BAD, area_id="NA-000012", area_name="呉港", cloud=90, reasons=("雲",), rings=area)
    whole = ImageReview("IMG-1", PART, image_name="画像1", image_time=T, satellite="OPT 1", rings=box(132, 34, 133, 35))
    saved = save_reviews(client, source, "IMG-1", [kure, whole], token=None, now=T, user="tsudo")
    assert (saved.added, saved.updated, saved.deleted, saved.problems) == (2, 0, 0, [])
    assert len(layer.rows) == 2 and all(r["geometry"]["rings"] for r in layer.rows)
    row = next(r["attributes"] for r in layer.rows if r["attributes"]["NAMED_AREA_ID"] == "NA-000012")
    assert (row["RATING"], row["REVIEWED_BY"], row["REVIEW_ID"].startswith("REV-")) == (BAD, "tsudo", True)
    first_id = row["REVIEW_ID"]
    # Rated again by someone else: the same row written over (its id kept), not a new one.
    again = save_reviews(client, source, "IMG-1", [ImageReview("IMG-1", GOOD, area_id="NA-000012", area_name="呉港", rings=area)],
                         now=T + 1000, user="suzuki")
    assert (again.added, again.updated) == (0, 1) and len(layer.rows) == 2
    row = next(r["attributes"] for r in layer.rows if r["attributes"]["NAMED_AREA_ID"] == "NA-000012")
    assert (row["RATING"], row["REVIEWED_BY"], row["REVIEW_ID"]) == (GOOD, "suzuki", first_id)
    # Cleared: its row deleted.
    cleared = save_reviews(client, source, "IMG-1", [], cleared=[""], now=T + 2000, user="suzuki")
    assert cleared.deleted == 1 and [r.key for r in cleared.rows] == ["NA-000012"]
    assert load_reviews(client, source, ["IMG-1", "IMG-2"]) == {"IMG-1": cleared.rows}


def test_two_rows_of_one_key_become_one():
    client, source, layer = setup()
    for who in ("a", "b"):
        layer.add({"REVIEW_ID": f"REV-{who}", "IMAGE_ID": "IMG-1", "RATING": GOOD, "NAMED_AREA_NAME": "呉港", "REVIEWED_BY": who})
    saved = save_reviews(client, source, "IMG-1", [ImageReview("IMG-1", BAD, area_name="呉港")], now=T, user="c")
    assert len(layer.rows) == 1 and layer.rows[0]["attributes"]["RATING"] == BAD and saved.rows[0].reviewed_by == "c"


def test_what_is_shown():
    whole = ImageReview("I", BAD, reasons=("雲",))
    a = ImageReview("I", GOOD, area_id="A", area_name="呉港")
    b = ImageReview("I", BAD, area_id="B", area_name="江田島", cloud=80)
    c = ImageReview("I", PART, area_id="C", area_name="広湾")
    assert summary_text([]) == "評価: なし" and summary_text([whole]) == "評価: 画像全体 使えない（雲）"
    assert summary_text([a, b, c]) == "評価: 使える 1・一部 1・使えない 1"
    assert catalog_text([whole, a]) == BAD and catalog_text([a, a]) == GOOD
    assert catalog_text([a, b, c]) == "一部（使えない 1/3）" and catalog_text([a, c]) == "一部（一部 1/2）" and catalog_text([]) == ""
    assert catalog_rating([a, b, c]) == PART and catalog_rating([b]) == BAD and catalog_rating([]) == ""
    assert lines_of([b]) == ["江田島: 使えない（雲 80%）"]


def test_an_image_unusable_over_a_request_area():
    request = box(132.55, 34.25, 132.56, 34.26)
    kure = ImageReview("I", BAD, area_id="A", area_name="呉港", reasons=("雲",), rings=box(132.5, 34.2, 132.6, 34.3))
    far = ImageReview("I", GOOD, area_id="B", area_name="松山", rings=box(132.7, 33.8, 132.8, 33.9))
    assert rings_overlap(kure.rings, request) and not rings_overlap(far.rings, request)
    assert unusable_over([kure, far], request) and unusable_text([kure, far], request) == "画像の評価: 使えない（雲）"
    # One named area over it is usable (一部 too): taken.
    assert not unusable_over([kure, ImageReview("I", PART, area_id="C", rings=box(132.54, 34.24, 132.57, 34.27))], request)
    # None over it: the whole image's.
    assert unusable_over([far, ImageReview("I", BAD)], request) and not unusable_over([far], request)
    assert not unusable_over([], request) and unusable_text([far], request) == ""


@pytest.mark.parametrize("share, rating", [(0.0, ""), (0.2, PART), (0.5, BAD), (1.0, BAD)])
def test_first_ratings_from_the_unseen_areas(share, rating):
    assert from_unseen(share, ["雲", "雲", "霞"]) == (rating, ("雲", "霞") if rating else ())


def test_the_follow_up_does_not_count_an_image_unusable_over_the_area():
    """設計 2: an image all 使えない over a request's area is not 撮像済み; a row already 撮像済み is only marked."""
    from mikage.core.catalog import catalog_of
    from mikage.core.image_reviews import UnusableImages
    from mikage.core.request.follow import follow_up
    from mikage.core.request.store import HistoryRow
    from mikage.tests.test_request import CATALOG, DAY, FIRST, LAST, TOKYO, request, server

    fake = server()  # IMG-1 over Tokyo Bay (139.7–140.0, 35.4–35.6) on the day
    client = EsriClient(fake)
    layer = fake.add_layer(URL, FakeLayer(dict(TYPES), "esriGeometryPolygon"))
    source = parse_config({"imageReviews": URL})[0].image_reviews
    catalog = catalog_of({"label": "画像", "url": CATALOG, "fields": {"id": "IMAGE_ID", "acquired": "ACQ", "sensor": "SENSOR", "source": "URL"}}, [], 0)
    req = request("R-1")
    row = HistoryRow("RUN", FIRST, DAY.isoformat(), "R-1", "SYS-A", "OPT-X 1")

    def follow():
        unusable = UnusableImages(client, source)
        return follow_up(client, catalog, None, [replace(row)], {"R-1": req}, LAST + 86_400_000, 3, unusable=unusable), unusable

    # Not rated: taken as before.
    found, _u = follow()
    assert [(f.captured, f.image_id) for f in found] == [("撮像済み", "IMG-1")]
    # Rated 使えない over the whole of the request's area (one named area there): not counted.
    layer.add({"REVIEW_ID": "REV-1", "IMAGE_ID": "IMG-1", "RATING": BAD, "NAMED_AREA_ID": "NA-1", "NAMED_AREA_NAME": "東京湾",
               "REASONS": "雲"}, {"rings": TOKYO})
    found, unusable = follow()
    assert found == [] and unusable.left == {"IMG-1": "画像の評価: 使えない（雲）"}
    # A row 撮像済み of it: marked (the text), not changed.
    assert unusable.text("IMG-1", req.rings) == "画像の評価: 使えない（雲）"
    # 一部 over it: taken.
    layer.rows[0]["attributes"]["RATING"] = PART
    found, _u = follow()
    assert [f.captured for f in found] == ["撮像済み"]
    # The table cannot be read: taken as before, said once.
    broken = UnusableImages(client, parse_config({"imageReviews": URL.replace("/30", "/31")})[0].image_reviews)
    assert follow_up(client, catalog, None, [replace(row)], {"R-1": req}, LAST + 86_400_000, 3, unusable=broken)
    assert len(broken.problems) == 1 and "評価を見ずに照合" in broken.problems[0]
