"""
画像カタログ's sensors by satellite and kind (core/catalog/sensors.py; tsudo 2026-11-25: 「画像検索の文から生成AIで
やるけん、衛星の種別も拾えないかな」): the センサー box's several sensors, a sensor's satellites and kind (the
satellite catalog's kind attribute, its other names, its series), what the card's words name in a catalog, and what
文で操作's 画像検索 reads from a sentence (by code, as its other values).
"""
from datetime import date
from types import SimpleNamespace as NS

from mikage.core import text_command as tc
from mikage.core.catalog.config import catalog_of
from mikage.core.catalog.query import CatalogSearch, where_of
from mikage.core.catalog.sensors import (
    OPTICAL, SAR, kind_word, kinds_in, pick_sensors, sensor_kind, sensor_list, sensors_named, words_of,
)
from mikage.core.plan.api import SatelliteNames, sample_satellites, satellites_from_text
from mikage.tests.test_plan import LINE1, LINE2


def sat(name, series, kind, aliases=(), id=None):
    return NS(name=name, id=id or name, series=series, aliases=tuple(aliases), sar=kind == "SAR", kind=kind)


SATS = [sat("ALOS-2", "ALOS", "SAR", ["だいち2号"], "39766"), sat("ICEYE-X27", "ICEYE", "SAR"), sat("ICEYE-X30", "ICEYE", "SAR"),
        sat("WorldView-3", "WorldView", "光学", id="40115"), sat("ASNARO-1", "ASNARO", "光学"),
        # The catalog gives no kind (no `fields.kind`, or blank).
        sat("SkySat-C12", "SkySat", None)]
#: The image catalog's sensors (as its SENSOR attribute says).
VALUES = ["ALOS-2", "ICEYE-X27", "ICEYE-X30", "ICEYE-X31", "WV03", "WorldView-2", "ASNARO-1", "SkySat", "GE01", "TerraSAR-X"]
#: `satelliteAliases`: WV03 is WorldView-3.
NAMES = SatelliteNames.of([("WorldView-3", "40115", "WV03")])


def test_the_sentence_names_kinds_and_the_box_takes_them_alone():
    assert kinds_in("横須賀で直近30日に撮ったSAR画像を出して") == [SAR]
    assert kinds_in("光学とＳＡＲの画像") == [OPTICAL, SAR] and kinds_in("合成開口レーダーの画像") == [SAR]
    # Not inside a name (TerraSAR-X, ASNARO), nor 「サービス」.
    assert kinds_in("TerraSAR-Xの画像") == [] and kinds_in("ASNAROの画像") == [] and kinds_in("呉の画像") == []
    assert [kind_word(w) for w in ("SAR", "sar", "レーダー", "SAR画像", "光学衛星", "可視", "ICEYE", "TerraSAR-X")] == [
        SAR, SAR, SAR, SAR, OPTICAL, OPTICAL, None, None]


def test_the_box_takes_several_sensors_any_of_them():
    assert sensor_list("ICEYE-X27、 ASNARO-2 、ICEYE-X27、") == ["ICEYE-X27", "ASNARO-2"]
    # A comma may be in a sensor's name: only 「、」 separates.
    assert sensor_list("WV03,PAN") == ["WV03,PAN"] and sensor_list("") == []
    assert words_of("ICEYE, SAR、 WV03") == ["ICEYE", "SAR", "WV03"]
    catalog = catalog_of({"label": "c", "url": "https://x.example/arcgis/rest/services/I/FeatureServer/0",
                          "fields": {"id": "ID", "sensor": "SENSOR", "source": "PATH"}}, [], 0)
    one = where_of(catalog, {}, CatalogSearch(sensor="WV03"))
    several = where_of(catalog, {}, CatalogSearch(sensor="ICEYE-X27、O'NEIL"))
    assert one == "SENSOR = 'WV03'" and several == "SENSOR IN ('ICEYE-X27', 'O''NEIL')"


def test_a_sensors_kind_comes_from_its_satellites():
    kinds = {v: sensor_kind(v, SATS, NAMES) for v in VALUES}
    assert kinds == {
        "ALOS-2": SAR, "ICEYE-X27": SAR, "ICEYE-X30": SAR,
        # Not in the satellite catalog: its series'.
        "ICEYE-X31": SAR, "WorldView-2": OPTICAL,
        # An other name (satelliteAliases).
        "WV03": OPTICAL, "ASNARO-1": OPTICAL,
        # No kind given, no satellite: not known; a name that says SAR.
        "SkySat": None, "GE01": None, "TerraSAR-X": SAR}
    # A series of both kinds: not known (ALOS-2 SAR, ALOS-3 光学), its satellites' own still are.
    both = [*SATS, sat("ALOS-3", "ALOS", "光学")]
    assert sensor_kind("ALOS-4", both) is None and sensor_kind("ALOS-2", both) == SAR and sensor_kind("ALOS-3", both) == OPTICAL
    # The catalog's kind: pasted satellites and the samples say theirs; one without a kind attribute none.
    pasted = satellites_from_text(f"A\n{LINE1}\n{LINE2}", 20, 0, 5)[0]
    assert (pasted.kind, satellites_from_text(f"A\n{LINE1}\n{LINE2}", 20, 0, 5, sar=True)[0].kind) == ("光学", "SAR")
    assert [s.kind for s in sample_satellites()] == ["光学", "SAR"]


def test_the_words_name_the_catalogs_sensors():
    def picked(*words):
        p = pick_sensors(list(words), VALUES, SATS, NAMES)
        return p.sensors, p.unknown, p.missing

    assert picked("SAR") == (["ALOS-2", "ICEYE-X27", "ICEYE-X30", "ICEYE-X31", "TerraSAR-X"], ["SkySat", "GE01"], [])
    assert picked("光学") == (["WV03", "WorldView-2", "ASNARO-1"], ["SkySat", "GE01"], [])
    # A satellite by its name or other name, a series, a sensor as written, a name the satellite catalog does not have.
    assert picked("WorldView-3") == (["WV03"], [], []) and picked("だいち2号") == (["ALOS-2"], [], [])
    assert picked("ICEYE") == (["ICEYE-X27", "ICEYE-X30", "ICEYE-X31"], [], [])
    assert picked("GE01") == (["GE01"], [], []) and picked("WorldView") == (["WV03", "WorldView-2"], [], [])
    # Names with a kind: those named of that kind (or of none known).
    assert picked("ICEYE", "SkySat", "光学") == (["SkySat"], [], [])
    assert picked("XYZ", "ALOS-2") == (["ALOS-2"], [], ["XYZ"])
    # A kind none of the catalog's sensors is.
    assert pick_sensors(["SAR"], ["WV03"], SATS, NAMES).missing == ["SAR"]
    # The catalog's sensors not listed (the service can not, or not read yet): the names as written, no kinds.
    unlisted = pick_sensors(["WorldView-3", "SAR"], [], SATS, NAMES)
    assert (unlisted.sensors, unlisted.missing, unlisted.listed) == (["WorldView-3"], ["SAR"], False)
    assert sensors_named("", VALUES, SATS) == []


TODAY = date(2026, 11, 25)
PLACES = ["横須賀", "那覇", "呉"]


def test_the_sentence_is_read_by_code():
    def read(text):
        r = tc.read(tc.BY_KEY["catalog"], text, TODAY, places=PLACES, sats=SATS, sensors=VALUES)
        return r.place, r.sensors

    assert read("那覇の昨日のSAR画像を見たい") == ("那覇", ["SAR"])
    assert read("横須賀の今月の光学の画像を探して") == ("横須賀", ["光学"])
    # A satellite (by an other name), a series, a sensor as the catalog writes it, a series only the catalog has.
    assert read("だいち2号の呉の先週の画像") == ("呉", ["ALOS-2"])
    assert read("ICEYEで撮った横須賀の画像") == ("横須賀", ["ICEYE"])
    assert read("GE01の那覇の画像") == ("那覇", ["GE01"])
    assert tc.read(tc.BY_KEY["catalog"], "ICEYEの那覇の画像", TODAY, places=PLACES, sensors=VALUES).sensors == ["ICEYE"]
    # A satellite named: not its kind too.
    assert read("ICEYEのSAR画像を那覇で") == ("那覇", ["ICEYE"])
    # The kind is not a place.
    assert read("光学の画像を探して") == (None, ["光学"]) and read("SAR画像を那覇で探して") == ("那覇", ["SAR"])
    assert read("呉の画像") == ("呉", [])
    # Only 画像検索 reads them.
    assert tc.read(tc.BY_KEY["jump"], "那覇のSAR画像", TODAY, places=PLACES, sats=SATS).sensors == []
