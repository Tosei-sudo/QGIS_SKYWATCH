"""Alerts (通報): settings, rules, geofences, the lines and the Esri log."""
import json

from skywatch.core.config import parse_config
from skywatch.core.esri import EsriClient
from skywatch.core.obs.alerts import (
    AlertField, AlertItem, AlertRule, Geofence, Moment, alert_settings_of, build_lines, fill, inside_rings, load_geofences, load_log, load_rules,
    manual_items, poll_log, remove_log, report_text, reported_ids, rules_of_rows, score_note, select, weight_of, write_file, write_log, written,
)
from skywatch.core.obs.model import DEFAULT_CONFIDENCES, Observation
from skywatch.tests.fake_esri import FakeEsri, FakeLayer

T0 = 1_762_500_000_000  # 2025-11-07T07:20:00Z


def obs(**kw) -> Observation:
    base = dict(id="r1", image_id="IMG1", image_name="img1.tif", image_time=T0, object_id="OBJ-1", object_name="船A",
                kind="船舶 > 支援船 > 〇〇級", activity="航行", present=True, note="", lon=135.0, lat=35.0,
                created_at=T0 + 60_000, updated_at=T0 + 60_000, confidence="確定", recorded_by="tsudo")
    base.update(kw)
    return Observation(**base)


SQUARE = [[[134.5, 34.5], [135.5, 34.5], [135.5, 35.5], [134.5, 35.5], [134.5, 34.5]]]
RULES_URL = "https://x/FeatureServer/10"
FENCE_URL = "https://x/FeatureServer/11"
LOG_URL = "https://x/FeatureServer/12"


def settings(extra=None, problems=None):
    value = {
        "default": "速報",
        "minConfidence": "推定",
        "rules": RULES_URL,
        "geofences": {"url": FENCE_URL},
        "table": LOG_URL,
        "formats": [
            {"name": "詳細", "delimiter": "\t"},
            {"name": "速報", "delimiter": "|", "escape": "／", "timeZone": "Asia/Tokyo", "dateFormat": "%m/%d %H:%M",
             "fields": [{"source": "imageTime"}, {"source": "kind", "level": 1}, {"source": "kind", "level": -1}, "objectName",
                        "activity", {"source": "lat", "digits": 3}, {"source": "lon", "digits": 3}, "area", {"text": "SKY"},
                        {"source": "present", "map": {"true": "在", "false": "不在"}}, {"source": "note", "empty": "-"}],
             "header": ["【注目】{date} {count}件", "{name}"], "footer": "以上 {now:%Y%m%d}", "newline": "lf"},
        ],
        **(extra or {}),
    }
    p = [] if problems is None else problems
    return alert_settings_of(value, p)


def test_settings_formats_default_and_problems():
    problems = []
    s = settings(problems=problems)
    assert problems == []
    assert [f.name for f in s.formats] == ["詳細", "速報"]
    assert s.default == "速報" and s.format().name == "速報" and s.format("詳細").delimiter == "\t"
    assert s.rules.fields["kind"] == "KIND" and s.table.fields["obsId"] == "OBS_ID"
    problems = []
    bad = alert_settings_of({"default": "なし", "formats": [{"name": "a", "fields": [{"source": "x"}]}, {"name": "a"}, {}],
                             "table": "https://x/MapServer/1"}, problems)
    assert bad.default == "a"
    assert any("default" in p for p in problems)
    assert any("source「x」" in p for p in problems)
    assert any("重複" in p for p in problems)
    assert any("name" in p for p in problems)
    assert any("table" in p for p in problems)
    assert alert_settings_of({"formats": []}, []) is None


def test_plugin_config_reads_the_key():
    config, problems = parse_config({"alertReports": {"formats": [{"name": "速報"}]}})
    assert problems == [] and config.alert_reports.format().name == "速報"
    assert parse_config({})[0].alert_reports is None


def test_rules_rows_activities_and_inactive():
    rules = rules_of_rows([
        {"KIND": "船舶 > 支援船", "ACTIVITIES": "航行、入港", "MIN_CONFIDENCE": "確定", "AREA": "", "ACTIVE": 1, "NOTE": "支援船の航行"},
        {"KIND": "航空機", "ACTIVITIES": "*", "ACTIVE": 0},
        {"KIND": "", "ACTIVITIES": "", "AREA": "*"},
    ], settings().rules.fields)
    assert len(rules) == 2
    assert rules[0].kind == ["船舶", "支援船"] and rules[0].activities == ["航行", "入港"]
    assert rules[1].activities == [] and rules[1].area == "*"


def test_inside_rings_with_a_hole():
    hole = [[134.9, 34.9], [135.1, 34.9], [135.1, 35.1], [134.9, 35.1], [134.9, 34.9]]
    assert inside_rings(135.0, 35.0, SQUARE)
    assert not inside_rings(135.0, 35.0, SQUARE + [hole])
    assert not inside_rings(136.0, 35.0, SQUARE)


def test_select_by_rules_geofence_confidence_period_and_reported():
    s = settings()
    fences = [Geofence("舞鶴", SQUARE)]
    rules = [AlertRule(kind=["船舶", "支援船"], activities=["航行"], note="支援船"), AlertRule(kind=["航空機"], area="舞鶴")]
    records = [
        obs(id="a"),                                                      # meets rule 1
        obs(id="b", activity="停泊"),                                      # activity not in rule
        obs(id="c", kind="航空機 > 戦闘機", lon=135.2),                     # rule 2, inside 舞鶴
        obs(id="d", kind="航空機", lon=140.0),                             # rule 2, outside
        obs(id="e", confidence="不明"),                                   # below minConfidence 推定
        obs(id="f", present=False),                                       # 不在
        obs(id="g", deleted_at=T0),                                       # deleted
        obs(id="h"),                                                      # already reported
        obs(id="i", created_at=T0 - 86_400_000),                          # before the period
    ]
    items = select(records, s, rules, fences, DEFAULT_CONFIDENCES, reported={"h"}, start=T0)
    # Highest score first: c is the only aircraft (rarer than a, with ships near it before).
    assert [i.record.id for i in items] == ["c", "a"]
    assert items[1].rules == ["支援船"] and items[0].areas == ["舞鶴"]
    # No rules table: everything meeting the fixed conditions.
    assert sorted(i.record.id for i in select(records, s, None, (), DEFAULT_CONFIDENCES, start=T0)) == ["a", "b", "c", "d", "h"]
    assert [i.record.id for i in select(records, settings({"score": False}), None, (), DEFAULT_CONFIDENCES, start=T0)] == ["a", "b", "c", "d", "h"]
    # By image time.
    assert [i.record.id for i in select([obs(id="x", image_time=T0 - 1)], s, None, (), DEFAULT_CONFIDENCES, start=T0, by="imageTime")] == []


def test_lines_header_footer_and_escape():
    s = settings()
    fmt = s.format()
    item = select([obs(note="a|b\nc", lon=135.12345, lat=35.98765)], s, None, [Geofence("舞鶴", SQUARE)], DEFAULT_CONFIDENCES)
    at = Moment(now=T0, user="tsudo", count=1)
    lines = build_lines(item, fmt, at)
    assert lines == ["11/07 16:20|船舶|〇〇級|船A|航行|35.988|135.123||SKY|在|a／b／c"]
    text = report_text(lines, fmt, at)
    assert text == "【注目】11/07 16:20 1件\n速報\n" + lines[0] + "\n以上 20251107\n"
    assert fill("{x} {user}", fmt, at) == "{x} tsudo"
    detail = s.format("詳細")
    assert build_lines(item, detail, at)[0].split("\t")[0] == "2025-11-07 16:20"
    assert report_text(["x"], detail, at) == "x\r\n"


def test_date_only_time_only_and_a_zone_per_field():
    problems = []
    s = alert_settings_of({"formats": [{
        "name": "日時", "timeZone": "Asia/Tokyo",
        "fields": [{"source": "imageTime", "format": "%Y/%m/%d"}, {"source": "imageTime", "format": "%H:%M"},
                   {"source": "imageTime", "format": "%H%M", "timeZone": "UTC", "suffix": "Z"},
                   {"source": "imageTime", "format": "iso", "timeZone": "-05:00"}, {"source": "imageTime", "timeZone": "UTC"},
                   {"source": "imageTime", "format": "iso-date", "timeZone": "America/New_York"}],
        "columns": {"TIME_UTC": {"source": "imageTime", "format": "%H:%M", "timeZone": "UTC"}},
        "header": "{date} JST／{now:%H:%M@UTC}Z／{now:%Y-%m-%d %H:%M@+05:30}／{date@UTC}／{now@-03:00}",
        "file": "D:/alerts/{now:%Y%m%d@UTC}.txt",
    }]}, problems)
    assert problems == []
    fmt = s.format()
    at = Moment(now=T0)
    # T0 = 2025-11-07 07:20 UTC = 16:20 JST = 02:20 in New York (EST since 11-02).
    assert build_lines([AlertItem(obs(), [], [])], fmt, at) == [
        "2025/11/07|16:20|0720Z|2025-11-07T02:20:00-05:00|2025-11-07 07:20|2025-11-07"]
    assert written(fmt.columns["TIME_UTC"], AlertItem(obs(), [], []), 0, fmt, at) == "07:20"
    assert fill(fmt.header, fmt, at) == "2025-11-07 16:20 JST／07:20Z／2025-11-07 12:50／2025-11-07 07:20／2025-11-07 04:20"
    assert fill("{date:%d日@UTC}", fmt, at) == "07日"
    # A zone not known stays as written; other places don't take one.
    assert fill("{now:%H@Mars/Base} {name@UTC} {name}", fmt, at) == "{now:%H@Mars/Base} {name@UTC} 日時"
    assert fmt.file.path == "D:/alerts/{now:%Y%m%d@UTC}.txt" and fill(fmt.file.path, fmt, at) == "D:/alerts/20251107.txt"


def test_zone_per_field_problems():
    problems = []
    alert_settings_of({"formats": [{
        "name": "a",
        "fields": [{"source": "imageTime", "timeZone": "Mars/Base"}, {"source": "kind", "timeZone": "UTC"},
                   {"source": "imageTime", "timeZone": ""}],
        "header": "{now:%H@Nowhere}", "footer": "{date@UTC}", "file": "x_{now:%Y@??}.txt",
    }]}, problems)
    assert any("fields[0] の timeZone は" in p for p in problems)
    assert any("fields[1] の timeZone は日時の項目" in p for p in problems)
    assert not any("fields[2]" in p for p in problems)
    assert any(".header の {now:%H@Nowhere} のタイムゾーン「Nowhere」" in p for p in problems)
    assert not any(".footer" in p for p in problems)
    assert any(".file の {now:%Y@??}" in p for p in problems)
    assert len(problems) == 4


def test_write_file_appends_in_its_encoding(tmp_path):
    s = alert_settings_of({"formats": [{"name": "a", "file": {"path": str(tmp_path / "out" / "{name}_{now:%Y%m%d}.txt"), "encoding": "shift_jis", "append": True}}]}, [])
    fmt = s.format()
    at = Moment(now=T0)
    path = write_file("注目\r\n", fmt, at)
    write_file("二件目\r\n", fmt, at)
    assert path.endswith("a_20251107.txt")
    assert open(path, "rb").read().decode("cp932") == "注目\r\n二件目\r\n"


def esri():
    fake = FakeEsri()
    rules = fake.add_layer(RULES_URL, FakeLayer({n: "esriFieldTypeString" for n in ("KIND", "ACTIVITIES", "MIN_CONFIDENCE", "AREA", "NOTE")} | {"ACTIVE": "esriFieldTypeSmallInteger"}, ""))
    rules.add({"KIND": "船舶", "ACTIVITIES": "*", "ACTIVE": 1, "NOTE": "船"})
    rules.add({"KIND": "航空機", "ACTIVE": 0})
    fences = fake.add_layer(FENCE_URL, FakeLayer({"NAME": "esriFieldTypeString"}, "esriGeometryPolygon"))
    fences.add({"NAME": "舞鶴"}, {"rings": SQUARE})
    log = fake.add_layer(LOG_URL, FakeLayer({"OBS_ID": "esriFieldTypeString", "REPORT_NAME": "esriFieldTypeString", "TEXT": "esriFieldTypeString",
                                             "REPORTED_AT": "esriFieldTypeDate", "REPORTED_BY": "esriFieldTypeString", "BATCH_ID": "esriFieldTypeString",
                                             "KIND1": "esriFieldTypeString", "LAT": "esriFieldTypeDouble"}, ""))
    return fake, rules, fences, log


def test_esri_rules_fences_log_round_trip():
    fake, _, _, log = esri()
    client = EsriClient(fake)
    s = settings()
    s.format().columns = alert_settings_of({"formats": [{"name": "x", "columns": {"KIND1": {"source": "kind", "level": 1}, "LAT": "lat", "NOPE": "id"}}]}, []).format().columns
    rules = load_rules(client, s.rules)
    fences = load_geofences(client, s.geofences)
    assert [r.note for r in rules] == ["船"] and [g.name for g in fences] == ["舞鶴"]
    assert reported_ids(client, s) == set()
    items = select([obs(id="a"), obs(id="b", kind="航空機")], s, rules, fences, DEFAULT_CONFIDENCES)
    at = Moment(now=T0, user="tsudo", count=len(items))
    lines = build_lines(items, s.format(), at)
    assert write_log(client, s, s.format(), items, lines, at) == 1
    adds = json.loads(next(f for u, f in fake.asked if u.endswith("/applyEdits"))["adds"])
    a = adds[0]["attributes"]
    assert (a["OBS_ID"], a["REPORT_NAME"], a["TEXT"], a["REPORTED_BY"], a["KIND1"], a["LAT"]) == ("a", "速報", lines[0], "tsudo", "船舶", 35.0)
    assert "NOPE" not in a and a["BATCH_ID"]
    assert reported_ids(client, s) == {"a"}
    # Only the records asked about (a save's few records: the whole log is not read).
    assert reported_ids(client, s, ids=["a", "zz"]) == {"a"} and reported_ids(client, s, ids=["zz"]) == set()
    assert "OBS_ID IN ('a', 'zz')" in [f for u, f in fake.asked if u.endswith("/query")][-2]["where"]
    # Reported once: left out next time, whatever the format.
    assert select([obs(id="a")], s, rules, fences, DEFAULT_CONFIDENCES, reported=reported_ids(client, s)) == []
    rows = load_log(client, s, start=T0 - 1000)
    assert [(r.obs_id, r.format, r.reported_by) for r in rows] == [("a", "速報", "tsudo")]
    assert load_log(client, s, start=T0 + 1000) == []
    assert remove_log(client, s, rows) == 1
    assert reported_ids(client, s) == set() and len(log.rows) == 0


def test_manual_items_and_notify_on_save():
    from skywatch.core.obs.alerts import manual_items

    s = settings()
    assert s.notify_on_save is True
    assert alert_settings_of({"notifyOnSave": False, "formats": [{"name": "a"}]}, []).notify_on_save is False
    items = manual_items([obs(id="a", activity="停泊"), obs(id="g", deleted_at=T0)], [Geofence("舞鶴", SQUARE)])
    assert [(i.record.id, i.areas, i.rules, i.manual) for i in items] == [("a", ["舞鶴"], ["手動"], True)]
    fmt = alert_settings_of({"formats": [{"name": "a", "fields": ["objectName", "rule", "manual", {"source": "manual", "map": {"": "自動"}}]}]}, []).format()
    at = Moment(now=T0)
    assert build_lines(items, fmt, at) == ["船A|手動|手動|手動"]
    auto = select([obs()], s, None, (), DEFAULT_CONFIDENCES)
    assert build_lines(auto, fmt, at) == ["船A|||自動"]


def test_score_parts_weights_rarity_and_order():
    problems = []
    s = settings({"score": {"weights": {"confidence": 1, "area": 2, "rule": 1, "rarity": 2},
                            "rarity": {"days": 10, "radiusKm": 20, "level": 1}, "minScore": 10}}, problems)
    assert problems == [] and s.score.weights["area"] == 2 and s.score.days == 10 and s.score.level == 1
    fences = [Geofence("舞鶴", SQUARE, 0.5), Geofence("湾内", [[[134.9, 34.9], [135.1, 34.9], [135.1, 35.1], [134.9, 35.1], [134.9, 34.9]]], 1.0)]
    rules = [AlertRule(kind=["船舶"], weight=0.4), AlertRule(kind=["船舶"], activities=["航行"], weight=0.8),
             AlertRule(kind=["航空機"])]
    day = 86_400_000
    records = [
        obs(id="a", lon=135.0, lat=35.0),                                     # 確定, 湾内 (1.0), rule 0.8, 2 ships before → 1/3
        obs(id="b", confidence="推定", activity="停泊", lon=135.3, lat=35.3),   # 推定 0.5, 舞鶴 0.5, rule 0.4
        obs(id="c", kind="航空機", lon=136.0, lat=36.0),                       # outside, aircraft: none before
        obs(id="old1", image_time=T0 - 2 * day, created_at=T0 - day, lon=135.01),    # counted for a (same 大分類 船舶, near)
        obs(id="old2", kind="船舶 > 貨物船", image_time=T0 - 3 * day, created_at=T0 - day),
        obs(id="far", image_time=T0 - day, created_at=T0 - day, lon=137.0),           # too far
        obs(id="long", image_time=T0 - 20 * day, created_at=T0 - day),                # too long ago
    ]
    items = select(records, s, rules, fences, DEFAULT_CONFIDENCES, start=T0)
    by = {i.record.id: i for i in items}
    a, b, c = by["a"], by["b"], by["c"]
    assert a.parts == {"confidence": 1.0, "area": 1.0, "rule": 0.8, "rarity": 1 / 3} and a.similar == 2
    assert a.score == round(100 * (1 + 2 + 0.8 + 2 / 3) / 6, 1)
    assert b.parts["confidence"] == 0.5 and b.parts["area"] == 0.5 and b.parts["rule"] == 0.4
    assert c.parts["area"] == 0.0 and c.similar == 0
    assert [i.record.id for i in items] == sorted(by, key=lambda k: -by[k].score)
    assert score_note(a) == "確度 100・場所 100・条件 80・珍しさ 33（類似 2 件）"
    # Written as fields.
    fmt = s.format("詳細")
    fmt.fields = [AlertField(source="score", digits=0), AlertField(source="similar")]
    assert build_lines([a], fmt, Moment(now=T0))[0] == f"{round(a.score)}\t2"
    # minScore drops the low ones.
    s.score.min_score = 99
    assert select(records, s, rules, fences, DEFAULT_CONFIDENCES, start=T0) == []
    # No score: image time order, no score field.
    plain = settings({"score": False})
    assert plain.score is None
    items = select(records, plain, rules, fences, DEFAULT_CONFIDENCES, start=T0)
    assert all(i.score is None for i in items) and score_note(items[0]) == ""
    # Parts with nothing to measure are left out (no geofences, no rules, no levels).
    only = select([obs(id="z")], settings({"minConfidence": ""}), None, (), (), start=T0)[0]
    assert set(only.parts) == {"rarity"} and only.score == 100.0
    # Records added by hand: the rule part is 1.
    m = manual_items([obs(id="m", activity="停泊")], fences, settings(), DEFAULT_CONFIDENCES, records)[0]
    assert m.parts["rule"] == 1.0 and m.score is not None


def test_score_settings_problems_and_weight_values():
    problems = []
    s = settings({"score": {"weights": {"confidence": -1, "size": 1}, "rarity": {"days": "x", "level": 0}, "minScore": 200}}, problems)
    assert s.score.weights == {"confidence": 1.0, "area": 1.0, "rule": 1.0, "rarity": 1.0} and s.score.min_score == 0
    assert len(problems) == 5
    assert [weight_of(v) for v in (None, "", 0.3, "50", "80%", 2, -1, "高")] == [1.0, 1.0, 0.3, 0.5, 0.8, 0.02, 0.0, 1.0]
    assert rules_of_rows([{"KIND": "船舶", "WEIGHT": 0.25}], dict(s.rules.fields))[0].weight == 0.25


def test_live_settings_and_polling_only_what_changed():
    assert (settings().live.seconds, settings().live.days) == (10.0, 7.0)
    assert settings({"live": False}).live.seconds == 0 and settings({"live": 30}).live.seconds == 30
    assert settings({"live": {"seconds": 0.1, "days": 1}}).live.seconds == 2.0  # not shorter than 2 s
    problems = []
    assert settings({"live": "fast"}, problems).live.seconds == 10.0 and problems
    fake, _, _, log = esri()
    client = EsriClient(fake)
    s = settings()
    a = log.add({"OBS_ID": "a", "REPORT_NAME": "速報", "TEXT": "船A", "REPORTED_AT": T0, "REPORTED_BY": "tsudo"})
    log.add({"OBS_ID": "old", "TEXT": "古い", "REPORTED_AT": T0 - 10 * 86_400_000})
    first = poll_log(client, s, set(), since=T0 - 86_400_000)
    assert [r.obs_id for r in first.added] == ["a"] and first.gone == set() and first.ids == {a}
    # Nothing new: only the ids are asked for.
    fake.asked.clear()
    again = poll_log(client, s, first.ids, since=T0 - 86_400_000)
    assert again.added == [] and again.gone == set()
    assert len(fake.asked) == 1 and fake.asked[0][1]["returnIdsOnly"] == "true"
    # A row added and one deleted (by someone else).
    b = log.add({"OBS_ID": "b", "TEXT": "船B", "REPORTED_AT": T0 + 1000})
    log.rows = [r for r in log.rows if r["attributes"]["OBJECTID"] != a]
    changed = poll_log(client, s, again.ids, since=T0 - 86_400_000)
    assert [(r.obs_id, r.oid) for r in changed.added] == [("b", b)] and changed.gone == {a} and changed.ids == {b}
    # No window: every row, newest first, at most `limit` at first.
    assert [r.obs_id for r in poll_log(client, s, set()).added] == ["b", "old"]
    assert [r.obs_id for r in poll_log(client, s, set(), limit=1).added] == ["b"]


PLACE_URL = "https://x/FeatureServer/13"


def _box(w, s, e, n):
    return [[[w, s], [w, n], [e, n], [e, s], [w, s]]]


def test_named_areas_smallest_place_cache_and_field_options():
    from skywatch.core.obs.alerts import (
        DEFAULT_PLACE_FIELDS, forget_named_areas, load_named_areas, named_areas_of_features, places_of, ring_size,
    )

    # Size: about 111 km × 91 km for 1° at 35°N; a hole takes off.
    assert 9_500 < ring_size(_box(135, 34.5, 136, 35.5)) < 10_500
    hole = [[[135.25, 34.75], [135.75, 34.75], [135.75, 35.25], [135.25, 35.25], [135.25, 34.75]]]
    assert ring_size(_box(135, 34.5, 136, 35.5) + hole) < ring_size(_box(135, 34.5, 136, 35.5)) * 0.8

    fake = FakeEsri()
    layer = fake.add_layer(PLACE_URL, FakeLayer({"NAME": "esriFieldTypeString", "KIND": "esriFieldTypeString",
                                                 "ACTIVE": "esriFieldTypeSmallInteger"}, "esriGeometryPolygon"))
    layer.add({"NAME": "若狭湾", "KIND": "湾"}, {"rings": _box(134.0, 34.0, 136.0, 36.0)})
    layer.add({"NAME": "舞鶴港", "KIND": "港"}, {"rings": _box(134.8, 34.8, 135.2, 35.2)})
    layer.add({"NAME": "北吸岸壁", "KIND": "施設", "ACTIVE": 0}, {"rings": _box(134.99, 34.99, 135.01, 35.01)})
    layer.add({"NAME": "", "KIND": "名前なし"}, {"rings": _box(134.9, 34.9, 135.1, 35.1)})
    client = EsriClient(fake)
    problems = []
    s = settings({"namedAreas": PLACE_URL}, problems)
    assert problems == [] and s.named_areas.fields == DEFAULT_PLACE_FIELDS
    assert settings({"namedAreas": False}).named_areas is None and settings().named_areas is None
    assert settings({"namedAreas": "https://x/nope"}, problems).named_areas is None and problems

    forget_named_areas()
    clock = [0.0]
    areas = load_named_areas(client, s.named_areas, clock=lambda: clock[0])
    assert [a.name for a in areas] == ["若狭湾", "舞鶴港"]  # inactive and nameless ones left out
    asked = len(fake.asked)
    clock[0] = 599
    assert load_named_areas(client, s.named_areas, clock=lambda: clock[0]) is areas and len(fake.asked) == asked
    clock[0] = 601
    assert load_named_areas(client, s.named_areas, clock=lambda: clock[0]) is not areas and len(fake.asked) > asked
    asked = len(fake.asked)
    load_named_areas(client, s.named_areas, max_age=0, clock=lambda: clock[0])
    assert len(fake.asked) > asked
    assert load_named_areas(client, None) == []

    # Several hits: the smallest first, whatever the layer's order.
    assert [a.name for a in places_of(135.0, 35.0, list(reversed(areas)))] == ["舞鶴港", "若狭湾"]
    assert places_of(137.0, 35.0, areas) == []

    fmt = alert_settings_of({"formats": [{"name": "a", "fields": [
        "objectName", "place", "placeKind", "places",
        {"source": "place", "prefix": "（", "suffix": "付近）"},
        {"source": "place", "empty": "地名なし"},
        {"source": "place", "omitEmpty": True},
        "activity",
    ]}]}, []).format()
    items = select([obs(id="in"), obs(id="bay", lon=134.2, lat=34.2), obs(id="out", lon=137.0)], s, None, (), DEFAULT_CONFIDENCES,
                   named=areas)
    at = Moment(now=T0)
    by = dict(zip([i.record.id for i in items], build_lines(items, fmt, at)))
    assert by["in"] == "船A|舞鶴港|港|舞鶴港・若狭湾|（舞鶴港付近）|舞鶴港|舞鶴港|航行"
    assert by["bay"] == "船A|若狭湾|湾|若狭湾|（若狭湾付近）|若狭湾|若狭湾|航行"
    # Outside every area: empty, the `empty` text, or left out with its delimiter.
    assert by["out"] == "船A|||||地名なし|航行"
    # No named areas: places stay empty.
    assert build_lines(select([obs()], s, None, (), DEFAULT_CONFIDENCES), fmt, at) == ["船A|||||地名なし|航行"]
    # By hand too.
    manual = manual_items([obs(id="m")], (), s, DEFAULT_CONFIDENCES, (), areas)
    assert [a.name for a in manual[0].places] == ["舞鶴港", "若狭湾"]
    # Bad options are said.
    problems = []
    alert_settings_of({"formats": [{"name": "a", "fields": [{"source": "place", "prefix": 1, "omitEmpty": "yes"}]}]}, problems)
    assert any("prefix" in p for p in problems) and any("omitEmpty" in p for p in problems)
