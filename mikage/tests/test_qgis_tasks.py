"""Requests through QGIS in a task: answers, errors, and a task cancelled while its request waits ends at once (a dock closed, the plugin unloaded)."""
import socket
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

pytest.importorskip("qgis.core")
from qgis.testing import start_app  # noqa: E402

APP = start_app()

from mikage.qgis_io.network import get_bytes, qgis_transport  # noqa: E402
from mikage.qgis_io.tasks import run_task  # noqa: E402
from mikage.tests.qgis_fakes import wait_for  # noqa: E402


@pytest.fixture
def silent_server():
    """A server that takes connections and never answers."""
    server = socket.socket()
    server.bind(("127.0.0.1", 0))
    server.listen()
    held = []
    stop = threading.Event()

    def accept():
        server.settimeout(0.2)
        while not stop.is_set():
            try:
                held.append(server.accept()[0])
            except OSError:
                pass

    thread = threading.Thread(target=accept, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{server.getsockname()[1]}/never", held
    stop.set()
    thread.join()
    for c in held:
        c.close()
    server.close()


def test_a_request_stops_with_its_task(silent_server):
    url, connections = silent_server
    ended = []

    def work(task):
        return get_bytes(url)

    task = run_task("止まる要求", work, lambda r: ended.append("done"), lambda m: ended.append(m))
    # Cancelled while the request waits for an answer.
    assert wait_for(lambda: connections)
    begun = time.monotonic()
    task.cancel()
    assert wait_for(lambda: ended, 5000)
    assert time.monotonic() - begun < 5
    assert ended != ["done"]


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def answer(self, body: bytes):
        path = self.path.split("?", 1)[0]
        if path == "/moved":
            self.send_response(302)
            self.send_header("Location", "/ok")
            self.end_headers()
            return
        status = {"/ok": 200, "/broken": 500}.get(path, 404)
        self.send_response(status)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):  # noqa: N802
        self.answer(b"got " + self.path.encode())

    def do_POST(self):  # noqa: N802
        self.answer(b"posted " + self.rfile.read(int(self.headers["Content-Length"])))


@pytest.fixture
def http():
    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{server.server_address[1]}"
    server.shutdown()
    server.server_close()


def in_task(work):
    out = []
    run_task("要求", lambda task: work(), lambda r: out.append(("ok", r)), lambda m: out.append(("failed", m)))
    assert wait_for(lambda: out)
    return out[0]


def test_answers_and_errors(http):
    assert in_task(lambda: get_bytes(http + "/ok")) == ("ok", b"got /ok")
    assert in_task(lambda: get_bytes(http + "/moved")) == ("ok", b"got /ok")
    assert in_task(lambda: qgis_transport()(http + "/ok", {"f": "json"})) == ("ok", b"posted f=json")
    kind, message = in_task(lambda: get_bytes(http + "/broken?token=secret"))
    assert kind == "failed" and message.startswith("サーバーのエラー（" + http + "/broken") and "secret" not in message
    kind, message = in_task(lambda: get_bytes(http + "/missing"))
    assert kind == "failed" and message.startswith("接続不可（")
    with pytest.raises(RuntimeError):
        get_bytes(http + "/ok")


def test_unloading_cancels_the_tasks_and_drops_their_answers(silent_server):
    """#88: the plugin's tasks stop when it is unloaded, and nothing is told to the docks that are gone."""
    from qgis.core import QgsApplication

    from mikage.qgis_io import tasks

    url, connections = silent_server
    told = []
    waiting = run_task("止まる要求", lambda task: get_bytes(url), lambda r: told.append("done"), lambda m: told.append(m))

    def busy(task):
        while True:
            task.check()
            time.sleep(0.01)

    looping = run_task("計算", busy, lambda r: told.append("done"), lambda m: told.append(m))
    assert wait_for(lambda: connections and looping.status() == looping.TaskStatus.Running)
    begun = time.monotonic()
    assert tasks.cancel_all() >= 2
    assert time.monotonic() - begun < 5
    assert waiting.discarded and looping.discarded
    assert wait_for(lambda: QgsApplication.taskManager().countActiveTasks() == 0, 5000)
    assert not wait_for(lambda: told, 300) and told == []


def test_an_answer_for_a_closed_window_is_not_an_error():
    """#88: a done() that touches a widget already deleted is logged as a note, not a critical error."""
    from qgis.core import Qgis, QgsApplication
    from qgis.PyQt import sip
    from qgis.PyQt.QtWidgets import QLabel

    label = QLabel()
    sip.delete(label)
    levels = []

    def log(message, tag, level):
        if tag == "MIKAGE":
            levels.append(level)

    QgsApplication.messageLog().messageReceived.connect(log)
    try:
        task = run_task("閉じた画面", lambda task: 1, lambda r: label.setText("済"))
        assert wait_for(lambda: levels)
    finally:
        QgsApplication.messageLog().messageReceived.disconnect(log)
    assert levels == [Qgis.MessageLevel.Info] and not task.discarded


def test_gil_handed_on_sooner_while_loaded(monkeypatch):
    import sys

    from mikage.qgis_io import tasks

    monkeypatch.setattr(tasks, "_switch_before", None)
    before = sys.getswitchinterval()
    try:
        sys.setswitchinterval(0.005)
        tasks.share_gil_faster()
        assert sys.getswitchinterval() == pytest.approx(tasks.SWITCH_INTERVAL)
        tasks.share_gil_faster()  # loaded again: the one from before the plugin is kept
        tasks.restore_gil_sharing()
        assert sys.getswitchinterval() == pytest.approx(0.005)
        # Shorter already (someone else's): left as it is.
        sys.setswitchinterval(0.0005)
        tasks.share_gil_faster()
        assert sys.getswitchinterval() == pytest.approx(0.0005)
        tasks.restore_gil_sharing()
        assert sys.getswitchinterval() == pytest.approx(0.0005)
    finally:
        sys.setswitchinterval(before)


def test_slow_result_is_told_in_the_log(monkeypatch):
    from qgis.core import QgsApplication

    from mikage.qgis_io import tasks

    monkeypatch.setattr(tasks, "SLOW_MS", 30)
    heard = []
    log = QgsApplication.messageLog()

    def got(message, tag, _level):
        if tag == tasks.LOG_TAG:
            heard.append(message)

    log.messageReceived.connect(got)
    try:
        done = []
        run_task("速い表示", lambda task: 1, done.append)
        run_task("遅い表示", lambda task: 2, lambda r: (time.sleep(0.06), done.append(r)))
        assert wait_for(lambda: sorted(done) == [1, 2], 5000)
        APP.processEvents()
        assert any(m.startswith("遅い表示: 結果の表示に ") for m in heard)
        assert not any(m.startswith("速い表示:") for m in heard)
    finally:
        log.messageReceived.disconnect(got)


def test_requests_while_widgets_are_deleted(http):
    """
    Requests in tasks while the main thread deletes widgets with Python slots (docks and dialogs closing).
    The request was driven from Python in its thread: it held Python's lock while Qt took its own, and the main
    thread, deleting a widget, held one of those and waited for Python's, so QGIS could freeze. Its short-lived
    Python wrappers were also taken for new objects at the same address ("wrapped C/C++ object … has been
    deleted", answers "cut off"); both showed within seconds under this load.
    """
    from qgis.PyQt.QtCore import QTimer
    from qgis.PyQt.QtWidgets import QLineEdit, QPushButton, QWidget

    stop = threading.Event()
    answers, failures = [], []

    def ask(task, n):
        while not stop.is_set():
            got = get_bytes(f"{http}/ok?n={n}") if n % 2 else qgis_transport()(http + "/ok", {"n": str(n)})
            answers.append(got == (f"got /ok?n={n}".encode() if n % 2 else f"posted n={n}".encode()) or got)
        return n

    ended = []
    for n in range(4):
        run_task(f"要求 {n}", lambda task, n=n: ask(task, n), ended.append, failures.append)
    finish = time.monotonic() + 2.5
    while time.monotonic() < finish:
        for _ in range(20):
            sender = QTimer()
            panel = QWidget()
            button, line = QPushButton("x", panel), QLineEdit(panel)
            button.clicked.connect(lambda *_: None)
            line.textChanged.connect(lambda *_: None)
            sender.timeout.connect(line.clear)
            panel.destroyed.connect(lambda *_: None)
            panel.deleteLater()
        APP.processEvents()
    stop.set()
    assert wait_for(lambda: len(ended) + len(failures) == 4, 30000)
    assert failures == [] and sorted(ended) == [0, 1, 2, 3]
    assert len(answers) > 20 and all(a is True for a in answers), [a for a in answers if a is not True][:3]


def test_a_request_that_takes_too_long_gives_up(silent_server, monkeypatch):
    from mikage.qgis_io import network

    url, connections = silent_server
    monkeypatch.setattr(network, "_time_limit_ms", lambda url: 300)
    begun = time.monotonic()
    kind, message = in_task(lambda: get_bytes(url + "?token=secret"))
    assert kind == "failed" and message == f"応答なし（タイムアウト）（{url}）"
    assert time.monotonic() - begun < 5
    # Its thread ends at once (the event loop QGIS waited in was quit), and is let go.
    assert wait_for(lambda: all(e.wait(0) for e in network._ending), 5000)


def test_an_authentication_that_cannot_be_used_is_told(http):
    kind, message = in_task(lambda: get_bytes(http + "/ok", "nosuchcfg"))
    assert (kind, message) == ("failed", f"認証設定（nosuchcfg）を使えず（{http}/ok）")


def test_tasks_wait_while_qgis_starts():
    """
    2026-11-14: a task's Python running while QGIS still imported another plugin crashed QGIS (signal 11 in sip):
    the plugin's tasks made while QGIS starts wait until it has.
    """
    from mikage.qgis_io import tasks

    ran, cancelled_ran = [], []
    tasks.hold_tasks()
    try:
        task = run_task("起動中", lambda t: ran.append(1) or 1, lambda r: ran.append(r))
        cancelled = run_task("起動中に取り消し", lambda t: cancelled_ran.append(1))
        wait_for(lambda: False, 300)
        assert ran == [] and task in tasks._held
        cancelled.cancel()
    finally:
        tasks.release_tasks()
    assert wait_for(lambda: ran == [1, 1])
    wait_for(lambda: False, 200)
    assert cancelled_ran == [] and tasks._held == []
    # Not holding any more: started at once.
    after = []
    run_task("起動後", lambda t: 2, after.append)
    assert wait_for(lambda: after == [2])


def test_unloading_drops_the_tasks_held():
    from mikage.qgis_io import tasks

    ran = []
    tasks.hold_tasks()
    run_task("起動中", lambda t: ran.append(1))
    started = time.monotonic()
    tasks.cancel_all()
    tasks.release_tasks()
    assert time.monotonic() - started < 1
    wait_for(lambda: False, 300)
    assert ran == [] and tasks._held == [] and not tasks._holding
