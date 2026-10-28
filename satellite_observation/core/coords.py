"""
Coordinate notations, as the viewer reads and writes them (the package
`coordinate-notation`, `src/index.ts` and `src/utm.ts`, with MGRS from the
`mgrs` 2.2.0 package it depends on): latitude / longitude (decimal or
degrees, minutes, seconds), MGRS and UTM. Everything goes through WGS 84
longitude / latitude, longitude first (as GeoJSON).

UTM is written `54N 386543 3950123`: the zone, then N or S for the
hemisphere, easting and northing in metres.
"""
from __future__ import annotations

import math
import re
import unicodedata
from typing import Optional

LonLat = tuple[float, float]


def _js_round(x: float) -> int:
    """JavaScript's Math.round (halves go up)."""
    return math.floor(x + 0.5)


def _fixed(x: float, digits: int) -> str:
    """JavaScript's Number.prototype.toFixed."""
    # -0 is written without its sign; a small negative number keeps it (`-0.000000`).
    return f"{0.0 if x == 0 else x:.{digits}f}"


# --- Writing -----------------------------------------------------------------

def format_lat_lon(lon_lat: LonLat) -> str:
    """`35.681200, 139.767100` (latitude first)."""
    lon, lat = lon_lat
    return f"{_fixed(lat, 6)}, {_fixed(lon, 6)}"


def format_dms(lon_lat: LonLat) -> str:
    """`35°40'52.32"N 139°46'01.56"E` (latitude first, seconds to 0.01″)."""
    lon, lat = lon_lat

    def part(value: float, plus: str, minus: str, width: int) -> str:
        # Rounded once in hundredths of a second, so 59.999″ carries into the minute.
        hundredths = _js_round(abs(value) * 360000)
        d = hundredths // 360000
        m = (hundredths % 360000) // 6000
        s = (hundredths % 6000) / 100
        return f"{str(d).zfill(width)}°{str(m).zfill(2)}'{_fixed(s, 2).zfill(5)}\"{minus if value < 0 else plus}"

    return f"{part(lat, 'N', 'S', 2)} {part(lon, 'E', 'W', 3)}"


def format_mgrs(lon_lat: LonLat) -> Optional[str]:
    """`54S UE 88433 49290` (1 m precision); None outside the MGRS area (the poles)."""
    try:
        m = re.match(r"^([0-9]{1,2}[A-Z])([A-Z]{2})([0-9]{5})([0-9]{5})$", mgrs_forward(lon_lat, 5))
    except ValueError:
        return None
    return f"{m[1]} {m[2]} {m[3]} {m[4]}" if m else None


def utm_zone(lon_lat: LonLat) -> Optional[int]:
    """The UTM zone of a point, with the Norway and Svalbard exceptions (as MGRS uses)."""
    try:
        return int(re.match(r"^[0-9]{1,2}", mgrs_forward(lon_lat, 1))[0])
    except ValueError:
        return None


def utm_epsg(zone: int, north: bool) -> str:
    """The EPSG code of a UTM zone on WGS 84."""
    return f"EPSG:{(32600 if north else 32700) + zone}"


def format_utm(lon_lat: LonLat) -> Optional[str]:
    """`54N 386543 3950123`; None outside UTM (beyond 84°N / 80°S)."""
    zone = utm_zone(lon_lat)
    if zone is None:
        return None
    north = lon_lat[1] >= 0
    e, n = to_utm(lon_lat, zone, north)
    return f"{zone}{'N' if north else 'S'} {_js_round(e)} {_js_round(n)}"


#: The notations of the cursor position (the viewer's CURSOR_NOTATIONS): id, label, writer.
NOTATIONS = (
    ("decimal", "10進", format_lat_lon),
    ("dms", "度分秒", format_dms),
    ("mgrs", "MGRS", format_mgrs),
    ("utm", "UTM", format_utm),
)


def format_as(notation: str, lon_lat: LonLat) -> Optional[str]:
    """The point in a notation of NOTATIONS (decimal degrees for one not known)."""
    for id_, _, write in NOTATIONS:
        if id_ == notation:
            return write(lon_lat)
    return format_lat_lon(lon_lat)


# --- Reading -----------------------------------------------------------------

def parse_coordinate(text: str) -> Optional[LonLat]:
    """
    Reads a coordinate typed in any of the supported notations:
    - latitude, longitude, in decimal degrees or degrees, minutes and seconds:
      `35.6812, 139.7671`, `N35.6812 E139.7671`, `139.7671E 35.6812N`,
      `35°40'52.3"N 139°46'1.6"E`, `35 40 52.3 139 46 1.6`, `35d40m52s N`,
      `北緯35度40分52秒 東経139度46分1秒`, packed `354052N 1394601E`,
      `lat 35.68 lon 139.76`, `POINT(139.76 35.68)` (WKT, longitude first),
      full-width digits and Japanese punctuation. Without N/S/E/W or a label,
      latitude comes first, unless only the first value can be a longitude
      (`139.76, 35.68`);
    - MGRS: `54SUE8843349290`, `54S UE 88433 49290` (any even number of digits);
    - UTM: `54N 386543 3950123` (zone, N or S, easting, northing; `E`/`N` or `m` after the numbers are allowed).
    Returns (lon, lat), or None when the text is none of them.
    """
    t = _normalize(text)
    if not t:
        return None
    for parse in (_parse_mgrs, _parse_utm, _parse_wkt, _parse_lat_lon):
        found = parse(t)
        if found is not None:
            return found
    return None


def _normalize(text: str) -> str:
    """Full-width characters, Japanese words and marks, labels and odd minus signs to one plain form."""
    t = unicodedata.normalize("NFKC", text)
    t = re.sub("[‐-―−﹣－]", "-", t)
    t = re.sub(r"[、，;；|\t]", ",", t)
    # Labels name the axis: `lat 35.6 lon 139.7`, `緯度: 35.6`; the sign stays with the number.
    t = re.sub(r"緯度\s*[:=]?\s*", " N ", t)
    t = re.sub(r"経度\s*[:=]?\s*", " E ", t)
    t = t.replace("北緯", " N ").replace("南緯", " S ").replace("東経", " E ").replace("西経", " W ")
    t = t.replace("度", "°").replace("分", "'").replace("秒", '"')
    t = t.upper()
    t = re.sub(r"(?:LATITUDE|LAT)\s*[:=]?\s*", " N ", t)
    t = re.sub(r"(?:LONGITUDE|LONG|LNG|LON)\s*[:=]?\s*", " E ", t)
    t = re.sub(r"[()[\]{}]", " ", t)
    return re.sub(r"\s+", " ", t).strip()


_NUM = r"-?[0-9]+(?:\.[0-9]+)?"
_WKT = re.compile(rf"^POINT ?Z? ?({_NUM}) ({_NUM})(?: {_NUM})?$")
_MGRS = re.compile(r"^([0-9]{1,2})([C-HJ-NP-X])([A-HJ-NP-Z]{2})([0-9]*)$")
_UTM = re.compile(r"^([0-9]{1,2}) ?([NS])[ ,]+([0-9]+(?:\.[0-9]+)?) ?(?:M ?)?E?[ ,]+([0-9]+(?:\.[0-9]+)?) ?(?:M ?)?N?$")


def _parse_wkt(t: str) -> Optional[LonLat]:
    """WKT `POINT(lon lat)`."""
    m = _WKT.match(t)
    return _valid(float(m[1]), float(m[2])) if m else None


def _parse_mgrs(t: str) -> Optional[LonLat]:
    compact = t.replace(" ", "")
    m = _MGRS.match(compact)
    if not m or len(m[4]) % 2 != 0 or len(m[4]) > 10 or not 1 <= int(m[1]) <= 60:
        return None
    try:
        lon, lat = mgrs_to_point(compact)
    except ValueError:
        return None
    return _valid(lon, lat)


def _parse_utm(t: str) -> Optional[LonLat]:
    m = _UTM.match(t)
    if not m:
        return None
    zone, easting, northing = int(m[1]), float(m[3]), float(m[4])
    if zone < 1 or zone > 60 or easting < 100_000 or easting > 900_000 or northing > 10_000_000:
        return None
    lon, lat = from_utm((easting, northing), zone, m[2] == "N")
    return _valid(lon, lat)


def _parse_lat_lon(t: str) -> Optional[LonLat]:
    # Degree, minute and second marks (also `35d40m52s`) become spaces; a comma separates the two values.
    s = re.sub(r"([0-9]) ?M ?([0-9]+(?:\.[0-9]+)?) ?S(?![0-9.])", r"\1 \2 ", t)
    s = re.sub(r"([0-9]) ?D ?(?=[0-9 ]|$)", r"\1 ", s)
    s = re.sub(r"([0-9]) ?M(?=[ 0-9]|$)", r"\1 ", s)
    s = re.sub("[°º˚'’′‘`\"”″“]", " ", s)
    s = re.sub(r" ?, ?", ",", s)
    s = re.sub(r"\s+", " ", s).strip()
    if "," in s:
        halves = [h for h in s.split(",") if h.strip()]
    else:
        # Split at the hemisphere letters, else by the count of numbers.
        numbers = re.findall(r"[-+]?[0-9]+(?:\.[0-9]+)?", s)
        if re.search(r"[NSEW]", s):
            halves = _split_by_letters(s)
        elif len(numbers) in (2, 4, 6):
            k = len(numbers) // 2
            halves = [" ".join(numbers[:k]), " ".join(numbers[k:])]
        else:
            return None
    if len(halves) != 2:
        return None
    a, b = _read_angle(halves[0]), _read_angle(halves[1])
    if a is None or b is None:
        return None
    if a[1] == "lon" or b[1] == "lat":
        lon, lat = a[0], b[0]
    elif not a[1] and not b[1] and abs(a[0]) > 90 and abs(b[0]) <= 90:
        lon, lat = a[0], b[0]
    else:
        lat, lon = a[0], b[0]
    return _valid(lon, lat)


def _split_by_letters(s: str) -> list[str]:
    """`N35 40 52 E139 46 1` or `35 40 52 N 139 46 1 E` → the two halves."""
    letters = [m.start() for m in re.finditer(r"[NSEW]", s)]
    if len(letters) != 2:
        return []
    first = letters[0]
    # A letter before the first number marks the start of each half; else it ends it.
    leading = not re.search(r"[0-9]", s[:first])
    cut = letters[1] if leading else first + 1
    return [s[:cut], s[cut:]]


_ANGLE = re.compile(r"^[-+]?[0-9]+(?:\.[0-9]+)?(?: [0-9]+(?:\.[0-9]+)?){0,2}$")


def _read_angle(half: str) -> Optional[tuple[float, Optional[str]]]:
    found = re.search(r"[NSEW]", half)
    letter = found[0] if found else None
    rest = re.sub(r"[NSEW]", " ", half).strip()
    if not _ANGLE.match(rest):
        return None
    parts = [float(x) for x in rest.split(" ")] if " " in rest else _unpack(rest)
    d, m, sec = (parts + [0.0, 0.0])[:3]
    if m >= 60 or sec >= 60:
        return None
    value = abs(d) + m / 60 + sec / 3600
    if d < 0 or rest.startswith("-") or letter in ("S", "W"):
        value = -value
    return value, "lat" if letter in ("N", "S") else "lon" if letter in ("E", "W") else None


def _unpack(text: str) -> list[float]:
    """
    One number: decimal degrees, or degrees, minutes and seconds written
    together (`354052.3` = 35° 40′ 52.3″, `1394601`, `3540` = 35° 40′) when it
    is too large to be degrees.
    """
    value = float(text)
    digits = re.match(r"^[-+]?([0-9]+)", text)[1]
    if abs(value) <= 180 or len(digits) < 4 or len(digits) > 7:
        return [value]
    sign = -1 if text.startswith("-") else 1
    fraction = text[text.index(digits) + len(digits):]
    if len(digits) <= 5:
        return [sign * float(digits[:-2]), float(digits[-2:] + fraction)]
    return [sign * float(digits[:-4]), float(digits[-4:-2]), float(digits[-2:] + fraction)]


def _valid(lon: float, lat: float) -> Optional[LonLat]:
    return (lon, lat) if math.isfinite(lon) and math.isfinite(lat) and abs(lat) <= 90 and abs(lon) <= 180 else None


# --- UTM (coordinate-notation's utm.ts) ----------------------------------------
# The transverse Mercator projection in Krüger's series to sixth order in n
# (Karney 2011), accurate to well under a millimetre within a zone and its neighbours.

_A = 6378137
_F = 1 / 298.257223563
_K0 = 0.9996
_E = math.sqrt(_F * (2 - _F))
_N = _F / (2 - _F)
_n2, _n3, _n4, _n5, _n6 = _N ** 2, _N ** 3, _N ** 4, _N ** 5, _N ** 6
#: Radius of the rectifying sphere, times the scale on the central meridian.
_KA = (_K0 * _A) / (1 + _N) * (1 + _n2 / 4 + _n4 / 64 + _n6 / 256)
_ALPHA = (
    _N / 2 - (2 / 3) * _n2 + (5 / 16) * _n3 + (41 / 180) * _n4 - (127 / 288) * _n5 + (7891 / 37800) * _n6,
    (13 / 48) * _n2 - (3 / 5) * _n3 + (557 / 1440) * _n4 + (281 / 630) * _n5 - (1983433 / 1935360) * _n6,
    (61 / 240) * _n3 - (103 / 140) * _n4 + (15061 / 26880) * _n5 + (167603 / 181440) * _n6,
    (49561 / 161280) * _n4 - (179 / 168) * _n5 + (6601661 / 7257600) * _n6,
    (34729 / 80640) * _n5 - (3418889 / 1995840) * _n6,
    (212378941 / 319334400) * _n6,
)
_BETA = (
    _N / 2 - (2 / 3) * _n2 + (37 / 96) * _n3 - (1 / 360) * _n4 - (81 / 512) * _n5 + (96199 / 604800) * _n6,
    (1 / 48) * _n2 + (1 / 15) * _n3 - (437 / 1440) * _n4 + (46 / 105) * _n5 - (1118711 / 3870720) * _n6,
    (17 / 480) * _n3 - (37 / 840) * _n4 - (209 / 4480) * _n5 + (5569 / 90720) * _n6,
    (4397 / 161280) * _n4 - (11 / 504) * _n5 - (830251 / 7257600) * _n6,
    (4583 / 161280) * _n5 - (108847 / 3991680) * _n6,
    (20648693 / 638668800) * _n6,
)
_RAD = math.pi / 180


def _central_meridian(zone: int) -> float:
    return (zone * 6 - 183) * _RAD


def _conformal_tan(tau: float) -> float:
    """Conformal latitude's tangent from the geodetic latitude's tangent."""
    sigma = math.sinh(_E * math.atanh((_E * tau) / math.hypot(1, tau)))
    return tau * math.hypot(1, sigma) - sigma * math.hypot(1, tau)


def to_utm(lon_lat: LonLat, zone: int, north: bool) -> tuple[float, float]:
    """(lon, lat) in degrees → (easting, northing) in metres in the given zone."""
    lon, lat = lon_lat
    lam = lon * _RAD - _central_meridian(zone)
    tau_p = _conformal_tan(math.tan(lat * _RAD))
    cos_l = math.cos(lam)
    xi_p = math.atan2(tau_p, cos_l)
    eta_p = math.asinh(math.sin(lam) / math.hypot(tau_p, cos_l))
    xi, eta = xi_p, eta_p
    for j in range(1, 7):
        xi += _ALPHA[j - 1] * math.sin(2 * j * xi_p) * math.cosh(2 * j * eta_p)
        eta += _ALPHA[j - 1] * math.cos(2 * j * xi_p) * math.sinh(2 * j * eta_p)
    return 500_000 + _KA * eta, _KA * xi + (0 if north else 10_000_000)


def from_utm(en: tuple[float, float], zone: int, north: bool) -> LonLat:
    """(easting, northing) in metres in the given zone → (lon, lat) in degrees."""
    easting, northing = en
    xi = (northing - (0 if north else 10_000_000)) / _KA
    eta = (easting - 500_000) / _KA
    xi_p, eta_p = xi, eta
    for j in range(1, 7):
        xi_p -= _BETA[j - 1] * math.sin(2 * j * xi) * math.cosh(2 * j * eta)
        eta_p -= _BETA[j - 1] * math.cos(2 * j * xi) * math.sinh(2 * j * eta)
    sinh_eta = math.sinh(eta_p)
    cos_xi = math.cos(xi_p)
    tau_p = math.sin(xi_p) / math.hypot(sinh_eta, cos_xi)
    # Newton's method for the geodetic latitude whose conformal latitude is tau_p.
    e2 = _E * _E
    tau = tau_p
    for _ in range(10):
        tau_i = _conformal_tan(tau)
        delta = ((tau_p - tau_i) / math.hypot(1, tau_i)) * ((1 + (1 - e2) * tau * tau) / ((1 - e2) * math.hypot(1, tau)))
        tau += delta
        if abs(delta) < 1e-12:
            break
    lam = math.atan2(sinh_eta, cos_xi)
    return (lam + _central_meridian(zone)) / _RAD, math.atan(tau) / _RAD


# --- MGRS (the `mgrs` package 2.2.0, as coordinate-notation uses it) -----------
# Its own UTM (Snyder's series on WGS 84 with e² = 0.00669438), so the letters
# and digits come out as the viewer's.

_ECC2 = 0.00669438
_SET_COLUMNS = "AJSAJS"
_SET_ROWS = "AFAFAF"
_CA, _CI, _CO, _CV, _CZ = 65, 73, 79, 86, 90
_BANDS = "CDEFGHJKLMNPQRSTUVWX"
_MIN_NORTHING = {
    "C": 1100000, "D": 2000000, "E": 2800000, "F": 3700000, "G": 4600000, "H": 5500000, "J": 6400000,
    "K": 7300000, "L": 8200000, "M": 9100000, "N": 0, "P": 800000, "Q": 1700000, "R": 2600000,
    "S": 3500000, "T": 4400000, "U": 5300000, "V": 6200000, "W": 7000000, "X": 7900000,
}


def mgrs_forward(lon_lat: LonLat, accuracy: int = 5) -> str:
    """MGRS without spaces (`54SUE8843349290`); raises ValueError outside 80°S–84°N."""
    lon, lat = lon_lat
    if not -180 <= lon <= 180 or not -80 <= lat <= 84:
        raise ValueError("MGRS の範囲外")
    zone, letter, easting, northing = _ll_to_utm(lat, lon)
    e, n = ("00000" + str(easting))[-5:], ("00000" + str(northing))[-5:]
    return f"{zone}{letter}{_100k_id(easting, northing, zone)}{e[:accuracy]}{n[:accuracy]}"


def _ll_to_utm(lat: float, lon: float) -> tuple[int, str, int, int]:
    lat_r, lon_r = math.radians(lat), math.radians(lon)
    zone = math.floor((lon + 180) / 6) + 1
    if lon == 180:
        zone = 60
    if 56 <= lat < 64 and 3 <= lon < 12:
        zone = 32
    if 72 <= lat < 84:
        if 0 <= lon < 9:
            zone = 31
        elif 9 <= lon < 21:
            zone = 33
        elif 21 <= lon < 33:
            zone = 35
        elif 33 <= lon < 42:
            zone = 37
    origin = math.radians((zone - 1) * 6 - 180 + 3)
    e2, a = _ECC2, _A
    ep2 = e2 / (1 - e2)
    n_ = a / math.sqrt(1 - e2 * math.sin(lat_r) * math.sin(lat_r))
    t = math.tan(lat_r) * math.tan(lat_r)
    c = ep2 * math.cos(lat_r) * math.cos(lat_r)
    aa = math.cos(lat_r) * (lon_r - origin)
    m = a * ((1 - e2 / 4 - 3 * e2 * e2 / 64 - 5 * e2 * e2 * e2 / 256) * lat_r
             - (3 * e2 / 8 + 3 * e2 * e2 / 32 + 45 * e2 * e2 * e2 / 1024) * math.sin(2 * lat_r)
             + (15 * e2 * e2 / 256 + 45 * e2 * e2 * e2 / 1024) * math.sin(4 * lat_r)
             - (35 * e2 * e2 * e2 / 3072) * math.sin(6 * lat_r))
    easting = _K0 * n_ * (aa + (1 - t + c) * aa ** 3 / 6 + (5 - 18 * t + t * t + 72 * c - 58 * ep2) * aa ** 5 / 120) + 500000
    northing = _K0 * (m + n_ * math.tan(lat_r) * (aa * aa / 2 + (5 - t + 9 * c + 4 * c * c) * aa ** 4 / 24
                                                  + (61 - 58 * t + t * t + 600 * c - 330 * ep2) * aa ** 6 / 720))
    if lat < 0:
        northing += 10000000
    return zone, _letter(lat), math.trunc(easting), math.trunc(northing)


def _letter(lat: float) -> str:
    if 72 <= lat <= 84:
        return "X"
    if -80 <= lat < 72:
        return _BANDS[math.floor((lat + 80) / 8)]
    return "Z"


def _set_for_zone(zone: int) -> int:
    return zone % 6 or 6


def _100k_id(easting: int, northing: int, zone: int) -> str:
    column, row, parm = easting // 100000, (northing // 100000) % 20, _set_for_zone(zone)
    col_origin, row_origin = ord(_SET_COLUMNS[parm - 1]), ord(_SET_ROWS[parm - 1])
    col, rw = col_origin + column - 1, row_origin + row
    rollover = False
    if col > _CZ:
        col, rollover = col - _CZ + _CA - 1, True
    if col == _CI or (col_origin < _CI < col) or ((col > _CI or col_origin < _CI) and rollover):
        col += 1
    if col == _CO or (col_origin < _CO < col) or ((col > _CO or col_origin < _CO) and rollover):
        col += 1
        if col == _CI:
            col += 1
    if col > _CZ:
        col = col - _CZ + _CA - 1
    if rw > _CV:
        rw, rollover = rw - _CV + _CA - 1, True
    else:
        rollover = False
    if rw == _CI or (row_origin < _CI < rw) or ((rw > _CI or row_origin < _CI) and rollover):
        rw += 1
    if rw == _CO or (row_origin < _CO < rw) or ((rw > _CO or row_origin < _CO) and rollover):
        rw += 1
        if rw == _CI:
            rw += 1
    if rw > _CV:
        rw = rw - _CV + _CA - 1
    return chr(col) + chr(rw)


def mgrs_to_point(text: str) -> LonLat:
    """The middle of an MGRS square (the `mgrs` package's toPoint); raises ValueError when it is not MGRS."""
    zone, letter, easting, northing, accuracy = _decode(text.upper())
    lon0, lat0 = _utm_to_ll(easting, northing, zone, letter)
    lon1, lat1 = _utm_to_ll(easting + accuracy, northing + accuracy, zone, letter)
    return (lon0 + lon1) / 2, (lat1 + lat0) / 2


def _decode(text: str) -> tuple[int, str, float, float, float]:
    s = text.replace(" ", "")
    m = re.match(r"^([0-9]{1,2})([A-Z])([A-Z]{2})([0-9]*)$", s)
    if not m:
        raise ValueError(f"MGRS として読み込み不可: {text}")
    zone, letter, hun_k, digits = int(m[1]), m[2], m[3], m[4]
    if letter <= "A" or letter in "BYIO" or letter >= "Z":
        raise ValueError(f"MGRS の帯の文字 {letter} は対象外")
    parm = _set_for_zone(zone)
    east100k = _easting_from_char(hun_k[0], parm)
    north100k = _northing_from_char(hun_k[1], parm)
    while north100k < _MIN_NORTHING[letter]:
        north100k += 2000000
    if len(digits) % 2:
        raise ValueError("MGRS の桁数は偶数")
    sep = len(digits) // 2
    bonus = 100000 / 10 ** sep
    e = float(digits[:sep]) * bonus if sep else 0.0
    n = float(digits[sep:]) * bonus if sep else 0.0
    return zone, letter, e + east100k, n + north100k, bonus


def _easting_from_char(ch: str, parm: int) -> float:
    cur, value, rewound = ord(_SET_COLUMNS[parm - 1]), 100000, False
    while cur != ord(ch):
        cur += 1
        if cur == _CI:
            cur += 1
        if cur == _CO:
            cur += 1
        if cur > _CZ:
            if rewound:
                raise ValueError(f"MGRS の文字 {ch} は対象外")
            cur, rewound = _CA, True
        value += 100000
    return value


def _northing_from_char(ch: str, parm: int) -> float:
    if ch > "V":
        raise ValueError(f"MGRS の文字 {ch} は対象外")
    cur, value, rewound = ord(_SET_ROWS[parm - 1]), 0, False
    while cur != ord(ch):
        cur += 1
        if cur == _CI:
            cur += 1
        if cur == _CO:
            cur += 1
        if cur > _CV:
            if rewound:
                raise ValueError(f"MGRS の文字 {ch} は対象外")
            cur, rewound = _CA, True
        value += 100000
    return value


def _utm_to_ll(easting: float, northing: float, zone: int, letter: str) -> LonLat:
    e2, a = _ECC2, _A
    e1 = (1 - math.sqrt(1 - e2)) / (1 + math.sqrt(1 - e2))
    x = easting - 500000
    y = northing - (10000000 if letter < "N" else 0)
    origin = (zone - 1) * 6 - 180 + 3
    ep2 = e2 / (1 - e2)
    mu = (y / _K0) / (a * (1 - e2 / 4 - 3 * e2 * e2 / 64 - 5 * e2 * e2 * e2 / 256))
    phi1 = (mu + (3 * e1 / 2 - 27 * e1 ** 3 / 32) * math.sin(2 * mu) + (21 * e1 * e1 / 16 - 55 * e1 ** 4 / 32) * math.sin(4 * mu)
            + (151 * e1 ** 3 / 96) * math.sin(6 * mu))
    n1 = a / math.sqrt(1 - e2 * math.sin(phi1) * math.sin(phi1))
    t1 = math.tan(phi1) * math.tan(phi1)
    c1 = ep2 * math.cos(phi1) * math.cos(phi1)
    r1 = a * (1 - e2) / math.pow(1 - e2 * math.sin(phi1) * math.sin(phi1), 1.5)
    d = x / (n1 * _K0)
    lat = phi1 - (n1 * math.tan(phi1) / r1) * (d * d / 2 - (5 + 3 * t1 + 10 * c1 - 4 * c1 * c1 - 9 * ep2) * d ** 4 / 24
                                              + (61 + 90 * t1 + 298 * c1 + 45 * t1 * t1 - 252 * ep2 - 3 * c1 * c1) * d ** 6 / 720)
    lon = (d - (1 + 2 * t1 + c1) * d ** 3 / 6 + (5 - 2 * c1 + 28 * t1 - 3 * c1 * c1 + 8 * ep2 + 24 * t1 * t1) * d ** 5 / 120) / math.cos(phi1)
    return origin + math.degrees(lon), math.degrees(lat)
