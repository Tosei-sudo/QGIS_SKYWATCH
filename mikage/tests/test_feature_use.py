"""機能の使われ方 (core/feature_use.py): counting, this PC's file, the table (over the Esri stand-in) and the summary."""
from __future__ import annotations

import json
import threading
import urllib.request
from datetime import date, timedelta
from http.server import ThreadingHTTPServer
from urllib.error import URLError

import pytest

from mikage.core import feature_use as fu
from mikage.core.config import parse_config
from mikage.core.esri import EsriClient, EsriError, form_body
from mikage.core.sources import EsriSource
from mikage.core.training import sample
from mikage.core.training import server as mock_esri

URL = "https://gis.example/arcgis/rest/services/Usage/FeatureServer/0"


def transport(url: str, form: dict[str, str]) -> bytes:
    request = urllib.request.Request(url, data=form_body(form), headers={"Content-Type": "application/x-www-form-urlencoded"})
    try:
        with urllib.request.urlopen(request, timeout=10) as answer:
            return answer.read()
    except URLError as error:
        raise EsriError(str(error)) from None


@pytest.fixture
def usage(tmp_path):
    """The stand-in with only the usage table (3 rows a page, to page the sums); (its URL, store)."""
    store = mock_esri.Store(str(tmp_path / "usage.sqlite"))
    mock_esri.load_services(store, {"services": [{"name": "Usage", "layers": [
        {"id": 0, "name": "機能の使われ方", "fields": sample.FEATURE_USE_FIELDS, "maxRecordCount": 3, "features": []}]}]})
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), mock_esri.make_handler(mock_esri.Service(store, log=lambda *_: None), str(tmp_path), quiet=True))
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{httpd.server_address[1]}/arcgis/rest/services/Usage/FeatureServer/0", store
    httpd.shutdown()
    httpd.server_close()


def table_rows(store) -> list[dict]:
    layer = store.layer("Usage", 0)
    return [dict(r) for r in store.select(layer)]


# ---- the settings ------------------------------------------------------------------------------------------


def test_settings_absent_false_url_and_object():
    config, problems = parse_config({})
    assert problems == [] and config.feature_use.enabled and config.feature_use.source is None
    config, _ = parse_config({"featureUse": False})
    assert not config.feature_use.enabled
    config, problems = parse_config({"featureUse": URL})
    assert problems == [] and config.feature_use.source.url == URL and config.feature_use.source.fields == fu.DEFAULT_FIELDS
    config, problems = parse_config({"featureUse": {"url": URL, "fields": {"user": "WHO"}, "sendMinutes": 30}})
    assert problems == [] and config.feature_use.source.fields["user"] == "WHO" and config.feature_use.send_minutes == 30
    _, problems = parse_config({"featureUse": {"url": URL, "sendMinutes": 1, "folder": "x"}})
    assert any("sendMinutes" in p for p in problems) and any("folder" in p for p in problems)
    _, problems = parse_config({"featureUse": "https://gis.example/arcgis/rest/services/U/MapServer/0"})
    assert any("featureUse" in p for p in problems)
    _, problems = parse_config({"featureUse": 3})
    assert any("featureUse" in p for p in problems)


# ---- counting ----------------------------------------------------------------------------------------------


def test_labels_lose_counts_digits_and_mnemonics():
    assert fu.label_of("通知（3）") == "通知"
    assert fu.label_of("記録 (12件)") == "記録"
    assert fu.label_of("送信待ち 4 件") == "送信待ち # 件"
    assert fu.label_of("&Save && close") == "Save & close"
    # A tooltip: up to where its explanation starts.
    assert fu.tip_label("依頼グループで絞り込み（チェックしたグループの依頼だけ）") == "依頼グループで絞り込み"
    assert fu.tip_label("画像を選ぶ。次に…\n2 行目") == "画像を選ぶ"
    assert fu.tip_label("（説明だけ）") == "（説明だけ）"
    assert fu.label_of("画像を開く\n説明の 2 行目") == "画像を開く"
    assert fu.label_of("  ") == ""
    assert len(fu.label_of("あ" * 100)) == fu.MAX_LABEL


def test_the_counter_adds_by_day_roles_and_place_and_is_taken_whole():
    day = ["2026-11-28"]
    c = fu.Counter(roles="OBSERVER", version="0.4.0", clock=lambda: day[0])
    c.add("観測記録", "MENU")
    c.add("観測記録", "MENU")
    c.add("観測記録", "TOOLBAR")
    c.add("記録", "DOCK", dock="観測記録")
    c.add("観測記録", "SHOWN", count=0, minutes=1.5)
    c.roles = "OBSERVER,PLANNER"
    c.add("観測記録", "MENU")
    day[0] = "2026-11-29"
    c.add("観測記録", "MENU")
    c.paused = True
    c.add("観測記録", "MENU")
    counts = c.take()
    assert counts[("2026-11-28", "OBSERVER", "0.4.0", "", "観測記録", "MENU")] == [2, 0.0]
    assert counts[("2026-11-28", "OBSERVER", "0.4.0", "観測記録", "記録", "DOCK")] == [1, 0.0]
    assert counts[("2026-11-28", "OBSERVER", "0.4.0", "", "観測記録", "SHOWN")] == [0, 1.5]
    assert counts[("2026-11-28", "OBSERVER,PLANNER", "0.4.0", "", "観測記録", "MENU")] == [1, 0.0]
    assert counts[("2026-11-29", "OBSERVER,PLANNER", "0.4.0", "", "観測記録", "MENU")] == [1, 0.0]
    assert len(counts) == 6 and len(c) == 0 and c.take() == {}


def test_roles_text_and_role_filters():
    assert fu.roles_text(["PLANNER", "OBSERVER", "", "PLANNER"]) == "OBSERVER,PLANNER"
    assert fu.has_role("OBSERVER,PLANNER", "PLANNER") and not fu.has_role("OBSERVER", "PLANNER")
    assert fu.has_role("", fu.NO_ROLE) and not fu.has_role("ADMIN", fu.NO_ROLE) and fu.has_role("ADMIN", "")
    where = fu.role_where("ROLE_NAMES", "PLANNER")
    assert "ROLE_NAMES = 'PLANNER'" in where and "LIKE '%,PLANNER,%'" in where
    assert fu.role_where("ROLE_NAMES", fu.NO_ROLE) == "(ROLE_NAMES IS NULL OR ROLE_NAMES = '')"
    assert fu.role_where("ROLE_NAMES", "") == ""


def test_user_keys_are_random_and_say_nothing():
    keys = {fu.new_user_key() for _ in range(20)}
    assert len(keys) == 20 and all(len(k) == 16 and int(k, 16) >= 0 for k in keys)


# ---- this PC's file ----------------------------------------------------------------------------------------


def counts_of(*items) -> dict:
    return {tuple(i[:6]): [i[6], i[7]] for i in items}


def test_the_file_adds_up_puts_rows_into_batches_and_forgets_what_was_sent(tmp_path):
    store = fu.UseStore(str(tmp_path / "use"))
    a = ("2026-11-28", "OBSERVER", "0.4.0", "", "観測記録", "MENU", 2, 0.0)
    b = ("2026-11-28", "OBSERVER", "0.4.0", "観測記録", "記録", "DOCK", 1, 0.0)
    store.merge(counts_of(a, b), now="2026-11-28")
    store.merge(counts_of(a), now="2026-11-28")
    rows = {tuple(r[:6]): r[6] for r in store.rows()}
    assert rows[tuple(a[:6])] == 4 and rows[tuple(b[:6])] == 1
    batches = store.batches(size=1)
    assert len(batches) == 2 and len({x.id for x in batches}) == 2 and all(not x.tried for x in batches)
    # Counted meanwhile: pending again, beside the batches (which stay as they are).
    store.merge(counts_of(a), now="2026-11-28")
    assert store.waiting() == 3
    store.tried(batches[0].id)
    again = store.batches(size=1)
    assert [x.tried for x in again] == [True, False, False]
    store.sent(batches[0].id)
    store.sent(batches[1].id)
    assert store.waiting() == 1
    assert json.loads((tmp_path / "use" / fu.FILE).read_text(encoding="utf-8"))["pending"] == []


def test_old_days_are_let_go_and_a_broken_file_is_put_aside(tmp_path):
    store = fu.UseStore(str(tmp_path))
    old = (date(2026, 11, 28) - timedelta(days=fu.KEEP_DAYS + 1)).isoformat()
    store.merge(counts_of((old, "", "", "", "古い", "MENU", 1, 0.0), ("2026-11-28", "", "", "", "新しい", "MENU", 1, 0.0)), now="2026-11-28")
    assert [r[4] for r in store.rows()] == ["新しい"]
    (tmp_path / fu.FILE).write_text("{ cut", encoding="utf-8")
    assert store.rows() == []
    store.merge(counts_of(("2026-11-28", "", "", "", "後", "MENU", 1, 0.0)), now="2026-11-28")
    assert [r[4] for r in store.rows()] == ["後"] and (tmp_path / (fu.FILE + ".broken")).exists()


# ---- the table ---------------------------------------------------------------------------------------------


def test_rows_are_sent_once_each_with_its_batch_id_and_no_name(usage, tmp_path):
    url, server_store = usage
    client = EsriClient(transport)
    source = EsriSource(url=url, fields=dict(fu.DEFAULT_FIELDS))
    store = fu.UseStore(str(tmp_path / "use"))
    store.merge(counts_of(("2026-11-28", "OBSERVER", "0.4.0", "", "観測記録", "MENU", 3, 0.0),
                          ("2026-11-28", "OBSERVER", "0.4.0", "観測記録", "記録", "DOCK", 2, 0.0),
                          ("2026-11-28", "OBSERVER", "0.4.0", "", "観測記録", "SHOWN", 1, 12.345)), now="2026-11-28")
    assert fu.send(client, source, store, "abcd1234abcd1234") == 3
    rows = table_rows(server_store)
    assert len(rows) == 3 and store.waiting() == 0
    assert {r["USER_KEY"] for r in rows} == {"abcd1234abcd1234"} and len({r["SEND_ID"] for r in rows}) == 1
    menu = next(r for r in rows if r["PLACE_KIND"] == "MENU")
    assert (menu["FEATURE_NAME"], menu["USE_COUNT"], menu["ROLE_NAMES"], menu["PLUGIN_VERSION"]) == ("観測記録", 3, "OBSERVER", "0.4.0")
    assert menu["USE_DATE"] == fu.day_ms("2026-11-28") and menu["DOCK_NAME"] is None
    assert next(r for r in rows if r["PLACE_KIND"] == "SHOWN")["USE_MINUTES"] == pytest.approx(12.35)
    # Nothing new: nothing sent.
    assert fu.send(client, source, store, "abcd1234abcd1234") == 0
    # A batch sent once without an answer, that had arrived: not sent again.
    store.merge(counts_of(("2026-11-28", "", "0.4.0", "", "画像カタログ", "KEY", 1, 0.0)), now="2026-11-28")
    batch = store.batches()[0]
    fu.send(client, source, store, "abcd1234abcd1234")
    with store.lock:
        data = store._read()
        data["batches"] = [{"id": batch.id, "rows": batch.rows, "tried": True}]
        store._write(data)
    assert fu.send(client, source, store, "abcd1234abcd1234") == 0
    assert len(table_rows(server_store)) == 4 and store.waiting() == 0


def test_a_table_without_the_needed_attributes_is_not_written(usage, tmp_path):
    url, _store = usage
    source = EsriSource(url=url, fields={**fu.DEFAULT_FIELDS, "count": "HOW_MANY"})
    store = fu.UseStore(str(tmp_path))
    store.merge(counts_of(("2026-11-28", "", "", "", "観測記録", "MENU", 1, 0.0)), now="2026-11-28")
    with pytest.raises(EsriError) as caught:
        fu.send(EsriClient(transport), source, store, "k")
    assert caught.value.code == "E704" and "HOW_MANY" in str(caught.value)
    assert store.waiting() == 1


def test_the_sums_by_feature_and_person_over_pages_and_by_role(usage, tmp_path):
    url, _server = usage
    client = EsriClient(transport)
    source = EsriSource(url=url, fields=dict(fu.DEFAULT_FIELDS))
    for key, roles, items in (("a" * 16, "OBSERVER", [("観測記録", "MENU", 3), ("観測記録", "TOOLBAR", 1), ("画像カタログ", "MENU", 1)]),
                              ("b" * 16, "PLANNER", [("撮像計画", "MENU", 2), ("観測記録", "KEY", 1)]),
                              ("c" * 16, "", [(fu.START_FEATURE, fu.START, 1)])):
        store = fu.UseStore(str(tmp_path / key))
        store.merge(counts_of(*[("2026-11-27", roles, "0.4.0", "", f, p, n, 0.0) for f, p, n in items],
                              ("2026-10-01", roles, "0.4.0", "", "古い日", "MENU", 5, 0.0)), now="2026-11-28")
        fu.send(client, source, store, key)
    groups, capped = fu.table_groups(client, source, "2026-11-20", "2026-11-28", page=2)
    assert not capped and len(groups) == 6  # 3 a page: paged
    summary = fu.summarize(groups, known=[("", "観測記録"), ("", "だれも使わない")])
    by = {line.feature: line for line in summary.lines}
    assert summary.people == 3
    assert (by["観測記録"].count, by["観測記録"].users, by["観測記録"].main_place) == (5, 2, "MENU")
    assert by["観測記録"].places == {"MENU": 3, "TOOLBAR": 1, "KEY": 1}
    assert by["だれも使わない"].users == 0 and summary.lines[0].feature == "だれも使わない"
    assert fu.START_FEATURE not in by and "古い日" not in by
    observers, _ = fu.table_groups(client, source, "2026-11-20", "2026-11-28", role="OBSERVER")
    assert {g[1] for g in observers} == {"観測記録", "画像カタログ"} and {g[3] for g in observers} == {"a" * 16}
    nobody, _ = fu.table_groups(client, source, "2026-11-20", "2026-11-28", role=fu.NO_ROLE)
    assert [g[1] for g in nobody] == [fu.START_FEATURE]


def test_this_pcs_rows_summed_like_the_table():
    rows = [["2026-11-27", "OBSERVER", "v", "", "観測記録", "MENU", 2, 0.0],
            ["2026-11-27", "OBSERVER", "v", "", "観測記録", "SHOWN", 1, 30.0],
            ["2026-11-28", "OBSERVER", "v", "", "観測記録", "MENU", 1, 0.0],
            ["2026-11-28", "", "v", "", "観測記録", "MENU", 7, 0.0],
            ["2026-11-01", "OBSERVER", "v", "", "観測記録", "MENU", 9, 0.0]]
    groups = fu.local_groups(rows, "me", "2026-11-20", "2026-11-28", role="OBSERVER")
    assert sorted(groups) == [("", "観測記録", "MENU", "me", 3, 0.0), ("", "観測記録", "SHOWN", "me", 1, 30.0)]
    summary = fu.summarize(groups)
    line = summary.lines[0]
    assert (line.count, line.shown, line.minutes, line.users, summary.people) == (3, 1, 30.0, 1, 1)
    text = fu.csv_text(summary)
    assert text.splitlines()[0].startswith("ドック,機能,回数,使った人") and ",観測記録,3,1,100%,メニュー,1,30" in text


def test_periods_and_days():
    assert fu.period(30, now="2026-11-28") == ("2026-10-30", "2026-11-28")
    assert fu.period(1, now="2026-11-28") == ("2026-11-28", "2026-11-28")
    assert fu.period(0, now="2026-11-28")[0] == "2000-01-01"
    assert fu.day_ms("2026-11-29") - fu.day_ms("2026-11-28") == 86_400_000
    source = EsriSource(url=URL, fields=dict(fu.DEFAULT_FIELDS), where="SITE = 'A'")
    where = fu.where_of(source, "2026-11-01", "2026-11-28", role="ADMIN")
    assert where.startswith("USE_DATE >= TIMESTAMP '") and "USE_DATE < TIMESTAMP '" in where
    assert "ROLE_NAMES = 'ADMIN'" in where and where.endswith("(SITE = 'A')")
