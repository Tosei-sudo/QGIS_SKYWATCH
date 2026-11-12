"""
定期照合 (core/request/follow_job.py, scheduled_follow.py): the follow-up of
the history run without QGIS (tsudo, 2026-11-11: 「定期的に照合したいので、
QGISに依存せず、照合を実行するPythonツールを入れてほしい」), through urllib,
one run at a time, its log and audit lines; and the times of a request taken
several times a day settled one by one by runs a few minutes apart.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
import time
from datetime import timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs

import pytest

from mikage.core import audit, edit_stamp
from mikage.core.config import parse_config
from mikage.core.esri import EsriClient, EsriError, UnreachableError
from mikage.core.plan.names import NO_NAMES
from mikage.core.request import follow_job
from mikage.core.request.follow import with_settled_times
from mikage.core.request.follow_job import (
    RunAudit, RunLock, SignIn, due_rows, follow_store, password_of, requests_of, server_root, url_transport,
)
from mikage.core.request.model import ImagingRequest, day_bounds, jst_day
from mikage.core.request.store import history_where, load_history, load_requests, save_request
from mikage.tests.fake_esri import FakeEsri, FakeLayer
from mikage.tests.test_request import TOKYO, ledger_layers

H = 3600000
D = 86400000
ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
SCRIPT = os.path.join(ROOT, "mikage", "scheduled_follow.py")
#: Yesterday (JST), so that its day has begun whenever the tests run.
DAY = jst_day(time.time() * 1000) - timedelta(days=1)
FIRST, LAST = day_bounds(DAY)
IMAGE_RING = [[[139.7, 35.4], [139.7, 35.6], [140.0, 35.6], [140.0, 35.4], [139.7, 35.4]]]
CATALOG_FIELDS = {"IMAGE_ID": "esriFieldTypeString", "ACQ": "esriFieldTypeDate", "SENSOR": "esriFieldTypeString", "URL": "esriFieldTypeString"}


def ask(rid: str, **kw) -> ImagingRequest:
    base = dict(id=rid, name=rid, category="臨時", achieve="期間内1回", start=FIRST - 5 * D, end=LAST + 10 * D, priority=500,
                kind="光学", rings=TOKYO, created=FIRST - 30 * D)
    base.update(kw)
    return ImagingRequest(**base)


def services(base: str) -> tuple[FakeEsri, dict]:
    """The ledger, its history and an image catalog under `base`, and a settings file naming them."""
    ledger, history, catalog = f"{base}/rest/services/R/FeatureServer/0", f"{base}/rest/services/R/FeatureServer/1", f"{base}/rest/services/I/FeatureServer/0"
    fake = ledger_layers(FakeEsri(), ledger, history)
    fake.add_layer(catalog, FakeLayer(CATALOG_FIELDS, "esriGeometryPolygon"))
    config = {
        "imageCatalogs": [{"label": "画像", "url": catalog, "fields": {"id": "IMAGE_ID", "acquired": "ACQ", "sensor": "SENSOR", "source": "URL"}}],
        "requestStores": [{"label": "依頼", "url": ledger, "history": history, "catalog": "画像"}],
    }
    return fake, config


def image(fake: FakeEsri, config: dict, image_id: str, at: float) -> None:
    fake.layers[config["imageCatalogs"][0]["url"]].add({"IMAGE_ID": image_id, "ACQ": at, "SENSOR": "OPT-X 1"},
                                                       {"rings": IMAGE_RING, "spatialReference": {"wkid": 4326}})


def handed_out(fake: FakeEsri, config: dict, rid: str, occurrence: int = 1, **kw) -> None:
    fake.layers[config["requestStores"][0]["history"]].add({"REQ_ID": rid, "TARGET_DAY": DAY.isoformat(), "RESULT": "採用", "CAPTURED": "未判定",
                                                           "OCCURRENCE": occurrence, **kw})


# ---------------------------------------------------------------- a local server

class _Handler(BaseHTTPRequestHandler):
    fake: FakeEsri
    base: str

    def log_message(self, *args):
        pass

    def _send(self, status: int, body: bytes, headers: dict = None) -> None:
        self.send_response(status)
        for k, v in (headers or {}).items():
            self.send_header(k, v)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):  # noqa: N802
        if self.path == "/config.json":
            self._send(200, json.dumps(self.server.config).encode("utf-8"))
        else:
            self._send(404, b"")

    def do_POST(self):  # noqa: N802
        length = int(self.headers.get("Content-Length") or 0)
        form = {k: v[0] for k, v in parse_qs(self.rfile.read(length).decode("utf-8"), keep_blank_values=True).items()}
        self.server.forms.append((self.path, form))
        if self.path.startswith("/broken"):
            return self._send(503, b"down")
        if self.path == "/negotiate":
            return self._send(401, b"", {"WWW-Authenticate": "Negotiate"})
        if self.path == "/moved":
            return self._send(302, b"", {"Location": "/elsewhere"})
        if self.path == "/slow":
            time.sleep(1.5)
            return self._send(200, b"{}")
        try:
            body = self.server.fake(self.server.base + self.path, form)
        except EsriError:
            return self._send(404, b"")
        self._send(200, body, {"Content-Type": "application/json; charset=utf-8"})


@pytest.fixture
def site():
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    httpd.base = f"http://127.0.0.1:{httpd.server_address[1]}"
    httpd.fake, httpd.config = services(httpd.base)
    httpd.forms = []
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    yield httpd
    httpd.shutdown()
    httpd.server_close()


@pytest.fixture(autouse=True)
def _clean_globals():
    yield
    audit.set_log(None)
    edit_stamp.set_tracking(None)


# ---------------------------------------------------------------- the whole run

def test_a_run_follows_up_saves_logs_and_audits(site, tmp_path):
    fake, config = site.fake, site.config
    share = tmp_path / "share"
    config["auditLog"] = {"folder": str(share)}
    store = parse_config(config)[0].request_stores[0]
    save_request(EsriClient(fake), store, None, ask("R-1", boost=200), FIRST - D)
    handed_out(fake, config, "R-1")
    image(fake, config, "IMG-1", FIRST + 10 * H)
    path = tmp_path / "config.json"
    path.write_text(json.dumps(config, ensure_ascii=False), encoding="utf-8")
    state = tmp_path / "state"

    assert follow_job.main(["--config", str(path), "--folder", str(state), "--quiet", "--editor", "定期照合"]) == 0
    client = EsriClient(fake)
    row = load_history(client, store, None)[0]
    assert (row.captured, row.image_id, row.adoption) == ("撮像済み", "IMG-1", "採用")
    saved = load_requests(client, store, None)[0]
    assert (saved.boost, saved.status) == (0, "完了")
    logs = list(state.glob("follow-*.log"))
    text = logs[0].read_text(encoding="utf-8")
    assert "依頼: 1 行を照合、撮像済み 1 件・未撮像 0 件を確認（台帳を更新）" in text
    # Its edits in the audit log: sent to the folder (a chain the checker accepts), under the name given.
    sent = list(share.rglob("mikage-audit_*"))
    assert len(sent) == 1
    lines = [json.loads(t) for t in sent[0].read_text(encoding="utf-8").splitlines()]
    assert [r["kind"] for r in lines][0] == "session" and lines[-1]["action"] == "end"
    assert {r["kind"] for r in lines} == {"session", "edit"} and {r["user"] for r in lines} == {"定期照合"}
    assert audit.main([str(share)]) == 0
    # Never the whole ledger: only the requests of the rows followed up.
    ledger_queries = [f["where"] for p, f in site.forms if p.endswith("/R/FeatureServer/0/query") and "where" in f]
    assert ledger_queries and all("IN ('R-1')" in w for w in ledger_queries)

    # The next run: nothing open, no catalog asked, no audit line, nothing sent.
    site.forms.clear()
    local = sorted((state / "audit").glob("audit-*.jsonl"))
    before = local[0].read_text(encoding="utf-8")
    assert follow_job.main(["--config", str(path), "--folder", str(state), "--quiet"]) == 0
    assert "依頼: 照合する行なし" in logs[0].read_text(encoding="utf-8")
    assert not any("/I/FeatureServer/0/query" in p for p, _ in site.forms)
    assert local[0].read_text(encoding="utf-8") == before
    assert len(list(share.rglob("mikage-audit_*"))) == 1


def test_settings_from_a_url_and_what_goes_wrong(site, tmp_path):
    state = str(tmp_path / "state")
    assert follow_job.main(["--config", f"{site.base}/config.json", "--folder", state, "--quiet"]) == 0
    # The options or the settings wrong: 2; nothing to follow up is no error.
    assert follow_job.main(["--config", str(tmp_path / "none.json"), "--folder", state, "--quiet"]) == 2
    assert follow_job.main(["--config", f"{site.base}/config.json", "--folder", state, "--quiet", "--store", "ない台帳"]) == 2
    empty = tmp_path / "empty.json"
    empty.write_text("{}", encoding="utf-8")
    assert follow_job.main(["--config", str(empty), "--folder", state, "--quiet"]) == 2
    assert follow_job.main(["--config", str(empty), "--folder", state, "--quiet", "--time-limit", "0"]) == 2
    # A ledger that cannot be read: 1, said in the log (the others are still followed up).
    broken = dict(site.config, requestStores=[*site.config["requestStores"],
                                              {"label": "落ちた台帳", "url": f"{site.base}/broken/rest/services/R/FeatureServer/0",
                                               "history": f"{site.base}/broken/rest/services/R/FeatureServer/1"}])
    path = tmp_path / "broken.json"
    path.write_text(json.dumps(broken, ensure_ascii=False), encoding="utf-8")
    assert follow_job.main(["--config", str(path), "--folder", state, "--quiet"]) == 1
    text = next((tmp_path / "state").glob("follow-*.log")).read_text(encoding="utf-8")
    assert "依頼: 照合する行なし" in text and "失敗: 落ちた台帳: サーバーのエラー" in text
    assert "--user のパスワードがない" not in text
    # A user without a password: 2, before anything is asked.
    assert follow_job.main(["--config", str(path), "--folder", state, "--quiet", "--user", "svc"]) == 2


def test_one_run_at_a_time(tmp_path):
    held = RunLock(str(tmp_path / "state" / follow_job.LOCK_NAME))
    assert held.acquire()
    try:
        assert not RunLock(held.path).acquire()
        assert follow_job.main(["--config", str(tmp_path / "none.json"), "--folder", str(tmp_path / "state"), "--quiet"]) == 0
        assert "まだ動いている" in next((tmp_path / "state").glob("follow-*.log")).read_text(encoding="utf-8")
    finally:
        held.release()
    again = RunLock(held.path)
    assert again.acquire()
    again.release()


def test_old_logs_are_forgotten(tmp_path):
    state = tmp_path / "state"
    state.mkdir()
    (state / "follow-20000101.log").write_text("old", encoding="utf-8")
    (state / "keep.txt").write_text("other", encoding="utf-8")
    follow_job.main(["--config", str(tmp_path / "none.json"), "--folder", str(state), "--quiet"])
    assert not (state / "follow-20000101.log").exists() and (state / "keep.txt").exists()


# ---------------------------------------------------------------- requests through urllib

def test_urllib_transport_says_what_went_wrong(site):
    send = url_transport()
    with pytest.raises(UnreachableError, match="サーバーのエラー.*HTTP 503"):
        send(f"{site.base}/broken", {"f": "json"})
    with pytest.raises(EsriError, match="統合 Windows 認証") as refused:
        send(f"{site.base}/negotiate", {"f": "json"})
    assert not isinstance(refused.value, UnreachableError)
    # A POST sent elsewhere would lose its form: refused, with where to.
    with pytest.raises(EsriError, match="転送された.*elsewhere"):
        send(f"{site.base}/moved", {"f": "json", "token": "secret"})
    with pytest.raises(UnreachableError, match="タイムアウト"):
        url_transport(timeout=0.3)(f"{site.base}/slow", {"f": "json"})
    with pytest.raises(UnreachableError, match="接続不可"):
        send("http://127.0.0.1:9/nothing", {"f": "json"})
    # The form as the plugin sends it; the token never in an error.
    client = EsriClient(send)
    url = site.config["requestStores"][0]["url"]
    assert client.layer(url, "tok").object_id_field == "OBJECTID"
    assert site.forms[-1][1] == {"f": "json", "token": "tok"}
    with pytest.raises(UnreachableError) as error:
        client.ask(f"{site.base}/broken", {}, "secret-token")
    assert "secret-token" not in str(error.value)


def test_server_root():
    assert server_root("https://gis.example/arcgis/rest/services/R/FeatureServer/0") == "https://gis.example/arcgis"
    assert server_root("https://gis.example/server/rest/services") == "https://gis.example/server"
    assert server_root("https://gis.example/other/path") == "https://gis.example"


# ---------------------------------------------------------------- sign-in

def _signing_server(token_url: str = "https://gis.example/portal/sharing/rest/generateToken", token_based=True):
    fake, config = services("https://gis.example/arcgis")
    for layer in fake.layers.values():
        layer.token = "T1"
    issued = []

    def send(url, form):
        if url == "https://gis.example/arcgis/rest/info":
            auth = {"isTokenBasedSecurity": token_based, **({"tokenServicesUrl": token_url} if token_url else {})}
            return json.dumps({"authInfo": auth}).encode()
        if url == token_url:
            issued.append(dict(form))
            if form.get("password") != "pw":
                return json.dumps({"error": {"code": 400, "message": "Unable to generate token.", "details": ["Invalid username or password."]}}).encode()
            return json.dumps({"token": "T%d" % len(issued), "expires": 0}).encode()
        return fake(url, form)

    return fake, config, send, issued


def test_tokens_from_generate_token():
    fake, config, send, issued = _signing_server()
    sign_in = SignIn(send, "svc", "pw")
    url = config["requestStores"][0]["url"]
    assert sign_in.token(url) == "T1" and sign_in.token(config["imageCatalogs"][0]["url"]) == "T1"
    assert len(issued) == 1 and issued[0]["username"] == "svc" and issued[0]["client"] == "requestip"
    client = EsriClient(send, renew=sign_in.renew)
    assert client.layer(url, sign_in.token(url)).object_id_field == "OBJECTID"
    # The server says the token is no longer good: a new one, asked once.
    for layer in fake.layers.values():
        layer.token = "T2"
    assert client.ask(f"{url}/query", {"where": "1=1", "returnCountOnly": "true"}, "T1") == {"count": 0}
    assert len(issued) == 2
    # A wrong password: the server's reason, never the password.
    with pytest.raises(EsriError, match="Invalid username or password") as error:
        SignIn(send, "svc", "wrong").token(url)
    assert "wrong" not in str(error.value)


def test_the_password_goes_only_over_https():
    _, config, send, issued = _signing_server(token_url="http://gis.example/portal/sharing/rest/generateToken")
    with pytest.raises(EsriError, match="https でない"):
        SignIn(send, "svc", "pw").token(config["requestStores"][0]["url"])
    assert issued == []
    # A server that does not sign in with tokens: none (the settings file's token, if any, is used).
    _, config, send, issued = _signing_server(token_url="", token_based=False)
    assert SignIn(send, "svc", "pw").token(config["requestStores"][0]["url"]) is None
    _, config, send, _ = _signing_server(token_url="")
    with pytest.raises(EsriError, match="tokenServicesUrl"):
        SignIn(send, "svc", "pw").token(config["requestStores"][0]["url"])


def test_signed_in_run_uses_the_server_token(tmp_path, monkeypatch):
    fake, config, send, issued = _signing_server()
    store = parse_config(config)[0].request_stores[0]
    for layer in fake.layers.values():
        layer.token = None
    save_request(EsriClient(fake), store, None, ask("R-1"), FIRST - D)
    handed_out(fake, config, "R-1")
    image(fake, config, "IMG-1", FIRST + 10 * H)
    for layer in fake.layers.values():
        layer.token = "T1"
    monkeypatch.setattr(follow_job, "url_transport", lambda context=None: send)
    monkeypatch.setenv("MIKAGE_PASSWORD", "pw")
    path = tmp_path / "config.json"
    path.write_text(json.dumps(config, ensure_ascii=False), encoding="utf-8")
    assert follow_job.main(["--config", str(path), "--folder", str(tmp_path / "state"), "--quiet", "--user", "svc"]) == 0
    assert len(issued) == 1
    for layer in fake.layers.values():
        layer.token = None
    assert load_history(EsriClient(fake), store, None)[0].captured == "撮像済み"


def test_passwords_of_credentials():
    assert password_of("pä ss".encode("utf-16-le")) == "pä ss"
    assert password_of("パスワード".encode("utf-16-le")) == "パスワード"
    assert password_of("pass".encode("utf-8")) == "pass"
    assert password_of("päss1".encode("utf-8")) == "päss1"
    if os.name != "nt":
        with pytest.raises(follow_job.UsageError, match="Windows でのみ"):
            follow_job.read_windows_credential("MIKAGE")


# ---------------------------------------------------------------- what is read

def test_only_the_rows_due_and_their_requests():
    fake, config = services("https://gis.example/arcgis")
    store = parse_config(config)[0].request_stores[0]
    client = EsriClient(fake)
    for i in range(5):
        save_request(client, store, None, ask(f"R-{i}"), FIRST - D)
    handed_out(fake, config, "R-1")
    handed_out(fake, config, "R-2", RESULT="見送り")
    handed_out(fake, config, "R-3", TARGET_DAY=(DAY + timedelta(days=3)).isoformat())
    history = load_history(client, store, None, history_where(store, open_rows=True))
    assert [r.request_id for r in due_rows(history, time.time() * 1000)] == ["R-1"]
    follow_job.IDS_PER_QUERY, keep = 2, follow_job.IDS_PER_QUERY
    try:
        assert sorted(requests_of(client, store, None, ["R-0", "R-1", "R-4", "R-1", "NONE"])) == ["R-0", "R-1", "R-4"]
    finally:
        follow_job.IDS_PER_QUERY = keep


def test_times_of_a_day_settled_run_by_run():
    """A request taken twice a day: each time its own image, though the runs settle them one by one."""
    fake, config = services("https://gis.example/arcgis")
    parsed = parse_config(config)[0]
    store = parsed.request_stores[0]
    client = EsriClient(fake)
    save_request(client, store, None, ask("R-1", times_min=2, boost=200), FIRST - D)
    handed_out(fake, config, "R-1", 1)
    handed_out(fake, config, "R-1", 2)
    image(fake, config, "IMG-1", FIRST + 10 * H)
    now = time.time() * 1000

    def run():
        return follow_store(client, parsed, store, lambda url, token: token, lambda: NO_NAMES, now)

    assert run()[0] == "2 行を照合、撮像済み 1 件・未撮像 0 件を確認"
    rows = {r.occurrence: r for r in load_history(client, store, None)}
    assert (rows[1].captured, rows[1].image_id, rows[2].captured) == ("撮像済み", "IMG-1", "未判定")
    # The next run reads only the open time: the image of the first is not taken again by the second.
    assert run()[0] == "1 行を照合、新たに判定できた行なし"
    assert {r.occurrence: r.captured for r in load_history(client, store, None)} == {1: "撮像済み", 2: "未判定"}
    # (Which it was before: the open rows alone.)
    open_rows = load_history(client, store, None, history_where(store, open_rows=True))
    by_id = {r.id: r for r in load_requests(client, store, None)}
    assert [r.occurrence for r in with_settled_times(client, store, None, open_rows, by_id)] == [2, 1]
    # A second image hours later: the second time, and the day counts (2 of 2): raise undone, 完了.
    image(fake, config, "IMG-2", FIRST + 14 * H)
    assert run()[0] == "1 行を照合、撮像済み 1 件・未撮像 0 件を確認（台帳を更新）"
    rows = {r.occurrence: r for r in load_history(client, store, None)}
    assert (rows[2].captured, rows[2].image_id) == ("撮像済み", "IMG-2")
    saved = load_requests(client, store, None)[0]
    assert (saved.boost, saved.status) == (0, "完了")


def test_rows_of_other_requests_are_not_added():
    fake, config = services("https://gis.example/arcgis")
    store = parse_config(config)[0].request_stores[0]
    client = EsriClient(fake)
    handed_out(fake, config, "R-1", CAPTURED="撮像済み", IMAGE_ID="IMG-9")
    handed_out(fake, config, "R-2", CAPTURED="撮像済み", IMAGE_ID="IMG-9")
    handed_out(fake, config, "R-2", 2)
    open_rows = load_history(client, store, None, history_where(store, open_rows=True))
    rows = with_settled_times(client, store, None, open_rows, {})
    assert [(r.request_id, r.occurrence, r.captured) for r in rows] == [("R-2", 2, "未判定"), ("R-2", 1, "撮像済み")]
    # Once a day: nothing more is read.
    once = load_history(client, store, None, history_where(store, open_rows=True, request_ids=["R-1"]))
    asked = len(fake.asked)
    assert with_settled_times(client, store, None, once, {"R-1": ask("R-1")}) == once and len(fake.asked) == asked


# ---------------------------------------------------------------- the audit log of a run

def test_audit_lines_only_when_something_changed(tmp_path):
    settings = audit.audit_log_of({"folder": str(tmp_path / "share"), "sendMinutes": 10}, [])
    quiet = RunAudit(settings, str(tmp_path))
    quiet.finish()
    assert not list((tmp_path / "audit").glob("audit-*"))
    busy = RunAudit(settings, str(tmp_path))
    audit.esri_request("https://gis.example/arcgis/rest/services/R/FeatureServer/1/applyEdits", {"updates": "[]"}, {"updateResults": []})
    busy.finish(now=time.time())
    assert len(list((tmp_path / "share").rglob("mikage-audit_*"))) == 1
    # Sent at most every sendMinutes: the next run's lines wait here.
    later = RunAudit(settings, str(tmp_path))
    assert not later.due()
    assert later.due(now=time.time() + 11 * 60)
    audit.esri_request("https://gis.example/arcgis/rest/services/R/FeatureServer/1/applyEdits", {"updates": "[]"}, {"updateResults": []})
    later.finish()
    assert len(list((tmp_path / "share").rglob("mikage-audit_*"))) == 1
    assert later.log.unsent() > 0
    assert audit.current() is None
    # A later run that changes nothing still sends them once due.
    RunAudit(settings, str(tmp_path)).finish(now=time.time() + 11 * 60)
    assert len(list((tmp_path / "share").rglob("mikage-audit_*"))) == 2 and later.log.unsent() == 0
    # The plugin's own folder on this PC (localFolder) is not the tool's.
    own = RunAudit(audit.audit_log_of({"folder": str(tmp_path / "share"), "localFolder": str(tmp_path / "plugin")}, []), str(tmp_path))
    assert own.log.folder == str(tmp_path / "audit")
    own.finish()
    # No folder to send to, or the log turned off: nothing is sent.
    assert not RunAudit(audit.audit_log_of({}, []), str(tmp_path)).due()
    audit.set_log(None)


# ---------------------------------------------------------------- the script

def test_the_script_runs_without_qgis(tmp_path):
    env = {k: v for k, v in os.environ.items() if k != "PYTHONPATH"}
    done = subprocess.run([sys.executable, SCRIPT, "--help"], capture_output=True, text=True, encoding="utf-8", env=env, timeout=60)
    assert done.returncode == 0 and "--config" in done.stdout
    check = ("import sys; sys.path.insert(0, sys.argv[1]); import mikage.core.request.follow_job; "
             "print(sorted({m.split('.')[0] for m in sys.modules} & {'qgis', 'PyQt5', 'PyQt6', 'osgeo'}))")
    done = subprocess.run([sys.executable, "-c", check, ROOT], capture_output=True, text=True, encoding="utf-8", env=env, timeout=60)
    assert done.returncode == 0 and done.stdout.strip() == "[]", done.stderr
    # From another folder, with the settings file missing: the reason in the log, 2.
    done = subprocess.run([sys.executable, SCRIPT, "--config", str(tmp_path / "none.json"), "--folder", str(tmp_path / "state")],
                          capture_output=True, text=True, encoding="utf-8", env=env, timeout=60, cwd=str(tmp_path))
    assert done.returncode == 2 and "設定ファイルがない" in done.stderr
