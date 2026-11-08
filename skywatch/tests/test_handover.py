"""分担 (who holds which image) and 引き継ぎメモ (the shift handover)."""
import json
import math

import pytest

from skywatch.core.config import parse_config
from skywatch.core.esri import EsriClient
from skywatch.core.obs.alerts import LogRow
from skywatch.core.obs.assignments import (
    Assignment, TakenError, holder_of, holders, load_assignments, progress_of, progress_text, release, same_person, take, update,
    with_status,
)
from skywatch.core.obs.handover import (
    HandoverData, file_path, handover_settings_of, handover_text, keep_memo, last_memo, open_requests, write_file,
)
from skywatch.core.obs.model import Observation
from skywatch.core.request.model import ImagingRequest
from skywatch.tests.fake_esri import FakeEsri, FakeLayer

T0 = 1_762_500_000_000  # 2025-11-07T07:20:00Z
H = 3_600_000
WORK_URL = "https://x/arcgis/rest/services/Work/FeatureServer/0"
MEMO_URL = "https://x/arcgis/rest/services/Work/FeatureServer/1"
S, D = "esriFieldTypeString", "esriFieldTypeDate"


def obs(**kw) -> Observation:
    base = dict(id="r1", image_id="IMG1", image_name="img1.tif", image_time=T0, object_id="OBJ-1", object_name="船A",
                kind="船舶 > 支援船", activity="航行", present=True, note="", lon=135.0, lat=35.0,
                created_at=T0 + H, updated_at=T0 + H, recorded_by="sato")
    base.update(kw)
    return Observation(**base)


def esri():
    fake = FakeEsri()
    work = fake.add_layer(WORK_URL, FakeLayer({"IMAGE_ID": S, "IMAGE_NAME": S, "IMAGE_TIME": D, "ASSIGNEE": S, "STATUS": S, "NOTE": S,
                                              "TAKEN_AT": D, "DONE_AT": D, "UPDATED_USER": S}, ""))
    memo = fake.add_layer(MEMO_URL, FakeLayer({"TEXT": S, "MEMO": S, "PERIOD_START": D, "PERIOD_END": D, "WRITTEN_AT": D, "WRITTEN_BY": S}, ""))
    config, problems = parse_config({"assignments": WORK_URL, "handover": {"table": MEMO_URL, "hours": 8, "newline": "lf"}})
    assert problems == []
    return fake, work, memo, config


def test_settings_and_problems():
    _, _, _, config = esri()
    assert config.assignments.fields["assignee"] == "ASSIGNEE" and config.handover.hours == 8
    assert parse_config({})[0].assignments is None and parse_config({})[0].handover.table is None
    problems = []
    parse_config({"assignments": "https://x/MapServer/0", "handover": {"hours": 0, "limit": "x", "file": {}, "timeZone": "火星"}}, )
    s = handover_settings_of({"hours": 0, "limit": "x", "file": {}, "timeZone": "火星", "table": "nope"}, problems)
    assert s.hours == 12 and len(problems) == 5
    assert parse_config({"assignments": "https://x/MapServer/0"})[1]


def test_take_refuses_an_image_someone_else_holds():
    fake, work, _, config = esri()
    client, src = EsriClient(fake), config.assignments
    mine = take(client, src, "IMG1", "sato", T0, "img1.tif", T0 - H)
    assert mine.oid == 1 and work.rows[0]["attributes"]["ASSIGNEE"] == "sato" and work.rows[0]["attributes"]["STATUS"] == "作業中"
    # Taking it again: the same row (case of the name aside).
    assert take(client, src, "IMG1", "SATO", T0).oid == 1 and len(work.rows) == 1
    with pytest.raises(TakenError, match="sato さんが担当中"):
        take(client, src, "IMG1", "suzuki", T0)
    assert len(work.rows) == 1
    # Done: anyone may take it again (a second look).
    x = load_assignments(client, src)[0]
    update(client, src, with_status(x, "完了", T0 + H), x)
    assert work.rows[0]["attributes"]["DONE_AT"] == T0 + H
    again = take(client, src, "IMG1", "suzuki", T0 + 2 * H)
    assert again.oid == 2 and [h.assignee for h in holders(client, src, "IMG1")] == ["suzuki"]
    # Given back: its row goes.
    release(client, src, again)
    assert holders(client, src, "IMG1") == []


def test_take_at_the_same_moment_keeps_the_first():
    fake, work, _, config = esri()
    client, src = EsriClient(fake), config.assignments
    real = fake.answer

    def racing(url, form):
        # suzuki's add lands between sato's check and sato's add.
        if url.endswith("/applyEdits") and "sato" in form.get("adds", "") and not any(r["attributes"]["ASSIGNEE"] == "suzuki" for r in work.rows):
            work.add({"IMAGE_ID": "IMG1", "ASSIGNEE": "suzuki", "STATUS": "作業中"})
        return real(url, form)

    fake.answer = racing
    with pytest.raises(TakenError, match="suzuki"):
        take(client, src, "IMG1", "sato", T0)
    assert [r["attributes"]["ASSIGNEE"] for r in work.rows] == ["suzuki"]


def test_update_writes_only_what_changed_and_checks_hand_overs():
    fake, work, _, config = esri()
    client, src = EsriClient(fake), config.assignments
    take(client, src, "IMG1", "sato", T0)
    x = load_assignments(client, src)[0]
    # Someone else writes a note meanwhile: the state change does not undo it.
    work.rows[0]["attributes"]["NOTE"] = "港の北側から"
    update(client, src, with_status(x, "保留", T0), x)
    a = work.rows[0]["attributes"]
    assert (a["STATUS"], a["NOTE"]) == ("保留", "港の北側から")
    sent = json.loads([f for u, f in fake.asked if u.endswith("/applyEdits")][-1]["updates"])[0]["attributes"]
    assert set(sent) - {"OBJECTID", "UPDATED_USER"} == {"STATUS"}
    # Handing over a row that someone else handed meanwhile fails.
    work.rows[0]["attributes"]["ASSIGNEE"] = "tanaka"
    from dataclasses import replace

    with pytest.raises(Exception, match="ほかの人が変更済み"):
        update(client, src, replace(x, assignee="suzuki"), x)
    assert work.rows[0]["attributes"]["ASSIGNEE"] == "tanaka"


def test_progress_counts_the_image_records_since_taken():
    x = Assignment(image_id="IMG1", assignee="Sato", taken_at=T0 + H // 2)
    records = [obs(id="a"), obs(id="b", recorded_by="suzuki"), obs(id="c", image_id="IMG2"), obs(id="d", created_at=T0),
               obs(id="e", deleted_at=T0 + 2 * H)]
    p = progress_of(x, records)
    assert (p.records, p.by_assignee, p.last) == (2, 1, T0 + H)
    assert progress_text(p) == "記録 2 件・うち担当者 1 件" and progress_text(progress_of(x, [])) == "記録なし"
    assert same_person(" sato ", "SATO") and not same_person("", "")
    items = [Assignment("IMG1", "a", status="完了", oid=1), Assignment("IMG1", "b", oid=3), Assignment("IMG1", "c", oid=2)]
    assert holder_of(items, "IMG1").assignee == "c" and holder_of(items, "IMG9") is None


def _request(**kw) -> ImagingRequest:
    base = dict(id="q1", name="横須賀港", start=T0 - 24 * H, end=T0 + 48 * H, priority=500)
    base.update(kw)
    return ImagingRequest(**base)


def test_the_memo_has_every_part():
    _, _, _, config = esri()
    s = config.handover
    reqs = [_request(), _request(id="q2", name="舞鶴", end=T0 + 10 * H, priority=600), _request(id="q3", status="完了"),
            _request(id="q4", end=T0 - H), _request(id="q5", name="無期限", end=math.nan)]
    open_ = open_requests(reqs, T0 + 2 * H, "台帳")
    assert [r.request.id for r in open_] == ["q1", "q2", "q5"]
    d = HandoverData(
        start=T0, end=T0 + 8 * H, now=T0 + 8 * H, user="sato",
        records=[obs(id="a"), obs(id="b", present=False, kind="航空機", recorded_by="suzuki"), obs(id="c", created_at=T0 - H, updated_at=T0 + H),
                 obs(id="d", created_at=T0 - H, deleted_at=T0 + 2 * H)],
        alerts=[LogRow(obs_id="a", format="速報", text="11/07 17:20|船舶|船A", reported_at=T0 + 3 * H)],
        requests=open_,
        assignments=[Assignment("IMG1", "sato", "img1.tif", taken_at=T0, oid=1),
                     Assignment("IMG2", "suzuki", "img2.tif", status="完了", taken_at=T0, done_at=T0 + 4 * H, oid=2)],
        images=[("IMG1", "img1.tif"), ("IMG3", "img3.tif")],
        memo="港の北側は雲で未確認\n次は IMG3 から",
    )
    text = handover_text(d, s)
    assert "\r" not in text
    lines = text.split("\n")
    assert lines[0] == "引き継ぎメモ 11/07 16:20〜11/08 00:20（作成 sato・11/08 00:20）"
    assert "■ 観測記録: 新規 2 件（うち不在 1 件）・更新 1 件・削除 1 件" in lines
    assert "  分類: 航空機 1・船舶 1" in lines and "  記録者: sato 1・suzuki 1" in lines
    assert "■ 通報: 1 件（速報 1）" in lines and "  11/07 19:20 11/07 17:20|船舶|船A" in lines
    i = lines.index("■ 未処理の撮像依頼: 3 件（うち 24 時間以内に期限 1 件）")
    assert lines[i + 1].startswith("  【期限間近】舞鶴（") and "横須賀港" in lines[i + 2] and "期限なし" in lines[i + 3]
    assert "■ 分担: 担当中 1 件・期間内に完了 1 件" in lines
    assert "  sato: 担当中 1 件・完了 0 件" in lines and "    img1.tif（作業中・記録 2 件・うち担当者 1 件）" in lines
    assert "    img2.tif（完了・記録なし）" in lines
    assert "  担当者なしの画像（観測記録で開いている画像）: 1 件" in lines and "    img3.tif" in lines
    assert lines[-4:] == ["■ 申し送り", "  港の北側は雲で未確認", "  次は IMG3 から", ""]
    # Parts not set up are left out; problems are said.
    text = handover_text(HandoverData(start=T0, end=T0 + H, now=T0 + H, problems=["分担: 接続不可"]), s)
    assert "通報" not in text and "撮像依頼" not in text and "■ 分担" not in text and "  分担: 接続不可" in text and "  なし" in text


def test_lists_are_cut_at_the_limit():
    s = handover_settings_of({"limit": 2}, [])
    d = HandoverData(start=T0, end=T0 + H, now=T0, requests=open_requests([_request(id=f"q{i}", name=f"r{i}") for i in range(5)], T0))
    assert "  …ほか 3 件" in handover_text(d, s)


def test_memo_table_round_trip_and_file(tmp_path):
    fake, _, memo, config = esri()
    client, s = EsriClient(fake), config.handover
    assert last_memo(client, s.table) is None
    d = HandoverData(start=T0, end=T0 + 8 * H, now=T0 + 8 * H, user="sato", memo="申し送り")
    keep_memo(client, s.table, "本文1", d)
    keep_memo(client, s.table, "本文2", HandoverData(start=T0 + 8 * H, end=T0 + 16 * H, now=T0 + 16 * H, user="suzuki"))
    last = last_memo(client, s.table, since=T0 - 24 * H)
    assert (last.text, last.written_by, last.start, last.end) == ("本文2", "suzuki", T0 + 8 * H, T0 + 16 * H)
    assert memo.rows[0]["attributes"]["MEMO"] == "申し送り"
    s = handover_settings_of({"file": {"path": str(tmp_path / "h" / "handover_{now}.txt"), "encoding": "cp932"}}, [])
    assert file_path(s, T0).endswith("handover_20251107_1620.txt")
    path = write_file("引き継ぎ\r\n", s, T0)
    assert open(path, encoding="cp932", newline="").read() == "引き継ぎ\r\n"


def test_done_rows_long_ago_are_not_read():
    fake, work, _, config = esri()
    client, src = EsriClient(fake), config.assignments
    work.add({"IMAGE_ID": "OLD", "ASSIGNEE": "a", "STATUS": "完了", "DONE_AT": T0 - 30 * 24 * H})
    work.add({"IMAGE_ID": "NEW", "ASSIGNEE": "a", "STATUS": "完了", "DONE_AT": T0})
    work.add({"IMAGE_ID": "HELD", "ASSIGNEE": "b", "STATUS": "作業中"})
    assert {x.image_id for x in load_assignments(client, src, done_since=T0 - 7 * 24 * H)} == {"NEW", "HELD"}
    assert len(load_assignments(client, src)) == 3


def test_a_memo_longer_than_the_table_takes_is_cut_and_said():
    fake = FakeEsri()
    layer = fake.add_layer(MEMO_URL, FakeLayer({"TEXT": S, "WRITTEN_AT": D}, ""))
    real = layer.info

    def info():
        value = real()
        for f in value["fields"]:
            if f["name"] == "TEXT":
                f["length"] = 20
        return value

    layer.info = info
    s = handover_settings_of({"table": MEMO_URL}, [])
    cut = keep_memo(EsriClient(fake), s.table, "あ" * 50, HandoverData(start=T0, end=T0 + H, now=T0 + H))
    assert cut == ["TEXT（20 文字まで）"]
    text = layer.rows[0]["attributes"]["TEXT"]
    assert len(text) == 20 and text.endswith("…（以下省略）")


def test_claim_on_record_setting():
    from skywatch.core.obs.assignments import assignment_source_of

    url = "https://x/arcgis/rest/services/Work/FeatureServer/0"
    problems: list[str] = []
    assert assignment_source_of(url, problems).claim_on_record is True
    s = assignment_source_of({"url": url, "claimOnRecord": False, "fields": {"assignee": "WHO"}}, problems)
    assert s.claim_on_record is False and s.fields["assignee"] == "WHO" and s.url.endswith("/0") and not problems
    assert assignment_source_of({"url": url, "claimOnRecord": "no"}, problems).claim_on_record is True
    assert problems == ["assignments.claimOnRecord は true か false で指定"]


# ---- 画像カタログの「分担」列 ----------------------------------------------------------------------

def test_the_work_state_of_each_image():
    from skywatch.core.esri import EsriClient
    from skywatch.core.obs.assignments import (
        UNWORKED, Assignment, assignment_source_of, assignments_of_images, take, with_status, update, work_state, work_text,
    )
    from skywatch.tests.fake_esri import FakeEsri, FakeLayer

    url = "https://x/arcgis/rest/services/Work/FeatureServer/0"
    s, d = "esriFieldTypeString", "esriFieldTypeDate"
    fake = FakeEsri()
    fake.add_layer(url, FakeLayer({"IMAGE_ID": s, "IMAGE_NAME": s, "IMAGE_TIME": d, "ASSIGNEE": s, "STATUS": s, "NOTE": s,
                                   "TAKEN_AT": d, "DONE_AT": d}, ""))
    source = assignment_source_of(url, [])
    client = EsriClient(fake)
    take(client, source, "A", "田中", 1000)
    b = take(client, source, "B", "鈴木", 1000)
    update(client, source, with_status(b, "完了", 2000), b)
    take(client, source, "B", "佐藤", 3000)  # a second look
    c = take(client, source, "C", "鈴木", 1000)
    update(client, source, with_status(c, "完了", 2000), c)
    rows = assignments_of_images(client, source, ["A", "B", "C", "D", "A"], chunk=2)
    assert sorted(rows) == ["A", "B", "C"]
    states = {i: work_state(rows.get(i, [])) for i in "ABCD"}
    assert states == {"A": ("作業中", "田中"), "B": ("作業中", "佐藤"), "C": ("完了", "鈴木"), "D": (UNWORKED, "")}
    assert [work_text(states[i]) for i in "ACD"] == ["作業中（田中）", "完了（鈴木）", "未着手"]
    assert work_state([]) == (UNWORKED, "") and isinstance(rows["A"][0], Assignment)
