"""地図の投影法と向き (core/map_view.py): which way goes up, QGIS's rotation, the UTM zone, the fit of a turned map."""
import math

import pytest

from mikage.core.geo_fix import heading_from_text, look_side
from mikage.core.map_view import (
    Directions, bearing_of, bearing_up, fit_box, heading_from_look, off_nadir_of, rotation_for, screen_axes, turned_text,
    utm_epsg,
)


def test_each_way_up_from_the_satellites_direction_and_heading():
    d = Directions(toward=100.0, toward_from="catalog", heading=190.0, heading_from="file", off_nadir=25.0)
    # The look (from the satellite to the ground): away from the satellite; the satellite itself; the flight.
    assert bearing_up("look", d) == (280.0, "カタログの方位角")
    assert bearing_up("satellite", d) == (100.0, "カタログの方位角")
    assert bearing_up("flight", d) == (190.0, "画像のメタデータ")
    assert bearing_up("north", d) == (None, "")


def test_not_known_or_from_straight_down_stays_north_up_with_the_reason():
    up, why = bearing_up("look", Directions())
    assert up is None and "衛星の方位がわからない" in why
    up, why = bearing_up("flight", Directions(toward=100.0))
    assert up is None and "進行方向がわからない" in why
    # Optical from nearly straight down: its satellite's direction means little.
    up, why = bearing_up("satellite", Directions(toward=100.0, off_nadir=1.5))
    assert up is None and "ほぼ真下" in why and "1.5°" in why
    assert bearing_up("look", Directions(toward=100.0, off_nadir=3.0))[0] == 280.0


def test_qgis_rotation_puts_the_bearing_at_the_top():
    # QGIS turns its map clockwise: north is shown `rotation` degrees clockwise from the top.
    assert rotation_for(0) == 0
    assert rotation_for(80) == -80
    assert rotation_for(280) == 80
    assert rotation_for(180) == 180
    assert turned_text(-80) == "−80°" and turned_text(80.4) == "80°"


def test_the_utm_zone_of_a_place():
    assert utm_epsg(139.7, 35.6) == 32654
    assert utm_epsg(135.0, 34.7) == 32653
    assert utm_epsg(-70.6, -33.4) == 32719
    assert utm_epsg(180.0, 10) == 32601 and utm_epsg(-180.0, 10) == 32601


def test_the_catalogs_angle_as_off_nadir():
    assert off_nadir_of(-12, "offNadir") == 12
    # An incidence through the Earth's curvature (smaller off-nadir), an elevation from the horizon.
    assert 30 < off_nadir_of(35, "incidence") < 35
    assert off_nadir_of(90, "elevation") == pytest.approx(0, abs=1e-9)
    assert off_nadir_of(None) is None


def test_a_radars_flight_from_its_look_and_the_metadata():
    # Right-looking: the satellite is to the left of its track (toward = heading − 90).
    assert heading_from_look(260.0, None) == 350.0
    assert heading_from_look(260.0, "R") == 350.0
    assert heading_from_look(80.0, "L") == 350.0
    assert heading_from_text("platform_heading = -10") == 350.0
    assert heading_from_text("sun_heading = 30") is None
    assert 188 < heading_from_text("pass = DESCENDING", 35) < 191
    assert heading_from_text("pass = DESCENDING") is None
    assert look_side("look_side = left") == "L" and look_side("") is None
    assert bearing_of(1, 0) == 90 and bearing_of(0, -1) == 180 and bearing_of(0, 0) is None


def test_the_box_that_fits_a_turned_map():
    # QGIS: with rotation 30 an extent 400 × 200 around (200, 100) shows the turned rectangle with these corners.
    corners = [(-23.2050807569, 86.6025403784), (323.2050807569, 286.6025403784), (423.2050807569, 113.3974596216),
               (76.7949192431, -86.6025403784)]
    cx, cy, w, h = fit_box(corners, 30, margin=1.0)
    assert (cx, cy) == pytest.approx((200, 100)) and (w, h) == pytest.approx((400, 200))
    # Not turned: the box itself.
    assert fit_box([(0, 0), (10, 4)], 0, 1.0) == pytest.approx((5, 2, 10, 4))
    (rx, ry), (ux, uy) = screen_axes(90)
    # Turned 90°: north shows to the right, so the screen's top is west.
    assert (rx, ry) == pytest.approx((0, 1), abs=1e-12) and (ux, uy) == pytest.approx((-1, 0), abs=1e-12)
    assert fit_box([], 10) is None
    assert math.isclose(fit_box([(0, 0), (10, 0)], 90, 1.0)[3], 10)
