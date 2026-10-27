"""The plugin's parts around planning that the viewer has in its panel: targets from features, the CSV and layer, the satellite tree, catalogs."""
import math
from dataclasses import replace

from satellite_observation.core.esri import EsriClient
from satellite_observation.core.plan.access import PlanOptions, PlanTarget, plan_access
from satellite_observation.core.plan.catalogs import load_satellites, load_sensor_modes
from satellite_observation.core.plan.cover import cover_plan
from satellite_observation.core.plan.modes import sample_sensor_modes, sensor_catalog_of, with_modes
from satellite_observation.core.plan.output import (
    cover_text, first_only, js_round, natural_key, percent, plan_csv, plan_features, sorted_ops, split_antimeridian,
)
from satellite_observation.core.plan.satellites import GroundStation, sample_satellites, satellite_catalog_of
from satellite_observation.core.plan.targets import CELLS, Shape, cells_of, in_polygon, name_of, priority_of, spread_out, target_of
from satellite_observation.core.plan.tree import SatelliteChoice, facts_of, label_of
from satellite_observation.tests.fake_esri import FakeEsri, FakeLayer
from satellite_observation.tests.test_plan import EPOCH, DAY, LINE1, LINE2, iss

SQUARE = [(139.0, 35.0), (140.0, 35.0), (140.0, 36.0), (139.0, 36.0), (139.0, 35.0)]


def test_in_polygon_with_hole():
    hole = [(139.4, 35.4), (139.6, 35.4), (139.6, 35.6), (139.4, 35.6), (139.4, 35.4)]
    assert in_polygon([SQUARE], 139.5, 35.5)
    assert not in_polygon([SQUARE, hole], 139.5, 35.5)
    assert in_polygon([SQUARE, hole], 139.2, 35.2)
    assert not in_polygon([SQUARE], 141, 35.5)


def test_cells_spread_inside():
    cells = cells_of([[SQUARE]])
    assert CELLS * 0.8 < len(cells) < CELLS * 1.25
    assert all(139 < x < 140 and 35 < y < 36 for x, y in cells)
    # A thin sliver: a finer grid until enough fall inside.
    sliver = [(139.0, 35.0), (140.0, 35.0), (140.0, 35.001), (139.0, 35.0)]
    assert len(cells_of([[sliver]])) > 0


def test_target_of_shapes():
    point = target_of([Shape([(139.7, 35.7)])], "p")
    assert point.center == (139.7, 35.7) and point.points == [] and point.cells is None
    area = target_of([Shape(SQUARE, [[SQUARE]], (139.5, 35.5))], "a")
    assert area.center == (139.5, 35.5)
    assert len(area.points) == 5 and len(area.cells) > 100
    two = target_of([Shape([(139.0, 35.0)]), Shape([(141.0, 37.0)])], "two")
    assert two.center == (140.0, 36.0) and two.cells is None
    assert target_of([], "none") is None
    assert len(spread_out(list(range(1000)), 256)) == 256


def test_priority_and_name():
    assert [priority_of(v) for v in [3, "2.5", " 7 ", "", "x", None, True, math.nan]] == [3, 2.5, 7, None, None, None, None, None]
    assert name_of({"NAME": "東京"}, 5, 0) == "東京"
    assert name_of({"名称": "大阪", "code": "A"}, 5, 0, "code") == "A"
    assert name_of({}, 12, 0) == "12"
    assert name_of({"name": ""}, None, 3) == "地物 4"


def test_texts():
    assert js_round(1.25) == "1.3" and js_round(3.0) == "3" and js_round(-0.04) == "0"
    assert [percent(v) for v in (1, 0.996, 0.004, 0, 0.5)] == ["100%", "100%", "1%", "0%", "50%"]
    assert sorted(["SAT-10", "SAT-2", "sat-1"], key=natural_key) == ["sat-1", "SAT-2", "SAT-10"]
    assert split_antimeridian([(179, 0), (179.5, 1), (-179.5, 2), (-179, 3)]) == [[(179, 0), (179.5, 1)], [(-179.5, 2), (-179, 3)]]


def _plan():
    sats = with_modes(sample_satellites(), sample_sensor_modes())
    targets = [PlanTarget("東京", (139.7, 35.68)), PlanTarget("面", (139.5, 35.5), SQUARE, cells_of([[SQUARE]]))]
    options = PlanOptions(EPOCH, EPOCH + 7 * DAY, None, "optical", 10, stations=[GroundStation("勝浦", 140.3, 35.15, 5)])
    return sats, targets, options, plan_access(sats, targets, options)


def test_csv_like_the_viewer():
    sats, targets, options, result = _plan()
    csv = plan_csv(result.opportunities, sats, targets, options.stations)
    assert csv.startswith("﻿日時（UTC）,日時（ローカル）,発注期限（ローカル）,衛星,撮像モード,衛星ID,対象,経度,緯度,オフナディア角,入射角,方向,軌道,太陽高度,幅km,長さkm,シーン数,被覆率%,受信見込み（ローカル）,地上局,TLE経過日数\r\n")
    lines = csv.split("\r\n")
    assert len(lines) == len(result.opportunities) + 2 and lines[-1] == ""
    first = lines[1].split(",")
    assert first[0].endswith("Z") and first[6] in ("東京", "面")
    plans = cover_plan(result.opportunities, targets, sats)
    combined = plan_csv([s for p in plans for s in p.steps], sats, targets)
    assert "追加%,累計%,シーン番号,同じシーンの地点数,TLE経過日数" in combined.split("\r\n")[0]


def test_layer_rows():
    sats, targets, options, result = _plan()
    layer = plan_features(result.opportunities, sats, targets, False, options.stations)
    assert [f.name for f in layer.fields][:5] == ["time", "satellite", "mode", "satellite_id", "target"]
    assert "downlink" in [f.name for f in layer.fields]
    assert len(layer.features) == len(result.opportunities)
    rings, a = layer.features[0]
    assert rings and rings[0][0] == rings[0][-1]
    assert a["time"] == result.opportunities[0].time and a["scenes"] == len(rings)


def test_sorting_and_first_only():
    sats, targets, options, result = _plan()
    ops = result.opportunities
    by_angle = sorted_ops(ops, "offNadir", False, sats, targets)
    assert [o.offNadir for o in by_angle] == sorted(o.offNadir for o in ops)
    by_sat = sorted_ops(ops, "satellite", True, sats, targets)
    assert by_sat[0].satellite != by_sat[-1].satellite
    firsts = first_only(ops)
    assert len({(o.satellite, o.target) for o in firsts}) == len(firsts)
    assert cover_text(replace(ops[0], cells=None, width=0, frames=1)) == "1 シーン"


def test_tree_groups_and_choices():
    choice = SatelliteChoice()
    sats = with_modes(sample_satellites(), sample_sensor_modes())
    choice.set(sats)
    # The SAR satellite by its usual mode only.
    assert [label_of(l) for l in choice.listed if l.checked] == ["サンプル光学衛星", "ストリップマップ"]
    assert choice.tally() == (2, 2)
    kinds = choice.tree()
    assert [k.name for k in kinds] == ["光学", "SAR"]
    sar_series = kinds[1].children[0]
    assert sar_series.name == "サンプル" and sar_series.children[0].level == 3
    choice.choose(kinds[1].members, False)
    assert choice.tally() == (1, 2)
    choice.check_mode("スポットライト", True)
    assert [l.sat.mode for l in choice.listed if l.checked] == [None, "スポットライト"]
    # Choosing the group again keeps the mode already checked.
    choice.choose(kinds[1].members, True)
    assert [l.sat.mode for l in choice.listed if l.checked] == [None, "スポットライト"]
    choice.check_only(["サンプルSAR衛星（スキャンSAR）"])
    assert [l.sat.mode for l in choice.listed if l.checked] == ["スキャンSAR"]
    # A new list keeps the choices; satellites new to it start checked.
    choice.set([*sats, iss(source="貼り付け")])
    assert [l.checked for l in choice.listed] == [False, False, False, True, True]
    assert facts_of(iss(minInterval=10, lookSide="right")) == "最大 45°・幅 10 km・間隔 10 分・右向き"


def test_catalogs_from_esri():
    fake = FakeEsri()
    url = "https://example.com/arcgis/rest/services/Sats/FeatureServer/0"
    layer = fake.add_layer(url, FakeLayer({"NAME": "esriFieldTypeString", "TLE": "esriFieldTypeString", "KIND": "esriFieldTypeString"}, max_record_count=2))
    layer.add({"NAME": "ISS", "TLE": f"{LINE1}\n{LINE2}", "KIND": "SAR"})
    layer.add({"NAME": "B", "TLE": f"{LINE1}\n{LINE2}", "KIND": "光学"})
    layer.add({"NAME": "C", "TLE": f"{LINE1}\n{LINE2}"})
    layer.add({"NAME": "NO-TLE"})
    catalog = satellite_catalog_of({"label": "テスト衛星", "url": url, "fields": {"name": "NAME", "tle": "TLE", "kind": "KIND"}}, [], 0)
    sats, problems = load_satellites(EsriClient(fake), catalog, lambda u, t: t)
    assert [s.name for s in sats] == ["ISS", "B", "C"] and [s.sar for s in sats] == [True, False, False]
    assert problems == ["NO-TLE: TLE なし"]
    table = "https://example.com/arcgis/rest/services/Sensors/FeatureServer/1"
    modes_layer = fake.add_layer(table, FakeLayer({"SENSOR": "esriFieldTypeString", "MODE": "esriFieldTypeString"}))
    modes_layer.add({"SENSOR": "ISS", "MODE": "Spot"})
    catalogs = [sensor_catalog_of({"url": table, "fields": {"sensor": "SENSOR", "mode": "MODE"}}, [], 0),
                sensor_catalog_of({"label": "壊れた表", "url": "https://example.com/arcgis/rest/services/X/FeatureServer/9", "fields": {"sensor": "S"}}, [], 1),
                sensor_catalog_of({"sensors": [{"sensor": "B", "modes": [{"mode": "Wide"}]}]}, [], 2)]
    modes, problems = load_sensor_modes(EsriClient(fake), catalogs, lambda u, t: t)
    assert [(m.sensor, m.mode) for m in modes] == [("ISS", "Spot"), ("B", "Wide")]
    assert len(problems) == 1 and problems[0].startswith("壊れた表 の読み込みに失敗")


def test_timeline_rows_and_layout():
    from satellite_observation.core.plan.modes import SensorMode
    from satellite_observation.core.plan.timeline import HEAD, ROW, bar_at, layout, timeline_rows
    from satellite_observation.tests.test_plan import _pair

    a, b, options = _pair()
    modes = [SensorMode("ISS", "スポット", swath=5, primary=True), SensorMode("ISS", "ストリップ", swath=30)]
    sats = [*with_modes([iss(minInterval=10)], modes), iss(name="ISS-2", id="2", tle2=LINE2.replace("325.0288", "145.0288"))]
    ops = plan_access(sats, [a, b], options).opportunities
    plans = cover_plan(ops, [a, b], sats)
    steps = [s for p in plans for s in p.steps]
    # A satellite's modes share one row (the viewer's sensor-modes test).
    rows = timeline_rows(steps, sats)
    assert len(rows) == len({s.satellite if s.satellite == 2 else 0 for s in steps})
    assert all(r.ops == sorted(r.ops, key=lambda o: o.time) for r in rows)
    lay = layout(steps, sats, options.start, options.end, 800, steps[0])
    assert lay.height == HEAD + len(rows) * ROW + 4
    assert [d for d in lay.days if d[1]] and all(120 <= d[0] <= 792 for d in lay.days)
    assert len(lay.bars) == len(steps) and sum(b.chosen for b in lay.bars) == 1
    # The busy time after each shot of the satellite with an interval.
    assert len(lay.busy) == sum(1 for s in steps if sats[s.satellite].minInterval > 0)
    bar = lay.bars[0]
    assert bar_at(lay, bar.x + 1, bar.y + ROW / 2) is bar.op
    assert bar_at(lay, bar.x, bar.y - 5) is None
    assert layout([], sats, 0, 1, 800) is None
