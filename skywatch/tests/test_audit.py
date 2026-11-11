"""core/audit.py: the audit log (監査ログ): lines of operations, their chain, and sending them to a folder."""
import json
import os
import threading
import time
from datetime import datetime

import pytest

from skywatch.core import audit
from skywatch.core.audit import AuditLog, AuditSettings, audit_log_of, verify
from skywatch.core.config import parse_config
from skywatch.core.esri import EsriClient, EsriError

URL = "https://gis.example/arcgis/rest/services/Obs/FeatureServer/0"
NOW = datetime(2026, 11, 11, 21, 30).timestamp()


def make_log(tmp_path, **settings) -> AuditLog:
    log = AuditLog(str(tmp_path / "here"), AuditSettings(**settings), session="s1", computer="PC-01", user="tsudo")
    audit.set_log(log)
    return log


def lines_of(path):
    with open(path, encoding="utf-8") as f:
        return [line for line in f.read().split("\n") if line]


def day_file(log, ext="jsonl"):
    [name] = [n for n in os.listdir(log.folder) if n.endswith("." + ext)]
    return os.path.join(log.folder, name)


def sent_lines(folder):
    return list(audit.read_folder(str(folder)))


# ---------------------------------------------------------------- settings

def test_the_settings_file():
    assert parse_config({})[0].audit_log == AuditSettings()
    assert AuditSettings().enabled and not AuditSettings().folder
    assert parse_config({"auditLog": False})[0].audit_log.enabled is False
    assert parse_config({"auditLog": r"\\server\audit"})[0].audit_log.folder == r"\\server\audit"
    config, problems = parse_config({"auditLog": {
        "folder": r"\\server\audit\{yyyy}{mm}", "localFolder": "D:/log", "format": "CSV", "encoding": "Shift_JIS",
        "sendMinutes": 5, "keepDays": 30, "events": ["edit", "export"], "values": True}})
    assert problems == []
    a = config.audit_log
    assert (a.folder, a.local_folder, a.format, a.encoding, a.send_minutes, a.keep_days, a.events, a.values) == (
        r"\\server\audit\{yyyy}{mm}", "D:/log", "csv", "cp932", 5, 30, ("edit", "export"), True)
    assert AuditSettings(format="csv").file_encoding == "utf-8-sig" and AuditSettings().file_encoding == "utf-8"


def test_settings_problems():
    problems = []
    a = audit_log_of({"folder": "x/{host}", "format": "xml", "encoding": "latin1", "sendMinutes": 0, "events": ["edit", "view"],
                      "values": "yes", "enabled": 1}, problems)
    assert a.format == "jsonl" and a.encoding == "" and a.send_minutes == 10 and a.events == ("edit",) and a.values is False
    assert len(problems) == 7, problems
    assert any("{host}" in p for p in problems) and any("view" in p for p in problems)
    problems = []
    assert audit_log_of(3, problems) == AuditSettings() and problems


# ---------------------------------------------------------------- lines

def test_lines_say_who_what_when_and_hide_secrets(tmp_path):
    log = make_log(tmp_path)
    exported = tmp_path / "out.csv"
    exported.write_text("a,b\n1,2\n", encoding="utf-8")
    audit.record("open", "image", "https://user:pw@img.example/a.tif?token=SECRET123", count=1,
                 detail={"files": ["/vsicurl/https://img.example/a.tif?token=SECRET123"], "token": "SECRET123"})
    audit.record("export", "observations-csv", str(exported), count=2, file=str(exported))
    audit.record("copy", "alert-text", text="通報 1 件", error="failed: password=hunter2")
    log.write_queued()
    lines = lines_of(day_file(log))
    assert "SECRET123" not in "\n".join(lines) and "hunter2" not in "\n".join(lines) and "user:pw" not in "\n".join(lines)
    first, second, third = (json.loads(x) for x in lines)
    assert first["kind"] == "open" and first["target"] == "https://img.example/a.tif"
    assert first["user"] == "tsudo" and first["computer"] == "PC-01" and first["session"] == "s1" and first["seq"] == 1
    assert first["time"].startswith(datetime.now().strftime("%Y-%m-%d"))
    assert first["detail"] == {"files": ["/vsicurl/https://img.example/a.tif?token=…"]}
    assert second["detail"]["bytes"] == 8 and len(second["detail"]["sha256"]) == 64 and second["count"] == 2
    assert third["detail"]["chars"] == 6 and third["error"] == "failed: password=…"
    # The chain: each line names the SHA-256 of the one before.
    assert "prev" not in first and second["prev"] == audit.sha256_text(lines[0]) and third["prev"] == audit.sha256_text(lines[1])


def test_kinds_left_out_and_switched_off_are_not_written(tmp_path):
    log = make_log(tmp_path, events=("edit",))
    audit.record("open", "image", "a")
    audit.record("edit", "save", URL)
    log.write_queued()
    assert [json.loads(x)["kind"] for x in lines_of(day_file(log))] == ["edit"]
    log.configure(AuditSettings(enabled=False))
    audit.record("edit", "save", URL)
    assert log.queue.empty()


class Server:
    """A layer answering applyEdits / deleteFeatures (object ids from 100)."""

    def __init__(self, fail=None):
        self.fail = fail

    def __call__(self, url, form):
        if url.endswith("/0"):
            return json.dumps({"name": "TBL_OBSERVATION", "fields": [{"name": "OBJECTID", "type": "esriFieldTypeOID"}]}).encode()
        if self.fail:
            raise EsriError(self.fail)
        if url.endswith("/deleteFeatures"):
            return json.dumps({"deleteResults": [{"objectId": 7, "success": True}, {"objectId": 8, "success": True}]}).encode()
        adds = json.loads(form.get("adds") or "[]")
        return json.dumps({
            "addResults": [{"objectId": 100 + i, "success": True} for i, _ in enumerate(adds)],
            "updateResults": [{"objectId": f["attributes"]["OBJECTID"], "success": f["attributes"]["OBJECTID"] != 9,
                               **({} if f["attributes"]["OBJECTID"] != 9 else {"error": {"description": "Locked"}})}
                              for f in json.loads(form.get("updates") or "[]")],
            "deleteResults": [{"objectId": int(i), "success": True} for i in (form.get("deletes") or "").split(",") if i],
        }).encode()


def test_esri_edits_are_recorded_with_ids_and_results(tmp_path):
    log = make_log(tmp_path)
    client = EsriClient(Server())
    client.layer(URL)
    client.apply_edits(URL, adds=[{"attributes": {"NAME": "a"}}, {"attributes": {"NAME": "b"}}],
                       updates=[{"attributes": {"OBJECTID": 5}}, {"attributes": {"OBJECTID": 9}}], deletes=[3], token="TOKEN-X")
    client.ask(f"{URL}/deleteFeatures", {"where": "REQ_ID = 'R1'"}, "TOKEN-X")
    log.write_queued()
    text = open(day_file(log), encoding="utf-8").read()
    assert "TOKEN-X" not in text and "NAME" not in text  # no values unless asked
    add, update, delete, by_where = (json.loads(x) for x in lines_of(day_file(log)))
    assert (add["action"], add["target"], add["result"], add["count"], add["ids"]) == ("add", URL, "ok", 2, [100, 101])
    assert add["detail"] == {"layer": "TBL_OBSERVATION"}
    assert (update["result"], update["ids"], update["detail"]["failed"], update["error"]) == ("partial", [5], 1, "Locked")
    assert (delete["action"], delete["ids"]) == ("delete", [3])
    assert (by_where["action"], by_where["ids"], by_where["detail"]["where"]) == ("delete", [7, 8], "REQ_ID = 'R1'")


def test_failed_edits_and_values(tmp_path):
    log = make_log(tmp_path, values=True)
    with pytest.raises(EsriError):
        EsriClient(Server(fail="サービスからエラー応答: busy")).apply_edits(
            URL, adds=[{"attributes": {"NAME": "a"}, "geometry": {"x": 1}}], updates=[{"attributes": {"OBJECTID": 4, "NAME": "b"}}])
    log.write_queued()
    add, update = (json.loads(x) for x in lines_of(day_file(log)))
    assert (add["result"], add["count"], add["error"]) == ("error", 1, "サービスからエラー応答: busy")
    assert add["detail"]["values"][0]["NAME"] == "a" and "geometry" not in json.dumps(add["detail"])
    assert update["ids"] == [4] and update["detail"]["values"][0]["NAME"] == "b"


def test_csv_and_cp932(tmp_path):
    log = make_log(tmp_path, format="csv", encoding="cp932")
    audit.record("export", "observations-csv", "C:/out/記録.csv", count=3, detail={"note": "a—b"})
    audit.record("copy", "alert-text", text="x")
    log.write_queued()
    lines = lines_of(day_file(log, "csv"))
    assert lines[0] == ",".join(audit.COLUMNS) and len(lines) == 3
    assert "a?b" in lines[1]  # cp932 has no —: written as ? here too, so that the chain holds in the file sent
    sessions, bad = verify((f"x:{i}", line) for i, line in enumerate(lines))
    assert not bad and len(sessions) == 1 and sessions[0].lines == 2 and not sessions[0].problems


# ---------------------------------------------------------------- sending

def test_sends_only_what_is_new_as_new_files(tmp_path):
    out = tmp_path / "share"
    log = make_log(tmp_path, folder=str(out / "{computer}" / "{yyyy}{mm}"))
    audit.record("open", "image", "IMG1")
    log.write_queued()
    first = log.send(NOW)
    assert not first.error and len(first.files) == 1
    folder = out / "PC-01" / "202611"
    assert first.files[0].startswith(str(folder)) and os.path.basename(first.files[0]).startswith(f"skywatch-audit_PC-01_tsudo_{audit.day_of(time.time())}_20261111-213000")
    assert log.unsent() == 0 and log.status.last_sent == NOW
    assert log.send(NOW).files == []  # nothing new: nothing written
    audit.record("open", "image", "IMG2")
    audit.record("open", "image", "IMG3")
    log.write_queued()
    assert log.unsent() > 0
    second = log.send(NOW)  # the same second: never over the first file
    assert len(second.files) == 1 and second.files[0] != first.files[0] and second.files[0].endswith("_2.jsonl")
    assert [json.loads(t)["target"] for _, t in sent_lines(folder)] == ["IMG1", "IMG2", "IMG3"]
    sessions, bad = verify(sent_lines(folder))
    assert not bad and sessions[0].lines == 3 and not sessions[0].problems


def test_csv_parts_have_their_header_and_the_encoding(tmp_path):
    out = tmp_path / "share"
    log = make_log(tmp_path, folder=str(out), format="csv")
    audit.record("open", "image", "画像1")
    log.write_queued()
    log.send(NOW)
    audit.record("open", "image", "画像2")
    log.write_queued()
    log.send(NOW + 600)
    files = sorted(os.listdir(out))
    assert len(files) == 2
    for name in files:
        data = (out / name).read_bytes()
        assert data.startswith(b"\xef\xbb\xbf" + ",".join(audit.COLUMNS).encode())  # utf-8-sig for Excel, a header each
    sessions, _bad = verify(sent_lines(out))
    assert sessions[0].lines == 2 and not sessions[0].problems


def test_a_folder_that_cannot_be_written_keeps_the_lines(tmp_path):
    blocker = tmp_path / "not-a-folder"
    blocker.write_text("x")
    log = make_log(tmp_path, folder=str(blocker / "sub"))
    audit.record("open", "image", "IMG1")
    log.write_queued()
    result = log.send(NOW)
    assert result.error and log.status.send_error and log.status.failing_since == NOW
    assert log.unsent() > 0
    log.configure(AuditSettings(folder=str(tmp_path / "share")))
    assert not log.send(NOW + 60).error
    assert log.status.send_error == "" and log.status.failing_since is None and log.unsent() == 0
    assert [json.loads(t)["target"] for _, t in sent_lines(tmp_path / "share")] == ["IMG1"]


def test_another_qgis_sending_is_left_alone(tmp_path):
    log = make_log(tmp_path, folder=str(tmp_path / "share"))
    audit.record("open", "image", "IMG1")
    log.write_queued()
    lock = os.path.join(log.folder, audit.LOCK)
    open(lock, "w").close()
    assert log.send(NOW).busy and not os.path.exists(tmp_path / "share")
    old = time.time() - audit.STALE_LOCK - 10
    os.utime(lock, (old, old))  # left by a QGIS that stopped while sending
    assert len(log.send(NOW).files) == 1 and not os.path.exists(lock)


def test_old_days_are_forgotten_once_sent(tmp_path):
    log = make_log(tmp_path, folder=str(tmp_path / "share"), keep_days=30)
    os.makedirs(log.folder)
    old, older = os.path.join(log.folder, "audit-20260901.jsonl"), os.path.join(log.folder, "audit-20260902.jsonl")
    for path in (old, older):
        with open(path, "w", encoding="utf-8") as f:
            f.write('{"session":"x","seq":1}\n')
    # Not sent yet (the folder can not be made): kept.
    (tmp_path / "file").write_text("x")
    log.configure(AuditSettings(folder=str(tmp_path / "file" / "sub"), keep_days=30))
    assert log.send(NOW).error
    assert os.path.exists(old) and os.path.exists(older)
    log.configure(AuditSettings(folder=str(tmp_path / "share"), keep_days=30))
    log._tidied = ""
    log.send(NOW)
    assert not os.path.exists(old) and not os.path.exists(older)
    # Without a folder: forgotten after keepDays all the same.
    no_folder = make_log(tmp_path / "b", keep_days=30)
    os.makedirs(no_folder.folder)
    path = os.path.join(no_folder.folder, "audit-20260901.jsonl")
    open(path, "w").close()
    no_folder.send(NOW)
    assert not os.path.exists(path)


# ---------------------------------------------------------------- checking

def test_verify_finds_lines_taken_out_or_changed(tmp_path):
    log = make_log(tmp_path)
    for i in range(5):
        audit.record("open", "image", f"IMG{i}")
    log.write_queued()
    lines = lines_of(day_file(log))
    ok, _ = verify((f"a:{i}", x) for i, x in enumerate(lines + lines[1:3]))  # sent twice: fine
    assert not ok[0].problems and ok[0].lines == 5 and not ok[0].ended
    gone, _ = verify((f"a:{i}", x) for i, x in enumerate(lines[:2] + lines[3:]))
    assert gone[0].problems == ["3 番の行がない"]
    changed = list(lines)
    changed[1] = changed[1].replace("IMG1", "IMG9")
    altered, _ = verify((f"a:{i}", x) for i, x in enumerate(changed))
    assert len(altered[0].problems) == 1 and "2 番の行が書き換えられている" in altered[0].problems[0]
    head, _ = verify((f"a:{i}", x) for i, x in enumerate(lines[2:]))
    assert head[0].problems == ["1〜2 番の行がない"]
    _sessions, bad = verify([("a:1", "{not json")])
    assert bad == ["a:1"]


def test_the_command_line_check(tmp_path, capsys):
    log = make_log(tmp_path, folder=str(tmp_path / "share"))
    audit.record("session", "start")
    audit.record("session", "end")
    log.write_queued()
    log.send(NOW)
    assert audit.main([str(tmp_path / "share")]) == 0
    text = capsys.readouterr().out
    assert "問題 0 件" in text and "[OK] s1" in text and "終了の行なし" not in text
    with open(os.path.join(tmp_path / "share", os.listdir(tmp_path / "share")[0]), "a", encoding="utf-8") as f:
        f.write(json.dumps({"session": "s1", "seq": 4, "kind": "open"}) + "\n")
    assert audit.main([str(tmp_path / "share")]) == 1


# ---------------------------------------------------------------- the threads

def test_recording_never_waits_for_the_file_or_the_folder(tmp_path, monkeypatch):
    """The caller only queues: a writer stuck on a slow disk does not slow `record` (the screen stays light)."""
    stuck = threading.Event()
    real = audit.append_lines

    def slow(*args):
        stuck.wait(5)
        real(*args)

    monkeypatch.setattr(audit, "append_lines", slow)
    log = make_log(tmp_path, folder=str(tmp_path / "share"))
    log.start(first_send=3600)
    try:
        audit.record("session", "start")
        started = time.perf_counter()
        for i in range(2000):
            audit.record("edit", "save", URL, ids=[i], file=str(tmp_path / "nothing.csv"))
        took = time.perf_counter() - started
        assert took < 0.5, took
    finally:
        stuck.set()
        log.stop(wait=5)
    lines = lines_of(day_file(log))
    assert len(lines) == 2001 and json.loads(lines[-1])["seq"] == 2001
    # Stopping sent what was written.
    assert len(sent_lines(tmp_path / "share")) == 2001


def test_lines_wait_for_the_settings_file(tmp_path):
    log = AuditLog(str(tmp_path / "default"), session="s2", computer="PC", user="u")
    audit.set_log(log)
    log.start(first_send=3600)
    try:
        audit.record("session", "start")
        time.sleep(0.2)
        assert not os.path.exists(tmp_path / "default")
        log.configure(AuditSettings(local_folder=str(tmp_path / "chosen"), format="csv"))
        audit.record("config", "read", "C:/config.json")
        log.send_soon()
        deadline = time.time() + 5
        while time.time() < deadline and not os.path.exists(tmp_path / "chosen"):
            time.sleep(0.05)
    finally:
        log.stop(wait=5)
    assert not os.path.exists(tmp_path / "default")
    assert len(lines_of(day_file(log, "csv"))) == 3  # the header and both lines


def test_long_edits_are_not_read_back(tmp_path, monkeypatch):
    """A big transfer's features are not parsed again in the writer (json.loads would hold Python's lock): the answer counts them."""
    monkeypatch.setattr(audit, "MAX_PARSE", 100)
    log = make_log(tmp_path, values=True)
    client = EsriClient(Server())
    client.apply_edits(URL, adds=[{"attributes": {"NAME": "x" * 50}} for _ in range(3)])
    with pytest.raises(EsriError):
        EsriClient(Server(fail="busy")).apply_edits(URL, adds=[{"attributes": {"NAME": "x" * 50}} for _ in range(3)])
    log.write_queued()
    ok, failed = (json.loads(x) for x in lines_of(day_file(log)))
    assert (ok["count"], ok["ids"], "values" in ok["detail"]) == (3, [100, 101, 102], False) and ok["detail"]["bytes"] > 100
    assert failed["result"] == "error" and "count" not in failed and failed["detail"]["bytes"] > 100
