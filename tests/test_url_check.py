import shutil
import ssl
import subprocess
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from claude_ops import url_check as u


@pytest.mark.parametrize("url, word", [
    ("http://example.org/", "only https"),
    ("ftp://example.org/x", "only https"),
    ("example.org", "https address"),
    ("https:///path", "host"),
    ("https://user:secret@example.org/", "user name or password"),
    ("https://exa mple.org/", "https address"),
    ("", "https address"),
    ("https://example.org:port/", "cannot read"),
])
def test_an_address_that_can_never_pass_is_refused_before_anything_is_sent(url, word):
    assert word in u.problem(url)
    with pytest.raises(u.NotHttps):
        u.ok(url)


def test_a_plain_https_address_has_no_problem():
    assert u.problem("https://example.org/a?b=1") is None


class Stub(BaseHTTPRequestHandler):
    seen: list = []

    def log_message(self, *args):
        pass

    def do_GET(self):
        Stub.seen.append(dict(self.headers))
        port = self.server.server_address[1]
        if self.path == "/ok":
            self.send_response(200)
            self.send_header("Set-Cookie", "session=abc")
            self.end_headers()
            self.wfile.write(b"ok")
        elif self.path == "/missing":
            self.send_error(404)
        elif self.path == "/slow":
            time.sleep(2)
            self.send_response(200)
            self.end_headers()
        elif self.path == "/to-http":
            self.send_response(302)
            self.send_header("Location", f"http://127.0.0.1:{port}/ok")
            self.end_headers()
        elif self.path == "/to-https":
            self.send_response(302)
            self.send_header("Location", f"https://127.0.0.1:{port}/ok")
            self.end_headers()


@pytest.fixture(scope="module")
def server(tmp_path_factory):
    if not shutil.which("openssl"):
        pytest.skip("openssl is needed to make the stub's certificate")
    d = tmp_path_factory.mktemp("tls")
    cert, key = d / "cert.pem", d / "key.pem"
    subprocess.run(["openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-days", "1",
                    "-subj", "/CN=127.0.0.1", "-addext", "subjectAltName=IP:127.0.0.1",
                    "-keyout", str(key), "-out", str(cert)], check=True, capture_output=True)
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), Stub)
    httpd.daemon_threads = True
    serving = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    serving.load_cert_chain(cert, key)
    httpd.socket = serving.wrap_socket(httpd.socket, server_side=True)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    trust = ssl.create_default_context(cafile=str(cert))
    yield f"https://127.0.0.1:{httpd.server_address[1]}", trust
    httpd.shutdown()


def test_a_200_passes_and_says_so(server):
    base, trust = server
    assert u.ok(f"{base}/ok", context=trust) == (True, "200 from 127.0.0.1")


def test_the_request_carries_no_cookie_or_credentials(server):
    base, trust = server
    Stub.seen.clear()
    u.ok(f"{base}/ok", context=trust)
    u.ok(f"{base}/ok", context=trust)
    for headers in Stub.seen:
        names = {k.lower() for k in headers}
        assert "cookie" not in names and "authorization" not in names
        assert headers.get("User-Agent") == u.USER_AGENT


def test_a_404_does_not_pass_and_names_the_status(server):
    base, trust = server
    assert u.ok(f"{base}/missing", context=trust) == (False, "404 from 127.0.0.1")


def test_a_slow_site_times_out_within_the_timeout(server):
    base, trust = server
    started = time.monotonic()
    ok, why = u.ok(f"{base}/slow", timeout=0.5, context=trust)
    assert not ok and why == "timed out after 0.5 s"
    assert time.monotonic() - started < 1.9


def test_a_redirect_is_followed_only_to_https(server):
    base, trust = server
    assert u.ok(f"{base}/to-https", context=trust) == (True, "200 from 127.0.0.1")
    ok, why = u.ok(f"{base}/to-http", context=trust)
    assert not ok and why.startswith("redirected to a non-https address, http://127.0.0.1:")


def test_a_certificate_it_does_not_trust_is_no_answer(server):
    base, _ = server
    ok, why = u.ok(f"{base}/ok")
    assert not ok and "127.0.0.1 did not answer" in why


def test_a_refused_connection_is_no_answer():
    import socket
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    ok, why = u.ok(f"https://127.0.0.1:{port}/", timeout=2)
    assert not ok and why.startswith("127.0.0.1 did not answer")
