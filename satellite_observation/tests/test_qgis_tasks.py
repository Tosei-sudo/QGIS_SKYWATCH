"""Requests through QGIS in a task: answers, errors, and a task cancelled while its request waits ends at once (a dock closed, the plugin unloaded)."""
import socket
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

pytest.importorskip("qgis.core")
from qgis.testing import start_app  # noqa: E402

APP = start_app()

from satellite_observation.qgis_io.network import get_bytes, qgis_transport  # noqa: E402
from satellite_observation.qgis_io.tasks import run_task  # noqa: E402
from satellite_observation.tests.qgis_fakes import wait_for  # noqa: E402


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
