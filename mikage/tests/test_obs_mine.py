"""
「自分の記録」 (core/obs/mine.py; tsudo 2026-11-18: 過去の自分がどの画像のどの観測結果を
作ったのかがわかりにくい): who is oneself, what the list shows, one row per
image, and one's own records asked of an Esri layer without reading them all.
"""
from datetime import datetime, timezone
from itertools import count

from mikage.core.esri import EsriClient
from mikage.core.obs.mine import (
    DAY_MS, MineFilter, by_image, changed_by_other, is_mine, my_names, observer_names, root_of, roots_text, same_name, select, since_of,
    start_of_day,
)
from mikage.core.obs.model import Observation, observation_store_of
from mikage.core.obs.stores import EsriObservationStore
from mikage.tests.fake_esri import FakeEsri, FakeLayer


def day(d: int, hour: int = 1) -> int:
    return int(datetime(2026, 11, d, hour, tzinfo=timezone.utc).timestamp() * 1000)


_n = count(1)


def obs(**o) -> Observation:
    base = dict(id=f"m{next(_n)}", image_id="img1", image_name="img1.tif", image_time=day(1), object_id="A", object_name="あおば",
                kind="船舶 > 護衛艦", activity="停泊", present=True, note="", lon=139.7, lat=35.6, created_at=day(10), updated_at=day(10),
                recorded_by="田中")
    base.update(o)
    return Observation(**base)


def test_oneself_is_the_recorder_name_or_the_user_name_case_and_spaces_aside():
    assert same_name(" Tanaka ", "tanaka") and not same_name("", "") and not same_name("田中", "鈴木")
    assert my_names("田中", "tanaka") == ["田中", "tanaka"]
    assert my_names("TANAKA", "tanaka") == ["TANAKA"] and my_names("", "tanaka") == ["tanaka"] and my_names("", "") == []
    assert is_mine("tanaka ", ["田中", "TANAKA"]) and not is_mine("", ["田中"])


def test_the_period_starts_days_back_or_at_the_start_of_today():
    jst = 9 * 3600 * 1000
    now = day(18, 3)  # 12:00 in Tokyo
    assert start_of_day(now, jst) == day(17, 15)  # 00:00 in Tokyo
    assert since_of(0, now, jst) == day(17, 15)
    assert since_of(30, now, jst) == now - 30 * DAY_MS


def test_the_list_shows_ones_records_of_the_period_newest_made_first():
    mine_old = obs(object_name="古い", created_at=day(1))
    mine = obs(object_name="自分", created_at=day(12))
    as_user = obs(object_name="ユーザー名で", recorded_by="tanaka", created_at=day(14), kind="航空機 > 戦闘機")
    others = obs(object_name="鈴木さんの", recorded_by="鈴木", created_at=day(13))
    gone = obs(object_name="削除", created_at=day(11), deleted_at=day(12))
    changed = obs(object_name="変えられた", created_at=day(9), updated_at=day(15), updated_by="鈴木", note="鈴木が直した")
    records = [mine_old, mine, as_user, others, gone, changed]
    me = ["田中", "tanaka"]
    flt = MineFilter(names=me, since=day(5))
    assert [o.object_name for o in select(records, flt, me)] == ["ユーザー名で", "自分", "変えられた"]
    # Everyone; the deleted too; a 大分類; a text (the note, the image too).
    assert len(select(records, MineFilter(since=day(5)), me)) == 4
    assert "削除" in [o.object_name for o in select(records, MineFilter(names=me, since=day(5), deleted=True), me)]
    assert [o.object_name for o in select(records, MineFilter(names=me, root="航空機"), me)] == ["ユーザー名で"]
    assert [o.object_name for o in select(records, MineFilter(names=me, text="鈴木 直した"), me)] == ["変えられた"]
    assert len(select(records, MineFilter(names=me, text="IMG1.TIF"), me)) == 4
    # Until (excluded); someone else changed it afterwards.
    assert [o.object_name for o in select(records, MineFilter(names=me, since=day(1), until=day(10)), me)] == ["変えられた", "古い"]
    assert [o.object_name for o in select(records, MineFilter(names=me, changed_by_others=True), me)] == ["変えられた"]
    assert changed_by_other(changed, me) and not changed_by_other(obs(updated_by="TANAKA", updated_at=day(20)), me)
    assert not changed_by_other(obs(updated_by="鈴木"), me)  # saved by 鈴木 when made (not changed since)


def test_one_row_per_image_the_image_recorded_on_last_first():
    a = obs(image_id="img1", image_name="img1.tif", kind="船舶 > 護衛艦", created_at=day(10))
    b = obs(image_id="img1", image_name="img1.tif", kind="船舶 > 補給艦", created_at=day(12))
    c = obs(image_id="img1", image_name="img1.tif", kind="航空機", created_at=day(11))
    gone = obs(image_id="img1", kind="車両", created_at=day(11), deleted_at=day(12))
    d = obs(image_id="img2", image_name="", image_time=None, kind="", created_at=day(11))
    rows = by_image([a, b, c, gone, d])
    assert [(r.image_id, r.image_name, len(r.records), r.last_made) for r in rows] == [("img1", "img1.tif", 4, day(12)), ("img2", "img2", 1, day(11))]
    assert roots_text(rows[0].roots) == "船舶 2・航空機 1" and roots_text(rows[1].roots) == "（種別なし） 1"
    assert root_of(" 船舶 > 護衛艦") == "船舶"
    assert observer_names([obs(recorded_by="鈴木"), obs(recorded_by="田中"), obs(recorded_by="すずき"), obs(recorded_by="鈴木 "), obs(recorded_by="")]) == \
        ["鈴木", "すずき", "田中"]


FIELDS = ["OBS_ID", "IMAGE_ID", "IMAGE_NAME", "IMAGE_TIME", "OBJECT_ID", "OBJECT_NAME", "KIND", "ACTIVITY", "PRESENT", "NOTE",
          "OBSERVER", "UPDATED_BY", "CREATED_AT", "UPDATED_AT", "DELETED_AT"]
TYPES = {"IMAGE_TIME": "esriFieldTypeDate", "CREATED_AT": "esriFieldTypeDate", "UPDATED_AT": "esriFieldTypeDate",
         "DELETED_AT": "esriFieldTypeDate", "PRESENT": "esriFieldTypeSmallInteger"}
URL = "https://x/FeatureServer/0"


def test_ones_records_of_the_period_are_asked_of_the_layer_alone():
    fake = FakeEsri()
    fake.add_layer(URL, FakeLayer({n: TYPES.get(n, "esriFieldTypeString") for n in FIELDS}))
    store = EsriObservationStore(observation_store_of({"url": URL}, [], 0), EsriClient(fake))
    store.save([obs(id="r1", recorded_by="田中", created_at=day(12)), obs(id="r2", recorded_by="Tanaka", created_at=day(13)),
                obs(id="r3", recorded_by="鈴木", created_at=day(13)), obs(id="r4", recorded_by="田中", created_at=day(2)),
                obs(id="r5", recorded_by="O'Neil", created_at=day(14))])
    fake.asked.clear()
    made = store.load_made(["田中", "tanaka"], day(5))
    assert sorted(o.id for o in made) == ["r1", "r2"]
    [(_url, form)] = [(u, f) for u, f in fake.asked if u.endswith("/query")][:1]
    assert "UPPER(OBSERVER) IN ('田中', 'TANAKA')" in form["where"] and "CREATED_AT >= TIMESTAMP '2026-11-05" in form["where"]
    assert [o.id for o in store.load_made(["o'neil"], None)] == ["r5"]
    assert len(store.load_made([], None)) == 5
