"""Readings of a service. Only throwaway listeners the test opens are connected to."""
import os
import shutil
import socket
import subprocess
import tempfile

import pytest

from claude_ops import service_check as sc


def _run(out="", code=0):
    return lambda *a, **k: subprocess.CompletedProcess(a, code, stdout=out, stderr="")


def test_a_listening_port_is_up():
    s = socket.socket(); s.bind(("127.0.0.1", 0)); s.listen(1)
    try:
        r = sc.check(sc.Spec("t", port=s.getsockname()[1]), run=_run())
    finally:
        s.close()
    assert (r.state, r.answers) == (sc.UP, True)


def test_a_closed_port_is_down():
    s = socket.socket(); s.bind(("127.0.0.1", 0)); port = s.getsockname()[1]; s.close()
    r = sc.check(sc.Spec("t", port=port), run=_run())
    assert (r.state, r.answers) == (sc.DOWN, False)


def test_a_unix_socket_is_read_the_same_way():
    d = tempfile.mkdtemp(dir="/tmp")
    path = os.path.join(d, "s")
    s = socket.socket(socket.AF_UNIX); s.bind(path); s.listen(1)
    try:
        assert sc.check(sc.Spec("t", socket_path=path), run=_run()).state == sc.UP
        s.close()
        assert sc.check(sc.Spec("t", socket_path=path), run=_run()).state == sc.DOWN
    finally:
        s.close()
        shutil.rmtree(d, ignore_errors=True)


def test_nothing_to_ask_is_unknown_and_never_down():
    assert sc.check(sc.Spec("t", label="x"), run=_run("pid = 5")).state == sc.UNKNOWN


def test_launchd_running_does_not_make_a_silent_service_up():
    s = socket.socket(); s.bind(("127.0.0.1", 0)); port = s.getsockname()[1]; s.close()
    r = sc.check(sc.Spec("t", label="x", domain="system", port=port), run=_run("state = running\n\tpid = 99\n"))
    assert (r.launchd, r.state) == ("running", sc.DOWN)


def test_launchd_states():
    assert sc.launchd_state(sc.Spec("t", label="x"), _run("pid = 7")) == "running"
    assert sc.launchd_state(sc.Spec("t", label="x"), _run("state = waiting")) == "waiting"
    assert sc.launchd_state(sc.Spec("t", label="x"), _run("", 113)) == "not loaded"
    assert sc.launchd_state(sc.Spec("t", label="x"), _run("", 1)) is None
    assert sc.launchd_state(sc.Spec("t"), _run()) is None


def test_a_port_and_a_socket_together_are_refused():
    with pytest.raises(ValueError):
        sc.answers(sc.Spec("t", port=1, socket_path="/x"))


def _http_server(status):
    import http.server
    import threading

    class H(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(status if self.path == "/health" else 404)
            self.end_headers()

        def log_message(self, *a):
            pass

    srv = http.server.HTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv


@pytest.mark.parametrize("status,expected", [(200, True), (204, True), (500, False)])
def test_an_http_path_answers_only_on_a_2xx_status(status, expected):
    srv = _http_server(status)
    try:
        spec = sc.Spec("web", port=srv.server_port, http_path="/health")
        assert sc.answers(spec, timeout=2) is expected
    finally:
        srv.shutdown()
        srv.server_close()


def test_an_unknown_http_path_is_down():
    srv = _http_server(200)
    try:
        assert sc.answers(sc.Spec("web", port=srv.server_port, http_path="/other"), timeout=2) is False
    finally:
        srv.shutdown()
        srv.server_close()


def test_a_listener_that_never_speaks_http_is_down_for_an_http_path():
    # a port forward to a dead program accepts the connection and then says nothing
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    s.listen(5)
    try:
        spec = sc.Spec("fwd", port=s.getsockname()[1], http_path="/health")
        assert sc.answers(spec, timeout=0.5) is False
        assert sc.answers(sc.Spec("fwd", port=s.getsockname()[1]), timeout=0.5) is True
    finally:
        s.close()


def test_an_http_path_without_a_port_is_refused():
    with pytest.raises(ValueError):
        sc.answers(sc.Spec("x", socket_path="/tmp/none.sock", http_path="/health"))
