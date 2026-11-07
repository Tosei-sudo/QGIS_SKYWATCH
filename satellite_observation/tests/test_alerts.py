"""Alerts (通報): settings, rules, geofences, the lines and the Esri log."""
import json

from satellite_observation.core.config import parse_config
from satellite_observation.core.esri import EsriClient
from satellite_observation.core.obs.alerts import (
    AlertRule, Geofence, Moment, alert_settings_of, build_lines, fill, inside_rings, load_geofences, load_log, load_rules,
    remove_log, report_text, reported_ids, rules_of_rows, select, write_file, write_log,
)
from satellite_observation.core.obs.model import DEFAULT_CONFIDENCES, Observation
from satellite_observation.tests.fake_esri import FakeEsri, FakeLayer

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
    assert [i.record.id for i in items] == ["a", "c"]
    assert items[0].rules == ["支援船"] and items[1].areas == ["舞鶴"]
    # No rules table: everything meeting the fixed conditions.
    assert [i.record.id for i in select(records, s, None, (), DEFAULT_CONFIDENCES, start=T0)] == ["a", "b", "c", "d", "h"]
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
    # Reported once: left out next time, whatever the format.
    assert select([obs(id="a")], s, rules, fences, DEFAULT_CONFIDENCES, reported=reported_ids(client, s)) == []
    rows = load_log(client, s, start=T0 - 1000)
    assert [(r.obs_id, r.format, r.reported_by) for r in rows] == [("a", "速報", "tsudo")]
    assert load_log(client, s, start=T0 + 1000) == []
    assert remove_log(client, s, rows) == 1
    assert reported_ids(client, s) == set() and len(log.rows) == 0


def test_manual_items_and_notify_on_save():
    from satellite_observation.core.obs.alerts import manual_items

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
