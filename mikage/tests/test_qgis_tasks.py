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
    assert kind == "failed" and message == "サーバーの中でエラー［E103］"
    kind, message = in_task(lambda: get_bytes(http + "/missing"))
    assert kind == "failed" and message == "サーバーが要求を受け付けない［E104］"
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
    assert kind == "failed" and message == "サーバーの応答がない（時間切れ）［E102］"
    assert time.monotonic() - begun < 5
    # Its thread ends at once (the event loop QGIS waited in was quit), and is let go.
    assert wait_for(lambda: all(e.wait(0) for e in network._ending), 5000)


def test_an_authentication_that_cannot_be_used_is_told(http):
    kind, message = in_task(lambda: get_bytes(http + "/ok", "nosuchcfg"))
    assert (kind, message) == ("failed", "サインインの設定を使えない［E204］")


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


def test_a_task_started_while_an_answer_is_shown_runs_once_it_is_shown():
    """
    2026-11-26: a task started in another task's answer (データに質問 asking
    again after its SQL failed) stayed queued for good once in a few runs
    under load: QGIS's task manager, cleaning up the task that finished, took
    back the new task's runnable (at the address of the one it had kept). It is
    handed to QGIS once the answer has been shown; one cancelled meanwhile is
    told so, as QGIS tells it.
    """
    from qgis.core import QgsApplication

    from mikage.qgis_io import tasks

    seen, chained = [], []

    def first_done(result):
        seen.append(result)
        # Not given to QGIS while its answer is shown.
        chained.append(run_task("次", lambda t: "次の答え", seen.append))
        assert QgsApplication.taskManager().taskId(chained[0]) == -1 and chained[0] in tasks._after_answer
        dropped = run_task("取り消す", lambda t: seen.append("走った"), seen.append, lambda m: seen.append(f"失敗: {m}"))
        dropped.cancel()

    run_task("最初", lambda t: "最初の答え", first_done)
    assert wait_for(lambda: "次の答え" in seen and "失敗: 取消" in seen)
    assert seen[0] == "最初の答え" and "走った" not in seen and tasks._after_answer == []
    # Unloaded while one waits: dropped with the others.
    def unloaded_meanwhile(_result):
        run_task("待つ", lambda t: seen.append("待った"))
        tasks.cancel_all()

    run_task("最初", lambda t: 1, unloaded_meanwhile)
    wait_for(lambda: False, 300)
    assert "待った" not in seen and tasks._after_answer == []


class KeepAlive(Handler):
    """HTTP/1.1: the connection stays open between requests (as ArcGIS Server's, IIS's)."""

    protocol_version = "HTTP/1.1"
    connections = 0

    def setup(self):
        type(self).connections += 1
        super().setup()


@pytest.fixture
def keep_alive():
    from mikage.qgis_io import network

    # As while the plugin is loaded (an earlier test's plugin, unloaded, stopped keeping lines).
    network.open_lines()
    KeepAlive.connections = 0
    server = ThreadingHTTPServer(("127.0.0.1", 0), KeepAlive)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{server.server_address[1]}"
    server.shutdown()
    server.server_close()


def test_requests_keep_their_connection(keep_alive):
    """
    2026-11-18: each request had a thread, and so a connection, of its own (a new TCP connection, TLS handshake
    and Windows sign-in each time on a far or busy server). They now go on the connection the last one opened.
    """
    from mikage.qgis_io import network

    def many():
        return [get_bytes(f"{keep_alive}/ok?n={n}") for n in range(8)] + [qgis_transport()(keep_alive + "/ok", {"n": "x"})]

    kind, answers = in_task(many)
    assert kind == "ok" and answers == [f"got /ok?n={n}".encode() for n in range(8)] + [b"posted n=x"]
    assert KeepAlive.connections == 1
    # Another task later: the same connection still.
    assert in_task(lambda: get_bytes(keep_alive + "/ok")) == ("ok", b"got /ok")
    assert KeepAlive.connections == 1
    assert network._idle


def test_a_request_given_up_on_does_not_take_its_line_again(silent_server, keep_alive, monkeypatch):
    from mikage.qgis_io import network

    url, _connections = silent_server
    monkeypatch.setattr(network, "_time_limit_ms", lambda url: 300)
    assert in_task(lambda: get_bytes(url))[0] == "failed"
    # Its line (whose event loops were quit) is let go; the next request works on another.
    assert wait_for(lambda: all(e.wait(0) for e in network._ending), 5000)
    assert in_task(lambda: get_bytes(keep_alive + "/ok")) == ("ok", b"got /ok")


def test_lines_end_when_the_plugin_is_unloaded(keep_alive):
    from mikage.qgis_io import network

    assert in_task(lambda: get_bytes(keep_alive + "/ok"))[0] == "ok"
    kept = list(network._idle)
    assert kept
    network.close_lines()
    try:
        assert network._idle == [] and all(line.wait(0) for line in kept)
        # Unloaded: a request still works (a task ending late), its line is not kept.
        assert in_task(lambda: get_bytes(keep_alive + "/ok"))[0] == "ok"
        assert network._idle == []
    finally:
        network.open_lines()


def test_progress_is_not_sent_for_every_row(monkeypatch):
    """Work telling its progress for each row (GDAL does) sent a message to the main thread each time."""
    from mikage.qgis_io.tasks import FunctionTask

    shown = []
    monkeypatch.setattr(FunctionTask, "setProgress", lambda self, p: shown.append(p))
    task = FunctionTask("行ごと", lambda t: None)
    for i in range(10001):
        task.progress(i / 100)
    assert shown[0] == 0.0 and shown[-1] == 100.0
    assert 100 <= len(shown) <= 110
    assert shown == sorted(shown)


def test_garbage_collected_less_often_while_loaded(monkeypatch):
    import gc

    from mikage.qgis_io import tasks

    monkeypatch.setattr(tasks, "_collect_before", None)
    before = gc.get_threshold()
    try:
        gc.set_threshold(700, 10, 10)
        tasks.collect_less_often()
        assert gc.get_threshold() == (tasks.COLLECT_AFTER, 10, 10)
        tasks.collect_less_often()  # loaded again: the one from before the plugin is kept
        tasks.restore_collection()
        assert gc.get_threshold() == (700, 10, 10)
        # Rarer already (someone else's), or turned off: left as it is.
        for kept in ((50_000, 10, 10), (0, 10, 10)):
            gc.set_threshold(*kept)
            tasks.collect_less_often()
            assert gc.get_threshold() == kept
            tasks.restore_collection()
            assert gc.get_threshold() == kept
    finally:
        gc.set_threshold(*before)
