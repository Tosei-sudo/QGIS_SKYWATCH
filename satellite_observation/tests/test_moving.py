"""移動体 (core.moving): the viewer's moving.test.ts."""
from datetime import datetime, timezone

import pytest

from satellite_observation.core.config import parse_config
from satellite_observation.core.moving import (
    FeedConfig, MovingRecord, TrackLimits, TrackStore, detect_fields, distance_metres, feed_type_of, feed_url, guess_kind, history_rows,
    match_detections, moving_feed_of, position_at, read_record, records_of, time_of, trail_of,
)

T0 = datetime(2026, 10, 17, 9, 0, 0, tzinfo=timezone.utc).timestamp() * 1000


def rec(ident, minutes, lon, lat, **extra):
    return MovingRecord(ident, T0 + minutes * 60_000, lon, lat, **extra)


def test_reads_plain_objects_geojson_esri_lists_and_opensky():
    assert records_of({"mmsi": 1, "lat": 35, "lon": 139}) == [{"mmsi": 1, "lat": 35, "lon": 139}]
    assert len(records_of([{"a": 1}, {"a": 2}, "x"])) == 2
    gj = records_of({"type": "FeatureCollection", "features": [{"type": "Feature", "id": 7, "properties": {"name": "A"},
                                                                 "geometry": {"type": "Point", "coordinates": [139.5, 35.2]}}]})
    assert gj == [{"name": "A", "_lon": 139.5, "_lat": 35.2, "id": 7}]
    esri = records_of({"attributes": {"TRACKID": "x"}, "geometry": {"x": 15584728.7, "y": 4257087.3, "spatialReference": {"wkid": 102100}}})
    assert esri[0]["TRACKID"] == "x" and esri[0]["_lon"] == pytest.approx(140, abs=1e-3) and esri[0]["_lat"] == pytest.approx(35.683, abs=1e-2)
    assert len(records_of({"vessels": [{"mmsi": 1}, {"mmsi": 2}]})) == 2
    sky = records_of({"time": 1, "states": [["abc123", "JAL1  ", "Japan", 1760000000, 1760000001, 139.7, 35.5, 1000, False, 200, 90]]})
    assert {k: sky[0][k] for k in ("icao24", "callsign", "longitude", "latitude", "true_track")} == {
        "icao24": "abc123", "callsign": "JAL1", "longitude": 139.7, "latitude": 35.5, "true_track": 90}


def test_flattens_nested_messages_and_finds_the_fields_by_name():
    msg = {
        "MessageType": "PositionReport",
        "MetaData": {"MMSI": 431000001, "ShipName": "HIKARI ", "latitude": 35.1, "longitude": 139.8, "time_utc": "2026-10-17 09:00:00.123456789 +0000 UTC"},
        "Message": {"PositionReport": {"Cog": 45.5, "Sog": 12.3, "TrueHeading": 511, "Latitude": 35.1, "Longitude": 139.8}},
    }
    flat = records_of(msg)[0]
    mapping = detect_fields(flat.keys())
    assert {k: mapping[k] for k in ("id", "time", "lat", "lon", "heading", "course", "speed", "name")} == {
        "id": "MetaData.MMSI", "time": "MetaData.time_utc", "lat": "MetaData.latitude", "lon": "MetaData.longitude",
        "heading": "Message.PositionReport.TrueHeading", "course": "Message.PositionReport.Cog", "speed": "Message.PositionReport.Sog",
        "name": "MetaData.ShipName"}
    r = read_record(flat, mapping, 0)
    assert (r.id, r.t, r.heading, r.course, r.speed, r.name) == ("431000001", T0 + 123, None, 45.5, 12.3, "HIKARI")
    assert guess_kind(flat.keys()) == "ship"


def test_finds_ads_b_and_marinecadastre_fields_and_given_fields_win():
    assert detect_fields(["icao24", "callsign", "latitude", "longitude", "true_track", "velocity", "last_contact"]) == {
        "id": "icao24", "name": "callsign", "lat": "latitude", "lon": "longitude", "course": "true_track", "speed": "velocity", "time": "last_contact"}
    assert guess_kind(["icao24", "callsign", "baro_altitude"]) == "aircraft"
    m = detect_fields(["MMSI", "BaseDateTime", "LAT", "LON", "SOG", "COG", "Heading", "VesselName"])
    assert {k: m[k] for k in ("id", "time", "lat", "lon", "heading", "name")} == {
        "id": "MMSI", "time": "BaseDateTime", "lat": "LAT", "lon": "LON", "heading": "Heading", "name": "VesselName"}
    m = detect_fields(["vid", "lat", "lon", "mmsi"], {"id": "vid"})
    assert m["id"] == "vid" and m["lat"] == "lat"
    assert detect_fields(["_lon", "_lat", "id"]) == {"id": "id", "lon": "_lon", "lat": "_lat"}


def test_reads_times_and_drops_bad_positions():
    assert time_of(1760691600) == 1760691600000
    assert time_of(1760691600000) == 1760691600000
    assert time_of("2026-10-17T09:00:00") == T0
    assert time_of("2026-10-17 18:00:00+09:00") == T0
    assert time_of(42) is None
    m = {"id": "id", "lat": "lat", "lon": "lon"}
    assert read_record({"id": "a", "lat": 91, "lon": 0}, m, 0) is None
    assert read_record({"lat": 1, "lon": 0}, m, 0) is None
    r = read_record({"id": "a", "lat": "1.5", "lon": "2", "x": "y"}, m, 5)
    assert (r.t, r.lat, r.lon, r.attrs) == (5, 1.5, 2, {"x": "y"})


def test_reads_a_history_csv():
    csv = b"mmsi,timestamp,lat,lon,sog\n1,2026-10-17T09:00:00Z,35,139,10\n1,2026-10-17T09:10:00Z,35.1,139.1,\n"
    assert history_rows(csv, "ais.csv") == [
        {"mmsi": "1", "timestamp": "2026-10-17T09:00:00Z", "lat": "35", "lon": "139", "sog": "10"},
        {"mmsi": "1", "timestamp": "2026-10-17T09:10:00Z", "lat": "35.1", "lon": "139.1", "sog": None},
    ]
    with pytest.raises(ValueError):
        history_rows(b"{", "x.json")


def test_tracks_keep_order_replace_repeated_times_and_cap():
    store = TrackStore(TrackLimits(max_objects=2, max_fixes_per_track=3, max_fixes=100))
    store.add(rec("a", 10, 0, 0))
    store.add(rec("a", 0, 0, 0, name="late"))
    store.add(rec("a", 5, 0, 0))
    store.add(rec("a", 5, 1, 1))
    assert [(f.t - T0, f.lon) for f in store.tracks["a"].fixes] == [(0, 0), (300_000, 1), (600_000, 0)]
    assert store.tracks["a"].name == "late"
    store.add(rec("a", 20, 0, 0))
    assert len(store.tracks["a"].fixes) == 3
    store.add(rec("b", 0, 0, 0))
    assert store.add(rec("c", 0, 0, 0)) is False and store.dropped == 1
    assert store.range() == (T0, T0 + 20 * 60_000)
    store.prune(T0 + 6 * 60_000)
    assert "b" not in store.tracks and store.fix_count == 2


def test_interpolates_across_the_antimeridian_and_holds_the_last_fix():
    store = TrackStore()
    store.add(rec("a", 0, 179.5, 10, heading=350))
    store.add(rec("a", 10, -179.5, 11, heading=10, speed=10))
    track = store.tracks["a"]
    mid = position_at(track, T0 + 5 * 60_000, hold=60_000)
    assert mid.interpolated and abs(mid.lon) == pytest.approx(180) and mid.lat == pytest.approx(10.5)
    assert mid.dir == pytest.approx(0) and mid.speed == 10
    assert position_at(track, T0 - 1, hold=60_000) is None
    held = position_at(track, T0 + 10.5 * 60_000, hold=60_000)
    assert (held.lon, held.age, held.interpolated) == (-179.5, 30_000, False)
    assert position_at(track, T0 + 12 * 60_000, hold=60_000) is None
    assert position_at(track, T0 + 5 * 60_000, hold=60_000, max_gap=60_000) is None
    assert trail_of(track, T0 - 1, T0 + 5 * 60_000, (180, 10.5)) == [(179.5, 10), (180, 10.5)]


def test_matches_detections_to_the_nearest_object_then():
    store = TrackStore()
    store.add(rec("a", 0, 139.0, 35, name="A"))
    store.add(rec("a", 20, 139.1, 35))
    store.add(rec("b", -60, 139.2, 35))
    at10 = T0 + 10 * 60_000
    results = match_detections([(139.05, 35.001, at10), (139.2, 35, at10), (139.05, 35.02, at10), (139.0, 35, None)],
                               store.tracks.values(), window=10 * 60_000, distance=500)
    assert (results[0].id, results[0].name, results[0].dt) == ("a", "A", 600) and results[0].distance == pytest.approx(111, abs=10)
    assert results[1:] == [None, None, None]
    wide = match_detections([(139.2, 35, at10)], store.tracks.values(), window=90 * 60_000, distance=500)
    assert wide[0].id == "b" and wide[0].distance == 0


def test_distances_on_the_sphere():
    assert distance_metres((0, 0), (1, 0)) == pytest.approx(111_195, abs=10)
    assert distance_metres((179.9, 0), (-179.9, 0)) == pytest.approx(22_239, abs=10)


def test_feed_types_and_urls():
    assert feed_type_of("wss://stream.example.com/v0/stream") == "websocket"
    assert feed_type_of("https://example.com/arcgis/rest/services/Ships/StreamServer") == "esri-stream"
    assert feed_type_of("https://example.com/ais/events") == "sse"
    assert feed_type_of("https://example.com/ais/positions.json") == "poll"
    assert feed_url("wss://a.example/x", "websocket") == "wss://a.example/x"
    for bad, kind in (("https://a.example/x", "websocket"), ("javascript:alert(1)", "poll"), ("file:///etc/passwd", "sse"), ("https://u:p@a.example/x", "poll")):
        with pytest.raises(ValueError):
            feed_url(bad, kind)
    assert feed_url("./pos.json", "poll", "https://site.example/viewer/") == "https://site.example/viewer/pos.json"


def test_settings_file_moving_feeds():
    problems = []
    assert moving_feed_of({"label": "AIS", "type": "websocket", "url": "wss://a/x", "kind": "ship", "fields": {"id": "MMSI", "foo": "x"}}, problems, 0) == \
        FeedConfig("AIS", "websocket", "wss://a/x", None, {"id": "MMSI"}, "ship")
    assert len(problems) == 1
    assert moving_feed_of({"type": "poll", "url": "./p.json", "interval": 30}, [], 1) == FeedConfig("フィード 2", "poll", "./p.json", 30)
    assert moving_feed_of({"type": "websocket", "url": "https://a/x"}, [], 0) is None
    assert moving_feed_of({"type": "ftp", "url": "ftp://a"}, [], 0) is None
    config, problems = parse_config({"movingFeeds": [{"type": "sse", "url": "https://a/events"}, 3]})
    assert config.moving_feeds == [FeedConfig("フィード 1", "sse", "https://a/events")] and len(problems) == 1
    assert parse_config({"movingFeeds": {}})[1] == ["movingFeeds は配列で指定"]
    assert parse_config({})[0].moving_feeds == []
