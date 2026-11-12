"""
The 「撮像予定」 dock in QGIS (2026-11-09): a day's rows of the history picked
on the calendar, listed and drawn by state, answered by hand or by a
system's result file, 要求外 plans added and taken off, and a layer of them.
"""
import json
import time
from datetime import date, timedelta

import pytest

pytest.importorskip("qgis.core")
from qgis.testing import start_app  # noqa: E402

APP = start_app()

from qgis.core import QgsProject, QgsRectangle, QgsSettings  # noqa: E402
from qgis.PyQt.QtCore import QDate  # noqa: E402
from qgis.PyQt.QtGui import QFont  # noqa: E402

from mikage.core.esri import EsriClient  # noqa: E402
from mikage.core.request.model import jst_day  # noqa: E402
from mikage.core.request.store import DEFAULT_HISTORY_FIELDS, DEFAULT_REQUEST_FIELDS, HistoryRow  # noqa: E402
from mikage.qgis_io import settings  # noqa: E402
from mikage.qgis_io.context import PluginContext  # noqa: E402
from mikage.tests.fake_esri import FakeEsri, FakeLayer  # noqa: E402
from mikage.tests.qgis_fakes import FakeIface, wait_for  # noqa: E402
from mikage.ui.schedule_dock import _when  # noqa: E402

LEDGER = "https://example.com/arcgis/rest/services/Req/FeatureServer/0"
HISTORY = "https://example.com/arcgis/rest/services/Req/FeatureServer/1"
TOKYO = [[[139.8, 35.45], [139.9, 35.45], [139.9, 35.55], [139.8, 35.55], [139.8, 35.45]]]
OSAKA = [[[135.4, 34.6], [135.5, 34.6], [135.5, 34.7], [135.4, 34.7], [135.4, 34.6]]]

CONFIG = {
    "cloudForecast": False,
    "requestStores": [{"label": "撮像依頼", "url": LEDGER, "history": HISTORY}],
    "requestExports": [
        {"system": "SYS-A", "label": "A", "kind": "光学", "format": "csv", "timeZone": "Asia/Tokyo",
         "columns": [{"key": "ID", "source": "id"}],
         "resultFile": {"key": "ID", "result": "判定", "adopted": ["OK"], "rejected": ["NG"], "missing": "不採用", "plannedAt": "予定",
                        "name": "名称", "geometry": {"bbox": ["W", "S", "E", "N"]}}},
        # SYS-B answers in JSON, read by its JSON Schema (a file beside the settings file, x-target).
        {"system": "SYS-B", "label": "B", "kind": "SAR", "format": "csv", "columns": [{"key": "ID", "source": "id"}],
         "resultFile": {"format": "json", "schema": "b.result.schema.json"}},
    ],
}


@pytest.fixture
def plugin(tmp_path, monkeypatch):
    QgsSettings().remove("mikage")
    config = tmp_path / "config.json"
    config.write_text(json.dumps(CONFIG, ensure_ascii=False), encoding="utf-8")
    settings.set_config_source(str(config))
    fake = FakeEsri()
    types = {n: "esriFieldTypeString" for n in DEFAULT_REQUEST_FIELDS.values()}
    types.update({n: "esriFieldTypeDate" for n in ("START_AT", "END_AT", "CREATED_AT", "UPDATED_AT")})
    types.update({n: "esriFieldTypeDouble" for n in ("PRIORITY", "BOOST")})
    ledger = fake.add_layer(LEDGER, FakeLayer(types, "esriGeometryPolygon"))
    for rid, name, rings in (("R-1", "東京湾", TOKYO), ("R-2", "大阪湾", OSAKA), ("R-3", "横浜", TOKYO)):
        ledger.add({"REQ_ID": rid, "REQ_NAME": name, "PRIORITY": 500, "KIND": "光学", "CATEGORY": "臨時", "STATUS": "有効"},
                   {"rings": rings, "spatialReference": {"wkid": 4326}})
    htypes = {n: "esriFieldTypeString" for n in DEFAULT_HISTORY_FIELDS.values()}
    htypes.update(RUN_DATE="esriFieldTypeDate", TARGET_DATE="esriFieldTypeDate", PLANNED_AT="esriFieldTypeDate", CLOUD="esriFieldTypeDouble",
                  SCENES="esriFieldTypeInteger", BOOST_DONE="esriFieldTypeInteger")
    history = fake.add_layer(HISTORY, FakeLayer(htypes, "esriGeometryPolygon"))
    from mikage import classFactory

    iface = FakeIface()
    p = classFactory(iface)
    monkeypatch.setattr(PluginContext, "esri", lambda self: EsriClient(fake))
    p.initGui()
    for dock in p.docks.values():
        dock.hide()
    assert p.api.problems == [], p.api.problems
    yield p, iface, fake, history, tmp_path
    p.unload()
    QgsProject.instance().removeAllMapLayers()
    QgsSettings().remove("mikage")


def written(history, day: str):
    """The day written out as the 「撮像計画ファイル出力」 dock does: 東京湾 to SYS-A, 大阪湾 to SYS-B, 横浜 held back."""
    from mikage.core.esri import EsriClient as Client  # noqa: F401 (the rows as the plugin writes them)
    from mikage.core.request.store import history_attributes

    now = time.time() * 1000
    for rid, name, system, result, rings in (("R-1", "東京湾", "SYS-A", "採用", TOKYO), ("R-2", "大阪湾", "SYS-B", "採用", OSAKA),
                                             ("R-3", "横浜", "", "見送り", [])):
        row = HistoryRow("RUN1", now, day, rid, system=system, result=result, name=name, captured="未判定" if result == "採用" else "",
                         adoption="不明" if result == "採用" else "", reason="" if result == "採用" else "枠なし", rings=rings)
        history.add({k: v for k, v in history_attributes(row, DEFAULT_HISTORY_FIELDS).items() if v is not None},
                    {"rings": rings, "spatialReference": {"wkid": 4326}} if rings else None)


def states(dock):
    return {(r.name, r.system): dock.model.item(i, 0).text() for i, r in enumerate(dock.shown)}


def test_the_days_plan_answered_by_hand_and_by_file(plugin):
    p, iface, fake, history, tmp_path = plugin
    today = jst_day(time.time() * 1000)
    written(history, today.isoformat())
    dock = p.api.schedule
    assert dock.windowTitle() == "撮像予定"
    dock.show()
    assert wait_for(lambda: dock._loaded and dock.task is None), dock.status.text()
    assert dock.day == today.isoformat() and len(dock.rows) == 3
    assert states(dock) == {("横浜", ""): "見送り", ("東京湾", "SYS-A"): "不明", ("大阪湾", "SYS-B"): "不明"}
    assert dock.status.text() == f"{today.isoformat()}: 計画 0・不明 2・見送り 1"
    # Drawn on the map in the state's colour (the held one, written without an area, with the ledger's).
    assert len(dock.overlay._painted) == 3
    # The calendar marks the days with rows (bold; blue while only answers are awaited).
    assert wait_for(lambda: dock.calendar.dateTextFormat(QDate(today.year, today.month, today.day)).fontWeight() == QFont.Weight.Bold)
    marked = dock.calendar.dateTextFormat(QDate(today.year, today.month, today.day))
    assert marked.foreground().color().name() == "#2563eb" and "不明 2" in marked.toolTip()

    # 東京湾 adopted by hand; 横浜 (held back) cannot be answered.
    row = next(i for i, r in enumerate(dock.shown) if r.name == "東京湾")
    dock.table.selectRow(row)
    assert dock.adopt_button.isEnabled()
    dock.reason.setText("電話で確認")
    dock.adopt_button.click()
    assert wait_for(lambda: dock.task is None and "採用 1 件を記録" in dock.status.text()), dock.status.text()
    assert states(dock)[("東京湾", "SYS-A")] == "採用"
    tokyo = next(r for r in history.rows if r["attributes"]["REQ_NAME"] == "東京湾")["attributes"]
    assert (tokyo["ADOPTION"], tokyo["ADOPTION_REASON"], tokyo["ADOPTION_SOURCE"]) == ("採用", "電話で確認", "手入力")
    held = next(i for i, r in enumerate(dock.shown) if r.name == "横浜")
    dock.table.selectRow(held)
    assert not dock.adopt_button.isEnabled() and not dock.delete_button.isEnabled()

    # SYS-A's file: 東京湾 NG after all, and two plans of its own (one with an area).
    path = tmp_path / "a_result.csv"
    path.write_text("ID,判定,予定,名称,W,S,E,N\nR-1,NG,,,,,,\n"
                    f"X-1,OK,{today.isoformat()} 10:30,定常 東京,139.0,35.0,139.2,35.2\nX-2,OK,,定常 2,,,,\n", encoding="utf-8")
    dock.system_filter.setCurrentIndex(dock.system_filter.findData("SYS-A"))
    assert {r.system for r in dock.shown} == {"SYS-A"}
    dock.import_file(str(path), ask=False)
    assert wait_for(lambda: dock.task is None and "a_result.csv: " in dock.status.text()), dock.status.text()
    assert states(dock) == {("東京湾", "SYS-A"): "不採用", ("定常 東京", "SYS-A"): "要求外", ("定常 2", "SYS-A"): "要求外"}
    # The day's tally is of every system (SYS-B's 大阪湾 still awaited); the list of SYS-A's only.
    assert dock.status.text() == (f"{today.isoformat()}: 計画 2（要求外 2）・不明 1・不採用 1・見送り 1（表示 3 件）。"
                                  "a_result.csv: 採用 0・不採用 1・要求外 2 を記録")
    own = next(r for r in dock.rows if r.name == "定常 東京")
    assert own.planned_at is not None and _when(own.planned_at).endswith("10:30") and own.rings
    # Read again: its 要求外 rows replaced, not doubled.
    dock.import_file(str(path), ask=False)
    assert wait_for(lambda: dock.task is None and dock.status.text().count("a_result.csv") == 1), dock.status.text()
    assert sum(1 for r in history.rows if r["attributes"]["RESULT"] == "要求外") == 2

    # The state filter, and the words.
    dock.system_filter.setCurrentIndex(0)
    dock.state_filter.setCurrentIndex(dock.state_filter.findData("plan"))
    assert sorted(r.name for r in dock.shown) == ["定常 2", "定常 東京"]
    dock.state_filter.setCurrentIndex(0)
    dock.search.setText("大阪")
    assert [r.name for r in dock.shown] == ["大阪湾"]
    dock.search.clear()

    # A 要求外 plan by hand (SYS-B's routine one, its area drawn on the map), then one taken off.
    dock.open_unrequested()
    dialog = dock.unrequested_dialog
    dialog.save()
    assert dialog.message.text() == "名称を入力"
    dialog.name.setText("B 定常")
    dialog.system.setCurrentIndex(dialog.system.findData("SYS-B"))
    dialog.timed.setChecked(True)
    dialog._extent_drawn(QgsRectangle(135.0, 34.0, 135.2, 34.2))
    assert dialog.area_label.text() == "範囲あり"
    dialog.save()
    assert wait_for(lambda: dock.task is None and "要求外「B 定常」を追加" in dock.status.text()), dock.status.text()
    mine = next(r for r in dock.rows if r.name == "B 定常")
    assert (mine.system, mine.adoption_source, mine.result) == ("SYS-B", "手入力", "要求外") and mine.rings
    assert jst_day(mine.planned_at) == today
    gone = next(i for i, r in enumerate(dock.shown) if r.name == "定常 2")
    dock.table.selectRow(gone)
    assert dock.delete_button.isEnabled()
    dock.delete_unrequested(ask=False)
    assert wait_for(lambda: dock.task is None and "要求外 1 件を削除" in dock.status.text()), dock.status.text()
    assert "定常 2" not in {r.name for r in dock.rows}

    # As a layer, coloured by state.
    layer = dock.to_layer()
    assert layer.featureCount() == len(dock.shown) == 5
    assert {c.value() for c in layer.renderer().categories()} == {"採用", "不明", "不採用", "見送り", "要求外"}
    # Another day: nothing planned.
    dock.set_day(today + timedelta(days=3))
    assert wait_for(lambda: dock.task is None and dock.day == (today + timedelta(days=3)).isoformat())
    assert dock.rows == [] and dock.status.text().endswith("計画なし") and dock.overlay._painted == []
    dock.close_all()


B_SCHEMA = {
    "type": "object", "required": ["results"],
    "properties": {
        "planDate": {"type": "string", "x-target": "day", "x-format": "%Y%m%d"},
        "results": {"type": "array", "x-target": "records", "items": {"type": "object", "properties": {
            "order": {"type": "string", "x-target": "id", "x-pattern": "^ORD-(.+)$"},
            "state": {"type": "string", "enum": ["ACCEPT", "REJECT", "PENDING"], "x-target": "result",
                      "x-map": {"ACCEPT": "採用", "REJECT": "不採用", "PENDING": "不明"}},
            "comment": {"type": "string", "x-target": "reason"},
            "eta": {"type": "integer", "x-target": "plannedAt", "x-format": "unixms"}}}},
    },
}


def test_a_json_file_read_by_the_systems_schema(plugin):
    p, iface, fake, history, tmp_path = plugin
    today = jst_day(time.time() * 1000)
    written(history, today.isoformat())
    (tmp_path / "b.result.schema.json").write_text(json.dumps(B_SCHEMA), encoding="utf-8")
    dock = p.api.schedule
    dock.show()
    assert wait_for(lambda: dock._loaded and dock.task is None), dock.status.text()
    at = int(time.time() * 1000)
    path = tmp_path / "b_answer.json"
    path.write_text(json.dumps({"planDate": today.strftime("%Y%m%d"), "results": [
        {"order": "ORD-R-2", "state": "REJECT", "comment": "衛星の都合"}, {"order": "ORD-Z-9", "state": "ACCEPT", "eta": at}]}),
        encoding="utf-8")
    dock.system_filter.setCurrentIndex(dock.system_filter.findData("SYS-B"))
    dock.import_file(str(path), ask=False)
    assert wait_for(lambda: dock.task is None and "b_answer.json: " in dock.status.text()), dock.status.text()
    assert states(dock) == {("大阪湾", "SYS-B"): "不採用", ("Z-9", "SYS-B"): "要求外"}
    osaka = next(r for r in dock.rows if r.name == "大阪湾")
    assert (osaka.adoption_reason, osaka.adoption_source) == ("衛星の都合", "b_answer.json")
    assert next(r for r in dock.rows if r.name == "Z-9").planned_at == at
    # A file the schema does not fit: told, and read all the same.
    path.write_text(json.dumps({"results": [{"order": "ORD-R-2", "state": "MAYBE"}]}), encoding="utf-8")
    reading = []
    dock._confirm_reading = lambda r, label: reading.append(r) or False
    dock.import_file(str(path))
    assert wait_for(lambda: dock.task is None and reading), dock.status.text()
    assert reading[0].problems == ["スキーマ: $.results[0].state: 'MAYBE' は ['ACCEPT', 'REJECT', 'PENDING'] 以外",
                                   "1 行目: 採否「MAYBE」が不明（列の読み替え map に MAYBE=採用 のように書く）"]
    dock.close_all()


def test_an_old_history_table_says_what_it_lacks(plugin, monkeypatch):
    p, iface, fake, history, tmp_path = plugin
    for name in ("ADOPTION", "ADOPTION_REASON", "PLANNED_AT", "EXTERNAL_ID", "ADOPTION_SOURCE"):
        history.types.pop(name)
    # A table without shapes either: the areas are the ledger's.
    history.geometry = "esriGeometryNone"
    today = jst_day(time.time() * 1000)
    written(history, today.isoformat())
    for r in history.rows:
        r["geometry"] = None
    dock = p.api.schedule
    dock.show()
    assert wait_for(lambda: dock._loaded and dock.task is None), dock.status.text()
    assert "属性 ADOPTION・ADOPTION_REASON・PLANNED_AT・EXTERNAL_ID・ADOPTION_SOURCE がない" in dock.status.text()
    assert not dock.import_button.isEnabled() and not dock.add_button.isEnabled()
    dock.table.selectRow(0)
    assert not dock.adopt_button.isEnabled()
    # Drawn from the ledger's areas.
    assert len(dock.overlay._painted) == 3
    assert dock.rings_of(next(r for r in dock.rows if r.name == "大阪湾"))
    dock.close_all()


def test_no_history_no_plan(tmp_path):
    QgsSettings().remove("mikage")
    config = tmp_path / "config.json"
    config.write_text(json.dumps({"requestStores": [{"url": LEDGER}]}), encoding="utf-8")
    settings.set_config_source(str(config))
    from mikage import classFactory

    p = classFactory(FakeIface())
    p.initGui()
    try:
        dock = p.api.schedule
        assert "出力履歴（history）なし" in dock.status.text()
        assert not dock.import_button.isEnabled() and not dock.layer_button.isEnabled()
    finally:
        p.unload()
        QgsSettings().remove("mikage")


def test_set_day_reads_the_day(plugin):
    p, iface, fake, history, tmp_path = plugin
    day = date(2026, 11, 20)
    written(history, day.isoformat())
    dock = p.api.schedule
    dock.show()
    assert wait_for(lambda: dock._loaded and dock.task is None)
    dock.set_day(day)
    assert wait_for(lambda: dock.task is None and dock.day == day.isoformat())
    assert {r.name for r in dock.rows} == {"東京湾", "大阪湾", "横浜"}
    dock.close_all()


def test_a_result_file_naming_rows_by_their_link_id(plugin):
    # 外部連携ID (2026-11-12): the file names the rows by the ids its order file was given; one of the day before is
    # that day's row (the system planned late a request handed out earlier).
    from mikage.core.request.store import history_attributes
    from mikage.core.request.systems import export_systems_of

    p, iface, fake, history, tmp_path = plugin
    today = jst_day(time.time() * 1000)
    yesterday = (today - timedelta(days=1)).isoformat()
    written(history, today.isoformat())
    for r in history.rows:
        if r["attributes"]["REQ_ID"] == "R-1":
            r["attributes"]["LINK_ID"] = "TOKYO0000001"
    row = HistoryRow("RUN0", time.time() * 1000 - 86400000, yesterday, "R-3", system="SYS-A", name="横浜", adoption="不明",
                     link_id="YOKOHAMA0001", rings=TOKYO)
    history.add({k: v for k, v in history_attributes(row, DEFAULT_HISTORY_FIELDS).items() if v is not None},
                {"rings": TOKYO, "spatialReference": {"wkid": 4326}})
    problems: list = []
    [system] = export_systems_of([{"system": "SYS-A", "label": "A", "columns": [{"key": "ID", "source": "id"}], "resultFile": {
        "columns": [{"key": "REF", "target": "linkId", "pattern": "^SW-(.+)$"},
                    {"key": "ST", "target": "result", "map": {"OK": "採用", "NG": "不採用"}}]}}], problems)
    assert problems == []
    dock = p.api.schedule
    dock.show()
    assert wait_for(lambda: dock._loaded and dock.task is None), dock.status.text()
    tokyo = next(i for i, r in enumerate(dock.shown) if r.name == "東京湾")
    assert dock.model.item(tokyo, 1).toolTip() == "外部連携ID: TOKYO0000001"
    dock.search.setText("tokyo0000001")
    assert [r.name for r in dock.shown] == ["東京湾"]
    dock.search.clear()
    path = tmp_path / "a_link.csv"
    path.write_text("REF,ST\nSW-TOKYO0000001,OK\nSW-yokohama0001,OK\n", encoding="utf-8")
    dock.import_file(str(path), system=system, ask=False)
    assert wait_for(lambda: dock.task is None and "a_link.csv: " in dock.status.text()), dock.status.text()
    assert states(dock)[("東京湾", "SYS-A")] == "採用"
    assert f"外部連携IDで {yesterday} の出力の行に照合した 1 行" in dock.status.text()
    earlier = next(r["attributes"] for r in history.rows if r["attributes"].get("LINK_ID") == "YOKOHAMA0001")
    assert (earlier["ADOPTION"], earlier["ADOPTION_SOURCE"]) == ("採用", "a_link.csv")
    # Neither is a plan of the system's own (要求外).
    assert not any(r["attributes"]["RESULT"] == "要求外" for r in history.rows)
    dock.close_all()
