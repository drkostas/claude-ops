import shutil
import ssl
import subprocess
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from claude_ops import bearer_check as b
from claude_ops import url_check

GOOD = "tok_" + "good" * 4
OLD = "tok_" + "old0" * 4


class Stub(BaseHTTPRequestHandler):
    seen: list = []

    def log_message(self, *args):
        pass

    def do_GET(self):
        Stub.seen.append(dict(self.headers))
        sent = self.headers.get("Authorization", "")
        if self.path == "/user":
            ok = sent == f"Bearer {GOOD}"
            self.send_response(200 if ok else 401)
            if ok:
                self.send_header("X-OAuth-Scopes", "repo, read:org")
            self.send_header("X-Other", "secret-looking")
            self.end_headers()
            self.wfile.write(b"body " + sent.encode())
        elif self.path == "/redirect":
            self.send_response(302)
            self.send_header("Location", "https://example.org/elsewhere")
            self.end_headers()
        elif self.path == "/slow":
            time.sleep(2)
            self.send_response(200)
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
    yield f"https://127.0.0.1:{httpd.server_address[1]}", ssl.create_default_context(cafile=str(cert))
    httpd.shutdown()


def test_the_right_value_is_a_200_and_a_wrong_one_a_401(server):
    base, trust = server
    assert b.status(base + "/user", GOOD, context=trust).status == 200
    assert b.status(base + "/user", OLD, context=trust).status == 401


def test_only_the_named_header_comes_back_and_none_when_not_asked(server):
    base, trust = server
    a = b.status(base + "/user", GOOD, header="X-OAuth-Scopes", context=trust)
    assert a.header == "repo, read:org"
    assert b.status(base + "/user", GOOD, context=trust).header is None
    assert b.status(base + "/user", OLD, header="X-OAuth-Scopes", context=trust).header is None


def test_the_value_is_in_no_field_why_or_repr_of_the_answer(server):
    base, trust = server
    for token in (GOOD, OLD):
        a = b.status(base + "/user", token, header="X-Other", context=trust)
        assert token not in repr(a) and token not in a.why


def test_the_value_goes_in_one_authorization_header_and_no_cookie(server):
    base, trust = server
    Stub.seen.clear()
    b.status(base + "/user", GOOD, context=trust)
    assert Stub.seen[-1]["Authorization"] == f"Bearer {GOOD}"
    assert "Cookie" not in Stub.seen[-1]


def test_a_redirect_is_an_answer_and_is_never_followed(server):
    base, trust = server
    a = b.status(base + "/redirect", GOOD, context=trust)
    assert a.status == 302


def test_a_slow_server_is_cut_off_at_the_total_timeout(server):
    base, trust = server
    t = time.monotonic()
    a = b.status(base + "/slow", GOOD, timeout=0.5, context=trust)
    assert a.status is None and "timed out" in a.why and time.monotonic() - t < 1.5


def test_nothing_listening_is_no_status_and_names_no_value():
    a = b.status("https://127.0.0.1:9/user", GOOD, timeout=2)
    assert a.status is None and GOOD not in a.why


@pytest.mark.parametrize("url", ["http://example.org/", "https://user:x@example.org/", "example.org", ""])
def test_an_address_that_can_never_pass_is_refused_before_anything_is_sent(url):
    with pytest.raises(url_check.NotHttps):
        b.status(url, GOOD)


@pytest.mark.parametrize("value", ["", "xq zq", "a\r\nX-Evil: 1", "tok\x00", "tök", "tab\tx"])
def test_a_value_that_could_carry_a_second_header_is_refused_and_never_repeated(value):
    with pytest.raises(ValueError) as e:
        b.status("https://example.org/", value)
    assert value.strip() == "" or value not in str(e.value)
