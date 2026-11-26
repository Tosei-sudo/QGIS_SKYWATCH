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
from mikage.features.requests import Requests  # noqa: E402
from mikage.tests.feature_harness import small_plugin  # noqa: E402
from mikage.ui.schedule_dock import _when, days_text  # noqa: E402

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

    iface = FakeIface()
    p = small_plugin(iface, Requests)
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
    assert wait_for(lambda: dock._loaded and not dock.busy), dock.status.text()
    assert dock.day == today.isoformat() and len(dock.rows) == 3
    # Unless chosen otherwise, all but 見送り (tsudo, 2026-11-16: 「初期値を、見送り以外にしてほしい」).
    assert dock.state_filter.text() == "見送り以外" and dock.state_filter.chosen() == ["採用", "不明", "不採用", "要求外"]
    assert states(dock) == {("東京湾", "SYS-A"): "不明", ("大阪湾", "SYS-B"): "不明"}
    dock.state_filter.choose([])
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
    assert wait_for(lambda: not dock.busy and "採用 1 件を記録" in dock.status.text()), dock.status.text()
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
    assert wait_for(lambda: not dock.busy and "a_result.csv: " in dock.status.text()), dock.status.text()
    assert states(dock) == {("東京湾", "SYS-A"): "不採用", ("定常 東京", "SYS-A"): "要求外", ("定常 2", "SYS-A"): "要求外"}
    # The day's tally is of every system (SYS-B's 大阪湾 still awaited); the list of SYS-A's only.
    assert dock.status.text() == (f"{today.isoformat()}: 計画 2（要求外 2）・不明 1・不採用 1・見送り 1（表示 3 件）。"
                                  "a_result.csv: 採用 0・不採用 1・要求外 2 を記録")
    own = next(r for r in dock.rows if r.name == "定常 東京")
    assert own.planned_at is not None and _when(own.planned_at).endswith("10:30") and own.rings
    # Read again: its 要求外 rows replaced, not doubled.
    dock.import_file(str(path), ask=False)
    assert wait_for(lambda: not dock.busy and dock.status.text().count("a_result.csv") == 1), dock.status.text()
    assert sum(1 for r in history.rows if r["attributes"]["RESULT"] == "要求外") == 2

    # The state filter, and the words.
    dock.system_filter.setCurrentIndex(0)
    plan = dock.state_filter
    plan.choose(["採用", "要求外"])
    assert sorted(r.name for r in dock.shown) == ["定常 2", "定常 東京"]
    # The preset's name in the box; kept for next time.
    assert plan.text() == "計画に入ったもの（採用・要求外）" and json.loads(settings._get("schedule/states")) == ["採用", "要求外"]
    plan.choose([])
    assert plan.text() == "すべての状態" and settings._get("schedule/states") == ""
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
    assert wait_for(lambda: not dock.busy and "要求外「B 定常」を追加" in dock.status.text()), dock.status.text()
    mine = next(r for r in dock.rows if r.name == "B 定常")
    assert (mine.system, mine.adoption_source, mine.result) == ("SYS-B", "手入力", "要求外") and mine.rings
    assert jst_day(mine.planned_at) == today
    gone = next(i for i, r in enumerate(dock.shown) if r.name == "定常 2")
    dock.table.selectRow(gone)
    assert dock.delete_button.isEnabled()
    dock.delete_unrequested(ask=False)
    assert wait_for(lambda: not dock.busy and "要求外 1 件を削除" in dock.status.text()), dock.status.text()
    assert "定常 2" not in {r.name for r in dock.rows}

    # As a layer, coloured by state.
    layer = dock.to_layer()
    assert layer.featureCount() == len(dock.shown) == 5
    assert {c.value() for c in layer.renderer().categories()} == {"採用", "不明", "不採用", "見送り", "要求外"}
    # Another day: nothing planned.
    dock.set_day(today + timedelta(days=3))
    assert wait_for(lambda: not dock.busy and dock.day == (today + timedelta(days=3)).isoformat())
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
    assert wait_for(lambda: dock._loaded and not dock.busy), dock.status.text()
    at = int(time.time() * 1000)
    path = tmp_path / "b_answer.json"
    path.write_text(json.dumps({"planDate": today.strftime("%Y%m%d"), "results": [
        {"order": "ORD-R-2", "state": "REJECT", "comment": "衛星の都合"}, {"order": "ORD-Z-9", "state": "ACCEPT", "eta": at}]}),
        encoding="utf-8")
    dock.system_filter.setCurrentIndex(dock.system_filter.findData("SYS-B"))
    dock.import_file(str(path), ask=False)
    assert wait_for(lambda: not dock.busy and "b_answer.json: " in dock.status.text()), dock.status.text()
    assert states(dock) == {("大阪湾", "SYS-B"): "不採用", ("Z-9", "SYS-B"): "要求外"}
    osaka = next(r for r in dock.rows if r.name == "大阪湾")
    assert (osaka.adoption_reason, osaka.adoption_source) == ("衛星の都合", "b_answer.json")
    assert next(r for r in dock.rows if r.name == "Z-9").planned_at == at
    # A file the schema does not fit: told, and read all the same.
    path.write_text(json.dumps({"results": [{"order": "ORD-R-2", "state": "MAYBE"}]}), encoding="utf-8")
    reading = []
    dock._confirm_reading = lambda r, label: reading.append(r) or False
    dock.import_file(str(path))
    assert wait_for(lambda: not dock.busy and reading), dock.status.text()
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
    assert wait_for(lambda: dock._loaded and not dock.busy), dock.status.text()
    assert "（管理者に連絡: 出力履歴の ADOPTION・ADOPTION_REASON・PLANNED_AT・EXTERNAL_ID・ADOPTION_SOURCE）" in dock.status.text()
    assert not dock.import_button.isEnabled() and not dock.add_button.isEnabled()
    dock.state_filter.choose([])
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

    p = small_plugin(FakeIface(), Requests)
    p.initGui()
    try:
        dock = p.api.schedule
        assert "（管理者に連絡: history）" in dock.status.text()
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
    assert wait_for(lambda: dock._loaded and not dock.busy)
    dock.set_day(day)
    assert wait_for(lambda: not dock.busy and dock.day == day.isoformat())
    assert {r.name for r in dock.rows} == {"東京湾", "大阪湾", "横浜"}
    dock.close_all()


def test_the_image_of_a_plan_taken_is_shown(plugin):
    # tsudo 2026-11-16: 「撮像済みになってるやつ、画像直接は開けなくていいけど、せめて画像IDは表示してほしい」.
    from mikage.ui.schedule_dock import COLUMNS

    p, iface, fake, history, tmp_path = plugin
    today = jst_day(time.time() * 1000)
    written(history, today.isoformat())
    for r in history.rows:
        a = r["attributes"]
        if a["REQ_ID"] == "R-1":
            a.update(CAPTURED="撮像済み", IMAGE_ID="IMG-42", ADOPTION="採用", ADOPTION_SOURCE="画像の入着")
        elif a["REQ_ID"] == "R-2":
            # Marked 未撮像 by hand once it had an image: the id left behind is not that of an image taken.
            a.update(CAPTURED="未撮像", IMAGE_ID="IMG-OLD")
    dock = p.api.schedule
    dock.show()
    assert wait_for(lambda: dock._loaded and not dock.busy), dock.status.text()
    dock.state_filter.choose([])
    column = COLUMNS.index("画像ID")
    images = {r.name: dock.model.item(i, column).text() for i, r in enumerate(dock.shown)}
    assert images == {"東京湾": "IMG-42", "大阪湾": "", "横浜": ""}
    assert dock.model.horizontalHeaderItem(column).text() == "画像ID"
    tokyo = next(i for i, r in enumerate(dock.shown) if r.name == "東京湾")
    assert dock.model.item(tokyo, COLUMNS.index("撮像")).text() == "撮像済み"
    # Found by its image's id too.
    dock.search.setText("img-42")
    assert [r.name for r in dock.shown] == ["東京湾"]
    dock.search.setText("img-old")
    assert dock.shown == []
    dock.search.clear()
    # And on the layer.
    layer = dock.to_layer()
    got = {f["名称"]: f["画像ID"] for f in layer.getFeatures()}
    assert got == {"東京湾": "IMG-42", "大阪湾": "", "横浜": ""}
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
    assert wait_for(lambda: dock._loaded and not dock.busy), dock.status.text()
    tokyo = next(i for i, r in enumerate(dock.shown) if r.name == "東京湾")
    assert dock.model.item(tokyo, 1).toolTip() == "外部連携ID: TOKYO0000001"
    dock.search.setText("tokyo0000001")
    assert [r.name for r in dock.shown] == ["東京湾"]
    dock.search.clear()
    path = tmp_path / "a_link.csv"
    path.write_text("REF,ST\nSW-TOKYO0000001,OK\nSW-yokohama0001,OK\n", encoding="utf-8")
    dock.import_file(str(path), system=system, ask=False)
    assert wait_for(lambda: not dock.busy and "a_link.csv: " in dock.status.text()), dock.status.text()
    assert states(dock)[("東京湾", "SYS-A")] == "採用"
    assert f"外部連携IDで {yesterday} の出力の行に照合した 1 行" in dock.status.text()
    earlier = next(r["attributes"] for r in history.rows if r["attributes"].get("LINK_ID") == "YOKOHAMA0001")
    assert (earlier["ADOPTION"], earlier["ADOPTION_SOURCE"]) == ("採用", "a_link.csv")
    # Neither is a plan of the system's own (要求外).
    assert not any(r["attributes"]["RESULT"] == "要求外" for r in history.rows)
    dock.close_all()


def test_a_result_file_of_another_day_goes_on_its_days(plugin, monkeypatch):
    # tsudo, 2026-11-20: 「別の日の選択でも正しくインポートしてほしい。計画外のやつは、選択している日付じゃなくて、
    # その撮像予定の日付のところに出してほしい」.
    from mikage.core.request.store import history_attributes
    from mikage.ui import schedule_dock

    p, iface, fake, history, tmp_path = plugin
    today = jst_day(time.time() * 1000)
    tomorrow, later = today + timedelta(days=1), today + timedelta(days=2)
    written(history, today.isoformat())
    written(history, tomorrow.isoformat())
    # A routine plan an import before 2026-11-20 put on the day shown (today), though planned for the day after tomorrow.
    stray = HistoryRow("OLD", time.time() * 1000, today.isoformat(), "", system="SYS-A", result="要求外", name="定常 東京", adoption="採用",
                       external_id="X-1", adoption_source="a_tomorrow.csv",
                       planned_at=schedule_dock.datetime.fromisoformat(f"{later.isoformat()}T10:30+09:00").timestamp() * 1000)
    history.add({k: v for k, v in history_attributes(stray, DEFAULT_HISTORY_FIELDS).items() if v is not None}, None)
    dock = p.api.schedule
    dock.show()
    assert wait_for(lambda: dock._loaded and not dock.busy), dock.status.text()
    assert dock.day == today.isoformat()
    dock.system_filter.setCurrentIndex(dock.system_filter.findData("SYS-A"))
    path = tmp_path / "a_tomorrow.csv"
    path.write_text("ID,判定,予定,名称,W,S,E,N\n"
                    f"R-1,OK,{tomorrow.isoformat()} 09:00,,,,,\n"
                    f"X-1,OK,{later.isoformat()} 10:30,定常 東京,139.0,35.0,139.2,35.2\n", encoding="utf-8")
    # What is asked first: the days it goes on, not the day shown.
    asked = []
    monkeypatch.setattr(schedule_dock, "ask_no", lambda parent, title, text, **_k: asked.append(text) or False)
    dock.import_file(str(path))
    assert wait_for(lambda: not dock.busy and asked), dock.status.text()
    assert asked[0].splitlines() == [
        "A・a_tomorrow.csv",
        f"{tomorrow.isoformat()}: 採用 1・不採用 0・要求外 0", f"{later.isoformat()}: 採用 0・不採用 0・要求外 1",
        f"表示中の {today.isoformat()} ではなく、ファイルの行の日（対象日、なければ撮像予定の日）に記録",
        f"前に結果ファイルから入れたこのシステムの要求外 1 件（{tomorrow.isoformat()}・{later.isoformat()} の分と、ほかの日の同じ計画）は置き換える"]
    assert dock.status.text() == "取り込みを取りやめ"

    dock.import_file(str(path), ask=False)
    assert wait_for(lambda: not dock.busy and f"a_tomorrow.csv（{tomorrow.isoformat()}・{later.isoformat()}）: " in dock.status.text()), \
        dock.status.text()
    # The file's day is shown, its rows answered there; today's rows are as they were.
    assert dock.day == tomorrow.isoformat() and dock.chosen_day() == tomorrow
    assert states(dock) == {("東京湾", "SYS-A"): "採用"}
    by_day = {(r["attributes"]["TARGET_DAY"], r["attributes"]["REQ_NAME"]): r["attributes"] for r in history.rows}
    assert by_day[(today.isoformat(), "東京湾")]["ADOPTION"] == "不明"
    assert (by_day[(tomorrow.isoformat(), "東京湾")]["ADOPTION"], by_day[(tomorrow.isoformat(), "東京湾")]["ADOPTION_SOURCE"]) == (
        "採用", "a_tomorrow.csv")
    # The 要求外 on the day of its 撮像予定, the one put on today replaced (not doubled).
    own = [r["attributes"] for r in history.rows if r["attributes"]["RESULT"] == "要求外"]
    assert [(a["TARGET_DAY"], a["REQ_NAME"]) for a in own] == [(later.isoformat(), "定常 東京")]
    dock.set_day(later)
    assert wait_for(lambda: not dock.busy and dock.day == later.isoformat())
    assert states(dock) == {("定常 東京", "SYS-A"): "要求外"} and _when(dock.rows[0].planned_at).endswith("10:30")
    dock.close_all()
    # A file of a week, told short (the status line, the confirmation).
    week = [(today + timedelta(days=i)).isoformat() for i in range(7)]
    assert days_text(week[:3]) == "・".join(week[:3]) and days_text(week) == f"{week[0]}〜{week[-1]}（7 日）"


def test_a_plan_taken_in_an_image_rated_unusable_is_marked(plugin):
    """画像の評価 (2026-11-24, 設計 2): a row 撮像済み whose image is 使えない over the request is marked, not changed."""
    from mikage.core.image_reviews import DEFAULT_REVIEW_FIELDS
    from mikage.ui.schedule_dock import COLUMNS

    p, iface, fake, history, tmp_path = plugin
    reviews_url = "https://example.com/arcgis/rest/services/Work/FeatureServer/2"
    reviews = fake.add_layer(reviews_url, FakeLayer({n: "esriFieldTypeString" for n in DEFAULT_REVIEW_FIELDS.values()}, "esriGeometryPolygon"))
    reviews.add({"REVIEW_ID": "REV-1", "IMAGE_ID": "IMG-42", "RATING": "使えない", "NAMED_AREA_ID": "NA-1", "NAMED_AREA_NAME": "東京湾",
                 "REASONS": "雲"}, {"rings": TOKYO})
    (tmp_path / "config.json").write_text(json.dumps({**CONFIG, "imageReviews": reviews_url}, ensure_ascii=False), encoding="utf-8")
    p.context.reload()
    today = jst_day(time.time() * 1000)
    written(history, today.isoformat())
    for r in history.rows:
        if r["attributes"]["REQ_ID"] == "R-1":
            r["attributes"].update(CAPTURED="撮像済み", IMAGE_ID="IMG-42", ADOPTION="採用")
    dock = p.api.schedule
    dock.show()
    assert wait_for(lambda: dock._loaded and not dock.busy), dock.status.text()
    dock.state_filter.choose([])
    tokyo = next(i for i, r in enumerate(dock.shown) if r.name == "東京湾")
    taken = dock.model.item(tokyo, COLUMNS.index("撮像"))
    assert taken.text() == "撮像済み（要確認）" and "画像の評価: 使えない（雲）" in taken.toolTip()
    assert dock.model.item(tokyo, COLUMNS.index("理由")).text().startswith("画像の評価: 使えない（雲）")
    assert dock.shown[tokyo].captured == "撮像済み"  # not changed
    osaka = next(i for i, r in enumerate(dock.shown) if r.name == "大阪湾")
    assert dock.model.item(osaka, COLUMNS.index("撮像")).text() == "未判定"
    dock.close_all()
