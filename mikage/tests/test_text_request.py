"""依頼文から下書き (core/text_request.py): what the words name, what is kept of the model's lines, editing."""
from dataclasses import replace
from datetime import date, datetime

from mikage.core import text_assist as ta
from mikage.core import text_request as tr
from mikage.core.plan.satellites import SatelliteSpec
from mikage.core.request.model import ImagingRequest, day_bounds, jst_day

TODAY = date(2026, 11, 21)  # a Saturday
BASE = SatelliteSpec(name="", id="", tle1="", tle2="", maxOffNadir=30, minOffNadir=0, swath=10, length=10, scenesPerPass=1,
                     minInterval=0, maxPerDay=0, leadTime=0, series="", lookSide="both", sar=False, source="test")


def sat(name, series, mode=None, sar=False, aliases=()):
    return replace(BASE, name=name, id=name.replace(" ", ""), series=series, mode=mode, sar=sar, aliases=aliases)


SATS = [
    sat("WorldView-3", "WorldView", aliases=("WV03",)),
    sat("WorldView-2", "WorldView"),
    sat("ALOS-2", "ALOS", "Spotlight", sar=True, aliases=("だいち2号",)),
    sat("ALOS-2", "ALOS", "StripMap", sar=True, aliases=("だいち2号",)),
    sat("OPT 1", "OPT"),
]


def answer(**lines):
    keys = {"名称": "name", "地名": "place", "期間": "period", "頻度": "often", "優先度": "priority", "雲量": "cloud", "分解能": "res",
            "オフナディア角": "ona", "入射角": "inc", "時間帯": "tod", "1日の回数": "times", "依頼者": "who"}
    return "\n".join(f"{k}: {lines.get(v, 'なし')}" for k, v in keys.items())


# ---- the days ---------------------------------------------------------------------------------------------


def test_the_period_a_text_names():
    p = tr.period_of
    assert p("来週月曜から2週間、横須賀港を光学で", TODAY)[:2] == (date(2026, 11, 23), date(2026, 12, 6))
    assert p("12月末まで延長", TODAY)[:2] == (None, date(2026, 12, 31))
    assert p("11/25〜30に1回", TODAY)[:2] == (date(2026, 11, 25), date(2026, 11, 30))
    assert p("11月25日から12月3日まで", TODAY)[:2] == (date(2026, 11, 25), date(2026, 12, 3))
    assert p("明日から3日間", TODAY)[:2] == (date(2026, 11, 22), date(2026, 11, 24))
    assert p("来週中に1回", TODAY)[:2] == (date(2026, 11, 23), date(2026, 11, 29))
    assert p("12月上旬", TODAY)[:2] == (date(2026, 12, 1), date(2026, 12, 10))
    assert p("12月中に", TODAY)[:2] == (date(2026, 12, 1), date(2026, 12, 31))
    assert p("2026年12月1日〜2027年1月31日", TODAY)[:2] == (date(2026, 12, 1), date(2027, 1, 31))
    assert p("12/20〜1/10", TODAY)[:2] == (date(2026, 12, 20), date(2027, 1, 10))
    assert p("期限は12/5です", TODAY)[:2] == (None, date(2026, 12, 5))
    assert p("12/10までに撮影", TODAY)[:2] == (None, date(2026, 12, 10))
    # Days ahead: a date long past without a year is next year's.
    assert p("1/10から", TODAY)[:2] == (date(2027, 1, 10), None)
    assert p("来週の水曜日", TODAY)[:2] == (date(2026, 11, 25), date(2026, 11, 25))
    assert p("今週末", TODAY)[:2] == (date(2026, 11, 21), date(2026, 11, 22))
    assert p("明日", TODAY)[:2] == (date(2026, 11, 22), date(2026, 11, 22))
    assert p("今月中に1回", TODAY)[:2] == (TODAY, date(2026, 11, 30))
    assert p("できるだけ早く", TODAY)[:2] == (None, None)
    # A weekday of a repeat is not a day.
    assert p("毎週月・木", TODAY)[:2] == (None, None)


def test_a_request_extended():
    assert tr.extension_of("1週間延長してください", date(2026, 11, 30)) == date(2026, 12, 7)
    assert tr.extension_of("2か月延長", date(2026, 11, 30)) == date(2027, 1, 30)
    assert tr.extension_of("延長してください", date(2026, 11, 30)) is None


# ---- how often and the other items -----------------------------------------------------------------------


def test_how_often():
    o = tr.often_of
    assert o("毎週月・木に撮影") == tr.Often("曜日", (0, 3), words="毎週月・木")
    assert o("月曜と木曜に毎週").weekdays == (0, 3)
    assert (o("隔日で").kind, o("隔日で").every) == ("間隔", 2)
    assert o("3日おきに").every == 4 and o("2日ごとに").every == 2
    assert o("毎週").every == 7
    assert o("毎日").kind == "毎日" and o("期間中に1回撮れれば").kind == "1回"
    assert o("1日1回") is None and o("1日2回") is None


def test_numbers_with_their_units():
    assert tr.cloud_of("雲量30%以下")[0] == 31 and tr.cloud_of("雲30％未満")[0] == 30
    assert tr.cloud_of("雲量30%以上なら中止")[0] == 30 and tr.cloud_of("雲は3割まで")[0] == 31
    assert tr.cloud_of("分解能1m") is None
    assert tr.resolution_of("分解能1m以下")[0] == 1 and tr.resolution_of("50cm級")[0] == 0.5
    assert tr.resolution_of("解像度 0.5m")[0] == 0.5 and tr.resolution_of("雲量30%") is None
    assert tr.angles_of("オフナディア角20度以下", tr.OFF_NADIR)[:2] == (None, 20)
    assert tr.angles_of("オフナディア10〜25°", tr.OFF_NADIR)[:2] == (10, 25)
    assert tr.angles_of("入射角30〜45度", tr.INCIDENCE)[:2] == (30, 45)
    assert tr.angles_of("入射角30度以上", tr.INCIDENCE)[:2] == (30, None)
    assert tr.times_of("1日2回")[:2] == (2, None) and tr.times_of("1日2〜3回")[:2] == (2, 3)
    assert tr.times_of("1日1回") is None


def test_words_of_a_closed_set():
    assert tr.priority_of("至急でお願いします")[0] == 100 and tr.priority_of("最優先")[0] == 100
    assert tr.priority_of("急ぎません")[0] == 700 and tr.priority_of("余裕があれば")[0] == 700
    assert tr.priority_of("優先度は高")[0] == 300 and tr.priority_of("ついでに")[0] == 900
    assert tr.priority_of("横須賀港") is None
    assert tr.kind_of("SARで") == "SAR" and tr.kind_of("光学で") == "光学" and tr.kind_of("光学またはSAR") == "任意"
    assert tr.orbit_of("昇交で") == "昇交" and tr.orbit_of("昇交でも降交でも") is None
    assert tr.look_of("右観測で") == "右" and tr.look_of("左ルック") == "左"
    assert tr.time_of_day_of("夜間に") == "夜" and tr.time_of_day_of("日中") == "昼" and tr.time_of_day_of("昼夜問わず") is None
    # Either time allowed, or one left out (the real 3B trial, 2026-11-21).
    assert tr.time_of_day_of("夜間もOK") is None and tr.time_of_day_of("夜でも構いません") is None
    assert tr.time_of_day_of("夜は不要です") == "昼" and tr.time_of_day_of("日中は不可、夜間のみ") == "夜"
    assert tr.priority_of("急ぎではありません")[0] == 700 and tr.priority_of("急ぎじゃないので")[0] == 700
    assert tr.priority_of("急ぎで")[0] == 300
    assert tr.number_of("管理番号", "管理番号: K-0123、識別番号は ID77") == ("K-0123", "管理番号: K-0123")
    assert tr.number_of("識別番号", "管理番号: K-0123、識別番号は ID77")[0] == "ID77"


def test_the_satellites_named():
    s = tr.satellites_of("WV03かWorldView-2で、ALOS2のスポットライトも", SATS)
    assert s.satellites == ("WorldView-3", "WorldView-2", "ALOS-2") and s.series == ()
    s = tr.satellites_of("WorldViewシリーズで", SATS)
    assert s.series == ("WorldView",) and s.satellites == ()
    s = tr.satellites_of("だいち2号の Spotlight モード", SATS)
    assert s.satellites == ("ALOS-2",) and s.modes == ("Spotlight",)
    # Not inside another word.
    assert tr.satellites_of("OPTIONAL", SATS).empty


def test_places_and_coordinates():
    assert tr.places_in("横須賀港と佐世保の港を", ["横須賀", "横須賀港", "佐世保", "呉"]) == ["横須賀港", "佐世保"]
    points = tr.points_of("北緯35度17分 東経139度40分 付近、35.2833, 139.6667")
    assert len(points) == 2 and abs(points[0][0] - 139.6667) < 0.001 and abs(points[1][1] - 35.2833) < 0.001


# ---- the draft --------------------------------------------------------------------------------------------

MAIL = "来週月曜から2週間、横須賀港を光学で毎日お願いします。雲量30%以下、分解能1m以下。至急です。\n第2課 山田"


def test_a_new_request_from_a_mail():
    raw = answer(name="横須賀港 光学 毎日", place="横須賀港", period="2026-11-23 〜 2026-12-06", often="毎日", priority="最優先",
                 cloud="30%以下", res="1m以下", who="第2課 山田")
    d = tr.request_draft(raw, MAIL, TODAY, SATS, ["横須賀港", "佐世保"])
    v = d.values
    assert v["name"] == "横須賀港 光学 毎日"
    assert v["period"] == (date(2026, 11, 23), date(2026, 12, 6))
    assert (v["category"], v["achieve"]) == ("臨時", "毎回") and "repeat" not in v
    assert v["priority"] == 100 and d.why["priority"] == "至急"
    assert v["kind"] == "光学" and v["cloud_no_go"] == 31 and v["resolution"] == 1
    assert v["requester"] == "第2課 山田"
    assert d.places == ["横須賀港"] and not d.checks
    assert d.text_of("cloud_no_go") == "実施しない雲量: 31%（31% 以上なら実施しない）（「雲量30%以下」から）"
    assert d.text_of("period") == "期間: 2026-11-23（月） 〜 2026-12-06（日）（「来週月曜から2週間」から）"


def test_what_the_words_name_wins_over_the_model():
    # The model reads 「毎日」 as 曜日 and invents a period, a priority and a cloud: the words win or it is left out.
    raw = answer(name="呉 SAR", place="呉、佐世保", period="2026-11-21 〜 2026-12-31", often="曜日:月", priority="高",
                 cloud="20%以下", who="田中")
    d = tr.request_draft(raw, "呉をSARで毎週月・木、12月末まで", TODAY, SATS)
    v = d.values
    assert v["period"] == (None, date(2026, 12, 31))
    assert (v["category"], v["achieve"], v["repeat"]) == ("定点", "毎回", "曜日:月木")
    assert v["kind"] == "SAR" and "priority" not in v and "cloud_no_go" not in v and "requester" not in v
    assert d.places == ["呉"]
    assert any("優先度「高」" in c for c in d.checks) and any("佐世保" in c for c in d.checks)
    assert any("依頼者「田中」" in c for c in d.checks) and any("雲量" in c for c in d.checks)


def test_the_models_reading_kept_where_the_words_give_it_a_footing():
    # No day the code reads, but a date cue: the model's period is taken, to be checked.
    d = tr.request_draft(answer(period="2026-12-01 〜 2026-12-07"), "12月の最初の週に1回", TODAY)
    assert d.values["period"] == (date(2026, 12, 1), date(2026, 12, 31))
    d = tr.request_draft(answer(period="2026-12-01 〜 2026-12-07"), "なるべく早く横須賀を", TODAY)
    assert "period" not in d.values and any("期間" in c for c in d.checks)
    # A name with a number the text does not have is left out; the first place stands in.
    d = tr.request_draft(answer(name="横須賀港 2025", place="横須賀港"), "横須賀港を光学で", TODAY)
    assert d.values["name"] == "横須賀港 光学" and any("2025" in c for c in d.checks)
    # The model's 雲量 in other words.
    d = tr.request_draft(answer(cloud="20%まで"), "雲はせいぜい20%までで", TODAY)
    assert d.values["cloud_no_go"] == 21


def test_several_places_and_coordinates():
    d = tr.request_draft(answer(place="横須賀港、佐世保港、呉"), "横須賀港・佐世保港・呉を同じ条件で", TODAY, place_names=["佐世保港"])
    assert d.places == ["横須賀港", "佐世保港", "呉"]
    d = tr.request_draft("", "北緯35度17分 東経139度40分 付近を至急", TODAY)
    assert d.points and d.values["priority"] == 100


def request(**kw):
    first, _ = day_bounds(date(2026, 11, 10))
    _, last = day_bounds(date(2026, 11, 30))
    r = ImagingRequest(id="R-1", name="横須賀港 光学 定点", category="定点", achieve="毎回", start=first, end=last, priority=500,
                       kind="光学", cloud_no_go=31, requester="第2課 山田", rings=[[[139, 35], [140, 35], [140, 36], [139, 35]]])
    for k, v in kw.items():
        setattr(r, k, v)
    return r


def test_editing_changes_only_what_the_text_names():
    r = request()
    raw = answer(name="横須賀港 延長", place="横須賀港", period="2026-11-21 〜 2026-12-31", who="第2課 山田")
    d = tr.request_draft(raw, "横須賀港の依頼を12月末まで延長してください", TODAY, SATS, ["横須賀港"], request=r)
    assert d.values == {"period": (None, date(2026, 12, 31))}
    assert not d.area_asked and not d.places
    # 「1週間延長」 from the request's end; 中止 stops it.
    d = tr.request_draft("", "1週間延長で", TODAY, request=r)
    assert d.values == {"period": (None, date(2026, 12, 7))}
    d = tr.request_draft("", "この依頼は中止してください", TODAY, request=r)
    assert d.values == {"status": "停止"}
    # What it already has is no change; a name or an area only when asked to change.
    d = tr.request_draft(answer(name="佐世保港"), "雲量30%以下のまま、優先度を最優先に", TODAY, request=r)
    assert d.values == {"priority": 100}
    d = tr.request_draft(answer(name="佐世保港 光学", place="佐世保港"), "名称を佐世保港 光学に、範囲を佐世保港に変更", TODAY,
                         place_names=["佐世保港"], request=r)
    assert d.values == {"name": "佐世保港 光学"} and d.area_asked and d.places == ["佐世保港"]


def test_the_draft_taken_into_a_request():
    r = request(note="既存のメモ")
    d = tr.request_draft("", "12月末まで延長、オフナディア角20度以下、1日2回", TODAY, request=r)
    out = tr.applied(r, d, [k for k, _ in tr.ITEMS], "12月末まで延長、オフナディア角20度以下、1日2回", now=datetime(2026, 11, 21, 16))
    assert jst_day(out.start) == date(2026, 11, 10) and jst_day(out.end) == date(2026, 12, 31)
    assert (out.off_nadir_min, out.off_nadir_max) == (None, 20) and (out.times_min, out.times_max) == (2, None)
    assert out.note == "既存のメモ\n依頼文（2026-11-21）: 12月末まで延長、オフナディア角20度以下、1日2回"
    # Only the items chosen; no note when not asked.
    out = tr.applied(r, d, ["times"], "x", note=False)
    assert out.off_nadir_max is None and out.end == r.end and out.note == "既存のメモ" and out.times_min == 2
    new = tr.request_draft("", "WorldViewシリーズで毎週火曜", TODAY, SATS)
    out = tr.applied(ImagingRequest(id="N"), new, [k for k, _ in tr.ITEMS], note=False)
    assert out.series == ["WorldView"] and out.repeat == "曜日:火" and out.category == "定点"


def test_the_conversation_and_the_settings():
    task = ta.task_of("requestDraft", "large")
    assert task.label == "依頼文から下書き" and task.max_tokens == 240 and "requestDraft" in ta.LARGE_TASKS
    m = tr.request_messages(task, MAIL, TODAY)
    assert m[0]["content"] == ta.REQUEST_PROMPT and "来週 2026-11-23 〜 2026-11-29" in m[-1]["content"]
    assert m[-1]["content"].endswith(MAIL[:100] + MAIL[100:]) or MAIL.strip() in m[-1]["content"]
    edit = tr.request_messages(task, "12月末まで延長", TODAY, request())
    assert "直す依頼: 横須賀港 光学 定点（期間 2026-11-10 〜 2026-11-30）" in edit[-1]["content"]
    problems: list = []
    s = ta.text_assist_settings_of({"models": {"large": "m3"}, "tasks": {"requestDraft": {
        "model": "large", "examples": [{"text": "明日横須賀を", "answer": "名称: 横須賀\n地名: 横須賀"}]}}}, problems)
    assert not problems and s.has("requestDraft") and s.task("requestDraft").examples == (("明日横須賀を", "名称: 横須賀\n地名: 横須賀"),)
    m = tr.request_messages(s.task("requestDraft"), "x", TODAY)
    assert [x["role"] for x in m] == ["system", "user", "assistant", "user"]
    ta.text_assist_settings_of({"models": {"large": "m3"}, "tasks": {"requestDraft": {"model": "large", "examples": [{"text": 1}]}}},
                               problems)
    assert problems and "requestDraft.examples[0]" in problems[0]


# ---- the areas ------------------------------------------------------------------------------------------


def test_the_areas_a_place_can_be():
    from mikage.core.obs.alerts import NamedArea

    ring = [[139.6, 35.2], [139.7, 35.2], [139.7, 35.3], [139.6, 35.3], [139.6, 35.2]]
    areas = [NamedArea("横須賀", [ring], "市", size=100.0), NamedArea("横須賀港", [ring], "港湾", size=5.0),
             NamedArea("横須賀港北", [ring], "港湾", size=1.0), NamedArea("佐世保港", [ring], "港湾", size=4.0, name_en="Sasebo")]
    got = tr.area_choices("横須賀港", areas)
    # The same name first, then those holding it or held by it, the smaller first.
    assert [c.label for c in got] == ["横須賀港（地名テーブル・港湾）", "横須賀港北（地名テーブル・港湾）", "横須賀（地名テーブル・市）"]
    assert got[0].kind == "範囲" and got[0].rings == [ring] and got[0].source == tr.NAMED
    assert [c.label for c in tr.area_choices("sasebo", areas)] == ["佐世保港（地名テーブル・港湾）"]
    assert tr.area_choices("", areas) == [] and tr.area_choices("呉", areas) == []
    # Requests before of that name, the latest first, one area once.
    other = [[[129.7, 33.1], [129.8, 33.1], [129.8, 33.2], [129.7, 33.1]]]
    before = [ImagingRequest(id="a", name="呉 光学", rings=other, updated=100.0, shape_kind="点"),
              ImagingRequest(id="b", name="呉 SAR", rings=other, updated=200.0),
              ImagingRequest(id="c", name="呉港 定点", rings=[ring], updated=50.0),
              ImagingRequest(id="d", name="呉 範囲なし")]
    got = tr.area_choices("呉", areas, before)
    assert [c.label.split("（")[0] for c in got] == ["呉 SAR", "呉港 定点"]
    assert got[0].source == tr.BEFORE and got[0].kind == "範囲"


def test_a_point_or_a_place_found_as_a_square():
    from mikage.core.geocode import Place

    c = tr.point_choice(139.65, 35.28, 2.0)
    assert c.kind == "点" and c.label == "35.2800N 139.6500E（座標・一辺 2 km）" and len(c.rings[0]) == 5
    xs = [x for x, _y in c.rings[0]]
    assert min(xs) < 139.65 < max(xs)
    f = tr.found_choice("呉", Place("呉市", (132.56, 34.25), "国土地理院"), 1.0)
    assert f.kind == "点" and f.place == "呉" and f.label == "呉市（地名・住所検索・国土地理院・一辺 1 km）"


def test_the_names_of_one_request_for_each_place():
    d = tr.RequestDraft(values={"name": "横須賀港 光学 定点", "kind": "光学"})
    assert tr.batch_names(d, ["横須賀港", "佐世保港", "呉"]) == ["横須賀港 光学 定点", "佐世保港 光学 定点", "呉 光学 定点"]
    assert tr.batch_names(tr.RequestDraft(values={"kind": "SAR"}), ["呉", "舞鶴"]) == ["呉 SAR", "舞鶴 SAR"]
    assert tr.batch_names(tr.RequestDraft(values={"name": "艦艇の動向"}), ["呉", "舞鶴"]) == ["呉 艦艇の動向", "舞鶴 艦艇の動向"]
    # A name of several of the places: each place with the 種別.
    assert tr.batch_names(tr.RequestDraft(values={"name": "横須賀港と呉の撮像", "kind": "光学"}), ["横須賀港", "呉"]) == ["横須賀港 光学", "呉 光学"]
