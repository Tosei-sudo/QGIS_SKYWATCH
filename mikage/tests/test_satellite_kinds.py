"""
衛星の種別 (`satelliteKinds`, core/plan/kinds.py; tsudo 2026-11-26: 「衛星の一番大きな種別にIRを足したい /
コンフィグカスタマイズ可能にできないかな？ / 文で操作もそれに応じてほしい」): the kinds of the settings (光学・SAR・IR
without them), a satellite's kind from its catalog's kind attribute, what each kind needs (daylight, clear skies) in the
plan and the requests, the satellite trees, and the kinds sentences name (文で操作's 画像検索, a request's text).
"""
from dataclasses import replace
from datetime import date

import pytest

from mikage.core import text_command as tc
from mikage.core.catalog.sensors import KINDS as CATALOG_KINDS, kind_word, kinds_in, pick_sensors, sensor_kind
from mikage.core.config import parse_config
from mikage.core.plan.access import plan_access
from mikage.core.plan.kinds import DEFAULT_KINDS, kinds, satellite_kinds_of, set_kinds
from mikage.core.plan.modes import with_modes
from mikage.core.plan.satellites import SatelliteSpec, satellite_of, satellites_from_text
from mikage.core.plan.tree import SatelliteChoice
from mikage.core.request.daily import cloudy_names, mixed_kinds, wants_weather
from mikage.core.request.model import KINDS as REQUEST_KINDS, ImagingRequest
from mikage.core.request.route import system_of_satellite, why_not
from mikage.core.request.systems import export_systems_of
from mikage.core.text_request import kind_of
from mikage.core.plan.orbit import sun_elevation
from mikage.tests.test_plan import BELOW, LINE1, LINE2, T, around, east, iss, near, point

URL = "https://gis.example/arcgis/rest/services/Sat/FeatureServer/0"


@pytest.fixture(autouse=True)
def built_in_kinds():
    """Each test starts with the built-in kinds and leaves them so."""
    set_kinds(None)
    yield
    set_kinds(None)


def sat(name, kind, series=None, **kw):
    """A satellite of the catalog whose kind attribute is `kind`."""
    catalog = parse_config({"satelliteCatalogs": [{"url": URL, "fields": {"tle": "TLE", "name": "NAME", "kind": "KIND", "series": "SERIES"}}]})[0]
    got = satellite_of({"TLE": f"{LINE1}\n{LINE2}", "NAME": name, "KIND": kind, "SERIES": series or name}, catalog.satellite_catalogs[0])
    return replace(got, **kw) if kw else got


def request(**kw):
    base = dict(id="R1", name="R1", category="臨時", achieve="期間内1回", start=0, end=1e13, priority=500, kind="任意")
    base.update(kw)
    return ImagingRequest(**base)


def test_without_the_key_the_kinds_are_optical_sar_and_ir():
    config, problems = parse_config({})
    assert problems == [] and config.satellite_kinds is DEFAULT_KINDS
    assert DEFAULT_KINDS.names == ("光学", "SAR", "IR")
    optical, radar, ir = DEFAULT_KINDS
    assert (optical.daylight, optical.clouds) == (True, True)
    assert (radar.daylight, radar.clouds, radar.radar) == (False, False, True)
    # Thermal infrared: by night too, not through clouds.
    assert (ir.daylight, ir.clouds, ir.radar) == (False, True, False)
    assert tuple(REQUEST_KINDS) == ("任意", "光学", "SAR", "IR") and "IR" in REQUEST_KINDS


def test_a_satellites_kind_comes_from_its_kind_attribute():
    of = DEFAULT_KINDS.of_text
    assert [of(t).name for t in ("SAR", "sar", "Radar", "レーダー衛星", "IR", "ｉｒ", "熱赤外", "TIR", "LWIR", "Thermal", "光学", "")] == [
        "SAR", "SAR", "SAR", "SAR", "IR", "IR", "IR", "IR", "IR", "IR", "光学", "光学"]
    # Short-wave infrared bands of an optical satellite, and words with IR in them, stay optical.
    assert [of(t).name for t in ("SWIR", "VNIR", "AIRBUS", "Pan/MS", None)] == ["光学"] * 5
    # A catalog's sarPattern stands for the SAR kind's match.
    import re
    assert of("Synthetic aperture", re.compile("aperture", re.I)).name == "SAR" and of("SAR衛星", re.compile("radar", re.I)).name == "光学"
    # The kind's own name always is that kind.
    assert of("SAR", re.compile("radar", re.I)).name == "SAR"
    s = sat("ASTER-X", "熱赤外")
    assert (s.kind_name, s.sar, s.daylight, s.kind) == ("IR", False, False, "熱赤外")
    assert (sat("ICEYE-X27", "SAR").kind_name, sat("WV-3", "光学").kind_name) == ("SAR", "光学")


def test_the_settings_reorder_rename_and_add_kinds():
    value = [
        {"name": "SAR"},
        {"name": "IR", "daylight": True, "words": ["IR", "近赤外"]},
        {"name": "ハイパー", "match": "hyper|ハイパー", "words": ["ハイパースペクトル", "ハイパー"]},
        "光学",
    ]
    problems = []
    ks = satellite_kinds_of(value, problems)
    assert problems == [] and ks.names == ("SAR", "IR", "ハイパー", "光学")
    sar, ir, hyper, optical = ks
    # A built-in name takes what it does not say from the built-in kind.
    assert sar.match is not None and (sar.daylight, sar.clouds) == (False, False)
    assert (ir.daylight, ir.clouds, ir.words) == (True, True, ("IR", "近赤外")) and ir.match is not None
    # A new kind: daylight and clouds unless it says.
    assert (hyper.daylight, hyper.clouds) == (True, True)
    assert [ks.of_text(t).name for t in ("Hyperspectral", "ハイパー", "SAR", "PAN")] == ["ハイパー", "ハイパー", "SAR", "光学"]
    # The first kind without a match takes the satellites no kind finds.
    assert ks.other is optical


def test_kinds_that_cannot_be_used_are_said():
    problems = []
    ks = satellite_kinds_of([{"name": ""}, {"name": "任意"}, {"name": "光学"}, {"name": "光学"}, {"name": "x" * 17},
                             {"name": "A", "match": "("}, {"name": "B", "words": "IR"}, {"name": "C", "daylight": "yes"}, 3], problems)
    assert ks.names == ("光学", "A", "B", "C")
    assert problems == [
        "satelliteKinds[0] に name（種別の名前）なし",
        "satelliteKinds[1] の name「任意」は依頼の「任意」と同じ",
        "satelliteKinds[3] の name「光学」は重複",
        "satelliteKinds[4] の name は 16 文字まで",
        "satelliteKinds[5] の match は正規表現として読めない",
        "satelliteKinds[6] の words は文字列の配列で指定",
        "satelliteKinds[7] の daylight は true か false で指定",
        "satelliteKinds[8] はオブジェクトで指定",
    ]
    problems = []
    assert satellite_kinds_of([], problems) is DEFAULT_KINDS and problems


def test_the_settings_file_gives_its_kinds_to_its_catalogs_and_modes():
    config, problems = parse_config({
        "satelliteKinds": [{"name": "光学"}, {"name": "SAR"}, {"name": "IR", "daylight": True}],
        "satelliteCatalogs": [{"url": URL, "fields": {"tle": "TLE", "name": "NAME", "kind": "KIND", "sensor": "SENSOR"}}],
        "sensorCatalogs": [{"sensors": [{"sensor": "MULTI", "modes": [{"mode": "PAN", "kind": "光学"}, {"mode": "TIR", "kind": "赤外"}]}]}],
        "requestExports": [
            {"system": "A", "kind": "IR", "columns": [{"key": "ID", "source": "id"}]},
            {"system": "B", "kind": ["光学", "IR"], "columns": [{"key": "ID", "source": "id"}]},
            {"system": "C", "kind": "ハイパー", "columns": [{"key": "ID", "source": "id"}]},
            {"system": "D", "columns": [{"key": "ID", "source": "id"}]},
        ],
    })
    assert problems == ["requestExports[2] の kind「ハイパー」は satelliteKinds にない種別"]
    catalog = config.satellite_catalogs[0]
    assert catalog.kinds is config.satellite_kinds
    # The file's own kinds (IR in daylight only here), not those in use.
    ir = satellite_of({"TLE": f"{LINE1}\n{LINE2}", "NAME": "IR-1", "KIND": "IR"}, catalog)
    assert (ir.kind_name, ir.daylight) == ("IR", True)
    modes = config.sensor_catalogs[0].modes
    assert [(m.mode, m.kind_name, m.sar, m.daylight) for m in modes] == [("PAN", "光学", False, True), ("TIR", "IR", False, True)]
    assert modes[1].values() == {"sensor": "MULTI", "mode": "TIR", "sar": False}
    # A satellite of that sensor is listed once per mode, each of its mode's kind.
    one = satellite_of({"TLE": f"{LINE1}\n{LINE2}", "NAME": "MULTI-1", "KIND": "光学", "SENSOR": "MULTI"}, catalog)
    assert [(s.mode, s.kind_name) for s in with_modes([one], modes)] == [("PAN", "光学"), ("TIR", "IR")]
    systems = config.request_exports
    assert [(s.kind, s.kinds) for s in systems] == [("IR", ("IR",)), ("光学・IR", ("光学", "IR")), ("ハイパー", ("ハイパー",)), ("両方", ())]


def test_a_systems_kinds_choose_its_satellites():
    problems = []
    systems = export_systems_of([{"system": "IRS", "kind": "IR", "series": ["X"], "columns": [{"key": "ID", "source": "id"}]},
                                 {"system": "ANY", "kind": "両方", "series": ["X"], "columns": [{"key": "ID", "source": "id"}]}], problems)
    assert problems == []
    assert system_of_satellite(sat("X-1", "IR", "X"), systems).system == "IRS"
    assert system_of_satellite(sat("X-2", "光学", "X"), systems).system == "ANY"
    problems = []
    export_systems_of([{"system": "Z", "kind": 3, "columns": [{"key": "ID", "source": "id"}]}], problems)
    assert problems == ["requestExports[0] の kind は種別の名前（satelliteKinds の name、光学・SAR など）かその配列、または 両方 で指定"]


def test_a_requests_kind_and_time_of_day_choose_the_satellites():
    optical, radar, ir = sat("OPT-1", "光学"), sat("SAR-1", "SAR"), sat("IR-1", "IR")
    assert [why_not(request(kind="IR"), s) for s in (optical, radar, ir)] == ["種別", "種別", None]
    assert [why_not(request(kind="ir"), s) for s in (optical, radar, ir)] == ["種別", "種別", None]
    # 夜: the kinds that image by night (SAR, IR).
    assert [why_not(request(time_of_day="夜"), s) for s in (optical, radar, ir)] == ["時間帯", None, None]


def test_the_weather_counts_for_the_kinds_clouds_stop():
    optical, radar, ir = sat("OPT-1", "光学"), sat("SAR-1", "SAR"), sat("IR-1", "IR")
    assert wants_weather(request(kind="IR"), [ir]) and not wants_weather(request(kind="SAR"), [radar])
    # Either kind: weighed when every satellite that can take it is one clouds stop (光学 and IR here).
    assert wants_weather(request(), [optical, ir]) and not mixed_kinds(request(), [optical, ir])
    # IR and SAR: the clouds pick between them, as 光学 and SAR.
    assert mixed_kinds(request(), [ir, radar]) and not wants_weather(request(), [ir, radar])
    assert cloudy_names() == "光学・IR"


def test_ir_satellites_image_by_night_in_the_plan():
    target = east(BELOW, 150)
    sun = sun_elevation(target, T)
    dark = replace(around(T), daylight="optical", minSunElevation=sun + 5)

    def ops(s):
        return near(plan_access([s], [point("b", target)], dark).opportunities, T, 600000)

    assert ops(iss()) == []
    assert len(ops(replace(iss(), kind_given="IR"))) == 1
    # An IR kind of the settings that images only in daylight.
    set_kinds(satellite_kinds_of([{"name": "光学"}, {"name": "SAR"}, {"name": "IR", "daylight": True}], []))
    assert ops(replace(iss(), kind_given="IR")) == []


def test_pasted_satellites_take_the_kind_chosen():
    ir = satellites_from_text(f"X\n{LINE1}\n{LINE2}", 30, 0, 10, kind="IR")[0]
    assert (ir.kind_name, ir.sar, ir.daylight, ir.kind) == ("IR", False, False, "IR")
    radar = satellites_from_text(f"X\n{LINE1}\n{LINE2}", 30, 0, 10, sar=True)[0]
    assert (radar.kind_name, radar.sar, radar.daylight) == ("SAR", True, False)
    # Made SAR afterwards, a satellite of no given kind follows.
    assert replace(satellites_from_text(f"X\n{LINE1}\n{LINE2}", 30, 0, 10)[0], sar=True).kind_name == "SAR"


def test_the_trees_top_level_is_the_kinds_in_their_order():
    choice = SatelliteChoice()
    choice.set([sat("IR-1", "IR", "IRS"), sat("OPT-1", "光学", "OPT"), sat("SAR-1", "SAR", "SARS")])
    assert [(g.key, g.name) for g in choice.tree()] == [("optical", "光学"), ("sar", "SAR"), ("kind:IR", "IR")]
    set_kinds(satellite_kinds_of(["IR", "SAR", "光学"], []))
    assert [g.name for g in choice.tree()] == ["IR", "SAR", "光学"]
    # A kind no longer in use (a satellite read before) comes last, not lost.
    set_kinds(satellite_kinds_of(["光学", "SAR"], []))
    assert [g.name for g in choice.tree()] == ["光学", "SAR", "IR"]


def test_sentences_name_the_kinds_by_their_words():
    assert kinds_in("横須賀の昨日の赤外画像") == ["IR"] and kinds_in("熱赤外とSARの画像") == ["IR", "SAR"]
    assert kinds_in("ＩＲで撮った画像") == ["IR"] and kinds_in("AIRBUSの画像") == [] and kinds_in("SWIRの画像") == []
    assert [kind_word(w) for w in ("IR", "赤外画像", "熱赤外線", "サーマル画像", "赤外衛星", "AIR")] == ["IR", "IR", "IR", "IR", "IR", None]
    assert "IR" in CATALOG_KINDS
    # A request's text: one kind, else 任意 for two or more.
    assert kind_of("横須賀港を赤外で毎日") == "IR" and kind_of("光学かIRで") == "任意" and kind_of("TerraSAR-Xで") == "SAR"
    # The kinds of the settings, and their own words.
    set_kinds(satellite_kinds_of(["光学", "SAR", {"name": "ハイパー", "words": ["ハイパースペクトル"]}], []))
    assert kinds_in("呉のハイパースペクトル画像") == ["ハイパー"] and kinds_in("赤外の画像") == []
    assert kind_of("ハイパーで撮って") == "ハイパー"


def test_the_image_search_reads_ir_and_picks_the_ir_sensors():
    sats = [sat("ALOS-2", "SAR", "ALOS"), sat("WorldView-3", "光学", "WorldView"), sat("TIR-1", "熱赤外", "TIR")]
    values = ["ALOS-2", "WorldView-3", "TIR-1", "TIR-2"]
    assert [sensor_kind(v, sats) for v in values] == ["SAR", "光学", "IR", "IR"]
    pick = pick_sensors(["IR"], values, sats)
    assert pick.sensors == ["TIR-1", "TIR-2"] and pick.missing == []
    r = tc.read(tc.BY_KEY["catalog"], "横須賀の昨日の赤外画像を探して", date(2026, 11, 26), [], ["横須賀"], sats, sensors=values)
    assert r.place == "横須賀" and r.sensors == ["IR"]
    # A kind's word is no place, nor the map's extent (「地図の範囲で」, real 3B bench 2026-11-26).
    assert tc.place_word("サーマルの画像を探して") is None and tc.place_word("昨日の赤外画像を地図の範囲で") is None


def test_the_models_line_names_the_kinds_in_use():
    line = tc.op_list([tc.BY_KEY["catalog"]])
    assert "（場所・期間・衛星・光学かSAR/レーダーかIR/赤外か）" in line
    set_kinds(satellite_kinds_of(["SAR", "光学"], []))
    assert "（場所・期間・衛星・SAR/レーダーか光学か）" in tc.op_list([tc.BY_KEY["catalog"]])
    assert kinds().names == ("SAR", "光学")


def test_a_satellite_made_by_hand_is_sar_or_optical_by_its_flag():
    base = dict(name="A", id="A", tle1="", tle2="", maxOffNadir=30, minOffNadir=0, swath=10, length=10, scenesPerPass=1,
                minInterval=0, maxPerDay=0, leadTime=0, series="A", lookSide="both", source="t")
    assert (SatelliteSpec(**base, sar=True).kind_name, SatelliteSpec(**base, sar=True).daylight) == ("SAR", False)
    assert (SatelliteSpec(**base, sar=False).kind_name, SatelliteSpec(**base, sar=False).daylight) == ("光学", True)
    assert SatelliteSpec(**base, sar=False, kind_given="IR").daylight is False
