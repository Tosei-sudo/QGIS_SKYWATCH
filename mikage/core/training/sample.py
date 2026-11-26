"""
The sample services of mock_esri.py: their layers (as the plugin's settings
expect by default), sample features (dates near today), small GeoTIFFs for
the catalog, and the settings file that points the plugin at the mock.

Everything is made here without GDAL or NumPy, so it runs on any Python 3.9+.
"""
from __future__ import annotations

import getpass
import json
import math
import os
import random
import struct
import time
import uuid
from datetime import datetime, timezone
from typing import Optional

DAY = 86_400_000
HOUR = 3_600_000

#: The sea area of the sample images (Tokyo Bay, near Yokosuka): west, south, east, north.
AREA = (139.72, 35.27, 139.78, 35.33)
PIXELS = 512

#: The sample satellites: name, NORAD-like number, series, kind, look side, max off-nadir, swath (km), RAAN offset.
SATELLITES = [
    ("OPT-X1", 99001, "OPT-X", "光学", "both", 30, 12, 0.0),
    ("OPT-X2", 99002, "OPT-X", "光学", "both", 30, 12, 90.0),
    ("SAR-Y1", 99011, "SAR-Y", "SAR", "right", 35, 30, 45.0),
    ("SAR-Y2", 99012, "SAR-Y", "SAR", "right", 35, 30, 225.0),
]

#: Other names of the sample satellites (satellite, alias as written in the table: one, or several with commas).
SATELLITE_ALIASES = [("OPT-X1", "OX1, OPTX-01"), ("OPT-X2", "OX2"), ("SAR-Y1", "SY1")]

#: The sample satellites' elements are for this time (UTC, ms after midnight) of the day before the data is made (the
#: day of the PC's calendar), their nodes turned with the Earth from those at TLE_REFERENCE (UTC ms): the passes over the
#: sample area on the next days (JST, as the plan's days) come at the same hours whenever and wherever the data is made.
#: (Elements of the very moment the data was made, before 2026-11-23, moved the passes with the hour: made before about
#: 11:30 JST, tomorrow had no shot to plan and the plan course stopped at 3-2.)
TLE_TIME_OF_DAY = int(2.5 * HOUR)
TLE_REFERENCE = 1_795_314_600_000  # 2026-11-22 02:30 UTC


# --- TLEs -------------------------------------------------------------------------------------------------


def sidereal_degrees(ms: int) -> float:
    """Greenwich mean sidereal time at `ms` (UTC), in degrees."""
    return (280.46061837 + 360.98564736629 * (ms / DAY + 2440587.5 - 2451545.0)) % 360


def tle_epoch(now: int) -> tuple[int, float]:
    """The sample TLEs' epoch for data made at `now`, and how far their nodes turn from the reference (degrees)."""
    today = datetime.fromtimestamp(now / 1000).date()
    epoch = int(datetime(today.year, today.month, today.day, tzinfo=timezone.utc).timestamp() * 1000) - DAY + TLE_TIME_OF_DAY
    return epoch, sidereal_degrees(epoch) - sidereal_degrees(TLE_REFERENCE)


def _checksum(line: str) -> str:
    return str(sum(int(c) if c.isdigit() else 1 if c == "-" else 0 for c in line) % 10)


def tle(number: int, epoch_ms: int, inclination: float, raan: float, mean_anomaly: float, mean_motion: float) -> tuple[str, str]:
    """A two-line element set (near-circular orbit) with valid checksums."""
    d = datetime.fromtimestamp(epoch_ms / 1000, tz=timezone.utc)
    start = datetime(d.year, 1, 1, tzinfo=timezone.utc)
    day = 1 + (d - start).total_seconds() / 86400
    line1 = f"1 {number:05d}U 26900A   {d.year % 100:02d}{day:012.8f}  .00000100  00000-0  40000-4 0  999"
    line2 = (f"2 {number:05d} {inclination:8.4f} {raan % 360:8.4f} 0001300  90.0000 {mean_anomaly % 360:8.4f} "
             f"{mean_motion:11.8f}{1:5d}")
    return line1 + _checksum(line1), line2 + _checksum(line2)


# --- GeoTIFF ----------------------------------------------------------------------------------------------


def write_geotiff(path: str, width: int, height: int, rgb: bytes, west: float, north: float, dx: float, dy: float) -> None:
    """An uncompressed RGB GeoTIFF in WGS 84 (EPSG:4326)."""
    rows_per_strip = 16
    strips = [(y, min(rows_per_strip, height - y)) for y in range(0, height, rows_per_strip)]
    row_bytes = width * 3
    header = 8
    tags: list[tuple[int, int, int, bytes]] = []  # tag, type, count, value bytes

    def add(tag: int, kind: int, values: list, fmt: str) -> None:
        tags.append((tag, kind, len(values), struct.pack("<" + fmt * len(values), *values)))

    data_offset = None  # filled below
    add(256, 4, [width], "I")
    add(257, 4, [height], "I")
    add(258, 3, [8, 8, 8], "H")
    add(259, 3, [1], "H")
    add(262, 3, [2], "H")
    add(273, 4, [0] * len(strips), "I")  # placeholder
    add(277, 3, [3], "H")
    add(278, 4, [rows_per_strip], "I")
    add(279, 4, [n * row_bytes for _, n in strips], "I")
    add(284, 3, [1], "H")
    add(339, 3, [1, 1, 1], "H")
    add(33550, 12, [dx, dy, 0.0], "d")
    add(33922, 12, [0.0, 0.0, 0.0, west, north, 0.0], "d")
    add(34735, 3, [1, 1, 0, 3, 1024, 0, 1, 2, 1025, 0, 1, 1, 2048, 0, 1, 4326], "H")
    tags.sort()
    ifd_size = 2 + len(tags) * 12 + 4
    extra_at = header + ifd_size
    extra = b""
    entries = []
    for tag, kind, count, value in tags:
        if len(value) <= 4:
            entries.append((tag, kind, count, value.ljust(4, b"\0"), None))
        else:
            if len(extra) % 2:
                extra += b"\0"
            entries.append((tag, kind, count, None, extra_at + len(extra)))
            extra += value
    data_offset = extra_at + len(extra)
    offsets = [data_offset + y * row_bytes for y, _ in strips]
    # Put the real strip offsets in place.
    packed_offsets = struct.pack("<" + "I" * len(offsets), *offsets)
    out_extra = bytearray(extra)
    ifd = bytearray(struct.pack("<H", len(entries)))
    for tag, kind, count, value, at in entries:
        if tag == 273:
            if at is None:
                value = packed_offsets.ljust(4, b"\0")
            else:
                out_extra[at - extra_at:at - extra_at + len(packed_offsets)] = packed_offsets
        ifd += struct.pack("<HHI", tag, kind, count) + (value if at is None else struct.pack("<I", at))
    ifd += struct.pack("<I", 0)
    with open(path, "wb") as f:
        f.write(b"II*\0" + struct.pack("<I", header))
        f.write(bytes(ifd))
        f.write(bytes(out_extra))
        f.write(rgb)


def _inside(px: float, py: float, ship: dict, scale: float) -> bool:
    """Whether a pixel is on a ship (a rotated rectangle, in pixels)."""
    a = math.radians(ship["heading"])
    dx, dy = px - ship["px"], py - ship["py"]
    along = dx * math.sin(a) - dy * math.cos(a)
    across = dx * math.cos(a) + dy * math.sin(a)
    return abs(along) <= ship["length"] / scale / 2 and abs(across) <= ship["width"] / scale / 2


def render(seed: int, ships: list[dict], sar: bool, west: float, north: float, dx: float, dy: float) -> bytes:
    """A sea with a coast to the west and ships on it."""
    rnd = random.Random(seed)
    metres = dx * 111_320 * math.cos(math.radians(north))
    out = bytearray(PIXELS * PIXELS * 3)
    for ship in ships:
        ship["px"] = (ship["lon"] - west) / dx
        ship["py"] = (north - ship["lat"]) / dy
    for y in range(PIXELS):
        coast = 60 + 18 * math.sin(y / 90 + seed) + 6 * math.sin(y / 17)
        for x in range(PIXELS):
            n = rnd.randint(-6, 6)
            if x < coast:
                v = (110 + n, 120 + n, 80 + n) if not sar else (95 + n * 3, 95 + n * 3, 95 + n * 3)
            else:
                v = (20 + n, 45 + n, 70 + n) if not sar else (15 + n, 15 + n, 15 + n)
            i = (y * PIXELS + x) * 3
            out[i], out[i + 1], out[i + 2] = (max(0, min(255, c)) for c in v)
    for ship in ships:
        r = int(ship["length"] / metres) + 2
        for y in range(max(0, int(ship["py"]) - r), min(PIXELS, int(ship["py"]) + r + 1)):
            for x in range(max(0, int(ship["px"]) - r), min(PIXELS, int(ship["px"]) + r + 1)):
                if _inside(x + 0.5, y + 0.5, ship, metres):
                    i = (y * PIXELS + x) * 3
                    out[i], out[i + 1], out[i + 2] = (235, 235, 230) if not sar else (250, 250, 250)
    return bytes(out)


def ship_ring(ship: dict) -> list[list[float]]:
    """The outline of a ship (lon / lat), closed."""
    a = math.radians(ship["heading"])
    k = 111_320 * math.cos(math.radians(ship["lat"]))
    pts = []
    for s_along, s_across in ((1, 1), (1, -1), (-1, -1), (-1, 1), (1, 1)):
        along, across = s_along * ship["length"] / 2, s_across * ship["width"] / 2
        east = along * math.sin(a) + across * math.cos(a)
        north = along * math.cos(a) - across * math.sin(a)
        pts.append([round(ship["lon"] + east / k, 7), round(ship["lat"] + north / 111_320, 7)])
    return pts


# --- the images -------------------------------------------------------------------------------------------

#: The ships, moving a little between images (lon, lat, heading, length, width).
SHIPS = [
    (139.742, 35.312, 30, 180, 28),
    (139.752, 35.292, 120, 120, 20),
    (139.763, 35.318, 75, 90, 16),
    (139.770, 35.284, 160, 60, 12),
]


def _day_start(ms: int) -> int:
    return ms // DAY * DAY


def image_plans(now: Optional[int] = None) -> list[dict]:
    """The sample images: ids, times, satellites, extents and ships (made the same every time for one day)."""
    now = now if now is not None else int(time.time() * 1000)
    today = _day_start(now)
    plans = []
    for k, (sat, days, hour, sar) in enumerate([("OPT-X1", 6, 1.5, False), ("SAR-Y1", 5, 20.8, True), ("OPT-X2", 4, 1.7, False),
                                                ("OPT-X1", 2, 1.5, False), ("SAR-Y2", 1, 9.3, True), ("OPT-X2", 0, 1.6, False)]):
        acquired = today - days * DAY + int(hour * HOUR)
        if acquired > now - HOUR:
            acquired -= DAY
        plans.append(_image_plan(k, sat, acquired, sar))
    return plans


def _image_plan(k: int, sat: str, acquired: int, sar: bool) -> dict:
    rnd = random.Random(k)
    shift = (rnd.uniform(-0.003, 0.003), rnd.uniform(-0.003, 0.003))
    west, south, east, north = AREA[0] + shift[0], AREA[1] + shift[1], AREA[2] + shift[0], AREA[3] + shift[1]
    stamp = datetime.fromtimestamp(acquired / 1000, tz=timezone.utc).strftime("%Y%m%d_%H%M")
    ships = []
    for j, (lon, lat, heading, length, width) in enumerate(SHIPS):
        if (k + j) % 5 == 4:
            continue  # this ship has left
        drift = rnd.uniform(-0.0003, 0.0003)
        ships.append({"n": j, "lon": lon + drift, "lat": lat + drift / 2, "heading": heading + rnd.uniform(-5, 5), "length": length, "width": width})
    return {"id": f"{sat.replace('-', '')}_{stamp}", "satellite": sat, "acquired": acquired, "sar": sar, "box": (west, south, east, north),
            "ships": ships, "seed": k, "angle": round(rnd.uniform(5, 28), 1), "azimuth": round(rnd.uniform(0, 360), 1), "cloud": 0 if sar else round(rnd.uniform(0, 0.3), 2)}


def write_image(files: str, plan: dict) -> str:
    """The plan's GeoTIFF under files/catalog (made once); its path."""
    folder = os.path.join(files, "catalog")
    os.makedirs(folder, exist_ok=True)
    path = os.path.join(folder, plan["id"] + ".tif")
    if not os.path.exists(path):
        west, south, east, north = plan["box"]
        dx, dy = (east - west) / PIXELS, (north - south) / PIXELS
        write_geotiff(path, PIXELS, PIXELS, render(plan["seed"], [dict(s) for s in plan["ships"]], plan["sar"], west, north, dx, dy), west, north, dx, dy)
    return path


def write_images(files: str) -> None:
    for plan in _created_plans(files):
        write_image(files, plan)


def _created_plans(files: str) -> list[dict]:
    path = os.path.join(files, "catalog", "images.json")
    if os.path.exists(path):
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    return []


def _keep_plans(files: str, plans: list[dict]) -> None:
    os.makedirs(os.path.join(files, "catalog"), exist_ok=True)
    with open(os.path.join(files, "catalog", "images.json"), "w", encoding="utf-8") as f:
        json.dump(plans, f, ensure_ascii=False, indent=1)


def _box_ring(box) -> list[list[float]]:
    w, s, e, n = box
    return [[w, s], [w, n], [e, n], [e, s], [w, s]]


def note_of(plan: dict) -> str:
    """The image's 備考 (for 「フリーワード」): a routine shot, or an urgent one with its request number in full-width letters."""
    return "横須賀港 定期観測" if plan.get("seed", 0) % 2 == 0 else "横須賀港 臨時観測（ＥＶ０１）"


def catalog_feature(plan: dict, files: str, base: str) -> dict:
    path = os.path.join(files, "catalog", plan["id"] + ".tif")
    return {"attributes": {
        "IMG_ID": plan["id"], "ACQ_TIME": plan["acquired"], "REG_TIME": plan.get("registered", plan["acquired"] + 3 * HOUR),
        "SYS_TIME": plan.get("registered", plan["acquired"] + 3 * HOUR), "SENSOR_NAME": plan["satellite"], "OFF_NADIR": plan["angle"],
        "SAT_AZIMUTH": plan["azimuth"], "CLOUD": plan["cloud"], "STATUS": "PUBLISHED", "NOTE": note_of(plan),
        "FILE_PATH": path, "COG_URL": f"{base}/files/catalog/{plan['id']}.tif",
    }, "geometry": {"rings": [_box_ring(plan["box"])]}}


def ai_features(plan: dict) -> list[dict]:
    """What the "external AI" found in an image: its ships, and one false alarm on land."""
    rnd = random.Random(plan["seed"] + 100)
    out = []
    for ship in plan["ships"]:
        out.append({"attributes": {"DET_ID": f"{plan['id']}-{ship['n']}", "IMAGE_ID": plan["id"], "CLASS": "ship",
                                   "SCORE": round(rnd.uniform(0.55, 0.98), 2), "MODEL": "mock-detector-1",
                                   "DETECTED_AT": plan["acquired"] + 4 * HOUR, "LENGTH_M": ship["length"], "WIDTH_M": ship["width"],
                                   "HEADING": round(ship["heading"] % 180, 1), "STATUS": "ok"},
                    "geometry": {"rings": [ship_ring(ship)]}})
    west, south, _, north = plan["box"]
    lon, lat = west + 0.004, (south + north) / 2
    out.append({"attributes": {"DET_ID": f"{plan['id']}-fp", "IMAGE_ID": plan["id"], "CLASS": "plane", "SCORE": 0.31, "MODEL": "mock-detector-1",
                               "DETECTED_AT": plan["acquired"] + 4 * HOUR, "STATUS": "ok"},
                "geometry": {"rings": [ship_ring({"lon": lon, "lat": lat, "heading": 0, "length": 40, "width": 40})]}})
    return out


# --- the services -----------------------------------------------------------------------------------------

S, I, D, T, F = "string", "integer", "double", "date", "smallinteger"

OBSERVATION_FIELDS = {
    "OBS_ID": {"type": S, "length": 64}, "IMAGE_ID": S, "IMAGE_NAME": S, "IMAGE_TIME": T, "OBJECT_ID": {"type": S, "length": 64},
    "OBJECT_NAME": S, "KIND": S, "ACTIVITY": S, "PRESENT": F, "NOTE": {"type": S, "length": 4000}, "LENGTH_M": D, "HEADING": D,
    "WIDTH_M": D, "PIXEL_X": D, "PIXEL_Y": D, "IMAGE_FILE": S, "SATELLITE": S, "QUANTITY": I, "CONFIDENCE": S, "OBSERVER": S, "UPDATED_BY": S, "CREATED_AT": T, "UPDATED_AT": T, "DELETED_AT": T, "PROJECT": S,
    "NAMED_AREA_OBJECTID": I, "UNSEEN_REASON": {"type": S, "length": 40},
}
#: As the enterprise geodatabase's attribute rule does (docs/arcgis-setup.md): the OBJECTID of the smallest named area holding the record.
OBSERVATION_RULES = [{"field": "NAMED_AREA_OBJECTID", "smallestContaining": "Alerts/3"}]
#: The named area layer (TBL_NAMED_AREA) with the attributes of the organisation's areas of interest (2026-11-24).
NAMED_AREA_FIELDS = {"NAME": S, "KIND": S, "ACTIVE": F, "NAME_EN": S, "KIND_EN": S, "NAMED_AREA_ID": S,
                     "AREA_USE": {"type": S, "length": 16}, "NAME_KANA": S, "NAME_ALIAS": {"type": S, "length": 1000},
                     "COUNTRY_CODE": {"type": S, "length": 8}, "CATEGORY_ID": {"type": S, "length": 64}, "PARENT_ID": S, "OWNER_GROUP": S,
                     "PRIORITY": I, "COLOR": {"type": S, "length": 32}, "NOTE": {"type": S, "length": 4000}}
#: 管理番号: the stand-in of the attribute rule on the Oracle sequence SEQ_NAMED_AREA_ID (docs/arcgis-setup.md).
NAMED_AREA_RULES = [{"field": "NAMED_AREA_ID", "sequence": {"prefix": "NA-", "digits": 6}}]
AREA_CATEGORY_FIELDS = {"CATEGORY_ID": {"type": S, "length": 64}, "CATEGORY_NAME": S, "CATEGORY_NAME_EN": S,
                        "PARENT_ID": {"type": S, "length": 64}, "SORT_ORDER": I, "COLOR": {"type": S, "length": 32}, "ACTIVE": F,
                        "NOTE": {"type": S, "length": 1000}}
AREA_FIELDS = {"AREA_ID": {"type": S, "length": 64}, "IMAGE_ID": S, "IMAGE_NAME": S, "IMAGE_TIME": T, "KIND": S,
               "NOTE": {"type": S, "length": 4000}, "OBSERVER": S, "CREATED_AT": T, "UPDATED_AT": T, "DELETED_AT": T,
               "UNSEEN_REASON": {"type": S, "length": 40}, "SATELLITE": S}
TAXONOMY_FIELDS = {"PATH": {"type": S, "length": 1000}, "LEVELS": S, "NAME_LABEL": S, "MEMBERS": {"type": S, "length": 8000},
                   "SPECS": {"type": S, "length": 16000}, "PHOTOS": {"type": S, "length": 8000}, "ACTIVITIES": {"type": S, "length": 2000},
                   "SORT_ORDER": I}
#: 分類の既知の個体 (TBL_TAXONOMY_MEMBER): other names and specifications of the classification's individuals.
TAXONOMY_MEMBER_FIELDS = {"MEMBER_ID": {"type": S, "length": 64}, "CLASS_PATH": {"type": S, "length": 1000}, "MEMBER_NAME": S,
                          "ALIASES": {"type": S, "length": 1000}, "SPECS": {"type": S, "length": 16000}, "PHOTOS": {"type": S, "length": 8000},
                          "NOTE": {"type": S, "length": 4000}, "SORT_ORDER": I}
REQUEST_FIELDS = {
    "REQ_ID": {"type": S, "length": 64}, "REQ_NAME": S, "MANAGEMENT_NO": S, "IDENTIFICATION_NO": S, "REQUEST_GROUP": S, "CATEGORY": S, "ACHIEVE": S, "START_AT": T, "END_AT": T, "REPEAT": S, "STATUS": S,
    "PRIORITY": I, "BOOST": I, "KIND": S, "SERIES": S, "SATELLITES": S, "SENSOR_MODES": S, "OFF_NADIR_MIN": D, "OFF_NADIR_MAX": D,
    "INCIDENCE_MIN": D, "INCIDENCE_MAX": D, "LOOK_SIDE": S, "ORBIT_DIR": S, "RESOLUTION_MAX": D, "TIME_OF_DAY": S,
    "TIME_FROM": {"type": S, "length": 5}, "TIME_TO": {"type": S, "length": 5}, "CLOUD_NO_GO": I,
    "TIMES_MIN": I, "TIMES_MAX": I, "TIMES_GAP": D, "SYSTEMS": S, "SHAPE_KIND": S, "REQUESTER": S, "NOTE": {"type": S, "length": 4000}, "CREATED_AT": T, "UPDATED_AT": T,
    "NAMED_AREA_ID": S, "AREA_FOLLOW": F,
}
HISTORY_FIELDS = {"RUN_ID": {"type": S, "length": 64}, "RUN_DATE": T, "TARGET_DAY": S, "TARGET_DATE": T, "REQ_ID": {"type": S, "length": 64},
                  "REQ_NAME": S, "MANAGEMENT_NO": S, "REQUESTER": S, "REQUEST_GROUP": S, "SYSTEM": S,
                  "SATELLITE": S, "PRIORITY_OUT": S, "CLOUD": D, "SCENES": I, "RESULT": S, "REASON": {"type": S, "length": 1000},
                  "OUTPUT_FILE": {"type": S, "length": 1000}, "CAPTURED": S, "IMAGE_ID": S, "BOOST_DONE": I,
                  "ADOPTION": S, "ADOPTION_REASON": {"type": S, "length": 1000}, "PLANNED_AT": T, "EXTERNAL_ID": S,
                  "ADOPTION_SOURCE": {"type": S, "length": 1000}, "MERGED_INTO": S, "SHOT_ID": {"type": S, "length": 64},
                  "OCCURRENCE": I, "LINK_ID": {"type": S, "length": 32}, "PART_NO": I, "NAMED_AREA_ID": S}
SHOT_FIELDS = {"SHOT_ID": {"type": S, "length": 64}, "RUN_ID": {"type": S, "length": 64}, "RUN_DATE": T, "TARGET_DAY": S, "TARGET_DATE": T,
               "SYSTEM": S, "SATELLITE": S, "PASS_TIME": T, "OFF_NADIR": D, "INCIDENCE": D, "ORBIT_DIR": S, "LOOK_SIDE": S, "SCENES": I,
               "SHOT_COUNT": I, "COVERAGE": D, "REQ_ID": {"type": S, "length": 64}, "REQ_IDS": {"type": S, "length": 4000},
               "REQ_NAMES": {"type": S, "length": 4000}, "REQ_COUNT": I, "OUTPUT_FILE": {"type": S, "length": 1000},
               "USE_START": T, "USE_END": T, "PASS_ID": {"type": S, "length": 20}}
#: 依頼の範囲の変更記録 (TBL_REQUEST_AREA_CHANGE, 地域の形に合わせる): with the edit tracking attributes (who and when).
AREA_CHANGE_FIELDS = {"CHANGE_ID": {"type": S, "length": 64}, "NAMED_AREA_ID": S, "REQ_ID": {"type": S, "length": 64}, "REQ_NAME": S,
                      "CHANGE_ACTION": {"type": S, "length": 16}, "REASON": {"type": S, "length": 1000},
                      "CREATED_DATE": T, "CREATED_USER": S, "UPDATED_DATE": T, "UPDATED_USER": S}
GROUP_FIELDS = {"GROUP_NAME": S, "MEMBERS": {"type": S, "length": 4000}, "SORT_ORDER": I, "ACTIVE": F, "NOTE": {"type": S, "length": 1000}}


def _user() -> str:
    """The user name the plugin goes by (USERNAME, as editTracking), so that 「自分のグループ」 has a group in the mock."""
    try:
        return os.environ.get("USERNAME") or getpass.getuser()
    except Exception:  # noqa: BLE001
        return "mock"

ALERT_RULE_FIELDS = {"KIND": {"type": S, "length": 1000}, "ACTIVITIES": {"type": S, "length": 2000}, "MIN_CONFIDENCE": S, "GEOFENCE_NAME": S,
                     "ACTIVE": F, "NOTE": {"type": S, "length": 1000}, "WEIGHT": D}
ALERT_LOG_FIELDS = {"OBS_ID": {"type": S, "length": 64}, "REPORT_NAME": S, "TEXT": {"type": S, "length": 4000}, "REPORTED_AT": T,
                    "REPORTED_BY": S, "BATCH_ID": {"type": S, "length": 64}, "MANUAL": F, "KIND": S}
#: 通報条件 (alertReports.rules): ships moving in the 横須賀 geofence, and every aircraft.
ALERT_RULES = [
    {"KIND": "船舶", "ACTIVITIES": "航行,入港,出港", "MIN_CONFIDENCE": "推定", "GEOFENCE_NAME": "横須賀", "ACTIVE": 1, "NOTE": "港内の船舶の動き",
     "WEIGHT": 1.0},
    {"KIND": "航空機", "ACTIVITIES": "*", "MIN_CONFIDENCE": "", "GEOFENCE_NAME": "", "ACTIVE": 1, "NOTE": "航空機", "WEIGHT": 0.6},
    {"KIND": "車両", "ACTIVITIES": "集積", "MIN_CONFIDENCE": "確定", "GEOFENCE_NAME": "*", "ACTIVE": 0, "NOTE": "（停止中の条件の例）"},
]

#: 地名 (alertReports.namedAreas): nested areas around the sample (the smallest one holding a record is its place),
#: with the organisation's management number (NAMED_AREA_ID).
NAMED_AREAS = [
    ("東京湾", "海域", (139.60, 35.00, 140.10, 35.70), "Tokyo Bay", "bay", "NA-000001"),
    ("横須賀港", "港", (139.70, 35.26, 139.80, 35.34), "Port of Yokosuka", "port", "NA-000101"),
    ("横須賀本港", "施設", (139.735, 35.285, 139.765, 35.315), "Yokosuka Main Port", "facility", "NA-000102"),
]
#: The same rows' attributes of the organisation's areas of interest (組織の関心地域, 2026-11-24), by name, and one more
#: area of interest only (用途 関心地域: not a place of records), with no 管理番号 (the server's stand-in of the rule on
#: SEQ_NAMED_AREA_ID numbers it as it starts: NA-000103).
NAMED_AREA_EXTRA = {
    "東京湾": {"AREA_USE": "地名", "NAME_KANA": "とうきょうわん", "COUNTRY_CODE": "JP"},
    "横須賀港": {"AREA_USE": "両方", "NAME_KANA": "よこすかこう", "NAME_ALIAS": "Yokosuka, 横須賀", "COUNTRY_CODE": "JP",
                 "CATEGORY_ID": "PORT", "PARENT_ID": "NA-000001", "OWNER_GROUP": "第1班", "PRIORITY": 300},
    "横須賀本港": {"AREA_USE": "両方", "NAME_KANA": "よこすかほんこう", "COUNTRY_CODE": "JP", "CATEGORY_ID": "PORT-NAVAL",
                   "PARENT_ID": "NA-000101", "OWNER_GROUP": "第1班", "PRIORITY": 100, "NOTE": "定点（毎日）"},
}
ORG_AREAS = [
    ("長浦港", (139.735, 35.300, 139.750, 35.312), {"AREA_USE": "関心地域", "NAME_KANA": "ながうらこう", "COUNTRY_CODE": "JP",
                                                    "CATEGORY_ID": "PORT-NAVAL", "PARENT_ID": "NA-000101", "OWNER_GROUP": "第2班",
                                                    "PRIORITY": 200, "NOTE": "モックの組織の関心地域（管理番号は保存時に付番）"}),
]
#: 地域のカテゴリー (TBL_AREA_CATEGORY): id, name, English name, parent, order, colour.
AREA_CATEGORIES = [
    ("PORT", "港湾", "Port", "", 1, "#0ea5e9"),
    ("PORT-NAVAL", "軍港", "Naval port", "PORT", 1, "#1d4ed8"),
    ("AIRPORT", "空港・飛行場", "Airfield", "", 2, "#a855f7"),
    ("INDUSTRY", "工業地帯", "Industrial area", "", 3, "#f59e0b"),
]
#: The countries (a rough box around Japan's islands and seas): code, name, English name, box.
COUNTRIES = [("JP", "日本", "Japan", (122.9, 24.0, 146.0, 45.6))]

ASSIGNMENT_FIELDS = {"IMAGE_ID": {"type": S, "length": 255}, "IMAGE_NAME": S, "IMAGE_TIME": T, "ASSIGNEE": S, "STATUS": S,
                     "NOTE": {"type": S, "length": 2000}, "TAKEN_AT": T, "DONE_AT": T}
#: 画像の評価 (TBL_IMAGE_REVIEW, 2026-11-24): one row per image × named area, its shape the named area's part the image shows.
REVIEW_FIELDS = {"REVIEW_ID": {"type": S, "length": 64}, "IMAGE_ID": {"type": S, "length": 255}, "IMAGE_NAME": S, "IMAGE_TIME": T,
                 "SATELLITE": S, "NAMED_AREA_ID": S, "NAMED_AREA_NAME": S, "RATING": {"type": S, "length": 16}, "CLOUD_PCT": D,
                 "REASONS": {"type": S, "length": 1000}, "NOTE": {"type": S, "length": 2000}, "REVIEWED_BY": S, "REVIEWED_AT": T}
HANDOVER_FIELDS = {"TEXT": {"type": S, "length": 100000}, "MEMO": {"type": S, "length": 4000}, "PERIOD_START": T, "PERIOD_END": T,
                   "WRITTEN_AT": T, "WRITTEN_BY": S}
EVENT_FIELDS = {"EVENT_ID": {"type": S, "length": 64}, "EVENT_NAME": S, "EVENT_KIND": S, "STATUS": S, "START_AT": T, "END_AT": T,
                "NOTE": {"type": S, "length": 4000}}
EVENT_LINK_FIELDS = {"EVENT_ID": {"type": S, "length": 64}, "ITEM_TYPE": S, "ITEM_ID": {"type": S, "length": 128},
                     "ITEM_LABEL": {"type": S, "length": 1000}, "ITEM_TIME": T, "SOURCE": {"type": S, "length": 1000},
                     "NOTE": {"type": S, "length": 4000}}
#: 出典つきの参考情報 (TBL_REFERENCE_INFO, 2026-11-24): one point per info, and its links (TBL_REFERENCE_LINK).
REFERENCE_FIELDS = {"INFO_ID": {"type": S, "length": 64}, "TITLE": S, "INFO_TIME": T, "PUBLISHED_AT": T, "SOURCE": S,
                    "SOURCE_REF": {"type": S, "length": 1000}, "RELIABILITY": {"type": S, "length": 64},
                    "SUMMARY": {"type": S, "length": 4000}, "KIND": {"type": S, "length": 1000}, "NAMED_AREA_ID": S,
                    "NOTE": {"type": S, "length": 2000}}
REFERENCE_LINK_FIELDS = {"INFO_ID": {"type": S, "length": 64}, "ITEM_TYPE": {"type": S, "length": 16}, "ITEM_ID": S, "LABEL": S}
#: 関心地域・地点 (both layers alike; the owner is the plugin's user name).
INTEREST_FIELDS = {"INTEREST_ID": {"type": S, "length": 64}, "INTEREST_NAME": S, "OWNER_USER": S, "COLOR": {"type": S, "length": 32},
                   "NOTE": {"type": S, "length": 4000}}
#: 担当 (one row per role of a person; empty: each person's ticks on 「担当と個人設定」 write theirs).
USER_ROLE_FIELDS = {"USER_NAME": S, "ROLE_NAME": {"type": S, "length": 32}}

#: 備考の単語集 (one word per row: other spellings, readings and 大分類 with commas between them).
GLOSSARY_FIELDS = {"TERM": S, "VARIANTS": {"type": S, "length": 1000}, "READING": S, "ROOT_KIND": {"type": S, "length": 1000},
                   "DESCRIPTION": {"type": S, "length": 1000}, "SORT_ORDER": I}
GLOSSARY_ROWS = [
    ("入港", "入航,入湊", "にゅうこう", "船舶", "港に入る", 1),
    ("入港直後", "", "にゅうこうちょくご", "船舶", "入港して間もない", 2),
    ("出港", "出航,出湊", "しゅっこう", "船舶", "港を出る", 3),
    ("岸壁係留", "接岸,着岸", "がんぺきけいりゅう", "船舶", "岸壁に係留している", 4),
    ("錨泊", "投錨,びょうはく", "びょうはく", "船舶", "錨を下ろして停泊", 5),
    ("タグボート", "タグ,曳船", "たぐぼーと", "船舶", "", 6),
    ("駐機", "", "ちゅうき", "航空機", "駐機場に止まっている", 1),
    ("誘導路", "", "ゆうどうろ", "航空機", "", 2),
    ("エプロン", "駐機場", "えぷろん", "航空機", "", 3),
    ("雲で不明瞭", "雲で見えず,雲で判読不可", "くもでふめいりょう", "", "雲で一部が見えない", 10),
    ("影で不明瞭", "", "かげでふめいりょう", "", "影で一部が見えない", 11),
    ("前回画像と同じ位置", "", "ぜんかいがぞうとおなじいち", "", "", 12),
    ("AIS", "ＡＩＳ信号", "えーあいえす", "", "船舶自動識別装置", 13),
]

#: Every layer's tracking fields (the plugin's editTracking defaults: when and who added / updated).
EDIT_FIELDS = {"CREATED_DATE": T, "CREATED_USER": S, "UPDATED_DATE": T, "UPDATED_USER": S}

TAXONOMY_ROWS = [
    ("船舶", "大分類,種別,級", "艦名", "", "入港,出港,停泊,航行,荷役"),
    ("船舶 > 貨物船", "", "", "", ""),
    ("船舶 > 貨物船 > コンテナ船", "", "", "", ""),
    ("船舶 > 支援船", "", "", "", ""),
    ("船舶 > 支援船 > 〇〇級", "", "", "△△,□□", ""),
    ("航空機", "大分類,種別,型式", "機番", "", "駐機,離陸,着陸,整備"),
    ("航空機 > 輸送機", "", "", "", ""),
    ("車両", "", "", "", "駐車,移動,集積"),
    ("建物", "", "", "", "工事,完成,解体"),
    ("その他", "", "", "", ""),
]


#: The individuals that say something of themselves: class, name, other names, specifications (JSON), note.
TAXONOMY_MEMBER_ROWS = [
    ("船舶 > 支援船 > 〇〇級", "△△", "AA-101,Sankaku", '{"番号":"101","拠点":"横須賀","状態":"現役"}', ""),
    ("船舶 > 支援船 > 〇〇級", "□□", "AA-102", '{"番号":"102","拠点":"呉","状態":"改修中"}', "2026 年度まで改修"),
]


def services(files: str, base: str, with_features: bool = True, now: Optional[int] = None) -> dict:
    """The sample services (the format of mock_esri.py --services), with their features when `with_features`."""
    now = now if now is not None else int(time.time() * 1000)
    plans = _created_plans(files) or image_plans(now)
    if with_features:
        _keep_plans(files, plans)
    feats = (lambda items: items) if with_features else (lambda items: [])

    sats, sensors = [], []
    epoch, turn = tle_epoch(now)
    for name, number, series, kind, side, max_ona, swath, raan in SATELLITES:
        l1, l2 = tle(number, epoch, 97.8, 300 + raan + turn, number * 37.0, 14.9)
        sats.append({"attributes": {"SAT_NAME": name, "NORAD_ID": str(number), "TLE_LINE1": l1, "TLE_LINE2": l2, "MAX_OFF_NADIR": max_ona,
                                    "MIN_OFF_NADIR": 15 if kind == "SAR" else 0, "SWATH": swath, "SCENE_LENGTH": swath, "LOOK_SIDE": side,
                                    "SENSOR_TYPE": kind, "SERIES": series, "SCENES_PER_PASS": 3 if kind == "光学" else 1,
                                    "MIN_INTERVAL_MIN": 0, "MAX_PER_DAY": 0, "STATUS": "ACTIVE"}})
    for mode, lo, hi, swath, length, res, primary in (("ストリップマップ", 15, 30, 30, 50, 3.0, 1), ("スポットライト", 15, 30, 5, 5, 1.0, 0),
                                                       ("スキャンSAR", 21, 29, 100, 100, 15.0, 0)):
        sensors.append({"attributes": {"SENSOR": "SAR-Y", "SENSOR_MODE": mode, "MIN_OFF_NADIR": lo, "MAX_OFF_NADIR": hi, "SWATH": swath,
                                       "SCENE_LENGTH": length, "SCENES_PER_PASS": 1, "RESOLUTION_M": res, "IS_DEFAULT": primary}})

    first = plans[0]
    observations = []
    obj_ids = {}
    for ship in first["ships"][:2]:
        obj_ids[ship["n"]] = str(uuid.UUID(int=ship["n"] + 1))
        observations.append({"attributes": {
            "OBS_ID": str(uuid.UUID(int=1000 + ship["n"])), "IMAGE_ID": first["id"], "IMAGE_NAME": first["id"] + ".tif",
            "IMAGE_TIME": first["acquired"], "OBJECT_ID": obj_ids[ship["n"]], "OBJECT_NAME": f"船舶-{ship['n'] + 1}",
            "KIND": "船舶 > 貨物船" if ship["n"] == 0 else "船舶 > 支援船", "ACTIVITY": "停泊", "PRESENT": 1, "NOTE": "サンプル",
            "LENGTH_M": ship["length"], "HEADING": round(ship["heading"] % 360, 1), "CONFIDENCE": "推定", "OBSERVER": "モック",
            "CREATED_AT": first["acquired"] + 5 * HOUR, "UPDATED_AT": first["acquired"] + 5 * HOUR, "PROJECT": "A"},
            "geometry": {"x": round(ship["lon"], 7), "y": round(ship["lat"], 7)}})

    # Name, reading, other names (comma-separated), lon, lat.
    places = [("横須賀港", "よこすかこう", "横須賀本港", 139.672, 35.284), ("東京港", "とうきょうこう", "", 139.790, 35.620),
              ("千葉港", "ちばこう", "", 140.080, 35.580), ("木更津港", "きさらづこう", "", 139.890, 35.375),
              ("羽田空港", "はねだくうこう", "東京国際空港,HND", 139.781, 35.549), ("浦賀水道", "うらがすいどう", "", 139.750, 35.230)]
    requests = []
    for k, (name, cat, achieve, lon, lat, priority, kind, group) in enumerate([
            ("横須賀 定点", "定点", "毎回", 139.672, 35.284, 120, "光学", "第1班"),
            ("木更津 臨時", "臨時", "期間内1回", 139.890, 35.375, 450, "任意", "第2班"),
            ("浦賀水道 夜間", "定点", "毎回", 139.750, 35.230, 700, "SAR", None)]):
        d = 0.02
        requests.append({"attributes": {
            "REQ_ID": str(uuid.UUID(int=5000 + k)), "REQ_NAME": name, "MANAGEMENT_NO": f"M-{k + 1:04d}", "REQUEST_GROUP": group,
            "IDENTIFICATION_NO": f"{20261100 + k + 1}", "CATEGORY": cat, "ACHIEVE": achieve, "START_AT": _day_start(now) - 2 * DAY,
            "END_AT": _day_start(now) + 14 * DAY, "REPEAT": "毎日" if cat == "定点" else "", "STATUS": "有効", "PRIORITY": priority, "BOOST": 0,
            "KIND": kind, "SERIES": "", "LOOK_SIDE": "任意", "ORBIT_DIR": "任意", "TIME_OF_DAY": "夜" if kind == "SAR" else "任意",
            "CLOUD_NO_GO": 60 if kind == "光学" else None, "SHAPE_KIND": "範囲", "REQUESTER": "モック", "CREATED_AT": now - 3 * DAY, "UPDATED_AT": now - 3 * DAY},
            "geometry": {"rings": [_box_ring((lon - d, lat - d, lon + d, lat + d))]}})

    taxonomy = [{"attributes": {"PATH": p, "LEVELS": lv, "NAME_LABEL": nl, "MEMBERS": m, "SPECS": "", "PHOTOS": "", "ACTIVITIES": act, "SORT_ORDER": i}}
                for i, (p, lv, nl, m, act) in enumerate(TAXONOMY_ROWS)]
    members = [{"attributes": {"MEMBER_ID": str(uuid.UUID(int=9000 + i)), "CLASS_PATH": p, "MEMBER_NAME": n, "ALIASES": a, "SPECS": sp, "PHOTOS": "",
                               "NOTE": note, "SORT_ORDER": i}} for i, (p, n, a, sp, note) in enumerate(TAXONOMY_MEMBER_ROWS)]

    out = {"services": [
        {"name": "Catalog", "layers": [{
            "id": 0, "name": "画像カタログ", "geometryType": "polygon", "maxRecordCount": 1000,
            "fields": {"IMG_ID": S, "ACQ_TIME": T, "REG_TIME": T, "SYS_TIME": T, "SENSOR_NAME": S, "OFF_NADIR": D, "SAT_AZIMUTH": D,
                       "CLOUD": D, "STATUS": S, "NOTE": S, "FILE_PATH": {"type": S, "length": 1000}, "COG_URL": {"type": S, "length": 1000}},
            "features": feats([catalog_feature(p, files, base) for p in plans])}]},
        {"name": "Satellites", "layers": [
            {"id": 0, "name": "衛星", "fields": {"SAT_NAME": S, "NORAD_ID": S, "TLE_LINE1": S, "TLE_LINE2": S, "MAX_OFF_NADIR": D, "MIN_OFF_NADIR": D,
                                                "SWATH": D, "SCENE_LENGTH": D, "LOOK_SIDE": S, "SENSOR_TYPE": S, "SERIES": S, "SCENES_PER_PASS": I,
                                                "MIN_INTERVAL_MIN": D, "MAX_PER_DAY": I, "STATUS": S},
             "features": feats(sats)},
            {"id": 1, "name": "センサーモード", "fields": {"SENSOR": S, "SENSOR_MODE": S, "MIN_OFF_NADIR": D, "MAX_OFF_NADIR": D, "SWATH": D,
                                                       "SCENE_LENGTH": D, "SCENES_PER_PASS": I, "RESOLUTION_M": D, "IS_DEFAULT": F},
             "features": feats(sensors)},
            {"id": 2, "name": "衛星名の対応表", "fields": {"SAT_NAME": S, "ALIAS": {"type": S, "length": 1000}},
             "features": feats([{"attributes": {"SAT_NAME": n, "ALIAS": a}} for n, a in SATELLITE_ALIASES])}]},
        {"name": "Observations", "layers": [
            {"id": 0, "name": "観測記録", "geometryType": "point", "fields": OBSERVATION_FIELDS, "attributeRules": OBSERVATION_RULES,
             "features": feats(observations)},
            {"id": 1, "name": "分類体系", "fields": TAXONOMY_FIELDS, "features": feats(taxonomy)},
            {"id": 2, "name": "確認範囲", "geometryType": "polygon", "fields": AREA_FIELDS, "features": []},
            {"id": 3, "name": "分類の既知の個体", "fields": TAXONOMY_MEMBER_FIELDS, "features": feats(members)}]},
        {"name": "AI", "layers": [{
            "id": 0, "name": "外部AI結果", "geometryType": "polygon",
            "fields": {"DET_ID": {"type": S, "length": 128}, "IMAGE_ID": S, "CLASS": S, "SCORE": D, "MODEL": S, "DETECTED_AT": T,
                       "LENGTH_M": D, "WIDTH_M": D, "HEADING": D, "STATUS": S},
            "features": feats([f for p in plans for f in ai_features(p)])}]},
        {"name": "Detections", "layers": [{
            "id": 0, "name": "検出結果（転記先）", "geometryType": "polygon",
            "fields": {"IMAGE_ID": S, "FILE_NAME": S, "ACQ_TIME": T, "DETECTED_AT": T, "REGISTERED_AT": T, "CLASS": S, "CLASS_ID": I,
                       "CONFIDENCE": D, "MODEL": S, "STATUS": S, "LON": D, "LAT": D, "AREA_M2": D, "LENGTH_M": D, "WIDTH_M": D, "HEADING": D,
                       "REMARKS": {"type": S, "length": 4000}, "SOURCE": S},
            "features": []}]},
        {"name": "Places", "layers": [{"id": 0, "name": "地名", "geometryType": "point",
                                       "fields": {"NAME": S, "NAME_KANA": {"type": S, "alias": "読み"}, "ALIAS": {"type": S, "alias": "別名"}},
                                       "features": feats([{"attributes": {"NAME": n, "NAME_KANA": k, "ALIAS": a or None}, "geometry": {"x": x, "y": y}}
                                                          for n, k, a, x, y in places])}]},
        {"name": "Requests", "layers": [
            {"id": 0, "name": "撮像依頼台帳", "geometryType": "polygon", "fields": REQUEST_FIELDS, "features": feats(requests)},
            {"id": 1, "name": "出力履歴", "geometryType": "polygon", "fields": HISTORY_FIELDS, "features": []},
            {"id": 2, "name": "依頼グループ", "fields": GROUP_FIELDS, "features": feats([{"attributes": a} for a in (
                {"GROUP_NAME": "第1班", "MEMBERS": f"{_user()}, yamada", "SORT_ORDER": 1, "ACTIVE": 1, "NOTE": "港湾の定点"},
                {"GROUP_NAME": "第2班", "MEMBERS": "sato", "SORT_ORDER": 2, "ACTIVE": 1, "NOTE": "臨時の依頼"},
                {"GROUP_NAME": "旧体制", "MEMBERS": "", "SORT_ORDER": 9, "ACTIVE": 0, "NOTE": "使用停止（古い依頼のため残す）"})])},
            {"id": 3, "name": "撮像の範囲", "geometryType": "polygon", "fields": SHOT_FIELDS, "features": []},
            {"id": 4, "name": "依頼の範囲の変更記録", "geometryType": "polygon", "fields": AREA_CHANGE_FIELDS, "features": []}]},
        {"name": "Alerts", "layers": [
            {"id": 0, "name": "通報条件", "fields": ALERT_RULE_FIELDS, "features": feats([{"attributes": r} for r in ALERT_RULES])},
            {"id": 1, "name": "ジオフェンス", "geometryType": "polygon", "fields": {"NAME": S, "ACTIVE": F, "WEIGHT": D, "NAME_EN": S},
             "features": feats([{"attributes": {"NAME": "横須賀", "ACTIVE": 1, "WEIGHT": 1.0, "NAME_EN": "Yokosuka"},
                                 "geometry": {"rings": [_box_ring(AREA)]}}])},
            {"id": 2, "name": "通報記録", "fields": ALERT_LOG_FIELDS, "features": []},
            {"id": 3, "name": "地名", "geometryType": "polygon", "fields": NAMED_AREA_FIELDS, "attributeRules": NAMED_AREA_RULES,
             "features": feats([{"attributes": {"NAME": n, "KIND": k, "ACTIVE": 1, "NAME_EN": ne, "KIND_EN": ke, "NAMED_AREA_ID": i,
                                                **NAMED_AREA_EXTRA.get(n, {})},
                                 "geometry": {"rings": [_box_ring(b)]}} for n, k, b, ne, ke, i in NAMED_AREAS]
                               + [{"attributes": {"NAME": n, "ACTIVE": 1, **extra}, "geometry": {"rings": [_box_ring(b)]}}
                                  for n, b, extra in ORG_AREAS])},
            {"id": 4, "name": "国", "geometryType": "polygon",
             "fields": {"COUNTRY_CODE": S, "COUNTRY_NAME": S, "COUNTRY_NAME_EN": S, "ACTIVE": F},
             "features": feats([{"attributes": {"COUNTRY_CODE": c, "COUNTRY_NAME": n, "COUNTRY_NAME_EN": ne, "ACTIVE": 1},
                                 "geometry": {"rings": [_box_ring(b)]}} for c, n, ne, b in COUNTRIES])},
            {"id": 5, "name": "地域のカテゴリー", "fields": AREA_CATEGORY_FIELDS,
             "features": feats([{"attributes": {"CATEGORY_ID": i, "CATEGORY_NAME": n, "CATEGORY_NAME_EN": ne, "PARENT_ID": p or None,
                                                "SORT_ORDER": o, "COLOR": c, "ACTIVE": 1}} for i, n, ne, p, o, c in AREA_CATEGORIES])}]},
        {"name": "Work", "layers": [
            {"id": 0, "name": "分担", "fields": ASSIGNMENT_FIELDS, "features": []},
            {"id": 1, "name": "引き継ぎメモ", "fields": HANDOVER_FIELDS, "features": []},
            {"id": 2, "name": "画像の評価", "geometryType": "polygon", "fields": REVIEW_FIELDS, "features": []}]},
        {"name": "Events", "layers": [
            {"id": 0, "name": "イベント", "geometryType": "polygon", "fields": EVENT_FIELDS,
             "features": feats([{"attributes": {"EVENT_ID": str(uuid.UUID(int=9000)), "EVENT_NAME": "東京湾 船舶事故（サンプル）", "EVENT_KIND": "事故",
                                                "STATUS": "対応中", "START_AT": now - 2 * DAY, "NOTE": "モックのイベント。画像・記録は「イベントに追加」で"},
                                 "geometry": {"rings": [_box_ring(AREA)]}}])},
            {"id": 1, "name": "イベントの紐付け", "fields": EVENT_LINK_FIELDS, "features": []},
            {"id": 2, "name": "参考情報", "geometryType": "point", "fields": REFERENCE_FIELDS,
             "features": feats([{"attributes": {"INFO_ID": "REF-SAMPLE-0001", "TITLE": "東京湾で船舶の座礁（サンプル）", "INFO_TIME": now - 2 * DAY,
                                                "PUBLISHED_AT": now - 2 * DAY + 3_600_000, "SOURCE": "サンプル海上保安部",
                                                "SOURCE_REF": "航行警報 第1号（モック）", "RELIABILITY": "推定",
                                                "SUMMARY": "モックの参考情報。湾の中ほどで貨物船が座礁、付近を航行する船に注意を呼びかけ",
                                                "KIND": "船舶", "NOTE": "練習用"},
                                 "geometry": {"x": (AREA[0] + AREA[2]) / 2, "y": (AREA[1] + AREA[3]) / 2}}])},
            {"id": 3, "name": "参考情報の紐付け", "fields": REFERENCE_LINK_FIELDS, "features": []}]},
        {"name": "Interests", "layers": [
            {"id": 0, "name": "関心地点", "geometryType": "point", "fields": INTEREST_FIELDS,
             "features": feats([{"attributes": {"INTEREST_ID": str(uuid.UUID(int=9100)), "INTEREST_NAME": "サンプルの岸壁", "OWNER_USER": _user(),
                                                "COLOR": "#1d4ed8", "NOTE": "モックの関心地点"},
                                 "geometry": {"x": (AREA[0] + AREA[2]) / 2, "y": (AREA[1] + AREA[3]) / 2}}])},
            {"id": 1, "name": "関心地域", "geometryType": "polygon", "fields": INTEREST_FIELDS, "features": []}]},
        {"name": "Roles", "layers": [{"id": 0, "name": "担当", "fields": USER_ROLE_FIELDS, "features": []}]},
        {"name": "Glossary", "layers": [{"id": 0, "name": "備考の単語集", "fields": GLOSSARY_FIELDS, "features": feats([
            {"attributes": {"TERM": t, "VARIANTS": v, "READING": r, "ROOT_KIND": k, "DESCRIPTION": d, "SORT_ORDER": o}}
            for t, v, r, k, d, o in GLOSSARY_ROWS])}]},
    ]}
    for service in out["services"]:
        for layer in service["layers"]:
            layer["fields"] = {**layer["fields"], **EDIT_FIELDS}
    return out


def add_new_image(store, files: str, base: str) -> str:
    """Registers a new image (acquired an hour ago, registered now) with its AI results; its id."""
    now = int(time.time() * 1000)
    plans = _created_plans(files)
    k = len(plans)
    sat = SATELLITES[k % len(SATELLITES)]
    plan = _image_plan(k, sat[0], now - HOUR, sat[3] == "SAR")
    plan["registered"] = now
    write_image(files, plan)
    _keep_plans(files, plans + [plan])
    store.add(store.layer("Catalog", 0), catalog_feature(plan, files, base))
    for f in ai_features(plan):
        store.add(store.layer("AI", 0), f)
    return plan["id"]


# --- the settings file ------------------------------------------------------------------------------------


def write_config(path: str, base: str, files: str) -> None:
    """A settings file (JSONC) whose services are the mock's (scripts/mock_esri/mock_esri.py)."""
    with open(path, "w", encoding="utf-8") as f:
        f.write(config_text(base, files))


#: The optical system's 採否 file as the practice's system would answer (for 「結果ファイルの取り込み」).
RESULT_FILE = """
      "resultFile": { "format": "csv", "encoding": "cp932",
        "columns": [ { "key": "依頼ID", "target": "id" },
                     { "key": "判定", "target": "result", "map": { "○": "採用", "×": "不採用" } },
                     { "key": "理由", "target": "reason" },
                     { "key": "撮像予定", "target": "plannedAt", "format": "%Y/%m/%d %H:%M" } ] },"""
#: The place search on the internet (地理院), left out of the practice's settings.
GSI_GEOCODER = ',\n    { "type": "gsi", "label": "地理院", "url": "https://msearch.gsi.go.jp/address-search/AddressSearch" }'
TRAINING_HEAD = ("// 練習モードの設定ファイル（MIKAGE が練習を始めるたびに書き出す。手で直しても次に上書きされる）\n"
                 "// サービスはすべてこの PC の中の練習用サーバー。本番の URL・フォルダーは入っていない\n")


def training_config(base: str, files: str, folder: str) -> str:
    """
    練習モード's settings file (JSONC): every service the practice server's
    (`base`), every file in the practice folder `folder` (the day's request
    files, the systems' result files, the 通報 file). No production URL, no
    folder outside it, no service on the internet (the 地理院 place search
    left out), no `userRoles` (担当 switched only on this computer while
    practising) and no `auditLog` (the practice is not audited: the plugin
    pauses the log while practising).
    """
    text = config_text(base, files, label="練習用", alerts=os.path.join(folder, "通報", "alert_{now:%Y%m%d}.txt"))
    text = TRAINING_HEAD + text.split("\n", 2)[2]
    exports = json.dumps(os.path.join(folder, "撮像計画ファイル").replace("\\", "/"), ensure_ascii=False)
    text = text.replace('"system": "MOCK-OPT",', f'"system": "MOCK-OPT", "folder": {exports},' + RESULT_FILE, 1)
    text = text.replace('"system": "MOCK-SAR",', f'"system": "MOCK-SAR", "folder": {exports},', 1)
    text = text.replace(GSI_GEOCODER, "", 1)
    lines = text.split("\n")
    at = next(i for i, line in enumerate(lines) if line.startswith('  "userRoles":'))
    del lines[at - 1:at + 1]  # its comment and itself
    return "\n".join(lines)


def config_text(base: str, files: str, label: str = "モック", alerts: Optional[str] = None) -> str:
    """The settings file's text whose services are the server's (`label` begins their names)."""
    rest = f"{base}/arcgis/rest/services"
    alert_file = json.dumps(alerts or os.path.join(files, "alerts", "alert_{now:%Y%m%d}.txt"), ensure_ascii=False)
    text = f"""// mock_esri.py が書き出した設定ファイル（起動のたびに上書き）。プラグインの「設定…」でこのファイルを選ぶ
// サービスはすべて {rest} のモック。手で直すならコピーしてから
{{
  "imageCatalogs": [
    {{ "label": "モック画像カタログ", "url": "{rest}/Catalog/FeatureServer/0",
      "fields": {{ "id": "IMG_ID", "acquired": "ACQ_TIME", "registered": "REG_TIME", "systemRegistered": "SYS_TIME",
                  "sensor": "SENSOR_NAME", "angle": "OFF_NADIR", "azimuth": "SAT_AZIMUTH", "source": ["FILE_PATH", "COG_URL"],
                  "keywords": ["NOTE", "IMG_ID"] }},
      "columns": [{{ "field": "CLOUD", "label": "雲量" }}, {{ "field": "NOTE", "label": "備考" }}],
      "where": "STATUS = 'PUBLISHED'", "angleKind": "offNadir", "orbitHeight": 600 }}
  ],
  "watchInterval": 1,
  "satelliteCatalogs": [
    {{ "label": "モック衛星", "url": "{rest}/Satellites/FeatureServer/0",
      "fields": {{ "name": "SAT_NAME", "id": "NORAD_ID", "tle1": "TLE_LINE1", "tle2": "TLE_LINE2",
                  "maxOffNadir": "MAX_OFF_NADIR", "minOffNadir": "MIN_OFF_NADIR", "swath": "SWATH", "length": "SCENE_LENGTH",
                  "lookSide": "LOOK_SIDE", "kind": "SENSOR_TYPE", "series": "SERIES", "scenesPerPass": "SCENES_PER_PASS",
                  "minInterval": "MIN_INTERVAL_MIN", "maxPerDay": "MAX_PER_DAY" }},
      "swathUnit": "km", "sarPattern": "SAR", "where": "STATUS = 'ACTIVE'" }}
  ],
  "sensorCatalogs": [
    {{ "label": "モック SAR モード", "url": "{rest}/Satellites/FeatureServer/1",
      "fields": {{ "sensor": "SENSOR", "mode": "SENSOR_MODE", "minOffNadir": "MIN_OFF_NADIR", "maxOffNadir": "MAX_OFF_NADIR", "swath": "SWATH",
                  "length": "SCENE_LENGTH", "scenesPerPass": "SCENES_PER_PASS", "resolution": "RESOLUTION_M", "primary": "IS_DEFAULT" }},
      "swathUnit": "km" }}
  ],
  "satelliteAliases": {{ "label": "モック衛星名の対応表", "url": "{rest}/Satellites/FeatureServer/2",
                        "fields": {{ "satellite": "SAT_NAME", "alias": "ALIAS" }} }},
  // 閉じたネットワークでも動くよう雲量予報は使わない（使うなら {{ "url": "https://api.open-meteo.com/v1/forecast" }}）
  "cloudForecast": false,
  "groundStations": [{{ "name": "勝浦", "lon": 140.3, "lat": 35.15, "minElevation": 5 }}],
  "observationStores": [
    {{ "label": "モック観測記録", "url": "{rest}/Observations/FeatureServer/0",
      "areas": "{rest}/Observations/FeatureServer/2",
      "taxonomyTable": {{ "url": "{rest}/Observations/FeatureServer/1", "members": "{rest}/Observations/FeatureServer/3" }} }}
  ],
  "aiResultSources": [
    {{ "label": "モック外部AI", "url": "{rest}/AI/FeatureServer/0",
      "fields": {{ "imageId": "IMAGE_ID", "id": "DET_ID", "class": "CLASS", "score": "SCORE", "model": "MODEL", "detectedAt": "DETECTED_AT",
                  "length": "LENGTH_M", "width": "WIDTH_M", "heading": "HEADING" }},
      "where": "STATUS <> 'rejected'", "minScore": 0.3, "kinds": {{ "ship": "船舶", "plane": "航空機" }}, "activity": "停泊", "confidence": "推定" }}
  ],
  "detectionOutputs": [
    {{ "label": "モック検出結果DB", "url": "{rest}/Detections/FeatureServer/0",
      "fields": {{ "imageId": "IMAGE_ID", "fileName": "FILE_NAME", "imageTime": "ACQ_TIME", "detectedAt": "DETECTED_AT", "sentAt": "REGISTERED_AT",
                  "class": "CLASS", "classId": "CLASS_ID", "score": "CONFIDENCE", "model": "MODEL", "status": "STATUS", "lon": "LON", "lat": "LAT",
                  "area": "AREA_M2", "length": "LENGTH_M", "width": "WIDTH_M", "angle": "HEADING", "note": "REMARKS" }},
      "constants": {{ "SOURCE": "mock" }} }}
  ],
  "geocoders": [
    {{ "type": "features", "label": "モック地名", "url": "{rest}/Places/FeatureServer/0", "field": ["NAME", "NAME_KANA", "ALIAS"] }},
    {{ "type": "gsi", "label": "地理院", "url": "https://msearch.gsi.go.jp/address-search/AddressSearch" }}
  ],
  // 撮像依頼台帳（docs/imaging-requests.md の既定の属性名）と日次出力のシステム（出力先は確認画面で選ぶ）
  "requestStores": [
    {{ "label": "モック撮像依頼", "url": "{rest}/Requests/FeatureServer/0", "history": "{rest}/Requests/FeatureServer/1",
      "groups": "{rest}/Requests/FeatureServer/2", "shots": "{rest}/Requests/FeatureServer/3",
      "areaChanges": "{rest}/Requests/FeatureServer/4", "catalog": "モック画像カタログ",
      "fields": {{}} }}
  ],
  "requestExports": [
    {{ "system": "MOCK-OPT", "label": "モック光学システム", "kind": "光学", "series": ["OPT-X"], "limit": {{ "count": 10 }},
      "format": "csv", "encoding": "cp932", "timeZone": "Asia/Tokyo", "dateFormat": "%Y/%m/%d %H:%M",
      "priority": {{ "table": [{{ "max": 200, "value": "High" }}, {{ "max": 600, "value": "Middle" }}, {{ "max": 1000, "value": "Low" }}] }},
      "columns": [{{ "key": "依頼ID", "source": "id" }}, {{ "key": "名称", "source": "name" }}, {{ "key": "衛星", "source": "satellite" }},
                  {{ "key": "開始", "source": "start" }}, {{ "key": "終了", "source": "end" }}, {{ "key": "範囲", "source": "geometry", "geometry": "wkt", "digits": 5 }},
                  {{ "key": "優先度", "source": "priority" }}, {{ "key": "雲量予報", "source": "cloudForecast" }}, {{ "key": "備考", "source": "warnings" }}] }},
    {{ "system": "MOCK-SAR", "label": "モックSARシステム", "kind": "SAR", "series": ["SAR-Y"], "limit": {{ "count": 10, "scenes": 20 }}, "sceneKm": [30, 30],
      "format": "json", "timeZone": "UTC",
      "priority": {{ "linear": {{ "min": 1, "max": 5 }} }},
      "schema": {{ "type": "object", "required": ["tasks"], "properties": {{
        "issuedAt": {{ "type": "string", "format": "date-time", "x-source": "runDate" }},
        "tasks": {{ "type": "array", "x-source": "requests", "items": {{ "type": "object", "required": ["id", "aoi"], "properties": {{
          "id": {{ "type": "string", "x-source": "id" }}, "satellite": {{ "type": "string", "x-source": "satellite" }},
          "start": {{ "type": "string", "format": "date-time", "x-source": "start" }}, "end": {{ "type": "string", "format": "date-time", "x-source": "end" }},
          "aoi": {{ "type": "object", "x-source": "geometry", "x-geometry": "geojson" }}, "priority": {{ "type": "integer", "x-source": "priority" }} }} }} }} }} }} }}
  ],
  // 注目の記録の通報（条件・ジオフェンス・通報記録は Esri。フォーマットは名前付きで複数、default が最初に選ばれる）
  "alertReports": {{
    "rules": "{rest}/Alerts/FeatureServer/0", "geofences": "{rest}/Alerts/FeatureServer/1", "table": "{rest}/Alerts/FeatureServer/2",
    "namedAreas": "{rest}/Alerts/FeatureServer/3", "countries": "{rest}/Alerts/FeatureServer/4",
    "minConfidence": "推定", "default": "速報",
    "formats": [
      {{ "name": "速報", "delimiter": "|", "timeZone": "Asia/Tokyo", "dateFormat": "%m/%d %H:%M",
        "fields": ["imageTime", {{ "source": "kind", "level": 1 }}, "objectName", "activity", "confidence",
                   {{ "source": "place", "empty": "（地名なし）" }}, {{ "source": "lat", "digits": 4 }}, {{ "source": "lon", "digits": 4 }}, "area", "rule"],
        "header": "【注目】{{date}} {{count}}件", "footer": "以上", "columns": {{ "KIND": "kind" }} }},
      // 英語・国コード・度分秒（地名がなければ座標）
      {{ "name": "English", "delimiter": "/", "timeZone": "UTC", "dateFormat": "%d%H%MZ %b %y",
        "fields": ["imageTime", "objectName", "confidence.en", {{ "source": "country", "empty": "--" }},
                   {{ "source": "place.en", "fallback": {{ "source": ["lat", "lon"], "format": "{{dd}}{{mm}}{{ss}}{{h}}" }} }},
                   {{ "source": ["lat", "lon"], "format": "dms" }}, {{ "source": "area.en", "omitEmpty": true }}] }},
      {{ "name": "ファイル（タブ区切り）", "delimiter": "\\t", "dateFormat": "%Y-%m-%d %H:%M:%S", "timeZone": "UTC", "clipboard": false,
        "fields": ["id", "imageTime", "kind", "objectName", "activity", "lat", "lon", "note"],
        "file": {{ "path": {alert_file}, "encoding": "cp932", "append": true }} }}
    ]
  }},
  // 分担（画像ごとに誰が担当中か。各自が「この画像を担当」で取る）と引き継ぎメモ（保存先のテーブル）
  "assignments": "{rest}/Work/FeatureServer/0",
  "imageReviews": "{rest}/Work/FeatureServer/2",
  "handover": {{ "table": "{rest}/Work/FeatureServer/1", "hours": 12, "timeZone": "Asia/Tokyo" }},
  // イベント（地震対応など）: イベントのレイヤーと紐付けテーブル
  "events": {{ "layer": "{rest}/Events/FeatureServer/0", "links": "{rest}/Events/FeatureServer/1" }},
  "referenceInfo": {{ "layer": "{rest}/Events/FeatureServer/2", "links": "{rest}/Events/FeatureServer/3" }},
  // 組織の関心地域（地名のレイヤーに用途・よみがな・別名・国・カテゴリーなど。ドック「関心地域・地点」の「組織」で全員が編集）
  "namedAreas": {{ "url": "{rest}/Alerts/FeatureServer/3", "categories": "{rest}/Alerts/FeatureServer/5",
                  "countries": "{rest}/Alerts/FeatureServer/4" }},
  // 個人の関心地域・地点（持ち主の列で一人ずつ。新着画像のカードの「写っている地物」のいちばん上に）
  "interests": {{ "points": "{rest}/Interests/FeatureServer/0", "areas": "{rest}/Interests/FeatureServer/1" }},
  // 担当（「担当と個人設定…」でチェックしたものを利用者名ごとに。別の PC でも同じ担当）
  "userRoles": "{rest}/Roles/FeatureServer/0",
  // 備考の単語集（観測記録・確認範囲の備考に語の候補を出し、表記ゆれを知らせる）
  "glossary": "{rest}/Glossary/FeatureServer/0",
  // 書き込むレイヤーの作成日時・作成者・更新日時・更新者（既定の属性名。作成者・更新者は環境変数 USERNAME）
  "editTracking": {{ "userVariable": "USERNAME" }}
}}
"""
    return text if label == "モック" else text.replace("モック", label)
