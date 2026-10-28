"""Coordinate notations (core.coords), with the viewer's own cases (coordinate-notation test/unit/coordinates.test.ts)."""
import pytest

from satellite_observation.core.coords import format_as, format_dms, format_lat_lon, format_mgrs, format_utm, parse_coordinate

# Tokyo Station.
TOKYO = (139.7671, 35.6812)


def near(actual, expected, digits=4):
    assert actual is not None
    assert actual[0] == pytest.approx(expected[0], abs=0.5 * 10 ** -digits)
    assert actual[1] == pytest.approx(expected[1], abs=0.5 * 10 ** -digits)


def test_writes_latitude_first_mgrs_and_utm():
    assert format_lat_lon(TOKYO) == "35.681200, 139.767100"
    assert format_mgrs(TOKYO) == "54S UE 88433 49290"
    assert format_utm(TOKYO) == "54N 388433 3949290"
    assert format_utm((-70.6, -33.45)).startswith("19S ")  # Santiago: southern hemisphere
    assert format_lat_lon((-1e-7, -1e-7)) == "-0.000000, -0.000000"  # as JavaScript's toFixed


def test_round_trips_through_the_parser():
    near(parse_coordinate(format_lat_lon(TOKYO)), TOKYO, 6)
    near(parse_coordinate(format_mgrs(TOKYO)), TOKYO, 4)
    near(parse_coordinate(format_utm(TOKYO)), TOKYO, 4)
    near(parse_coordinate(format_utm((-70.6, -33.45))), (-70.6, -33.45), 4)
    near(parse_coordinate(format_dms(TOKYO)), TOKYO, 5)
    near(parse_coordinate(format_dms((-70.6, -33.45))), (-70.6, -33.45), 5)


def test_writes_degrees_minutes_and_seconds():
    assert format_dms(TOKYO) == "35°40'52.32\"N 139°46'01.56\"E"
    assert format_dms((-70.6, -33.45)) == "33°27'00.00\"S 070°36'00.00\"W"
    # 59.999″ carries into the next minute rather than showing 60.00″.
    assert format_dms((0, 1 - 0.001 / 3600)) == "01°00'00.00\"N 000°00'00.00\"E"


def test_no_mgrs_or_utm_at_the_poles():
    assert format_mgrs((0, 85)) is None and format_utm((0, -81)) is None
    assert format_as("mgrs", (0, 85)) is None
    assert format_as("dms", TOKYO) == format_dms(TOKYO) and format_as("?", TOKYO) == format_lat_lon(TOKYO)


@pytest.mark.parametrize("text", [
    "35.6812, 139.7671", "35.6812 139.7671", "35.6812,139.7671", "N35.6812 E139.7671", "35.6812N 139.7671E",
    "139.7671E, 35.6812N", "E139.7671 N35.6812",
])
def test_decimal_degrees(text):
    near(parse_coordinate(text), TOKYO)


def test_degrees_minutes_and_seconds():
    near(parse_coordinate("35°40'52.32\"N 139°46'1.56\"E"), TOKYO)
    near(parse_coordinate("35 40 52.32 139 46 1.56"), TOKYO)
    near(parse_coordinate("35°40.872′N, 139°46.026′E"), TOKYO)
    near(parse_coordinate("33°27'S 70°36'W"), (-70.6, -33.45))


@pytest.mark.parametrize("text", [
    "北緯35度40分52.32秒 東経139度46分1.56秒", "北緯35.6812度、東経139.7671度", "３５．６８１２，１３９．７６７１",
    "35d40m52.32s N 139d46m1.56s E", "35D40M52.32SN, 139D46M1.56SE", "354052.32N 1394601.56E", "354052.32 1394601.56",
    "35°40′52.32″N139°46′01.56″E", "lat 35.6812 lon 139.7671", "Lng: 139.7671, Lat: 35.6812", "緯度 35.6812 経度 139.7671",
    "(35.6812, 139.7671)", "35.6812; 139.7671", "35.6812 / 139.7671", "139.7671, 35.6812", "POINT(139.7671 35.6812)",
    "35°40.872 139°46.026",
])
def test_more_notations(text):
    near(parse_coordinate(text), TOKYO)


def test_southern_and_western_hemispheres_and_odd_minus_signs():
    near(parse_coordinate("南緯33度27分 西経70度36分"), (-70.6, -33.45))
    near(parse_coordinate("−33.45, −70.6"), (-70.6, -33.45))
    near(parse_coordinate("lat -33.45 lon -70.6"), (-70.6, -33.45))
    near(parse_coordinate("-33.45, -70.6"), (-70.6, -33.45))


def test_mgrs_at_any_precision():
    near(parse_coordinate("54SUE8843349290"), TOKYO, 3)
    near(parse_coordinate("54s ue 88433 49290"), TOKYO, 3)
    near(parse_coordinate("54SUE884492"), TOKYO, 2)


def test_utm_with_n_or_s_for_the_hemisphere():
    zone, e, n = format_utm(TOKYO).split(" ")
    near(parse_coordinate(f"{zone} {e}mE {n}mN"), TOKYO)
    near(parse_coordinate(f"{zone}, {e}, {n}"), TOKYO)


@pytest.mark.parametrize("text", ["", "hello", "95, 100", "354099N 1394601E", "10, 200", "54SUE884", "35.6", "61N 500000 4000000", "東京駅"])
def test_rejects(text):
    assert parse_coordinate(text) is None
