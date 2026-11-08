"""The satellites of an imaging request picked in the imaging plan's tree (core/request/pick.py)."""
from dataclasses import replace

from skywatch.core.plan.satellites import SatelliteSpec
from skywatch.core.plan.tree import SatelliteChoice
from skywatch.core.request.model import ImagingRequest
from skywatch.core.request.pick import Picked, checks_of, picked, summary, unknown
from skywatch.core.request.route import why_not

BASE = SatelliteSpec(name="", id="", tle1="", tle2="", maxOffNadir=30, minOffNadir=0, swath=10, length=10, scenesPerPass=1,
                     minInterval=0, maxPerDay=0, leadTime=0, series="", lookSide="both", sar=False, source="test")


def sat(name, series, mode=None, primary=None, sar=False, aliases=(), sensor=None):
    return replace(BASE, name=name, id=name.replace(" ", ""), series=series, mode=mode, primary=primary, sar=sar,
                   aliases=aliases, sensor=sensor)


SATS = [
    sat("WorldView-3", "WorldView", aliases=("WV03",)),
    sat("WorldView-2", "WorldView"),
    sat("OPT 1", "OPT", "Spot", True, sensor="PAN"),
    sat("OPT 1", "OPT", "Strip", False, sensor="PAN"),
    sat("RADAR 1", "RADAR", sar=True),
]


def choice():
    c = SatelliteChoice(whole_satellites=True)
    c.set(SATS, lambda s, first: False)
    return c


def checked_names(c):
    return sorted({f"{l.sat.name}{'/' + l.sat.mode if l.sat.mode else ''}" for l in c.listed if l.checked})


def apply(c, p):
    for key, on in checks_of(c.listed, p.series, p.satellites, p.modes).items():
        next(l for l in c.listed if l.key == key).checked = on


def test_nothing_checked_is_no_condition_and_a_whole_series_is_the_series():
    c = choice()
    assert picked(c.listed) == Picked()
    assert summary(picked(c.listed)) == "指定なし（どの衛星でも）"
    c.choose([l for l in c.listed if l.sat.series == "OPT"], True)
    # A group takes every mode of its satellites (any mode will do for a request).
    assert checked_names(c) == ["OPT 1/Spot", "OPT 1/Strip"]
    assert picked(c.listed) == Picked(series=["OPT"])
    c.choose([l for l in c.listed if l.sat.series == "RADAR"], True)
    assert picked(c.listed) == Picked(series=["OPT", "RADAR"])


def test_part_of_a_series_is_named_satellites_in_the_order_they_were_written():
    c = choice()
    for l in c.listed:
        l.checked = l.sat.name in ("WorldView-2", "RADAR 1")
    assert picked(c.listed) == Picked(satellites=["WorldView-2", "RADAR 1"])
    # Already named keep their place, new ones follow.
    assert picked(c.listed, Picked(satellites=["RADAR 1"])).satellites == ["RADAR 1", "WorldView-2"]
    # Series and satellites both would have to hold: with part of a series, the whole series is named satellite by satellite.
    for l in c.listed:
        l.checked = l.sat.series == "OPT" or l.sat.name == "WorldView-3"
    p = picked(c.listed)
    assert p == Picked(satellites=["WorldView-3", "OPT 1"])
    req = ImagingRequest(id="x", series=p.series, satellites=p.satellites, modes=p.modes)
    assert [s.name for s in SATS if why_not(req, s) is None] == ["WorldView-3", "OPT 1", "OPT 1"]


def test_a_mode_left_out_is_written_as_the_modes_checked():
    c = choice()
    for l in c.listed:
        l.checked = l.sat.mode == "Spot"
    assert picked(c.listed) == Picked(series=["OPT"], modes=["Spot"])
    req = ImagingRequest(id="x", series=["OPT"], modes=["Spot"])
    assert [s.mode for s in SATS if why_not(req, s) is None] == ["Spot"]


def test_ledger_names_are_shown_as_checks_and_written_back_as_they_were():
    c = choice()
    # An alias (satelliteAliases) and a sensor name stand for the catalog's satellite and modes.
    before = Picked(satellites=["WV03"], modes=[])
    apply(c, before)
    assert checked_names(c) == ["WorldView-3"]
    assert picked(c.listed, before) == before
    before = Picked(series=["opt"], modes=["PAN"])
    apply(c, before)
    assert checked_names(c) == ["OPT 1/Spot", "OPT 1/Strip"]
    assert picked(c.listed, before) == Picked(series=["opt"])
    # Nothing given: nothing checked.
    apply(c, Picked())
    assert checked_names(c) == []


def test_names_the_catalog_does_not_know_are_kept():
    c = choice()
    before = Picked(series=["OLD"], satellites=["WV03", "GONE-1"], modes=["X"])
    assert unknown(c.listed, before.series, before.satellites, before.modes) == Picked(["OLD"], ["GONE-1"], ["X"])
    apply(c, Picked(satellites=["WV03"]))
    assert picked(c.listed, before) == Picked(series=["OLD"], satellites=["WV03", "GONE-1"], modes=["X"])
    assert summary(Picked(series=["A"], satellites=["B", "C"], modes=["M"])) == "衛星シリーズ A・衛星 B → C・センサーモード M"


def test_the_plan_tree_still_checks_a_satellite_by_its_usual_mode():
    c = SatelliteChoice()
    c.set(SATS)
    c.check_all(False)
    c.choose(c.listed, True)
    assert sorted(l.sat.mode or "" for l in c.listed if l.checked) == ["", "", "", "Spot"]
